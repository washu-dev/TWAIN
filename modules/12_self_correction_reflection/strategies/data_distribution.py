"""Correction for out-of-distribution failures.

Two complementary corrections: request more in-domain training data, or relax the
acceptance criteria so a genuinely harder point isn't judged against an
unrealistic bar.
"""
from __future__ import annotations

from typing import Dict

from self_correction.failure_classifier import Diagnosis, FailureMode
from self_correction.strategies.base import (
    CorrectionContext,
    CorrectionStrategy,
    build_plan,
)


class DataDistributionStrategy(CorrectionStrategy):
    mode = FailureMode.DATA_DISTRIBUTION

    def propose(self, diagnosis: Diagnosis, context: CorrectionContext) -> Dict:
        corrections = [
            {
                "modification_type": "request_more_data",
                "target": "input",
                "new_value": {"needs": "in_domain_training_examples"},
                "rationale": "Test point is out-of-distribution; augment training data near this region "
                "to bring it into the model's applicability domain.",
            },
            {
                "modification_type": "relax_constraints",
                "target": "solver",
                "new_value": {"widen_acceptance_tolerance": True},
                "rationale": "If more in-domain data is unavailable, relax the acceptance tolerance so an "
                "inherently harder, out-of-domain point is judged fairly.",
            },
        ]
        return build_plan(
            diagnosis=diagnosis.explanation or "Data-distribution (out-of-domain) failure.",
            proposed_corrections=corrections,
            fallback_strategy="Report the out-of-domain finding and ask the researcher whether to gather data "
            "or accept a wider tolerance.",
            context=context,
            primary_metric_delta=self._forecast(diagnosis, context, 0.4),
            confidence=diagnosis.confidence,
        )
