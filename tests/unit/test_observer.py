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
