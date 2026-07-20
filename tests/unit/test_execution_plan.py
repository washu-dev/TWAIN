"""Unit tests for plan_synthesizer.execution_plan.

Exercises the ExecutionPlan dataclass and its nested types (SelectedMethod,
ComputeEstimate, SlurmRequest, CostEstimate, ExecutionPlanMetadata,
AcceptanceMetric): valid construction from the shipped example, type coercion
of nested dicts, validation/rejection of bad input, and an asdict round-trip.

Run from the repo root with:  pixi run pytest tests/unit/test_execution_plan.py
"""
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from plan_synthesizer.execution_plan import (
    AcceptanceMetric,
    ComputeEstimate,
    CostEstimate,
    ExecutionPlan,
    ExecutionPlanMetadata,
    SelectedMethod,
    SlurmRequest,
)

EXAMPLE_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "examples"
    / "execution_plan_example.json"
)


@pytest.fixture
def example_dict():
    """A fresh copy of the canonical example plan for each test."""
    with open(EXAMPLE_PATH, "r") as f:
        return json.load(f)


def with_field(base, **overrides):
    """Return a shallow copy of `base` with `overrides` applied."""
    return {**base, **overrides}


# --------------------------------------------------------------------------- #
# SelectedMethod
# --------------------------------------------------------------------------- #
class TestSelectedMethod:
    def test_valid(self):
        m = SelectedMethod(tool_name="VASP", tool_version=6.3)
        assert m.tool_name == "VASP"
        assert m.tool_version == 6.3

    def test_tool_name_must_be_str(self):
        with pytest.raises(ValueError):
            SelectedMethod(tool_name=123, tool_version=6.3)

    def test_tool_name_must_not_be_none(self):
        with pytest.raises(ValueError):
            SelectedMethod(tool_name=None, tool_version=6.3)

    def test_tool_version_wrong_type_rejected(self):
        with pytest.raises(ValueError):
            SelectedMethod(tool_name="VASP", tool_version="6.3")

    def test_tool_version_must_not_be_none(self):
        with pytest.raises(ValueError):
            SelectedMethod(tool_name="VASP", tool_version=None)


# --------------------------------------------------------------------------- #
# ComputeEstimate
# --------------------------------------------------------------------------- #
class TestComputeEstimate:
    def test_valid(self):
        c = ComputeEstimate(cpu_hours=128.0)
        assert c.cpu_hours == 128.0

    def test_accepts_int(self):
        assert ComputeEstimate(cpu_hours=128).cpu_hours == 128

    def test_cpu_hours_must_be_number(self):
        with pytest.raises(ValueError):
            ComputeEstimate(cpu_hours="128")

    def test_cpu_hours_must_not_be_none(self):
        with pytest.raises(ValueError):
            ComputeEstimate(cpu_hours=None)


# --------------------------------------------------------------------------- #
# SlurmRequest
# --------------------------------------------------------------------------- #
class TestSlurmRequest:
    def test_valid(self):
        s = SlurmRequest(cpu_count=32, gpu_count=4, max_time=24.0, ram=64)
        assert s.cpu_count == 32
        assert s.gpu_count == 4
        assert s.max_time == 24.0
        assert s.ram == 64

    def test_cpu_count_must_be_int(self):
        with pytest.raises(ValueError):
            SlurmRequest(cpu_count=32.0, gpu_count=4, max_time=24.0, ram=64)

    def test_gpu_count_must_be_int(self):
        with pytest.raises(ValueError):
            SlurmRequest(cpu_count=32, gpu_count=None, max_time=24.0, ram=64)

    def test_max_time_must_be_number(self):
        with pytest.raises(ValueError):
            SlurmRequest(cpu_count=32, gpu_count=4, max_time="24", ram=64)

    def test_ram_must_be_int(self):
        with pytest.raises(ValueError):
            SlurmRequest(cpu_count=32, gpu_count=4, max_time=24.0, ram=64.5)


# --------------------------------------------------------------------------- #
# CostEstimate
# --------------------------------------------------------------------------- #
class TestCostEstimate:
    def test_valid(self):
        c = CostEstimate(min_tokens=1500, min_cost=12.5)
        assert c.min_tokens == 1500
        assert c.min_cost == 12.5

    def test_min_tokens_must_be_int(self):
        with pytest.raises(ValueError):
            CostEstimate(min_tokens=1500.0, min_cost=12.5)

    def test_min_cost_must_be_number(self):
        with pytest.raises(ValueError):
            CostEstimate(min_tokens=1500, min_cost="12.5")


