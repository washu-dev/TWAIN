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
    """A StateMachine built offline (no eager AgentInterface) writing to tmp_path.

    ``ask`` answers "rerun" at the accept-or-rerun gate, so the tests below
    exercise the automated loop -- the path taken when the researcher chooses to
    keep going. The gate itself is covered by TestAcceptOrLoopGate, which scripts
    its own answers. Questions are recorded on ``machine.asked``.
    """
    with patch.object(DataStorage, "load", return_value=None):
        m = SM.StateMachine(data_path=str(tmp_path / "state.json"), run_id="testrun")
    m.artifacts_dir = tmp_path
    m.asked = []

    def ask(question):
        m.asked.append(question)
        return "rerun"

    m.ask = ask
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
    assert report["verified"] is True              # a literature value checked it


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


def test_an_unspecified_target_is_not_judged_against_zero(machine, tmp_path):
    """A metric with no number to hit must be delivered, not measured from 0.

    This is what makes a null target safe to represent at all (session 79761036:
    a CaPt2 bulk modulus with no researcher-supplied target). The criterion is
    skipped rather than coerced -- a 0.0 default would make a correct 158 GPa look
    like a 158-unit miss and reject it.
    """
    plan = dict(PLAN, acceptance_metrics=[
        {"metric_name": "bulk_modulus", "target_value": None, "tolerance": None}])
    _seed(machine, tmp_path, "execution_plan", plan)

    status, rationale, gap = machine._acceptance_fallback(
        {"primary_metric": {"name": "bulk_modulus", "value": 158.0},
         "secondary_metrics": []})

    assert status == "accepted"
    assert "without external validation" in rationale
    assert "vs target" not in rationale  # nothing was judged, so nothing invented
    assert gap is None


# -- validate: Materials Project as a live baseline source ---------------------

CRYSTAL_PLAN = dict(
    PLAN,
    requested_property="bulk_modulus",
    target_system={"formula": "CaPt2",
                   "crystal": {"formula": "CaPt2", "name": "Calcium diplatinide",
                               "crystal_system": "cubic", "space_group": "Fd-3m",
                               "space_group_number": 227}},
    acceptance_metrics=[{"metric_name": "bulk_modulus",
                         "target_value": None, "tolerance": None}],
)

# The real mp-842 entry (C15 CaPt2), from the live API on 2026-08-05.
MP_DOC = {
    "material_id": "mp-842", "formula_pretty": "CaPt2",
    "symmetry": {"number": 227}, "energy_above_hull": 0.0, "theoretical": False,
    "bulk_modulus": {"voigt": 132.905, "reuss": 132.905, "vrh": 132.905},
    "band_gap": 0.0, "density": 15.2,
}


