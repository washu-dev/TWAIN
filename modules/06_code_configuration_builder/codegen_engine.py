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


def extract_valid_source(raw, calculator_import: Optional[str] = None) -> Optional[str]:
    """Fenced-or-raw agent reply -> a usable script, or ``None``.

    Returns ``None`` when the reply is non-string/empty, doesn't compile, has no
    runnable entrypoint, or (when ``calculator_import`` is given) never references
    that import. The entrypoint check catches a truncated reply: a script cut off
    mid-body often still *compiles* (its last partial line is a valid statement)
    and mentions the calculator, but defines functions it never calls -- so
    running it does nothing. Rejecting it lets the caller repair or fall back
    instead of shipping a script that silently no-ops.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    source = strip_code_fences(raw)
    try:
        compile(source, "main.py", "exec")
    except SyntaxError:
        return None
    if calculator_import and calculator_import.lower() not in source.lower():
        return None
    if not has_runnable_entrypoint(source):
        return None
    return source


@dataclass(frozen=True)
class TemplateSpec:
    """A template file plus the defaults used when rendering it."""

    filename: str
    model_name: str
    input_file: str
    output_file: str
    default_params: Dict[str, object] = field(default_factory=dict)


# Tool (canonical key) -> template. RDKit is resolved to one of two templates by
# objective; unmapped tools fall back to the generic runner.
_PYMATGEN = TemplateSpec("template_pymatgen_analysis.py", "structure_analysis", "structure.json", "results.csv", {"round_digits": 4})
_ASE = TemplateSpec("template_molecular_dynamics.py", "emt_md", "system.json", "trajectory.csv", {"steps": 20, "timestep_fs": 1.0, "temperature_K": 300.0})
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
# or one needing pseudopotential/Slater-Koster files) it only loads the calculator,
# since a real run can't happen in a smoke check. Both carry {output_file}/{property}
# placeholders and are pre-formatted before being spliced into the prompt.
_SMOKE_COMPUTE = (
    '- Provide argparse with `--output` (CSV path, default "{output_file}") and '
    "`--smoke`. In --smoke mode, build the SMALLEST valid system and run the ACTUAL "
    "{property} computation once, end-to-end, at the cheapest valid settings (a tiny "
    "cell/molecule, minimal basis/cutoff, a single k-point) -- INCLUDING the real "
    "prediction/compute call and reading the returned value -- then write it and exit. "
    "Do NOT stub, skip, or hard-code the computation in --smoke: its purpose is to make "
    "a wrong API call, keyword argument, or return type fail fast here. Downloading the "
    "tool's own model weights / parameter files is allowed; do not use the network "
    "otherwise."
)
_SMOKE_LOAD_ONLY = (
    '- Provide argparse with `--output` (CSV path, default "{output_file}") and '
    "`--smoke`. In --smoke mode, build a SMALL system and construct/load the calculator "
    "(instantiate it, or load the pretrained model) to prove it is real and callable, "
    "then exit WITHOUT the expensive part (no large SCF, dense k-grid, or long dynamics) "
    "-- a full run needs external parameter files or is too costly for a smoke check. "
    "Downloading the tool's own model weights / parameter files is allowed; do not use "
    "the network otherwise."
)


# LLM code-synthesis prompt. The builder asks the gateway for a self-contained
# main.py tailored to the selected library + calculator + material + property;
# a deterministic template is the fallback when synthesis is unavailable/invalid.
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
pymatgen), and never substitute a different or more common polymorph (e.g. do not build \
rutile when anatase was requested). For a simple element or binary that `ase.build.bulk` \
supports, use it (it carries the correct experimental lattice constant); otherwise supply \
the standard reference lattice parameters and Wyckoff positions for the named polymorph -- \
these are structural INPUTS that define the cell, not the {property} you compute. Do NOT \
hardcode the {property} value itself or any other result you are meant to calculate.
- Attach the {calculator} calculator (`{calculator_import}`) and compute {property}. \
Do NOT invent model, dataset, or parameter-set identifiers -- a name you guess may \
not exist. If the calculator loads a named pretrained model, discover the valid \
identifier at runtime (e.g. call the library's "list available/pretrained models" \
API and select the one matching the task) rather than hardcoding a guessed string. \
Call every API with the argument types it documents.
- Keep the heavy imports (`{library_import}`, `{calculator_import}`) INSIDE functions \
so the module still imports where they are not installed.
- First thing in the `if __name__ == "__main__":` block, anchor the working \
directory to the script's own directory (`os.chdir(os.path.dirname(os.path.abspath(\
__file__)))`) so relative outputs and calculator scratch files land next to the \
script, never in the caller's working directory.
{smoke_instruction}
- Print a JSON object to stdout whose keys include {metric_keys} (the computed \
value(s)), plus "tool", "calculator", "property", and "output_file". Write the same \
metrics as one CSV row to --output.
- End the file with an `if __name__ == "__main__":` block that runs the script \
(calls your main function). Output the COMPLETE script in one reply -- do not stop \
partway or omit the entrypoint.
- Use the Python standard library, `{library_import}`, and `{calculator_import}`. \
{also_available} The script MUST compile and MUST succeed when run with --smoke.

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
Optimize the geometry first if the property needs a relaxed structure. Do NOT hardcode \
the {property} value or any other result you are meant to calculate.
- Compute {property} with {library}. Do NOT invent method, basis-set, functional, or \
parameter identifiers -- a name you guess may not exist. Use documented defaults or \
discover valid identifiers at runtime, and call every API with the argument types it \
documents.
- Keep the heavy import (`{library_import}`) INSIDE functions so the module still \
imports where it is not installed.
- First thing in the `if __name__ == "__main__":` block, anchor the working \
directory to the script's own directory (`os.chdir(os.path.dirname(os.path.abspath(\
__file__)))`) so relative outputs and calculator scratch files land next to the \
script, never in the caller's working directory.
{smoke_instruction}
- Print a JSON object to stdout whose keys include {metric_keys} (the computed \
value(s)), plus "tool", "property", and "output_file". Write the same metrics as one \
CSV row to --output.
- End the file with an `if __name__ == "__main__":` block that runs the script \
(calls your main function). Output the COMPLETE script in one reply -- do not stop \
partway or omit the entrypoint.
- Use the Python standard library and `{library_import}`. {also_available} The script \
MUST compile and MUST succeed when run with --smoke.

Acceptance criteria (JSON list of {{metric_name, target_value, tolerance}}): \
{acceptance_json}

Begin the script now."""


