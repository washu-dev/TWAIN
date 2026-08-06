"""ScriptDoctor: verify, proactively review, and repair a generated main.py.

This is the engine behind the pipeline's REPAIR stage. BUILD (the
:class:`code_gen.codegen_engine.CodegenEngine`) emits a *plausible* ``main.py``
-- it compiles, references the chosen calculator, and has an entrypoint -- but
"compiles" is a low bar: a script can still guess a model identifier that does
not exist, misuse an API, or (after a truncated synthesis reply) define a
function it never finishes. ScriptDoctor turns that plausible script into one
that actually runs.

It works in two phases, both driven by structured :class:`Diagnostic` findings
so the caller can log exactly what was wrong and what changed:

1. **Correctness.** Static checks (does it compile? does it have a runnable
   entrypoint? does it reference the calculator? are any names used-but-never-
   defined -- the fingerprint of truncation?) plus a ``--smoke`` run in the heavy
   -calculator env. Any hard error is handed back to the model with the real
   diagnostics; the fix is re-checked. Repeats up to ``max_rounds``.

2. **Proactive hardening.** Once the script is runnable, one LLM *review* pass
   scans the whole script for latent bugs that a smoke run would not surface
   (wrong argument types, unit/shape mistakes, logic that doesn't compute the
   requested property). Anything it flags as an error is fixed and re-verified,
   so bugs are caught *before* the expensive real run rather than after.

Design notes
------------
* **No network / no LLM needed to be useful.** With ``agent=None`` (the default
  offline path) the doctor still runs every static check and reports findings; it
  simply can't apply LLM fixes. With no sim-env interpreter, the smoke phase
  degrades to ``unverifiable`` instead of blocking. It never raises on an
  environment gap -- repair is best-effort hardening, and EXECUTE remains the
  real gate.
* **Injectable seams for tests.** ``agent`` (prompt -> str), ``verifier``
  ((source, brief) -> :class:`SmokeOutcome`) and ``sim_python`` are all
  injectable so the loop can be exercised fully offline and deterministically.
* **Single acceptance bar.** Repaired candidates are validated with the same
  :func:`code_gen.codegen_engine.extract_valid_source` BUILD uses, so a repair
  can never lower the bar the initial synthesis had to clear.
"""
from __future__ import annotations

import ast
import builtins
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

# Reuse the engine's env plumbing + static validators so BUILD and REPAIR share
# one acceptance bar (see module docstring). The doctor depends on the engine;
# the engine never depends on the doctor -- no import cycle.
try:  # pragma: no cover - import shim (package alias vs. bare name)
    from code_gen.codegen_engine import (
        SIM_ENV, extract_valid_source, has_runnable_entrypoint,
        interpolated_placeholder_tokens, pixi_env_python, strip_code_fences,
        wants_thermo_cycle, _LEFTOVER_PLACEHOLDER,
    )
except ImportError:  # pragma: no cover
    from codegen_engine import (
        SIM_ENV, extract_valid_source, has_runnable_entrypoint,
        interpolated_placeholder_tokens, pixi_env_python, strip_code_fences,
        wants_thermo_cycle, _LEFTOVER_PLACEHOLDER,
    )

# Sentinel so ``sim_python=None`` ("explicitly no interpreter, skip smoke") is
# distinguishable from "not provided, resolve it yourself".
_UNSET = object()

# How long a single --smoke run may take before we treat it as unverifiable.
_SMOKE_TIMEOUT_S = 300


@dataclass(frozen=True)
class SmokeOutcome:
    """Result of running a candidate script with ``--smoke`` in the sim env.

    ``status`` is one of:
      * ``"pass"``          -- the script ran, loaded the calculator, and wrote its
                               output; accept it.
      * ``"repairable"``    -- a real code bug (bad identifier, misused API, a run
                               that produced no output); the ``error`` is fed back
                               to the model to self-correct.
      * ``"unverifiable"``  -- we couldn't verify here (env not built, calculator/
                               library not importable in the env, or a network
                               failure); accept the compiling script as-is.
    """

    status: str
    error: str = ""


@dataclass(frozen=True)
class Diagnostic:
    """One problem found in a script.

    ``source`` records which check produced it (``compile``, ``entrypoint``,
    ``calculator``, ``undefined-name``, ``placeholder``, ``smoke``, ``review``);
    ``severity`` is ``error`` (blocks a correct run) or ``warning`` (worth fixing
    but not fatal). ``line`` is 1-based when known.
    """

    source: str
    severity: str
    message: str
    line: Optional[int] = None

    def render(self) -> str:
        where = f" (line {self.line})" if self.line else ""
        return f"[{self.severity}:{self.source}]{where} {self.message}"


@dataclass
class HealReport:
    """Outcome of :meth:`ScriptDoctor.heal`.

    ``status`` is one of:
      * ``"healthy"``      -- passed every check unchanged.
      * ``"repaired"``     -- one or more fixes were applied and the result passes.
      * ``"unverifiable"`` -- statically clean, but the smoke run couldn't execute
                              here (no sim env / calculator absent).
      * ``"unrepairable"`` -- still has blocking errors (no agent, agent gave up,
                              or the round budget was exhausted).

    ``source`` is the best script the doctor produced (possibly unchanged).
    ``fixes`` is a human-readable log of what each repair round addressed, and
    ``remaining`` lists diagnostics still present at the end.
    """

    source: str
    status: str
    rounds: int = 0
    fixes: List[str] = field(default_factory=list)
    remaining: List[Diagnostic] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.status == "repaired"

    @property
    def healthy(self) -> bool:
        """Whether the script is safe to hand to EXECUTE (no known blockers)."""
        return self.status in ("healthy", "repaired", "unverifiable")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "rounds": self.rounds,
            "fixes": list(self.fixes),
            "remaining": [d.render() for d in self.remaining],
        }


# --------------------------------------------------------------------------- #
# Prompts. Both are domain-agnostic -- they describe *how* to review/repair a
# reliable script, never *what* property or material to expect.
# --------------------------------------------------------------------------- #
_REVIEW_PROMPT = """You are reviewing a Python script (main.py) that computes \
{property} of {material_desc} using the {library} library with the {calculator} \
calculator. Statically review it for LATENT bugs that would make it crash or \
produce a wrong result at run time -- for example: calling a library API with the \
wrong argument types or arity, using a model/dataset/parameter identifier that may \
not exist, unit or array-shape mistakes, an uninitialized or undefined variable, a \
resource blow-up (dense k-grid or huge cell in the main path), or logic that does \
not actually compute {property}. Flag SCIENTIFIC-CORRECTNESS defects too: a built \
structure with the wrong stoichiometry or atom count, or physically impossible \
geometry (atoms fused far below a bond length, a doubled or overlapping cell, a \
wrong space-group setting or origin choice) -- flag these so the structure-building \
code can be CORRECTED, not so a runtime guard can be added; \
computing {property} at an unrelaxed geometry when it is only defined at \
equilibrium; numerical settings far too coarse to converge {property} in the main \
run; reporting a cheaper proxy that does not match the quantity or averaging scheme \
{property} names; or a value reported without its physical unit.

Do NOT report style, formatting, naming, or import-placement preferences. Only \
report real, actionable bugs.

Return ONLY a JSON array (no prose, no markdown fences). Each element must be an \
object: {{"severity": "error" or "warning", "line": <1-based int or null>, \
"message": "<what is wrong and how to fix it>"}}. Return [] if you find nothing.

--- main.py ---
{source}
--- end main.py ---"""


_REPAIR_PROMPT = """A Python script (main.py) that computes {property} of \
{material_desc} using {library} + the {calculator} calculator has problems. Return \
the COMPLETE corrected script (Python source only -- no markdown fences, no prose). \
Keep everything that already works and fix the root cause of every problem listed.

--- problems found ---
{problems}
--- end problems ---

--- current main.py ---
{source}
--- end main.py ---

How to fix:
- If a model/dataset/parameter identifier was not found (a 401/404, "Repository Not \
Found", or "No valid model" error), you used a name that does not exist. Discover \
the valid identifier at runtime from the library (list its available/pretrained \
models and pick the one matching the task) instead of guessing.
- If an argument had the wrong type or an API was misused (TypeError, \
AttributeError), match the library's documented signature and types.
- If a variable is undefined or the script looks truncated, complete the logic.
- Do NOT hardcode physical constants, lattice parameters, or the expected answer; \
build the system from the library's reference data and actually compute the value.
- If the built structure is wrong (a previous attempt hallucinated the wrong cell, \
stoichiometry, or space-group setting), OVERWRITE the structure-building code with a \
correct version that builds the right cell (correct lattice parameters, Wyckoff \
positions, and stoichiometric ratio for the formula). Do NOT add runtime guards that \
raise or exit on an unexpected composition, atom count, or symmetry, and REMOVE any such \
guard already present in the script -- fix the structure itself rather than halting on it.
- If {property} needs an equilibrium structure, relax the geometry first; if the \
numerical settings are too coarse, raise them to converged values; if the script \
computes a proxy, replace it with the quantity {property} actually names, and report \
the value with its physical unit.
- Keep the same libraries, calculator, material, property, argparse flags \
(--output and --smoke), and the JSON-to-stdout + CSV-to-output contract.
- End the file with an `if __name__ == "__main__":` block that runs it. Output the \
COMPLETE script in one reply -- do not stop partway or omit the entrypoint.

Return the full corrected script now."""


