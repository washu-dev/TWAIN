"""Unit tests for the REPAIR stage's engine, :class:`ScriptDoctor`.

These cover the reliability mechanism that turns a *plausible* generated script
into one that actually runs -- catching truncation, hallucinated model
identifiers, and misused APIs *generically* (no property is hardcoded), then
proactively scanning a runnable script for latent bugs before the expensive run.

A synthesized main.py is checked statically, run with --smoke in the heavy-
calculator env, and (once runnable) reviewed by the model; a real failure is fed
back to self-correct, while an env/network problem that can't be verified here is
accepted rather than falsely "repaired". The agent, verifier, and interpreter are
injected so the tests run fully offline and deterministically.

Run from the repo root with:  pixi run pytest tests/unit/test_codegen_verify.py
"""
import doctest

import pytest

from code_gen import codegen_engine as eng
from code_gen import script_doctor as sd
from code_gen.codegen_engine import CodegenEngine, pixi_env_python
from code_gen.script_doctor import (
    Diagnostic, ScriptDoctor, SmokeOutcome, _parse_findings, _undefined_names,
)


# A runnable script the fake agent returns when the test only cares about wiring.
OK_SCRIPT = ("import ase\ndef main():\n    print('ok')\n"
             "if __name__ == '__main__':\n    main()\n")


def _calc_plan(*, calculator="NWChem", calculator_import="ase.calculators.nwchem",
               libraries=("ASE",), metric="total_energy", target=-1.0,
               tolerance=0.1, requested_property=None, system=None):
    """A schema-shaped ExecutionPlan for a calculator-driven run.

    One factory rather than a near-identical dict per test class: the fields that
    actually vary between these tests are the toolset and the metric.
    """
    return {
        "selected_method": {"tool_name": "ASE", "libraries": list(libraries),
                            "calculator": calculator,
                            "calculator_import": calculator_import,
                            "calculator_library": "ASE"},
        "requested_property": requested_property,
        "metadata": {"timestamp": "t", "goal_id": "g", "candidate_rank": 1},
        "acceptance_metrics": [{"metric_name": metric, "target_value": target,
                                "tolerance": tolerance}],
        "compute_estimate": {"cpu_hours": 1.0}, "cost_estimate": {"min_cost": 0.1},
        "slurm_request": {"cpu_count": 2, "gpu_count": 0, "max_time": 1.0, "ram": 8},
        "safety_notes": [],
        "target_system": system or {"molecule": {"name": "carbon dioxide",
                                                 "SMILES": "O=C=O"}},
    }


def _helper_source(name: str) -> str:
    """A bundle helper's source as shipped -- bundles must match it byte for byte."""
    from pathlib import Path
    return (Path(eng.__file__).resolve().parent / "bundle_helpers"
            / f"{name}.py").read_text(encoding="utf-8")


# A minimal calculator brief (what the REPAIR stage hands the doctor).
def _brief():
    return {
        "library": "Pymatgen",
        "library_import": "pymatgen",
        "calculator": "MatGL",
        "calculator_import": "matgl",
        "property": "band_gap",
        "material_desc": "silicon (Si)",
        "acceptance": [{"metric_name": "band_gap", "target_value": 1.1, "tolerance": 0.2}],
        "output_file": "results.csv",
    }


# Runnable scripts that reference the calculator import + have an entrypoint
# (so they clear the static bar). GUESS "loads" a guessed model; FIXED a real one.
GUESS = ("import matgl\ndef main():\n    print('load MEGNet-guessed-name')\n"
         "if __name__ == '__main__':\n    main()\n")
FIXED = ("import matgl\ndef main():\n    print('discovered a real model at runtime')\n"
         "if __name__ == '__main__':\n    main()\n")
# A truncated reply: compiles, references matgl, but defines main() and never
# calls it AND uses an undefined name -- the exact shape of the reported bug.
TRUNCATED = "import matgl\ndef main():\n    band_gap = comp\n"


def _seq(outcomes):
    """A verifier that returns the given SmokeOutcomes in order across rounds."""
    it = iter(outcomes)
    return lambda source, brief: next(it)


class TestModuleDoctests:
    def test_engine_doctests_pass(self):
        assert doctest.testmod(eng, verbose=False).failed == 0

    def test_doctor_doctests_pass(self):
        assert doctest.testmod(sd, verbose=False).failed == 0


class TestStaticDiagnostics:
    def test_clean_runnable_script_has_no_static_errors(self):
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(GUESS)
        assert [d for d in diags if d.severity == "error"] == []

    def test_syntax_error_is_reported_and_short_circuits(self):
        diags = ScriptDoctor(brief=_brief()).static_diagnostics("def main(:\n")
        assert len(diags) == 1 and diags[0].source == "compile"

    def test_missing_entrypoint_is_an_error(self):
        diags = ScriptDoctor(brief=_brief()).static_diagnostics("import matgl\nx = matgl\n")
        assert any(d.source == "entrypoint" and d.severity == "error" for d in diags)

    def test_missing_calculator_reference_is_an_error(self):
        script = "def main():\n    pass\nif __name__ == '__main__':\n    main()\n"
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "calculator" for d in diags)

    def test_undefined_name_is_an_error(self):
        # The truncation fingerprint: `band_gap = comp` where comp is undefined.
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(TRUNCATED)
        assert any(d.source == "undefined-name" and "comp" in d.message for d in diags)

    def test_undefined_names_helper_flags_only_truly_undefined(self):
        assert _undefined_names("x = 1\nprint(x)") == []
        assert _undefined_names("import os\nprint(os.getcwd())") == []
        assert [n for n, _ in _undefined_names("print(nope)")] == ["nope"]


# A runnable script that builds the conventional cell (`primitive_cell` off /
# missing) -- the fingerprint of the 24-atom CaPt2 EOS that wasted hours on the
# cluster when the 6-atom primitive cell answers the same question.
_CONVENTIONAL = """\
import matgl
from ase.spacegroup import crystal

def build():
    return crystal(("Ca", "Pt"), basis=[(0.125,)*3, (0.5,)*3],
                   spacegroup=227, cellpar=[7.6]*3 + [90]*3,
                   primitive_cell={pc})

if __name__ == "__main__":
    build()
"""