def _fake_mp(monkeypatch, docs=(MP_DOC,), *, key="test-key"):
    """Point the state machine's MP source at a canned doc. Never touches a network."""
    import cross_validation.mp_reference as mpref

    class _Rester:
        """Shaped like mp-api's SummaryRester: a context manager with .search()."""

        calls = []

        def search(self, **kwargs):
            type(self).calls = self.calls + [kwargs]
            return list(docs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def factory(*args, **kwargs):
        return mpref.MaterialsProjectBaselines(
            formula=kwargs.get("formula"), mp_id=kwargs.get("mp_id"),
            space_group_number=kwargs.get("space_group_number"),
            api_key=key, client_factory=lambda _k: _Rester())

    monkeypatch.setattr(SM, "MaterialsProjectBaselines", factory)
    return _Rester


def test_a_crystal_run_is_validated_against_materials_project(
        machine, tmp_path, monkeypatch):
    """The gap this closes: a bulk modulus with no target got no check at all.

    Run e496cf22 computed 131.07 GPa for CaPt2 and VALIDATE could only report it
    "delivered without external validation" -- the researcher had asked for it to
    be validated against the Materials Project reference value.
    """
    _fake_mp(monkeypatch)
    _seed(machine, tmp_path, "intent_spec", INTENT)
    _seed(machine, tmp_path, "execution_plan", CRYSTAL_PLAN)
    _seed_normalized(machine, tmp_path, 131.07, name="bulk_modulus")

    machine.validate()
    report = machine._load_artifact("validation_report")
    _validator("validation_report.schema.json").validate(report)

    comparisons = report["cross_validation"]["comparisons"]
    assert len(comparisons) == 1, report["cross_validation"]["unmatched"]
    assert comparisons[0]["property"] == "bulk_modulus"
    assert comparisons[0]["literature"] == 132.905
    assert "mp-842" in comparisons[0]["literature_source"]
    assert "without external validation" not in report["rationale"]


def test_a_molecular_run_is_never_graded_against_materials_project(
        machine, tmp_path, monkeypatch):
    """MP holds a solid CO2 entry; a gas-phase enthalpy must not meet it.

    The gate is the plan's own description of the system, not the formula: a
    molecule has no space group, so no MP request is made at all.
    """
    summary = _fake_mp(monkeypatch)
    summary.calls = []
    _seed_planning(machine, tmp_path)          # aspirin, molecular
    _seed_normalized(machine, tmp_path, -1.70)

    machine.validate()

    assert summary.calls == []                 # never queried
    report = machine._load_artifact("validation_report")
    assert report["cross_validation"]["comparisons"][0]["molecule"] == "aspirin"


def test_without_a_key_the_crystal_run_falls_back_cleanly(
        machine, tmp_path, monkeypatch):
    """No key on this host must degrade to the old behaviour, not to an error."""
    _fake_mp(monkeypatch, key=None)
    monkeypatch.delenv("MP_API_KEY", raising=False)
    _seed(machine, tmp_path, "intent_spec", INTENT)
    _seed(machine, tmp_path, "execution_plan", CRYSTAL_PLAN)
    _seed_normalized(machine, tmp_path, 131.07, name="bulk_modulus")

    machine.validate()
    report = machine._load_artifact("validation_report")

    assert report["cross_validation"]["comparisons"] == []
    assert "without external validation" in report["rationale"]


def test_a_failing_materials_project_does_not_fail_the_run(
        machine, tmp_path, monkeypatch):
    """The run already computed its answer; a reference lookup cannot cost it."""
    import cross_validation.mp_reference as mpref

    def exploding(*args, **kwargs):
        src = mpref.MaterialsProjectBaselines(
            formula="CaPt2", api_key="k",
            client_factory=lambda _k: (_ for _ in ()).throw(
                ConnectionError("api.materialsproject.org unreachable")))
        return src

    monkeypatch.setattr(SM, "MaterialsProjectBaselines", exploding)
    _seed(machine, tmp_path, "intent_spec", INTENT)
    _seed(machine, tmp_path, "execution_plan", CRYSTAL_PLAN)
    _seed_normalized(machine, tmp_path, 131.07, name="bulk_modulus")

    machine.validate()   # must not raise
    report = machine._load_artifact("validation_report")
    assert report["cross_validation"]["comparisons"] == []
    assert report["acceptance_status"]  # a verdict was still reached


def test_a_zero_placeholder_rejects_a_correct_answer(machine, tmp_path):
    """Why null has to be representable: 0.0 is not a harmless "unset".

    Session 1fed66e6 ran the same CaPt2 request one hour earlier and passed PLAN
    because its target was 0/0 -- valid numbers. It never reached VALIDATE (the
    researcher terminated it), but had it finished, a correct ~158 GPa would have
    been measured from zero, rejected, and sent to REPLAN to "fix" a right answer.
    A loud PLAN failure was the better of the two outcomes; neither is acceptable.
    """
    plan = dict(PLAN, acceptance_metrics=[
        {"metric_name": "bulk_modulus", "target_value": 0, "tolerance": 0}])
    _seed(machine, tmp_path, "execution_plan", plan)

    status, rationale, gap = machine._acceptance_fallback(
        {"primary_metric": {"name": "bulk_modulus", "value": 158.0},
         "secondary_metrics": []})

    assert status == "rejected"
    assert "vs target 0 +/- 0" in rationale
    assert gap == 158.0


def test_a_real_target_is_still_judged(machine, tmp_path):
    """The guard above must not have turned every criterion into a free pass."""
    plan = dict(PLAN, acceptance_metrics=[
        {"metric_name": "bulk_modulus", "target_value": 180.0, "tolerance": 1.0}])
    _seed(machine, tmp_path, "execution_plan", plan)

    status, rationale, _ = machine._acceptance_fallback(
        {"primary_metric": {"name": "bulk_modulus", "value": 158.0},
         "secondary_metrics": []})

    assert status == "rejected"
    assert "vs target 180" in rationale


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
    # "accepted" only because nothing objected: the summary must say "not verified".
    assert report["verified"] is False


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


# -- regressions found in review ----------------------------------------------

def test_interpret_reads_a_summary_that_is_the_whole_of_stdout(machine, tmp_path):
    """Regression: the pretty-printed summary starting at offset 0.

    The generic template does json.dump(summary, sys.stdout, indent=2) with
    nothing printed before it, so the object begins at index 0. Treating -1 as
    "no brace found" discarded exactly that case and failed a run that had
    computed the right answer.
    """
    _seed_planning(machine, tmp_path)
    _seed_execution(machine, tmp_path,
                    stdout='{\n  "logS": -1.70,\n  "n_molecules": 1\n}')
    assert machine.interpret() == State.VALIDATE
    normalized = machine._load_artifact("normalized_result")
    assert normalized["primary_metric"]["name"] == "logS"
    assert normalized["primary_metric"]["value"] == pytest.approx(-1.70)


def test_interpret_keeps_a_series_that_has_one_bad_row(machine, tmp_path):
    """Regression: one diverged row must not discard 199 good predictions."""
    outdir = tmp_path / "exec_testrun"
    outdir.mkdir()
    rows = "\n".join(["-1.60"] * 199)
    (outdir / "predictions.csv").write_text(f"logS\n{rows}\nnan\n")
    _seed_planning(machine, tmp_path)
    _seed_execution(machine, tmp_path, stdout="done", artifacts_dir=str(outdir))

    assert machine.interpret() == State.VALIDATE
    normalized = machine._load_artifact("normalized_result")
    assert normalized["primary_metric"]["value"] == pytest.approx(-1.60)


def test_written_artifacts_are_strict_json(machine, tmp_path):
    """Regression: a bare NaN in an artifact 500s the whole report endpoint.

    json.dump emits NaN by default; Starlette serializes with allow_nan=False,
    so one non-finite diagnostic took down the entire report page rather than
    degrading a single field.
    """
    path = machine._write_artifact("probe", {
        "finite": 1.5,
        "not_a_number": float("nan"),
        "infinite": float("inf"),
        "nested": {"values": [1.0, float("nan")]},
    })
    raw = Path(path).read_text()
    assert "NaN" not in raw and "Infinity" not in raw
    # json.loads is lenient about NaN; the strict parse is what the API does.
    reloaded = json.loads(raw, parse_constant=_reject_constant)
    assert reloaded["finite"] == 1.5
    assert reloaded["not_a_number"] is None
    assert reloaded["nested"]["values"] == [1.0, None]


def _reject_constant(name):
    raise AssertionError(f"artifact contains non-JSON constant {name!r}")


def test_interpret_clears_a_stale_result_when_the_rerun_did_not_run(machine, tmp_path):
    """Regression: a correction loop whose rerun was deferred re-graded the
    previous pass's numbers and reported them as this run's result."""
    _seed_planning(machine, tmp_path)
    _seed_execution(machine, tmp_path, stdout='{"logS": -1.70}')
    machine.interpret()
    assert "normalized_result" in machine.context.artifacts

    # second pass: the heavy-calc gate declined, so nothing actually ran
    _seed_execution(machine, tmp_path, succeeded=False)
    assert machine.interpret() == State.VALIDATE
    assert "normalized_result" not in machine.context.artifacts
    assert machine.validate() == State.ACCEPT  # nothing to grade, not a re-grade


def test_a_rejected_result_replans_without_revoking_the_approval(machine, tmp_path):
    """Regression: the approval card appeared twice on every rejected run.

    validate() used to withdraw the approval on its way to REPLAN, so the run
    re-planned, parked at BUILD and posted a second card -- even though the
    replan almost always lands on the same method. Whether the new plan needs
    approval is plan()'s call, once there is a new plan to compare.
    """
    _seed_planning(machine, tmp_path)
    machine.context.plan_approved = True
    _seed_normalized(machine, tmp_path, -3.0)

    assert machine.validate() == State.REPLAN
    assert machine.context.plan_approved is True


def test_replanning_the_same_method_keeps_the_approval(machine, tmp_path):
    """The researcher approved this method and these resources; re-deriving the
    identical plan does not need a second decision."""
    _seed_planning(machine, tmp_path)
    machine.approve_plan(True)
    assert machine.context.approved_plan is not None

    _seed_planning(machine, tmp_path)          # PLAN re-derives the same plan
    machine._revoke_approval_if_plan_changed()
    assert machine.context.plan_approved is True


def test_replanning_a_different_method_revokes_the_approval(machine, tmp_path):
    """A replan that switches tool is a plan the researcher never saw, so it
    must not be built and executed on the old approval."""
    _seed_planning(machine, tmp_path)
    machine.approve_plan(True)
    approved = machine.context.approved_plan

    switched = dict(PLAN, selected_method={"tool_name": "Pymatgen"})
    _seed(machine, tmp_path, "execution_plan", switched)
    machine._revoke_approval_if_plan_changed()
    assert machine.context.plan_approved is False
    assert machine.context.approved_plan is None
    assert approved != machine._plan_fingerprint()


def test_a_changed_resource_request_revokes_the_approval(machine, tmp_path):
    """The card shows the resources the run will consume, so those are part of
    what was agreed to."""
    _seed(machine, tmp_path, "intent_spec", INTENT)
    _seed(machine, tmp_path, "execution_plan",
          dict(PLAN, slurm_request={"ram": 16, "max_time": 0.5}))
    machine.approve_plan(True)

    _seed(machine, tmp_path, "execution_plan",
          dict(PLAN, slurm_request={"ram": 128, "max_time": 12}))
    machine._revoke_approval_if_plan_changed()
    assert machine.context.plan_approved is False


def test_a_seeded_approval_is_not_revoked(machine, tmp_path):
    """The runner seeds an approval for unattended runs, and older checkpoints
    carry no fingerprint; revoking those would strand the run at the gate."""
    _seed_planning(machine, tmp_path)
    machine.context.plan_approved = True       # seeded, never recorded
    machine.context.approved_plan = None

    machine._revoke_approval_if_plan_changed()
    assert machine.context.plan_approved is True
    # ... and this plan is adopted, so a LATER switch is still caught
    assert machine.context.approved_plan is not None
    _seed(machine, tmp_path, "execution_plan",
          dict(PLAN, selected_method={"tool_name": "Pymatgen"}))
    machine._revoke_approval_if_plan_changed()
    assert machine.context.plan_approved is False


def test_marginal_result_keeps_the_approval_for_the_correction_loop(machine, tmp_path):
    """CORRECT rebuilds the SAME approved plan, so it must not re-gate --
    CORRECT->BUILD is guarded on plan_approved and would deadlock."""
    _seed_planning(machine, tmp_path)
    machine.context.plan_approved = True
    _seed_normalized(machine, tmp_path, -2.05)

    assert machine.validate() == State.CORRECT
    assert machine.context.plan_approved is True


def test_correct_reports_an_undetermined_failure_mode(machine, tmp_path):
    """The four failure modes need signals this pipeline does not yet collect
    (rejected input, activation range, loss curve, OOD score); a marginal
    disagreement supports none of them.

    So UNKNOWN is the honest diagnosis and the generic next-candidate plan is
    the designed outcome -- NOT a bug to be papered over by synthesizing a
    z-score out of the run's numerical-precision uncertainty, which would make
    the diagnosis depend on how tightly the script converged rather than on the
    science. See StateMachine._run_evidence.
    """
    _seed_planning(machine, tmp_path)
    _seed(machine, tmp_path, "discovery", {"candidates": [
        {"rank": 1, "id": "rdkit", "name": "RDKit"},
        {"rank": 2, "id": "pymatgen", "name": "Pymatgen"},
    ]})
    _seed_normalized(machine, tmp_path, -2.05)
    machine.validate()

    assert machine.correct() == State.BUILD
    plan = machine._load_artifact("correction_plan")
    assert plan["diagnosis_detail"]["mode"] == "unknown"
    # the generic plan still proposes the best untried lever, and says why
    assert plan["proposed_corrections"][0]["new_value"] == "Pymatgen"
    assert plan["fallback_strategy"]
    plan_for_schema = {k: v for k, v in plan.items() if k != "diagnosis_detail"}
    _validator("correction_plan.schema.json").validate(plan_for_schema)


def test_correct_records_the_measured_gap_for_the_forecast(machine, tmp_path):
    """The gap must reach the CorrectionPlan even on the acceptance-criteria
    path, where cross_validation carries no mean_relative_error."""
    plan_no_baseline = _plan_for("unobtainium-oxide", [
        {"metric_name": "logS", "target_value": -2.0, "tolerance": 0.1}])
    _seed(machine, tmp_path, "execution_plan", plan_no_baseline)
    _seed_normalized(machine, tmp_path, -2.15)  # 1.5x tolerance -> needs_review

    assert machine.validate() == State.CORRECT
    report = machine._load_artifact("validation_report")
    assert report["gap_basis"] == "tolerance_multiples"
    assert report["gap"] == pytest.approx(1.5)


def test_the_rerun_budget_fits_inside_the_orchestrator_backstop():
    """The state machine must stop the loop gracefully BEFORE the orchestrator's
    runaway-loop backstop aborts the run -- otherwise the researcher loses the
    result and the rationale instead of receiving them flagged for review."""
    import inspect
    # The orchestrator lives in a digit-prefixed dir that can't be imported by
    # dotted name; put it on sys.path the way test_orchestrator.py does.
    sys.path.insert(0, str(REPO_ROOT / "modules" / "07_runtime_orchestrator"))
    import orchestrator  # noqa: E402

    params = inspect.signature(orchestrator.Orchestrator.__init__).parameters
    budget = SM.RerunController().policy.max_iterations
    assert params["max_replans"].default > budget
    assert params["max_corrections"].default > budget


def test_a_run_with_nothing_to_interpret_does_not_loop(machine, tmp_path):
    """Regression: clearing the stale result removed the only thing advancing
    the correction loop.

    _gate_rerun (which owns the iteration cap) runs only when there IS a result
    to cross-validate. A pass that produced nothing would otherwise route on the
    PREVIOUS verdict, straight back into CORRECT, forever -- until the
    orchestrator's backstop failed the run.
    """
    _seed_planning(machine, tmp_path)
    _seed_normalized(machine, tmp_path, -2.05)
    assert machine.validate() == State.CORRECT  # pass 1 asks for a correction

    # pass 2: the rebuilt run was deferred, so there is nothing new to grade
    _seed_execution(machine, tmp_path, succeeded=False)
    assert machine.interpret() == State.VALIDATE
    assert machine.validate() == State.ACCEPT
    assert machine.context.validation_result == "accepted"

    report = machine._load_artifact("validation_report")
    assert report["rerun"]["stop_reason"] == "no_new_result"
    assert report["acceptance_status"] == "needs_review"  # true verdict kept


def test_a_seeded_verdict_is_not_overridden_before_anything_is_graded(machine, tmp_path):
    """The runner seeds validation_result to satisfy the stub guards; a run that
    has graded nothing yet must route on that seed untouched."""
    machine.context.validation_result = "rejected"
    _seed_execution(machine, tmp_path, succeeded=False)

    assert machine.interpret() == State.VALIDATE
    assert machine.validate() == State.REPLAN
    assert machine.context.validation_result == "rejected"


def test_the_rerun_budget_survives_a_new_process(machine, tmp_path):
    """Regression: the runner drives a run in slices, and a rejected result now
    hands control back for re-approval, so the next pass is a NEW StateMachine.
    An in-memory-only counter would restart at zero every pass and the cap could
    never be reached."""
    _seed_planning(machine, tmp_path)
    _seed_normalized(machine, tmp_path, -10.0)
    machine.validate()
    carried = machine._load_artifact("validation_report")["rerun"]
    assert carried["iteration"] == 1
    assert carried["metric_history"]

    # a fresh machine for the next slice, pointed at the same artifacts
    with patch.object(DataStorage, "load", return_value=None):
        resumed = SM.StateMachine(data_path=str(tmp_path / "state2.json"),
                                  run_id="testrun")
    resumed.artifacts_dir = tmp_path
    resumed.ask = lambda question: "rerun"     # the researcher keeps going
    resumed.context.artifacts = dict(machine.context.artifacts)
    assert resumed._rerun.iteration == 0  # nothing counted in this process yet

    _seed_normalized(resumed, tmp_path, -9.0)
    resumed.validate()
    assert resumed._rerun.iteration == 2  # continued, not restarted


def test_stdout_json_prefers_the_last_object_and_backtracks(machine, tmp_path):
    """Regression: an earlier single-line object beat the real summary, and a
    trailing line that merely looked like JSON buried a valid one."""
    pretty = json.dumps({"logS": -1.70, "tool": "RDKit"}, indent=2)

    # a progress line before the summary must not win
    assert json.loads(SM.StateMachine._stdout_json(
        '{"logS": -9.99, "stage": "init"}\n' + pretty))["logS"] == -1.70
    # a Python dict repr after the summary must not bury it
    assert json.loads(SM.StateMachine._stdout_json(
        pretty + "\n{'converged': True}\n"))["logS"] == -1.70
    # a truncated trailing object must not bury it either
    assert json.loads(SM.StateMachine._stdout_json(
        pretty + "\n{ elapsed 3s\n"))["logS"] == -1.70
    # a top-level array is not a summary, however it is indented
    for indent in (None, 0, 2):
        assert SM.StateMachine._stdout_json(
            json.dumps([{"logS": -1.7}], indent=indent)) is None


# -- Epic 6 gaps: benchmark grading, applied corrections, thresholds -----------

_BENCHMARK_CSV = (
    "molecule,logS\n"
    "aspirin,-1.72\n"       # each agrees with its own literature value
    "caffeine,-0.87\n"
)

# Two molecules whose errors cancel: the mean is near aspirin's -1.72 while
# neither prediction is anywhere near its own reference.
_CANCELLING_CSV = (
    "molecule,logS\n"
    "aspirin,-4.72\n"
    "caffeine,1.28\n"
)


def _seed_csv(machine, tmp_path, text, name="results.csv"):
    outdir = tmp_path / "exec_testrun"
    outdir.mkdir(exist_ok=True)
    (outdir / name).write_text(text)
    _seed_execution(machine, tmp_path, stdout="done", artifacts_dir=str(outdir))
    return outdir


def test_benchmark_rows_are_graded_per_molecule(machine, tmp_path):
    """Each system must be compared against its OWN literature value, which is
    also what makes RMSE and a correlation computable."""
    _seed_planning(machine, tmp_path)
    _seed_csv(machine, tmp_path, _BENCHMARK_CSV)
    machine.interpret()

    normalized = machine._load_artifact("normalized_result")
    assert [r["entity"] for r in normalized["entities"]] == ["aspirin", "caffeine"]

    assert machine.validate() == State.ACCEPT
    cross = machine._load_artifact("validation_report")["cross_validation"]
    assert {c["molecule"] for c in cross["comparisons"]} == {"aspirin", "caffeine"}
    assert cross["rmse"] is not None      # undefined for a single point
    assert cross["pearson"] is not None


def test_a_benchmark_whose_errors_cancel_is_not_accepted(machine, tmp_path):
    """Regression: averaging the column hid two badly wrong predictions.

    -4.72 and 1.28 average to -1.72, exactly aspirin's literature value, so the
    aggregate scored ~0% relative error and the run was ACCEPTED while both
    molecules were wildly off.
    """
    _seed_planning(machine, tmp_path)
    _seed_csv(machine, tmp_path, _CANCELLING_CSV)
    machine.interpret()

    assert machine.validate() != State.ACCEPT
    report = machine._load_artifact("validation_report")
    assert report["acceptance_status"] == "rejected"


def test_an_unidentifiable_aggregate_is_not_graded(machine, tmp_path):
    """With no identifier column the rows cannot be matched to references, so
    the mean must not be graded against one molecule's literature value."""
    _seed_planning(machine, tmp_path)
    _seed_csv(machine, tmp_path, "logS\n-4.72\n1.28\n")
    machine.interpret()
    normalized = machine._load_artifact("normalized_result")
    assert "entities" not in normalized
    assert normalized["metadata"]["primary_samples"] == 2

    machine.validate()
    report = machine._load_artifact("validation_report")
    assert report["acceptance_status"] == "needs_review"
    assert "does not say which row belongs to which system" in report["rationale"]


def test_a_single_measurement_is_still_graded_normally(machine, tmp_path):
    """The aggregate guard must not fire on an ordinary one-system run."""
    _seed_planning(machine, tmp_path)
    _seed_normalized(machine, tmp_path, -1.70)
    assert machine.validate() == State.ACCEPT
    assert machine._load_artifact("validation_report")["acceptance_status"] == "accepted"


def test_the_researchers_tolerance_survives_a_matching_baseline(machine, tmp_path):
    """A result can agree with the literature yet miss the tolerance the
    researcher asked for; the stricter of the two verdicts must win."""
    strict = dict(PLAN, acceptance_metrics=[
        {"metric_name": "logS", "target_value": -1.72, "tolerance": 0.001}])
    _seed(machine, tmp_path, "intent_spec", INTENT)
    _seed(machine, tmp_path, "execution_plan", strict)
    _seed_normalized(machine, tmp_path, -1.80)  # ~4.7% off: baseline says accept

    assert machine.validate() != State.ACCEPT
    report = machine._load_artifact("validation_report")
    assert report["acceptance_status"] == "rejected"
    assert "stricter" in report["rationale"]


def test_acceptance_thresholds_are_configurable(machine, tmp_path, monkeypatch):
    """Story 6.2 requires the thresholds be adjustable, not hardcoded 15/30."""
    _seed_planning(machine, tmp_path)
    _seed_normalized(machine, tmp_path, -1.80)  # ~4.7% off: accepted by default

    monkeypatch.setenv("TWAIN_ACCEPT_BELOW", "0.01")   # 1% -- much stricter
    monkeypatch.setenv("TWAIN_REVIEW_BELOW", "0.02")
    machine.validate()
    report = machine._load_artifact("validation_report")
    assert report["thresholds"]["accept_below"] == 0.01
    # logS is graded in log units (0.08 off, inside 0.5), not by relative error.
    assert report["acceptance_status"] == "accepted"
    # ... and its band is configurable per run too.
    plan = machine._load_artifact("execution_plan")
    plan["acceptance_thresholds"] = {"absolute": {"logS": [0.02, 0.05]}}
    _seed(machine, tmp_path, "execution_plan", plan)
    machine.validate()
    assert machine._load_artifact("validation_report")["acceptance_status"] == "rejected"


def test_incoherent_thresholds_fall_back_to_the_defaults(machine, tmp_path, monkeypatch):
    monkeypatch.setenv("TWAIN_ACCEPT_BELOW", "0.9")   # accept > review: invalid
    monkeypatch.setenv("TWAIN_REVIEW_BELOW", "0.1")
    _seed_planning(machine, tmp_path)
    _seed_normalized(machine, tmp_path, -1.70)
    machine.validate()
    assert machine._load_artifact("validation_report")["thresholds"] == {
        "accept_below": 0.15, "review_below": 0.30, "absolute": {"logS": [0.5, 1.0]}}


def test_correct_never_rewrites_the_selected_method(machine, tmp_path):
    """Regression: a live run died in EXECUTE because CORRECT re-selected the tool.

    An aqueous-solubility run planned around the xtb calculator was corrected to
    the next-ranked discovery candidate (Pymatgen) by editing
    selected_method.tool_name. That silently re-templated the bundle as a crystal
    structure analysis, which cannot compute a solubility -- the smoke test died
    with "no structure to analyse". Method selection is PLAN's job: it grounds
    the choice against the calculator registry and picks a matching template, so
    CORRECT must record the proposal, not act on it.
    """
    calculator_plan = dict(PLAN, selected_method={
        "tool_name": "xtb", "calculator": "xtb",
        "calculator_import": "xtb", "libraries": ["xtb", "RDKit", "ASE"]})
    _seed(machine, tmp_path, "intent_spec", INTENT)
    _seed(machine, tmp_path, "execution_plan", calculator_plan)
    _seed_validation_report(machine, tmp_path)
    _seed(machine, tmp_path, "discovery", {"candidates": [
        {"rank": 1, "id": "xtb", "name": "xtb"},
        {"rank": 2, "id": "pymatgen", "name": "Pymatgen"},
    ]})
    machine.context.plan_approved = True
    machine.context.artifacts["run_bundle"] = "/built/bundle"

    assert machine.correct() == State.BUILD
    # the plan the researcher approved is untouched, in every part
    assert machine._load_artifact("execution_plan")["selected_method"] == \
        calculator_plan["selected_method"]
    assert machine.context.artifacts["run_bundle"] == "/built/bundle"
    assert machine.context.plan_approved is True   # CORRECT->BUILD is guarded on it

    # the proposal is still recorded, for the researcher and the audit trail
    plan = machine._load_artifact("correction_plan")
    assert plan["proposed_corrections"][0]["modification_type"] == "switch_model"


def test_validate_retires_the_previous_correction_plan(machine, tmp_path):
    """VALIDATE owns correction_plan (see _STAGE_OUTPUTS): a run that ends up
    accepted must not still ship a diagnosis of what supposedly went wrong."""
    _seed_planning(machine, tmp_path)
    _seed(machine, tmp_path, "correction_plan", {"diagnosis": "from the last pass"})
    _seed_normalized(machine, tmp_path, -1.70)   # this pass agrees with literature

    assert machine.validate() == State.ACCEPT
    assert "correction_plan" not in machine.context.artifacts


def test_an_unreadable_previous_report_is_not_replaced_by_a_stub(machine, tmp_path):
    """_stop_unproductive_loop amends the previous report. If that report cannot
    be read, writing anyway would REPLACE it with a rerun-only stub carrying no
    comparison and no rationale."""
    _seed_planning(machine, tmp_path)
    missing = tmp_path / "gone_validation_report.json"
    machine.context.artifacts["validation_report"] = str(missing)
    machine.context.validation_result = "needs_review"
    _seed_execution(machine, tmp_path, succeeded=False)

    machine.interpret()
    assert machine.validate() == State.ACCEPT
    assert not missing.exists()      # no stub written over the real thing


def test_the_plan_metric_name_matches_the_baseline_property(machine, tmp_path):
    """Regression: aspirin's own literature value was in configs/baselines.json
    and the run missed it, because the plan names the metric after the property
    AND its unit ('aqueous_solubility_logS') while the DB keys on 'logS'."""
    plan = dict(PLAN, requested_property="aqueous_solubility_logS",
                acceptance_metrics=[{"metric_name": "aqueous_solubility_logS",
                                     "target_value": -1.72, "tolerance": 0.5}])
    _seed(machine, tmp_path, "intent_spec", INTENT)
    _seed(machine, tmp_path, "execution_plan", plan)
    _seed_normalized(machine, tmp_path, -1.70, name="aqueous_solubility_logS")

    assert machine.validate() == State.ACCEPT
    report = machine._load_artifact("validation_report")
    # graded against the literature baseline, not the acceptance-criteria fallback
    assert report["gap_basis"] == "relative_error"
    assert report["cross_validation"]["comparisons"][0]["literature"] == -1.72


# -- the heavy-calculation confirmation is asked once per run ------------------

class _FakeHeavyCalculator:
    name = "GPAW"
    heavy = True

    def needs_docker(self, _platform):
        return False


def _heavy_machine(machine, answers):
    """Wire the machine up as a heavy-calculator run with a scripted researcher."""
    machine._selected_calculator = lambda: _FakeHeavyCalculator()
    asked = []

    def ask(question):
        asked.append(question)
        return answers.pop(0) if answers else "no"

    machine.ask = ask
    return asked


def test_the_heavy_run_is_confirmed_once_per_run(machine, tmp_path):
    """Regression: session f7a51a7e was asked to confirm the same GPAW run twice.

    Epic 6 made VALIDATE -> REPLAN -> PLAN -> BUILD -> REPAIR -> EXECUTE
    reachable, and every EXECUTE entry re-asked. The researcher had already
    agreed to spend that compute and their answer had not changed.
    """
    asked = _heavy_machine(machine, ["yes"])

    assert machine._confirm_heavy_execution() is True     # first EXECUTE asks
    assert len(asked) == 1
    assert machine.context.heavy_confirmed == "GPAW"

    assert machine._confirm_heavy_execution() is True     # the replan's EXECUTE
    assert len(asked) == 1, "asked the researcher a second time"


def test_a_declined_heavy_run_is_asked_again(machine, tmp_path):
    """A 'no' is not a standing decision: it defers this run, and a later pass
    must be free to ask again rather than inheriting the refusal."""
    asked = _heavy_machine(machine, ["no", "yes"])

    assert machine._confirm_heavy_execution() is False
    assert machine.context.heavy_confirmed is None
    assert machine._confirm_heavy_execution() is True
    assert len(asked) == 2


def test_a_different_heavy_engine_is_confirmed_separately(machine, tmp_path):
    """The confirmation covers the engine that was named, so a re-plan landing
    on a different heavy calculator has to ask about that one."""
    asked = _heavy_machine(machine, ["yes", "yes"])
    machine._confirm_heavy_execution()

    class _Other(_FakeHeavyCalculator):
        name = "Quantum ESPRESSO"

    machine._selected_calculator = lambda: _Other()
    assert machine._confirm_heavy_execution() is True
    assert len(asked) == 2
    assert machine.context.heavy_confirmed == "Quantum ESPRESSO"


def test_rewinding_to_execute_asks_again(machine, tmp_path):
    """An explicit re-run from EXECUTE is a fresh decision to spend the compute,
    so the standing confirmation must not carry into it."""
    _heavy_machine(machine, ["yes"])
    machine._confirm_heavy_execution()
    assert machine.context.heavy_confirmed == "GPAW"

    machine.context.artifacts["intent_spec"] = "/x/intent.json"
    machine.rewind_to(State.EXECUTE)
    assert machine.context.heavy_confirmed is None


# -- the accept-or-rerun gate --------------------------------------------------

class TestAcceptOrLoopGate:
    """A failing verdict does not mean the run is wrong, so the researcher
    decides whether another calculation is worth spending."""

    def _answer(self, machine, reply):
        asked = []

        def ask(question):
            asked.append(question)
            return reply

        machine.ask = ask
        return asked

    def test_accepting_delivers_the_result_and_stops(self, machine, tmp_path):
        _seed_planning(machine, tmp_path)
        _seed_normalized(machine, tmp_path, -3.0)          # would be rejected
        asked = self._answer(machine, "accept")

        assert machine.validate() == State.ACCEPT
        assert len(asked) == 1
        report = machine._load_artifact("validation_report")
        assert report["acceptance_status"] == "rejected"    # true verdict kept
        assert report["rerun"]["stop_reason"] == "researcher_accepted"
        assert report["rerun"]["disposition"] == "accepted_by_researcher"
        assert machine._rerun.iteration == 0               # no calculation spent

    def test_choosing_rerun_enters_the_bounded_loop(self, machine, tmp_path):
        _seed_planning(machine, tmp_path)
        _seed_normalized(machine, tmp_path, -3.0)
        self._answer(machine, "rerun")

        assert machine.validate() == State.REPLAN
        assert machine._rerun.iteration == 1

    def test_an_accepted_result_is_never_questioned(self, machine, tmp_path):
        """Nothing to decide when the result already agrees with the reference."""
        _seed_planning(machine, tmp_path)
        _seed_normalized(machine, tmp_path, -1.70)
        asked = self._answer(machine, "accept")

        assert machine.validate() == State.ACCEPT
        assert asked == []

    def test_the_question_says_what_the_result_was_compared_against(self, machine, tmp_path):
        """The researcher cannot judge a verdict without knowing whether the
        target is a measurement or a value the plan proposed."""
        plan = _plan_for("unobtainium-oxide", [
            {"metric_name": "logS", "target_value": -2.0, "tolerance": 0.1}])
        _seed(machine, tmp_path, "execution_plan", plan)
        _seed_normalized(machine, tmp_path, -4.0)
        asked = self._answer(machine, "accept")

        machine.validate()
        assert "not taken from a measurement" in asked[0]

    def test_the_silicon_bandgap_case_is_offered_for_acceptance(self, machine, tmp_path):
        """The run that motivated this gate: PBE puts silicon's gap near 0.6 eV
        against an experimental ~1.17, so the verdict fails forever and the loop
        can never fix it -- only a human can say the run is as good as PBE gets.
        """
        plan = {"selected_method": {"tool_name": "GPAW", "calculator": "GPAW"},
                "requested_property": "band_gap",
                "target_system": {"crystal": {"name": "silicon", "formula": "Si"}},
                "acceptance_metrics": [{"metric_name": "band_gap",
                                        "target_value": 1.17, "tolerance": 0.1}]}
        _seed(machine, tmp_path, "execution_plan", plan)
        _seed(machine, tmp_path, "normalized_result", {
            "primary_metric": {"name": "band_gap", "value": 0.570,
                               "uncertainty": 0.057,
                               "uncertainty_method": "fallback", "unit": "eV"},
            "secondary_metrics": [], "metadata": {"primary_samples": 1}})
        asked = self._answer(machine, "accept")

        assert machine.validate() == State.ACCEPT
        assert "0.57" in asked[0] and "GPAW" in asked[0]
        report = machine._load_artifact("validation_report")
        assert report["rerun"]["stop_reason"] == "researcher_accepted"

    def test_unattended_runs_keep_looping_without_asking(self, machine, tmp_path):
        _seed_planning(machine, tmp_path)
        _seed_normalized(machine, tmp_path, -3.0)
        machine.auto_approve = True
        asked = self._answer(machine, "accept")

        assert machine.validate() == State.REPLAN
        assert asked == []

    def test_with_no_way_to_ask_the_result_is_accepted_not_rerun(self, machine, tmp_path):
        """Spending an unattended DFT calculation on an unreviewed guess is the
        worse default, so a headless run delivers instead of looping."""
        _seed_planning(machine, tmp_path)
        _seed_normalized(machine, tmp_path, -3.0)
        machine.ask = None

        with patch.object(SM.sys, "stdin", None):
            assert machine.validate() == State.ACCEPT
        assert machine._rerun.iteration == 0

    @pytest.mark.parametrize("reply,expect_rerun", [
        ("rerun", True), ("re-run", True), ("retry", True), ("  RERUN  ", True),
        ("accept", False), ("a", False), ("", False),
        # "yes" at a two-named-choice question most likely means "yes, accept";
        # reading it as a rerun would spend a calculation nobody asked for.
        ("yes", False), ("y", False), ("no", False), ("sure whatever", False),
    ])
    def test_only_an_explicit_rerun_spends_the_compute(self, machine, tmp_path,
                                                       reply, expect_rerun):
        _seed_planning(machine, tmp_path)
        _seed_normalized(machine, tmp_path, -3.0)
        self._answer(machine, reply)

        routed = machine.validate()
        assert (routed == State.REPLAN) is expect_rerun


# -- physically impossible results ---------------------------------------------

class TestImplausibleValuesAreNotDelivered:
    """The gap run 1cd39ffd fell through.

    A CO2 heat of formation of -27452 kJ/mol (experiment -393.5) reached the
    researcher labelled "accepted": the plan's acceptance metric carried
    target_value: null and no literature baseline covered the molecule, so both
    comparative branches of _cross_validate honestly had nothing to say. A bound
    on what the quantity can physically be is the only check that needs no
    reference value.
    """

    BAD = -27452.226
    GOOD = -395.6064420439893

    def _seed_thermo_run(self, machine, tmp_path, value, *, criteria=None,
                         unit=None, name="standard_heat_of_formation_kJ_mol"):
        plan = _plan_for("carbon dioxide", criteria if criteria is not None else [
            # Exactly what the run carried: a metric named, nothing to check it
            # against. Nullable targets are legal, so this is not a bad plan.
            {"metric_name": "standard_heat_of_formation",
             "target_value": None, "tolerance": None},
        ])
        _seed(machine, tmp_path, "execution_plan", plan)
        _seed(machine, tmp_path, "normalized_result", {
            "primary_metric": {"name": name, "value": value,
                               "uncertainty": abs(value) * 0.1,
                               "uncertainty_method": "fallback", "unit": unit},
            "secondary_metrics": [],
            "metadata": {"parser": "csv"},
        })

    def test_the_impossible_value_is_rejected(self, machine, tmp_path):
        self._seed_thermo_run(machine, tmp_path, self.BAD)
        assert machine.validate() != State.ACCEPT

        report = machine._load_artifact("validation_report")
        assert report["acceptance_status"] == "rejected"
        assert "not physically possible" in report["rationale"]
        assert report["plausibility"], "the finding must be on the record"

    def test_the_verdict_it_overrode_is_preserved(self, machine, tmp_path):
        """The old rationale was true -- nothing COULD grade it -- so it stays
        readable next to the new one rather than being overwritten."""
        self._seed_thermo_run(machine, tmp_path, self.BAD)
        machine.validate()
        rationale = machine._load_artifact("validation_report")["rationale"]
        assert "without external validation" in rationale
        assert rationale.index("physically possible") < rationale.index(
            "without external validation")

    def test_the_report_names_the_likely_unit_slip(self, machine, tmp_path):
        self._seed_thermo_run(machine, tmp_path, self.BAD)
        machine.validate()
        rationale = machine._load_artifact("validation_report")["rationale"]
        assert "unit slip" in rationale
        assert "96.4853" in rationale          # the factor that actually caused it

    def test_the_run_that_was_right_is_still_accepted(self, machine, tmp_path):
        """The regression that matters: a backstop which flags the good runs is
        worse than none. -395.6 is the answer the NWChem run delivered."""
        self._seed_thermo_run(machine, tmp_path, self.GOOD)
        assert machine.validate() == State.ACCEPT
        report = machine._load_artifact("validation_report")
        assert report["acceptance_status"] == "accepted"
        assert "plausibility" not in report

    def test_a_stated_unit_that_differs_is_not_second_guessed(self, machine, tmp_path):
        """-94060 kcal/mol IS -393.5 kJ/mol; judging it against a kJ/mol bound
        would invent a failure."""
        self._seed_thermo_run(machine, tmp_path, -94060.0, unit="kcal/mol",
                              name="standard_heat_of_formation")
        assert machine.validate() == State.ACCEPT

    def test_a_non_finite_metric_is_not_delivered_as_a_result(self, machine, tmp_path):
        """NaN survives extraction but is dropped by _normalized_metrics, so it
        used to leave nothing to grade -- i.e. "accepted"."""
        self._seed_thermo_run(machine, tmp_path, float("nan"))
        assert machine.validate() != State.ACCEPT
        report = machine._load_artifact("validation_report")
        assert report["acceptance_status"] == "rejected"

    def test_it_still_defers_to_a_stricter_real_criterion(self, machine, tmp_path):
        """When the plan CAN grade the value, that verdict is not weakened: the
        bound only ever makes a verdict worse."""
        self._seed_thermo_run(machine, tmp_path, self.GOOD, criteria=[
            {"metric_name": "standard_heat_of_formation",
             "target_value": -393.5, "tolerance": 0.1}])
        assert machine.validate() != State.ACCEPT
        assert machine._load_artifact(
            "validation_report")["acceptance_status"] == "rejected"

    def test_the_env_switch_restores_the_old_behaviour(self, machine, tmp_path,
                                                      monkeypatch):
        """An escape hatch, so a deployment with a legitimately out-of-bounds
        property never has to edit the pipeline."""
        monkeypatch.setenv("TWAIN_PLAUSIBILITY_CHECK", "0")
        self._seed_thermo_run(machine, tmp_path, self.BAD)
        assert machine.validate() == State.ACCEPT

    def test_a_broken_range_table_does_not_fail_a_finished_run(self, machine,
                                                               tmp_path):
        with patch.object(SM.plausibility, "check_metrics",
                          side_effect=RuntimeError("table on fire")):
            self._seed_thermo_run(machine, tmp_path, self.GOOD)
            assert machine.validate() == State.ACCEPT


# -- run 52019085: validate the result, not every number in the output ---------

_RUN_52019085 = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                            / "run_52019085_normalized.json").read_text())
_ASPIRIN_PLAN = dict(PLAN, requested_property=None,
                     target_system={"formula": "C9H8O4", "molecule": {"name": "aspirin"}},
                     acceptance_metrics=[{"metric_name": "aqueous_solubility_at_25C",
                                          "target_value": None, "tolerance": None}])