class ScriptDoctor:
    """Verify and repair a synthesized ``main.py`` (the REPAIR stage's engine).

    ``agent`` is a ``prompt -> str`` callable (the live gateway, already wrapped
    with a generous output budget) or ``None`` for a static-only pass. ``brief``
    carries the run's context (calculator/library import names, property,
    material description, acceptance metrics, output filename) used both to check
    invariants and to give the model context in prompts. ``verifier`` and
    ``sim_python`` are injectable so the smoke phase runs offline in tests.
    """

    def __init__(self, *, agent: Optional[Callable[[str], str]] = None,
                 brief: Optional[dict] = None,
                 sim_python: Union[str, None, object] = _UNSET,
                 verifier: Optional[Callable[[str, dict], SmokeOutcome]] = None,
                 max_rounds: int = 3, review: bool = True,
                 bundle_files: Optional[Dict[str, str]] = None):
        self.agent = agent
        self.brief = dict(brief or {})
        # Extra bundle files (filename -> source) placed beside main.py in the
        # smoke sandbox. A bundle can now carry helper modules the script imports
        # by bare name -- twain_pseudo.py, twain_thermo.py -- and smoking main.py
        # alone makes those imports fail for a reason that is not the code's
        # fault. Worse, the cheapest repair for "No module named twain_pseudo" is
        # to drop the import and inline the pseudopotential filenames, which is
        # exactly what that helper exists to prevent.
        self.bundle_files = dict(bundle_files or {})
        self._sim_python = sim_python
        self._verifier = verifier
        self.max_rounds = max(1, max_rounds)
        self.review_enabled = review

    # -- public API ----------------------------------------------------------
    def heal(self, source: str) -> HealReport:
        """Diagnose, repair, and proactively harden ``source``; return a report.

        Never raises on an environment gap: with no agent it reports findings it
        can't fix; with no sim env it skips the smoke run. The returned
        ``HealReport.source`` is always usable (the best script produced).
        """
        if not isinstance(source, str) or not source.strip():
            return HealReport(source or "", "unrepairable", 0, [],
                              [Diagnostic("input", "error", "empty script")])

        fixes: List[str] = []
        rounds = 0
        last_smoke: Optional[SmokeOutcome] = None

        # -- phase 1: correctness (static + smoke), repair-driven --------------
        while rounds < self.max_rounds:
            static = self.static_diagnostics(source)
            errors = [d for d in static if d.severity == "error"]
            last_smoke = None
            if not errors:
                last_smoke = self.smoke(source)
                if last_smoke.status == "repairable":
                    errors = [Diagnostic("smoke", "error", last_smoke.error)]
            if not errors:
                break  # runnable: compiles, has an entrypoint, and smoke is clean

            if self.agent is None:
                # Static/dynamic problems found but nothing can fix them here.
                return HealReport(source, "unrepairable", rounds, fixes, static or errors)

            warnings = [d for d in static if d.severity == "warning"]
            fixed = self._repair(source, errors + warnings)
            rounds += 1
            if not fixed or fixed == source:
                return HealReport(source, "unrepairable", rounds, fixes, static or errors)
            fixes.append(f"round {rounds}: " + _summarize(errors))
            source = fixed
        else:
            # Budget exhausted with errors still present.
            return HealReport(source, "unrepairable", rounds, fixes,
                              self.static_diagnostics(source))

        # -- phase 2: proactive hardening (one LLM review of a runnable script) -
        if self.agent is not None and self.review_enabled:
            findings = self.review(source)
            # Static warnings ride along here rather than in phase 1. Phase 1 only
            # repairs when an error exists, and promoting a warning to an error
            # would let an unfixable resource-utilisation nit block a
            # scientifically-correct run. Here a failed fix keeps the runnable
            # script, which is the right trade for "correct but wasteful".
            warnings = [d for d in self.static_diagnostics(source)
                        if d.severity == "warning"]
            actionable = [d for d in findings if d.severity == "error"] + warnings
            if actionable:
                fixed = self._repair(source, findings + warnings)
                # A hardening fix must not regress the script -- neither the hard
                # static checks NOR the runtime. Static-only acceptance let a review
                # that introduced a bad-but-syntactically-valid API call (e.g.
                # `matgl.get_available_models()`) slip through to EXECUTE. So we
                # re-verify: accept the review only if it still passes static AND
                # smokes clean (repairing a runtime break the review introduced);
                # otherwise keep the runnable pre-review script.
                if fixed and fixed != source and not _has_errors(self.static_diagnostics(fixed)):
                    verified = self._smoke_repair(fixed)
                    if verified is not None:
                        source = verified
                        rounds += 1
                        fixes.append(f"proactive review: addressed {len(actionable)} latent issue(s)")

        return HealReport(source, _final_status(fixes, last_smoke), rounds, fixes,
                          self.static_diagnostics(source))

    def repair_runtime(self, source: str, failure: str) -> Optional[str]:
        """Repair ``source`` against a failure from the REAL run (post-EXECUTE).

        This is the general net behind the per-incident static checks: those
        catch the failure modes we've already seen, while ANY novel API misuse
        the script commits surfaces as a runtime traceback -- the ground truth
        -- which one repair round here turns into a fixed script the caller
        can re-execute. The result must still pass the static checks and (when
        a sim env exists) smoke clean, so a "fix" can never regress the bundle.
        Returns the healed script, or ``None`` when nothing better could be
        produced (no agent, unchanged output, or the fix failed verification).
        """
        if self.agent is None or not (source or "").strip() or not (failure or "").strip():
            return None
        fixed = self._repair(
            source, [Diagnostic("runtime-failure", "error", failure)])
        if not fixed or fixed == source or _has_errors(self.static_diagnostics(fixed)):
            return None
        return self._smoke_repair(fixed)

    def _smoke_repair(self, source: str) -> Optional[str]:
        """Smoke ``source`` and repair a runtime break, bounded by ``max_rounds``.

        Assumes ``source`` already passes the static checks. Returns a version that
        smokes clean (``pass``/``unverifiable``), or ``None`` when a ``repairable``
        runtime break can't be fixed here -- so the caller can keep the previous,
        known-good script instead of shipping a regression. Used to re-verify the
        proactive review's output (BUILD's initial script is verified by phase 1).
        """
        for _ in range(self.max_rounds):
            outcome = self.smoke(source)
            if outcome.status != "repairable":
                return source  # clean (or can't-verify-here) -> good enough
            if self.agent is None:
                return None
            fixed = self._repair(source, [Diagnostic("smoke", "error", outcome.error)])
            if not fixed or fixed == source or _has_errors(self.static_diagnostics(fixed)):
                return None
            source = fixed
        return None

    # -- diagnosis -----------------------------------------------------------
    def static_diagnostics(self, source: str) -> List[Diagnostic]:
        """All problems detectable without running the script.

        Ordered cheapest-first. A syntax error short-circuits the rest (the AST
        the other checks need can't be built).
        """
        try:
            compile(source, "main.py", "exec")
        except SyntaxError as exc:
            return [Diagnostic("compile", "error",
                               f"does not compile: {exc.msg}", exc.lineno)]

        diags: List[Diagnostic] = []
        if not has_runnable_entrypoint(source):
            diags.append(Diagnostic(
                "entrypoint", "error",
                "no runnable entrypoint: the module defines functions but never "
                "calls them (add `if __name__ == \"__main__\":`). Often a sign the "
                "script was truncated."))
        calc = self.brief.get("calculator_import")
        if calc and calc.lower() not in source.lower():
            diags.append(Diagnostic(
                "calculator", "error",
                f"never references the required calculator import '{calc}'."))
        # The mirror of the check above. That one catches a script IGNORING the
        # planned calculator; this catches a script INVENTING one the plan never
        # selected -- and only fires when the brief actually carries a plan, so
        # other callers are unaffected.
        if "calculator" in self.brief and not self.brief.get("calculator"):
            sites = _energy_evaluation_sites(source)
            if sites:
                shown = ", ".join(f"{what} (line {line})" for what, line in sites[:3])
                diags.append(Diagnostic(
                    "calculator-not-planned", "error",
                    f"evaluates energies but the plan selected no calculator "
                    f"(tool_name='{self.brief.get('library')}', calculator=null): "
                    f"{shown}. A library-only plan carries no engine and its "
                    f"requirements.txt lists none, so whatever this imports is "
                    f"absent at run time -- a NaCl2 run reached EXECUTE and died on "
                    f"'No module named matgl' after trying MACE, CHGNet and M3GNet, "
                    f"none of which is provisioned (Slurm job 2633871). Either "
                    f"compute the property with the planned libraries alone, or "
                    f"retrieve it rather than calculating it. Do NOT import an "
                    f"engine the plan did not choose: the plan is what was "
                    f"approved, resourced and provisioned for.",
                    sites[0][1]))
        for what, line in _preconverted_thermo_energy(source):
            diags.append(Diagnostic(
                "thermo-units", "error",
                f"{what} before being handed to twain_thermo, which converts to "
                f"kJ/mol itself -- so the reaction term is converted TWICE while "
                f"the kJ/mol reference enthalpies are not. That is how a CO2 "
                f"standard heat of formation came back as -27452 kJ/mol instead "
                f"of -393.8 (run 1cd39ffd): wrong by a factor, not slightly off, "
                f"so no accuracy check catches it. Pass the number the calculator "
                f"returned and NAME its unit -- `species(sym, psi4.energy(...), "
                f"unit='Hartree', correction=thermo.get_enthalpy(T), "
                f"correction_unit='eV')` -- or, if you keep the conversion, "
                f"declare it with `unit='kJ/mol'`. Converting toward eV "
                f"(`e_hartree * Hartree`) is fine and is not what this is about.",
                line))
        for name, line in _undefined_names(source):
            diags.append(Diagnostic(
                "undefined-name", "error",
                f"name '{name}' is used but never defined, imported, or assigned "
                f"(likely a typo or a truncated script).", line))
        # Minus the f-string fields and .format() keys, which are spelled the same
        # way and are ordinary Python -- see interpolated_placeholder_tokens.
        leftover = (set(_LEFTOVER_PLACEHOLDER.findall(source))
                    - interpolated_placeholder_tokens(source))
        for token in sorted(leftover):
            diags.append(Diagnostic(
                "placeholder", "warning",
                f"unfilled template placeholder {token} left in the script."))
        for name, line in _stale_ase_filter_imports(source):
            diags.append(Diagnostic(
                "ase-filters", "error",
                f"imports {name} from `ase.constraints`, but in ASE >= 3.23 cell "
                f"filters live in `ase.filters` (use `from ase.filters import "
                f"{name}`); the old path raises ImportError at runtime.", line))
        for line in _fixed_occupations_without_numbers(source):
            diags.append(Diagnostic(
                "gpaw-occupations", "error",
                'uses occupations={"name": "fixed"} without a `numbers` array: '
                'GPAW\'s "fixed" mode requires explicit per-band occupation '
                'numbers and raises TypeError at calculator init. For a '
                'frozen-occupations band-structure pass use '
                '{"name": "fixed-uniform"} instead.', line))
        for line in _engine_launch_ignored(
                source, str(self.brief.get("parallelism") or "threads")):
            diags.append(Diagnostic(
                "engine-launch-ignored", "warning",
                "never reads TWAIN_ENGINE_LAUNCH, so this run will use one core of "
                "the allocation. This engine is a separate program and the job "
                "gives its ranks to the ENGINE, not to this script -- the script is "
                "run single-process on purpose, because several ranks of it would "
                "each drive their own copy of the engine in this one directory and "
                "overwrite each other's files. Read "
                "`os.environ.get('TWAIN_ENGINE_LAUNCH', '')` and prefix it onto the "
                "engine command handed to the calculator (its `command=` argument, "
                "or its Profile's) so the allocated cores are actually used.", line))
        for calc_name, missing, line in _element_limited_calculator(
                source, self.brief.get("formula")):
            supported = " ".join(sorted(_ELEMENT_LIMITED_CALCULATORS[calc_name]))
            diags.append(Diagnostic(
                "element-coverage", "error",
                f"uses {calc_name} for a material containing "
                f"{', '.join(missing)}, which {calc_name} has no parameters for "
                f"(it covers only: {supported}). The first energy evaluation "
                f"raises NotImplementedError from inside initialize(), and if "
                f"that is swallowed the next one fails as \"'{calc_name}' object "
                f"has no attribute 'nl'\" -- an error that says nothing about the "
                f"real cause. Use a potential that covers these elements, or let "
                f"the missing library fail so the method can be replanned.", line))
        for attempted, fallback, line in _silent_calculator_substitution(source):
            diags.append(Diagnostic(
                "calculator-substitution", "error",
                f"falls back from '{attempted}' to a calculator from '{fallback}' "
                f"when the import fails. That silently changes the method: the run "
                f"reports whatever the provenance string claims while a different "
                f"potential produced the number, and a wrong-but-successful result "
                f"is worse than a failed one. Import the planned library "
                f"unconditionally and let a missing one raise, so REPLAN can "
                f"choose a method this cluster has.", line))
        for line in _uncorrelated_method_for_thermochemistry(
                source, self.brief.get("property")):
            diags.append(Diagnostic(
                "uncorrelated-thermochemistry", "error",
                "selects a bare Hartree-Fock method (scf/hf) for a property "
                "assembled from bond energies. Correlation is most of a bond's "
                "energy, so HF underestimates every bond by a large systematic "
                "amount and the run reports a clean, confident, badly wrong "
                "number -- for CO2 an atomization of ~1012 kJ/mol against ~1628, "
                "putting the heat of formation at +245.7 where the answer is "
                "-393.5, i.e. the wrong SIGN. Use at least a hybrid functional "
                "(B3LYP, PBE0, wB97X-D) or a correlated wavefunction method "
                "(MP2, CCSD(T)).", line))
        for line in _ambiguous_spin_specification(source):
            diags.append(Diagnostic(
                "ambiguous-spin-state", "error",
                "states an open-shell spin state as an explicit count "
                "(multiplicity / mult / uhf) without reconciling the other "
                "channel, the Atoms object's initial magnetic moments. ASE "
                "calculators read one or the other -- GPAW, Quantum ESPRESSO, "
                "ABINIT and DFTB+ take the magnetic moments; Psi4, CP2K, NWChem "
                "and xTB take the count -- and when both are present the loser is "
                "discarded SILENTLY, converging the WRONG SPIN STATE with no "
                "warning and plausible-looking forces. ASE's Psi4 even overwrites "
                "reference with 'uhf' and multiplicity with None when the atoms "
                "carry moments, and ase.build.molecule sets moments on exactly the "
                "open-shell species that need a multiplicity: that combination put "
                "a CO2 heat of formation at -1072 vs a -393.5 kJ/mol target. Fix: "
                "state the spin ONCE for each species. If the calculator reads the "
                "count, clear the moments first with "
                "`atoms.set_initial_magnetic_moments([0.0] * len(atoms))`; if it "
                "reads the moments, set them explicitly and drop the count "
                "keyword. Either way make it explicit in the source.", line))
        for line in _signature_probe_calls(source):
            diags.append(Diagnostic(
                "signature-probe", "error",
                "gates behavior on inspect.signature(): ASE-style calculators "
                "(e.g. xtb-python's XTB) take **kwargs and route options "
                "through default_parameters, so the probe falsely reports "
                "keywords like 'solvent' as unsupported and aborts a runnable "
                "job. Pass the documented keywords directly and let a real "
                "TypeError surface.", line))
        for line in _hardcoded_bandpath_calls(source):
            diags.append(Diagnostic(
                "bandpath-literal", "error",
                "hardcodes a band-path string in `.bandpath(...)`: the special "
                "points available depend on the lattice ASE detects in the "
                "ACTUAL cell, and after a relaxation the (noisy) cell is often "
                "no longer recognized as the ideal lattice -- a hardcoded "
                "letter then raises KeyError after the ground state was "
                "already computed. Call `atoms.cell.bandpath(npoints=..., "
                "pbc=atoms.pbc)` with NO path string so ASE picks the standard "
                "path for the detected lattice, or build the string only from "
                "letters in `atoms.cell.bandpath().special_points`.", line))
        for line in _scf_grid_bandgap_calls(source):
            diags.append(Diagnostic(
                "bandgap-on-scf-grid", "error",
                "reads the fundamental band gap straight off the SCF k-grid "
                "(`bandgap(calc)` after the ground state): band extrema "
                "generally lie BETWEEN grid points (silicon's CBM is at ~0.85 "
                "of Gamma->X, which no uniform grid samples), so the gap comes "
                "out too large -- the script runs cleanly and the number is "
                "silently wrong. Converge the density on the SCF grid, then "
                "run a NON-self-consistent fixed-density pass along the "
                "standard path and take extrema from THAT: `bs_calc = "
                "calc.fixed_density(kpts=atoms.cell.bandpath(npoints=200, "
                "pbc=atoms.pbc), symmetry='off')` then "
                "`bandgap(bs_calc, direct=False)`.", line))
        diags.extend(self._primitive_cell_diagnostics(source))
        diags.extend(self._dft_budget_diagnostics(source))
        return diags

    def _dft_budget_diagnostics(self, source: str) -> List[Diagnostic]:
        """Flag numerical settings far beyond the default-accuracy budget.

        The wall-clock killer behind Slurm job 2472788: nobody asked for high
        accuracy, but the script ran PW(600) with a 12x12x12 grid (182
        irreducible k-points on 6-atom CaPt2 -- ~10 minutes per SCF
        iteration), so the FIRST of its 18 EOS points consumed the whole
        4-hour wall time. Prompt guidance alone keeps losing to the model's
        conservatism, so the budget is mechanical; it stands down when the
        researcher's own request asks for accuracy/convergence.
        """
        asked = " ".join(
            str(self.brief.get(k) or "")
            for k in ("objective", "property", "material_desc")
        ).lower()
        if any(word in asked for word in _ACCURACY_WORDS):
            return []
        return [
            Diagnostic(
                "dft-budget", "error",
                f"uses {desc}: far beyond the default cost budget, and the "
                "researcher did not ask for high accuracy. Default protocol: "
                "plane-wave cutoff <= 450 eV (350-400 eV is fine for metals "
                "with PAW), k-grid <= 8x8x8 for a primitive cell, and ONE "
                "equation-of-state scan of 5-7 points (about +-5% volume) -- "
                "that resolves bulk properties to a few percent in minutes "
                "instead of blowing the job's wall clock.", line)
            for desc, line in _extravagant_dft_settings(source)
        ]

    def _primitive_cell_diagnostics(self, source: str) -> List[Diagnostic]:
        """Flag conventional-cell builds the researcher never asked for.

        Plane-wave DFT cost grows ~cubically with the atom count, so a
        ``crystal(...)`` call with ``primitive_cell=False`` (or omitted -- ASE
        defaults to the conventional cell) turns a minutes-long bulk-property
        run into hours (a 24-atom conventional CaPt2 EOS vs the 6-atom
        primitive cell). The codegen prompt already demands the primitive
        cell; this makes the rule mechanical. It stands down whenever the
        researcher's own request/material mentions the conventional cell or a
        genuinely bigger system (supercell, surface, defect, ...): an explicit
        instruction always beats the fast default.
        """
        asked = " ".join(
            str(self.brief.get(k) or "")
            for k in ("objective", "property", "material_desc")
        ).lower()
        if any(word in asked for word in _EXPLICIT_CELL_WORDS):
            return []
        return [
            Diagnostic(
                "primitive-cell", "error",
                "builds the CONVENTIONAL cell: this `crystal(...)` call must pass "
                "`primitive_cell=True` for a bulk property (the researcher did not "
                "ask for a conventional cell or supercell). Run the calculation on "
                "the primitive cell and convert any conventional-cell quantity "
                "(e.g. a cubic lattice parameter) from the primitive result in "
                "code; update any atom-count self-checks/assertions to the "
                "primitive count.", line)
            for line in _conventional_cell_calls(source)
        ]

    def smoke(self, source: str) -> SmokeOutcome:
        """Run ``source`` with ``--smoke`` in the sim env and classify the result.

        Best-effort and side-effect-free: the script runs in a throwaway temp dir.
        Any inability to actually verify (no interpreter, spawn/timeout failure) is
        reported as ``unverifiable`` so the caller accepts the compiling candidate.
        A zero exit that produced no output file is a real bug (``repairable``) --
        it is how a truncated, entrypoint-less script fails silently.
        """
        if self._verifier is not None:
            return self._verifier(source, self.brief)  # injected verifier (tests)
        python = self._resolve_sim_python()
        if not python:
            return SmokeOutcome("unverifiable", "no sim-env interpreter to verify with")
        output_file = self.brief.get("output_file") or "results.csv"
        # Mirror the execution adapter's env plumbing so a *calculator* smoke can
        # actually run here: put the interpreter's own bin/ first on PATH (else an
        # ASE calculator that shells out to `dftb+`/`nwchem` hits command-not-found
        # -- exit 127, which would be misread as a code bug and loop the repair),
        # and default DFTB_PREFIX to the fetched slako/ dir so DFTB+ finds its
        # Slater-Koster files. Running the sim interpreter by path does NOT activate
        # its env, so neither is set otherwise.
        env = {**os.environ}
        env["PATH"] = str(Path(python).parent) + os.pathsep + env.get("PATH", "")
        if not env.get("DFTB_PREFIX"):
            try:
                from twain_paths import SLAKO_DIR
                if SLAKO_DIR.is_dir():
                    env["DFTB_PREFIX"] = str(SLAKO_DIR) + os.sep
            except Exception:  # noqa: BLE001 - best-effort default only
                pass
        try:
            with tempfile.TemporaryDirectory(prefix="twain_repair_") as tmp:
                (Path(tmp) / "main.py").write_text(source, encoding="utf-8")
                for name, text in self.bundle_files.items():
                    (Path(tmp) / name).write_text(text, encoding="utf-8")
                proc = subprocess.run(
                    [python, "main.py", "--smoke"],
                    cwd=tmp, capture_output=True, text=True, timeout=_SMOKE_TIMEOUT_S,
                    env=env,
                )
                produced = (Path(tmp) / output_file).is_file()
        except (OSError, subprocess.SubprocessError):
            return SmokeOutcome("unverifiable", "could not run the smoke verification")
        if proc.returncode == 0:
            if produced:
                return SmokeOutcome("pass")
            return SmokeOutcome(
                "repairable",
                f"the script exited 0 but wrote no output file ('{output_file}'). "
                f"In --smoke mode it must build a small system, load the calculator, "
                f"and write the metrics row. Ensure the entrypoint actually runs.")
        return self._classify_smoke((proc.stderr or "") + (proc.stdout or ""))

    def review(self, source: str) -> List[Diagnostic]:
        """Ask the model to scan a runnable script for latent bugs.

        Returns ``[]`` when there is no agent, the reply doesn't parse, or nothing
        was found -- the proactive pass is advisory and must never wedge the loop.
        """
        if self.agent is None:
            return []
        prompt = _REVIEW_PROMPT.format(source=source, **self._prompt_context())
        try:
            raw = self.agent(prompt)
        except Exception:  # noqa: BLE001 - any agent failure -> no findings
            return []
        return _parse_findings(raw)

    # -- repair --------------------------------------------------------------
    def _repair(self, source: str, diagnostics: List[Diagnostic]) -> Optional[str]:
        """One repair call: hand the model the script + findings, validate the fix.

        Returns a compiling, entrypoint-bearing, calculator-referencing script, or
        ``None`` when the agent is absent/fails or the reply doesn't pass the bar.
        """
        if self.agent is None:
            return None
        problems = "\n".join(f"{i}. {d.render()}" for i, d in enumerate(diagnostics, 1))
        prompt = _REPAIR_PROMPT.format(
            source=source, problems=problems, **self._prompt_context())
        try:
            raw = self.agent(prompt)
        except Exception:  # noqa: BLE001 - agent failure -> no fix this round
            return None
        return extract_valid_source(raw, self.brief.get("calculator_import"))

    # -- internals -----------------------------------------------------------
    def _prompt_context(self) -> Dict[str, str]:
        """The material/tool context both prompts interpolate (with safe defaults)."""
        b = self.brief
        return {
            "property": b.get("property") or "the requested property",
            "material_desc": b.get("material_desc") or "the requested material",
            "library": b.get("library") or b.get("library_import") or "the library",
            "calculator": b.get("calculator") or b.get("calculator_import") or "the calculator",
        }

    def _resolve_sim_python(self) -> Optional[str]:
        """The heavy-calculator interpreter to verify in, or None if unavailable."""
        if self._sim_python is not _UNSET:
            return self._sim_python  # injected (a path, or None to force skip)
        return pixi_env_python(SIM_ENV)

    def _classify_smoke(self, output: str) -> SmokeOutcome:
        """A non-zero smoke run -> repairable code bug vs. can't-verify-here.

        Missing top-level toolset packages and network failures mean the env (not
        the code) is the blocker -> ``unverifiable``. A missing *submodule* of an
        installed package, or any other runtime error (bad model id, wrong argument
        type, ...) is a real bug in the generated code -> ``repairable``.
        """
        tail = "\n".join(output.strip().splitlines()[-25:])
        low = output.lower()
        network_markers = (
            "connectionerror", "max retries", "temporary failure in name resolution",
            "getaddrinfo", "failed to establish a new connection", "read timed out",
            "connection refused", "network is unreachable", "nameresolutionerror",
        )
        if any(m in low for m in network_markers):
            return SmokeOutcome("unverifiable", tail)
        # Top-level packages the run legitimately needs; if one of *these* is
        # missing, the env isn't provisioned here (not a code bug).
        toolset = {
            str(self.brief.get("calculator_import") or "").split(".")[0].lower(),
            str(self.brief.get("library_import") or "").split(".")[0].lower(),
        }
        toolset.discard("")
        # Bundle-local helper modules. The sandbox above copies them, so a miss
        # here means the copy did not happen -- a harness gap, never a reason to
        # ask the model to delete an import it was told to make.
        helpers = {Path(name).stem.lower() for name in self.bundle_files}
        for match in re.finditer(r"No module named ['\"]([\w.]+)['\"]", output):
            name = match.group(1)
            if "." in name:
                continue
            if name.lower() in toolset:
                return SmokeOutcome("unverifiable", tail)  # e.g. matgl absent here
            if name.lower() in helpers or name.lower().startswith("twain_"):
                return SmokeOutcome("unverifiable", tail)
        return SmokeOutcome("repairable", tail)