# --------------------------------------------------------------------------- #
# ExecutionPlanMetadata
# --------------------------------------------------------------------------- #
class TestExecutionPlanMetadata:
    def test_valid(self):
        m = ExecutionPlanMetadata(
            timestamp="2026-06-15T12:00:00Z", goal_id="goal-001", candidate_rank=1
        )
        assert m.timestamp == "2026-06-15T12:00:00Z"
        assert m.goal_id == "goal-001"
        assert m.candidate_rank == 1

    def test_timestamp_must_be_str(self):
        with pytest.raises(ValueError):
            ExecutionPlanMetadata(timestamp=123, goal_id="goal-001", candidate_rank=1)

    def test_goal_id_must_be_str(self):
        with pytest.raises(ValueError):
            ExecutionPlanMetadata(
                timestamp="2026-06-15T12:00:00Z", goal_id=None, candidate_rank=1
            )

    def test_candidate_rank_must_be_int(self):
        with pytest.raises(ValueError):
            ExecutionPlanMetadata(
                timestamp="2026-06-15T12:00:00Z", goal_id="goal-001", candidate_rank="1"
            )


# --------------------------------------------------------------------------- #
# AcceptanceMetric
# --------------------------------------------------------------------------- #
class TestAcceptanceMetric:
    def test_valid(self):
        a = AcceptanceMetric(metric_name="energy", target_value=-5.2, tolerance=0.1)
        assert a.metric_name == "energy"
        assert a.target_value == -5.2
        assert a.tolerance == 0.1

    def test_metric_name_must_be_str(self):
        with pytest.raises(ValueError):
            AcceptanceMetric(metric_name=1, target_value=-5.2, tolerance=0.1)

    def test_target_value_must_be_number(self):
        with pytest.raises(ValueError):
            AcceptanceMetric(metric_name="energy", target_value="x", tolerance=0.1)

    def test_tolerance_must_be_number(self):
        with pytest.raises(ValueError):
            AcceptanceMetric(metric_name="energy", target_value=-5.2, tolerance=None)


# --------------------------------------------------------------------------- #
# ExecutionPlan (top-level)
# --------------------------------------------------------------------------- #
class TestExecutionPlan:
    def test_builds_from_example(self, example_dict):
        plan = ExecutionPlan(**example_dict)
        assert plan.selected_method.tool_name == example_dict["selected_method"]["tool_name"]

    def test_nested_types_are_coerced(self, example_dict):
        plan = ExecutionPlan(**example_dict)
        assert isinstance(plan.selected_method, SelectedMethod)
        assert isinstance(plan.compute_estimate, ComputeEstimate)
        assert isinstance(plan.slurm_request, SlurmRequest)
        assert isinstance(plan.cost_estimate, CostEstimate)
        assert isinstance(plan.metadata, ExecutionPlanMetadata)
        assert all(isinstance(a, AcceptanceMetric) for a in plan.acceptance_metrics)

    def test_accepts_nested_instances(self, example_dict):
        method = SelectedMethod(tool_name="VASP", tool_version=6.3)
        plan = ExecutionPlan(**with_field(example_dict, selected_method=method))
        assert plan.selected_method is method

    def test_selected_method_wrong_type_rejected(self, example_dict):
        with pytest.raises(ValueError):
            ExecutionPlan(**with_field(example_dict, selected_method=["VASP"]))

    def test_slurm_request_wrong_type_rejected(self, example_dict):
        with pytest.raises(ValueError):
            ExecutionPlan(**with_field(example_dict, slurm_request="8 cores"))

    def test_nested_validation_propagates(self, example_dict):
        with pytest.raises(ValueError):
            ExecutionPlan(
                **with_field(example_dict, cost_estimate={"min_tokens": 1500.0, "min_cost": 12.5})
            )

    def test_acceptance_metric_wrong_type_rejected(self, example_dict):
        with pytest.raises(ValueError):
            ExecutionPlan(**with_field(example_dict, acceptance_metrics=["energy"]))

    def test_empty_acceptance_metrics_allowed(self, example_dict):
        plan = ExecutionPlan(**with_field(example_dict, acceptance_metrics=[]))
        assert plan.acceptance_metrics == []

    def test_safety_notes_must_be_list_of_str(self, example_dict):
        with pytest.raises(ValueError):
            ExecutionPlan(**with_field(example_dict, safety_notes=[1, 2]))

    def test_empty_safety_notes_allowed(self, example_dict):
        plan = ExecutionPlan(**with_field(example_dict, safety_notes=[]))
        assert plan.safety_notes == []


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #
class TestRoundTrip:
    def test_asdict_roundtrips_example(self, example_dict):
        """asdict() of a plan built from the example reproduces every field the
        example specifies. The plan also carries optional calculator/material
        fields (added for calculator-driven runs like a DFT band gap); when the
        example omits them they round-trip as None."""
        plan = ExecutionPlan(**example_dict)
        dumped = asdict(plan)
        for key, value in example_dict.items():
            if key == "selected_method":
                for sub_key, sub_value in value.items():
                    assert dumped[key][sub_key] == sub_value
            else:
                assert dumped[key] == value
        # Optional additions default to None / empty when unset.
        assert dumped["selected_method"]["calculator"] is None
        assert dumped["selected_method"]["calculator_import"] is None
        assert dumped["selected_method"]["calculator_library"] is None
        assert dumped["selected_method"]["libraries"] == []
        assert dumped["target_system"] is None
        assert dumped["requested_property"] is None
