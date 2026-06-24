"""Unit tests for the DECOMPOSE / DISCOVER / PLAN state-machine handlers.

These three planning stages wire the control plane to the Epic 1/4 modules:
  * decompose() -> goal_decomposer.GoalGraph (validated DAG)
  * discover()  -> method_discovery ranking over the registry
  * plan()      -> plan_synthesizer.ExecutionPlan

The handlers are designed to run offline (no agent / no network) and to no-op
gracefully when their upstream artifact is missing, so they are exercised here
by seeding an intent_spec artifact and calling them directly.

Run from the repo root with:  pixi run pytest tests/unit/test_planning_stages.py
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO_ROOT / "modules" / "16_agent_mesh_control_plane"
sys.path.insert(0, str(MODULE_DIR))

from states import State  # noqa: E402
from crash_recovery import DataStorage  # noqa: E402
import statemachine as SM  # noqa: E402

SCHEMA_DIR = REPO_ROOT / "schemas"

INTENT = {
    "objective": "Predict the aqueous solubility of aspirin",
    "domain": "materials",
    "system_descriptors": {
        "formula": "C9H8O4",
        "molecule": {"name": "aspirin", "SMILES": "CC(=O)Oc1ccccc1C(=O)O"},
    },
    "acceptance_metrics": [
        {"metric_name": "logS_MAE", "target_value": 0.5, "tolerance": 0.1}
    ],
    "metadata": {"ambiguity": False, "confidence_scores": {"objective_confidence": 0.95}},
}


def _validator(schema_name):
    with open(SCHEMA_DIR / schema_name) as f:
        return Draft202012Validator(json.load(f))


@pytest.fixture
def machine(tmp_path):
    """A StateMachine built offline (no eager AgentInterface) writing to tmp_path."""
    with patch.object(DataStorage, "load", return_value=None):
        m = SM.StateMachine(data_path=str(tmp_path / "state.json"), run_id="testrun")
    m.artifacts_dir = tmp_path
    return m


def _seed_intent(machine, tmp_path):
    path = tmp_path / "intent_spec_seed.json"
    path.write_text(json.dumps(INTENT))
    machine.context.artifacts["intent_spec"] = str(path)
    return path


# ── construction ─────────────────────────────────────────────────────────────

def test_machine_constructs_offline(machine):
    """No network at construction: the agent must be lazy, not eager."""
    assert machine._agent is None
    assert machine.current_state == State.INTAKE


# ── decompose ────────────────────────────────────────────────────────────────

def test_decompose_writes_valid_goal_graph(machine, tmp_path):
    _seed_intent(machine, tmp_path)
    assert machine.decompose() == State.DISCOVER

    graph = machine._load_artifact("goal_graph")
    assert graph is not None
    _validator("goal_graph.schema.json").validate(graph)
    assert [g["id"] for g in graph["goals"]] == [
        "discover_method", "run_execution", "validate_results"
    ]
    # acceptance metrics from the intent flow into the validation goal
    assert any("logS_MAE" in c for c in graph["goals"][2]["acceptance_criteria"])


# ── discover ─────────────────────────────────────────────────────────────────

def test_discover_ranks_candidates(machine, tmp_path):
    _seed_intent(machine, tmp_path)
    assert machine.discover() == State.PLAN

    discovery = machine._load_artifact("discovery")
    assert discovery is not None
    assert discovery["query"]["input_format"] == "SMILES"
    assert "property_prediction" in discovery["query"]["capability_tags"]

    candidates = discovery["candidates"]
    assert 1 <= len(candidates) <= 3
    # ranked 1..n and sorted by descending composite
    assert [c["rank"] for c in candidates] == list(range(1, len(candidates) + 1))
    composites = [c["composite"] for c in candidates]
    assert composites == sorted(composites, reverse=True)


# ── plan ─────────────────────────────────────────────────────────────────────

def test_plan_writes_valid_execution_plan(machine, tmp_path):
    _seed_intent(machine, tmp_path)
    machine.decompose()  # so the plan can target the execution goal id
    assert machine.plan() == State.BUILD

    plan = machine._load_artifact("execution_plan")
    assert plan is not None
    _validator("execution_plan.schema.json").validate(plan)
    assert plan["metadata"]["goal_id"] == "run_execution"
    assert plan["metadata"]["candidate_rank"] == 1
    assert plan["selected_method"]["tool_name"]
    # acceptance metric carried through from the intent
    assert plan["acceptance_metrics"][0]["metric_name"] == "logS_MAE"


def test_plan_does_not_set_approval(machine, tmp_path):
    """plan() must leave the PLAN->BUILD approval gate untouched."""
    _seed_intent(machine, tmp_path)
    machine.plan()
    assert machine.context.plan_approved is False


# ── full planning sequence ───────────────────────────────────────────────────

def test_planning_sequence_chains_artifacts(machine, tmp_path):
    _seed_intent(machine, tmp_path)
    assert machine.decompose() == State.DISCOVER
    assert machine.discover() == State.PLAN
    assert machine.plan() == State.BUILD

    for name in ("goal_graph", "discovery", "execution_plan"):
        assert machine._load_artifact(name) is not None

    # the plan's chosen tool is the discovery #1 candidate
    discovery = machine._load_artifact("discovery")
    plan = machine._load_artifact("execution_plan")
    top = next(c for c in discovery["candidates"] if c["rank"] == 1)
    assert plan["selected_method"]["tool_name"] == top["name"]


# ── no-op safety when upstream artifact is absent ────────────────────────────

def test_handlers_noop_without_intent(machine):
    """With no intent_spec artifact the handlers don't write any output.

    decompose() routes back to INTAKE to obtain the missing intent; discover()
    and plan() advance without producing an artifact.
    """
    assert machine.decompose() == State.INTAKE
    assert machine.discover() == State.PLAN
    assert machine.plan() == State.BUILD
    assert machine.context.artifacts == {}
