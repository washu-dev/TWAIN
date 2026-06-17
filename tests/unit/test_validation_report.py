"""Unit tests for cross_validation.validation_report.

Exercises the ValidationReport dataclass and its nested types (Comparison,
ValidationReportMetadata): JSON Schema validation of the shipped example, valid
construction, type coercion of nested dicts, enum/range validation, rejection of
bad or missing-required input, and an asdict round-trip with no data loss.

Run from the repo root with:  pixi run pytest tests/unit/test_validation_report.py
"""
import json
from dataclasses import asdict
from pathlib import Path

import jsonschema
import pytest

from cross_validation.validation_report import (
    Comparison,
    ValidationReport,
    ValidationReportMetadata,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "validation_report.schema.json"
EXAMPLE_PATH = REPO_ROOT / "schemas" / "examples" / "validation_report_example.json"


@pytest.fixture
def schema():
    with open(SCHEMA_PATH, "r") as f:
        return json.load(f)


@pytest.fixture
def example_dict():
    with open(EXAMPLE_PATH, "r") as f:
        return json.load(f)


def with_field(base, **overrides):
    return {**base, **overrides}


# --------------------------------------------------------------------------- #
# JSON Schema validation
# --------------------------------------------------------------------------- #
class TestSchema:
    def test_example_validates_against_schema(self, schema, example_dict):
        jsonschema.validate(instance=example_dict, schema=schema)

    @pytest.mark.parametrize("field", ["comparison", "acceptance_status", "metadata"])
    def test_missing_top_level_required_rejected(self, schema, example_dict, field):
        bad = {k: v for k, v in example_dict.items() if k != field}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)

    def test_missing_nested_required_rejected(self, schema, example_dict):
        bad = with_field(example_dict, comparison={"literature_results": "x", "agreement": 0.9})
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)

    def test_bad_enum_rejected_by_schema(self, schema, example_dict):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(
                instance=with_field(example_dict, acceptance_status="maybe"), schema=schema
            )


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
class TestComparison:
    def test_valid(self):
        c = Comparison(literature_results="ref", agreement=0.9, difference_analysis="gap")
        assert c.agreement == 0.9

    def test_agreement_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            Comparison(literature_results="ref", agreement=1.5, difference_analysis="gap")

    def test_literature_results_must_be_str(self):
        with pytest.raises(ValueError):
            Comparison(literature_results=None, agreement=0.9, difference_analysis="gap")


# --------------------------------------------------------------------------- #
# ValidationReportMetadata
# --------------------------------------------------------------------------- #
class TestValidationReportMetadata:
    def test_valid(self):
        m = ValidationReportMetadata(ID="validation-001", timestamp="14:31:00")
        assert m.ID == "validation-001"

    def test_id_required(self):
        with pytest.raises(ValueError):
            ValidationReportMetadata(ID=None, timestamp="14:31:00")


# --------------------------------------------------------------------------- #
# ValidationReport (top-level)
# --------------------------------------------------------------------------- #
class TestValidationReport:
    def test_builds_from_example(self, example_dict):
        report = ValidationReport(**example_dict)
        assert report.acceptance_status == "accepted"

    def test_nested_types_are_coerced(self, example_dict):
        report = ValidationReport(**example_dict)
        assert isinstance(report.comparison, Comparison)
        assert isinstance(report.metadata, ValidationReportMetadata)

    @pytest.mark.parametrize("status", ["accepted", "rejected", "needs_review"])
    def test_valid_statuses(self, example_dict, status):
        report = ValidationReport(**with_field(example_dict, acceptance_status=status))
        assert report.acceptance_status == status

    def test_invalid_status_rejected(self, example_dict):
        with pytest.raises(ValueError):
            ValidationReport(**with_field(example_dict, acceptance_status="maybe"))

    def test_comparison_wrong_type_rejected(self, example_dict):
        with pytest.raises(ValueError):
            ValidationReport(**with_field(example_dict, comparison=["not", "a", "comparison"]))

    def test_metadata_required(self, example_dict):
        with pytest.raises(ValueError):
            ValidationReport(**with_field(example_dict, metadata=None))


# --------------------------------------------------------------------------- #
# Round-trip (no data loss)
# --------------------------------------------------------------------------- #
class TestRoundTrip:
    def test_asdict_roundtrips_example(self, example_dict):
        report = ValidationReport(**example_dict)
        assert asdict(report) == example_dict

    def test_report_from_result_baseline_is_schema_valid(self, schema, example_dict):
        """A report assembled from result + baseline parts validates and round-trips."""
        report = ValidationReport(
            comparison={
                "literature_results": "baseline-xyz",
                "agreement": 0.88,
                "difference_analysis": "within tolerance",
            },
            acceptance_status="needs_review",
            metadata={"ID": "validation-002", "timestamp": "15:00:00"},
        )
        jsonschema.validate(instance=asdict(report), schema=schema)