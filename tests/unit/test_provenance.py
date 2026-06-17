"""Unit tests for Story 1.5: CorrectionPlan + ProvenanceEvent schemas and the
append-only provenance event log.

Covers:
  * JSON Schema 2020-12 validity of correction_plan + provenance_event schemas,
    and validation of the shipped examples.
  * EventLog append/read, input/output + chain hashing, tamper detection.
  * The headline acceptance criterion: replay produces identical artifacts
    (deterministic, and stable across a fresh EventLog over the same file).

Run from the repo root with:  pixi run pytest tests/unit/test_provenance.py
"""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from provenance_memory.event_log import EventLog, ProvenanceIntegrityError

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = REPO_ROOT / "schemas"
EXAMPLES = SCHEMAS / "examples"


def _load(path):
    with open(path, "r") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# CorrectionPlan schema
# --------------------------------------------------------------------------- #
class TestCorrectionPlanSchema:
    @pytest.fixture
    def schema(self):
        return _load(SCHEMAS / "correction_plan.schema.json")

    @pytest.fixture
    def example(self):
        return _load(EXAMPLES / "correction_plan_example.json")

    def test_schema_well_formed(self, schema):
        Draft202012Validator.check_schema(schema)

    def test_example_validates(self, schema, example):
        Draft202012Validator(schema).validate(example)

    def test_missing_diagnosis_rejected(self, schema, example):
        bad = deepcopy(example)
        del bad["diagnosis"]
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)

    def test_missing_fallback_strategy_rejected(self, schema, example):
        bad = deepcopy(example)
        del bad["fallback_strategy"]
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)

    def test_missing_iteration_count_rejected(self, schema, example):
        bad = deepcopy(example)
        del bad["metadata"]["iteration_count"]
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)

    def test_bad_target_rejected(self, schema, example):
        bad = deepcopy(example)
        bad["proposed_corrections"][0]["target"] = "resources"
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)

    def test_bad_modification_type_rejected(self, schema, example):
        bad = deepcopy(example)
        bad["proposed_corrections"][0]["modification_type"] = "teleport"
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)

    def test_confidence_out_of_range_rejected(self, schema, example):
        bad = deepcopy(example)
        bad["expected_gain"]["confidence"] = 1.5
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)

    def test_empty_corrections_rejected(self, schema, example):
        bad = deepcopy(example)
        bad["proposed_corrections"] = []
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)


# --------------------------------------------------------------------------- #
# ProvenanceEvent schema
# --------------------------------------------------------------------------- #
class TestProvenanceEventSchema:
    @pytest.fixture
    def schema(self):
        return _load(SCHEMAS / "provenance_event.schema.json")

    @pytest.fixture
    def example(self):
        return _load(EXAMPLES / "provenance_event_example.json")

    def test_schema_well_formed(self, schema):
        Draft202012Validator.check_schema(schema)

    def test_example_validates(self, schema, example):
        Draft202012Validator(schema).validate(example)

    def test_example_hashes_are_64_hex(self, example):
        assert len(example["hash"]) == 64
        assert len(example["input_hash"]) == 64
        assert len(example["output_hash"]) == 64

    def test_bad_event_type_rejected(self, schema, example):
        bad = deepcopy(example)
        bad["event_type"] = "deploy"
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)

    def test_missing_agent_id_rejected(self, schema, example):
        bad = deepcopy(example)
        del bad["agent_id"]
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)

    def test_missing_decision_rationale_rejected(self, schema, example):
        bad = deepcopy(example)
        del bad["decision_rationale"]
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)

    def test_negative_seq_rejected(self, schema, example):
        bad = deepcopy(example)
        bad["seq"] = -1
        with pytest.raises(ValidationError):
            Draft202012Validator(schema).validate(bad)


# --------------------------------------------------------------------------- #
# EventLog
# --------------------------------------------------------------------------- #
@pytest.fixture
def log(tmp_path):
    return EventLog(tmp_path / "provenance.jsonl")


