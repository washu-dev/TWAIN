"""Correction for bad input (e.g. malformed SMILES)."""
from __future__ import annotations

from typing import Dict

from self_correction.failure_classifier import Diagnosis, FailureMode
from self_correction.strategies.base import (
    CorrectionContext,
    CorrectionStrategy,
    build_plan,
)


class InputQualityStrategy(CorrectionStrategy):
    mode = FailureMode.INPUT_QUALITY

    def propose(self, diagnosis: Diagnosis, context: CorrectionContext) -> Dict:
        if context.sanitized_input:
            # recoverable: substitute the sanitized input and retry automatically.
            correction = {
                "modification_type": "request_more_data",
                "target": "input",
                "new_value": context.sanitized_input,
                "rationale": "Input was malformed but sanitizable; retry with the canonicalized structure.",
            }
            recovery, fallback = 0.9, "If the sanitized input still fails, ask the researcher to confirm the structure."
        else:
            # unrecoverable automatically: bounce back to the researcher.
            correction = {
                "modification_type": "request_more_data",
                "target": "input",
                "new_value": {"needs": "corrected_structure"},
                "rationale": "Input could not be parsed or sanitized; a valid structure is required to proceed.",
            }
            recovery, fallback = 0.0, "Halt and request a corrected input from the researcher."

        return build_plan(
            diagnosis=diagnosis.explanation or "Input quality failure.",
            proposed_corrections=[correction],
            fallback_strategy=fallback,
            context=context,
            primary_metric_delta=self._forecast(diagnosis, context, recovery),
            confidence=diagnosis.confidence,
        )
