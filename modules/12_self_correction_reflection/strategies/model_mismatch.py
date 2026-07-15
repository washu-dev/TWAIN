"""Correction for wrong-model failures: fall back to the next-best candidate."""
from __future__ import annotations

from typing import Dict

from self_correction.failure_classifier import Diagnosis, FailureMode
from self_correction.strategies.base import (
    CorrectionContext,
    CorrectionStrategy,
    build_plan,
)


class ModelMismatchStrategy(CorrectionStrategy):
    mode = FailureMode.MODEL_MISMATCH

    def propose(self, diagnosis: Diagnosis, context: CorrectionContext) -> Dict:
        candidate = context.next_candidate
        if candidate:
            correction = {
                "modification_type": "switch_model",
                "target": "model",
                "new_value": candidate,
                "rationale": "Current method is miscalibrated for this task; rebuild the plan around the "
                "second-ranked discovery candidate and rerun.",
            }
            recovery = 0.7
            fallback = "If the next-best candidate also disagrees, escalate to the researcher for method selection."
        else:
            correction = {
                "modification_type": "switch_model",
                "target": "model",
                "new_value": {"needs": "alternative_candidate"},
                "rationale": "Method appears wrong for this task but no alternative candidate is available; "
                "re-run discovery with relaxed capability filters.",
            }
            recovery = 0.3
            fallback = "Escalate to the researcher to choose or supply an appropriate method."

        return build_plan(
            diagnosis=diagnosis.explanation or "Model mismatch failure.",
            proposed_corrections=[correction],
            fallback_strategy=fallback,
            context=context,
            primary_metric_delta=self._forecast(diagnosis, context, recovery),
            confidence=diagnosis.confidence,
        )
