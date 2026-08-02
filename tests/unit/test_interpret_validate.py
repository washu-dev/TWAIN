"""Unit tests for the INTERPRET / VALIDATE / CORRECT state-machine handlers.

These stages wire the control plane to the Epic 6 modules:
  * interpret() -> result_interpreter (parse run output, normalize metrics)
  * validate()  -> cross_validation (baseline comparison + acceptance verdict)
  * correct()   -> self_correction (failure diagnosis + CorrectionPlan artifact)

All tests run offline: they seed the upstream artifacts (execution_result,
execution_plan, intent_spec) directly and call the handlers.

Run from the repo root with:  pixi run pytest tests/unit/test_interpret_validate.py
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO_ROOT / "modules" / "16_agent_mesh_control_plane"
sys.path.insert(0, str(MODULE_DIR))

from states import State  # noqa: E402
from crash_recovery import DataStorage  # noqa: E402
import statemachine as SM  # noqa: E402

SCHEMA_DIR = REPO_ROOT / "schemas"

INTENT = {
    "objective": "Predict the aqueous solubility of aspirin",
    "domain": "materials",
    "system_descriptors": {
        "formula": "C9H8O4",
        "molecule": {"name": "aspirin", "SMILES": "CC(=O)Oc1ccccc1C(=O)O"},
    },
    "acceptance_metrics": [
        {"metric_name": "logS", "target_value": -1.72, "tolerance": 0.3}
    ],
    "metadata": {"ambiguity": False,
                 "confidence_scores": {"objective_confidence": 0.95}},
}

PLAN = {
    "selected_method": {"tool_name": "RDKit", "tool_version": 2024.3},
    "requested_property": "logS",
    "target_system": {"formula": "C9H8O4",
                      "molecule": {"name": "aspirin",
                                   "SMILES": "CC(=O)Oc1ccccc1C(=O)O"}},
    "acceptance_metrics": [
        {"metric_name": "logS", "target_value": -1.72, "tolerance": 0.3}
    ],
}


def _validator(schema_name):
    with open(SCHEMA_DIR / schema_name) as f:
        return Draft202012Validator(json.load(f))


@pytest.fixture
def machine(tmp_path):
    """A StateMachine built offline (no eager AgentInterface) writing to tmp_path."""
    with patch.object(DataStorage, "load", return_value=None):
        m = SM.StateMachine(data_path=str(tmp_path / "state.json"), run_id="testrun")
    m.artifacts_dir = tmp_path
    return m


def _seed(machine, tmp_path, name, payload):
    path = tmp_path / f"{name}_seed.json"
    path.write_text(json.dumps(payload))
    machine.context.artifacts[name] = str(path)
    return path


def _seed_planning(machine, tmp_path, plan=PLAN):
    _seed(machine, tmp_path, "intent_spec", INTENT)
    _seed(machine, tmp_path, "execution_plan", plan)


def _seed_execution(machine, tmp_path, *, stdout="", succeeded=True,
                    artifacts_dir=None):
    _seed(machine, tmp_path, "execution_result", {
        "status": "success" if succeeded else "skipped_missing_dependency",
        "succeeded": succeeded,
        "stdout": stdout,
        "artifacts_dir": artifacts_dir,
    })


# -- interpret ----------------------------------------------------------------

def test_interpret_noops_without_execution_result(machine):
    assert machine.interpret() == State.VALIDATE
    assert "normalized_result" not in machine.context.artifacts


def test_interpret_noops_when_nothing_ran(machine, tmp_path):
    """Deferred/skipped runs carry succeeded=False; nothing to parse."""
    _seed_execution(machine, tmp_path, succeeded=False)
    assert machine.interpret() == State.VALIDATE
    assert "normalized_result" not in machine.context.artifacts


def test_interpret_parses_stdout_json_summary(machine, tmp_path):
    _seed_planning(machine, tmp_path)
    _seed_execution(machine, tmp_path,
                    stdout='starting run\n{"logS": -1.70, "n_molecules": 1}\n')
    assert machine.interpret() == State.VALIDATE

    normalized = machine._load_artifact("normalized_result")
    assert normalized is not None
    # the plan's requested_property picks logS as primary, not n_molecules
    assert normalized["primary_metric"]["name"] == "logS"
    assert normalized["primary_metric"]["value"] == pytest.approx(-1.70)


def test_interpret_parses_csv_output_file(machine, tmp_path):
    outdir = tmp_path / "exec_testrun"
    outdir.mkdir()
    (outdir / "results.csv").write_text("logS\n-1.90\n-1.74\n-1.71\n-1.70\n")
    _seed_planning(machine, tmp_path)
    _seed_execution(machine, tmp_path, stdout="done",
                    artifacts_dir=str(outdir))
    machine.interpret()

    normalized = machine._load_artifact("normalized_result")
    assert normalized["primary_metric"]["name"] == "logS"
    assert normalized["primary_metric"]["uncertainty"] > 0


def test_interpret_falls_back_to_log_parser(machine, tmp_path):
    _seed_planning(machine, tmp_path)
    _seed_execution(machine, tmp_path, stdout="logS=-1.70\nruntime=3.2\n")
    machine.interpret()

    normalized = machine._load_artifact("normalized_result")
    assert normalized["primary_metric"]["name"] == "logS"


def test_interpret_unparseable_output_without_a_requested_metric_advances(machine, tmp_path):
    """With no metric requested anywhere, an output with no numbers is
    delivered as-is (there is nothing to hold the run to)."""
    plan = {k: v for k, v in PLAN.items()
            if k not in ("requested_property", "acceptance_metrics")}
    intent = {k: v for k, v in INTENT.items() if k != "acceptance_metrics"}
    _seed(machine, tmp_path, "intent_spec", intent)
    _seed(machine, tmp_path, "execution_plan", plan)
    _seed_execution(machine, tmp_path, stdout="all done, no numbers here")
    assert machine.interpret() == State.VALIDATE
    assert "normalized_result" not in machine.context.artifacts


def test_interpret_fails_loudly_when_requested_metric_never_appears(machine, tmp_path):
    """A run that exits 0 without ever printing the requested quantity is a
    hollow success -- delivering it hands the researcher nothing. interpret()
    must fail with the reason instead of quietly skipping validation."""
    _seed_planning(machine, tmp_path)
    _seed_execution(machine, tmp_path, stdout="all done, no numbers here")
    with pytest.raises(Exception, match="no finite value for 'logS'"):
        machine.interpret()
    assert "normalized_result" not in machine.context.artifacts


def test_interpret_refuses_bookkeeping_when_requested_metric_is_absent(machine, tmp_path):
    """Regression: the generic-scaffold run.

    The stub's output parses fine but carries only bookkeeping (n_criteria,
    input_present) -- nothing matching the requested logS. Normalizing it
    anyway made n_criteria the "primary metric" and validate() graded it;
    the honest outcome is failing the run, not grading a fake metric.
    """
    outdir = tmp_path / "exec_testrun"
    outdir.mkdir()
    (outdir / "results.csv").write_text(
        "tool,model,n_criteria,input_present\nOpenMM,generic_run,1,False\n")
    _seed_planning(machine, tmp_path)
    _seed_execution(machine, tmp_path,
                    stdout='{"tool": "OpenMM", "n_criteria": 1}',
                    artifacts_dir=str(outdir))
    with pytest.raises(Exception, match="no finite value"):
        machine.interpret()
    assert "normalized_result" not in machine.context.artifacts


def test_interpret_treats_nan_metric_as_no_result(machine, tmp_path):
    """Regression: the aspirin solvation run printed NaN for every field.

    The output names the requested metric, but NaN is not a result -- the
    field must not count as a match, and the hollow success must fail."""
    _seed_execution(machine, tmp_path,
                    stdout='{"logS": NaN, "dG_solv": NaN, "n_molecules": 1}')
    _seed_planning(machine, tmp_path)
    with pytest.raises(Exception, match="no finite value for 'logS'"):
        machine.interpret()
    assert "normalized_result" not in machine.context.artifacts


def test_interpret_ignores_nan_noise_next_to_a_finite_metric(machine, tmp_path):
    """A NaN diagnostic beside a finite requested metric must not spoil the
    interpretation -- only the non-finite field is discarded."""
    _seed_planning(machine, tmp_path)
    _seed_execution(machine, tmp_path,
                    stdout='{"logS": -1.70, "convergence_stdev": NaN}')
    assert machine.interpret() == State.VALIDATE
    normalized = machine._load_artifact("normalized_result")
    assert normalized["primary_metric"]["name"] == "logS"


def test_interpret_reads_pretty_printed_stdout_json(machine, tmp_path):
    """A script that pretty-prints its summary (indent=2) spans lines; the
    single-line scan misses it, so the block fallback must pick it up."""
    _seed_planning(machine, tmp_path)
    _seed_execution(machine, tmp_path,
                    stdout='starting\n{\n  "logS": -1.70,\n  "n_molecules": 1\n}\n')
    assert machine.interpret() == State.VALIDATE
    normalized = machine._load_artifact("normalized_result")
    assert normalized["primary_metric"]["name"] == "logS"
    assert normalized["primary_metric"]["value"] == pytest.approx(-1.70)


# -- validate: seeded/no-result behavior --------------------------------------

def test_validate_defaults_to_accept_without_interpretation(machine):
    assert machine.validate() == State.ACCEPT


def test_validate_routes_on_seeded_verdict(machine):
    machine.context.validation_result = "rejected"
    assert machine.validate() == State.REPLAN
    machine.context.validation_result = "needs_review"
    assert machine.validate() == State.CORRECT


# -- validate: cross-validation against baselines ------------------------------

def _seed_normalized(machine, tmp_path, value, name="logS"):
    _seed(machine, tmp_path, "normalized_result", {
        "primary_metric": {"name": name, "value": value, "uncertainty": 0.05,
                           "uncertainty_method": "reported", "unit": None},
        "secondary_metrics": [],
        "metadata": {},
    })


def test_validate_accepts_close_agreement(machine, tmp_path):
    """aspirin logS literature -1.72; -1.70 is ~1% relative error."""
    _seed_planning(machine, tmp_path)
    _seed_normalized(machine, tmp_path, -1.70)
    assert machine.validate() == State.ACCEPT
    assert machine.context.validation_result == "accepted"

    report = machine._load_artifact("validation_report")
    _validator("validation_report.schema.json").validate(report)
    assert report["acceptance_status"] == "accepted"
    assert report["cross_validation"]["comparisons"][0]["molecule"] == "aspirin"


def test_validate_marginal_agreement_routes_to_correct(machine, tmp_path):
    """-2.05 vs -1.72 is ~19% relative error: needs_review -> CORRECT."""
    _seed_planning(machine, tmp_path)
    _seed_normalized(machine, tmp_path, -2.05)
    assert machine.validate() == State.CORRECT
    assert machine.context.validation_result == "needs_review"

    report = machine._load_artifact("validation_report")
    assert report["acceptance_status"] == "needs_review"
    assert report["rerun"]["decision"] == "rerun"


def test_validate_poor_agreement_routes_to_replan(machine, tmp_path):
    """-3.0 vs -1.72 is ~74% relative error: rejected -> REPLAN."""
    _seed_planning(machine, tmp_path)
    _seed_normalized(machine, tmp_path, -3.0)
    assert machine.validate() == State.REPLAN
    assert machine.context.validation_result == "rejected"


def test_validate_rerun_loop_is_bounded(machine, tmp_path):
    """A second identical marginal result converges: deliver flagged, not loop."""
    _seed_planning(machine, tmp_path)
    _seed_normalized(machine, tmp_path, -2.05)
    assert machine.validate() == State.CORRECT  # round 1: try a correction

    _seed_normalized(machine, tmp_path, -2.05)  # rerun didn't move the needle
    assert machine.validate() == State.ACCEPT
    assert machine.context.validation_result == "accepted"

    report = machine._load_artifact("validation_report")
    assert report["acceptance_status"] == "needs_review"  # true verdict on record
    assert report["rerun"]["decision"] == "stop"
    assert report["rerun"]["stop_reason"] == "converged"
    assert report["rerun"]["disposition"] == "delivered_for_researcher_review"


def test_validate_rerun_loop_stops_at_the_iteration_cap(machine, tmp_path):
    """A run that keeps improving but never gets good enough must still stop.

    The convergence check can't catch this one -- each pass genuinely improves
    by more than the 5% floor -- so the iteration cap is the only thing standing
    between the researcher and an endless correct/replan loop.
    """
    _seed_planning(machine, tmp_path)
    cap = machine._rerun.policy.max_iterations
    # ~10% closer each pass, but never within 30% of the -1.72 baseline.
    for value in (-10.0, -9.0, -8.1, -7.29, -6.56):
        _seed_normalized(machine, tmp_path, value)
        assert machine.validate() == State.REPLAN
        assert machine._load_artifact("validation_report")["rerun"]["decision"] == "rerun"
    assert machine._rerun.iteration == cap

    _seed_normalized(machine, tmp_path, -5.90)
    assert machine.validate() == State.ACCEPT  # delivered, not looped

    report = machine._load_artifact("validation_report")
    assert report["acceptance_status"] == "rejected"  # true verdict on record
    assert report["rerun"]["stop_reason"] == "iteration_cap"
    assert report["rerun"]["disposition"] == "delivered_for_researcher_review"


def test_validate_metric_alias_matches_baseline(machine, tmp_path):
    """A metric named 'solubility' still matches the logS baseline row."""
    _seed_planning(machine, tmp_path)
    _seed_normalized(machine, tmp_path, -1.70, name="solubility")
    assert machine.validate() == State.ACCEPT
    report = machine._load_artifact("validation_report")
    assert report["cross_validation"]["comparisons"]


# -- validate: acceptance-criteria fallback (no baseline) ----------------------

def _plan_for(molecule, criteria):
    plan = dict(PLAN)
    plan["target_system"] = {"formula": None, "molecule": {"name": molecule}}
    plan["acceptance_metrics"] = criteria
    return plan


def test_validate_unbaselined_molecule_uses_acceptance_criteria(machine, tmp_path):
    plan = _plan_for("unobtainium-oxide", [
        {"metric_name": "logS", "target_value": -2.0, "tolerance": 0.5}])
    _seed(machine, tmp_path, "execution_plan", plan)
    _seed_normalized(machine, tmp_path, -2.2)  # within +/- 0.5 of target
    assert machine.validate() == State.ACCEPT

    report = machine._load_artifact("validation_report")
    assert report["acceptance_status"] == "accepted"
    assert "acceptance criteria" in report["rationale"]


def test_validate_unbaselined_molecule_far_from_target_is_rejected(machine, tmp_path):
    plan = _plan_for("unobtainium-oxide", [
        {"metric_name": "logS", "target_value": -2.0, "tolerance": 0.1}])
    _seed(machine, tmp_path, "execution_plan", plan)
    _seed_normalized(machine, tmp_path, -4.0)  # miss of 2.0 >> 2 * tolerance
    assert machine.validate() == State.REPLAN
    assert machine.context.validation_result == "rejected"


def test_validate_no_reference_at_all_delivers_as_is(machine, tmp_path):
    plan = _plan_for("unobtainium-oxide", [])
    _seed(machine, tmp_path, "execution_plan", plan)
    _seed_normalized(machine, tmp_path, -2.2)
    assert machine.validate() == State.ACCEPT

    report = machine._load_artifact("validation_report")
    assert report["acceptance_status"] == "accepted"
    assert "without external validation" in report["rationale"]


# -- correct -------------------------------------------------------------------

def _seed_validation_report(machine, tmp_path, gap=0.19):
    _seed(machine, tmp_path, "validation_report", {
        "comparison": {"literature_results": "aspirin/logS=-1.72",
                       "agreement": 0.81,
                       "difference_analysis": "predicted -2.05 vs -1.72"},
        "acceptance_status": "needs_review",
        "metadata": {"ID": "val-testrun", "timestamp": "2026-07-13T00:00:00Z"},
        "cross_validation": {"mean_relative_error": gap, "comparisons": []},
    })


def test_correct_writes_schema_valid_correction_plan(machine, tmp_path):
    _seed_planning(machine, tmp_path)
    _seed_validation_report(machine, tmp_path)
    _seed(machine, tmp_path, "discovery", {"candidates": [
        {"rank": 1, "id": "rdkit", "name": "RDKit"},
        {"rank": 2, "id": "pymatgen", "name": "Pymatgen"},
    ]})

    assert machine.correct() == State.BUILD

    plan = machine._load_artifact("correction_plan")
    assert plan is not None
    plan_for_schema = {k: v for k, v in plan.items() if k != "diagnosis_detail"}
    _validator("correction_plan.schema.json").validate(plan_for_schema)
    # with no stronger signal, the plan proposes the next-ranked tool
    assert plan["proposed_corrections"][0]["modification_type"] == "switch_model"
    assert plan["proposed_corrections"][0]["new_value"] == "Pymatgen"
    assert plan["metadata"]["validation_report_id"] == "val-testrun"


def test_correct_without_alternative_candidate_reruns_unchanged(machine, tmp_path):
    _seed_planning(machine, tmp_path)
    _seed_validation_report(machine, tmp_path)
    _seed(machine, tmp_path, "discovery", {"candidates": [
        {"rank": 1, "id": "rdkit", "name": "RDKit"},
    ]})

    assert machine.correct() == State.BUILD
    plan = machine._load_artifact("correction_plan")
    plan_for_schema = {k: v for k, v in plan.items() if k != "diagnosis_detail"}
    _validator("correction_plan.schema.json").validate(plan_for_schema)
    assert plan["proposed_corrections"][0]["modification_type"] == "relax_constraints"


def test_correct_noops_gracefully_without_report(machine):
    """Even with nothing seeded, correct() records a plan and returns BUILD."""
    assert machine.correct() == State.BUILD
    assert machine._load_artifact("correction_plan") is not None