# --------------------------------------------------------------------------- #
# Module-level helpers (pure; unit-testable in isolation).
# --------------------------------------------------------------------------- #
def _summarize(diagnostics: List[Diagnostic]) -> str:
    """A short, human-readable summary of a batch of diagnostics."""
    if not diagnostics:
        return "no issues"
    kinds = sorted({d.source for d in diagnostics})
    head = diagnostics[0].message.split(".")[0]
    return f"fixed {len(diagnostics)} issue(s) [{', '.join(kinds)}]: {head}"


def _has_errors(diagnostics: List[Diagnostic]) -> bool:
    return any(d.severity == "error" for d in diagnostics)


def _final_status(fixes: List[str], last_smoke: Optional[SmokeOutcome]) -> str:
    """Terminal status for a script that cleared phase 1 (no blocking errors)."""
    if last_smoke is not None and last_smoke.status == "unverifiable" and not fixes:
        return "unverifiable"
    return "repaired" if fixes else "healthy"


def _parse_findings(raw) -> List[Diagnostic]:
    """Parse an LLM review reply (a JSON array) into review Diagnostics.

    Tolerant of markdown fences and surrounding prose; returns ``[]`` on anything
    it can't confidently read as a list of findings.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    text = strip_code_fences(raw).strip()
    data = _loads_array(text)
    if not isinstance(data, list):
        return []
    out: List[Diagnostic] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        severity = "error" if str(item.get("severity")).lower() == "error" else "warning"
        line = item.get("line")
        line = line if isinstance(line, int) else None
        out.append(Diagnostic("review", severity, message, line))
    return out


def _loads_array(text: str):
    """``json.loads`` the first top-level JSON array in ``text``, or ``None``."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            return None
    return None


