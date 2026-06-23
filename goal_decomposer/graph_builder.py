"""GoalGraph dataclasses and graph utilities for module 03_goal_decomposer.

Mirrors schemas/goal_graph.schema.json. Provides:
  * Goal / Edge / GoalGraphMetadata / GoalGraph dataclasses with nested-dict
    coercion (same pattern as intake.intent_spec and plan_synthesizer.execution_plan).
  * GraphBuilder with cycle detection, Kahn topological sort, in/out degree
    maps, and edge-reference verification.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Union


class GoalType(str, Enum):
    DISCOVERY = "discovery"
    MODEL_SELECTION = "model_selection"
    DATA_PREPARATION = "data_preparation"
    EXECUTION = "execution"
    VALIDATION = "validation"
    ANALYSIS = "analysis"
    REVIEW = "review"


class EdgeType(str, Enum):
    SEQ = "seq"
    PARALLEL = "parallel"
    CONDITIONAL = "conditional"


@dataclass
class Goal:
    id: str
    category: Union[str, GoalType]
    purpose: str
    owner_agent: str
    acceptance_criteria: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.id or type(self.id) is not str:
            raise ValueError("Goal id must be a non-empty str")
        if type(self.category) is str:
            try:
                self.category = GoalType(self.category)
            except ValueError:
                raise ValueError(
                    f"Invalid goal type. Viable options: {[t.value for t in GoalType]}"
                )
        elif not isinstance(self.category, GoalType):
            raise ValueError("Goal type must be a GoalType enum or matching string")
        if not self.purpose or type(self.purpose) is not str:
            raise ValueError("Goal description must be a non-empty str")
        if not self.owner_agent or type(self.owner_agent) is not str:
            raise ValueError("Goal owner_agent must be a non-empty str")
        if type(self.acceptance_criteria) is not list or any(
            type(c) is not str for c in self.acceptance_criteria
        ):
            raise ValueError("Goal acceptance_criteria must be a list of str")


@dataclass
class Edge:
    source: str
    target: str
    category: Union[str, EdgeType]
    condition: Optional[str] = None

    def __post_init__(self):
        if not self.source or type(self.source) is not str:
            raise ValueError("Edge source must be a non-empty str")
        if not self.target or type(self.target) is not str:
            raise ValueError("Edge target must be a non-empty str")
        if self.source == self.target:
            raise ValueError(f"Self-loop edge is not allowed (id={self.source!r})")
        if type(self.category) is str:
            try:
                self.category = EdgeType(self.category)
            except ValueError:
                raise ValueError(
                    f"Invalid edge type. Viable options: {[t.value for t in EdgeType]}"
                )
        elif not isinstance(self.category, EdgeType):
            raise ValueError("Edge type must be an EdgeType enum or matching string")
        if self.condition is not None and type(self.condition) is not str:
            raise ValueError("Edge condition must be a str when provided")


@dataclass
class GoalGraphMetadata:
    created_at: str
    source_intent_id: str
    rationale: Optional[str] = None

    def __post_init__(self):
        if not self.created_at or type(self.created_at) is not str:
            raise ValueError("GoalGraphMetadata created_at must be a non-empty str (ISO-8601 date-time)")
        if not self.source_intent_id or type(self.source_intent_id) is not str:
            raise ValueError("GoalGraphMetadata source_intent_id must be a non-empty str")
        if self.rationale is not None and type(self.rationale) is not str:
            raise ValueError("GoalGraphMetadata rationale must be a str when provided")


@dataclass
class GoalGraph:
    goals: List[Union[Goal, dict]]
    edges: List[Union[Edge, dict]]
    metadata: Union[GoalGraphMetadata, dict]

    def __post_init__(self):
        if type(self.goals) is not list or len(self.goals) == 0:
            raise ValueError("GoalGraph goals must be a non-empty list")
        coerced_goals: List[Goal] = []
        seen_ids: set = set()
        for g in self.goals:
            if type(g) is dict:
                g = Goal(**g)
            elif type(g) is not Goal:
                raise ValueError("GoalGraph goals items must be of type dict or Goal")
            if g.id in seen_ids:
                raise ValueError(f"Duplicate goal id: {g.id!r}")
            seen_ids.add(g.id)
            coerced_goals.append(g)
        self.goals = coerced_goals

        if type(self.edges) is not list:
            raise ValueError("GoalGraph edges must be a list")
        coerced_edges: List[Edge] = []
        for e in self.edges:
            if type(e) is dict:
                e = Edge(**e)
            elif type(e) is not Edge:
                raise ValueError("GoalGraph edges items must be of type dict or Edge")
            coerced_edges.append(e)
        self.edges = coerced_edges

        if type(self.metadata) is dict:
            self.metadata = GoalGraphMetadata(**self.metadata)
        elif type(self.metadata) is not GoalGraphMetadata:
            raise ValueError("GoalGraph metadata must be of type dict or GoalGraphMetadata")


class GraphBuilder:
    """Validation and traversal utilities over a GoalGraph.

    Stateless: every method takes a GoalGraph and returns a derived value.
    """

    @staticmethod
    def goal_ids(graph: GoalGraph) -> List[str]:
        return [g.id for g in graph.goals]

    @staticmethod
    def verify_edge_references(graph: GoalGraph) -> None:
        """Raise ValueError if any edge references a goal id that does not exist."""
        valid = set(GraphBuilder.goal_ids(graph))
        for e in graph.edges:
            if e.source not in valid:
                raise ValueError(f"Edge source {e.source!r} is not a known goal id")
            if e.target not in valid:
                raise ValueError(f"Edge target {e.target!r} is not a known goal id")

    @staticmethod
    def in_degree(graph: GoalGraph) -> Dict[str, int]:
        deg = {gid: 0 for gid in GraphBuilder.goal_ids(graph)}
        for e in graph.edges:
            if e.target in deg:
                deg[e.target] += 1
        return deg

    @staticmethod
    def out_degree(graph: GoalGraph) -> Dict[str, int]:
        deg = {gid: 0 for gid in GraphBuilder.goal_ids(graph)}
        for e in graph.edges:
            if e.source in deg:
                deg[e.source] += 1
        return deg

    @staticmethod
    def adjacency(graph: GoalGraph) -> Dict[str, List[str]]:
        """Return source -> sorted list of targets (sorted for deterministic traversal)."""
        adj: Dict[str, List[str]] = {gid: [] for gid in GraphBuilder.goal_ids(graph)}
        for e in graph.edges:
            adj[e.source].append(e.target)
        for src in adj:
            adj[src].sort()
        return adj

    @staticmethod
    def topological_sort(graph: GoalGraph) -> List[str]:
        """Return goal ids in a deterministic topological order (Kahn's algorithm).

        Ties are broken by goal id (lexicographic) so the output is reproducible.
        Raises ValueError if the graph is not a DAG or has dangling edges.
        """
        GraphBuilder.verify_edge_references(graph)
        in_deg = GraphBuilder.in_degree(graph)
        adj = GraphBuilder.adjacency(graph)

        ready: List[str] = sorted([gid for gid, d in in_deg.items() if d == 0])
        ordered: List[str] = []
        while ready:
            ready.sort()
            current = ready.pop(0)
            ordered.append(current)
            for nxt in adj[current]:
                in_deg[nxt] -= 1
                if in_deg[nxt] == 0:
                    ready.append(nxt)

        if len(ordered) != len(graph.goals):
            remaining = [gid for gid, d in in_deg.items() if d > 0]
            raise ValueError(
                f"GoalGraph is not a DAG; cycle detected among goals {sorted(remaining)}"
            )
        return ordered

    @staticmethod
    def has_cycle(graph: GoalGraph) -> bool:
        try:
            GraphBuilder.topological_sort(graph)
        except ValueError:
            return True
        return False

    @staticmethod
    def validate(graph: GoalGraph) -> Dict[str, object]:
        """Run all structural checks. Raises ValueError on the first failure.

        On success returns a summary dict for caller diagnostics:
          {
            "order": [...],            # topological order
            "in_degree": {...},
            "out_degree": {...},
            "roots":   [...],          # in_degree == 0
            "leaves":  [...],          # out_degree == 0
          }
        """
        order = GraphBuilder.topological_sort(graph)
        in_deg = GraphBuilder.in_degree(graph)
        out_deg = GraphBuilder.out_degree(graph)
        return {
            "order": order,
            "in_degree": in_deg,
            "out_degree": out_deg,
            "roots": sorted([gid for gid, d in in_deg.items() if d == 0]),
            "leaves": sorted([gid for gid, d in out_deg.items() if d == 0]),
        }
