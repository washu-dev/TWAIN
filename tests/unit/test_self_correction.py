"""Unit tests for self-correction reflection (Story 6.3).

Covers:
  * failure classification on synthetic cases (accuracy well above the 80% bar),
    with 0-1 confidence and human-readable signals,
  * each correction strategy emits a CorrectionPlan that validates against
    schemas/correction_plan.schema.json and carries an improvement forecast,
  * the rerun controller enforces the iteration cap, the <5% convergence stop,
    and the cost-benefit gate (no infinite loops), and
  * the reflect() convenience wires classify -> strategy end-to-end.

Run from the repo root with:  pixi run pytest tests/unit/test_self_correction.py
"""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from self_correction.failure_classifier import (
    Diagnosis,
    FailureMode,
    RunEvidence,
    classify,
)
from self_correction.strategies import CorrectionContext, propose_correction, strategy_for
from self_correction.rerun_controller import RerunController, RerunPolicy
from self_correction.reflection import reflect

REPO_ROOT = Path(__file__).resolve().parents[2]
CORRECTION_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "correction_plan.schema.json").read_text()
)
_VALIDATOR = Draft202012Validator(CORRECTION_SCHEMA)


# ── failure classification ───────────────────────────────────────────────────

def test_classify_input_quality_from_parse_error():
    d = classify(RunEvidence(input_parse_error="invalid SMILES 'c1ccc'"))
    assert d.mode is FailureMode.INPUT_QUALITY
    assert 0.0 <= d.confidence <= 1.0 and d.confidence > 0.5
    assert d.signals  # explainable


def test_classify_model_mismatch_from_activation():
    d = classify(RunEvidence(activation_magnitude=8.0, activation_threshold=3.0))
    assert d.mode is FailureMode.MODEL_MISMATCH
    assert d.confidence > 0.5


def test_classify_hyperparameter_from_divergence():
    d = classify(RunEvidence(loss_diverged=True))
    assert d.mode is FailureMode.HYPERPARAMETER


def test_classify_hyperparameter_from_early_plateau():
    # loss flattens quickly but far above the target it needed to reach
    curve = [10.0, 6.0, 5.05, 5.02, 5.01, 5.0]
    d = classify(RunEvidence(loss_curve=curve, target_loss=1.0))
    assert d.mode is FailureMode.HYPERPARAMETER


def test_classify_data_distribution_from_ood():
    d = classify(RunEvidence(ood_score=2.5, ood_threshold=1.0))
    assert d.mode is FailureMode.DATA_DISTRIBUTION
    assert d.confidence > 0.5


def test_classify_unknown_when_no_signal():
    d = classify(RunEvidence())
    assert d.mode is FailureMode.UNKNOWN
    assert d.confidence == 0.0


def test_within_range_activation_is_not_mismatch():
    d = classify(RunEvidence(activation_magnitude=1.0, activation_threshold=3.0))
    assert d.mode is not FailureMode.MODEL_MISMATCH


def test_classification_accuracy_above_80pct_on_synthetic_cases():
    cases = [
        (RunEvidence(input_valid=False), FailureMode.INPUT_QUALITY),
        (RunEvidence(input_parse_error="bad"), FailureMode.INPUT_QUALITY),
        (RunEvidence(activation_magnitude=6.0), FailureMode.MODEL_MISMATCH),
        (RunEvidence(activation_magnitude=10.0), FailureMode.MODEL_MISMATCH),
        (RunEvidence(loss_diverged=True), FailureMode.HYPERPARAMETER),
        (
            RunEvidence(loss_curve=[9.0, 5.1, 5.02, 5.01, 5.0], target_loss=1.0),
            FailureMode.HYPERPARAMETER,
        ),
        (RunEvidence(ood_score=3.0), FailureMode.DATA_DISTRIBUTION),
        (RunEvidence(ood_score=1.5), FailureMode.DATA_DISTRIBUTION),
        (RunEvidence(input_valid=True, activation_magnitude=9.0), FailureMode.MODEL_MISMATCH),
        (RunEvidence(ood_score=5.0, activation_magnitude=1.0), FailureMode.DATA_DISTRIBUTION),
    ]
    correct = sum(classify(ev).mode is expected for ev, expected in cases)
    assert correct / len(cases) >= 0.8


# ── correction strategies ────────────────────────────────────────────────────

def _ctx(**kw):
    base = dict(validation_report_id="val-001", iteration_count=1, gap=0.5)
    base.update(kw)
    return CorrectionContext(**base)


@pytest.mark.parametrize(
    "evidence, kwargs",
    [
        (RunEvidence(input_parse_error="bad"), dict(sanitized_input="CC(=O)O")),
        (RunEvidence(activation_magnitude=8.0), dict(next_candidate="model_b")),
        (RunEvidence(loss_diverged=True), dict(lr_grid=[1e-4, 1e-3, 1e-2])),
        (RunEvidence(ood_score=3.0), dict()),
    ],
)
def test_strategies_emit_schema_valid_plans(evidence, kwargs):
    diagnosis = classify(evidence)
    plan = propose_correction(diagnosis, _ctx(**kwargs))
    _VALIDATOR.validate(plan)  # raises if invalid
    assert plan["proposed_corrections"]
    assert "expected_gain" in plan
    assert 0.0 <= plan["expected_gain"]["confidence"] <= 1.0


