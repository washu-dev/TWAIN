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
    assert summary.startswith("Run finished.")
    assert "TERMINATE" not in summary          # a state name read as an abnormal stop
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


def test_accepted_with_nothing_checked_says_not_verified(tmp_path):
    # Runs 6678e7e7 and the formation-energy run: "Validation: accepted -- No
    # literature baseline ..." read as a pass; nothing had checked the value.
    orch = _orch(tmp_path, {
        "normalized_result": NORMALIZED,
        "validation_report": {
            "acceptance_status": "accepted", "verified": False,
            "rationale": "No literature baseline or matching acceptance criterion "
                         "covers this result; delivered without external validation.",
        },
    })
    summary = _engine().final_summary(orch)
    assert "Validation: not verified" in summary and "accepted" not in summary


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


class TestAcceptanceOverrides:
    """The bar the result is judged against, edited on the approval card.

    TWAIN frequently has no defensible target and writes ``target_value: null``,
    which leaves VALIDATE with nothing to check the answer against -- a silver band
    gap ran that way and was graded on a literature baseline alone (run 913c1ee9).
    Only the researcher knows what "close enough" is for their purpose.
    """

    def _orch(self, tmp_path, plan):
        import json
        import types
        path = tmp_path / "execution_plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        sm = types.SimpleNamespace(
            context=types.SimpleNamespace(artifacts={"execution_plan": str(path)}))
        return types.SimpleNamespace(sm=sm), path

    def _plan(self, tmp_path, metrics):
        return self._orch(tmp_path, {"acceptance_metrics": metrics,
                                     "slurm_request": {"cpu_count": 8}})

    def _read(self, path):
        import json
        return json.loads(path.read_text(encoding="utf-8"))

    def _engine(self):
        from runner.engine import _RealEngine
        # The applier touches only the plan file, so no orchestrator is built.
        return _RealEngine.__new__(_RealEngine)

    def test_a_null_target_becomes_the_researchers_number(self, tmp_path):
        orch, path = self._plan(tmp_path, [
            {"metric_name": "bandgap", "target_value": None, "tolerance": None}])
        self._engine().apply_acceptance_overrides(orch, [
            {"metric_name": "bandgap", "target_value": 0.0, "tolerance": 0.05}])
        assert self._read(path)["acceptance_metrics"] == [
            {"metric_name": "bandgap", "target_value": 0.0, "tolerance": 0.05}]

    def test_clearing_a_bar_is_expressible(self):
        """None is a legal target, so an emptied field must round-trip as "no bar"
        rather than collapsing to 0.0 -- which is a bar, and a very strict one."""
        from runner.engine import _as_number
        assert _as_number(None) is None
        assert _as_number("") is None
        assert _as_number("   ") is None
        assert _as_number("abc") is None
        assert _as_number(True) is None          # bool is not a measurement
        assert _as_number(float("nan")) is None
        assert _as_number(float("inf")) is None
        assert _as_number("0") == 0.0            # zero IS a target
        assert _as_number(-393.5) == -393.5

    def test_an_untouched_metric_is_left_alone(self, tmp_path):
        """A partial edit matches by name, so editing one bar does not blank another."""
        orch, path = self._plan(tmp_path, [
            {"metric_name": "bandgap", "target_value": 1.1, "tolerance": 0.2},
            {"metric_name": "lattice_constant", "target_value": 4.09, "tolerance": 0.05}])
        self._engine().apply_acceptance_overrides(orch, [
            {"metric_name": "bandgap", "target_value": 0.0, "tolerance": 0.05}])
        metrics = {m["metric_name"]: m for m in self._read(path)["acceptance_metrics"]}
        assert metrics["bandgap"]["target_value"] == 0.0
        assert metrics["lattice_constant"]["target_value"] == 4.09

    def test_a_new_metric_is_appended(self, tmp_path):
        """Asking for a bar TWAIN never proposed is a legitimate request."""
        orch, path = self._plan(tmp_path, [
            {"metric_name": "bandgap", "target_value": None, "tolerance": None}])
        self._engine().apply_acceptance_overrides(orch, [
            {"metric_name": "lattice_constant", "target_value": 4.09, "tolerance": 0.05}])
        names = [m["metric_name"] for m in self._read(path)["acceptance_metrics"]]
        assert names == ["bandgap", "lattice_constant"]

    def test_the_rest_of_the_plan_is_untouched(self, tmp_path):
        orch, path = self._plan(tmp_path, [
            {"metric_name": "bandgap", "target_value": None, "tolerance": None}])
        self._engine().apply_acceptance_overrides(orch, [
            {"metric_name": "bandgap", "target_value": 0.0, "tolerance": 0.05}])
        assert self._read(path)["slurm_request"] == {"cpu_count": 8}

    def test_junk_is_ignored_rather_than_written(self, tmp_path):
        orch, path = self._plan(tmp_path, [
            {"metric_name": "bandgap", "target_value": 1.1, "tolerance": 0.2}])
        engine = self._engine()
        for payload in (None, [], "nope", [{}], [{"target_value": 1}], [None]):
            engine.apply_acceptance_overrides(orch, payload)
        assert self._read(path)["acceptance_metrics"] == [
            {"metric_name": "bandgap", "target_value": 1.1, "tolerance": 0.2}]

    def test_a_missing_plan_file_is_not_a_crash(self, tmp_path):
        import types
        sm = types.SimpleNamespace(context=types.SimpleNamespace(artifacts={}))
        self._engine().apply_acceptance_overrides(
            types.SimpleNamespace(sm=sm),
            [{"metric_name": "bandgap", "target_value": 0.0, "tolerance": 0.05}])
