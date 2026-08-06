"""Codegen engine: ExecutionPlan -> runnable RunBundle (Story 5.1).

This is the heart of the code-configuration builder. Given an ``ExecutionPlan``
(the artifact Story 1.3 / the plan synthesizer produces) it emits a **RunBundle**
-- a self-contained directory the execution adapter (Story 5.2) can run with no
manual editing:

    main.py           the generated, executable script (a template with the
                      plan's specifics substituted in)
    config.yaml       parameters, input/output paths, and the SLURM resource
                      request, for the scheduler and for reproducibility
    requirements.txt  pinned Python dependencies (see dependency_inferencer)
    inline_tests.py   smoke tests run *before* the real execution to catch a
                      missing dependency or a syntax error early

How a template becomes ``main.py``
----------------------------------
Templates in ``templates/`` are ordinary, valid Python modules that carry
UPPER_SNAKE placeholder tokens *inside string literals* (``TOOL_NAME =
"{TOOL_NAME}"``) plus two JSON blobs embedded in raw triple-quoted strings
(``_CONFIG_JSON``/``_ACCEPTANCE_JSON``). Because the tokens live in strings and
the heavy scientific import is lazy, a template is valid, importable Python both
*before* and *after* substitution -- so its embedded doctests run in any
environment. Rendering is a set of exact ``str.replace`` calls, and every render
is validated two ways: no placeholder token may remain, and the result must
``compile()``.

Two codegen paths:

* **Library-only (default, deterministic, offline).** Template selection is by
  tool: Pymatgen and ASE have bespoke templates; RDKit maps to
  property-prediction or graph-manipulation by objective; any other importable
  tool falls back to a generic runner. Material-aware templates bake the target
  system from the IntentSpec so the script analyses *that* material, not a
  hard-coded sample.
* **Calculator-driven (LLM-synthesized, general -- no per-property presets).**
  When the plan selected a python calculator (e.g. GPAW/DFTB+ for a band gap),
  the engine asks the LLM gateway for a ``main.py`` tailored to the *discovered*
  library + calculator + material + property. The reply is accepted only if it
  compiles, references the calculator, and has a runnable entrypoint; there is
  deliberately no hand-written, property-specific template. If the gateway is
  unavailable or returns invalid Python, the fallback is the tool-agnostic
  generic runner (loads the toolset, writes a stub) -- never a preset. Smoke-
  verifying and repairing that script is the job of the REPAIR stage
  (:class:`code_gen.script_doctor.ScriptDoctor`), which runs after BUILD.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

# The heavy calculator stack (GPAW/DFTB+/MatGL/...) lives in this pixi environment
# (Python 3.11), separate from the default env. A generated calculator bundle must
# therefore be run and verified there, not with the default interpreter.
SIM_ENV = "sim"


def _repo_root() -> Optional[Path]:
    """Walk up to the directory that holds pixi.toml (the repo root), or None."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pixi.toml").exists():
            return parent
    return None


def pixi_env_python(env_name: str = SIM_ENV, repo_root: Optional[Union[str, Path]] = None) -> Optional[str]:
    """Absolute path to the interpreter of a named pixi environment, or None.

    Returns None when the environment hasn't been materialized (``pixi install``
    not run) so callers can degrade gracefully instead of assuming it exists.
    """
    root = Path(repo_root) if repo_root else _repo_root()
    if root is None:
        return None
    base = root / ".pixi" / "envs" / env_name
    candidate = base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(candidate) if candidate.exists() else None


# Sibling modules -- import via the package alias when available (tests, the
# state machine), else by bare name when this file is run from its own dir.
try:  # pragma: no cover - import shim
    from code_gen import dependency_inferencer as _depinf
    from code_gen import smoke_test_generator as _smoke
except ImportError:  # pragma: no cover
    import dependency_inferencer as _depinf
    import smoke_test_generator as _smoke

canonical_tool_key = _depinf.canonical_tool_key

# Placeholder tokens the engine fills. Every one must be gone after rendering.
PLACEHOLDER_KEYS = [
    "TOOL_NAME", "TOOL_IMPORT", "MODEL_NAME", "INPUT_FILE", "OUTPUT_FILE",
    "CONFIG_FILE", "GENERATED_AT", "CONFIG_JSON", "ACCEPTANCE_JSON",
    # Material-aware standard templates (e.g. Pymatgen) bake the target crystal:
    "STRUCTURE_JSON",
]
# Any leftover ``{UPPER_SNAKE}`` token after rendering is an unfilled placeholder
# (a template bug). JSON object braces never match: ``{`` is always followed by
# ``"`` or ``}`` in our emitted JSON, not an uppercase letter.
_LEFTOVER_PLACEHOLDER = re.compile(r"\{[A-Z][A-Z0-9_]{2,}\}")

_CODE_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


# --------------------------------------------------------------------------- #
# Reusable static validation of a synthesized script.
#
# Shared by BUILD (this engine, validating initial synthesis) and the REPAIR
# stage (:class:`code_gen.script_doctor.ScriptDoctor`, validating each repaired
# candidate) so both apply the exact same acceptance bar. Kept here, engine-side,
# so ``script_doctor`` can depend on the engine without a circular import.
# --------------------------------------------------------------------------- #
def strip_code_fences(text: str) -> str:
    """Extract python from a possibly markdown-fenced agent reply.

    If the reply contains fenced code blocks, concatenate *all* of them (a model
    sometimes splits a script across blocks); otherwise use the raw text.
    Concatenating is safer than taking only the first block.

    >>> strip_code_fences("```python\\nx = 1\\n```").strip()
    'x = 1'
    >>> strip_code_fences("x = 1").strip()
    'x = 1'
    """
    stripped = text.strip()
    blocks = _CODE_FENCE.findall(stripped)
    if blocks:
        stripped = "\n".join(b.strip("\n") for b in blocks).strip()
    return stripped if stripped.endswith("\n") else stripped + "\n"


def has_runnable_entrypoint(source: str) -> bool:
    """Whether ``source`` actually *runs* something when executed.

    True when the module body has an ``if __name__ == "__main__":`` guard or a
    top-level call statement (e.g. ``main()``). A module that only defines
    functions/classes and never invokes them (the tell-tale of a truncated
    synthesis reply) returns False.

    >>> has_runnable_entrypoint("def main():\\n    pass\\nif __name__ == '__main__':\\n    main()")
    True
    >>> has_runnable_entrypoint("print('hi')")
    True
    >>> has_runnable_entrypoint("def main():\\n    pass")
    False
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"):
                return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return True
    return False


def validate_source(raw, calculator_import: Optional[str] = None):
    """``(source, None)`` for a usable script, else ``(None, reason)``.

    The reason matters: a rejected reply used to vanish into a bare ``None``, so
    a silent fall back to the do-nothing scaffold was indistinguishable from a
    gateway outage -- and the run went to the cluster either way (observed on
    Slurm job 2569967, which "succeeded" in 5 seconds having computed nothing).

    The entrypoint check catches a truncated reply: a script cut off mid-body
    often still *compiles* (its last partial line is a valid statement) and
    mentions the calculator, but defines functions it never calls -- so running
    it does nothing. ``no_entrypoint`` is therefore the fingerprint of hitting
    the token cap, which is why it is reported separately.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, "empty_reply"
    source = strip_code_fences(raw)
    try:
        compile(source, "main.py", "exec")
    except SyntaxError as exc:
        return None, f"syntax_error: {exc.msg} (line {exc.lineno})"
    if calculator_import and calculator_import.lower() not in source.lower():
        return None, f"never_references_{calculator_import}"
    if not has_runnable_entrypoint(source):
        # Compiles but calls nothing -- almost always a reply cut off at the cap.
        return None, "no_entrypoint (reply likely truncated at the token cap)"
    return source, None


def extract_valid_source(raw, calculator_import: Optional[str] = None) -> Optional[str]:
    """Fenced-or-raw agent reply -> a usable script, or ``None``.

    Thin wrapper over :func:`validate_source` for callers that only need the
    script (the REPAIR stage); BUILD wants the reason too.
    """
    return validate_source(raw, calculator_import)[0]