def _validate_run(machine, tmp_path, normalized=_RUN_52019085, plan=_ASPIRIN_PLAN):
    _seed_planning(machine, tmp_path, plan)
    _seed(machine, tmp_path, "normalized_result", normalized)
    machine.validate()
    return machine._load_artifact("validation_report")


def test_only_the_log_s_result_is_compared(machine, tmp_path):
    # Not the mol/L copy (0.0102), not the ESOL error estimate (0.69), not the
    # descriptor block's duplicate: one comparison, -1.99 against -1.72.
    report = _validate_run(machine, tmp_path)
    (only,) = report["cross_validation"]["comparisons"]
    assert (only["property"], round(only["predicted"], 2), only["literature"]) == \
        ("logS", -1.99, -1.72)


def test_it_is_graded_in_log_units_and_accepted(machine, tmp_path):
    report = _validate_run(machine, tmp_path)
    assert report["acceptance_status"] == "accepted"
    assert "off by 0.27" in report["rationale"]


def test_a_stated_mol_per_l_result_is_converted_to_log_s(machine, tmp_path):
    only_molar = {"primary_metric": {"name": "aqueous_solubility_at_25C", "value": 0.0101886,
                                     "unit": "mol/L", "uncertainty": 0.001},
                  "secondary_metrics": []}
    (only,) = _validate_run(machine, tmp_path, only_molar)["cross_validation"]["comparisons"]
    assert round(only["predicted"], 2) == -1.99


