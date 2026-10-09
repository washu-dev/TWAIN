"""INTERPRET on a real RIS job's stdout (run 7376b5bf, aspirin solubility by ESOL).

The job printed its summary, but every RIS job's stdout starts with the
wrapper's "[env] using ..." line, and INTERPRET took that '[' for a top-level
JSON array and discarded the summary: "no finite value", for a run that worked.
"""
from __future__ import annotations

from pathlib import Path

import statemachine as SM

STDOUT = (Path(__file__).resolve().parents[1] / "fixtures" / "run_7376b5bf_stdout.txt").read_text()
PLAN = {"requested_property": None,
        "acceptance_metrics": [{"metric_name": "aqueous_solubility_at_25C",
                                "target_value": None, "tolerance": None}]}


def _machine():
    m = SM.StateMachine.__new__(SM.StateMachine)
    artifacts = {"execution_plan": PLAN, "intent_spec": {}}
    m._load_artifact = lambda name: artifacts.get(name)
    return m


def test_the_summary_after_the_env_line_is_found():
    blob = SM.StateMachine._stdout_json(STDOUT)
    assert blob and "aqueous_solubility_at_25C" in blob


def test_a_real_array_is_still_not_a_summary():
    assert SM.StateMachine._stdout_json('[\n  {"a": 1},\n  {"a": 2}\n]\n') is None


def test_the_log_s_variant_is_the_result():
    result = _machine()._normalize_run_output({"stdout": STDOUT, "succeeded": True})
    metric = result.primary_metric
    assert "logs" in metric.name.lower() and round(metric.value, 2) == -1.99


def test_it_is_graded_against_the_logs_reference():
    assert _machine()._baseline_property("aqueous_solubility_at_25C") == "logS"
    assert _machine()._baseline_property("band_gap") == "band_gap"