def _seed(log):
    """Append three representative pipeline decisions; return their output artifacts."""
    steps = [
        ("request", "01_intake_nlu",
         {"raw_request": "predict solubility"},
         {"intent_id": "intent-1", "objective": "predict solubility"},
         "Parsed NL request into structured intent."),
        ("plan", "05_plan_synthesis",
         {"intent_id": "intent-1"},
         {"plan_id": "plan-1", "selected_method": "predictor-x"},
         "Selected top-ranked solubility predictor."),
        ("validate", "11_cross_validation",
         {"result_id": "result-1"},
         {"acceptance_status": "rejected", "gap": 0.42},
         "Result exceeded the literature baseline tolerance."),
    ]
    outputs = []
    for event_type, agent_id, inp, outp, rationale in steps:
        log.append(event_type, agent_id, inp, outp, rationale)
        outputs.append(outp)
    return outputs


class TestEventLogAppend:
    def test_empty_log_reads_empty(self, log):
        assert log.read_all() == []
        assert log.replay() == []

    def test_first_event_seq_zero_empty_prev_hash(self, log):
        rec = log.append("request", "01_intake_nlu", {"a": 1}, {"b": 2}, "because")
        assert rec["seq"] == 0
        assert rec["prev_hash"] == ""
        assert len(rec["hash"]) == 64
        assert len(rec["input_hash"]) == 64
        assert len(rec["output_hash"]) == 64

    def test_chain_links_prev_hash(self, log):
        first = log.append("request", "a", {"x": 1}, {"y": 2}, "r1")
        second = log.append("plan", "b", {"y": 2}, {"z": 3}, "r2")
        assert second["seq"] == 1
        assert second["prev_hash"] == first["hash"]

    def test_invalid_event_type_rejected(self, log):
        with pytest.raises(ValueError):
            log.append("deploy", "a", {"x": 1}, {"y": 2})

    def test_inputs_must_be_dict(self, log):
        with pytest.raises(ValueError):
            log.append("request", "a", ["nope"], {"y": 2})

    def test_outputs_must_be_dict(self, log):
        with pytest.raises(ValueError):
            log.append("request", "a", {"x": 1}, "nope")

    def test_appended_events_validate_against_schema(self, log):
        rec = log.append("request", "01_intake_nlu", {"x": 1}, {"y": 2}, "r")
        schema = _load(SCHEMAS / "provenance_event.schema.json")
        Draft202012Validator(schema).validate(rec)


