"""Plan composer for module 05_plan_synthesis (Story 4.4).

Takes the top-ranked discovery candidate plus the resolved goal/acceptance
criteria and composes a complete, validatable `ExecutionPlan` (the Epic 1
contract in plan_synthesizer.execution_plan), including cost, compute, and
risk/safety metadata.
"""

import re
from datetime import datetime, timezone
from typing import List, Optional, Union

from method_discovery.scorers import ScoredCandidate
from plan_synthesizer.cost_estimator import CostEstimator
from plan_synthesizer.execution_plan import (
    AcceptanceMetric,
    ComputeEstimate,
    CostEstimate,
    ExecutionPlan,
    ExecutionPlanMetadata,
    SelectedMethod,
    SlurmRequest,
)
from plan_synthesizer.risk_assessor import RiskAssessor

_VERSION_NUM = re.compile(r"(\d+)(?:\.(\d+))?")

# Slurm defaults when the caller doesn't specify resources.
# ``ram`` is gigabytes (matches schemas/examples + codegen's ``ram_gb``);
# the Slurm adapter converts to MB at submit time. Never go below the floor —
# 16M OOMs a venv install on RIS (see Story 5.4 runs).
DEFAULT_CPU_COUNT = 8
DEFAULT_GPU_COUNT = 0
DEFAULT_RAM = 16          # GB
MIN_RAM_GB = 4            # floor applied in synthesize()
DEFAULT_WALL_MINUTES = 10.0
MIN_WALL_MINUTES = 10.0


def version_to_number(version: str) -> float:
    """Coerce a version string to the numeric form the ExecutionPlan contract wants.

    Keeps major.minor (e.g. '2.8.0' -> 2.8, '2024.03.5' -> 2024.03). Falls back to
    1.0 when no leading number can be parsed (e.g. 'unknown').
    """
    if type(version) is not str:
        return 1.0
    m = _VERSION_NUM.search(version)
    if not m:
        return 1.0
    major = m.group(1)
    minor = m.group(2) or "0"
    try:
        return float(f"{int(major)}.{int(minor)}")
    except ValueError:
        return 1.0


class PlanSynthesizer:
    def __init__(
        self,
        cost_estimator: Optional[CostEstimator] = None,
        risk_assessor: Optional[RiskAssessor] = None,
    ):
        self.cost_estimator = cost_estimator or CostEstimator()
        self.risk_assessor = risk_assessor or RiskAssessor()

    def synthesize(
        self,
        candidate: ScoredCandidate,
        goal_id: str,
        acceptance_metrics: List[Union[AcceptanceMetric, dict]],
        requested_capability: Optional[str] = None,
        cpu_count: int = DEFAULT_CPU_COUNT,
        gpu_count: int = DEFAULT_GPU_COUNT,
        ram: int = DEFAULT_RAM,
        wall_minutes: float = DEFAULT_WALL_MINUTES,
        timestamp: Optional[str] = None,
        extra_safety_notes: Optional[List[str]] = None,
    ) -> ExecutionPlan:
        """Compose an ExecutionPlan from a ranked candidate and resolved criteria."""
        if not isinstance(candidate, ScoredCandidate):
            raise ValueError("candidate must be a ScoredCandidate from the discovery ranking")
        if not goal_id or type(goal_id) is not str:
            raise ValueError("goal_id must be a non-empty str")

        entry = candidate.entry

        # Clamp so a bad/missing estimate can't produce an un-runnable sbatch
        # (e.g. --mem=16M). Plan stores ram in GB and max_time in hours.
        ram_gb = max(int(ram), MIN_RAM_GB)
        wall = max(float(wall_minutes), MIN_WALL_MINUTES)

        cost = self.cost_estimator.estimate(wall_minutes=wall, cpu_count=cpu_count)
        risk = self.risk_assessor.assess(entry, requested_capability=requested_capability)

        safety_notes: List[str] = list(risk.notes)
        safety_notes.append(f"Aggregate risk score: {risk.score:.2f}")
        if extra_safety_notes:
            safety_notes.extend(extra_safety_notes)

        ts = timestamp or datetime.now(timezone.utc).isoformat()

        return ExecutionPlan(
            selected_method=SelectedMethod(
                tool_name=entry.name,
                tool_version=version_to_number(entry.version),
            ),
            compute_estimate=ComputeEstimate(cpu_hours=cost.cpu_hours),
            slurm_request=SlurmRequest(
                cpu_count=cpu_count,
                gpu_count=gpu_count,
                max_time=round(wall / 60.0, 4),
                ram=ram_gb,
            ),
            cost_estimate=CostEstimate(min_tokens=cost.tokens, min_cost=cost.usd),
            metadata=ExecutionPlanMetadata(
                timestamp=ts,
                goal_id=goal_id,
                candidate_rank=candidate.rank,
            ),
            acceptance_metrics=acceptance_metrics,
            safety_notes=safety_notes,
        )