def test_far_off_in_log_units_is_still_rejected(machine, tmp_path):
    wrong = {"primary_metric": {"name": "logS", "value": -4.0, "uncertainty": 0.1},
             "secondary_metrics": []}
    assert _validate_run(machine, tmp_path, wrong)["acceptance_status"] == "rejected"


def test_your_target_met_but_the_literature_disagrees_needs_your_review(machine, tmp_path):
    wrong = {"primary_metric": {"name": "aqueous_solubility_at_25C_logS", "value": -4.0,
                                "uncertainty": 0.1}, "secondary_metrics": []}
    plan = dict(_ASPIRIN_PLAN, acceptance_metrics=[
        {"metric_name": "aqueous_solubility_at_25C", "target_value": -4.0, "tolerance": 0.3,
         "set_by": "researcher"}])
    report = _validate_run(machine, tmp_path, wrong, plan)
    assert report["acceptance_status"] == "needs_review"
    assert report["rationale"].startswith("Your acceptance target is met")
    assert "literature disagrees" in report["rationale"]


def test_a_target_twain_proposed_does_not_soften_a_rejection(machine, tmp_path):
    wrong = {"primary_metric": {"name": "aqueous_solubility_at_25C_logS", "value": -4.0,
                                "uncertainty": 0.1}, "secondary_metrics": []}
    plan = dict(_ASPIRIN_PLAN, acceptance_metrics=[
        {"metric_name": "aqueous_solubility_at_25C", "target_value": -4.0, "tolerance": 0.3}])
    assert _validate_run(machine, tmp_path, wrong, plan)["acceptance_status"] == "rejected"


def test_validate_reads_the_typed_target_and_shows_the_working(machine, tmp_path):
    # The researcher typed "log S = −1.72" / "0.75 log units" (#237); the
    # result's headline is in mol/L. Compared on one scale: accepted.
    plan = dict(_ASPIRIN_PLAN, acceptance_metrics=[
        {"metric_name": "aqueous_solubility_at_25C", "target_value": -1.72, "tolerance": 0.75,
         "target_text": "log S = −1.72", "tolerance_text": "0.75 log units",
         "set_by": "researcher"}])
    report = _validate_run(machine, tmp_path, plan=plan)
    (check,) = report["target_checks"]
    assert check["status"] == "accepted" and check["unit"] == "log10(mol/L)"
    assert round(check["result"], 2) == -1.99 and check["reading"]["source"] == "rules"
    assert report["acceptance_status"] == "accepted"
