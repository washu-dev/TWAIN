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
    """With no agent the decomposer falls back to the deterministic canonical DAG."""
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


# A bespoke DAG an agent might return: deliberately different from the canonical
# template so a passing assertion proves the graph came from the agent, not a stub.
AGENT_GRAPH = {
    "goals": [
        {"id": "discover", "category": "discovery",
         "purpose": "Find a solubility predictor", "owner_agent": "method_discovery"},
        {"id": "prepare", "category": "data_preparation",
         "purpose": "Normalize the SMILES input", "owner_agent": "code_builder"},
        {"id": "run", "category": "execution",
         "purpose": "Run the predictor", "owner_agent": "execution_adapter"},
        {"id": "check", "category": "validation",
         "purpose": "Compare against the acceptance metrics", "owner_agent": "cross_validation",
         "acceptance_criteria": ["logS_MAE within 0.1 of 0.5"]},
    ],
    "edges": [
        {"source": "discover", "target": "prepare", "category": "seq"},
        {"source": "prepare", "target": "run", "category": "seq"},
        {"source": "run", "target": "check", "category": "seq"},
    ],
    "metadata": {"created_at": "2026-06-30T00:00:00Z", "source_intent_id": "testrun"},
}


def _machine_with_agent(tmp_path, agent):
    with patch.object(DataStorage, "load", return_value=None):
        m = SM.StateMachine(
            data_path=str(tmp_path / "state.json"), run_id="testrun", agent=agent
        )
    m.artifacts_dir = tmp_path
    return m


def test_decompose_uses_agent_to_build_dag(tmp_path):
    """When an agent is configured, decompose() builds the DAG it returns."""
    calls = []

    def agent(prompt):
        calls.append(prompt)
        return json.dumps(AGENT_GRAPH)

    machine = _machine_with_agent(tmp_path, agent)
    _seed_intent(machine, tmp_path)
    assert machine.decompose() == State.DISCOVER

    graph = machine._load_artifact("goal_graph")
    _validator("goal_graph.schema.json").validate(graph)
    # the persisted graph is the agent's bespoke DAG, not the canonical template
    assert [g["id"] for g in graph["goals"]] == ["discover", "prepare", "run", "check"]
    assert calls, "the agent should have been consulted for the decomposition"
    # the schema is anchored into the prompt the agent received
    assert "GoalGraphSchema" in calls[0]


def test_decompose_parses_fenced_json(tmp_path):
    """The agent's JSON is extracted even when wrapped in prose / ```json fences."""
    wrapped = (
        "Sure! Here is the decomposition you asked for:\n\n```json\n"
        + json.dumps(AGENT_GRAPH)
        + "\n```\nLet me know if you'd like changes."
    )
    machine = _machine_with_agent(tmp_path, lambda prompt: wrapped)
    _seed_intent(machine, tmp_path)
    assert machine.decompose() == State.DISCOVER

    graph = machine._load_artifact("goal_graph")
    _validator("goal_graph.schema.json").validate(graph)
    assert [g["id"] for g in graph["goals"]] == ["discover", "prepare", "run", "check"]


def test_decompose_falls_back_when_agent_output_invalid(tmp_path):
    """A malformed/cyclic agent decomposition degrades to the canonical DAG.

    The fallback must be observable: the raw response is dumped to a
    goal_graph_error artifact and the canonical graph records why it was used.
    """
    cyclic = {
        "goals": [
            {"id": "a", "category": "execution", "purpose": "x", "owner_agent": "o"},
            {"id": "b", "category": "validation", "purpose": "y", "owner_agent": "o"},
        ],
        "edges": [
            {"source": "a", "target": "b", "category": "seq"},
            {"source": "b", "target": "a", "category": "seq"},  # cycle => invalid DAG
        ],
        "metadata": {"created_at": "2026-06-30T00:00:00Z", "source_intent_id": "testrun"},
    }
    machine = _machine_with_agent(tmp_path, lambda prompt: json.dumps(cyclic))
    _seed_intent(machine, tmp_path)
    assert machine.decompose() == State.DISCOVER

    graph = machine._load_artifact("goal_graph")
    _validator("goal_graph.schema.json").validate(graph)
    assert [g["id"] for g in graph["goals"]] == [
        "discover_method", "run_execution", "validate_results"
    ]
    # the fallback is traceable: rationale explains it + raw output is captured
    assert "agent decomposition failed" in graph["metadata"]["rationale"]
    error = machine._load_artifact("goal_graph_error")
    assert error is not None
    assert error["raw_response"] == json.dumps(cyclic)


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


# -- the suggested CPU count is a heuristic, not a rule -----------------------

class TestSuggestedCpuCount:
    """One core per atom is a serviceable default for plane-wave DFT, not a law,
    so the ratio is tunable and the result is rounded to a width that decomposes
    cleanly instead of landing on whatever the atom count happens to be."""

    def test_it_scales_with_system_size(self):
        assert SM._suggest_cpu_count(2, 64) == 2
        assert SM._suggest_cpu_count(21, 64) == 20     # not 21
        assert SM._suggest_cpu_count(50, 64) == 48

    def test_it_never_asks_for_more_than_the_node_has(self):
        assert SM._suggest_cpu_count(200, 64) == 64
        assert SM._suggest_cpu_count(21, 8) == 8
        assert SM._suggest_cpu_count(1000, 2) == 2

    def test_it_floors_at_two(self):
        # k-point / domain parallelism needs a partner.
        assert SM._suggest_cpu_count(1, 64) == 2
        assert SM._suggest_cpu_count(0, 64) == 2

    def test_it_snaps_down_not_up(self):
        """Asking for more cores than the calculation can use just queues longer."""
        for atoms in range(3, 64):
            assert SM._suggest_cpu_count(atoms, 64) <= max(2, atoms)

    def test_the_ratio_is_tunable(self, monkeypatch):
        monkeypatch.setenv("TWAIN_CORES_PER_ATOM", "0.5")
        assert SM._suggest_cpu_count(40, 64) == 20
        monkeypatch.setenv("TWAIN_CORES_PER_ATOM", "2")
        assert SM._suggest_cpu_count(10, 64) == 20

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-3"])
    def test_a_bad_ratio_falls_back_to_one_per_atom(self, monkeypatch, bad):
        monkeypatch.setenv("TWAIN_CORES_PER_ATOM", bad)
        assert SM._suggest_cpu_count(21, 64) == 20
