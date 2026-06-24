"""Feasibility validation for synthesized ExecutionPlans (Stories 1.3 + 4.4).

Checks an ExecutionPlan against researcher/policy constraints and the selected
tool's capabilities, returning a structured result with clear error messages
rather than failing silently. Use `validate_or_raise` when a hard failure is
desired (e.g. before handing the plan to code generation).
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ValidationResult:
    is_valid: bool
    errors: List[str] = field(default_factory=list)


class PlanValidator:
    """Validate an ExecutionPlan for feasibility.

    Constraints are optional; only the ones supplied are enforced:
      * max_cost_usd            estimated cost must not exceed the per-run budget.
      * wall_clock_limit_hours  slurm max_time must fit within the limit.
      * required_input_format   the input format the goal needs (see tool_input_formats).
      * tool_input_formats      formats the selected tool accepts; the required
                                format must be among them.
      * require_acceptance_metrics  plan must carry at least one acceptance metric.
    """

    def __init__(
        self,
        max_cost_usd: Optional[float] = None,
        wall_clock_limit_hours: Optional[float] = None,
        required_input_format: Optional[str] = None,
        tool_input_formats: Optional[List[str]] = None,
        require_acceptance_metrics: bool = True,
    ):
        self.max_cost_usd = max_cost_usd
        self.wall_clock_limit_hours = wall_clock_limit_hours
        self.required_input_format = required_input_format
        self.tool_input_formats = tool_input_formats
        self.require_acceptance_metrics = require_acceptance_metrics

    def validate_plan(self, plan) -> ValidationResult:
        errors: List[str] = []

        if self.max_cost_usd is not None:
            cost = plan.cost_estimate.min_cost
            if cost > self.max_cost_usd:
                errors.append(
                    f"Estimated cost ${cost:.2f} exceeds per-run budget ${self.max_cost_usd:.2f}."
                )

        if self.wall_clock_limit_hours is not None:
            max_time = plan.slurm_request.max_time
            if max_time > self.wall_clock_limit_hours:
                errors.append(
                    f"Estimated wall-clock {max_time:.2f}h exceeds limit "
                    f"{self.wall_clock_limit_hours:.2f}h."
                )

        if self.required_input_format is not None and self.tool_input_formats is not None:
            accepted = {f.lower() for f in self.tool_input_formats}
            if self.required_input_format.lower() not in accepted:
                errors.append(
                    f"Selected tool '{plan.selected_method.tool_name}' cannot ingest required "
                    f"input format '{self.required_input_format}' "
                    f"(accepts: {sorted(self.tool_input_formats)})."
                )

        if self.require_acceptance_metrics and not plan.acceptance_metrics:
            errors.append("Plan has no acceptance metrics; outcome cannot be judged.")

        return ValidationResult(is_valid=len(errors) == 0, errors=errors)

    def validate_or_raise(self, plan) -> None:
        result = self.validate_plan(plan)
        if not result.is_valid:
            raise ValueError("Infeasible plan: " + "; ".join(result.errors))