def _imported_modules(source: str) -> set:
    """Every module path ``source`` imports, at any nesting depth.

    Walks the tree rather than reading top-level statements only: generated
    scripts deliberately keep heavy imports inside functions so the module still
    imports where the engine is absent.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _satisfies(modules, want: str) -> bool:
    """Whether any module in ``modules`` covers ``want``, in either direction.

    ``import ase.calculators.nwchem`` satisfies a requirement on ``ase``, and
    ``import psi4`` satisfies one on ``psi4.driver`` -- either direction means the
    package has to be present.
    """
    return any(m == want or m.startswith(f"{want}.") or want.startswith(f"{m}.")
               for m in modules)


def _imports_module(source: str, want: str) -> bool:
    """Whether ``source`` imports ``want``, a parent of it, or a child of it."""
    return _satisfies(_imported_modules(source), want)


def _needed_imports(candidates, source: str) -> List[str]:
    """The toolset imports ``source`` actually uses, in order.

    Falls back to the full list when nothing matches: a script that imports none
    of its toolset is a scaffold or a synthesis failure, and weakening the gate to
    nothing there would let it through silently. The synthesis backstop is what
    catches that case, not this filter.
    """
    candidates = list(candidates)
    modules = _imported_modules(source)   # parse once, not once per candidate
    return [name for name in candidates if _satisfies(modules, name)] or candidates


_BUNDLE_HELPERS = Path(__file__).resolve().parent / "bundle_helpers"




# Property classes whose value is assembled from several species' energies, where
# a dropped term or a wrong stoichiometric coefficient yields a plausible number
# rather than a crash. Keyed on the QUANTITY, not on any engine or molecule.
_THERMO_CYCLE_WORDS = (
    "formation", "atomization", "dissociation", "reaction_enthalpy",
    "reaction enthalpy", "combustion", "hydrogenation", "binding_energy",
    "binding energy", "cohesive",
)


def wants_thermo_cycle(*fields) -> bool:
    """Whether any field names a property built from a multi-species cycle.

    >>> wants_thermo_cycle("standard_heat_of_formation_kJ_per_mol")
    True
    >>> wants_thermo_cycle("band_gap", None)
    False
    """
    text = " ".join(str(f).lower() for f in fields if f)
    return any(word in text for word in _THERMO_CYCLE_WORDS)


def _bundle_helper_source(name: str) -> str:
    """Source of a ``bundle_helpers`` module, copied verbatim into a bundle.

    Read from disk rather than templated: these are real modules, imported
    directly by their unit tests, so what a run executes is exactly what the
    tests cover. Deliberately uncached -- editing a helper should take effect
    without restarting the process.
    """
    return (_BUNDLE_HELPERS / f"{name}.py").read_text(encoding="utf-8")


def _first_metric_name(plan: dict) -> Optional[str]:
    """The first acceptance metric's name, or None.

    Stands in for a plan whose ``requested_property`` the planner left unset: the
    metric names what the run is for, so it is enough to decide that real code is
    wanted rather than a scaffold.
    """
    for metric in plan.get("acceptance_metrics") or []:
        if isinstance(metric, dict) and metric.get("metric_name"):
            return str(metric["metric_name"])
    return None


class SynthesisFailed(RuntimeError):
    """Code synthesis produced nothing usable and the caller needs a real script.

    Raised only when ``generate(require_synthesis=True)``: the run is about to
    execute, and the generic scaffold would burn the allocation computing nothing
    while reporting success. Carries the per-attempt reasons so the failure names
    a cause -- a gateway error, a reply that never mentioned the calculator, or a
    reply truncated at the token cap -- instead of leaving it to be guessed at.
    """

    def __init__(self, tool_name, calculator, agent_missing, synthesis):
        self.synthesis = synthesis or {}
        self.attempts = self.synthesis.get("attempts") or []
        target = calculator or tool_name
        if agent_missing:
            detail = "no LLM agent was available to write it"
        elif self.attempts:
            detail = "; ".join(
                f"attempt {a.get('attempt')}: {a.get('reason')}" for a in self.attempts)
        else:
            detail = "no attempt was recorded"
        super().__init__(
            f"could not generate a runnable {target} script, and this run is set "
            f"to execute -- refusing to submit the placeholder scaffold, which "
            f"would exit 0 having computed nothing. Reason(s): {detail}"
        )


@dataclass(frozen=True)
class TemplateSpec:
    """A template file plus the defaults used when rendering it."""

    filename: str
    model_name: str
    input_file: str
    output_file: str
    default_params: Dict[str, object] = field(default_factory=dict)
    # True for material-aware templates that fail loudly without a baked-in
    # structure (pymatgen analysis, ASE MD). When BUILD has no real structure to
    # bake, it routes these to LLM synthesis + the REPAIR self-heal loop instead
    # of rendering the fail-loud template.
    requires_structure: bool = False


# Tool (canonical key) -> template. RDKit is resolved to one of two templates by
# objective; unmapped tools fall back to the generic runner.
_PYMATGEN = TemplateSpec("template_pymatgen_analysis.py", "structure_analysis", "structure.json", "results.csv", {"round_digits": 4}, requires_structure=True)
_ASE = TemplateSpec("template_molecular_dynamics.py", "emt_md", "system.json", "trajectory.csv", {"steps": 20, "timestep_fs": 1.0, "temperature_K": 300.0}, requires_structure=True)
_RDKIT_PROPERTY = TemplateSpec("template_property_prediction.py", "rdkit_descriptors", "molecules.csv", "predictions.csv", {"round_digits": 4})
_RDKIT_MANIP = TemplateSpec("template_rdkit_manipulation.py", "graph_manipulation", "molecules.smi", "graph_features.csv", {"canonical": True})
_GENERIC = TemplateSpec("template_generic.py", "generic_run", "input.json", "results.csv", {})

# There is deliberately NO per-property template. A calculator-driven run (e.g.
# a band gap via GPAW/DFTB+) is written by the LLM from the discovered library +
# calculator + material + property; when the gateway is unavailable the fallback
# is the tool-agnostic generic runner above -- never a hand-written, property-
# specific preset.

_RDKIT_MANIP_HINTS = (
    "manipul", "scaffold", "graph", "canonical", "substructure",
    "fingerprint", "representation", "fragment",
)


# The --smoke instruction, chosen per calculator. For a cheap, self-contained tool
# (an ML predictor / semiempirical method with bundled parameters) the smoke runs
# the ACTUAL computation on a tiny system, so a wrong API call/keyword fails during
# REPAIR instead of the real run. For a heavy or external-data calculator (full DFT,
# or one needing pseudopotential/Slater-Koster files) it only builds the structure
# and loads the calculator, then writes a sentinel output row -- the real value can't
# be computed inside a fast smoke check, and forcing one would make --smoke as slow as
# the full run (which is what made a GPAW smoke time out). Both carry
# {output_file}/{property} placeholders and are pre-formatted before the prompt splice.
_SMOKE_COMPUTE = (
    '- Provide argparse with `--output` (CSV path, default "{output_file}") and '
    "`--smoke`. In --smoke mode, build the ACTUAL target system using the SAME "
    "structure-building code your full run uses -- never a smaller or simpler "
    "substitute material -- then "
    "run the real {property} computation once, end-to-end, at the CHEAPEST valid "
    "settings so it finishes quickly (aim for seconds, and well under a minute): "
    "minimal basis/cutoff, a single k-point, the fewest iterations/steps that still "
    "complete one real call. Reduce COST through settings ONLY, never by swapping in a "
    "different system, and never carry these smoke settings into the full run. INCLUDE "
    "the real prediction/compute call and read the returned value, then write it and "
    "exit. Do NOT stub, skip, or hard-code the computation in --smoke: its purpose is to "
    "make a wrong API call, keyword argument, return type, or malformed structure fail "
    "fast here. Downloading the tool's own model weights / parameter files is allowed; do "
    "not use the network otherwise."
)
_SMOKE_LOAD_ONLY = (
    '- Provide argparse with `--output` (CSV path, default "{output_file}") and '
    "`--smoke`. In --smoke mode, build the ACTUAL target system using the SAME "
    "structure-building code your full run uses (never a smaller or simpler substitute "
    "material), and construct/load the calculator (instantiate it, or load the pretrained "
    "model) to prove it is real and callable -- then STOP before any expensive numerical "
    "work and exit. A --smoke check is a fast wiring test that must finish in a few "
    "seconds: do NOT start an SCF cycle, a geometry or cell relaxation, dense k-point "
    "sampling, a band-structure or DOS pass, or any dynamics in --smoke -- those belong "
    "ONLY to the full (non-smoke) run, because for this tool they are too costly or need "
    "external parameter files. You must still write the {output_file} CSV row so the "
    "smoke check finds its output file, but you do NOT need the real {property} value to "
    "do so: write a sentinel (e.g. null / NaN / empty) in place of any number the skipped "
    "computation would have produced, and add a boolean 'smoke' column so the placeholder "
    "is unmistakable. Building the real structure and loading the calculator here is what "
    "surfaces a broken builder or wrong API before a costly run. Downloading the tool's "
    "own model weights / parameter files is allowed; do not use the network otherwise."
)


# Spliced into BOTH synthesis prompts, unconditionally. Not gated on the toolset:
# the two rival spin channels are an ASE-wide design split, so any engine list
# here would protect only the engines already known to have bitten us -- and the
# ground-state multiplicity of an open-shell reference is a correctness issue for
# every electronic-structure run, not a per-engine quirk.
_SPIN_GUIDANCE = (
    "- OPEN-SHELL SPECIES -- get the spin state right, and state it exactly ONCE. "
    "Atoms and radicals are usually NOT closed-shell singlets: an O atom and a C "
    "atom are 3P triplets (multiplicity 3), O2 is a 3Sigma-g- triplet (3), an N "
    "atom is 4S (4). A singlet stands in silently for any of these and corrupts "
    "every energy difference built on it.\n"
    "- ASE has two rival ways to convey that spin state and each calculator reads "
    "only one: GPAW, Quantum ESPRESSO, ABINIT and DFTB+ read the Atoms object's "
    "initial magnetic moments, while Psi4, CP2K, NWChem and xTB read an explicit "
    "count keyword (`multiplicity`, `mult`, `uhf`). If BOTH are set the loser is "
    "discarded with no warning -- ASE's Psi4 goes further and overwrites your "
    "`reference` with 'uhf' and your `multiplicity` with None whenever the atoms "
    "carry moments, and `ase.build.molecule` puts moments on exactly the "
    "open-shell species that need a multiplicity. That pairing turned a CO2 heat "
    "of formation of -393.5 into -1072 kJ/mol. So pick the channel your "
    "calculator actually reads and neutralize the other: passing a count, first "
    "call `atoms.set_initial_magnetic_moments([0.0] * len(atoms))` (a no-op when "
    "they were already zero) and pass `reference=`/unrestricted explicitly; "
    "relying on moments, set them yourself and pass no count keyword. Never let "
    "the two disagree, and never leave which one wins to the calculator.\n"
)


# LLM code-synthesis prompt. The builder asks the gateway for a self-contained
# main.py tailored to the selected library + calculator + material + property;
# a deterministic template is the fallback when synthesis is unavailable/invalid.
# Objective phrasings that ask to RETRIEVE a stored value from the Materials
# Project instead of computing it. A lookup is a genuinely different route from
# a calculation -- it needs the mp-api client and an MP_API_KEY at run time --
# so codegen must be told about it explicitly (property + material alone give
# the model no way to know; the CaPt2 run failed exactly this way).
_MP_LOOKUP_NEEDLES = ("materials project", "materialsproject", "mprester", "mp-api")
_MP_LOOKUP_VERBS = ("retriev", "look up", "lookup", "precomputed", "database",
                    "stored", "query", "fetch")


def mp_lookup_requested(objective) -> bool:
    """Whether the researcher's objective asks for a Materials Project retrieval.

    Requires BOTH a Materials Project mention and a retrieval verb, so "compare
    against the Materials Project value" or "the mp-1023 structure" alone do not
    reroute a compute task into a lookup.

    >>> mp_lookup_requested("retrieve the precomputed bulk modulus from the Materials Project")
    True
    >>> mp_lookup_requested("compute the band gap of Si")
    False
    """
    text = (objective or "").lower()
    return (any(n in text for n in _MP_LOOKUP_NEEDLES)
            and any(v in text for v in _MP_LOOKUP_VERBS))


# The requirements are deliberately domain-agnostic -- they describe *how* to write
# a reliable script (discover identifiers, don't hardcode constants, exercise the
# tool in --smoke), never *what* property or material to expect.
_LLM_CODEGEN_PROMPT = """You are TWAIN's code-configuration builder. Write ONE \
self-contained Python 3 script (the contents of main.py) that computes the \
{property} of {material_desc} using the {library} library with the {calculator} \
calculator (import name: `{calculator_import}`).

