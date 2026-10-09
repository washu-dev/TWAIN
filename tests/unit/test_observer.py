"""The observer's deterministic gates (#185, P1b)."""
from __future__ import annotations

from pathlib import Path

import observer as O
import pytest
import statemachine as SM
from states import State

REPO = Path(__file__).resolve().parents[2]

# Run 44ad9f9e's bundle, in miniature: the descriptor template chosen for a
# solubility request, needing a molecules.csv that nobody supplied.
DESCRIPTOR_MAIN = '''
INPUT_FILE = "molecules.csv"      # default SMILES CSV input path
_ACCEPTANCE_JSON = r"""[{"metric_name": "aqueous_solubility_at_25C"}]"""
def run(config, input_path, output_path):
    raise SystemExit("no molecules to predict: TWAIN refuses to fabricate placeholder molecules.")
def main():
    parser.add_argument("--input", default=INPUT_FILE, help="SMILES CSV input")
    rows.append({"mol_weight": 1, "logp": 2, "tpsa": 3})
'''
SOLUBILITY_PLAN = {"requested_property": "aqueous_solubility_at_25C",
                   "acceptance_metrics": [{"metric_name": "aqueous_solubility_at_25C",
                                           "target_value": None, "tolerance": None}]}


def _bundle(tmp_path, main, **files):
    (tmp_path / "main.py").write_text(main)
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    return tmp_path


class TestBundleGate:
    def test_run_44ad9f9e_is_stopped_before_the_cluster(self, tmp_path):
        v = O.bundle_gate(SOLUBILITY_PLAN, _bundle(tmp_path, DESCRIPTOR_MAIN),
                          template="property_prediction", periodic=False)
        assert {c.name for c in v.failed} == {"input", "metric"}
        assert "molecules.csv" in next(c for c in v.failed if c.name == "input").detail

    def test_the_input_file_present_is_fine(self, tmp_path):
        v = O.bundle_gate({"requested_property": "logp"},
                          _bundle(tmp_path, DESCRIPTOR_MAIN, **{"molecules.csv": "smiles\nCCO\n"}),
                          template="property_prediction", periodic=False)
        assert not v.failed

    def test_a_synthesized_script_is_not_judged_on_names(self, tmp_path):
        v = O.bundle_gate(SOLUBILITY_PLAN, _bundle(tmp_path, "print({'logS': -1.7})\n"),
                          template="llm_synthesized", periodic=False)
        assert not v.failed

    def test_a_crystal_bundle_without_the_guard_warns(self, tmp_path):
        v = O.bundle_gate({"requested_property": "band_gap"}, _bundle(tmp_path, "band_gap = 1\n"),
                          template="llm_synthesized", periodic=True)
        assert [c.name for c in v.warnings] == ["structure"]
        (tmp_path / "twain_expected_structure.json").write_text("{}")
        v = O.bundle_gate({"requested_property": "band_gap"}, tmp_path,
                          template="llm_synthesized", periodic=True)
        assert not v.warnings


class TestPlanGate:
    def test_nothing_to_verify_against_warns_before_approval(self, monkeypatch):
        monkeypatch.delenv("MP_API_KEY", raising=False)
        plan = {"requested_property": "band_gap", "target_system": {"kind": "crystal", "formula": "Si"}}
        v = O.plan_gate(plan, None, repo_root=REPO, periodic=True, env_candidates=["gpaw"],
                        execute_slurm=True)
        assert [c.name for c in v.warnings] == ["reference"]
        assert "Set a target" in v.warnings[0].detail

    def test_materials_project_covers_a_crystal(self, monkeypatch):
        monkeypatch.setenv("MP_API_KEY", "k")
        v = O.plan_gate({"requested_property": "band_gap"}, None, repo_root=REPO, periodic=True,
                        env_candidates=["gpaw"], execute_slurm=True)
        assert not v.warnings and "Materials Project" in v.checks[-1].detail

    def test_esol_covers_caffeine_solubility(self, monkeypatch):
        monkeypatch.delenv("MP_API_KEY", raising=False)
        plan = {"requested_property": "logS",
                "target_system": {"kind": "molecule", "molecule": {"name": "caffeine"}}}
        v = O.plan_gate(plan, None, repo_root=REPO, periodic=False, env_candidates=["default"],
                        execute_slurm=True)
        assert not v.warnings and "ESOL" in v.checks[-1].detail

    def test_a_target_on_the_card_counts(self, monkeypatch):
        monkeypatch.delenv("MP_API_KEY", raising=False)
        plan = {"requested_property": "band_gap", "acceptance_metrics": [
            {"metric_name": "band_gap", "target_value": 0.6, "tolerance": 0.15}]}
        v = O.plan_gate(plan, None, repo_root=REPO, periodic=True, env_candidates=["gpaw"],
                        execute_slurm=True)
        assert not v.warnings and "0.6" in v.checks[-1].detail

    def test_no_environment_fails(self):
        v = O.plan_gate({}, None, repo_root=REPO, periodic=False, env_candidates=None,
                        execute_slurm=True)
        assert [c.name for c in v.failed] == ["environment"]