def test_model_mismatch_uses_next_candidate():
    diagnosis = classify(RunEvidence(activation_magnitude=8.0))
    plan = propose_correction(diagnosis, _ctx(next_candidate="second_best_model"))
    correction = plan["proposed_corrections"][0]
    assert correction["modification_type"] == "switch_model"
    assert correction["new_value"] == "second_best_model"


def test_hyperparameter_proposes_lr_grid():
    diagnosis = classify(RunEvidence(loss_diverged=True))
    plan = propose_correction(diagnosis, _ctx(lr_grid=[0.001, 0.01, 0.1]))
    nv = plan["proposed_corrections"][0]["new_value"]
    assert nv["learning_rate_grid"] == [0.001, 0.01, 0.1]


def test_data_distribution_offers_data_or_relaxation():
    diagnosis = classify(RunEvidence(ood_score=3.0))
    plan = propose_correction(diagnosis, _ctx())
    types = {c["modification_type"] for c in plan["proposed_corrections"]}
    assert types == {"request_more_data", "relax_constraints"}


def test_forecast_scales_with_confidence_and_gap():
    diagnosis = classify(RunEvidence(activation_magnitude=8.0))
    plan = propose_correction(diagnosis, _ctx(gap=1.0, next_candidate="m2"))
    delta = plan["expected_gain"]["primary_metric_delta"]
    assert 0 < delta <= 1.0  # bounded by the gap

    # forecast is zero when there's nothing to recover (unrecoverable input)
    d2 = classify(RunEvidence(input_parse_error="bad"))
    plan2 = propose_correction(d2, _ctx(gap=1.0, sanitized_input=None))
    assert plan2["expected_gain"]["primary_metric_delta"] == 0.0


def test_rerun_budget_echoed_into_plan():
    diagnosis = classify(RunEvidence(loss_diverged=True))
    plan = propose_correction(
        diagnosis, _ctx(token_budget=1000, iteration_allowance=3, cost_ceiling=2.5)
    )
    assert plan["rerun_budget"] == {
        "token_budget": 1000,
        "iteration_allowance": 3,
        "cost_ceiling": 2.5,
    }
    _VALIDATOR.validate(plan)


def test_unknown_mode_has_no_strategy():
    assert strategy_for(FailureMode.UNKNOWN) is None
    assert propose_correction(Diagnosis(FailureMode.UNKNOWN, 0.0), _ctx()) is None


# ── bounded rerun control ────────────────────────────────────────────────────

def test_iteration_cap_stops_the_loop():
    ctrl = RerunController(RerunPolicy(max_iterations=3))
    for _ in range(3):
        ctrl.begin_iteration()
    decision = ctrl.decide(expected_benefit=1.0, estimated_cost=0.1)
    assert decision.should_rerun is False
    assert decision.stop_reason == "iteration_cap"


def test_convergence_stop_below_five_percent():
    ctrl = RerunController(RerunPolicy(min_improvement=0.05))
    ctrl.begin_iteration()
    ctrl.record_metric(1.00)
    ctrl.record_metric(0.98)  # only 2% better than previous
    decision = ctrl.decide(expected_benefit=1.0, estimated_cost=0.1)
    assert decision.should_rerun is False
    assert decision.stop_reason == "converged"


def test_cost_benefit_gate_blocks_expensive_reruns():
    ctrl = RerunController()
    ctrl.begin_iteration()
    decision = ctrl.decide(expected_benefit=0.1, estimated_cost=0.5)
    assert decision.should_rerun is False
    assert decision.stop_reason == "not_cost_effective"


def test_rerun_allowed_when_worth_it_and_under_cap():
    ctrl = RerunController(RerunPolicy(max_iterations=5))
    ctrl.begin_iteration()
    ctrl.record_metric(1.0)
    ctrl.record_metric(0.5)  # 50% improvement -> not converged
    decision = ctrl.decide(expected_benefit=0.8, estimated_cost=0.2)
    assert decision.should_rerun is True
    assert decision.stop_reason is None


def test_controller_never_loops_forever():
    """Simulate a loop that always wants to rerun; the cap must halt it."""
    ctrl = RerunController(RerunPolicy(max_iterations=5, min_improvement=0.0))
    iterations = 0
    while True:
        decision = ctrl.decide(expected_benefit=1.0, estimated_cost=0.0)
        if not decision.should_rerun:
            break
        ctrl.begin_iteration()
        ctrl.record_metric(1.0)  # no improvement, but min_improvement=0 disables that guard
        iterations += 1
        assert iterations <= 5  # safety net
    assert iterations == 5


def test_bad_policy_rejected():
    with pytest.raises(ValueError):
        RerunPolicy(max_iterations=0)
    with pytest.raises(ValueError):
        RerunPolicy(min_improvement=1.5)


# ── end-to-end reflect() ─────────────────────────────────────────────────────

def test_reflect_end_to_end_produces_valid_plan():
    evidence = RunEvidence(activation_magnitude=7.0)
    result = reflect(evidence, _ctx(next_candidate="model_b"))
    assert result.diagnosis.mode is FailureMode.MODEL_MISMATCH
    assert result.correction_plan is not None
    _VALIDATOR.validate(result.correction_plan)


def test_reflect_unknown_has_no_plan():
    result = reflect(RunEvidence(), _ctx())
    assert result.diagnosis.mode is FailureMode.UNKNOWN
    assert result.correction_plan is None