class TestEventLogIntegrity:
    def test_verify_chain_passes_for_clean_log(self, log):
        _seed(log)
        assert log.verify_chain() is True

    def test_tampered_output_is_detected(self, log):
        _seed(log)
        records = log.read_all()
        records[1]["outputs"]["selected_method"] = "tampered"
        with open(log.path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        with pytest.raises(ProvenanceIntegrityError):
            log.verify_chain()

    def test_broken_link_is_detected(self, log):
        _seed(log)
        records = log.read_all()
        records[2]["prev_hash"] = "0" * 64
        with open(log.path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        with pytest.raises(ProvenanceIntegrityError):
            log.verify_chain()

    def test_replay_refuses_tampered_log(self, log):
        _seed(log)
        records = log.read_all()
        records[0]["outputs"]["objective"] = "something else"
        with open(log.path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        with pytest.raises(ProvenanceIntegrityError):
            log.replay()


class TestReplayProducesIdenticalArtifacts:
    """The headline Story 1.5 acceptance criterion."""

    def test_replay_matches_appended_outputs(self, log):
        outputs = _seed(log)
        assert log.replay() == outputs

    def test_replay_is_deterministic(self, log):
        _seed(log)
        assert log.replay() == log.replay()

    def test_replay_stable_across_fresh_instance(self, tmp_path):
        path = tmp_path / "provenance.jsonl"
        writer = EventLog(path)
        outputs = _seed(writer)
        # A brand-new EventLog reading the same file reconstructs identical artifacts.
        reader = EventLog(path)
        assert reader.replay() == outputs
        assert reader.replay() == writer.replay()

    def test_replayed_artifacts_are_independent_copies(self, log):
        _seed(log)
        artifacts = log.replay()
        artifacts[0]["objective"] = "mutated"
        # Mutating a replayed artifact must not affect a subsequent replay.
        assert log.replay()[0]["objective"] == "predict solubility"


# --------------------------------------------------------------------------- #
# Integration: real Story 1.4 artifacts flowing through the 1.5 provenance log
# --------------------------------------------------------------------------- #
def _result_package():
    """A schema-valid ResultPackage-shaped execution output (Story 1.4) that is
    then provenance-logged. Authored as a plain dict because the 1.4 dataclass's
    asdict() emits null optionals that result_package.schema.json rejects."""
    return {
        "result": {
            "experiment_name": "aspirin_solubility",
            "conclusion": "Predicted log-solubility 0.42 above baseline.",
            "exit_code": 0,
            "certainty": {
                "confidence_interval": [0.1, 0.3],
                "expected": 0.2,
                "mean_squared_error": 0.05,
            },
        },
        "output": [{"name": "run_log", "path_to_file": "/runs/aspirin-001/out.log"}],
        "resource_usage": {"total_cost": 1.25, "cpu_hours": 0.5},
        "metadata": {"ID": "result-aspirin-001", "timestamp": "2026-06-16T13:00:00Z"},
    }


def _validation_report():
    """A schema-valid, rejecting ValidationReport whose ID the CorrectionPlan
    references."""
    return {
        "comparison": {
            "literature_results": "aspirin logS = -1.72 (Wang 2009)",
            "agreement": 0.6,
            "difference_analysis": "0.42 above baseline; outside 15% tolerance.",
        },
        "acceptance_status": "rejected",
        "metadata": {"ID": "validation-aspirin-001", "timestamp": "2026-06-16T13:15:00Z"},
    }


class TestPipelineProvenanceIntegration:
    """Exercises 1.4 (ResultPackage/ValidationReport) + 1.5 (CorrectionPlan +
    EventLog) composing into one provenance-logged, replayable decision chain."""

    def test_execute_validate_correct_chain_replays_identically(self, log):
        result_pkg = _result_package()
        validation_report = _validation_report()
        correction_plan = _load(EXAMPLES / "correction_plan_example.json")

        log.append("execute", "08_execution_adapter",
                   {"plan_id": "plan-aspirin-001"}, result_pkg, "Ran the solubility predictor locally.")
        log.append("validate", "11_cross_validation",
                   result_pkg, validation_report, "Compared against the literature baseline.")
        log.append("correct", "12_self_correction_reflection",
                   validation_report, correction_plan, "Proposed corrections after rejection.")

        assert log.verify_chain() is True
        assert log.replay() == [result_pkg, validation_report, correction_plan]

    def test_logged_artifacts_validate_against_their_schemas(self, log):
        result_pkg = _result_package()
        validation_report = _validation_report()
        correction_plan = _load(EXAMPLES / "correction_plan_example.json")

        log.append("execute", "08_execution_adapter", {"plan_id": "p"}, result_pkg, "ran")
        log.append("validate", "11_cross_validation", result_pkg, validation_report, "compared")
        log.append("correct", "12_self_correction_reflection", validation_report, correction_plan, "proposed")

        replayed = log.replay()
        Draft202012Validator(_load(SCHEMAS / "result_package.schema.json")).validate(replayed[0])
        Draft202012Validator(_load(SCHEMAS / "validation_report.schema.json")).validate(replayed[1])
        Draft202012Validator(_load(SCHEMAS / "correction_plan.schema.json")).validate(replayed[2])

    def test_correction_plan_links_to_validation_report(self):
        validation_report = _validation_report()
        correction_plan = _load(EXAMPLES / "correction_plan_example.json")
        # The CorrectionPlan's validation_report_id resolves to the rejecting report.
        assert (
            correction_plan["metadata"]["validation_report_id"]
            == validation_report["metadata"]["ID"]
        )