Hard requirements:
- Output ONLY the Python source code -- no markdown fences, no prose, no commentary.
- Build the atomic system in code for {material_desc}. Do NOT read any external \
structure file. Build the EXACT phase/polymorph named -- if a space group is given, \
construct THAT structure (e.g. `ase.spacegroup.crystal(...)` with that space group, or \
pymatgen), and never substitute a different or more common polymorph than the one \
requested. For a simple element or binary that `ase.build.bulk` supports, use it (it \
carries the correct experimental lattice constant); otherwise supply the standard \
reference lattice parameters and Wyckoff positions for the named polymorph -- these are \
structural INPUTS that define the cell, not the {property} you compute. Do NOT hardcode \
the {property} value itself or any other result you are meant to calculate.
- Build the structure CORRECTLY rather than defensively. Use the standard reference cell \
for the named polymorph -- correct lattice parameters, Wyckoff positions, and the right \
stoichiometric ratio for the formula -- so the cell is right the first time. Do NOT write \
runtime guards that raise or exit when the composition, atom count, formula-unit count, \
or detected symmetry is not what you expected: no `if counts[...] != n: raise`, no \
stoichiometry, atom-count, or space-group assertions that abort the run. If the structure \
would come out wrong, the fix is to CORRECT the structure-building code so it produces the \
right cell -- never to bolt on a validator that halts execution. You may print the \
composition, cell, and minimum interatomic distance for visibility, but a mismatch must \
never stop the computation.
- Keep the space-group ORIGIN SETTING and the Wyckoff coordinates consistent: origin \
choice 1 and origin choice 2 place the same site at DIFFERENT fractional coordinates, and \
a mismatch silently builds the wrong occupancy/stoichiometry (in Fd-3m origin choice 2, \
8a is (1/8,1/8,1/8) and 16d is (1/2,1/2,1/2); (0,0,0) is the 16c site there, whereas 8a \
is (0,0,0) only in origin choice 1). Because this is easy to get wrong from memory, \
SELF-CHECK it at runtime: after building, derive the element counts from the structure, \
and if their reduced ratio does not match the target formula, REBUILD with the same \
Wyckoff coordinates under the other `setting=` value and use whichever cell matches. \
This rebuild-on-mismatch is required; aborting on mismatch is forbidden.
- Import from the CURRENT module layout of the pinned library versions -- do not use \
import paths that only worked in older releases, and NEVER invent a module path that \
merely sounds plausible. In ASE >= 3.23, cell-relaxation filters live in `ase.filters` \
(`from ase.filters import FrechetCellFilter`), NOT `ase.constraints`. The band-gap \
helper is `from ase.dft.bandgap import bandgap` (pass it the attached calculator); \
there is NO `gpaw.bandgap` module. In GPAW, occupations={{"name": "fixed"}} requires an \
explicit per-band `numbers` array -- for a frozen-occupations band-structure pass use \
{{"name": "fixed-uniform"}}.
- NEVER gate behavior on `inspect.signature()` capability probes: ASE-style calculators \
(e.g. xtb-python's `XTB`) declare `__init__(self, atoms=None, **kwargs)` and route real \
options (`method`, `accuracy`, `solvent`, ...) through `default_parameters`, so the probe \
falsely reports them unsupported. Pass the documented keywords directly \
(`XTB(method="GFN2-xTB", solvent="water")`) and let a genuinely wrong keyword raise.
- Attach the {calculator} calculator (`{calculator_import}`) and compute {property}. \
Do NOT invent model, dataset, or parameter-set identifiers -- a name you guess may \
not exist. If the calculator loads a named pretrained model, discover the valid \
identifier at runtime (e.g. call the library's "list available/pretrained models" \
API and select the one matching the task) rather than hardcoding a guessed string. \
Call every API with the argument types it documents.
- Compute the quantity the property NAME denotes. If {property} names a specific route \
or averaging scheme (e.g. an elastic-tensor-derived modulus and its averaging \
convention, a specific gap type, a named ensemble), compute THAT quantity by its proper \
method -- do not report a cheaper proxy under the requested name -- and add a comment \
stating how the number you print maps to {property}.
- Default to the FASTEST protocol that answers the question. Unless the researcher \
explicitly asked for a relaxed/optimized structure, do NOT run any geometry or cell \
optimization: evaluate the property directly at the standard reference structure \
(experimental lattice parameters) -- a band gap, band structure, DOS, or single-point \
energy needs no relaxation step. Relax first ONLY when {property} is undefined without \
equilibrium (e.g. an equation-of-state minimum, elastic response, adsorption geometry), \
and then relax only the degrees of freedom the property depends on, to converged \
forces/stress -- never from an arbitrary unrelaxed guess.
- The same default-speed rule caps the NUMERICAL settings. Unless the researcher \
explicitly asked for high accuracy or tight convergence: plane-wave cutoff <= 450 eV \
(350-400 eV is fine for metals with PAW), k-point grids no denser than 8x8x8 for a \
primitive cell, and an equation of state is ONE scan of 5-7 volume points spanning \
about +-5% -- never a wide scan followed by a refinement scan. This resolves bulk \
properties (lattice constant, bulk modulus, band gap) to a few percent, which is the \
expected default; a 600 eV cutoff with a 12x12x12 grid and 18 EOS points costs ~50x \
more and gets the job killed at its wall-clock limit with zero results.
- In the real (non-smoke) run, use numerical settings converged well enough for \
{property} (adequate k-point density, plane-wave/basis cutoff, SCF tolerance, sampling) \
within the budget above; use the library's documented production defaults when unsure, \
and do not carry any reduced settings from the --smoke check into the full run.
- Do NOT pay for atoms the property does not need: for a bulk crystal property \
(lattice parameter, bulk modulus, cohesive/formation energy, band property) run the \
calculation on the PRIMITIVE cell, converting any conventional-cell quantity (like a \
cubic lattice parameter) from the primitive-cell result at the end. This must happen \
IN CODE, not in a comment: with `ase.spacegroup.crystal` actually pass \
`primitive_cell=True` in the call, and SELF-CHECK by printing the atom count (diamond \
Si primitive = 2 atoms, not the 8-atom conventional cube; C15 CaPt2 primitive = 6 \
atoms, not 24). Plane-wave DFT cost grows roughly with the CUBE of the atom count, so \
a conventional cell wastes an order of magnitude or more. Use the full conventional \
cell only when the property genuinely requires it (e.g. a defect or surface supercell).
- Keep sampling scans minimal-but-sufficient: an equation-of-state fit needs 5-7 \
volume points around the reference cell -- only widen or rescan if the minimum is not \
bracketed.
- For a band structure / band path, NEVER hardcode special-point letters: which \
letters exist (W, L, M, R, ...) depends on the Bravais lattice ASE detects from the \
actual cell, and a wrong guess raises KeyError after the whole SCF has already been \
paid for. Use the cell's own default path (e.g. \
`atoms.cell.bandpath(npoints=..., pbc=atoms.pbc)` with no path string), or build the \
path only from letters present in `atoms.cell.bandpath().special_points`.
- For a fundamental band gap, NEVER read it off the SCF k-grid (`bandgap(calc)` \
right after the ground state): band extrema generally lie BETWEEN grid points \
(silicon's CBM sits at ~0.85 of Gamma->X, which no uniform grid samples), so the \
gap comes out too large while the script runs cleanly. Two-step method: converge \
the density on the SCF grid, then run a non-self-consistent fixed-density pass \
along the standard path and take the extrema from THAT calculation -- e.g. \
`bs_calc = calc.fixed_density(kpts=atoms.cell.bandpath(npoints=200, \
pbc=atoms.pbc), symmetry='off')` then `bandgap(bs_calc, direct=False)`.
- Make output MPI-safe: when the calculator can run under MPI, every rank executes the \
script, so write files and print through rank-0-only helpers (e.g. \
`ase.parallel.parprint` and `ase.parallel.paropen`, or an explicit \
`world.rank == 0` guard). These are no-ops in serial runs, so use them unconditionally.
- Print every metric WITH its physical unit, and for any fitted or derived value also \
print a fit-quality / convergence diagnostic (e.g. fit residual, R^2, number of sample \
points) so the result's reliability is visible.
- Keep the heavy imports (`{library_import}`, `{calculator_import}`) INSIDE functions \
so the module still imports where they are not installed.
- First thing in the `if __name__ == "__main__":` block, anchor the working \
directory to the script's own directory (`os.chdir(os.path.dirname(os.path.abspath(\
__file__)))`) so relative outputs and calculator scratch files land next to the \
script, never in the caller's working directory.
{spin_note}{engine_note}{thermo_note}{pseudo_note}{database_note}{smoke_instruction}
- Print a JSON object to stdout whose keys include {metric_keys} (the computed \
value(s)), plus "tool", "calculator", "property", and "output_file". Write the same \
metrics as one CSV row to --output.
- End the file with an `if __name__ == "__main__":` block that runs the script \
(calls your main function). Output the COMPLETE script in one reply -- do not stop \
partway or omit the entrypoint.
- Run entirely IN-PROCESS on the packages installed in the run environment (the Python \
standard library, `{library_import}`, and `{calculator_import}`). {also_available} Do NOT \
require an external command-line program or separate binary that is not part of that \
installed stack -- a tool you ASSUME is on PATH may be absent and will crash the run. The \
one exception is the engine this plan selected: when a PARALLELISM note below tells you it \
is a separate program, its binary is declared in the registry and checked on PATH by the \
bundle's smoke test before anything runs, so driving it is expected -- what is forbidden is \
reaching for some OTHER binary nobody verified. Do \
NOT add physics corrections that computing {property} does not require. When a driver uses \
ASE and a DFT-D3 dispersion correction IS warranted (e.g. a van-der-Waals-bound molecular \
crystal), the IN-PROCESS `dftd3.ase.DFTD3` calculator (from the installed dftd3-python) is \
available -- prefer it, and do NOT use `ase.calculators.dftd3.DFTD3`, which shells out to an \
external `dftd3` executable that is not installed. If you DO attach any optional add-on that \
delegates to an external backend, guard the actual energy/force/stress EVALUATION -- not \
merely the object's construction, which can succeed even when the backend is missing -- with \
try/except, so an absent backend falls back to the base calculator and the run still \
completes instead of aborting mid-calculation. The script MUST compile and MUST succeed when \
run with --smoke.

Acceptance criteria (JSON list of {{metric_name, target_value, tolerance}}): \
{acceptance_json}

Begin the script now."""


# Library-only variant: the selected library computes the property itself (e.g.
# PySCF/Psi4 for a molecular HOMO-LUMO gap), so there is no separate calculator to
# attach. Same domain-agnostic reliability requirements; used when a library-only
# run requests a real property and no dedicated template fits, so we never hand
# back the generic stub (which computes nothing) when the LLM can write real code.
_LLM_CODEGEN_PROMPT_LIBRARY = """You are TWAIN's code-configuration builder. Write ONE \
self-contained Python 3 script (the contents of main.py) that computes the {property} \
of {material_desc} using the {library} library (import name: `{library_import}`), \
which computes this property directly.

Hard requirements:
- Output ONLY the Python source code -- no markdown fences, no prose, no commentary.
- Build the system in code for {material_desc}. Do NOT read any external structure \
file. For a molecule, build from its formula/SMILES with the library's own tools. For a \
crystal, build the EXACT phase/polymorph named -- if a space group is given, construct \
THAT structure and never substitute a different or more common polymorph; its standard \
reference lattice parameters are structural INPUTS, not the {property} you compute. \
Default to the FASTEST protocol that answers the question: unless the researcher \
explicitly asked for a relaxed/optimized structure, compute directly at the standard \
reference geometry with NO optimization step; optimize first only when {property} is \
undefined without equilibrium, using numerical settings converged well enough for \
{property} in the real run. Do NOT hardcode the {property} value or any other result \
you are meant to calculate.
- Build the structure CORRECTLY rather than defensively: use the standard reference cell \
for the named polymorph (correct lattice parameters, Wyckoff positions, and stoichiometric \
ratio for the formula). Do NOT write runtime guards that raise or exit when the \
composition, atom count, or symmetry is not what you expected -- no stoichiometry, \
atom-count, or space-group assertions that abort the run. If the built structure would be \
wrong, CORRECT the structure-building code so it produces the right cell instead of adding \
a validator that halts execution. Printing the composition and cell for visibility is \
fine; a mismatch must never stop the computation.
{structure_note}
- Compute the quantity the property NAME denotes -- if {property} names a specific \
route or averaging scheme, compute THAT, not a cheaper proxy, and comment how your \
printed number maps to {property}. Do NOT invent method, basis-set, functional, or \
parameter identifiers -- a name you guess may not exist. Use documented defaults or \
discover valid identifiers at runtime, and call every API with the argument types it \
documents.
- Do NOT pay for atoms the property does not need: compute bulk crystal properties on \
the PRIMITIVE cell (converting conventional-cell quantities from it at the end), and \
keep sampling scans minimal-but-sufficient (an equation-of-state fit needs 5-7 volume \
points; widen only if the minimum is not bracketed).
- Keep the heavy import (`{library_import}`) INSIDE functions so the module still \
imports where it is not installed.
- First thing in the `if __name__ == "__main__":` block, anchor the working \
directory to the script's own directory (`os.chdir(os.path.dirname(os.path.abspath(\
__file__)))`) so relative outputs and calculator scratch files land next to the \
script, never in the caller's working directory.
{spin_note}{engine_note}{thermo_note}{database_note}{smoke_instruction}
- Print a JSON object to stdout whose keys include {metric_keys} (the computed \
value(s)), plus "tool", "property", and "output_file"; print each metric WITH its \
physical unit, and for any fitted or derived value also print a fit-quality / \
convergence diagnostic so its reliability is visible. Write the same metrics as one \
CSV row to --output.
- End the file with an `if __name__ == "__main__":` block that runs the script \
(calls your main function). Output the COMPLETE script in one reply -- do not stop \
partway or omit the entrypoint.
- Run entirely IN-PROCESS on the packages installed in the run environment (the Python \
standard library and `{library_import}`). {also_available} Do NOT require an external \
command-line program or separate binary that is not part of that installed stack -- a tool \
you assume is on PATH may be absent and will crash the run. Do NOT add corrections that \
computing {property} does not require. If you use an optional external-backend helper, \
guard the actual computation call (not merely its construction) with try/except so a \
missing backend degrades gracefully instead of aborting mid-run. The script MUST compile \
and MUST succeed when run with --smoke.

Acceptance criteria (JSON list of {{metric_name, target_value, tolerance}}): \
{acceptance_json}

Begin the script now."""


# The verify-and-repair prompts now live with the REPAIR stage in
# ``script_doctor`` -- BUILD only asks for the initial script (prompt above).


# --------------------------------------------------------------------------- #
# No offline structure fabrication.
#
# TWAIN ships NO hard-coded lattice constants or sample structures. A run must
# build the *actual* requested system (the LLM path) or be handed a real
# structure; material-aware templates fail loudly rather than substitute a
# placeholder, so a missing structure surfaces as an error instead of a
# silently wrong-material result.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Minimal YAML emitter (controlled input -> valid block YAML; no PyYAML needed).
# --------------------------------------------------------------------------- #
def _yaml_scalar(value) -> str:
    """Render a scalar as YAML.

    >>> _yaml_scalar(True), _yaml_scalar(None), _yaml_scalar(3), _yaml_scalar(1.5)
    ('true', 'null', '3', '1.5')
    >>> _yaml_scalar("plain"), _yaml_scalar("has: colon")
    ('plain', '"has: colon"')
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if _needs_quote(text):
        return json.dumps(text)  # double-quoted form is valid YAML
    return text


def _needs_quote(text: str) -> bool:
    """Whether a string must be quoted to round-trip as YAML."""
    if text == "" or text.strip() != text:
        return True
    if text.lower() in {"true", "false", "null", "yes", "no", "on", "off", "~"}:
        return True
    if re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", text):
        return True  # looks like a number
    return any(ch in text for ch in ":#{}[],&*!|>'\"%@`") or text[0] in "-?"


def dump_yaml(obj, _indent: int = 0) -> str:
    """Serialize dict/list/scalar ``obj`` to block-style YAML.

    Handles exactly the shapes the engine emits (nested dicts, lists of scalars,
    lists of flat dicts, scalars). JSON is valid YAML, but this produces the
    readable block form the config file wants.

    >>> print(dump_yaml({"tool": "ASE", "parameters": {"steps": 5}}).strip())
    tool: ASE
    parameters:
      steps: 5
    >>> print(dump_yaml({"notes": ["a", "b"]}).strip())
    notes:
      - a
      - b
    """
    pad = "  " * _indent
    lines: List[str] = []
    if isinstance(obj, dict):
        if not obj:
            return f"{pad}{{}}\n" if _indent == 0 else "{}"
        for key, val in obj.items():
            if isinstance(val, dict) and val:
                lines.append(f"{pad}{key}:")
                lines.append(dump_yaml(val, _indent + 1).rstrip("\n"))
            elif isinstance(val, list) and val:
                lines.append(f"{pad}{key}:")
                lines.append(_dump_list(val, _indent + 1).rstrip("\n"))
            elif isinstance(val, dict):  # empty
                lines.append(f"{pad}{key}: {{}}")
            elif isinstance(val, list):  # empty
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(val)}")
        return "\n".join(lines) + "\n"
    if isinstance(obj, list):
        return _dump_list(obj, _indent)
    return f"{pad}{_yaml_scalar(obj)}\n"


def _dump_list(items: List, indent: int) -> str:
    pad = "  " * indent
    lines: List[str] = []
    for item in items:
        if isinstance(item, dict) and item:
            # "- k: v" on the first key, remaining keys aligned under it.
            keys = list(item.items())
            first_key, first_val = keys[0]
            lines.append(f"{pad}- {first_key}: {_yaml_scalar(first_val)}")
            for k, v in keys[1:]:
                lines.append(f"{pad}  {k}: {_yaml_scalar(v)}")
        else:
            lines.append(f"{pad}- {_yaml_scalar(item)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# RunBundle
# --------------------------------------------------------------------------- #
@dataclass
class RunBundle:
    """The four files that make up a runnable bundle, plus provenance metadata.

    :meth:`write` materializes them into a directory the execution adapter can
    ``pip install -r requirements.txt`` and then ``python main.py`` -- no manual
    edits required.
    """

    main_py: str
    config_yaml: str
    requirements_txt: str
    inline_tests_py: str
    tool_name: str
    template_name: str
    entrypoint: str = "main.py"
    # twain_pseudo.py, present only for an engine that ships no pseudopotentials
    # (Quantum ESPRESSO, ABINIT). Copied verbatim rather than generated, so the
    # element -> filename lookup that keeps a hallucinated pseudopotential out of
    # a run is version-controlled and unit-tested instead of re-derived per plan.
    # Extra ``bundle_helpers`` modules copied verbatim alongside main.py, keyed by
    # filename: twain_pseudo.py when the engine ships no pseudopotentials, and
    # twain_thermo.py when the property is assembled from several species'
    # energies. A mapping rather than a field per helper so the next one is free.
    helpers: Dict[str, str] = dataclasses.field(default_factory=dict)

    def files(self) -> Dict[str, str]:
        """Map of filename -> contents for the bundle."""
        files = {
            "main.py": self.main_py,
            "config.yaml": self.config_yaml,
            "requirements.txt": self.requirements_txt,
            "inline_tests.py": self.inline_tests_py,
        }
        files.update(self.helpers)
        return files

    def write(self, dest: Union[str, Path]) -> Path:
        """Write every bundle file into ``dest`` (created if needed); return it."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for name, content in self.files().items():
            (dest / name).write_text(content, encoding="utf-8")
        return dest


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class CodegenEngine:
    """Turns an ExecutionPlan into a :class:`RunBundle`.

    BUILD-time codegen is deterministic: standard tools render a template, and a
    calculator-driven run asks the LLM for a tailored ``main.py`` that is accepted
    only if it compiles, references the calculator, and has a runnable entrypoint
    (else it falls back to the generic template). Making the synthesized script
    actually *run* -- smoke-verifying it, feeding real errors back to the model,
    and proactively scanning for latent bugs -- is a separate concern that lives
    in the REPAIR stage (:class:`code_gen.script_doctor.ScriptDoctor`), which runs
    after BUILD. Keeping generation and repair apart means BUILD never executes
    generated code and stays offline/deterministic for unit tests.
    """

    # Samples to draw before giving up on synthesis. One draw is flaky -- the same
    # prompt that fails extraction often succeeds on a retry -- and the REPAIR
    # stage already takes two for exactly this reason.
    SYNTHESIS_ATTEMPTS = 2

    def __init__(self, templates_dir: Optional[Union[str, Path]] = None):
        self.templates_dir = (
            Path(templates_dir) if templates_dir
            else Path(__file__).resolve().parent / "templates"
        )
        # What the last synthesis attempt did, for the caller to record. Without
        # it a rejected reply and an unreachable gateway were indistinguishable:
        # both fell back to the do-nothing scaffold, silently.
        self.last_synthesis: Optional[dict] = None

    # -- template selection --------------------------------------------------
    def select_template(self, tool_name: str, *, hint: str = "") -> TemplateSpec:
        """Pick the template for a tool (RDKit disambiguated by ``hint``).

        >>> CodegenEngine().select_template("Pymatgen").filename
        'template_pymatgen_analysis.py'
        >>> CodegenEngine().select_template("ASE").filename
        'template_molecular_dynamics.py'
        >>> CodegenEngine().select_template("rdkit", hint="scaffold graph ops").filename
        'template_rdkit_manipulation.py'
        >>> CodegenEngine().select_template("rdkit").filename
        'template_property_prediction.py'
        >>> CodegenEngine().select_template("VASP").filename
        'template_generic.py'
        """
        key = canonical_tool_key(tool_name)
        hint = (hint or "").lower()
        if key == "pymatgen":
            return _PYMATGEN
        if key == "ase":
            return _ASE
        if key == "rdkit":
            if any(word in hint for word in _RDKIT_MANIP_HINTS):
                return _RDKIT_MANIP
            return _RDKIT_PROPERTY
        return _GENERIC

    # -- rendering -----------------------------------------------------------
    def _render(self, filename: str, substitutions: Dict[str, str]) -> str:
        template_path = self.templates_dir / filename
        text = template_path.read_text(encoding="utf-8")
        for key, value in substitutions.items():
            text = text.replace("{" + key + "}", str(value))
        return text

    @staticmethod
    def _validate_rendered(source: str, filename: str = "main.py") -> None:
        """Guard a rendered script: no leftover placeholder, and it compiles."""
        leftovers = _LEFTOVER_PLACEHOLDER.findall(source)
        if leftovers:
            raise ValueError(
                f"unsubstituted placeholder(s) left in {filename}: {sorted(set(leftovers))}"
            )
        compile(source, filename, "exec")  # raises SyntaxError if invalid

    # -- plan normalization --------------------------------------------------
    @staticmethod
    def _plan_to_dict(plan) -> dict:
        if isinstance(plan, (str, Path)):
            return json.loads(Path(plan).read_text(encoding="utf-8"))
        if dataclasses.is_dataclass(plan) and not isinstance(plan, type):
            return dataclasses.asdict(plan)
        if isinstance(plan, dict):
            return plan
        raise TypeError(f"unsupported ExecutionPlan type: {type(plan)!r}")

    @staticmethod
    def _hint_from(plan: dict, intent: Optional[dict]) -> str:
        parts: List[str] = []
        if isinstance(intent, dict):
            parts.append(str(intent.get("objective", "")))
        for metric in plan.get("acceptance_metrics", []) or []:
            if isinstance(metric, dict):
                parts.append(str(metric.get("metric_name", "")))
        return " ".join(parts)

    # -- main entry ----------------------------------------------------------
    def generate(self, plan, *, intent: Optional[dict] = None, agent=None,
                 smoke_compute: bool = False,
                 require_synthesis: bool = False,
                 calculator_executable: Optional[str] = None,
                 pseudo_library: Optional[str] = None,
                 parallelism: str = "threads") -> RunBundle:
        """Build a :class:`RunBundle` from an ExecutionPlan.

        ``plan`` may be an ``ExecutionPlan`` dataclass, a plain dict, or a path
        to the plan's JSON artifact. ``intent`` sharpens template selection and
        supplies the target material. ``agent`` (a ``prompt -> str`` callable,
        e.g. the state machine's ``_agent_text``) enables LLM code synthesis: for a
        calculator-driven run, and for a library-only run that requests a real
        property with no dedicated template (e.g. PySCF for a molecular HOMO-LUMO
        gap). Any synthesis failure falls back to a deterministic template so
        codegen never depends on the network to succeed; offline (``agent=None``)
        always renders a template.

        ``require_synthesis`` refuses that fallback. The generic scaffold loads
        the tool and writes a stub -- a fine deliverable to read, but running it
        computes nothing while exiting 0, so the payload, Slurm and TWAIN all
        report success (observed on job 2569967: "completed successfully" in five
        seconds, no chemistry done). A caller about to EXECUTE passes True and
        gets a loud failure carrying the synthesis reasons instead.
        """
        plan = self._plan_to_dict(plan)
        method = plan.get("selected_method", {}) or {}
        tool_name = method.get("tool_name") or "unknown-tool"
        libraries = method.get("libraries") or [tool_name]
        calculator = method.get("calculator")
        calculator_import = method.get("calculator_import")
        calculator_library = method.get("calculator_library") or tool_name

        if calculator and calculator_import:
            return self._generate_with_calculator(
                plan, libraries, calculator, calculator_import, calculator_library,
                intent=intent, agent=agent, smoke_compute=smoke_compute,
                require_synthesis=require_synthesis,
                calculator_executable=calculator_executable,
                pseudo_library=pseudo_library,
                parallelism=parallelism,
            )
        # Library-only run. Prefer a dedicated, tested template when one fits the
        # tool (Pymatgen/ASE/RDKit). Otherwise, if the plan asks for a real property
        # and an agent is available, LLM-synthesize a script for the
        # library+material+property -- this is what stops a library that computes the
        # property itself (e.g. PySCF for a molecular HOMO-LUMO gap) from silently
        # falling back to the tool-agnostic stub, which computes nothing. Without an
        # agent (offline/tests) it still renders the deterministic template.
        spec = self.select_template(tool_name, hint=self._hint_from(plan, intent))
        # Prefer LLM synthesis (which the REPAIR stage then self-heals) over a
        # fail-loud template whenever the model can write real code: either no
        # dedicated template fits (_GENERIC), or the chosen template needs a real
        # structure the plan didn't carry. Both cases are fixable by synthesis +
        # REPAIR rather than aborting the run. When a real structure IS present it
        # is baked into the template below, so we keep the deterministic path.
        needs_structure = spec.requires_structure and not self._structure_for(plan, intent)
        # What to compute: the plan's requested_property, or the acceptance
        # metric's name when the planner left it unset. Gating on
        # requested_property alone meant a plan carrying
        # standard_heat_of_formation_kJ_per_mol but a null requested_property
        # skipped synthesis and rendered the placeholder scaffold instead --
        # which then ran on the cluster and "succeeded" (job 2571447).
        wants_property = plan.get("requested_property") or _first_metric_name(plan)
        if (spec is _GENERIC or needs_structure) and wants_property and agent is not None:
            return self._generate_with_calculator(
                plan, libraries, None, None, calculator_library,
                intent=intent, agent=agent, smoke_compute=smoke_compute,
                require_synthesis=require_synthesis,
            )
        bundle = self._generate_standard(plan, tool_name, intent=intent)
        # (b) The backstop. Any route to the placeholder scaffold is refused when
        # the run is going to execute, not just the synthesis route -- the first
        # version of this guard only covered synthesis, so this path walked
        # straight past it.
        if require_synthesis and bundle.template_name == _GENERIC.filename:
            raise SynthesisFailed(
                tool_name, calculator, agent is None, self.last_synthesis)
        return bundle

    # -- standard (library-only) path ----------------------------------------
    def _generate_standard(self, plan: dict, tool_name: str, *, intent) -> RunBundle:
        spec = self.select_template(tool_name, hint=self._hint_from(plan, intent))
        tool_deps = _depinf.import_names(tool_name)
        tool_import = tool_deps[0].import_name if tool_deps else canonical_tool_key(tool_name)
        generated_at = (plan.get("metadata") or {}).get("timestamp", "") or ""
        acceptance = plan.get("acceptance_metrics", []) or []
        params = dict(spec.default_params)

        substitutions = {
            "TOOL_NAME": tool_name,
            "TOOL_IMPORT": tool_import,
            "MODEL_NAME": spec.model_name,
            "INPUT_FILE": spec.input_file,
            "OUTPUT_FILE": spec.output_file,
            "CONFIG_FILE": "config.yaml",
            "GENERATED_AT": generated_at,
            "CONFIG_JSON": json.dumps(params),
            "ACCEPTANCE_JSON": json.dumps(acceptance),
            # Material baked from the intent so material-aware templates build the
            # requested system instead of their hard-coded sample. Templates that
            # don't carry a {STRUCTURE_JSON} token simply ignore it.
            "STRUCTURE_JSON": json.dumps(self._structure_for(plan, intent)),
        }
        main_py = self._render(spec.filename, substitutions)
        self._validate_rendered(main_py, "main.py")

        config_yaml = dump_yaml(
            self._config_doc(plan, spec, tool_name, tool_import, params, generated_at, acceptance)
        )
        requirements_txt = _depinf.requirements_txt(tool_name)
        import_names = [dep.import_name for dep in tool_deps]
        inline_tests_py = _smoke.generate_inline_tests(
            tool_name=tool_name,
            required_import_names=import_names,
            main_filename="main.py",
            output_filename=spec.output_file,
            run_smoke=True,
        )

        return RunBundle(
            main_py=main_py,
            config_yaml=config_yaml,
            requirements_txt=requirements_txt,
            inline_tests_py=inline_tests_py,
            tool_name=tool_name,
            template_name=spec.filename,
        )

    # -- calculator-driven path (LLM synthesis + tool-agnostic fallback) ------
    def _generate_with_calculator(self, plan, libraries, calculator,
                                  calculator_import, calculator_library,
                                  *, intent, agent, smoke_compute: bool = False,
                                  require_synthesis: bool = False,
                                  calculator_executable: Optional[str] = None,
                                  pseudo_library: Optional[str] = None,
                                  parallelism: str = "threads") -> RunBundle:
        generated_at = (plan.get("metadata") or {}).get("timestamp", "") or ""
        acceptance = plan.get("acceptance_metrics", []) or []
        requested_property = plan.get("requested_property") or "the requested property"
        # Read the metric names too: this plan's requested_property was null while
        # its metric was standard_heat_of_formation_kJ_per_mol.
        wants_cycle = wants_thermo_cycle(
            plan.get("requested_property"),
            *[m.get("metric_name") for m in (plan.get("acceptance_metrics") or [])
              if isinstance(m, dict)],
            (intent or {}).get("objective") if isinstance(intent, dict) else None)

        # The calculator is driven through this library (e.g. GPAW via ASE); the
        # rest of the toolset is available too (e.g. Pymatgen for structure work).
        driver = calculator_library or (libraries[0] if libraries else "ASE")
        driver_deps = _depinf.import_names(driver)
        driver_import = driver_deps[0].import_name if driver_deps else canonical_tool_key(driver)
        also = [lib for lib in libraries if lib.lower() != driver.lower()]
        material = self._material_brief(plan, intent)
        # A real cell (atoms + lattice) baked in upstream (CIF / Materials Project)
        # is passed to the model so it builds THAT verified structure verbatim
        # instead of reconstructing lattice constants from a bare space group.
        structure = self._structure_for(plan, intent)

        brief = {
            "library": driver,
            "library_import": driver_import,
            "also_available": also,
            "calculator": calculator,
            "calculator_import": calculator_import,
            "property": requested_property,
            "material": material,
            "material_desc": self._material_desc(material),
            "structure": structure,
            "acceptance": acceptance,
            "output_file": "results.csv",
            # Verbatim researcher objective: the property + material fields
            # can't express a routing decision like "retrieve this from the
            # Materials Project instead of computing it".
            "objective": (intent or {}).get("objective") if isinstance(intent, dict) else None,
            # When True, the generated --smoke path runs the real (tiny) computation
            # so an API/keyword error is caught in REPAIR, not at the real run.
            "smoke_compute": smoke_compute,
            # "sssp"/"pseudodojo" for an engine shipping no pseudopotentials:
            # switches the prompt to resolve filenames and cutoffs through the
            # bundle's twain_pseudo.py instead of writing them out.
            "pseudo_library": pseudo_library,
            # Where the engine's parallelism lives: decides whether the script must
            # hand its ranks to the engine, and whether 6N+1 engine invocations for
            # finite-difference frequencies are worth warning about.
            "parallelism": parallelism,
            # True when the property is a formation/atomization/reaction enthalpy:
            # switches the prompt to assemble it through twain_thermo.py.
            "thermo_cycle": wants_cycle,
        }

        # The script is written by the LLM from the discovered toolset + material
        # + property -- there is NO per-property template. If the gateway is
        # unavailable or returns invalid Python, fall back to a tool-agnostic
        # scaffold (loads the toolset, writes a stub) -- never a preset.
        self.last_synthesis = None
        main_py = self._synthesize_with_llm(brief, agent) if agent is not None else None
        template_name = "llm_synthesized"
        if main_py is None:
            if require_synthesis:
                raise SynthesisFailed(
                    calculator_library or (libraries[0] if libraries else "the tool"),
                    calculator, agent is None, self.last_synthesis)
            main_py = self._render_generic_fallback(brief, generated_at, acceptance)
            template_name = _GENERIC.filename

        # requirements + smoke imports cover the whole toolset (every library, plus
        # the calculator when one is attached), deduped.
        requirements_txt = self._requirements_for_toolset(libraries, calculator)
        if mp_lookup_requested(brief.get("objective")):
            # A Materials Project retrieval needs the mp-api client, which no
            # library's dependency info carries -- without this line the venv
            # fallback installs pymatgen but the lookup dies on import.
            requirements_txt += "mp-api\n"
        toolset_imports = [d.import_name for lib in libraries for d in _depinf.import_names(lib)]
        if calculator:
            toolset_imports += [d.import_name for d in _depinf.import_names(calculator)]
        # Gate on what the SCRIPT needs, not on what the plan advertised. A plan
        # can name more of a toolset than the code uses, and each unused member
        # becomes a requirement no environment has to satisfy: a plan carrying
        # both Psi4 and NWChem demanded the psi4 python package AND the nwchem
        # binary, which are conda-only and provisioned in different envs, so no
        # single env could pass however the run was set up (Slurm job 2580768).
        import_names = _needed_imports(dict.fromkeys(toolset_imports), main_py)
        # Same reasoning for the engine binary: only demand it when the script
        # actually drives that engine.
        if calculator_executable and calculator_import and not _imports_module(
                main_py, calculator_import):
            calculator_executable = None
        inline_tests_py = _smoke.generate_inline_tests(
            tool_name="+".join(libraries + ([calculator] if calculator else [])),
            required_import_names=import_names,
            # An external engine's ASE bindings import wherever ASE is installed,
            # so the import list alone cannot prove this environment can RUN it.
            required_executables=([calculator_executable]
                                  if calculator_executable else []),
            main_filename="main.py",
            output_filename="results.csv",
            run_smoke=True,
            # A load-only smoke (heavy calculator) constructs the calculator and
            # exits without computing, so no results file is owed.
            require_output=bool(brief.get("smoke_compute")),
        )
        helpers = {}
        if pseudo_library:
            helpers["twain_pseudo.py"] = _bundle_helper_source("twain_pseudo")
        if wants_cycle:
            helpers["twain_thermo.py"] = _bundle_helper_source("twain_thermo")
        # A calculator run executes in the heavy sim env; a library-only run runs on
        # the default interpreter (where its library is installed) -- record that so
        # provenance and run guidance point at the right environment.
        config_yaml = dump_yaml(self._config_doc_calc(
            plan, libraries, driver, driver_import, calculator, calculator_import,
            requested_property, material, generated_at, acceptance, template_name,
            environment=(SIM_ENV if calculator else None)))

        return RunBundle(
            main_py=main_py,
            config_yaml=config_yaml,
            requirements_txt=requirements_txt,
            inline_tests_py=inline_tests_py,
            tool_name=driver,
            template_name=template_name,
            helpers=helpers,
        )

    def _render_generic_fallback(self, brief, generated_at, acceptance) -> str:
        """Tool-agnostic scaffold used only when LLM synthesis is unavailable.

        Loads the discovered toolset and writes a stub result -- no property- or
        calculator-specific code. The real, property-aware script comes from the
        LLM; this just keeps the bundle runnable/inspectable without the gateway.
        """
        spec = _GENERIC
        tool_label = (f'{brief["library"]}+{brief["calculator"]}'
                      if brief.get("calculator") else brief["library"])
        substitutions = {
            "TOOL_NAME": tool_label,
            "TOOL_IMPORT": brief["library_import"],
            "MODEL_NAME": spec.model_name,
            "INPUT_FILE": spec.input_file,
            "OUTPUT_FILE": spec.output_file,
            "CONFIG_FILE": "config.yaml",
            "GENERATED_AT": generated_at,
            "CONFIG_JSON": json.dumps(dict(spec.default_params)),
            "ACCEPTANCE_JSON": json.dumps(acceptance),
        }
        main_py = self._render(spec.filename, substitutions)
        self._validate_rendered(main_py, "main.py")
        return main_py

    def _synthesize_with_llm(self, brief, agent) -> Optional[str]:
        """Ask the agent for a tailored main.py; return it only if it's usable.

        A single call, validated by :func:`extract_valid_source` (compiles +
        references the calculator + has a runnable entrypoint). Returns ``None`` on
        any failure so the caller falls back to a deterministic template. Smoke-
        verifying the script, feeding real errors back to the model, and
        proactively scanning for latent bugs are the REPAIR stage's job
        (:class:`code_gen.script_doctor.ScriptDoctor`), which runs after BUILD --
        so codegen itself never executes generated code.
        """
        # Require the script to reference the calculator (calculator run) or, for a
        # library-only run, the library itself -- so a stub that never touches the
        # tool is rejected and the caller falls back to the deterministic template.
        must_reference = brief.get("calculator_import") or brief.get("library_import")
        prompt = self._codegen_prompt(brief)
        attempts = []
        # Two samples, for the reason the REPAIR stage already takes two: one
        # draw is flaky, and the same prompt that fails extraction often
        # succeeds on a retry. Cheap next to a wasted cluster allocation.
        for attempt in range(1, self.SYNTHESIS_ATTEMPTS + 1):
            try:
                raw = agent(prompt)
            except Exception as exc:  # noqa: BLE001 - recorded, then retried/fallen back
                attempts.append({"attempt": attempt,
                                 "reason": f"agent_error: {type(exc).__name__}: {exc}"})
                continue
            source, reason = validate_source(raw, must_reference)
            if source is not None:
                self.last_synthesis = {"ok": True, "attempts": attempts,
                                       "attempt": attempt,
                                       "reply_chars": len(raw or "")}
                return source
            attempts.append({"attempt": attempt, "reason": reason,
                             "reply_chars": len(raw or "")})
        self.last_synthesis = {"ok": False, "attempts": attempts}
        return None

    def _codegen_prompt(self, brief) -> str:
        """Render the initial code-synthesis prompt from the run brief."""
        metric_keys = [
            m.get("metric_name") for m in brief["acceptance"]
            if isinstance(m, dict) and m.get("metric_name")
        ] or [brief["property"]]
        also = brief.get("also_available") or []
        also_line = (f"You may also use these libraries if helpful (already installed): "
                     f"{', '.join(also)}." if also else "")
        # A calculator run gets the calculator-centric prompt; a library-only run
        # (the library computes the property itself) gets the library variant.
        template = _LLM_CODEGEN_PROMPT if brief.get("calculator") else _LLM_CODEGEN_PROMPT_LIBRARY
        # The --smoke instruction depends on whether the tool can cheaply compute the
        # property with no external data: if so, the smoke runs it for real (so an API
        # error is caught in REPAIR); otherwise it only loads the tool. Pre-format its
        # own {output_file}/{property} placeholders before splicing it in.
        smoke_tmpl = _SMOKE_COMPUTE if brief.get("smoke_compute") else _SMOKE_LOAD_ONLY
        smoke_instruction = smoke_tmpl.format(
            output_file=brief["output_file"], property=brief["property"])
        # When a verified cell was baked in upstream, direct the model to build it
        # verbatim rather than reconstruct lattice constants from memory (the failure
        # mode that produces a plausible-but-wrong crystal). With no baked cell we
        # leave the standard "build the named polymorph" guidance above untouched.
        structure = brief.get("structure") or {}
        if isinstance(structure, dict) and structure.get("atoms") and structure.get("lattice"):
            structure_note = (
                "- A VERIFIED reference structure is provided as JSON below. Build "
                "EXACTLY this cell -- use these lattice vectors and atomic positions "
                "verbatim and do NOT re-derive lattice constants, Wyckoff positions, "
                "or the space group from memory. Structure (JSON): "
                + json.dumps(structure)
            )
        else:
            structure_note = ""
        # The researcher explicitly asked to RETRIEVE the value from the
        # Materials Project rather than compute it. This route overrides the
        # compute-protocol requirements above, and it is credential-gated: the
        # key check must run in --smoke too, so a missing MP_API_KEY fails at
        # BUILD on the runner (clear, immediate) instead of after a Slurm
        # queue wait on a compute node.
        if mp_lookup_requested(brief.get("objective")):
            database_note = (
                "- OVERRIDE -- database retrieval, not a calculation: the "
                "researcher's objective explicitly asks to RETRIEVE this value "
                "from the Materials Project database instead of computing it. "
                f"Objective: {json.dumps(brief.get('objective'))}. Do NOT run a "
                "new calculation and do NOT build the structure. Query the "
                "database with `MPRester` (`from pymatgen.ext.matproj import "
                "MPRester`, imported inside the function; the installed "
                "`mp-api` client backs it), reading the API key from the "
                "`MP_API_KEY` environment variable. FIRST -- in both the "
                "--smoke and the real path, before any network call -- check "
                "the key: if MP_API_KEY is unset or empty, print one clear "
                "line telling the user to add MP_API_KEY to the runner's .env "
                "(free key: https://materialsproject.org/api) and exit(3). "
                "The --smoke path must make NO network request: after the key "
                "check, `import mp_api` to prove the client is installed, "
                "then exit 0. In the real run, NEVER trust a provided mp-id "
                "blindly -- upstream ids are sometimes hallucinated (an "
                "intent carried mp-1023 for CaPt2, which is actually "
                "Ho2Co17). Query by id when one is given, but VERIFY the "
                "returned formula (reduced composition) matches the target "
                "material; on a mismatch or a missing id, search by formula "
                "instead and pick the entry matching the requested space "
                "group/phase when stated (else the lowest energy_above_hull), "
                "printing which entry was used and why. Retrieve the stored "
                "property from the appropriate endpoint (e.g. "
                "elasticity/summary), and include a \"source\" field in the "
                "JSON output stating the value is a Materials Project "
                "database retrieval (with the material_id used), not a new "
                "calculation.\n")
        else:
            database_note = ""
        # A plane-wave engine that ships no pseudopotentials (Quantum ESPRESSO,
        # ABINIT). The filenames are unguessable but look guessable -- silicon's
        # SSSP file is Si.pbe-n-rrkjus_psl.1.0.0.UPF while the equally plausible
        # Si.pbe-n-kjpaw_psl.1.0.0.UPF (oxygen's naming scheme) does not exist --
        # and a wrong one either dies hours into a queued job or names a real file
        # for different physics. Same for the cutoffs. So the bundle carries
        # twain_pseudo.py and the model is told to look both up through it.
        if brief.get("pseudo_library") == "sssp":
            pseudo_note = (
                "- PSEUDOPOTENTIALS -- never write a pseudopotential filename or "
                "an energy cutoff yourself. The bundle contains `twain_pseudo.py`, "
                "which reads the installed SSSP library's own manifest. Use it "
                "verbatim: `from twain_pseudo import espresso_pseudopotentials, "
                "espresso_cutoffs, pseudo_dir`, then `pseudos = "
                "espresso_pseudopotentials(atoms)`, `ecutwfc, ecutrho = "
                "espresso_cutoffs(atoms)` (both already in Ry), and build the "
                "profile as `EspressoProfile(command='pw.x', "
                "pseudo_dir=pseudo_dir())`. Pass `pseudopotentials=pseudos` and "
                "those cutoffs into the Espresso calculator's input data. Do NOT "
                "hardcode a .UPF name, do NOT invent ecutwfc/ecutrho, and do NOT "
                "wrap these calls in try/except -- if the library cannot supply an "
                "element the run MUST fail loudly rather than substitute another "
                "pseudopotential.\n")
        elif brief.get("pseudo_library") == "pseudodojo":
            pseudo_note = (
                "- PSEUDOPOTENTIALS -- never write a pseudopotential filename or "
                "an energy cutoff yourself. The bundle contains `twain_pseudo.py`, "
                "which reads the installed PseudoDojo table's own manifest. Use it "
                "verbatim: `from twain_pseudo import abinit_pp_paths, abinit_ecut`, "
                "then build the profile as `AbinitProfile(command='abinit', "
                "pp_paths=abinit_pp_paths(atoms))` and take `ecut = "
                "abinit_ecut(atoms)` (Hartree, PseudoDojo's recommended hint -- ASE's "
                "Abinit takes ecut in eV, so pass `ecut * 27.2114`). Do NOT hardcode "
                "a .psp8 name, do NOT invent ecut, and do NOT wrap these calls in "
                "try/except -- if the table cannot supply an element the run MUST "
                "fail loudly rather than substitute another pseudopotential.\n"
                "- ABINIT keyword requirements, all three verified against ABINIT "
                "10.0.3 with this pseudopotential table: pass `pps='psp8'` (without "
                "it ASE searches for LDA/FHI-format files and aborts with \"Could "
                "not find lda pseudopotential fhi\" even though the .psp8 files are "
                "right there), pass `xc='PBE'` (the table is PBE; an LDA functional "
                "with PBE pseudopotentials is silently inconsistent physics), and "
                "pass `chksymbreak=0` (ASE writes a shifted Monkhorst-Pack grid, "
                "which ABINIT rejects for symmetric cells like diamond with \"the k "
                "point grid is not symmetric\"; note ASE's Abinit does NOT accept a "
                "dict for `kpts`, so a gamma-centred grid is not an alternative).\n")
        else:
            pseudo_note = ""
        # A property assembled from several species' energies. The algebra is
        # short and looks obvious, and both of its failure modes return a
        # plausible number: a dropped H(T)-E_elec term, or a stoichiometric
        # coefficient that does not match the molecule.
        # How this engine is parallelised, from the registry. Two instructions
        # depend on it, and both are about cost rather than correctness.
        placement = brief.get("parallelism") or "threads"
        if placement == "engine":
            engine_note = (
                "- PARALLELISM -- this engine is a separate program, and the job "
                "gives its ranks to the ENGINE, not to this script. The script runs "
                "single-process on purpose: several ranks of it would each drive "
                "their own copy of the engine in this one directory and overwrite "
                "each other's input and output files. Read "
                "`os.environ.get('TWAIN_ENGINE_LAUNCH', '')` and, when it is "
                "non-empty, prefix it onto the engine command you hand the "
                "calculator (its `command=` argument, or its Profile's) so the "
                "allocated cores are actually used -- e.g. "
                "`cmd = f\"{os.environ.get('TWAIN_ENGINE_LAUNCH','')} pw.x\".strip()`. "
                "Never call mpirun on python.\n"
                "- FREQUENCIES ARE THE EXPENSIVE PART of any thermal correction. "
                "`ase.vibrations.Vibrations` is finite-difference: it invokes the "
                "engine 6N+1 separate times and caches each displacement to disk. "
                "For a 3-atom molecule that is 19 engine runs, and every one pays "
                "the engine's startup and SCF again. If this engine has its own "
                "analytic frequency/Hessian task, use it and read the frequencies "
                "back; only fall back to ase Vibrations when it does not, and say "
                "in the output which route you took.\n")
        else:
            engine_note = ""
        if brief.get("thermo_cycle"):
            thermo_note = (
                "- THERMOCHEMICAL CYCLE -- this property is assembled from "
                "several species' energies, so do not write the algebra yourself. "
                "The bundle contains `twain_thermo.py`; use it verbatim. It counts "
                "stoichiometry from the structure, supplies each free atom's "
                "H(T)-E_elec, refuses an unbalanced reaction, and refuses a "
                "polyatomic species whose correction you forgot to pass.\n"
                "- PREFER AN ERROR-CANCELLING REACTION over atomization. "
                "Atomization breaks every bond, so the method's per-bond error "
                "accumulates straight into the answer -- on CO2's 1608 kJ/mol "
                "atomization that is ~17 kJ/mol for B3LYP and ~616 for "
                "Hartree-Fock. Instead pick a BALANCED reaction that forms the "
                "target from reference species whose standard formation "
                "enthalpies are known experimentally, conserving bond count and "
                "type as closely as you can (for CO2: CO + 1/2 O2 -> CO2, not "
                "C + 2 O -> CO2), so the errors cancel between the two sides. "
                "Use `from twain_thermo import species, "
                "formation_enthalpy_via_reaction`, build each participant with "
                "`species(symbols, energy, correction=H_minus_Eelec, "
                "coefficient=..., unit=<the unit your calculator returned>)`, "
                "and call "
                "`formation_enthalpy_via_reaction(target_symbols, reactants, "
                "products, {'CO': -110.53, 'O2': 0.0})` -- reference enthalpies "
                "keyed by ordinary formula (any spelling is matched by species, "
                "so write them the way a chemist would), 0.0 for an element in "
                "its standard state. State in the printed output which reaction you "
                "used and where each reference enthalpy came from. Fall back to "
                "`atomization_enthalpy` + `formation_enthalpy` only when no "
                "suitable reference reaction exists, and say so.\n"
                "- NAME THE UNIT; NEVER PRE-CONVERT. Every twain_thermo entry "
                "point takes `unit=` ('eV', 'Hartree', 'Rydberg', 'kJ/mol', "
                "'kcal/mol') and converts internally, so pass exactly what the "
                "calculator handed you: `species(sym, psi4.energy(...), "
                "unit='Hartree', correction=ideal_gas.get_enthalpy(T), "
                "correction_unit='eV')`. Psi4/PySCF/NWChem return Hartree, "
                "Quantum ESPRESSO returns Rydberg, ASE returns eV, and ASE's "
                "IdealGasThermo returns eV whatever computed the energies -- "
                "which is why energy and correction have separate unit "
                "arguments. Converting to kJ/mol yourself before calling is the "
                "one thing that must not happen: the module converts to kJ/mol "
                "on the way out, so a pre-converted energy is multiplied by "
                "96.485 twice and CO2's heat of formation comes back as -27452 "
                "instead of -393.8 (run 1cd39ffd), a number no accuracy check "
                "catches because it is not slightly wrong.\n"
                "- The method must include electron correlation. Bare "
                "Hartree-Fock (Psi4's `method='scf'`, NWChem's `theory='scf'`) "
                "recovers none of it and underestimates bond energies by "
                "hundreds of kJ/mol -- it put this very property at +245.7 "
                "instead of -393.5. For a bond-energy or thermochemical quantity "
                "use at least a hybrid functional (B3LYP, PBE0, wB97X-D) or a "
                "correlated wavefunction method (MP2, CCSD(T)); never plain SCF.\n"
                "- Every species in the cycle contributes its own H(T)-E_elec, "
                "the reference ATOMS included. An atom has no vibrations and no "
                "rotations, so it is tempting to give it no correction at all, but "
                "it still carries 3/2 kT of translation plus kT of PV = 5/2 kT = "
                "6.197 kJ/mol at 298.15 K. Omitting it for three reference atoms "
                "is 18.6 kJ/mol, which is what put a CO2 heat of formation at "
                "-357.6 against a -393.5 target. The tabulated atomic formation "
                "enthalpies do NOT absorb it -- those are the atoms' own formation "
                "enthalpies, and the cycle they feed needs a true enthalpy "
                "difference at T. Compute the molecule's correction with "
                "`ase.thermochemistry.IdealGasThermo` (its real geometry and "
                "symmetry number) and let twain_thermo handle the atoms.\n")
        else:
            thermo_note = ""
        return template.format(
            property=brief["property"],
            material_desc=brief["material_desc"],
            library=brief["library"],
            library_import=brief["library_import"],
            calculator=brief.get("calculator"),
            calculator_import=brief.get("calculator_import"),
            output_file=brief["output_file"],
            metric_keys=", ".join(repr(k) for k in metric_keys),
            acceptance_json=json.dumps(brief["acceptance"]),
            also_available=also_line,
            smoke_instruction=smoke_instruction,
            structure_note=structure_note,
            database_note=database_note,
            # Ignored by the library-only template, which has no such placeholder.
            pseudo_note=pseudo_note,
            thermo_note=thermo_note,
            engine_note=engine_note,
            spin_note=_SPIN_GUIDANCE,
        )

    @staticmethod
    def _material_brief(plan: dict, intent: Optional[dict]) -> Dict[str, Optional[str]]:
        sysd = plan.get("target_system") or (intent or {}).get("system_descriptors") or {}
        if not isinstance(sysd, dict):
            sysd = {}
        # A molecular run describes its target under `molecule` (name + SMILES); a
        # solid-state run under `crystal` (polymorph/phase + space group). Read BOTH
        # so a crystal's polymorph survives into codegen -- otherwise only `formula`
        # reaches the model and it builds the most common polymorph (e.g. rutile for
        # a request that asked for anatase TiO2).
        molecule = sysd.get("molecule") if isinstance(sysd.get("molecule"), dict) else {}
        crystal = sysd.get("crystal") if isinstance(sysd.get("crystal"), dict) else {}
        return {
            "formula": sysd.get("formula") or crystal.get("formula") or molecule.get("formula"),
            "name": crystal.get("name") or molecule.get("name") or sysd.get("name"),
            "SMILES": molecule.get("SMILES"),
            "phase": crystal.get("phase"),
            "crystal_system": crystal.get("crystal_system"),
            "space_group": crystal.get("space_group"),
            "space_group_number": crystal.get("space_group_number"),
            "mp_id": crystal.get("mp_id") or sysd.get("mp_id"),
        }

    @staticmethod
    def _material_desc(material: Dict[str, Optional[str]]) -> str:
        name, formula = material.get("name"), material.get("formula")
        phase = material.get("phase")
        # Name the polymorph even when `name` doesn't already carry it (e.g. name is
        # "titanium dioxide" while phase is "anatase") so the model builds the
        # requested phase rather than the most common one.
        if name and phase and str(phase).lower() not in name.lower():
            name = f"{phase} {name}"
        base = f"{name} ({formula})" if name and formula else (name or formula or "the requested material")
        # Append the space group / crystal system so the exact structure is
        # unambiguous. These are reference INPUTS that define the cell, not the
        # property being computed.
        quals: List[str] = []
        sg, sgn = material.get("space_group"), material.get("space_group_number")
        if sg and sgn:
            quals.append(f"space group {sg} (No. {sgn})")
        elif sg:
            quals.append(f"space group {sg}")
        elif sgn:
            quals.append(f"space group No. {sgn}")
        cs = material.get("crystal_system")
        if cs and str(cs).lower() not in base.lower():
            quals.append(str(cs))
        if material.get("mp_id"):
            # The database id is the exact handle for a lookup route and a
            # useful cross-reference for a compute route.
            quals.append(f"Materials Project id {material['mp_id']}")
        return f"{base}, {', '.join(quals)}" if quals else base

    @staticmethod
    def _requirements_for_toolset(libraries: List[str], calculator: Optional[str]) -> str:
        """requirements.txt covering every pip-installable part of the toolset.

        Conda-only packages are deliberately left out: pip cannot install them
        anywhere, so listing one turns a recoverable "use the provisioned env"
        into a hard install failure. NWChem is the example -- ``nwchem==7.3.1``
        on PyPI resolves only to a 0.0.1 stub, so the cluster's pip fallback died
        with "No matching distribution found" even though twain-envs/nwchem was
        provisioned and correct. These packages arrive through a cluster env spec
        (scripts/ris/envs/) or the local pixi env, never through this file, and
        the smoke probe is what confirms an env actually provides them.
        """
        deps = []
        for lib in libraries:
            deps.extend(_depinf.infer(lib))
        if calculator:
            deps.extend(_depinf.infer(calculator))
        seen: set[str] = set()
        unique: List[str] = []
        for dep in deps:
            if dep.package.lower() in seen:
                continue
            seen.add(dep.package.lower())
            if dep.package.lower() in _depinf.CONDA_ONLY_PACKAGES:
                unique.append(f"# {dep.package}: conda-only, comes from the "
                              f"environment (pip cannot install it)")
                continue
            unique.append(dep.requirement_line())
        header = "# Auto-generated by TWAIN code_configuration_builder -- pinned for reproducibility."
        return "\n".join([header, *unique]) + "\n"

    @staticmethod
    def _structure_for(plan: dict, intent: Optional[dict]) -> dict:
        """Real structure to bake into a material-aware template, or ``{}``.

        TWAIN never fabricates a structure from a bare formula or space group: it
        bakes one only when an explicit cell (atoms + lattice) was actually provided
        upstream (e.g. from a CIF or Materials Project entry). Otherwise it returns
        ``{}`` and the template fails loudly rather than analysing a placeholder.
        """
        for src in (intent or {}, plan or {}):
            if not isinstance(src, dict):
                continue
            descriptors = src.get("system_descriptors")
            target = src.get("target_system")
            candidates = (
                src.get("structure"),
                descriptors.get("structure") if isinstance(descriptors, dict) else None,
                target.get("structure") if isinstance(target, dict) else None,
            )
            for candidate in candidates:
                if (isinstance(candidate, dict)
                        and candidate.get("atoms") and candidate.get("lattice")):
                    return candidate
        return {}

    @staticmethod
    def _config_doc(plan, spec, tool_name, tool_import, params, generated_at, acceptance) -> dict:
        slurm = plan.get("slurm_request", {}) or {}
        return {
            "tool": tool_name,
            "tool_import": tool_import,
            "template": spec.filename,
            "model": spec.model_name,
            "generated_at": generated_at,
            "entrypoint": "main.py",
            "paths": {"input": spec.input_file, "output": spec.output_file},
            "parameters": params,
            "resources": {
                "cpu_count": slurm.get("cpu_count"),
                "gpu_count": slurm.get("gpu_count"),
                "max_time_hours": slurm.get("max_time"),
                "ram_gb": slurm.get("ram"),
            },
            "acceptance_criteria": [
                {
                    "metric_name": m.get("metric_name"),
                    "target_value": m.get("target_value"),
                    "tolerance": m.get("tolerance"),
                }
                for m in acceptance if isinstance(m, dict)
            ],
            "safety_notes": list(plan.get("safety_notes", []) or []),
        }

    @staticmethod
    def _config_doc_calc(plan, libraries, driver, driver_import, calculator,
                         calculator_import, requested_property, material,
                         generated_at, acceptance, template_name,
                         environment=SIM_ENV) -> dict:
        slurm = plan.get("slurm_request", {}) or {}
        return {
            "libraries": list(libraries),
            "tool": driver,
            "tool_import": driver_import,
            "calculator": calculator,
            "calculator_import": calculator_import,
            "calculator_library": driver if calculator else None,
            "property": requested_property,
            "material": material,
            "template": template_name,
            "generated_at": generated_at,
            "entrypoint": "main.py",
            # A calculator run's heavy stack lives in the sim pixi env (run with
            # `pixi run -e <environment> python main.py`); a library-only run
            # (environment=None) runs on the default interpreter.
            "environment": environment,
            "paths": {"output": "results.csv"},
            "parameters": {},
            "resources": {
                "cpu_count": slurm.get("cpu_count"),
                "gpu_count": slurm.get("gpu_count"),
                "max_time_hours": slurm.get("max_time"),
                "ram_gb": slurm.get("ram"),
            },
            "acceptance_criteria": [
                {
                    "metric_name": m.get("metric_name"),
                    "target_value": m.get("target_value"),
                    "tolerance": m.get("tolerance"),
                }
                for m in acceptance if isinstance(m, dict)
            ],
            "safety_notes": list(plan.get("safety_notes", []) or []),
        }


if __name__ == "__main__":  # pragma: no cover - manual smoke of the module
    import sys

    if len(sys.argv) < 2:
        print("Usage: python codegen_engine.py <execution_plan.json> [dest_dir]")
        raise SystemExit(1)
    engine = CodegenEngine()
    bundle = engine.generate(sys.argv[1])
    dest = sys.argv[2] if len(sys.argv) > 2 else "run_bundle"
    path = bundle.write(dest)
    print(f"RunBundle for {bundle.tool_name} ({bundle.template_name}) -> {path}")
