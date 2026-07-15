"""Correction for hyperparameter failures: sweep a few learning rates."""
from __future__ import annotations

from typing import Dict, List

from self_correction.failure_classifier import Diagnosis, FailureMode
from self_correction.strategies.base import (
    CorrectionContext,
    CorrectionStrategy,
    build_plan,
)

_DEFAULT_LR_GRID: List[float] = [1e-4, 1e-3, 1e-2]


class HyperparameterStrategy(CorrectionStrategy):
    mode = FailureMode.HYPERPARAMETER

    def propose(self, diagnosis: Diagnosis, context: CorrectionContext) -> Dict:
        grid = list(context.lr_grid) if context.lr_grid else list(_DEFAULT_LR_GRID)
        correction = {
            "modification_type": "change_hyperparameters",
            "target": "param",
            "new_value": {"learning_rate_grid": grid, "select": "best_validation"},
            "rationale": "Loss curve indicates a poorly chosen learning rate; grid-search "
            f"{len(grid)} values and keep the run with the best validation metric.",
        }
        return build_plan(
            diagnosis=diagnosis.explanation or "Hyperparameter failure.",
            proposed_corrections=[correction],
            fallback_strategy="If no learning rate in the grid converges, widen the sweep or escalate to the researcher.",
            context=context,
            primary_metric_delta=self._forecast(diagnosis, context, 0.6),
            confidence=diagnosis.confidence,
        )