class TestStaleAseFilterImportGate:
    def test_expcellfilter_from_constraints_is_an_error(self):
        # The fingerprint of Slurm job 2337355: a lazy `from ase.constraints
        # import ExpCellFilter` inside a function the smoke run never calls,
        # exploding with ImportError only in the real run (ASE >= 3.23 moved
        # cell filters to ase.filters).
        script = (
            "import matgl\n"
            "def relax():\n"
            "    from ase.constraints import ExpCellFilter\n"
            "if __name__ == '__main__':\n"
            "    relax()\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "ase-filters" and "ase.filters" in d.message
                   for d in diags)

    def test_import_from_ase_filters_is_clean(self):
        script = (
            "import matgl\n"
            "def relax():\n"
            "    from ase.filters import ExpCellFilter\n"
            "if __name__ == '__main__':\n"
            "    relax()\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not any(d.source == "ase-filters" for d in diags)

    def test_other_constraints_imports_are_clean(self):
        # FixAtoms & friends still legitimately live in ase.constraints.
        script = (
            "import matgl\n"
            "from ase.constraints import FixAtoms\n"
            "if __name__ == '__main__':\n"
            "    FixAtoms(indices=[0])\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not any(d.source == "ase-filters" for d in diags)


class TestGpawFixedOccupationsGate:
    def test_fixed_without_numbers_is_an_error(self):
        # Fingerprint of Slurm job 2337668: occupations={"name": "fixed"} with
        # no numbers array raises TypeError at calculator init, after the
        # ground-state SCF was already paid for.
        script = (
            "import matgl\n"
            "def bands():\n"
            "    return {'occupations': {'name': 'fixed'}, 'symmetry': 'off'}\n"
            "if __name__ == '__main__':\n"
            "    bands()\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "gpaw-occupations" and "fixed-uniform" in d.message
                   for d in diags)

    def test_fixed_uniform_is_clean(self):
        script = (
            "import matgl\n"
            "def bands():\n"
            "    return {'occupations': {'name': 'fixed-uniform'}}\n"
            "if __name__ == '__main__':\n"
            "    bands()\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not any(d.source == "gpaw-occupations" for d in diags)

    def test_fixed_with_numbers_is_clean(self):
        script = (
            "import matgl\n"
            "def bands():\n"
            "    return {'occupations': {'name': 'fixed', 'numbers': [2, 2, 0]}}\n"
            "if __name__ == '__main__':\n"
            "    bands()\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not any(d.source == "gpaw-occupations" for d in diags)


class TestSignatureProbeGate:
    def test_inspect_signature_probe_is_an_error(self):
        # Fingerprint of Slurm job 2459489's bundle: the script probed
        # inspect.signature(XTB.__init__) for a 'solvent' parameter, but the
        # calculator takes **kwargs (options live in default_parameters), so
        # the probe falsely reported "unsupported" and aborted a runnable job.
        script = (
            "import matgl\n"
            "import inspect\n"
            "def make_calc(solvent):\n"
            "    from xtb.ase.calculator import XTB\n"
            "    accepted = set(inspect.signature(XTB.__init__).parameters)\n"
            "    if 'solvent' not in accepted:\n"
            "        raise RuntimeError('no solvent kw')\n"
            "    return XTB(solvent=solvent)\n"
            "if __name__ == '__main__':\n"
            "    make_calc('water')\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "signature-probe" and d.severity == "error"
                   for d in diags)

    def test_bare_signature_import_is_an_error(self):
        script = (
            "import matgl\n"
            "from inspect import signature\n"
            "def make_calc():\n"
            "    from xtb.ase.calculator import XTB\n"
            "    return signature(XTB.__init__)\n"
            "if __name__ == '__main__':\n"
            "    make_calc()\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "signature-probe" for d in diags)

    def test_unrelated_signature_name_is_clean(self):
        # A local function named `signature` is not an inspect probe.
        script = (
            "import matgl\n"
            "def signature(x):\n"
            "    return x\n"
            "if __name__ == '__main__':\n"
            "    signature(1)\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not any(d.source == "signature-probe" for d in diags)


class TestRuntimeRepair:
    """repair_runtime: the general net EXECUTE uses against real-run crashes."""

    TRACEBACK = (
        "Traceback (most recent call last):\n"
        '  File "main.py", line 91, in compute_band_gaps\n'
        "    path = atoms.cell.bandpath('GXWKGL', npoints=200)\n"
        "KeyError: 'W'"
    )

    def test_traceback_drives_a_verified_fix(self):
        doctor = ScriptDoctor(agent=lambda p: FIXED, brief=_brief(),
                              verifier=_seq([SmokeOutcome("pass")]))
        assert doctor.repair_runtime(GUESS, self.TRACEBACK) == FIXED

    def test_no_agent_means_no_repair(self):
        doctor = ScriptDoctor(agent=None, brief=_brief())
        assert doctor.repair_runtime(GUESS, self.TRACEBACK) is None

    def test_unchanged_output_is_rejected(self):
        doctor = ScriptDoctor(agent=lambda p: GUESS, brief=_brief(),
                              verifier=_seq([SmokeOutcome("pass")]))
        assert doctor.repair_runtime(GUESS, self.TRACEBACK) is None

    def test_fix_that_fails_static_checks_is_rejected(self):
        # A "fix" that no longer compiles/references the calculator must never
        # replace the bundle -- the repair verifies before accepting.
        doctor = ScriptDoctor(agent=lambda p: "def broken(:\n", brief=_brief())
        assert doctor.repair_runtime(GUESS, self.TRACEBACK) is None

    def test_fix_that_breaks_smoke_is_rejected(self):
        # Statically fine but smoke-broken, and every further round returns the
        # same script -> keep the original bundle (return None).
        doctor = ScriptDoctor(
            agent=lambda p: FIXED, brief=_brief(),
            verifier=_seq([SmokeOutcome("repairable", error="boom")] * 4))
        assert doctor.repair_runtime(GUESS, self.TRACEBACK) is None


class TestBandpathLiteralGate:
    def test_hardcoded_path_string_is_an_error(self):
        # Fingerprint of Slurm job 2487027: after an ExpCellFilter relaxation
        # the (noisy) cell is no longer detected as FCC, so the hardcoded 'W'
        # in bandpath("GXWKGL") raised KeyError after the ground-state SCF.
        script = (
            "import matgl\n"
            "def bands(atoms):\n"
            "    return atoms.cell.bandpath('GXWKGL', npoints=200)\n"
            "if __name__ == '__main__':\n"
            "    bands(None)\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "bandpath-literal" and d.severity == "error"
                   for d in diags)

    def test_path_keyword_literal_is_an_error(self):
        script = (
            "import matgl\n"
            "def bands(atoms):\n"
            "    return atoms.cell.bandpath(path='GXL', npoints=100)\n"
            "if __name__ == '__main__':\n"
            "    bands(None)\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "bandpath-literal" for d in diags)

    def test_pathless_bandpath_is_clean(self):
        # The recommended form: ASE picks the standard path for whatever
        # lattice it actually detects in the cell.
        script = (
            "import matgl\n"
            "def bands(atoms):\n"
            "    return atoms.cell.bandpath(npoints=200, pbc=atoms.pbc)\n"
            "if __name__ == '__main__':\n"
            "    bands(None)\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not any(d.source == "bandpath-literal" for d in diags)

    def test_path_built_from_special_points_is_clean(self):
        # A path assembled from the detected lattice's own letters is fine --
        # only literals are flagged.
        script = (
            "import matgl\n"
            "def bands(atoms):\n"
            "    letters = ''.join(atoms.cell.bandpath().special_points)\n"
            "    return atoms.cell.bandpath(letters, npoints=200)\n"
            "if __name__ == '__main__':\n"
            "    bands(None)\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not any(d.source == "bandpath-literal" for d in diags)


class TestScfGridBandgapGate:
    """bandgap() must read a fixed-density band-path pass, not the SCF grid.

    Fingerprint of the "silicon gap = 0.81 eV" run: `bandgap(calc)` right
    after the ground state searches extrema only among the SCF grid's
    k-points, but silicon's CBM sits at ~0.85 of Gamma->X -- between grid
    points -- so the gap comes out ~0.2 eV too large while the script runs
    cleanly.
    """

    def test_bandgap_on_scf_calc_alone_is_an_error(self):
        script = (
            "from ase.dft.bandgap import bandgap\n"
            "def gap(calc):\n"
            "    g, p1, p2 = bandgap(calc, direct=False)\n"
            "    return g\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "bandgap-on-scf-grid" and d.severity == "error"
                   for d in diags)

    def test_fixed_density_band_path_two_step_is_clean(self):
        # The recommended method: non-SCF pass along the standard path, then
        # extrema from that calculation.
        script = (
            "from ase.dft.bandgap import bandgap\n"
            "def gap(atoms, calc):\n"
            "    path = atoms.cell.bandpath(npoints=200, pbc=atoms.pbc)\n"
            "    bs_calc = calc.fixed_density(kpts=path, symmetry='off')\n"
            "    g, p1, p2 = bandgap(bs_calc, direct=False)\n"
            "    return g\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not any(d.source == "bandgap-on-scf-grid" for d in diags)

    def test_script_without_bandgap_calls_is_unaffected(self):
        script = "def total_energy(atoms):\n    return atoms.get_potential_energy()\n"
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not any(d.source == "bandgap-on-scf-grid" for d in diags)


class TestDftBudgetGate:
    def test_pw_cutoff_above_500_is_an_error(self):
        # Fingerprint of Slurm job 2472788: PW(600) + 12x12x12 k-points on a
        # 6-atom cell -> ~10 min per SCF iteration; the first of 18 EOS
        # points ate the entire 4-hour wall clock.
        script = (
            "import matgl\n"
            "from gpaw import GPAW, PW\n"
            "def calc():\n"
            "    return GPAW(mode=PW(600), kpts=(8, 8, 8))\n"
            "if __name__ == '__main__':\n"
            "    calc()\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "dft-budget" and "PW(600)" in d.message
                   for d in diags)

    def test_dense_kpts_grid_is_an_error(self):
        script = (
            "import matgl\n"
            "from gpaw import GPAW, PW\n"
            "def calc():\n"
            "    return GPAW(mode=PW(400), kpts=(12, 12, 12))\n"
            "if __name__ == '__main__':\n"
            "    calc()\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "dft-budget" and "(12, 12, 12)" in d.message
                   for d in diags)

    def test_budget_conformant_settings_are_clean(self):
        script = (
            "import matgl\n"
            "from gpaw import GPAW, PW\n"
            "def calc():\n"
            "    return GPAW(mode=PW(400), kpts={'size': (8, 8, 8)})\n"
            "if __name__ == '__main__':\n"
            "    calc()\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not any(d.source == "dft-budget" for d in diags)

    def test_explicit_accuracy_request_stands_down(self):
        script = (
            "import matgl\n"
            "from gpaw import GPAW, PW\n"
            "def calc():\n"
            "    return GPAW(mode=PW(700), kpts=(16, 16, 16))\n"
            "if __name__ == '__main__':\n"
            "    calc()\n"
        )
        brief = dict(_brief())
        brief["objective"] = "high accuracy converged bulk modulus of CaPt2"
        diags = ScriptDoctor(brief=brief).static_diagnostics(script)
        assert not any(d.source == "dft-budget" for d in diags)


class TestPrimitiveCellGate:
    def test_primitive_cell_false_is_an_error(self):
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(
            _CONVENTIONAL.format(pc="False"))
        assert any(d.source == "primitive-cell" and d.severity == "error"
                   for d in diags)

    def test_omitted_primitive_cell_is_an_error(self):
        # ASE defaults to the conventional cell, so omission is a violation too.
        script = (
            "import matgl\n"
            "from ase.spacegroup import crystal\n"
            "def build():\n"
            "    return crystal(('Ca', 'Pt'), spacegroup=227, cellpar=[7.6]*3+[90]*3)\n"
            "if __name__ == '__main__':\n"
            "    build()\n"
        )
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert any(d.source == "primitive-cell" for d in diags)

    def test_primitive_cell_true_is_clean(self):
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(
            _CONVENTIONAL.format(pc="True"))
        assert not any(d.source == "primitive-cell" for d in diags)

    def test_explicit_researcher_request_disables_the_gate(self):
        # Saying "conventional cell" (or supercell/surface/defect...) in the
        # request wins: the gate must stand down.
        brief = {**_brief(),
                 "objective": "bulk modulus of CaPt2 using the conventional cell"}
        diags = ScriptDoctor(brief=brief).static_diagnostics(
            _CONVENTIONAL.format(pc="False"))
        assert not any(d.source == "primitive-cell" for d in diags)

    def test_surface_property_disables_the_gate(self):
        brief = {**_brief(), "property": "surface_energy"}
        diags = ScriptDoctor(brief=brief).static_diagnostics(
            _CONVENTIONAL.format(pc="False"))
        assert not any(d.source == "primitive-cell" for d in diags)

    def test_script_without_crystal_call_is_clean(self):
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(GUESS)
        assert not any(d.source == "primitive-cell" for d in diags)


class TestClassifySmoke:
    def setup_method(self):
        self.doctor = ScriptDoctor(brief=_brief())

    def test_bad_model_id_is_repairable(self):
        out = self.doctor._classify_smoke("huggingface_hub.errors.RepositoryNotFoundError: 401 ...")
        assert out.status == "repairable"

    def test_wrong_argument_type_is_repairable(self):
        out = self.doctor._classify_smoke(
            "TypeError: embedding(): argument 'indices' must be Tensor, not list")
        assert out.status == "repairable"

    def test_missing_toplevel_calculator_is_unverifiable(self):
        # matgl itself not installed in the verify env -> can't verify, not a bug.
        out = self.doctor._classify_smoke("ModuleNotFoundError: No module named 'matgl'")
        assert out.status == "unverifiable"

    def test_missing_submodule_of_installed_pkg_is_repairable(self):
        # The package is present but the code imported a submodule that isn't ->
        # a code bug worth repairing, not an env gap.
        out = self.doctor._classify_smoke("No module named 'matgl.does_not_exist'")
        assert out.status == "repairable"

    def test_network_failure_is_unverifiable(self):
        out = self.doctor._classify_smoke("requests.exceptions.ConnectionError: Max retries exceeded")
        assert out.status == "unverifiable"


class TestHealCorrectness:
    def test_healthy_script_is_returned_unchanged(self):
        doctor = ScriptDoctor(agent=lambda p: GUESS, brief=_brief(),
                              verifier=_seq([SmokeOutcome("pass")]), review=False)
        report = doctor.heal(GUESS)
        assert report.status == "healthy" and not report.changed
        assert report.source == GUESS

    def test_repairable_failure_feeds_diagnostics_back_and_uses_the_fix(self):
        # First smoke reports a repairable 401; the repaired script verifies clean.
        seen = []

        def agent(prompt):
            seen.append(prompt)
            return FIXED

        doctor = ScriptDoctor(
            agent=agent, brief=_brief(), review=False,
            verifier=_seq([SmokeOutcome("repairable", "RepositoryNotFoundError: 401"),
                           SmokeOutcome("pass")]))
        report = doctor.heal(GUESS)
        assert report.status == "repaired" and report.source == FIXED
        # The repair prompt must carry the real error + the failing source.
        assert any("RepositoryNotFoundError: 401" in p for p in seen)
        assert any("current main.py" in p for p in seen)

    def test_no_agent_reports_but_cannot_fix(self):
        # A broken script with no agent: preserved, reported, never raises.
        doctor = ScriptDoctor(agent=None, brief=_brief(), sim_python=None)
        report = doctor.heal(TRUNCATED)
        assert report.status == "unrepairable"
        assert report.source == TRUNCATED           # untouched
        assert any(d.source == "undefined-name" for d in report.remaining)

    def test_unverifiable_is_accepted_not_repaired(self):
        # The env can't verify here (calculator absent / no network): accept the
        # compiling script rather than falsely repairing working code.
        calls = []
        agent = lambda p: calls.append(p) or GUESS
        doctor = ScriptDoctor(agent=agent, brief=_brief(), review=False,
                              verifier=_seq([SmokeOutcome("unverifiable", "no matgl here")]))
        report = doctor.heal(GUESS)
        assert report.status == "unverifiable" and report.source == GUESS
        assert calls == []                            # never asked to repair

    def test_exhausted_rounds_returns_last_tailored_script_not_none(self):
        # Every attempt keeps failing smoke, each returning a distinct script: we
        # still hand back the last tailored script (better than the generic stub).
        scripts = iter([GUESS, FIXED, GUESS.replace("guessed", "third")])
        doctor = ScriptDoctor(
            agent=lambda p: next(scripts), brief=_brief(), max_rounds=3, review=False,
            verifier=lambda s, b: SmokeOutcome("repairable", "still broken"))
        report = doctor.heal(FIXED)
        assert report.status == "unrepairable"
        assert report.source and "matgl" in report.source   # a tailored script, not None

    def test_truncated_script_is_healed_end_to_end(self):
        # Regression for the reported bug: a truncated (entrypoint-less, undefined-
        # name) script is caught statically and repaired before it ever runs.
        doctor = ScriptDoctor(agent=lambda p: FIXED, brief=_brief(),
                              verifier=_seq([SmokeOutcome("pass")]), review=False)
        report = doctor.heal(TRUNCATED)
        assert report.status == "repaired" and report.source == FIXED


class TestProactiveReview:
    def test_review_parses_json_findings(self):
        raw = '[{"severity":"error","line":3,"message":"guessed model id may not exist"}]'
        doctor = ScriptDoctor(agent=lambda p: raw, brief=_brief())
        findings = doctor.review(GUESS)
        assert len(findings) == 1 and findings[0].severity == "error" and findings[0].line == 3

    def test_review_without_agent_is_empty(self):
        assert ScriptDoctor(agent=None, brief=_brief()).review(GUESS) == []

    def test_review_tolerates_garbage(self):
        assert _parse_findings("not json at all") == []
        assert _parse_findings("here you go:\n```json\n[]\n```") == []

    def test_runnable_script_is_hardened_by_review(self):
        # The script passes smoke, but the proactive review finds a latent bug and
        # the fix is applied and re-accepted.
        review_json = '[{"severity":"error","line":2,"message":"model id may not exist"}]'

        def agent(prompt):
            return review_json if "Return ONLY a JSON array" in prompt else FIXED

        doctor = ScriptDoctor(agent=agent, brief=_brief(),
                              verifier=lambda s, b: SmokeOutcome("pass"), review=True)
        report = doctor.heal(GUESS)
        assert report.status == "repaired" and report.source == FIXED
        assert any("proactive review" in f for f in report.fixes)

    def test_review_fix_that_regresses_is_rejected(self):
        # If the review "fix" fails the hard static bar, keep the runnable script.
        review_json = '[{"severity":"error","line":1,"message":"x"}]'
        BROKEN = "import matgl\ndef main():\n    oops = undefined_thing\n"  # no entrypoint

        def agent(prompt):
            return review_json if "Return ONLY a JSON array" in prompt else BROKEN

        doctor = ScriptDoctor(agent=agent, brief=_brief(),
                              verifier=lambda s, b: SmokeOutcome("pass"), review=True)
        report = doctor.heal(GUESS)
        assert report.source == GUESS                 # regression rejected

    # A review "fix" that passes static analysis but breaks at RUNTIME (a
    # syntactically-valid but nonexistent API, e.g. matgl.get_available_models())
    # must be re-smoked -- not shipped to EXECUTE on the strength of static checks.
    _BAD_API = ("import matgl\ndef main():\n    x = matgl.get_available_models()\n    print(x)\n"
                "if __name__ == '__main__':\n    main()\n")
    _GOOD_API = ("import matgl\ndef main():\n    x = matgl.get_available_pretrained_models()\n"
                 "    print(x)\nif __name__ == '__main__':\n    main()\n")

    @staticmethod
    def _runtime_verifier(source, brief):
        # The bad API is valid syntax but fails at runtime; the fixed one is clean.
        if "get_available_models(" in source and "pretrained" not in source:
            return SmokeOutcome("repairable",
                                "AttributeError: module 'matgl' has no attribute 'get_available_models'")
        return SmokeOutcome("pass")

    def test_review_runtime_regression_is_resmoked_and_repaired(self, tmp_path=None):
        review_json = '[{"severity":"error","line":2,"message":"harden model discovery"}]'

        def agent(prompt):
            if "Return ONLY a JSON array" in prompt:
                return review_json                       # the review's findings
            if "get_available_models" in prompt:
                return self._GOOD_API                    # error-driven smoke repair
            return self._BAD_API                         # the review's own (broken) rewrite

        doctor = ScriptDoctor(agent=agent, brief=_brief(),
                              verifier=self._runtime_verifier, review=True)
        report = doctor.heal(GUESS)
        assert "get_available_pretrained_models" in report.source   # runtime break repaired
        assert "get_available_models(" not in report.source or "pretrained" in report.source

    def test_unfixable_review_runtime_regression_is_rejected(self):
        review_json = '[{"severity":"error","line":2,"message":"harden model discovery"}]'

        def agent(prompt):
            if "Return ONLY a JSON array" in prompt:
                return review_json
            return self._BAD_API                          # never fixes it

        doctor = ScriptDoctor(agent=agent, brief=_brief(),
                              verifier=self._runtime_verifier, review=True)
        report = doctor.heal(GUESS)
        assert report.source == GUESS                     # broken review discarded; runnable kept


class TestSimEnvResolution:
    def test_pixi_env_python_none_for_unknown_env(self):
        assert pixi_env_python("does-not-exist-env-xyz") is None

    def test_injected_sim_python_none_forces_unverifiable(self):
        # sim_python=None (explicit) forces "no interpreter" -> unverifiable.
        doctor = ScriptDoctor(brief=_brief(), sim_python=None)
        assert doctor._resolve_sim_python() is None
        assert doctor.smoke("import matgl\n").status == "unverifiable"


class TestConfigRecordsEnvironment:
    def test_calculator_bundle_config_names_the_sim_env(self):
        plan = {
            "selected_method": {
                "tool_name": "Pymatgen", "libraries": ["Pymatgen"],
                "calculator": "MatGL", "calculator_import": "matgl",
                "calculator_library": "Pymatgen",
            },
            "slurm_request": {"cpu_count": 4, "gpu_count": 0, "max_time": 0.5, "ram": 8},
            "metadata": {"timestamp": "t"},
            "acceptance_metrics": [{"metric_name": "band_gap", "target_value": 1.1, "tolerance": 0.2}],
            "safety_notes": [],
            "target_system": {"formula": "Si"},
            "requested_property": "band_gap",
        }
        # No agent -> generic fallback, but the config still records the run env.
        bundle = CodegenEngine().generate(plan)
        assert "environment: sim" in bundle.config_yaml


# ── the smoke gate must prove the ENGINE runs, not just that ASE imports ──────

class TestEngineBinaryIsGated:
    """Slurm job 2573801 died mid-optimization with "nwchem: command not found"
    (exit 127) after passing the smoke gate and a queue wait.

    An external engine's ASE bindings are pure Python: `import
    ase.calculators.nwchem` succeeds wherever ASE is installed, binary or not. So
    the import check accepted twain-envs/default -- ASE and pymatgen present, no
    nwchem executable -- and the run failed at execution instead of at the gate.
    """

    def _smoke(self, **kw):
        from code_gen.smoke_test_generator import generate_inline_tests
        return generate_inline_tests(
            tool_name="ASE+NWChem",
            required_import_names=["ase", "ase.calculators.nwchem"], **kw)

    def test_the_executable_is_required_when_given(self):
        src = self._smoke(required_executables=["nwchem"])
        assert 'REQUIRED_EXECUTABLES = ["nwchem"]' in src
        assert "shutil.which" in src

    def test_no_binary_check_when_the_calculator_is_a_python_package(self):
        """GPAW is importable Python -- the import check already suffices."""
        src = self._smoke()
        assert "REQUIRED_EXECUTABLES = []" in src

    def test_the_rendered_smoke_compiles(self):
        compile(self._smoke(required_executables=["nwchem"]), "inline_tests.py", "exec")

    def test_a_missing_binary_fails_the_gate(self, tmp_path):
        """The behaviour that matters: run the generated smoke in an environment
        without the engine and confirm it refuses instead of passing."""
        import subprocess
        import sys
        src = self._smoke(required_executables=["definitely-not-a-real-engine"])
        (tmp_path / "inline_tests.py").write_text(src)
        (tmp_path / "main.py").write_text("print('hi')\n")
        proc = subprocess.run([sys.executable, "inline_tests.py"], cwd=tmp_path,
                              capture_output=True, text=True)
        assert proc.returncode == 2, proc.stdout
        assert "MISSING DEPENDENCY" in proc.stdout
        assert "executable not on PATH" in proc.stdout

    def test_a_present_binary_passes_the_gate(self, tmp_path):
        import subprocess
        import sys
        src = self._smoke(required_executables=["sh"])   # certainly on PATH
        (tmp_path / "inline_tests.py").write_text(src)
        (tmp_path / "main.py").write_text("print('hi')\n")
        proc = subprocess.run([sys.executable, "inline_tests.py"], cwd=tmp_path,
                              capture_output=True, text=True)
        assert "executables OK: sh" in proc.stdout, proc.stdout

    def test_build_passes_the_registrys_executable_through(self, tmp_path):
        """End to end: an NWChem plan's bundle demands the nwchem binary."""
        import json
        from pathlib import Path
        from unittest.mock import patch

        import statemachine as SM
        from crash_recovery import DataStorage

        plan = {
            "selected_method": {"tool_name": "ASE", "libraries": ["ASE"],
                                "calculator": "NWChem",
                                "calculator_import": "ase.calculators.nwchem",
                                "calculator_library": "ASE"},
            "requested_property": "total_energy",
            "metadata": {"timestamp": "t", "goal_id": "g", "candidate_rank": 1},
            "acceptance_metrics": [{"metric_name": "total_energy",
                                    "target_value": -1.0, "tolerance": 0.1}],
            "compute_estimate": {"cpu_hours": 1.0}, "cost_estimate": {"min_cost": 0.1},
            "slurm_request": {"cpu_count": 2, "gpu_count": 0, "max_time": 1.0, "ram": 8},
            "safety_notes": [],
            "target_system": {"molecule": {"name": "carbon dioxide", "SMILES": "O=C=O"}},
        }
        path = tmp_path / "execution_plan.json"
        path.write_text(json.dumps(plan))
        script = ("import ase\nfrom ase.calculators.nwchem import NWChem\n"
                  "def main():\n    print('ok')\nif __name__ == '__main__':\n    main()\n")
        with patch.object(DataStorage, "load", return_value=None):
            m = SM.StateMachine(data_path=str(tmp_path / "s.json"), run_id="exe",
                                agent=lambda p: script)
        m.artifacts_dir = tmp_path
        m.context.artifacts["execution_plan"] = str(path)
        m.build()

        tests = (Path(m.context.artifacts["run_bundle"]) / "inline_tests.py").read_text()
        assert 'REQUIRED_EXECUTABLES = ["nwchem"]' in tests



# ── plane-wave engines get the resolver, and the prompt is told to use it ─────

class TestPseudopotentialWiring:
    """QE and ABINIT ship no pseudopotentials, so the bundle carries the resolver.

    The registry decides (``pseudo_library``); codegen must not have to recognise
    engine names, and an engine with its own basis sets (NWChem) or bundled
    datasets (GPAW) must not pay for a helper it cannot use.
    """

    def _plan(self, calculator, calc_import):
        return _calc_plan(calculator=calculator, calculator_import=calc_import)

    def test_the_registry_carries_the_library_per_engine(self):
        from method_discovery.calculator_registry import find_calculator
        assert find_calculator("Quantum ESPRESSO").pseudo_library == "sssp"
        assert find_calculator("ABINIT").pseudo_library == "pseudodojo"
        # Ships its own basis sets / bundled datasets -- nothing to resolve.
        assert find_calculator("NWChem").pseudo_library is None
        assert find_calculator("GPAW").pseudo_library is None

    def test_the_engine_binaries_are_the_ones_actually_installed(self):
        """Observed in the provisioned RIS envs, not guessed from the engine name."""
        from method_discovery.calculator_registry import find_calculator
        assert find_calculator("Quantum ESPRESSO").executable == "pw.x"
        assert find_calculator("ABINIT").executable == "abinit"
        # The glibc-pinned cp2k=2024.2 nompi build ships ONLY cp2k.ssmp -- not
        # ASE's default cp2k.psmp, and not the documented cp2k_shell.
        assert find_calculator("CP2K").executable == "cp2k.ssmp"
        assert find_calculator("DFTB+").executable == "dftb+"

    def test_a_qe_bundle_carries_the_resolver(self):
        engine = CodegenEngine()
        bundle = engine.generate(
            self._plan("Quantum ESPRESSO", "ase.calculators.espresso"),
            agent=lambda p: OK_SCRIPT,
            pseudo_library="sssp")
        assert "twain_pseudo.py" in bundle.files()
        assert "espresso_pseudopotentials" in bundle.files()["twain_pseudo.py"]

    def test_an_engine_with_its_own_basis_sets_does_not(self):
        engine = CodegenEngine()
        bundle = engine.generate(
            self._plan("NWChem", "ase.calculators.nwchem"),
            agent=lambda p: OK_SCRIPT)
        assert "twain_pseudo.py" not in bundle.files()

    def test_the_helper_is_written_into_the_bundle_directory(self, tmp_path):
        engine = CodegenEngine()
        bundle = engine.generate(
            self._plan("ABINIT", "ase.calculators.abinit"),
            agent=lambda p: OK_SCRIPT,
            pseudo_library="pseudodojo")
        dest = bundle.write(tmp_path / "b")
        written = (dest / "twain_pseudo.py").read_text()
        # Copied verbatim, so a run executes exactly what the helper's tests cover.
        assert written == _helper_source("twain_pseudo")
        compile(written, "twain_pseudo.py", "exec")

    def test_the_prompt_forbids_writing_a_filename(self):
        engine = CodegenEngine()
        captured = []

        def agent(prompt):
            captured.append(prompt)
            return ("import ase\ndef main():\n    print('ok')\n"
                    "if __name__ == '__main__':\n    main()\n")

        engine.generate(self._plan("Quantum ESPRESSO", "ase.calculators.espresso"),
                        agent=agent, pseudo_library="sssp")
        prompt = captured[0]
        assert "espresso_pseudopotentials" in prompt
        assert "espresso_cutoffs" in prompt
        assert "never write a pseudopotential filename" in prompt
        # It must not be given a filename to copy, and must not be invited to
        # swallow a resolver failure.
        assert ".UPF" not in prompt.replace("hardcode a .UPF name", "")
        assert "do NOT wrap these calls in try/except" in prompt

    def test_abinit_gets_its_own_api_not_espresso_s(self):
        engine = CodegenEngine()
        captured = []
        engine.generate(
            self._plan("ABINIT", "ase.calculators.abinit"),
            agent=lambda p: (captured.append(p) or OK_SCRIPT),
            pseudo_library="pseudodojo")
        assert "abinit_pp_paths" in captured[0]
        assert "abinit_ecut" in captured[0]
        assert "espresso_pseudopotentials" not in captured[0]

    def test_no_pseudo_note_leaks_into_an_ordinary_prompt(self):
        engine = CodegenEngine()
        captured = []
        engine.generate(
            self._plan("NWChem", "ase.calculators.nwchem"),
            agent=lambda p: (captured.append(p) or OK_SCRIPT))
        assert "PSEUDOPOTENTIALS" not in captured[0]


# ── an open-shell spin state stated twice, in two rival channels ──────────────

class TestAmbiguousSpinStateGuard:
    """ASE takes the spin state two ways and each calculator reads only one.

    Magnetic moments on the Atoms object (GPAW, Quantum ESPRESSO, ABINIT, DFTB+)
    versus an explicit count keyword (Psi4/CP2K `multiplicity`, NWChem `mult`,
    xTB `uhf`). State both and the loser is dropped silently.

    Found via a CO2 heat of formation that came back -1072 kJ/mol against a
    -393.5 target: the script asked for multiplicity 3 on its C and O references
    and ASE's Psi4 threw that away because `ase.build.molecule` had put magnetic
    moments on them, so the atoms converged as singlets. Measured at
    b3lyp/sto-3g that is +313 kJ/mol on an O atom and +226 on a C atom. The check
    is written against the two-channel split rather than against Psi4, so the
    engines that have not bitten yet are covered too.
    """

    def _script(self, body):
        return ("from ase.build import molecule\n"
                "def run():\n"
                "    atoms = molecule('O')\n"
                f"{body}"
                "    return atoms.get_potential_energy()\n"
                "if __name__ == '__main__':\n    run()\n")

    # ---- the count channel, across the engines that read it ----------------
    def test_psi4_multiplicity_is_flagged(self):
        assert sd._ambiguous_spin_specification(self._script(
            "    atoms.calc = Psi4(method='b3lyp', multiplicity=3, reference='uks')\n"))

    def test_cp2k_multiplicity_is_flagged(self):
        assert sd._ambiguous_spin_specification(self._script(
            "    atoms.calc = CP2K(multiplicity=3, uks=True)\n"))

    def test_nwchem_mult_in_a_nested_dict_is_flagged(self):
        """NWChem takes it inside an input mapping, not as a call keyword."""
        assert sd._ambiguous_spin_specification(self._script(
            "    atoms.calc = NWChem(dft={'mult': 3, 'xc': 'b3lyp'})\n"))

    def test_xtb_unpaired_electron_count_is_flagged(self):
        """xTB counts unpaired electrons, so open shell is uhf != 0."""
        assert sd._ambiguous_spin_specification(self._script(
            "    atoms.calc = XTB(method='GFN2-xTB', uhf=2)\n"))

    def test_quaccs_spelling_is_flagged(self):
        assert sd._ambiguous_spin_specification(self._script(
            "    e = static_job(atoms, spin_multiplicity=3)\n"))

    def test_a_computed_multiplicity_is_flagged(self):
        """`multiplicity=mult` is exactly the ambiguity, not an exemption."""
        assert sd._ambiguous_spin_specification(self._script(
            "    atoms.calc = Psi4(multiplicity=mult)\n"))

    # ---- the magmom channel, which must stay idiomatic ---------------------
    def test_the_gpaw_idiom_is_not_flagged(self):
        """GPAW reads the moments; spinpol is a switch, not a rival count."""
        assert sd._ambiguous_spin_specification(self._script(
            "    atoms.set_initial_magnetic_moments([2.0])\n"
            "    atoms.calc = GPAW(xc='PBE', spinpol=True)\n")) == []

    def test_a_switch_keyword_alone_is_not_flagged(self):
        for switch in ("spinpol=True", "nspin=2", "uks=True", "unrestricted=True"):
            assert sd._ambiguous_spin_specification(self._script(
                f"    atoms.calc = C({switch})\n")) == [], switch

    def test_reconciling_the_moments_clears_the_finding(self):
        assert sd._ambiguous_spin_specification(self._script(
            "    atoms.set_initial_magnetic_moments([0.0] * len(atoms))\n"
            "    atoms.calc = Psi4(multiplicity=3, reference='uks')\n")) == []

    # ---- closed shell is not a conflict -----------------------------------
    def test_a_closed_shell_multiplicity_is_not_flagged(self):
        assert sd._ambiguous_spin_specification(self._script(
            "    atoms.calc = Psi4(multiplicity=1)\n")) == []

    def test_a_zero_unpaired_electron_count_is_not_flagged(self):
        """uhf=0 is closed shell -- the sentinel differs from multiplicity's."""
        assert sd._ambiguous_spin_specification(self._script(
            "    atoms.calc = XTB(uhf=0)\n")) == []

    def test_no_spin_specification_at_all_is_not_flagged(self):
        assert sd._ambiguous_spin_specification(self._script(
            "    atoms.calc = Psi4(method='b3lyp')\n")) == []

    def test_syntactically_broken_source_does_not_raise(self):
        assert sd._ambiguous_spin_specification("def f(:\n  pass") == []

    # ---- wiring ------------------------------------------------------------
    def test_the_diagnostic_explains_both_channels(self):
        script = ("import matgl\n" + self._script(
            "    atoms.calc = Psi4(multiplicity=3, reference='uks')\n"))
        diags = [d for d in ScriptDoctor(brief=_brief()).static_diagnostics(script)
                 if d.source == "ambiguous-spin-state"]
        assert diags, "the static pass did not surface the finding"
        assert diags[0].severity == "error"
        for expected in ("initial magnetic moments", "set_initial_magnetic_moments",
                         "GPAW", "Psi4", "wrong spin state".upper()):
            assert expected in diags[0].message, expected

    def test_every_prompt_carries_the_spin_rule(self):
        """Unconditional: not gated on an engine list that would date instantly.

        The run that exposed this was library-only (calculator null, toolset
        quacc/ASE/Psi4), so a calculator-template-only rule would have missed it.
        """
        engine = CodegenEngine()
        for library, calculator in (("quacc", None), ("Psi4", None),
                                    ("ASE", "GPAW"), ("Pymatgen", "MatGL")):
            prompt = engine._codegen_prompt({
                "library": library, "library_import": library.lower(),
                "also_available": ["ASE"], "calculator": calculator,
                "calculator_import": "x" if calculator else None,
                "property": "energy", "material": {}, "material_desc": "CO2",
                "structure": None, "acceptance": [{"metric_name": "e"}],
                "output_file": "results.csv", "objective": None,
                "smoke_compute": False, "pseudo_library": None,
            })
            assert "state it exactly ONCE" in prompt, (library, calculator)
            assert "3P triplets" in prompt, (library, calculator)
            assert "set_initial_magnetic_moments" in prompt, (library, calculator)


# ── a multi-species thermochemical cycle gets the helper ──────────────────────

class TestThermoCycleWiring:
    """Gated on the PROPERTY, not on an engine or a molecule.

    The run that exposed the dropped term used NWChem; the one before it used
    Psi4. What they had in common was the quantity -- a heat of formation
    assembled from atomic references -- so that is what the gate reads. Note the
    plan's requested_property was null and only the acceptance metric named the
    property, so the gate has to look at both.
    """

    def _plan(self, metric, prop=None):
        return _calc_plan(metric=metric, target=-393.5, tolerance=5.0,
                          requested_property=prop)

    def test_the_predicate_reads_the_quantity(self):
        assert eng.wants_thermo_cycle("standard_heat_of_formation_kJ_per_mol")
        assert eng.wants_thermo_cycle("atomization_energy_kJ_per_mol")
        assert eng.wants_thermo_cycle("bond_dissociation_enthalpy")
        assert eng.wants_thermo_cycle("cohesive_energy")
        assert eng.wants_thermo_cycle(None, "compute the enthalpy of combustion")
        # Single-species properties need no cycle.
        assert not eng.wants_thermo_cycle("band_gap")
        assert not eng.wants_thermo_cycle("bulk_modulus", None)
        assert not eng.wants_thermo_cycle(None, None)

    def test_a_formation_enthalpy_bundle_carries_the_helper(self):
        bundle = CodegenEngine().generate(
            self._plan("standard_heat_of_formation_kJ_per_mol"),
            agent=lambda p: OK_SCRIPT)
        assert "twain_thermo.py" in bundle.files()
        assert "monatomic_enthalpy_correction" in bundle.files()["twain_thermo.py"]

    def test_the_metric_alone_is_enough_to_trigger_it(self):
        """requested_property was null in the run that dropped the term."""
        plan = self._plan("standard_heat_of_formation_kJ_per_mol", prop=None)
        assert plan["requested_property"] is None
        assert "twain_thermo.py" in CodegenEngine().generate(
            plan, agent=lambda p: OK_SCRIPT).files()

    def test_a_single_species_property_does_not(self):
        bundle = CodegenEngine().generate(
            self._plan("band_gap"), agent=lambda p: OK_SCRIPT)
        assert "twain_thermo.py" not in bundle.files()

    def test_the_helper_is_written_out_verbatim(self, tmp_path):
        from pathlib import Path
        bundle = CodegenEngine().generate(
            self._plan("standard_heat_of_formation_kJ_per_mol"),
            agent=lambda p: OK_SCRIPT)
        dest = bundle.write(tmp_path / "b")
        written = (dest / "twain_thermo.py").read_text()
        assert written == _helper_source("twain_thermo")
        compile(written, "twain_thermo.py", "exec")

    def test_the_prompt_names_the_dropped_term(self):
        captured = []
        CodegenEngine().generate(
            self._plan("standard_heat_of_formation_kJ_per_mol"),
            agent=lambda p: (captured.append(p) or OK_SCRIPT))
        prompt = captured[0]
        assert "twain_thermo" in prompt
        assert "5/2 kT" in prompt
        assert "6.197" in prompt
        # The false justification that shipped must be contradicted explicitly.
        assert "do NOT absorb it" in prompt

    def test_no_thermo_note_on_an_unrelated_property(self):
        captured = []
        CodegenEngine().generate(self._plan("band_gap"),
                                 agent=lambda p: (captured.append(p) or OK_SCRIPT))
        assert "THERMOCHEMICAL CYCLE" not in captured[0]


class TestUncorrelatedThermochemistryGuard:
    """Bare Hartree-Fock for a bond-energy property is a wrong-sign answer.

    A rerun of the CO2 conversation asked Psi4 for method='scf' and reported
    dHf = +245.7 kJ/mol against -393.5. HF recovers no electron correlation,
    which is most of a bond's energy: its atomization came out ~1012 kJ/mol
    against ~1628. The run converged cleanly and printed a confident number.

    Gated on the PROPERTY, not the method: an SCF orbital energy or an HF
    geometry is a reasonable request, so only a multi-species energy difference
    makes this an error.
    """

    def _brief_for(self, prop):
        b = _brief()
        b["property"] = prop
        return b

    HF_SCRIPT = ("import matgl\n"
                 "def run():\n"
                 "    return psi4.energy(method='scf', molecule=m)\n"
                 "if __name__ == '__main__':\n    run()\n")

    def test_hf_for_a_formation_enthalpy_is_an_error(self):
        diags = ScriptDoctor(
            brief=self._brief_for("standard_heat_of_formation_kJ_per_mol")
        ).static_diagnostics(self.HF_SCRIPT)
        hits = [d for d in diags if d.source == "uncorrelated-thermochemistry"]
        assert hits and hits[0].severity == "error"
        assert "wrong SIGN" in hits[0].message

    def test_hf_for_an_orbital_property_is_fine(self):
        """The method is not the problem; the property decides."""
        diags = ScriptDoctor(brief=self._brief_for("homo_lumo_gap")
                             ).static_diagnostics(self.HF_SCRIPT)
        assert not [d for d in diags if d.source == "uncorrelated-thermochemistry"]

    def test_each_engine_spelling_is_caught(self):
        for snippet in ("psi4.energy(method='scf')", "NWChem(theory='scf')",
                        "NWChem(dft={'xc': 'hf'})", "Psi4(method='hf')",
                        "method = 'scf'"):
            script = (f"import matgl\ndef run():\n    x = {snippet}\n"
                      "if __name__ == '__main__':\n    run()\n")
            diags = ScriptDoctor(brief=self._brief_for("atomization_energy")
                                 ).static_diagnostics(script)
            assert [d for d in diags
                    if d.source == "uncorrelated-thermochemistry"], snippet

    def test_a_correlated_method_is_clean(self):
        for method in ("b3lyp", "pbe0", "wb97x-d", "mp2", "ccsd(t)"):
            script = (f"import matgl\ndef run():\n"
                      f"    return psi4.energy(method='{method}')\n"
                      "if __name__ == '__main__':\n    run()\n")
            diags = ScriptDoctor(brief=self._brief_for("heat_of_formation")
                                 ).static_diagnostics(script)
            assert not [d for d in diags
                        if d.source == "uncorrelated-thermochemistry"], method

    def test_the_reaction_route_is_recommended_in_the_prompt(self):
        captured = []
        CodegenEngine().generate(
            _calc_plan(metric="standard_heat_of_formation_kJ_per_mol",
                       target=-393.5, tolerance=10.0),
            agent=lambda p: (captured.append(p) or OK_SCRIPT))
        prompt = captured[0]
        assert "ERROR-CANCELLING REACTION" in prompt
        assert "CO + 1/2 O2 -> CO2" in prompt
        assert "formation_enthalpy_via_reaction" in prompt
        assert "never plain SCF" in prompt


class TestTheGateTestsWhatTheCodeNeeds:
    """A plan can advertise more toolset than the script uses.

    Slurm job 2580768: the plan carried quacc+ASE+Psi4+NWChem, so the smoke gate
    demanded the psi4 python package AND (via the calculator) the nwchem binary.
    Both are conda-only and provisioned in separate envs, so NO single env could
    pass -- whichever engine the script actually drove. The layered venv worked
    exactly as designed ("layering on twain-envs/nwchem; pip adding: quacc") and
    still could not help, because pip cannot supply a conda-only package.

    The gate now requires what the generated source imports. An engine the code
    never touches is not a dependency of the run.
    """

    TWO_ENGINE = {
        "tool_name": "quacc", "calculator": "NWChem",
        "calculator_import": "ase.calculators.nwchem", "calculator_library": "ASE",
        "libraries": ["quacc", "ASE", "Psi4"],
    }

    def _plan(self, method=None):
        plan = _calc_plan(metric="standard_heat_of_formation_kJ_per_mol",
                          target=-393.5, tolerance=10.0,
                          requested_property="standard_heat_of_formation_kJ_per_mol")
        plan["selected_method"] = method or self.TWO_ENGINE
        return plan

    def _requirements(self, script, **kw):
        bundle = CodegenEngine().generate(self._plan(), agent=lambda _p: script, **kw)
        tests = bundle.files()["inline_tests.py"]
        imports = next(l for l in tests.splitlines() if l.startswith("REQUIRED_IMPORTS"))
        execs = next(l for l in tests.splitlines()
                     if l.startswith("REQUIRED_EXECUTABLES"))
        return imports, execs

    PSI4_ONLY = ("import psi4\nfrom quacc.recipes.psi4.core import static_job\n"
                 "def main():\n    print('ok')\n"
                 "if __name__ == '__main__':\n    main()\n")
    NWCHEM_ONLY = ("from ase.calculators.nwchem import NWChem\n"
                   "from quacc.recipes.nwchem.core import static_job\n"
                   "def main():\n    print('ok')\n"
                   "if __name__ == '__main__':\n    main()\n")

    def test_an_unused_engine_is_not_demanded(self):
        """The realistic shape: the script drives the plan's calculator (NWChem)
        and ignores the Psi4 the plan also advertised.

        A script that ignored the calculator entirely could not reach the gate at
        all -- _synthesize_with_llm rejects source that never references
        calculator_import, so it would be replaced by the scaffold and caught by
        the synthesis backstop instead. See the scaffold test below.
        """
        imports, _ = self._requirements(self.NWCHEM_ONLY)
        assert "ase.calculators.nwchem" in imports, "the engine it does drive"
        assert '"psi4"' not in imports, (
            "the script never imports psi4, so requiring it rules out the only "
            "env that has the nwchem binary -- and psi4 is conda-only, so the "
            "pip layer cannot supply it either")

    def test_the_symmetric_case_drops_the_other_engine(self):
        """Same plan shape with the roles swapped: Psi4 driven, NWChem advertised."""
        method = dict(self.TWO_ENGINE, calculator="Psi4", calculator_import="psi4",
                      libraries=["quacc", "ASE", "NWChem"])
        bundle = CodegenEngine().generate(self._plan(method),
                                          agent=lambda _p: self.PSI4_ONLY)
        imports = next(l for l in bundle.files()["inline_tests.py"].splitlines()
                       if l.startswith("REQUIRED_IMPORTS"))
        assert "psi4" in imports
        assert "ase.calculators.nwchem" not in imports

    def test_the_binary_is_only_demanded_when_the_engine_is_driven(self):
        _, execs = self._requirements(self.PSI4_ONLY, calculator_executable="nwchem")
        assert "nwchem" not in execs, (
            "demanding the nwchem binary for a psi4-only script rules out the one "
            "env that can run it")

    def test_the_binary_is_demanded_when_the_engine_is_driven(self):
        _, execs = self._requirements(self.NWCHEM_ONLY, calculator_executable="nwchem")
        assert '"nwchem"' in execs

    def test_a_lazy_import_inside_a_function_still_counts(self):
        """Generated scripts keep heavy imports in functions on purpose."""
        lazy = ("def run():\n    import psi4\n    return psi4\n"
                "def main():\n    run()\n"
                "if __name__ == '__main__':\n    main()\n")
        imports, _ = self._requirements(lazy)
        assert "psi4" in imports

    def test_a_script_importing_none_of_its_toolset_keeps_the_full_gate(self):
        """A scaffold must not get a weakened gate -- that is how a hollow run
        reported success. The synthesis backstop is the real guard; this filter
        must not undercut it."""
        scaffold = ("def main():\n    print('stub')\n"
                    "if __name__ == '__main__':\n    main()\n")
        imports, _ = self._requirements(scaffold)
        for expected in ("quacc", "ase", "psi4", "ase.calculators.nwchem"):
            assert expected in imports, expected

    def test_the_helpers_handle_broken_source(self):
        assert eng._imported_modules("def f(:\n") == set()
        assert eng._needed_imports(["ase"], "def f(:\n") == ["ase"]

    def test_parent_and_child_imports_both_satisfy(self):
        assert eng._imports_module("import ase.calculators.nwchem", "ase")
        assert eng._imports_module("import psi4", "psi4.driver")
        assert not eng._imports_module("import numpy", "psi4")


class TestParallelismIsRegistryDeclared:
    """Where the ranks go, and what finite-difference frequencies cost.

    Job 2601849 launched a file-by-file NWChem driver under `mpirun -np 2`; the
    two ranks clobbered each other's engine files and the run hung for 2h12m. The
    fix is not "never mpirun" -- GPAW must still be wrapped -- so the placement is
    registry data with three values, and the prompt follows it.
    """

    def _prompt(self, *, calculator, parallelism):
        captured = []
        CodegenEngine().generate(
            _calc_plan(calculator=calculator,
                       metric="standard_heat_of_formation_kJ_per_mol"),
            agent=lambda p: (captured.append(p) or OK_SCRIPT),
            parallelism=parallelism)
        return captured[0]

    def test_an_engine_placement_hands_the_ranks_to_the_engine(self):
        prompt = self._prompt(calculator="NWChem", parallelism="engine")
        assert "TWAIN_ENGINE_LAUNCH" in prompt
        assert "Never call mpirun on python" in prompt

    def test_an_engine_placement_warns_about_finite_difference_cost(self):
        prompt = self._prompt(calculator="NWChem", parallelism="engine")
        assert "6N+1" in prompt
        assert "analytic frequency" in prompt

    def test_an_in_process_engine_gets_neither_instruction(self):
        """GPAW is wrapped by the payload; the script must not second-guess it."""
        prompt = self._prompt(calculator="GPAW", parallelism="interpreter")
        assert "TWAIN_ENGINE_LAUNCH" not in prompt

    def test_a_threaded_engine_gets_neither_instruction(self):
        prompt = self._prompt(calculator="xtb", parallelism="threads")
        assert "TWAIN_ENGINE_LAUNCH" not in prompt
        assert "6N+1" not in prompt


class TestEngineLaunchIsActuallyUsed:
    """The payload publishes the allocation; nothing forced the script to spend it.

    For the "engine" placement the driver runs single-process on purpose and the
    ranks are handed over as TWAIN_ENGINE_LAUNCH. A script that ignores it is
    correct and single-core -- it asks Slurm for N CPUs and uses one, which is how
    a job that should take minutes takes hours (job 2601849 burned 2h12m).
    """

    IGNORES = ("from ase.calculators.nwchem import NWChem\n"
               "def run():\n"
               "    return NWChem(command='nwchem PREFIX.nwi > PREFIX.nwo')\n"
               "if __name__ == '__main__':\n    run()\n")
    USES = ("import os\n"
            "from ase.calculators.nwchem import NWChem\n"
            "def run():\n"
            "    launch = os.environ.get('TWAIN_ENGINE_LAUNCH', '')\n"
            "    return NWChem(command=f'{launch} nwchem PREFIX.nwi > PREFIX.nwo')\n"
            "if __name__ == '__main__':\n    run()\n")

    def _diags(self, source, parallelism):
        brief = dict(_brief(), parallelism=parallelism)
        return [d for d in ScriptDoctor(brief=brief).static_diagnostics(source)
                if d.source == "engine-launch-ignored"]

    def test_an_engine_run_that_ignores_the_launcher_is_flagged(self):
        hits = self._diags(self.IGNORES, "engine")
        assert hits, "a single-core run of an N-core allocation must be surfaced"
        assert hits[0].line == 3, "point at where the command is built"

    def test_reading_the_launcher_clears_it(self):
        assert self._diags(self.USES, "engine") == []

    def test_it_is_a_warning_not_an_error(self):
        """The science is right; blocking the run over utilisation would be worse.

        Phase 1 only repairs when an error exists, so an unfixable error here would
        make an otherwise-good script `unrepairable` and stop EXECUTE.
        """
        assert self._diags(self.IGNORES, "engine")[0].severity == "warning"

    def test_the_other_placements_never_flag(self):
        """Wrapping is the payload's job for `interpreter`, and `threads` has no MPI."""
        for placement in ("interpreter", "threads"):
            assert self._diags(self.IGNORES, placement) == [], placement

    def test_a_missing_placement_defaults_to_silent(self):
        brief = _brief()          # no parallelism key at all
        assert [d for d in ScriptDoctor(brief=brief).static_diagnostics(self.IGNORES)
                if d.source == "engine-launch-ignored"] == []

    def test_broken_source_yields_nothing(self):
        """A syntax error is the compile check's finding, and it short-circuits."""
        assert sd._engine_launch_ignored("def f(:\n", "engine") == []

    def test_an_unlocalisable_finding_reports_no_line(self):
        """Whole-file property: better no line than a misleading first import."""
        source = ("from ase.calculators.nwchem import NWChem\n"
                  "def run():\n    return NWChem(xc='b3lyp')\n"
                  "if __name__ == '__main__':\n    run()\n")
        hits = self._diags(source, "engine")
        assert hits and hits[0].line is None

    def test_it_points_at_a_profile_when_there_is_no_command_kwarg(self):
        source = ("from ase.calculators.espresso import Espresso, EspressoProfile\n"
                  "def run():\n"
                  "    p = EspressoProfile(pseudo_dir='/x')\n"
                  "    return Espresso(profile=p)\n"
                  "if __name__ == '__main__':\n    run()\n")
        assert self._diags(source, "engine")[0].line == 3

    def test_a_runnable_but_wasteful_script_reaches_the_hardening_pass(self):
        """Warnings act in phase 2, where a failed fix keeps the runnable script."""
        seen = {}

        def agent(prompt):
            seen["prompt"] = prompt
            return self.USES

        doctor = ScriptDoctor(
            brief=dict(_brief(), parallelism="engine"), agent=agent,
            verifier=lambda src, brief: SmokeOutcome("pass"),
            review=lambda src: [])
        report = doctor.heal("import matgl\n" + self.IGNORES)
        assert report.healthy, "a utilisation warning must never block the run"
        assert "TWAIN_ENGINE_LAUNCH" in seen.get("prompt", ""), (
            "the warning has to reach the model for it to be fixable")


class TestTheRepairSandboxRunsTheRealBundle:
    """A bundle can carry helper modules; smoking main.py alone breaks them.

    twain_pseudo.py and twain_thermo.py reach a run only through
    RunBundle.helpers, but ScriptDoctor.smoke() wrote main.py into a bare temp
    dir. The import then failed for a reason that is not the code's fault -- and
    _classify_smoke graded it `repairable`, so up to max_rounds LLM repairs were
    spent on it. The cheapest way for a model to silence
    "No module named twain_pseudo" is to drop the import and write the
    pseudopotential filenames inline, which is precisely what that helper exists
    to prevent. A safety feature that talks the model out of using it is worse
    than not having it.
    """

    def _doctor(self, **kw):
        return ScriptDoctor(brief=dict(_brief(),
                                       calculator_import="ase.calculators.espresso",
                                       library_import="ase"), **kw)

    def _missing(self, module):
        return f"Traceback...\nModuleNotFoundError: No module named '{module}'\n"

    def test_a_missing_helper_is_never_a_code_bug(self):
        doctor = self._doctor(bundle_files={"twain_pseudo.py": "x = 1\n"})
        assert doctor._classify_smoke(
            self._missing("twain_pseudo")).status == "unverifiable"

    def test_that_holds_even_if_the_copy_never_happened(self):
        """Belt and braces: the twain_ prefix is enough on its own."""
        doctor = self._doctor()
        for module in ("twain_pseudo", "twain_thermo"):
            assert doctor._classify_smoke(
                self._missing(module)).status == "unverifiable", module

    def test_a_real_missing_module_is_still_repairable(self):
        """The guard must not blunt the check it lives in."""
        assert self._doctor()._classify_smoke(
            self._missing("matgl")).status == "repairable"

    def test_the_toolset_exemption_still_works(self):
        assert self._doctor()._classify_smoke(
            self._missing("ase")).status == "unverifiable"

    def test_the_doctor_carries_the_bundle_files(self, tmp_path):
        doctor = self._doctor(bundle_files={"twain_thermo.py": "VALUE = 41\n"},
                             sim_python=None)
        assert doctor.bundle_files == {"twain_thermo.py": "VALUE = 41\n"}

    def test_a_script_importing_a_helper_smokes_clean_in_the_sandbox(self, tmp_path):
        """End to end through the real subprocess path."""
        import sys
        script = ("import argparse\nfrom twain_thermo import VALUE\n"
                  "p = argparse.ArgumentParser(); p.add_argument('--smoke', "
                  "action='store_true'); p.add_argument('--output', default='results.csv')\n"
                  "a = p.parse_args()\n"
                  "open(a.output, 'w').write(f'value\\n{VALUE}\\n')\n")
        doctor = self._doctor(bundle_files={"twain_thermo.py": "VALUE = 41\n"},
                              sim_python=sys.executable)
        assert doctor.smoke(script).status == "pass"


# ═══════════════════════════════════════════════════════════════════════════
# Element coverage + silent calculator substitution (job 2611227)
# ═══════════════════════════════════════════════════════════════════════════
class TestElementCoverage:
    """A potential used outside its parameter table is a guaranteed failure.

    Job 2611227 burned its allocation on it: MACE was absent from the env, the
    generated script fell back to EMT, and the material contained Ca. ASE raised
    NotImplementedError from inside initialize(); the script's bare except
    swallowed it; the next evaluation reused the same calculator on new Atoms with
    the same species, so 'numbers' was not in system_changes, initialize() was
    skipped, and it died on "'EMT' object has no attribute 'nl'".
    """

    def _brief_with(self, formula):
        b = _brief()
        b["formula"] = formula
        b["calculator_import"] = "ase"
        return b

    EMT_SCRIPT = ("import ase\n"
                  "from ase.calculators.emt import EMT\n"
                  "def main():\n"
                  "    calc = EMT()\n"
                  "    print(calc)\n"
                  "if __name__ == '__main__':\n    main()\n")

    def test_an_unsupported_element_is_an_error(self):
        diags = ScriptDoctor(brief=self._brief_with("CaPt2")).static_diagnostics(
            self.EMT_SCRIPT)
        hits = [d for d in diags if d.source == "element-coverage"]
        assert len(hits) == 1
        assert hits[0].severity == "error"
        assert "Ca" in hits[0].message
        assert "nl" in hits[0].message      # names the misleading symptom

    def test_a_supported_material_is_left_alone(self):
        """EMT on Pt or Cu is legitimate; the check must not fire."""
        for formula in ("Pt", "Cu", "AgAu", "CuNi"):
            diags = ScriptDoctor(brief=self._brief_with(formula)).static_diagnostics(
                self.EMT_SCRIPT)
            assert not [d for d in diags if d.source == "element-coverage"], formula

    def test_a_script_that_never_touches_emt_is_left_alone(self):
        script = ("import ase\ndef main():\n    print('hi')\n"
                  "if __name__ == '__main__':\n    main()\n")
        diags = ScriptDoctor(brief=self._brief_with("CaPt2")).static_diagnostics(script)
        assert not [d for d in diags if d.source == "element-coverage"]

    @pytest.mark.parametrize("formula", [
        None, "", "   ", "the requested material", "Calcium diplatinide", "Foo",
    ])
    def test_a_non_formula_never_blocks_a_run(self, formula):
        """error severity can block, so an unparseable name must yield nothing."""
        diags = ScriptDoctor(brief=self._brief_with(formula)).static_diagnostics(
            self.EMT_SCRIPT)
        assert not [d for d in diags if d.source == "element-coverage"]

    @pytest.mark.parametrize("text,expected", [
        ("CaPt2", {"Ca", "Pt"}),
        ("C9H8O4", {"C", "H", "O"}),
        ("Si", {"Si"}),
        ("Mg(OH)2", {"Mg", "O", "H"}),
        ("CuSO4·5H2O", {"Cu", "S", "O", "H"}),
        # Not formulas -- must yield nothing rather than a plausible-looking guess
        ("Foo", frozenset()),                    # "Fo" is not an element
        ("Calcium diplatinide", frozenset()),    # would otherwise find "Ca"
        ("Calciumdiplatinide", frozenset()),     # same, without the space to help
        ("the requested material", frozenset()),
        (None, frozenset()),
        ("", frozenset()),
    ])
    def test_formula_elements_is_conservative(self, text, expected):
        assert sd.formula_elements(text) == expected

    def test_the_emt_table_matches_ase(self):
        """Pinned to ASE itself, so an ASE update cannot leave the table stale."""
        try:
            from ase.calculators.emt import parameters
        except Exception:                                  # pragma: no cover
            pytest.skip("ase not importable in this env")
        assert sd._ELEMENT_LIMITED_CALCULATORS["EMT"] == frozenset(parameters)


class TestSilentCalculatorSubstitution:
    """A missing library must fail loudly, not become a different experiment."""

    SUBSTITUTING = (
        "import ase\n"
        "def get_calculator():\n"
        "    try:\n"
        "        from mace.calculators import mace_mp\n"
        "    except ModuleNotFoundError:\n"
        "        from ase.calculators.emt import EMT\n"
        "        return EMT()\n"
        "    return mace_mp(model='small')\n"
        "def main():\n"
        "    print(get_calculator())\n"
        "if __name__ == '__main__':\n    main()\n")

    def test_a_swapped_in_calculator_is_an_error(self):
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(self.SUBSTITUTING)
        hits = [d for d in diags if d.source == "calculator-substitution"]
        assert len(hits) == 1
        assert hits[0].severity == "error"
        assert "mace" in hits[0].message and "ase" in hits[0].message

    def test_an_aliasing_fallback_is_not_flagged(self):
        """`except ImportError: import tomli as tomllib` builds nothing."""
        script = ("import ase\n"
                  "try:\n    import tomllib\n"
                  "except ImportError:\n    import tomli as tomllib\n"
                  "def main():\n    print(tomllib)\n"
                  "if __name__ == '__main__':\n    main()\n")
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not [d for d in diags if d.source == "calculator-substitution"]

    def test_a_retry_within_the_same_library_is_not_flagged(self):
        """Trying several checkpoints of the SAME potential is legitimate."""
        script = ("import ase\n"
                  "def load():\n"
                  "    try:\n        from mace.calculators import mace_mp\n"
                  "    except ImportError:\n"
                  "        from mace.calculators import mace_off\n"
                  "        return mace_off()\n"
                  "    return mace_mp()\n"
                  "def main():\n    print(load())\n"
                  "if __name__ == '__main__':\n    main()\n")
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not [d for d in diags if d.source == "calculator-substitution"]

    def test_a_non_import_except_is_not_flagged(self):
        """Only ImportError fallbacks substitute a library."""
        script = ("import ase\n"
                  "def load():\n"
                  "    try:\n        from ase.calculators.emt import EMT\n"
                  "        return EMT()\n"
                  "    except ValueError:\n"
                  "        from numpy import zeros\n"
                  "        return zeros(3)\n"
                  "def main():\n    print(load())\n"
                  "if __name__ == '__main__':\n    main()\n")
        diags = ScriptDoctor(brief=_brief()).static_diagnostics(script)
        assert not [d for d in diags if d.source == "calculator-substitution"]

    def test_both_checks_fire_on_the_real_failing_script(self):
        """The shape of job 2611227's main.py: substitution AND bad coverage."""
        brief = _brief()
        brief["formula"] = "CaPt2"
        brief["calculator_import"] = "ase"
        diags = ScriptDoctor(brief=brief).static_diagnostics(self.SUBSTITUTING)
        sources = {d.source for d in diags if d.severity == "error"}
        assert "calculator-substitution" in sources
        assert "element-coverage" in sources


class TestACalculationNeedsAPlannedCalculator:
    """A library-only plan must not have a script that computes energies.

    The mirror of the existing "never references the required calculator import"
    check: that one catches a script IGNORING the planned engine, this catches one
    INVENTING an engine the plan never chose. A NaCl2 heat-of-formation run planned
    as Pymatgen with calculator=null generated a script that tried MACE, then
    CHGNet, then M3GNet, and died in EXECUTE on "No module named matgl" -- none of
    the three is provisioned, and none was in requirements.txt because none was in
    the plan (Slurm job 2633871). It is the second run lost to this.
    """

    LIBRARY_ONLY = {"library": "Pymatgen", "library_import": "pymatgen",
                    "calculator": None, "calculator_import": None,
                    "property": "total_energy", "acceptance": [],
                    "output_file": "results.csv"}

    def _script(self, body):
        return ("import pymatgen\n"
                f"def main():\n{body}"
                "if __name__ == '__main__':\n    main()\n")

    def _findings(self, body, brief=None):
        script = self._script(body)
        #  would treat an EMPTY brief as absent -- which is the
        # exact case one test below is checking.
        chosen = self.LIBRARY_ONLY if brief is None else brief
        diags = ScriptDoctor(brief=chosen).static_diagnostics(script)
        return [d for d in diags if d.source == "calculator-not-planned"]

    def test_attaching_a_calculator_is_an_error(self):
        hits = self._findings("    atoms.calc = something()\n")
        assert len(hits) == 1 and hits[0].severity == "error"
        assert ".calc" in hits[0].message

    def test_asking_for_an_energy_is_an_error(self):
        hits = self._findings("    e = atoms.get_potential_energy()\n")
        assert len(hits) == 1
        assert "get_potential_energy" in hits[0].message

    @pytest.mark.parametrize("call", [
        "get_forces", "get_stress", "get_dipole_moment", "get_magnetic_moments",
    ])
    def test_other_calculator_only_quantities(self, call):
        assert self._findings(f"    x = atoms.{call}()\n")

    def test_silent_when_the_plan_selected_a_calculator(self):
        """With an engine planned it is resourced and provisioned -- and the
        existing check already insists the script actually uses it."""
        planned = {**self.LIBRARY_ONLY, "calculator": "GPAW",
                   "calculator_import": "gpaw"}
        assert not self._findings("    atoms.calc = GPAW()\n"
                                  "    e = atoms.get_potential_energy()\n", planned)

    def test_silent_for_a_genuine_library_only_script(self):
        """A lookup or a descriptor pass computes nothing and must not be flagged."""
        for body in (
            "    from mp_api.client import MPRester\n    print('lookup')\n",
            "    from pymatgen.core import Composition\n"
            "    print(Composition('NaCl').weight)\n",
            "    from rdkit import Chem\n    print(Chem.MolFromSmiles('CCO'))\n",
            "    print(structure.volume, structure.density)\n",
        ):
            assert not self._findings(body), body

    def test_silent_when_the_brief_carries_no_plan(self):
        """Other callers pass no plan; they must not be second-guessed."""
        assert not self._findings("    atoms.calc = x\n", brief={})

    def test_it_reports_where(self):
        hits = self._findings("    atoms.calc = x\n")
        assert hits[0].line is not None

    def test_a_syntax_error_does_not_crash_it(self):
        diags = ScriptDoctor(brief=self.LIBRARY_ONLY).static_diagnostics("def f(:\n")
        assert [d for d in diags if d.source == "compile"]
        assert not [d for d in diags if d.source == "calculator-not-planned"]