# Words in the researcher's own request/material that mean the conventional
# cell (or a bigger system) was asked for deliberately -- the primitive-cell
# gate must stand down. Substring-matched, lowercase.
_EXPLICIT_CELL_WORDS = (
    "conventional", "supercell", "super-cell", "super cell",
    "surface", "slab", "interface", "grain",
    "defect", "vacancy", "interstitial", "dopant", "doped", "adsor",
)

# Words meaning the researcher deliberately asked for expensive, tightly
# converged settings -- the DFT cost-budget gate must stand down.
_ACCURACY_WORDS = (
    "high accuracy", "high-accuracy", "accurate", "converged", "convergence",
    "publication", "benchmark", "tight", "precise", "precision",
)


def _static_int_elements(node) -> List[int]:
    """Constant ints of a tuple/list literal (or a ``{"size": (...)}`` dict)."""
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "size":
                node = value
                break
    if isinstance(node, (ast.Tuple, ast.List)):
        vals = [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, int)]
        return vals if len(vals) == len(node.elts) else []
    return []


def _extravagant_dft_settings(source: str) -> List[Tuple[str, int]]:
    """(description, line) pairs for numerical settings beyond the default budget.

    Two statically checkable cost drivers: a plane-wave cutoff above 500 eV
    (``PW(600)``) and a k-point grid denser than 10 per axis
    (``kpts=(12, 12, 12)`` or ``kpts={"size": (12, 12, 12)}``). Either one
    multiplies every SCF by a large factor; together they took a 6-atom CaPt2
    EOS from minutes to ~10 minutes PER SCF ITERATION.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        if name == "PW" and node.args:
            arg = node.args[0]
            if (isinstance(arg, ast.Constant)
                    and isinstance(arg.value, (int, float))
                    and arg.value > 500):
                found.append((f"a PW({arg.value:g}) cutoff", node.lineno))
        for kw in node.keywords:
            if kw.arg == "kpts":
                dims = _static_int_elements(kw.value)
                if dims and max(dims) > 10:
                    found.append(
                        (f"a kpts={tuple(dims)} grid", kw.value.lineno))
    return found


# Cell filters that moved from ase.constraints to ase.filters in ASE 3.23.
# Importing them from the old path raises ImportError on the cluster env --
# and typically from INSIDE a function the smoke run never calls, so only a
# static check catches it before the expensive run.
_MOVED_ASE_FILTERS = frozenset({
    "ExpCellFilter", "FrechetCellFilter", "UnitCellFilter", "StrainFilter",
})


def _stale_ase_filter_imports(source: str) -> List[Tuple[str, int]]:
    """(name, line) pairs importing a moved cell filter from ``ase.constraints``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "ase.constraints":
            for alias in node.names:
                if alias.name in _MOVED_ASE_FILTERS:
                    found.append((alias.name, node.lineno))
    return found


