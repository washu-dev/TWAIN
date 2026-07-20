"""Failure diagnosis for self-correction (Story 6.3).

When validation (Story 6.2) rejects a result, this module diagnoses *why* so the
right correction strategy can be chosen. It scores four failure modes from
observable run evidence and returns the most likely one with a 0-1 confidence and
the diagnostic signals that drove the decision:

  * ``input_quality``      -- bad/invalid input (e.g. unparseable SMILES).
  * ``model_mismatch``     -- wrong method for the task; signal: activation
                              magnitudes far outside the model's calibrated range.
  * ``hyperparameter``     -- e.g. learning rate too high (loss diverges) or too
                              low (loss plateaus early, well above target).
  * ``data_distribution``  -- test point is out-of-distribution for the model
                              (applicability-domain distance large).

The classifier is deliberately rule-based and transparent: every score comes with
a human-readable signal string, so the diagnosis is auditable rather than a black
box. Evidence fields are all optional; absent evidence simply contributes no
signal for that mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence, Tuple


class FailureMode(Enum):
    INPUT_QUALITY = "input_quality"
    MODEL_MISMATCH = "model_mismatch"
    HYPERPARAMETER = "hyperparameter"
    DATA_DISTRIBUTION = "data_distribution"
    UNKNOWN = "unknown"


@dataclass
class RunEvidence:
    """Observable signals from a failed run. All fields optional."""

    # input quality
    input_valid: Optional[bool] = None          # False => bad input (e.g. SMILES)
    input_parse_error: Optional[str] = None      # non-empty => parse failed

    # model mismatch
    activation_magnitude: Optional[float] = None  # z-score vs model's calibrated range
    activation_threshold: float = 3.0             # |z| above this => out of range

    # hyperparameter
    loss_diverged: Optional[bool] = None          # True => lr almost certainly too high
    loss_curve: Optional[Sequence[float]] = None  # to detect early plateau
    target_loss: Optional[float] = None           # loss we needed to reach

    # data distribution
    ood_score: Optional[float] = None             # applicability-domain distance
    ood_threshold: float = 1.0                    # above this => out-of-distribution

    # overall (used only as a weak tie-breaker / sanity signal)
    relative_error: Optional[float] = None


@dataclass
class Diagnosis:
    mode: FailureMode
    confidence: float
    signals: List[str] = field(default_factory=list)
    explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "confidence": self.confidence,
            "signals": list(self.signals),
            "explanation": self.explanation,
        }


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score_input_quality(ev: RunEvidence) -> Tuple[float, Optional[str]]:
    if ev.input_parse_error:
        return 0.95, f"input failed to parse: {ev.input_parse_error}"
    if ev.input_valid is False:
        return 0.9, "input flagged invalid (e.g. malformed SMILES)"
    return 0.0, None


def _score_model_mismatch(ev: RunEvidence) -> Tuple[float, Optional[str]]:
    if ev.activation_magnitude is None:
        return 0.0, None
    mag = abs(ev.activation_magnitude)
    if mag <= ev.activation_threshold:
        return 0.0, None
    # confidence grows with how far past the threshold we are
    score = _clamp01(0.5 + 0.5 * (mag - ev.activation_threshold) / ev.activation_threshold)
    return score, (
        f"activation magnitude |z|={mag:.1f} exceeds calibrated range "
        f"(>{ev.activation_threshold:.1f}); model likely wrong for this task"
    )


def _looks_early_plateau(loss: Sequence[float], target: Optional[float]) -> bool:
    """Loss stops moving early yet stays well above the target it needed to hit."""
    if len(loss) < 4:
        return False
    n = len(loss)
    tail = list(loss[n // 2:])
    spread = max(tail) - min(tail)
    scale = abs(loss[0]) or 1.0
    flat = spread < 0.02 * scale
    if target is None:
        # no target: a very early flatten is still weakly suggestive
        return flat
    final = tail[-1]
    return flat and final > target * 1.5


def _score_hyperparameter(ev: RunEvidence) -> Tuple[float, Optional[str]]:
    if ev.loss_diverged:
        return 0.9, "loss diverged (NaN/increasing) -> learning rate too high"
    if ev.loss_curve is not None and _looks_early_plateau(ev.loss_curve, ev.target_loss):
        return 0.75, "loss plateaued early above target -> learning rate too low / under-training"
    return 0.0, None


def _score_data_distribution(ev: RunEvidence) -> Tuple[float, Optional[str]]:
    if ev.ood_score is None:
        return 0.0, None
    if ev.ood_score <= ev.ood_threshold:
        return 0.0, None
    score = _clamp01(0.5 + 0.5 * (ev.ood_score - ev.ood_threshold) / ev.ood_threshold)
    return score, (
        f"applicability-domain distance {ev.ood_score:.2f} exceeds "
        f"{ev.ood_threshold:.2f}; test point is out-of-distribution"
    )


_SCORERS = {
    FailureMode.INPUT_QUALITY: _score_input_quality,
    FailureMode.MODEL_MISMATCH: _score_model_mismatch,
    FailureMode.HYPERPARAMETER: _score_hyperparameter,
    FailureMode.DATA_DISTRIBUTION: _score_data_distribution,
}


def classify(evidence: RunEvidence) -> Diagnosis:
    """Diagnose the most likely failure mode from ``evidence``.

    Returns the highest-scoring mode with its confidence and the signals that
    fired. If nothing scores, returns :attr:`FailureMode.UNKNOWN`.
    """
    scored = []
    all_signals: List[str] = []
    for mode, scorer in _SCORERS.items():
        score, signal = scorer(evidence)
        if signal:
            all_signals.append(f"[{mode.value}] {signal}")
        scored.append((mode, score, signal))

    best_mode, best_score, best_signal = max(scored, key=lambda t: t[1])

    if best_score <= 0.0:
        return Diagnosis(
            FailureMode.UNKNOWN,
            confidence=0.0,
            signals=all_signals,
            explanation="No diagnostic signal fired; failure mode is undetermined.",
        )

    return Diagnosis(
        mode=best_mode,
        confidence=round(best_score, 3),
        signals=all_signals or ([best_signal] if best_signal else []),
        explanation=best_signal or "",
    )
