"""Cost & compute estimation for plan synthesis (Story 4.4).

Produces the token count, compute (CPU-hours), and dollar figures that populate
an ExecutionPlan's `cost_estimate` and `compute_estimate`. All rates and defaults
live in a `CostModel` so they can be recalibrated after the first few real runs.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class CostModel:
    """Pricing and default assumptions for cost estimation."""

    usd_per_1k_tokens: float = 0.01     # blended LLM token price
    usd_per_cpu_hour: float = 0.05      # local/cloud compute price per CPU-hour
    base_synthesis_tokens: int = 2_000  # tokens to synthesize one plan
    clarification_tokens: int = 6_000   # ~3 Q&A rounds (5-10k range per backlog)
    default_wall_minutes: float = 10.0  # fallback when tool metadata is absent
    default_cpu_count: int = 8


@dataclass
class CostBreakdown:
    tokens: int
    cpu_hours: float
    usd: float

    def __post_init__(self):
        if type(self.tokens) is not int or self.tokens < 0:
            raise ValueError("CostBreakdown tokens must be a non-negative int")
        if type(self.cpu_hours) not in (int, float) or self.cpu_hours < 0:
            raise ValueError("CostBreakdown cpu_hours must be a non-negative number")
        if type(self.usd) not in (int, float) or self.usd < 0:
            raise ValueError("CostBreakdown usd must be a non-negative number")


class CostEstimator:
    def __init__(self, model: Optional[CostModel] = None):
        self.model = model or CostModel()

    def estimate_tokens(self, extra_tokens: int = 0) -> int:
        """Total LLM tokens: synthesis + clarification + any caller-supplied extra."""
        if type(extra_tokens) is not int or extra_tokens < 0:
            raise ValueError("extra_tokens must be a non-negative int")
        return self.model.base_synthesis_tokens + self.model.clarification_tokens + extra_tokens

    def estimate_cpu_hours(
        self,
        wall_minutes: Optional[float] = None,
        cpu_count: Optional[int] = None,
    ) -> float:
        """CPU-hours = wall-clock minutes / 60 * CPU count (tool metadata or defaults)."""
        wall = self.model.default_wall_minutes if wall_minutes is None else wall_minutes
        cpus = self.model.default_cpu_count if cpu_count is None else cpu_count
        if wall < 0:
            raise ValueError("wall_minutes must be non-negative")
        if cpus < 1:
            raise ValueError("cpu_count must be >= 1")
        return round(wall / 60.0 * cpus, 4)

    def estimate(
        self,
        wall_minutes: Optional[float] = None,
        cpu_count: Optional[int] = None,
        extra_tokens: int = 0,
    ) -> CostBreakdown:
        """Full breakdown: tokens, CPU-hours, and total USD cost."""
        tokens = self.estimate_tokens(extra_tokens=extra_tokens)
        cpu_hours = self.estimate_cpu_hours(wall_minutes=wall_minutes, cpu_count=cpu_count)
        usd = tokens / 1_000.0 * self.model.usd_per_1k_tokens
        usd += cpu_hours * self.model.usd_per_cpu_hour
        return CostBreakdown(tokens=tokens, cpu_hours=cpu_hours, usd=round(usd, 4))