def _fixed_occupations_without_numbers(source: str) -> List[int]:
    """Lines passing GPAW ``occupations={"name": "fixed"}`` with no ``numbers``.

    GPAW's ``"fixed"`` mode means explicit per-band occupation numbers and
    requires a ``numbers`` array; the frozen-occupations band-structure mode
    the scripts actually want is ``"fixed-uniform"``. The wrong name raises
    TypeError only when the calculator initializes -- after the ground-state
    SCF was already paid for.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        vals = {k.value: v for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant)}
        name = vals.get("name")
        if (isinstance(name, ast.Constant) and name.value == "fixed"
                and "numbers" not in keys):
            lines.append(node.lineno)
    return lines


# Keywords that state the spin state as an explicit COUNT, i.e. that duplicate
# what initial magnetic moments encode: a multiplicity (Psi4, CP2K), NWChem's
# ``mult``, or a count of unpaired electrons (xTB's ``uhf``, ``nopen``). The value
# meaning "closed shell" differs, hence the pair.
_SPIN_COUNT_KEYS = {
    "multiplicity": 1, "spin_multiplicity": 1, "mult": 1,   # 2S+1
    "uhf": 0, "nopen": 0,                                   # unpaired electrons
}
# Deliberately NOT included: ``spinpol``, ``nspin``, ``uks``, ``unrestricted``,
# ``reference``. Those switch spin polarization on and are the CORRECT companion
# to magnetic moments rather than a competing statement of the spin count, so
# flagging them would punish the idiomatic GPAW/QE/ABINIT spelling.


# Uncorrelated methods, by the spelling each engine uses. Bare Hartree-Fock
# recovers no electron correlation at all, which is most of a bond's energy.
# Keyword names that select the electronic-structure method, across engines:
# Psi4's `method=`, NWChem's `theory=`, ASE/GPAW's `xc=`, and `functional=`.
_METHOD_KEYS = frozenset({"method", "theory", "xc", "functional"})

_UNCORRELATED_METHODS = frozenset({"scf", "hf", "rhf", "uhf", "rohf",
                                   "hfexch", "hartree-fock"})


def _keyword_bindings(tree):
    """Yield ``(lineno, key, value_node)`` for every keyword bound in ``tree``.

    Covers both spellings engines use for the same setting: a call keyword
    (``Psi4(multiplicity=3)``) and a key in a nested input mapping
    (``NWChem(dft={"mult": 3})``). Several checks need exactly this, and writing
    the ast.Call/ast.Dict pair out per check is how they drift apart.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg:
                    yield node.lineno, kw.arg, kw.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    yield node.lineno, key.value, value


