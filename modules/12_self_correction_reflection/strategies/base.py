"""Shared plumbing for correction strategies (Story 6.3).

A strategy turns a :class:`~self_correction.failure_classifier.Diagnosis` into a
``CorrectionPlan`` (Story 1.5 schema): a diagnosis string, one or more concrete
proposed corrections, an expected-improvement forecast, a rerun budget, and a
fallback for when auto-correction gives up.

Plans are emitted as plain dicts that validate against
``schemas/correction_plan.schema.json`` so they can be logged and consumed by the
rerun controller without a bespoke dataclass.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from self_correction.failure_classifier import Diagnosis, FailureMode


@dataclass
class CorrectionContext:
    """Inputs a strategy needs to turn a diagnosis into concrete edits."""

    validation_report_id: str = ""
    iteration_count: int = 0

    # shortfall in the primary metric we are trying to close (absolute units);
    # drives the expected-gain forecast.
    gap: Optional[float] = None

    # mode-specific hooks
    next_candidate: Optional[str] = None          # model_mismatch: try #2 from discovery
    sanitized_input: Optional[str] = None         # input_quality: cleaned SMILES if recoverable
    lr_grid: Optional[List[float]] = None         # hyperparameter: learning rates to sweep
    current_params: Optional[Dict] = None         # hyperparameter: current solver settings

    # optional rerun budget echoed into the plan
    token_budget: Optional[int] = None
    iteration_allowance: Optional[int] = None
    cost_ceiling: Optional[float] = None

    # identity
    plan_id: Optional[str] = None
    timestamp: Optional[str] = None


def build_plan(
    diagnosis: str,
    proposed_corrections: List[Dict],
    fallback_strategy: str,
    context: CorrectionContext,
    *,
    primary_metric_delta: float,
    confidence: float,
) -> Dict:
    """Assemble a schema-conformant CorrectionPlan dict."""
    plan: Dict = {
        "diagnosis": diagnosis,
        "proposed_corrections": proposed_corrections,
        "expected_gain": {
            "primary_metric_delta": primary_metric_delta,
            "confidence": max(0.0, min(1.0, confidence)),
        },
        "fallback_strategy": fallback_strategy,
        "metadata": {
            "validation_report_id": context.validation_report_id,
            "iteration_count": context.iteration_count,
        },
    }

    budget = {}
    if context.token_budget is not None:
        budget["token_budget"] = context.token_budget
    if context.iteration_allowance is not None:
        budget["iteration_allowance"] = context.iteration_allowance
    if context.cost_ceiling is not None:
        budget["cost_ceiling"] = context.cost_ceiling
    if budget:
        plan["rerun_budget"] = budget

    if context.plan_id:
        plan["metadata"]["ID"] = context.plan_id
    if context.timestamp:
        plan["metadata"]["timestamp"] = context.timestamp

    return plan


class CorrectionStrategy(ABC):
    """Base class: one strategy per failure mode."""

    mode: FailureMode = FailureMode.UNKNOWN

    @abstractmethod
    def propose(self, diagnosis: Diagnosis, context: CorrectionContext) -> Dict:
        """Return a CorrectionPlan dict for the given diagnosis + context."""
        raise NotImplementedError

    def _forecast(self, diagnosis: Diagnosis, context: CorrectionContext, recovery: float) -> float:
        """Forecast primary-metric improvement: a fraction ``recovery`` of the
        known gap, scaled by how confident we are in the diagnosis."""
        gap = abs(context.gap) if context.gap is not None else 0.0
        return round(gap * recovery * diagnosis.confidence, 4)