class TestOutputsGate:
    def test_a_job_with_nothing_to_interpret_fails(self, tmp_path):
        v = O.outputs_gate({"status": "success", "succeeded": True,
                            "artifacts_dir": str(tmp_path), "stdout": ""})
        assert v.failed

    def test_outputs_pass(self, tmp_path):
        (tmp_path / "results.csv").write_text("band_gap\n0.6\n")
        assert not O.outputs_gate({"status": "success", "succeeded": True,
                                   "artifacts_dir": str(tmp_path)}).failed


class TestInTheStateMachine:
    def _machine(self, tmp_path, plan, bundle=None):
        steps = []
        artifacts = {"execution_plan": plan}
        m = SM.StateMachine.__new__(SM.StateMachine)
        m.execute_slurm = False
        m.context = SM.Context() if hasattr(SM, "Context") else type("C", (), {"artifacts": {}})()
        m.context.artifacts = {"run_bundle": str(bundle)} if bundle else {}
        m._load_artifact = lambda name: artifacts.get(name)
        m._write_artifact = lambda name, data: (artifacts.__setitem__(name, data), str(tmp_path / name))[1]
        m._bundle_config = lambda d: {"template": "property_prediction"}
        m._progress = lambda *a, **k: steps.append(a)
        return m, steps, artifacts

    def test_a_failing_bundle_stops_before_execute(self, tmp_path):
        m, steps, _ = self._machine(tmp_path, SOLUBILITY_PLAN, _bundle(tmp_path, DESCRIPTOR_MAIN))
        with pytest.raises(Exception, match="observer stopped the run before EXECUTE"):
            m._observe(State.REPAIR, State.EXECUTE)
        assert any("Observer ✕" in s[3] for s in steps)

    def test_the_plan_warning_lands_on_the_plan_card_once(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MP_API_KEY", raising=False)
        plan = {"requested_property": "band_gap", "target_system": {"kind": "crystal", "formula": "Si"}}
        m, _, artifacts = self._machine(tmp_path, plan)
        m._observe(State.PLAN, State.BUILD)
        m._observe(State.PLAN, State.BUILD)
        notes = artifacts["execution_plan"]["safety_notes"]
        assert len([n for n in notes if n.startswith("Observer:")]) == 1

    def test_it_can_be_switched_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TWAIN_OBSERVER", "0")
        m, _, _ = self._machine(tmp_path, SOLUBILITY_PLAN, _bundle(tmp_path, DESCRIPTOR_MAIN))
        m._observe(State.REPAIR, State.EXECUTE)            # no raise

    def test_other_transitions_are_not_gated(self, tmp_path):
        m, steps, _ = self._machine(tmp_path, SOLUBILITY_PLAN)
        m._observe(State.INTAKE, State.CLARIFY)
        assert steps == []


def _reviewer(verdict, reason="because", seen=None):
    def agent(prompt):
        if seen is not None:
            seen.append(prompt)
        return f'Sure. {{"verdict": "{verdict}", "reason": "{reason}"}}'
    return agent


class TestReviewerOnTheScript:
    """The LLM reviewer before EXECUTE (#188): request + script only."""

    PLAN = {"requested_property": "aqueous_solubility_at_25C",
            "target_system": {"kind": "molecule", "molecule": {"name": "caffeine"}}}

    def _check(self, agent, monkeypatch, mode=None, main="print(logS)\n"):
        if mode:
            monkeypatch.setenv("TWAIN_OBSERVER_LLM", mode)
        else:
            monkeypatch.delenv("TWAIN_OBSERVER_LLM", raising=False)
        v = O.Verdict("bundle")
        O.llm_script_check(v, agent, request="Aqueous solubility of caffeine at 25 C",
                           plan=self.PLAN, main_py=main)
        return v

    def test_it_sees_the_request_and_the_script_and_nothing_else(self, monkeypatch):
        seen = []
        self._check(_reviewer("yes", seen=seen), monkeypatch, main="SCRIPT_BODY = 1\n")
        assert "Aqueous solubility of caffeine" in seen[0] and "SCRIPT_BODY" in seen[0]
        assert "did not write" in seen[0]

    def test_a_yes_passes(self, monkeypatch):
        v = self._check(_reviewer("yes"), monkeypatch)
        assert [c.status for c in v.checks] == [O.PASS]

    def test_a_no_stops_the_run(self, monkeypatch):
        v = self._check(_reviewer("no", "it computes logP descriptors, not solubility"), monkeypatch)
        assert v.failed and "logP descriptors" in v.failed[0].detail

    def test_warn_mode_only_warns(self, monkeypatch):
        v = self._check(_reviewer("no"), monkeypatch, mode="warn")
        assert not v.failed and v.warnings

    @pytest.mark.parametrize("agent", [_reviewer("unsure"), _reviewer("maybe"),
                                       lambda p: "no json", lambda p: 1 / 0, None])
    def test_an_unclear_answer_or_no_agent_adds_nothing(self, agent, monkeypatch):
        assert self._check(agent, monkeypatch).checks == []

    def test_off_means_off(self, monkeypatch):
        seen = []
        assert self._check(_reviewer("no", seen=seen), monkeypatch, mode="0").checks == []
        assert seen == []


class TestReviewerOnTheOutputs:
    def test_doubtful_outputs_warn_and_never_stop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TWAIN_OBSERVER_LLM", raising=False)
        (tmp_path / "results.json").write_text('{"band_gap_eV": 0.0}')
        seen, v = [], O.Verdict("outputs")
        O.llm_outputs_check(v, _reviewer("no", "silicon is not a metal", seen), request="Si gap",
                            plan={"requested_property": "band_gap"},
                            execution_result={"succeeded": True, "artifacts_dir": str(tmp_path)})
        assert not v.failed and "silicon is not a metal" in v.warnings[0].detail
        assert '"band_gap_eV": 0.0' in seen[0]

    def test_a_failed_job_is_not_reviewed(self, tmp_path):
        v = O.Verdict("outputs")
        O.llm_outputs_check(v, _reviewer("no"), request="x", plan={},
                            execution_result={"succeeded": False, "stdout": "boom"})
        assert v.checks == []


class TestReviewerInTheStateMachine:
    _machine = TestInTheStateMachine._machine

    def test_a_no_on_the_script_stops_before_execute(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TWAIN_OBSERVER_LLM", raising=False)
        m, _, _ = self._machine(tmp_path, {"requested_property": "logp"},
                                _bundle(tmp_path, "logp = None\nprint('hello')\n"))
        m._agent = _reviewer("no", "it prints hello")
        m._request = "logP of ethanol"
        with pytest.raises(Exception, match="it prints hello"):
            m._observe(State.REPAIR, State.EXECUTE)

    def test_doubtful_outputs_are_recorded_with_the_result(self, tmp_path, monkeypatch):
        monkeypatch.delenv("TWAIN_OBSERVER_LLM", raising=False)
        (tmp_path / "results.csv").write_text("band_gap\n0.0\n")
        m, _, artifacts = self._machine(tmp_path, {"requested_property": "band_gap"})
        artifacts["execution_result"] = {"status": "success", "succeeded": True,
                                         "artifacts_dir": str(tmp_path)}
        m._agent = _reviewer("no", "a zero gap for silicon")
        m.review_request = "band gap of silicon"
        m._observe(State.EXECUTE, State.INTERPRET)
        assert "a zero gap for silicon" in artifacts["execution_result"]["observer_warnings"][0]


class TestReviewerOnTheMethod:
    """#222: the reviewer judges the method at PLAN, before the approval card."""

    PLAN = {"requested_property": None,
            "acceptance_metrics": [{"metric_name": "aqueous_solubility_at_25C"}],
            "summary": "Hydration free energy with OpenMM + GBSA; log S = -dG_hyd / 2.303RT",
            "selected_method": {"libraries": ["OpenMM", "OpenFF Toolkit"], "calculator": None}}

    def test_it_sees_the_method_the_summary_and_the_guidance(self, monkeypatch):
        monkeypatch.delenv("TWAIN_OBSERVER_LLM", raising=False)
        seen = []
        O.llm_method_check(_reviewer("no", seen=seen), request="aspirin solubility",
                           plan=self.PLAN, guidance="ESOL ... a hydration free energy alone")
        assert "OpenMM + OpenFF Toolkit" in seen[0] and "GBSA" in seen[0]
        assert "Established routes" in seen[0] and "ESOL" in seen[0]

    @pytest.mark.parametrize("verdict, expected", [("yes", "yes"), ("no", "no"),
                                                   ("unsure", None), ("maybe", None)])
    def test_verdicts(self, verdict, expected, monkeypatch):
        monkeypatch.delenv("TWAIN_OBSERVER_LLM", raising=False)
        answer = O.llm_method_check(_reviewer(verdict), request="x", plan=self.PLAN)
        assert (answer[0] if answer else None) == expected

    def test_off_or_no_agent_asks_nothing(self, monkeypatch):
        monkeypatch.setenv("TWAIN_OBSERVER_LLM", "0")
        assert O.llm_method_check(_reviewer("no"), request="x", plan=self.PLAN) is None
        monkeypatch.delenv("TWAIN_OBSERVER_LLM")
        assert O.llm_method_check(None, request="x", plan=self.PLAN) is None


class TestReviewerRejectionAtRepairReplans:
    _machine = TestInTheStateMachine._machine

    def _repair(self, tmp_path, monkeypatch, failed=()):
        monkeypatch.delenv("TWAIN_OBSERVER_LLM", raising=False)
        plan = {"requested_property": "logp",
                "selected_method": {"libraries": ["RDKit"], "calculator": None}}
        m, _, _ = self._machine(tmp_path, plan, _bundle(tmp_path, "logp = None\nprint('hello')\n"))
        m._agent = _reviewer("no", "it prints hello")
        m._request = "logP of ethanol"
        m.context.failed_methods = list(failed)
        return m

    def test_a_rejected_script_replans_without_the_method(self, tmp_path, monkeypatch):
        m = self._repair(tmp_path, monkeypatch)
        assert m._observe(State.REPAIR, State.EXECUTE) == State.REPLAN
        (entry,) = m.context.failed_methods
        assert (entry["method"], entry["stage"]) == ("rdkit", "REPAIR")
        assert "it prints hello" in entry["reason"]
        assert SM.GUARDS[(State.REPAIR, State.REPLAN)](m.context)

    def test_past_the_budget_it_stops_with_the_reason_first(self, tmp_path, monkeypatch):
        m = self._repair(tmp_path, monkeypatch,
                         failed=[{"method": "xtb", "stage": "EXECUTE", "last_attempt": 2}])
        with pytest.raises(Exception) as err:
            m._observe(State.REPAIR, State.EXECUTE)
        assert str(err.value).startswith("The reviewer found the script doesn't compute")

    def test_plan_rejections_dont_spend_the_fallback_budget(self, tmp_path, monkeypatch):
        m = self._repair(tmp_path, monkeypatch,
                         failed=[{"method": "openmm", "stage": "PLAN", "last_attempt": 0}])
        assert m._observe(State.REPAIR, State.EXECUTE) == State.REPLAN


def test_the_failure_card_leads_with_the_reviewers_reason(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "modules" / "07_runtime_orchestrator"))
    import error_handler
    m, _, _ = TestInTheStateMachine._machine(TestInTheStateMachine(), tmp_path,
                                             {"requested_property": "logp"},
                                             _bundle(tmp_path, "logp = None\n"))
    m._agent = _reviewer("no", "It computes a hydration energy, not a solubility")
    monkeypatch.delenv("TWAIN_OBSERVER_LLM", raising=False)
    with pytest.raises(Exception) as err:
        m._observe(State.REPAIR, State.EXECUTE)
    card = error_handler.describe_failure(error_handler.classify(err.value, "REPAIR"), "REPAIR", None)
    assert "hydration energy, not a solubility" in card["headline"]
