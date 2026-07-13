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
