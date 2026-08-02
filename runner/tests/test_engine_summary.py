"""The completion message must say what the run found and how it was checked.

Regression for "the run succeeded but there was no sign of validation
anywhere": interpret/validate wrote their artifacts, but the chat summary was
a fixed "Run complete" line, so the researcher never saw the verdict.
``final_summary`` reads the Epic 6 artifacts (normalized_result,
validation_report) and surfaces the result + verdict inline.
"""
import json
import types

from runner.engine import _RealEngine


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