def _uncorrelated_method_for_thermochemistry(source: str,
                                             property_name: str) -> List[int]:
    """Lines selecting bare Hartree-Fock for a property built from bond energies.

    Gated on the PROPERTY, not the method: an SCF orbital energy or a
    Hartree-Fock geometry is a perfectly reasonable thing to ask for. Rationale
    and the measured cost are in the Diagnostic message this feeds.
    """
    if not wants_thermo_cycle(property_name):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = [lineno for lineno, key, value in _keyword_bindings(tree)
             if key in _METHOD_KEYS and isinstance(value, ast.Constant)
             and str(value.value).strip().lower() in _UNCORRELATED_METHODS]
    # `method = "scf"` as a plain assignment, which binds no keyword.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(t.id in _METHOD_KEYS for t in node.targets
                        if isinstance(t, ast.Name))
                and isinstance(node.value, ast.Constant)
                and str(node.value.value).strip().lower() in _UNCORRELATED_METHODS):
            lines.append(node.lineno)
    return sorted(set(lines))


def _engine_launch_ignored(source: str,
                           parallelism: str) -> List[Optional[int]]:
    """Lines building an engine command that ignores the ranks the job allocated.

    For the "engine" placement the payload deliberately runs this script
    single-process -- several ranks of a driver that invokes a separate binary per
    calculation would overwrite each other's files in the one working directory --
    and publishes the allocation as ``TWAIN_ENGINE_LAUNCH`` instead. A script that
    never reads it is correct and single-core: it asks Slurm for N CPUs and uses
    one, which is how a job that should take minutes takes hours.

    Not an error. The science is right, so blocking the run over it would be worse
    than running it slowly; it rides into the proactive-hardening pass, where a
    failed fix keeps the runnable script.
    """
    if parallelism != "engine" or "TWAIN_ENGINE_LAUNCH" in source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    # Point at where the command is set, when the script says so explicitly.
    lines = sorted({lineno for lineno, key, _ in _keyword_bindings(tree)
                    if key in ("command", "commands")})
    if not lines:
        lines = sorted({node.lineno for node in ast.walk(tree)
                        if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id.endswith("Profile")})
    # None, not line 1: this is a property of the whole file, and pointing a reader
    # at an unrelated first import is worse than pointing nowhere.
    return lines[:1] or [None]


def _ambiguous_spin_specification(source: str) -> List[int]:
    """Lines stating an open-shell spin state twice, in two rival channels.

    ASE takes the spin state either from the Atoms object's magnetic moments or
    from an explicit count keyword, and which one a calculator reads is
    engine-specific -- so stating both means one is silently discarded. Flagged
    when an open-shell COUNT is given and the magmom channel is never reconciled.

    Deliberately engine-agnostic: naming engines here would only catch the ones
    already known to bite, and the remedy is one line that is a no-op when the
    moments were already zero. Which engine reads which channel, and the measured
    cost, are in the Diagnostic message this feeds.
    """
    # Touching the magmom channel at all counts as reconciling it: the author has
    # made a deliberate choice between the two mechanisms, which is the ask.
    # Checked before parsing -- no need to build a tree we will discard.
    if "set_initial_magnetic_moments" in source or "initial_magmoms" in source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    def open_shell(key: str, value) -> bool:
        """Whether ``value`` states an open shell for spin keyword ``key``."""
        if not isinstance(value, ast.Constant) or not isinstance(value.value, int):
            # A computed value (e.g. multiplicity=mult) could be either; treat it
            # as open-shell, since the ambiguity is exactly what is being flagged.
            return not isinstance(value, ast.Constant)
        return value.value != _SPIN_COUNT_KEYS[key]

    return sorted({lineno for lineno, key, value in _keyword_bindings(tree)
                   if key in _SPIN_COUNT_KEYS and open_shell(key, value)})


def _signature_probe_calls(source: str) -> List[int]:
    """Lines probing API capabilities with ``inspect.signature(...)``.

    Synthesized scripts use it defensively ("does this calculator accept a
    ``solvent`` kwarg?") -- but ASE-style calculators (xtb-python's ``XTB``
    among them) declare ``__init__(self, atoms=None, **kwargs)`` and route
    every real option through ``default_parameters``, so the probe reports a
    false "unsupported" and the script aborts a run that would have worked.
    Pass the documented keywords directly; a genuinely wrong keyword raises
    its own clear error.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    bare_signature_imported = any(
        isinstance(node, ast.ImportFrom) and node.module == "inspect"
        and any(alias.name == "signature" for alias in node.names)
        for node in ast.walk(tree))
    lines: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "signature"
                and isinstance(func.value, ast.Name)
                and func.value.id == "inspect"):
            lines.append(node.lineno)
        elif (isinstance(func, ast.Name) and func.id == "signature"
                and bare_signature_imported):
            lines.append(node.lineno)
    return lines


def _hardcoded_bandpath_calls(source: str) -> List[int]:
    """Lines calling ``.bandpath(...)`` with a literal special-point string.

    The killer behind Slurm job 2487027: the script relaxed the cell (adding
    numerical noise), then asked for ``bandpath("GXWKGL")``. ASE derives the
    available special points from the lattice it detects in the *actual* cell
    -- after relaxation the FCC cell is no longer recognized as FCC, the
    detected lattice has no 'W', and the whole DFT run dies on KeyError after
    the ground state was already paid for. Calling ``bandpath(npoints=...)``
    with no path string always works: ASE picks the standard path for whatever
    lattice it detected.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "bandpath"):
            continue
        literal = (node.args and isinstance(node.args[0], ast.Constant)
                   and isinstance(node.args[0].value, str))
        literal = literal or any(
            kw.arg == "path" and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
            for kw in node.keywords)
        if literal:
            lines.append(node.lineno)
    return lines


def _scf_grid_bandgap_calls(source: str) -> List[int]:
    """Lines reading the fundamental gap straight off the SCF k-grid.

    The silent-wrong-answer behind the "silicon gap = 0.81 eV" run: calling
    ``ase.dft.bandgap.bandgap(calc)`` right after the ground state searches for
    band extrema only among the SCF grid's k-points -- but extrema generally
    lie BETWEEN grid points (silicon's CBM sits at ~0.85 of Gamma->X, which no
    8x8x8 Monkhorst-Pack grid samples), so the reported gap is the minimum over
    sampled points and comes out too large (0.81 eV vs the true ~0.6 eV PBE
    value). The script runs cleanly, so only this gate catches it.

    Flags every ``bandgap(...)`` call when the script has no non-SCF band-path
    machinery at all -- no ``fixed_density`` (GPAW's fixed-density second pass)
    and no ``bandpath`` call anywhere. Presence of either is taken as the
    two-step method being used; which calc object each call reads is beyond a
    static check.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    has_path_machinery = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("fixed_density", "bandpath")
        for node in ast.walk(tree))
    if has_path_machinery:
        return []
    lines: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        if name == "bandgap":
            lines.append(node.lineno)
    return lines


def _conventional_cell_calls(source: str) -> List[int]:
    """Line numbers of ``crystal(...)`` calls that build the conventional cell.

    A call counts when ``primitive_cell`` is ``False`` or omitted (ASE's
    default is the conventional cell). Only bare ``crystal(...)`` /
    ``*.crystal(...)`` calls are considered -- the ``ase.spacegroup`` builder
    the synthesized scripts use.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None)
        if name != "crystal":
            continue
        kw = next((k for k in node.keywords if k.arg == "primitive_cell"), None)
        if kw is None or (isinstance(kw.value, ast.Constant) and kw.value.value is False):
            lines.append(node.lineno)
    return lines


