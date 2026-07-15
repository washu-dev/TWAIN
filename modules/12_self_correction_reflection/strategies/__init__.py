"""Correction strategy registry (Story 6.3).

Maps each :class:`FailureMode` to the strategy that repairs it. ``strategy_for``
resolves a diagnosis to its strategy; ``propose_correction`` is the one-call
shortcut used by the reflection loop.
"""
from __future__ import annotations

from typing import Dict, Optional

from self_correction.failure_classifier import Diagnosis, FailureMode
from self_correction.strategies.base import (
    CorrectionContext,
    CorrectionStrategy,
    build_plan,
)
from self_correction.strategies.data_distribution import DataDistributionStrategy
from self_correction.strategies.hyperparameter import HyperparameterStrategy
from self_correction.strategies.input_quality import InputQualityStrategy
from self_correction.strategies.model_mismatch import ModelMismatchStrategy

_REGISTRY: Dict[FailureMode, CorrectionStrategy] = {
    FailureMode.INPUT_QUALITY: InputQualityStrategy(),
    FailureMode.MODEL_MISMATCH: ModelMismatchStrategy(),
    FailureMode.HYPERPARAMETER: HyperparameterStrategy(),
    FailureMode.DATA_DISTRIBUTION: DataDistributionStrategy(),
}


def strategy_for(mode: FailureMode) -> Optional[CorrectionStrategy]:
    return _REGISTRY.get(mode)


def propose_correction(diagnosis: Diagnosis, context: CorrectionContext) -> Optional[Dict]:
    """Return a CorrectionPlan for ``diagnosis``, or ``None`` for UNKNOWN modes."""
    strategy = strategy_for(diagnosis.mode)
    if strategy is None:
        return None
    return strategy.propose(diagnosis, context)


__all__ = [
    "CorrectionContext",
    "CorrectionStrategy",
    "build_plan",
    "strategy_for",
    "propose_correction",
    "DataDistributionStrategy",
    "HyperparameterStrategy",
    "InputQualityStrategy",
    "ModelMismatchStrategy",
]