# The verify-and-repair prompts now live with the REPAIR stage in
# ``script_doctor`` -- BUILD only asks for the initial script (prompt above).


# --------------------------------------------------------------------------- #
# Minimal offline elemental-crystal table.
#
# Lets material-aware templates build the *requested* element (conventional cubic
# cell) instead of a hard-coded sample, with no ASE dependency at generation
# time. Returns None for anything not in the table, so the template keeps its
# built-in default. (symbol -> (structure, lattice constant in Å))
# --------------------------------------------------------------------------- #
_ELEMENTAL_CRYSTALS: Dict[str, tuple] = {
    "Si": ("diamond", 5.43), "Ge": ("diamond", 5.658), "C": ("diamond", 3.567),
    "Fe": ("bcc", 2.87), "W": ("bcc", 3.16), "Na": ("bcc", 4.23), "Cr": ("bcc", 2.88),
    "Cu": ("fcc", 3.61), "Al": ("fcc", 4.05), "Au": ("fcc", 4.08),
    "Ag": ("fcc", 4.09), "Ni": ("fcc", 3.52), "Pt": ("fcc", 3.92), "Pd": ("fcc", 3.89),
}

_CONVENTIONAL_BASIS: Dict[str, list] = {
    "fcc": [(0, 0, 0), (0.5, 0.5, 0), (0.5, 0, 0.5), (0, 0.5, 0.5)],
    "bcc": [(0, 0, 0), (0.5, 0.5, 0.5)],
    "diamond": [
        (0, 0, 0), (0.5, 0.5, 0), (0.5, 0, 0.5), (0, 0.5, 0.5),
        (0.25, 0.25, 0.25), (0.75, 0.75, 0.25), (0.75, 0.25, 0.75), (0.25, 0.75, 0.75),
    ],
}


