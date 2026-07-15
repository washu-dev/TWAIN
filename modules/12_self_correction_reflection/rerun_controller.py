"""Bounded self-correction loop control (Story 6.3).

Decides whether the system should attempt another correction+rerun, or stop and
hand back to the researcher. Three guards, checked in order:

  1. **Iteration cap** -- at most ``max_iterations`` reruns (default 5),
     budget-constrained by Story 2.4.
  2. **Convergence** -- if the last rerun improved the metric by less than
     ``min_improvement`` (default 5%), further reruns are unlikely to help; stop.
  3. **Cost-benefit** -- only rerun when the forecast benefit exceeds the
     estimated cost (LLM + compute), both expressed on a common utility scale
     supplied by the caller.

The controller is state-light: it tracks the iteration count and the metric
history, and every decision carries a human-readable reason so the researcher
can see *why* the loop continued or stopped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class RerunPolicy:
    max_iterations: int = 5
    min_improvement: float = 0.05  # fractional; below this vs last iter => converged

    def __post_init__(self):
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if not 0 <= self.min_improvement < 1:
            raise ValueError("min_improvement must be in [0, 1)")


@dataclass
class RerunDecision:
    should_rerun: bool
    reason: str
    stop_reason: Optional[str] = None  # "iteration_cap" | "converged" | "not_cost_effective"


class RerunController:
    def __init__(self, policy: Optional[RerunPolicy] = None):
        self.policy = policy or RerunPolicy()
        self.iteration = 0
        self.metric_history: List[float] = []

    def record_metric(self, value: float) -> None:
        """Record the primary-metric value observed after an iteration."""
        self.metric_history.append(value)

    def last_improvement(self) -> Optional[float]:
        """Fractional improvement of the latest metric vs the previous one.

        Returns ``None`` until at least two metrics have been recorded. Improvement
        is measured as reduction in error/distance-to-target, so a *smaller* latest
        value counts as positive improvement.
        """
        if len(self.metric_history) < 2:
            return None
        prev, cur = self.metric_history[-2], self.metric_history[-1]
        if prev == 0:
            return 0.0
        return (prev - cur) / abs(prev)

    def decide(
        self,
        *,
        expected_benefit: float,
        estimated_cost: float,
    ) -> RerunDecision:
        """Decide whether to run another correction iteration.

        Args:
            expected_benefit: forecast utility of the next rerun (e.g. from a
                CorrectionPlan's ``expected_gain``), on the same scale as cost.
            estimated_cost: estimated utility cost of the next rerun (LLM + compute).
        """
        if self.iteration >= self.policy.max_iterations:
            return RerunDecision(
                False,
                f"Reached the iteration cap ({self.policy.max_iterations}); escalating to the researcher.",
                stop_reason="iteration_cap",
            )

        improvement = self.last_improvement()
        if improvement is not None and improvement < self.policy.min_improvement:
            return RerunDecision(
                False,
                f"Last iteration improved the metric by only {improvement * 100:.1f}% "
                f"(< {self.policy.min_improvement * 100:.0f}%); converged, asking the researcher.",
                stop_reason="converged",
            )

        if expected_benefit <= estimated_cost:
            return RerunDecision(
                False,
                f"Forecast benefit ({expected_benefit:.3g}) does not exceed estimated cost "
                f"({estimated_cost:.3g}); not worth rerunning.",
                stop_reason="not_cost_effective",
            )

        return RerunDecision(
            True,
            f"Iteration {self.iteration + 1}/{self.policy.max_iterations}: forecast benefit "
            f"({expected_benefit:.3g}) exceeds cost ({estimated_cost:.3g}); rerunning.",
        )

    def begin_iteration(self) -> int:
        """Advance to the next iteration; returns the new (1-based) count."""
        self.iteration += 1
        return self.iteration
