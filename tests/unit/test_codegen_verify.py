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

from code_gen import codegen_engine as eng
from code_gen import script_doctor as sd
from code_gen.codegen_engine import CodegenEngine, pixi_env_python
from code_gen.script_doctor import (
    Diagnostic, ScriptDoctor, SmokeOutcome, _parse_findings, _undefined_names,
)


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
