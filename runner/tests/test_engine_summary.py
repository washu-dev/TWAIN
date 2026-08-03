"""The completion message must say what the run found and how it was checked.

Regression for "the run succeeded but there was no sign of validation
anywhere": interpret/validate wrote their artifacts, but the chat summary was
a fixed "Run complete" line, so the researcher never saw the verdict.
``final_summary`` reads the Epic 6 artifacts (normalized_result,
validation_report) and surfaces the result + verdict inline.
"""
import json
import types

from runner import runner as runner_mod
from runner.artifacts import capture_artifacts
from runner.engine import _RealEngine
from runner.tests.test_runner import (
    FakeDB,
    FakeEngine,
    FakeOrchestrator,
    RecordingNotifier,
)


def _engine():
    # __init__ imports the heavy pipeline; final_summary only reads artifacts.
    return _RealEngine.__new__(_RealEngine)


def _orch(tmp_path, artifacts, state="TERMINATE"):
    paths = {}
    for name, payload in artifacts.items():
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        paths[name] = str(p)
    sm = types.SimpleNamespace(
        context=types.SimpleNamespace(artifacts=paths),
        current_state=types.SimpleNamespace(name=state),
    )
    return types.SimpleNamespace(sm=sm)


NORMALIZED = {
    "primary_metric": {"name": "logS", "value": -1.7012, "uncertainty": 0.05,
                       "uncertainty_method": "reported", "unit": "log10(mol/L)"},
    "secondary_metrics": [],
    "metadata": {},
}


def test_planning_only_run_keeps_the_plain_line(tmp_path):
    summary = _engine().final_summary(_orch(tmp_path, {}))
    assert summary.startswith("Run complete (final state: TERMINATE).")
    assert "Validation" not in summary


def test_summary_surfaces_result_and_verdict(tmp_path):
    orch = _orch(tmp_path, {
        "normalized_result": NORMALIZED,
        "validation_report": {
            "acceptance_status": "accepted",
            "rationale": "logS -1.70 vs literature -1.72 (1% relative error)",
        },
    })
    summary = _engine().final_summary(orch)
    assert "Result: logS = -1.7012 ± 0.05 log10(mol/L)" in summary
    assert "Validation: accepted — logS -1.70 vs literature -1.72" in summary


def test_interpreted_result_without_a_reference_says_so(tmp_path):
    orch = _orch(tmp_path, {"normalized_result": NORMALIZED})
    summary = _engine().final_summary(orch)
    assert "Result: logS" in summary
    assert "Validation: not performed — no reference" in summary


def test_stopped_correction_loop_is_flagged(tmp_path):
    orch = _orch(tmp_path, {
        "normalized_result": NORMALIZED,
        "validation_report": {
            "acceptance_status": "needs_review",
            "rationale": "predicted -2.05 vs -1.72",
            "rerun": {"decision": "stop", "stop_reason": "converged",
                      "final_verdict": "needs_review"},
        },
    })
    summary = _engine().final_summary(orch)
    assert ("Validation: needs_review (correction loop stopped: converged; "
            "delivered for your review)") in summary
    assert "predicted -2.05 vs -1.72" in summary


class TestCorrectionPassIsNotTheApprovalGate:
    """A correction pass (CORRECT -> BUILD) re-enters BUILD with the plan the
    researcher already approved. The driver must not read that as the approval
    gate: doing so posts a second card for a decision already made and stalls
    the run behind an answer it does not need.
    """

    def test_gate_proceeds_immediately_when_the_plan_is_already_approved(self):
        db, engine = FakeDB(), FakeEngine()
        engine.approved = True                      # approved in an earlier slice
        orch = FakeOrchestrator(lambda q: "", None)
        notifier = RecordingNotifier()

        outcome = runner_mod._cross_approval_gate(
            db, "conv-1", orch, engine, notifier)

        assert outcome == "proceed"
        assert db.kinds() == []                     # no second approval card
        assert notifier.calls == []                 # and no second notification

    def test_gate_still_posts_the_card_when_nothing_is_approved(self):
        db, engine = FakeDB(), FakeEngine()
        orch = FakeOrchestrator(lambda q: "", None)

        outcome = runner_mod._cross_approval_gate(
            db, "conv-1", orch, engine, RecordingNotifier())

        assert outcome == "released"
        assert "approval_request" in db.kinds()


class TestArtifactRetirement:
    """capture_artifacts must be able to express "this pass produced no result":
    the report endpoint reads the artifacts table, not the run context, so an
    upsert-only store served the previous pass's metric as this run's own.
    """

    def test_a_result_no_longer_produced_is_removed_from_the_store(self):
        db = FakeDB()
        db.upsert_artifact("conv-1", "normalized_result", '{"old": 1}', "json")
        db.upsert_artifact("conv-1", "intent_spec", '{"keep": 1}', "json")

        # a pass whose context no longer references a normalized result
        orch = types.SimpleNamespace(
            sm=types.SimpleNamespace(context=types.SimpleNamespace(artifacts={})))
        capture_artifacts(db, "conv-1", orch)

        names = [a["name"] for a in db.artifacts]
        assert "normalized_result" not in names
        assert "intent_spec" in names       # only retractable outputs are retired
