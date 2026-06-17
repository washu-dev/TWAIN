"""Unit tests for result_interpreter.result_package.

Exercises the ResultPackage dataclass and its nested types (Result, Certainty,
OutputLog, ResourceUsage, ToolUsed, Metadata): JSON Schema validation of the
shipped example, valid construction, type coercion of nested dicts, metric
extraction, validation/rejection of bad input, and an asdict round-trip with no
data loss.

Run from the repo root with:  pixi run pytest tests/unit/test_result_package.py
"""
import json
from dataclasses import asdict
from pathlib import Path

import jsonschema
import pytest

from result_interpreter.result_package import (
    Certainty,
    Metadata,
    OutputLog,
    ResourceUsage,
    Result,
    ResultPackage,
    ToolUsed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "result_package.schema.json"
EXAMPLE_PATH = REPO_ROOT / "schemas" / "examples" / "result_package_example.json"


@pytest.fixture
def schema():
    with open(SCHEMA_PATH, "r") as f:
        return json.load(f)


@pytest.fixture
def example_dict():
    """A fresh copy of the canonical example package for each test."""
    with open(EXAMPLE_PATH, "r") as f:
        return json.load(f)


def with_field(base, **overrides):
    """Return a shallow copy of `base` with `overrides` applied."""
    return {**base, **overrides}


# --------------------------------------------------------------------------- #
# JSON Schema validation
# --------------------------------------------------------------------------- #
class TestSchema:
    def test_example_validates_against_schema(self, schema, example_dict):
        jsonschema.validate(instance=example_dict, schema=schema)

    @pytest.mark.parametrize(
        "field", ["result", "output", "resource_usage", "metadata"]
    )
    def test_missing_top_level_required_rejected(self, schema, example_dict, field):
        bad = {k: v for k, v in example_dict.items() if k != field}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)

    def test_missing_nested_required_rejected(self, schema, example_dict):
        bad = with_field(
            example_dict,
            metadata={"timestamp": "14:30:05", "ID": "result-001"},  # no tools_used
        )
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)


# --------------------------------------------------------------------------- #
# Certainty
# --------------------------------------------------------------------------- #
class TestCertainty:
    def test_valid(self):
        c = Certainty(confidence_interval=[0.1, 0.9], expected=0.5, mean_squared_error=0.01)
        assert c.confidence_interval == [0.1, 0.9]

    def test_interval_must_have_two_elements(self):
        with pytest.raises(ValueError):
            Certainty(confidence_interval=[0.1], expected=0.5, mean_squared_error=0.01)

    def test_interval_must_be_ascending(self):
        with pytest.raises(ValueError):
            Certainty(confidence_interval=[0.9, 0.1], expected=0.5, mean_squared_error=0.01)

    def test_expected_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            Certainty(confidence_interval=[0.1, 0.9], expected=1.5, mean_squared_error=0.01)


# --------------------------------------------------------------------------- #
# ResourceUsage (all fields required)
# --------------------------------------------------------------------------- #
class TestResourceUsage:
    def _kwargs(self, **overrides):
        base = dict(
            total_cost=1.0, tokens_used=10, token_cost=0.1, cpu_hours=2.0,
            gpu_hours=0.5, slurm_cost=0.9, total_time="01:00:00",
            start_time="12:00:00", end_time="13:00:00",
        )
        return {**base, **overrides}

    def test_valid(self):
        ru = ResourceUsage(**self._kwargs())
        assert ru.tokens_used == 10

    def test_none_field_rejected(self):
        with pytest.raises(ValueError):
            ResourceUsage(**self._kwargs(cpu_hours=None))

    def test_negative_cost_rejected(self):
        with pytest.raises(ValueError):
            ResourceUsage(**self._kwargs(total_cost=-1.0))

    def test_tokens_used_must_be_int(self):
        with pytest.raises(ValueError):
            ResourceUsage(**self._kwargs(tokens_used=10.5))


# --------------------------------------------------------------------------- #
# Metadata
# --------------------------------------------------------------------------- #
class TestMetadata:
    def test_valid_and_coerces_tools(self):
        m = Metadata(
            timestamp="14:30:05",
            ID="result-001",
            tools_used=[{"name": "rdkit", "version": 2024.3}],
        )
        assert m.ID == "result-001"
        assert all(isinstance(t, ToolUsed) for t in m.tools_used)

    def test_id_required(self):
        with pytest.raises(ValueError):
            Metadata(timestamp="14:30:05", ID=None, tools_used=[])

    def test_tools_used_required(self):
        with pytest.raises(ValueError):
            Metadata(timestamp="14:30:05", ID="result-001", tools_used=None)


# --------------------------------------------------------------------------- #
# ResultPackage (top-level)
# --------------------------------------------------------------------------- #
class TestResultPackage:
    def test_builds_from_example(self, example_dict):
        pkg = ResultPackage(**example_dict)
        assert pkg.result.experiment_name == "aspirin_solubility"

    def test_nested_types_are_coerced(self, example_dict):
        pkg = ResultPackage(**example_dict)
        assert isinstance(pkg.result, Result)
        assert isinstance(pkg.result.certainty, Certainty)
        assert all(isinstance(o, OutputLog) for o in pkg.output)
        assert isinstance(pkg.resource_usage, ResourceUsage)
        assert isinstance(pkg.metadata, Metadata)

    def test_metric_extraction(self, example_dict):
        """The primary metric and uncertainty can be read off programmatically."""
        pkg = ResultPackage(**example_dict)
        assert pkg.result.certainty.expected == 0.2
        assert pkg.result.certainty.confidence_interval == [0.1, 0.3]
        assert pkg.result.certainty.mean_squared_error == 0.05
        assert pkg.result.exit_code == 0

    def test_resource_usage_now_required(self, example_dict):
        with pytest.raises(ValueError):
            ResultPackage(**with_field(example_dict, resource_usage=None))

    def test_metadata_now_required(self, example_dict):
        with pytest.raises(ValueError):
            ResultPackage(**with_field(example_dict, metadata=None))

    def test_output_must_be_list(self, example_dict):
        with pytest.raises(ValueError):
            ResultPackage(**with_field(example_dict, output="not-a-list"))


# --------------------------------------------------------------------------- #
# Round-trip (no data loss)
# --------------------------------------------------------------------------- #
class TestRoundTrip:
    def test_asdict_roundtrips_example(self, example_dict):
        pkg = ResultPackage(**example_dict)
        assert asdict(pkg) == example_dict

    def test_roundtrip_still_schema_valid(self, schema, example_dict):
        pkg = ResultPackage(**example_dict)
        jsonschema.validate(instance=asdict(pkg), schema=schema)
