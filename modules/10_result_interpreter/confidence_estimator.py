"""Uncertainty / confidence estimation for extracted metrics (Story 6.1).

Every normalized metric needs an absolute uncertainty so downstream
cross-validation (Story 6.2) can judge agreement against literature baselines.
We estimate it with a three-tier fallback, most-trusted first:

  1. **Reported**   -- the tool told us (a standard deviation, or a 95% CI). Use
     it directly. A CI is converted to a 1-sigma std via the normal-approx
     ``half_width / 1.96``.
  2. **Convergence**-- a multi-value series (e.g. loss per epoch, or repeated
     measurements). For a converging series we use the spread of the settled
     tail; for independent repeats we use the standard error of the mean.
  3. **Fallback**   -- nothing else to go on: assume 10% of |value|.

Each result carries a :class:`Uncertainty` with the numeric ``std`` and the
``method`` used, so the estimate is explainable.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence

FALLBACK_FRACTION = 0.10
CI95_Z = 1.96  # normal-approx: 95% CI half-width = 1.96 * sigma


class UncertaintyMethod(str, Enum):
    REPORTED_STD = "reported_std"
    REPORTED_CI = "reported_ci"
    CONVERGENCE = "convergence"
    REPEATS = "repeats"
    FALLBACK = "fallback"


@dataclass
class Uncertainty:
    """An absolute 1-sigma uncertainty plus how it was derived."""

    std: float
    method: UncertaintyMethod

    def __post_init__(self):
        if self.std < 0:
            raise ValueError("uncertainty std must be non-negative")
        self.method = UncertaintyMethod(self.method)


def _looks_convergent(values: Sequence[float]) -> bool:
    """True when a series settles down: the back half varies less than the front
    half (typical of a training loss / iterative solver converging)."""
    n = len(values)
    if n < 4:
        return False
    mid = n // 2
    front = values[:mid]
    back = values[mid:]
    front_spread = statistics.pstdev(front)
    back_spread = statistics.pstdev(back)
    if front_spread == 0:
        return False
    return back_spread < front_spread * 0.5


def _tail_std(values: Sequence[float]) -> float:
    """Std of the settled tail (last third, min 2 points) of a converging series."""
    n = len(values)
    tail = list(values[max(1, 2 * n // 3):])
    if len(tail) < 2:
        tail = list(values[-2:])
    return statistics.stdev(tail)


def estimate(
    value: float,
    values: Optional[Sequence[float]] = None,
    *,
    reported_std: Optional[float] = None,
    confidence_interval: Optional[Sequence[float]] = None,
) -> Uncertainty:
    """Estimate the absolute uncertainty of ``value``.

    Args:
        value: the representative (aggregated) metric value.
        values: the full numeric series the metric was drawn from, if any.
        reported_std: a standard deviation the tool reported, if any.
        confidence_interval: a ``[low, high]`` 95% CI the tool reported, if any.
    """
    # 1) reported
    if reported_std is not None:
        if reported_std < 0:
            raise ValueError("reported_std must be non-negative")
        return Uncertainty(float(reported_std), UncertaintyMethod.REPORTED_STD)
    if confidence_interval is not None:
        if len(confidence_interval) != 2:
            raise ValueError("confidence_interval must be [low, high]")
        low, high = confidence_interval
        if high < low:
            raise ValueError("confidence_interval must be ascending")
        return Uncertainty((float(high) - float(low)) / 2.0 / CI95_Z, UncertaintyMethod.REPORTED_CI)

    # 2) convergence / repeats
    series: List[float] = [float(v) for v in (values or [])]
    if len(series) >= 2:
        if _looks_convergent(series):
            return Uncertainty(_tail_std(series), UncertaintyMethod.CONVERGENCE)
        # independent repeats -> standard error of the mean
        sem = statistics.stdev(series) / (len(series) ** 0.5)
        return Uncertainty(sem, UncertaintyMethod.REPEATS)

    # 3) fallback
    return Uncertainty(abs(float(value)) * FALLBACK_FRACTION, UncertaintyMethod.FALLBACK)