def _undefined_names(source: str) -> List[Tuple[str, int]]:
    """Names used (Load context) but never defined anywhere in the module.

    A deliberately *conservative*, scope-flattening scan: a name counts as defined
    if it is imported, assigned, a parameter, a function/class name, a
    global/nonlocal, or an exception alias *anywhere* in the module. That
    under-reports (a name defined only in another function is treated as defined),
    which keeps false positives near zero while still catching the case that
    matters -- a truncated script referring to a name it never introduces (e.g.
    ``band_gap = comp``). Bails out entirely on ``from x import *`` (which can
    supply arbitrary names, so certainty is impossible).

    >>> _undefined_names("x = 1\\nprint(x)")
    []
    >>> _undefined_names("print(comp)")
    [('comp', 1)]
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            return []

    defined = set(dir(builtins)) | {
        "__name__", "__file__", "__doc__", "__builtins__",
        "__spec__", "__loader__", "__package__", "__annotations__",
    }
    used: Dict[str, int] = {}

    class _Scan(ast.NodeVisitor):
        def visit_Import(self, node):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])

        def visit_ImportFrom(self, node):
            for a in node.names:
                defined.add(a.asname or a.name)

        def visit_FunctionDef(self, node):
            defined.add(node.name)
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            defined.add(node.name)
            self.generic_visit(node)

        def visit_arg(self, node):
            defined.add(node.arg)

        def visit_Global(self, node):
            defined.update(node.names)

        visit_Nonlocal = visit_Global

        def visit_ExceptHandler(self, node):
            if node.name:
                defined.add(node.name)
            self.generic_visit(node)

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                defined.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                used.setdefault(node.id, node.lineno)

    _Scan().visit(tree)
    return [(name, line) for name, line in used.items() if name not in defined]


# --------------------------------------------------------------------------- #
# a calculation in a plan that has no calculator

# Attaching a calculator, or asking for a quantity only a calculator can produce.
# These are the honest signals that a script is COMPUTING rather than reading:
# a database lookup, a descriptor pass or a structure manipulation needs none of
# them, so they are safe to key on without recognising any engine by name --
# which matters, because the registry's EMT entry imports as plain "ase".
_CALCULATOR_METHODS = frozenset({
    "get_potential_energy", "get_potential_energies", "get_forces", "get_stress",
    "get_stresses", "get_dipole_moment", "get_magnetic_moment",
    "get_magnetic_moments", "get_charges",
})


def _energy_evaluation_sites(source: str) -> List[tuple]:
    """``(what, line)`` for each place the script performs a calculation.

    Deliberately not a search for engine imports: the calculator registry spells
    EMT's import as "ase", so importing ASE at all would look like a calculator
    and every library-only script imports ASE. Attaching ``.calc`` or calling
    ``get_potential_energy()`` cannot be explained any other way.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: List[tuple] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "calc":
                    out.append(("attaches a calculator with `.calc = ...`",
                                getattr(node, "lineno", None)))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _CALCULATOR_METHODS:
                out.append((f"calls `.{node.func.attr}()`",
                            getattr(node, "lineno", None)))
    return sorted(out, key=lambda pair: (pair[1] or 0))


# --------------------------------------------------------------------------- #
# energies converted out of eV before twain_thermo, which converts again

# Factors that move a value AWAY from eV, keyed by the operation that does it.
# Direction is the whole point: multiplying by 27.211 is the CORRECT Hartree -> eV
# step (the run that got the right answer did exactly that), while multiplying by
# 96.485 leaves eV for kJ/mol, which twain_thermo then does a second time.
_AWAY_FROM_EV_MULTIPLIERS = {
    96.48533212: "eV -> kJ/mol",
    2625.4996394798254: "Hartree -> kJ/mol",
    23.060547830619026: "eV -> kcal/mol",
}
_AWAY_FROM_EV_DIVISORS = {
    27.211386245988: "eV -> Hartree",
    13.605693122994: "eV -> Rydberg",
}
# ase.units spells the same conversion symbolically: a value in eV divided by
# ``(kJ / mol)`` is that value in kJ/mol.
_ASE_ENERGY_UNIT_NAMES = frozenset({"kJ", "kcal", "mol"})

# Which arguments of each twain_thermo entry point are energies, and which
# keyword declares their unit. formation_enthalpy's third argument is reference
# DATA in kJ/mol by definition, so it is deliberately absent.
_THERMO_ENERGY_ARGS = {
    "species": (
        ("energy", 1, "unit"),
        ("correction", None, "correction_unit"),
    ),
    "atomization_enthalpy": (
        ("molecule_energy", 1, "unit"),
        ("atom_energies", 2, "unit"),
        ("molecule_correction", None, "correction_unit"),
        ("atom_corrections", None, "correction_unit"),
    ),
    "formation_enthalpy": (
        ("atomization_enthalpy_ev", 1, "unit"),
    ),
}


def _near(value, table, tolerance=1e-3):
    """The label for a float that matches a known conversion factor, else None."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not value:
        return None
    for factor, label in table.items():
        if abs(value - factor) <= tolerance * factor:
            return label
    return None


def _walk_scope(node):
    """Every node in ``node``'s own scope, not descending into nested scopes.

    Scope matters here in a way it does not for ``_undefined_names``: that check
    flattens scopes deliberately because flattening makes it *under*-report, and
    a missed warning is cheap. This check reports an ERROR, so flattening would
    make it over-report -- two functions each with their own ``energy`` would let
    one taint the other.
    """
    nested = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
    stack = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        yield current
        if not isinstance(current, nested):
            stack.extend(ast.iter_child_nodes(current))


def _conversion_factor_names(tree) -> dict:
    """``{name: label}`` for module constants holding an away-from-eV factor.

    Detected by the VALUE, not the name: ``EV_TO_KJ_MOL``, ``KJ_PER_EV`` and
    ``FACTOR`` are all the same problem, and a name-based list would miss the
    third. ``EV_TO_KJ_PER_MOL`` imported from twain_thermo carries no literal, so
    it is recognised by name as the one documented exception.
    """
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            label = _near(node.value.value, _AWAY_FROM_EV_MULTIPLIERS)
            if label:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = label
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "EV_TO_KJ_PER_MOL":
                    found[alias.asname or alias.name] = "eV -> kJ/mol"
    return found


def _converts_away_from_ev(node, factors: dict):
    """The conversion label if this expression leaves eV, else None."""
    if node is None:
        return None
    for inner in [node, *ast.walk(node)]:
        if not isinstance(inner, ast.BinOp):
            continue
        if isinstance(inner.op, ast.Mult):
            for side in (inner.left, inner.right):
                label = (factors.get(side.id) if isinstance(side, ast.Name)
                         else _near(getattr(side, "value", None),
                                    _AWAY_FROM_EV_MULTIPLIERS))
                if label:
                    return label
        elif isinstance(inner.op, ast.Div):
            right = inner.right
            label = _near(getattr(right, "value", None), _AWAY_FROM_EV_DIVISORS)
            if label:
                return label
            # ``e / (kJ / mol)``: the ase.units idiom for eV -> kJ/mol.
            if (isinstance(right, ast.BinOp) and isinstance(right.op, ast.Div)
                    and _unit_name(right.left) in ("kJ", "kcal")
                    and _unit_name(right.right) == "mol"):
                return f"eV -> {_unit_name(right.left)}/mol (ase.units)"
    return None


def _unit_name(node):
    """``kJ`` from ``kJ``, ``units.kJ`` or ``ase.units.kJ``; else None."""
    if isinstance(node, ast.Name) and node.id in _ASE_ENERGY_UNIT_NAMES:
        return node.id
    if isinstance(node, ast.Attribute) and node.attr in _ASE_ENERGY_UNIT_NAMES:
        return node.attr
    return None


def _declares_a_non_ev_unit(call, keyword: str, fallback: str) -> bool:
    """Whether the call states a unit that is not eV for this argument.

    Saying ``unit="kJ/mol"`` makes a pre-converted value CORRECT, so the check
    must stay silent -- the defect is converting without saying so.
    """
    for name in (keyword, fallback):
        if not name:
            continue
        for kw in call.keywords:
            if kw.arg == name and isinstance(kw.value, ast.Constant):
                text = str(kw.value.value).strip().lower()
                squashed = "".join(ch for ch in text if ch.isalnum())
                if squashed and squashed not in ("ev", "electronvolt",
                                                 "electronvolts"):
                    return True
        # A stated unit at this level settles it; do not fall through to `unit=`
        # when `correction_unit=` was given explicitly.
        if any(kw.arg == name for kw in call.keywords):
            return False
    return False


def _preconverted_thermo_energy(source: str) -> List[tuple]:
    """``(what, line)`` for an energy converted out of eV and not declared.

    The failure this exists for: twain_thermo works in eV and converts to kJ/mol
    on the way out, so a script that converts first has the reaction term
    multiplied by 96.485 twice. CO2's standard heat of formation came back as
    -27452 kJ/mol instead of -393.8, and nothing in the pipeline noticed because
    the number is not slightly wrong -- it is wrong by a factor (run 1cd39ffd).

    Silent when the conversion is DECLARED (``unit="kJ/mol"``), which is now the
    supported way to pass a pre-converted value, and when the conversion moves
    TOWARD eV (``e_hartree * Hartree``), which is what a correct script does.

    >>> src = ('K = 96.485332\\n'
    ...        'def go(e, c):\\n'
    ...        '    e_kj = e * K\\n'
    ...        '    return species("CO2", e_kj, correction=c)\\n')
    >>> [what for what, _ in _preconverted_thermo_energy(src)]
    ['`species(energy=...)` is multiplied by a eV -> kJ/mol factor']
    >>> declared = src.replace('correction=c)', 'correction=c, unit="kJ/mol")')
    >>> _preconverted_thermo_energy(declared)
    []
    >>> toward = src.replace('e * K', 'e * 27.211386245988')
    >>> _preconverted_thermo_energy(toward)
    []
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    factors = _conversion_factor_names(tree)
    if not factors and not any(
            _near(getattr(n, "value", None), _AWAY_FROM_EV_MULTIPLIERS)
            or _near(getattr(n, "value", None), _AWAY_FROM_EV_DIVISORS)
            for n in ast.walk(tree) if isinstance(n, ast.Constant)):
        return []  # nothing in the script converts anything: nothing to say

    scopes = [tree]
    scopes.extend(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    out: List[tuple] = []
    module_tainted: dict = {}
    for scope in scopes:
        tainted = dict(module_tainted)
        nodes = list(_walk_scope(scope))
        # Three passes so a short chain (a = e * K; b = a; c = b) settles. Longer
        # chains than that do not appear in generated scripts, and an extra pass
        # cannot introduce a false positive -- only find one more real hop.
        for _ in range(3):
            for node in nodes:
                if not isinstance(node, ast.Assign):
                    continue
                label = (_converts_away_from_ev(node.value, factors)
                         or _tainted_label(node.value, tainted))
                if label:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            tainted.setdefault(target.id, label)
        if scope is tree:
            module_tainted = dict(tainted)
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else None)
            # Grouped per call, not per argument: one species() call with a
            # converted energy AND a converted correction is one mistake, and
            # emitting it twice just crowds the repair prompt.
            hits: dict = {}
            for arg_name, position, unit_kw in _THERMO_ENERGY_ARGS.get(name, ()):
                value = _argument(node, arg_name, position)
                if value is None:
                    continue
                label = (_converts_away_from_ev(value, factors)
                         or _tainted_label(value, tainted))
                if not label:
                    continue
                fallback = "unit" if unit_kw != "unit" else None
                if _declares_a_non_ev_unit(node, unit_kw, fallback):
                    continue
                hits.setdefault(label, []).append(arg_name)
            for label, args in hits.items():
                shown = ", ".join(f"{a}=..." for a in args)
                out.append((f"`{name}({shown})` is multiplied by a {label} "
                            f"factor", getattr(node, "lineno", None)))
    return sorted(set(out), key=lambda pair: (pair[1] or 0, pair[0]))


