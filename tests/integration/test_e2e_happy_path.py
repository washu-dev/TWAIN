"""Story 1.6: End-to-end happy-path contract integration test.

Walks a single request through the pipeline stages and asserts each stage's
artifact validates against its contract:

    request   -> IntentSpec
    plan      -> GoalGraph -> ExecutionPlan
    execution -> ResultPackage
    validation-> ValidationReport

Uses the shipped example documents as the per-stage artifacts, so this also
guards that the examples remain a coherent, validatable pipeline together.

Run from the repo root with:  pixi run pytest tests/integration/test_e2e_happy_path.py
"""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"
EXAMPLE_DIR = SCHEMA_DIR / "examples"

# Ordered pipeline stages: (stage name, schema file, example file).
PIPELINE = [
    ("request", "intent_spec.schema.json", "intent_spec_example.json"),
    ("plan:goals", "goal_graph.schema.json", "goal_graph_molecular_solubility.json"),
    ("plan:execution", "execution_plan.schema.json", "execution_plan_example.json"),
    ("execution", "result_package.schema.json", "result_package_example.json"),
    ("validation", "validation_report.schema.json", "validation_report_example.json"),
]


def _load(path):
    with open(path, "r") as f:
        return json.load(f)


@pytest.mark.parametrize("stage,schema_file,example_file", PIPELINE, ids=[s[0] for s in PIPELINE])
def test_stage_artifact_validates(stage, schema_file, example_file):
    schema = _load(SCHEMA_DIR / schema_file)
    artifact = _load(EXAMPLE_DIR / example_file)
    Draft202012Validator(schema).validate(artifact)


def test_full_happy_path_validates_all_contracts():
    """Every stage of the request->plan->execution->result->validation flow
    validates against its contract in one pass."""
    for stage, schema_file, example_file in PIPELINE:
        schema = _load(SCHEMA_DIR / schema_file)
        artifact = _load(EXAMPLE_DIR / example_file)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(artifact),
            key=lambda e: e.path,
        )
        assert not errors, f"stage {stage!r} failed contract: {[e.message for e in errors]}"


def test_pipeline_artifacts_are_consistent():
    """Light cross-stage linkage: the validation report concerns a result, and
    the execution plan references the goal stage."""
    execution_plan = _load(EXAMPLE_DIR / "execution_plan_example.json")
    result_package = _load(EXAMPLE_DIR / "result_package_example.json")
    validation_report = _load(EXAMPLE_DIR / "validation_report_example.json")

    # ExecutionPlan carries a goal reference and a ranked candidate.
    assert "goal_id" in execution_plan["metadata"]
    assert execution_plan["metadata"]["candidate_rank"] >= 1

    # The validated result and the report both identify themselves for provenance.
    assert result_package["metadata"]["ID"]
    assert validation_report["metadata"]["ID"]
    assert validation_report["acceptance_status"] in {"accepted", "rejected", "needs_review"}
