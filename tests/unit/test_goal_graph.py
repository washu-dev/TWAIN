"""Unit tests for goal_decomposer.graph_builder and schemas/goal_graph.schema.json.

Covers:
  * Goal / Edge / GoalGraphMetadata / GoalGraph dataclass coercion + validation.
  * GraphBuilder cycle detection, topological sort determinism, degree maps,
    and reference verification.
  * JSON Schema 2020-12 validation against the shipped example.

Run from the repo root with:  pixi run pytest tests/unit/test_goal_graph.py
"""
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from goal_decomposer.graph_builder import (
    Edge,
    EdgeType,
    Goal,
    GoalGraph,
    GoalGraphMetadata,
    GoalType,
    GraphBuilder,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PATH = REPO_ROOT / "schemas" / "examples" / "goal_graph_molecular_solubility.json"
SCHEMA_PATH = REPO_ROOT / "schemas" / "goal_graph.schema.json"


@pytest.fixture
def example_dict():
    with open(EXAMPLE_PATH, "r") as f:
        return json.load(f)


@pytest.fixture
def schema_dict():
    with open(SCHEMA_PATH, "r") as f:
        return json.load(f)


def _minimal_graph_dict():
    """A tiny valid graph: a -> b -> c."""
    return {
        "goals": [
            {"id": "a", "category": "discovery", "purpose": "A", "owner_agent": "01"},
            {"id": "b", "category": "execution", "purpose": "B", "owner_agent": "08"},
            {"id": "c", "category": "validation", "purpose": "C", "owner_agent": "11"},
        ],
        "edges": [
            {"source": "a", "target": "b", "category": "seq"},
            {"source": "b", "target": "c", "category": "seq"},
        ],
        "metadata": {
            "created_at": "2026-06-16T12:00:00Z",
            "source_intent_id": "intent-test",
        },
    }


# --------------------------------------------------------------------------- #
# Goal
# --------------------------------------------------------------------------- #
class TestGoal:
    def test_valid_coerces_category_string(self):
        g = Goal(id="g1", category="discovery", purpose="d", owner_agent="04")
        assert g.category is GoalType.DISCOVERY

    def test_accepts_enum_directly(self):
        g = Goal(id="g1", category=GoalType.VALIDATION, purpose="d", owner_agent="11")
        assert g.category is GoalType.VALIDATION

    def test_empty_id_rejected(self):
        with pytest.raises(ValueError):
            Goal(id="", category="discovery", purpose="d", owner_agent="04")

    def test_invalid_category_rejected(self):
        with pytest.raises(ValueError):
            Goal(id="g1", category="not-a-category", purpose="d", owner_agent="04")

    def test_empty_purpose_rejected(self):
        with pytest.raises(ValueError):
            Goal(id="g1", category="discovery", purpose="", owner_agent="04")

    def test_non_string_acceptance_criterion_rejected(self):
        with pytest.raises(ValueError):
            Goal(
                id="g1",
                category="discovery",
                purpose="d",
                owner_agent="04",
                acceptance_criteria=["ok", 5],
            )


# --------------------------------------------------------------------------- #
# Edge
# --------------------------------------------------------------------------- #
class TestEdge:
    def test_valid(self):
        e = Edge(source="a", target="b", category="seq")
        assert e.category is EdgeType.SEQ
        assert e.condition is None

    def test_conditional_keeps_condition(self):
        e = Edge(source="a", target="b", category="conditional", condition="x > 0")
        assert e.category is EdgeType.CONDITIONAL
        assert e.condition == "x > 0"

    def test_self_loop_rejected(self):
        with pytest.raises(ValueError):
            Edge(source="a", target="a", category="seq")

    def test_invalid_category_rejected(self):
        with pytest.raises(ValueError):
            Edge(source="a", target="b", category="not-a-category")

    def test_empty_endpoint_rejected(self):
        with pytest.raises(ValueError):
            Edge(source="", target="b", category="seq")


# --------------------------------------------------------------------------- #
# GoalGraphMetadata
# --------------------------------------------------------------------------- #
class TestGoalGraphMetadata:
    def test_valid(self):
        m = GoalGraphMetadata(
            created_at="2026-06-16T12:00:00Z", source_intent_id="intent-1"
        )
        assert m.rationale is None

    def test_missing_source_intent_id_rejected(self):
        with pytest.raises(ValueError):
            GoalGraphMetadata(created_at="2026-06-16T12:00:00Z", source_intent_id="")


# --------------------------------------------------------------------------- #
# GoalGraph (top-level)
# --------------------------------------------------------------------------- #
class TestGoalGraph:
    def test_builds_from_example(self, example_dict):
        g = GoalGraph(**example_dict)
        assert len(g.goals) == 6
        assert isinstance(g.metadata, GoalGraphMetadata)
        assert all(isinstance(goal, Goal) for goal in g.goals)
        assert all(isinstance(edge, Edge) for edge in g.edges)

    def test_empty_goals_rejected(self, example_dict):
        bad = deepcopy(example_dict)
        bad["goals"] = []
        with pytest.raises(ValueError):
            GoalGraph(**bad)

    def test_duplicate_goal_ids_rejected(self):
        d = _minimal_graph_dict()
        d["goals"].append(
            {"id": "a", "category": "analysis", "purpose": "dup", "owner_agent": "10"}
        )
        with pytest.raises(ValueError, match="Duplicate goal id"):
            GoalGraph(**d)


# --------------------------------------------------------------------------- #
# GraphBuilder
# --------------------------------------------------------------------------- #
class TestGraphBuilder:
    def test_verify_references_accepts_valid(self):
        g = GoalGraph(**_minimal_graph_dict())
        GraphBuilder.verify_edge_references(g)  # should not raise

    def test_verify_references_rejects_dangling_target(self):
        d = _minimal_graph_dict()
        d["edges"].append({"source": "c", "target": "missing", "category": "seq"})
        g = GoalGraph(**d)
        with pytest.raises(ValueError, match="not a known goal id"):
            GraphBuilder.verify_edge_references(g)

    def test_in_and_out_degree(self):
        g = GoalGraph(**_minimal_graph_dict())
        assert GraphBuilder.in_degree(g) == {"a": 0, "b": 1, "c": 1}
        assert GraphBuilder.out_degree(g) == {"a": 1, "b": 1, "c": 0}

    def test_topological_sort_linear_chain(self):
        g = GoalGraph(**_minimal_graph_dict())
        assert GraphBuilder.topological_sort(g) == ["a", "b", "c"]

    def test_topological_sort_is_deterministic_across_runs(self, example_dict):
        g = GoalGraph(**example_dict)
        order1 = GraphBuilder.topological_sort(g)
        # Rebuild from the same dict and ensure identical ordering.
        g2 = GoalGraph(**deepcopy(example_dict))
        order2 = GraphBuilder.topological_sort(g2)
        assert order1 == order2

    def test_topological_sort_respects_dependencies(self, example_dict):
        g = GoalGraph(**example_dict)
        order = GraphBuilder.topological_sort(g)
        idx = {gid: i for i, gid in enumerate(order)}
        for e in g.edges:
            assert idx[e.source] < idx[e.target], (
                f"{e.source!r} must precede {e.target!r} in topological order"
            )

    def test_cycle_is_detected(self):
        d = _minimal_graph_dict()
        d["edges"].append({"source": "c", "target": "a", "category": "seq"})
        g = GoalGraph(**d)
        assert GraphBuilder.has_cycle(g) is True
        with pytest.raises(ValueError, match="not a DAG"):
            GraphBuilder.topological_sort(g)

    def test_validate_summary_shape(self, example_dict):
        g = GoalGraph(**example_dict)
        summary = GraphBuilder.validate(g)
        assert set(summary.keys()) == {"order", "in_degree", "out_degree", "roots", "leaves"}
        assert summary["roots"] == ["g1_discover_methods"]
        assert summary["leaves"] == ["g6_researcher_review"]
        assert summary["order"][0] == "g1_discover_methods"
        assert summary["order"][-1] == "g6_researcher_review"


# --------------------------------------------------------------------------- #
# JSON Schema validation
# --------------------------------------------------------------------------- #
class TestSchema:
    def test_schema_itself_is_well_formed(self, schema_dict):
        Draft202012Validator.check_schema(schema_dict)

    def test_example_validates_against_schema(self, schema_dict, example_dict):
        Draft202012Validator(schema_dict).validate(example_dict)

    def test_invalid_edge_category_fails_schema(self, schema_dict, example_dict):
        bad = deepcopy(example_dict)
        bad["edges"][0]["category"] = "not-a-category"
        with pytest.raises(Exception):
            Draft202012Validator(schema_dict).validate(bad)

    def test_missing_metadata_fails_schema(self, schema_dict, example_dict):
        bad = deepcopy(example_dict)
        del bad["metadata"]
        with pytest.raises(Exception):
            Draft202012Validator(schema_dict).validate(bad)


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #
def _strip_none(value):
    """Recursively drop keys whose value is None.

    The example legitimately omits optional fields (e.g. Edge.condition for
    non-conditional edges), but asdict() always emits them as None. For the
    roundtrip check we want "same data", not "same keys".
    """
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value


class TestRoundTrip:
    def test_asdict_roundtrips_example(self, example_dict):
        """asdict() of a graph built from the example reproduces the example
        (after stripping optional-None keys).

        GoalType/EdgeType are str-Enums, so the coerced enum members compare
        equal to their string values under dict equality.
        """
        g = GoalGraph(**example_dict)
        round_tripped = _strip_none(asdict(g))
        assert round_tripped == example_dict