def _tainted_label(node, tainted: dict):
    """The conversion label if this expression reads an already-converted name."""
    if node is None:
        return None
    for inner in [node, *ast.walk(node)]:
        if isinstance(inner, ast.Name) and inner.id in tainted:
            return tainted[inner.id]
    return None


def _argument(call, name: str, position):
    """A call's argument by keyword, or by position when passed positionally."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    if position is not None and len(call.args) > position:
        return call.args[position]
    return None


# --------------------------------------------------------------------------- #
# calculators that only cover some elements

# Every chemical symbol, so a formula-shaped string that is not actually a
# formula cannot be mistaken for one. Without this, "Foo" parses as the symbol
# "Fo" and would be reported as an element no potential supports.
_ELEMENT_SYMBOLS = frozenset("""
H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn
Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La
Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po
At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg
Cn Nh Fl Mc Lv Ts Og
""".split())

# Calculators whose parameter tables cover a fixed, small set of elements. ASE's
# EMT is the one that matters in practice, because it is the natural "always
# available, no setup" fallback and so is exactly what a script reaches for when
# its real potential is missing. Cross-checked against ase.calculators.emt in the
# test suite so an ASE update that adds an element cannot leave this stale.
_ELEMENT_LIMITED_CALCULATORS = {
    "EMT": frozenset("Ag Al Au C Cu H N Ni O Pd Pt".split()),
}

# Digits, brackets, hydrate dots and charges: the non-symbol characters a real
# formula may contain. Anything else means the string is prose.
_FORMULA_PUNCTUATION = "()[]{}·.,+-*/0123456789"


def formula_elements(formula) -> frozenset:
    """The element symbols in a formula, or empty if it is not a formula.

    The whole string has to be consumable as a formula, not merely to contain
    something symbol-shaped. Scanning for ``[A-Z][a-z]?`` is not enough: it finds
    "Ca" inside "Calcium diplatinide" and would report calcium as an unsupported
    element for a run whose brief happened to carry a name instead of a formula.
    Since these diagnostics are errors that can block a run, an unrecognised
    string must yield nothing at all.
    """
    if not isinstance(formula, str):
        return frozenset()
    text = formula.strip()
    if not text or any(ch.isspace() for ch in text):
        return frozenset()
    symbols, i = set(), 0
    while i < len(text):
        ch = text[i]
        if ch.isupper():
            symbol = ch
            if i + 1 < len(text) and text[i + 1].islower():
                symbol += text[i + 1]
                i += 1
            if symbol not in _ELEMENT_SYMBOLS:
                return frozenset()
            symbols.add(symbol)
        elif ch not in _FORMULA_PUNCTUATION:
            # A lowercase run that is not part of a symbol: prose, not a formula.
            return frozenset()
        i += 1
    return frozenset(symbols)


def _element_limited_calculator(source: str, formula) -> List[tuple]:
    """(calculator, sorted missing elements, line) for a guaranteed failure.

    A script that can reach EMT for a material EMT has no parameters for fails at
    its first energy evaluation, and it fails *misleadingly*: ASE raises
    ``NotImplementedError: No EMT-potential for Ca`` from inside ``initialize()``,
    after ``Calculator.calculate`` has already cached ``self.atoms``. If the script
    swallows that (a bare ``except`` around a relaxation, say) the next evaluation
    on a new Atoms with the same species sees no ``'numbers'`` change, skips
    ``initialize`` and dies on ``AttributeError: 'EMT' object has no attribute
    'nl'`` -- three layers from the cause, and what REPAIR would try to fix.

    Job 2611227 lost its allocation to exactly this: MACE was absent, the script
    fell back to EMT, and the material contained Ca.
    """
    elements = formula_elements(formula)
    if not elements:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: List[tuple] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name not in _ELEMENT_LIMITED_CALCULATORS:
            continue
        missing = sorted(elements - _ELEMENT_LIMITED_CALCULATORS[name])
        if missing:
            out.append((name, missing, getattr(node, "lineno", None)))
    return out


# --------------------------------------------------------------------------- #
# silent calculator substitution

def _silent_calculator_substitution(source: str) -> List[tuple]:
    """(attempted module, fallback module, line) for a swapped-in calculator.

    ``try: from mace.calculators import mace_mp / except ModuleNotFoundError:
    from ase.calculators.emt import EMT; return EMT()`` is not a fallback. A
    universal machine-learned potential and a 1980s effective-medium pair
    potential are different experiments, and the substitution happens silently at
    runtime, so the result is labelled with whichever tool the script's provenance
    string names -- in job 2611227, "MACE-MP interatomic potential", for a run
    that never loaded MACE. Had the material been one EMT does cover, that run
    would have SUCCEEDED and reported a foundation-model number produced by a pair
    potential, which is worse than the crash.

    A missing library has to fail loudly so REPLAN can pick a method the cluster
    actually has. Matched narrowly: the handler must both import a different
    top-level package and produce an object, so aliasing fallbacks
    (``except ImportError: import tomli as tomllib``) are not flagged.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    def roots(body) -> set:
        found = set()
        for node in body:
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.module:
                    found.add(sub.module.split(".")[0])
                elif isinstance(sub, ast.Import):
                    found.update(a.name.split(".")[0] for a in sub.names)
        return found

    def builds_an_object(body) -> bool:
        for node in body:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Call):
                    return True
                if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call):
                    return True
        return False

    out: List[tuple] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        attempted = roots(node.body)
        if not attempted:
            continue
        for handler in node.handlers:
            names = {n.id for n in ast.walk(handler) if isinstance(n, ast.Name)}
            if not ({"ImportError", "ModuleNotFoundError"} & names):
                continue
            fallback = roots(handler.body) - attempted
            if fallback and builds_an_object(handler.body):
                out.append((sorted(attempted)[0], sorted(fallback)[0],
                            getattr(handler, "lineno", None)))
    return out