def elemental_structure(formula: Optional[str]) -> Optional[dict]:
    """Build a conventional cubic cell for a known elemental crystal, else None.

    >>> s = elemental_structure("Si")
    >>> s["crystalstructure"], len(s["atoms"]), s["atoms"][0]["species"]
    ('diamond', 8, 'Si')
    >>> elemental_structure("Unobtanium") is None
    True
    >>> elemental_structure(None) is None
    True
    """
    if not formula:
        return None
    symbol = str(formula).strip()
    info = _ELEMENTAL_CRYSTALS.get(symbol)
    if info is None:
        return None
    structure, a = info
    matrix = [[a, 0.0, 0.0], [0.0, a, 0.0], [0.0, 0.0, a]]
    return {
        "formula": symbol,
        "crystalstructure": structure,
        "a": a,
        "lattice": matrix,
        "cell": matrix,
        "pbc": True,
        "atoms": [{"species": symbol, "coordinates": list(c)} for c in _CONVENTIONAL_BASIS[structure]],
        "coordinateSystem": "fractional",
    }


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

    def files(self) -> Dict[str, str]:
        """Map of filename -> contents for the bundle."""
        return {
            "main.py": self.main_py,
            "config.yaml": self.config_yaml,
            "requirements.txt": self.requirements_txt,
            "inline_tests.py": self.inline_tests_py,
        }

    def write(self, dest: Union[str, Path]) -> Path:
        """Write all four files into ``dest`` (created if needed); return it."""
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

    def __init__(self, templates_dir: Optional[Union[str, Path]] = None):
        self.templates_dir = (
            Path(templates_dir) if templates_dir
            else Path(__file__).resolve().parent / "templates"
        )

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
                 smoke_compute: bool = False) -> RunBundle:
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
            )
        # Library-only run. Prefer a dedicated, tested template when one fits the
        # tool (Pymatgen/ASE/RDKit). Otherwise, if the plan asks for a real property
        # and an agent is available, LLM-synthesize a script for the
        # library+material+property -- this is what stops a library that computes the
        # property itself (e.g. PySCF for a molecular HOMO-LUMO gap) from silently
        # falling back to the tool-agnostic stub, which computes nothing. Without an
        # agent (offline/tests) it still renders the deterministic template.
        spec = self.select_template(tool_name, hint=self._hint_from(plan, intent))
        if spec is _GENERIC and plan.get("requested_property") and agent is not None:
            return self._generate_with_calculator(
                plan, libraries, None, None, calculator_library,
                intent=intent, agent=agent, smoke_compute=smoke_compute,
            )
        return self._generate_standard(plan, tool_name, intent=intent)

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
                                  *, intent, agent, smoke_compute: bool = False) -> RunBundle:
        generated_at = (plan.get("metadata") or {}).get("timestamp", "") or ""
        acceptance = plan.get("acceptance_metrics", []) or []
        requested_property = plan.get("requested_property") or "the requested property"

        # The calculator is driven through this library (e.g. GPAW via ASE); the
        # rest of the toolset is available too (e.g. Pymatgen for structure work).
        driver = calculator_library or (libraries[0] if libraries else "ASE")
        driver_deps = _depinf.import_names(driver)
        driver_import = driver_deps[0].import_name if driver_deps else canonical_tool_key(driver)
        also = [lib for lib in libraries if lib.lower() != driver.lower()]
        material = self._material_brief(plan, intent)

        brief = {
            "library": driver,
            "library_import": driver_import,
            "also_available": also,
            "calculator": calculator,
            "calculator_import": calculator_import,
            "property": requested_property,
            "material": material,
            "material_desc": self._material_desc(material),
            "acceptance": acceptance,
            "output_file": "results.csv",
            # When True, the generated --smoke path runs the real (tiny) computation
            # so an API/keyword error is caught in REPAIR, not at the real run.
            "smoke_compute": smoke_compute,
        }

        # The script is written by the LLM from the discovered toolset + material
        # + property -- there is NO per-property template. If the gateway is
        # unavailable or returns invalid Python, fall back to a tool-agnostic
        # scaffold (loads the toolset, writes a stub) -- never a preset.
        main_py = self._synthesize_with_llm(brief, agent) if agent is not None else None
        template_name = "llm_synthesized"
        if main_py is None:
            main_py = self._render_generic_fallback(brief, generated_at, acceptance)
            template_name = _GENERIC.filename

        # requirements + smoke imports cover the whole toolset (every library, plus
        # the calculator when one is attached), deduped.
        requirements_txt = self._requirements_for_toolset(libraries, calculator)
        toolset_imports = [d.import_name for lib in libraries for d in _depinf.import_names(lib)]
        if calculator:
            toolset_imports += [d.import_name for d in _depinf.import_names(calculator)]
        import_names = list(dict.fromkeys(toolset_imports))
        inline_tests_py = _smoke.generate_inline_tests(
            tool_name="+".join(libraries + ([calculator] if calculator else [])),
            required_import_names=import_names,
            main_filename="main.py",
            output_filename="results.csv",
            run_smoke=True,
        )
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
        try:
            raw = agent(self._codegen_prompt(brief))
        except Exception:  # noqa: BLE001 - any agent failure -> caller falls back to a template
            return None
        # Require the script to reference the calculator (calculator run) or, for a
        # library-only run, the library itself -- so a stub that never touches the
        # tool is rejected and the caller falls back to the deterministic template.
        must_reference = brief.get("calculator_import") or brief.get("library_import")
        return extract_valid_source(raw, must_reference)

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
        return f"{base}, {', '.join(quals)}" if quals else base

    @staticmethod
    def _requirements_for_toolset(libraries: List[str], calculator: Optional[str]) -> str:
        """requirements.txt covering every library in the toolset (+ the calculator)."""
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
            unique.append(dep.requirement_line())
        header = "# Auto-generated by TWAIN code_configuration_builder -- pinned for reproducibility."
        return "\n".join([header, *unique]) + "\n"

    @staticmethod
    def _structure_for(plan: dict, intent: Optional[dict]) -> dict:
        """Structure baked into material-aware standard templates (see task 6).

        Returns ``{}`` when the material can't be resolved to a crystal, so the
        template keeps its built-in sample.
        """
        material = CodegenEngine._material_brief(plan, intent)
        return elemental_structure(material.get("formula")) or {}

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
