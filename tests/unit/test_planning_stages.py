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


class TestPeriodicCoreFloor:
    """Atoms-per-core points the WRONG WAY for a periodic cell.

    The dominant parallel dimension of a plane-wave run is its k-points, and the
    mesh a cell needs scales inversely with the cell's size -- a small primitive
    cell wants a dense mesh and therefore has the most parallelism to spend.
    CaPt2 was the worst case: the atom count comes from the formula (CaPt2 -> 3,
    while the C15 primitive cell holds 6), so one core per atom asked for 3,
    snapped down to 2, and job 2625288 ran ~26x slower than the same study on 24
    cores (e496cf22).
    """

    def _capt2(self, **kw):
        return SM._suggest_cpu_count(3, 64, **kw)   # 3 == _atom_count("CaPt2")

    def test_the_capt2_regression(self):
        assert self._capt2() == 2                                    # the old answer
        assert self._capt2(periodic=True, scales_with_ranks=True) == 24

    def test_a_molecule_keeps_atom_scaling(self):
        """A gas-phase molecule has one k-point; extra ranks buy little."""
        assert self._capt2(periodic=False, scales_with_ranks=True) == 2

    def test_a_threads_only_calculator_is_not_widened(self):
        """Registry says extra ranks do nothing -- they would sit idle."""
        assert self._capt2(periodic=True, scales_with_ranks=False) == 2

    def test_it_is_a_floor_not_a_cap(self):
        """A big periodic cell still gets its larger atoms-derived width."""
        assert SM._suggest_cpu_count(
            200, 64, periodic=True, scales_with_ranks=True) == 64
        assert SM._suggest_cpu_count(
            40, 64, periodic=True, scales_with_ranks=True) == 40

    def test_it_still_respects_the_node(self):
        for ceiling in (2, 8, 16):
            assert SM._suggest_cpu_count(
                3, ceiling, periodic=True, scales_with_ranks=True) <= ceiling

    def test_the_floor_is_tunable(self, monkeypatch):
        monkeypatch.setenv("TWAIN_PERIODIC_MIN_CORES", "8")
        assert self._capt2(periodic=True, scales_with_ranks=True) == 8

    @pytest.mark.parametrize("bad", ["", "abc", "0", "-3", "1"])
    def test_a_bad_floor_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("TWAIN_PERIODIC_MIN_CORES", bad)
        assert self._capt2(periodic=True, scales_with_ranks=True) == 24

    @pytest.mark.parametrize("descriptors,expected", [
        ({"kind": "crystal", "formula": "CaPt2"}, True),
        ({"crystal": {"formula": "CaPt2", "space_group": "Fd-3m"}}, True),
        ({"kind": "CRYSTAL"}, True),
        ({"kind": "molecule", "molecule": {"name": "aspirin"}}, False),
        ({"formula": "C9H8O4"}, False),
        ({"crystal": {}}, False),
        ({}, False),
        (None, False),
        ("CaPt2", False),
    ])
    def test_is_periodic(self, descriptors, expected):
        assert SM._is_periodic(descriptors) is expected


class TestAPlanMustHaveSomethingToComputeWith:
    """A plan promising a CALCULATED property needs an engine to produce it.

    A NaCl2 heat of formation was planned as Pymatgen with calculator=null; codegen
    filled the gap by inventing MACE/CHGNet/M3GNet, none of which is provisioned or
    in requirements.txt, and the run died in EXECUTE on "No module named matgl"
    (Slurm job 2633871). BUILD's static check can stop the script computing, but it
    cannot conjure the engine the plan needed -- so the refusal belongs here.
    """

    def _needs(self):
        import json
        from pathlib import Path
        cfg = json.loads((Path(__file__).resolve().parents[2]
                          / "configs" / "discovery_intent_map.json").read_text())
        return cfg["capabilities_requiring_a_calculator"]

    def test_the_config_declares_which_capabilities_need_one(self):
        needs = self._needs()
        assert "electronic_structure" in needs and "quantum_chemistry" in needs
        # A descriptor, a fingerprint or a lookup answers this with libraries alone
        # -- the entire point of a library-only plan.
        assert "property_prediction" not in needs

    def test_drivers_and_self_contained_engines_are_distinguished(self):
        """The distinction the check turns on, straight from the registry.

        Capability tags cannot make it: ASE also claims electronic_structure, so
        believing them would refuse a legitimate self-contained PySCF band gap.
        """
        from method_discovery.calculator_registry import load_calculators
        drivers = {str(c.driver_library).strip().lower()
                   for c in load_calculators() if c.driver_library}
        assert "ase" in drivers and "pymatgen" in drivers
        assert "pyscf" not in drivers and "psi4" not in drivers


class TestARequestedCompositionIsSanityChecked:
    """NaCl2 cannot exist; a number computed for it means nothing."""

    def test_mp_absence_is_distinct_from_not_knowing(self, monkeypatch):
        from cross_validation import mp_reference as mp

        class Rester:
            def __init__(self, docs): self.docs = docs
            def search(self, **kw): return self.docs
            def __enter__(self): return self
            def __exit__(self, *e): return False

        assert mp.formula_is_known(
            "NaCl2", api_key="k", client_factory=lambda _k: Rester([])) is False
        assert mp.formula_is_known(
            "CaPt2", api_key="k", client_factory=lambda _k: Rester([{"m": 1}])) is True
        # No key, unusable input, or a failure -> None, never False.
        monkeypatch.delenv("MP_API_KEY", raising=False)
        assert mp.formula_is_known("NaCl2") is None
        assert mp.formula_is_known("", api_key="k") is None

        def boom(_k):
            raise ConnectionError("offline")
        assert mp.formula_is_known(
            "NaCl2", api_key="k", client_factory=boom) is None

    def test_it_never_raises(self):
        from cross_validation import mp_reference as mp
        for bad in (None, 42, "", "   "):
            assert mp.formula_is_known(bad, api_key="k") is None


# -- method fallback (#188) ---------------------------------------------------

def test_a_replan_after_a_failed_method_picks_another_and_says_so(machine, tmp_path):
    _seed_intent(machine, tmp_path)
    machine.decompose()
    machine.plan()
    first = machine._load_artifact("execution_plan")
    key = SM.StateMachine._method_key(first)
    machine.context.plan_approved = True
    machine.context.approved_plan = machine._plan_fingerprint()
    machine.context.failed_methods = [{"method": key, "calculator": None,
                                       "reason": "the script kept crashing", "last_attempt": 4}]
    assert machine.plan() == State.BUILD
    second = machine._load_artifact("execution_plan")
    _validator("execution_plan.schema.json").validate(second)
    assert SM.StateMachine._method_key(second) != key
    assert second["safety_notes"][0].startswith("Method changed:")
    assert "the script kept crashing" in second["safety_notes"][0]
    assert machine.context.plan_approved is False       # a new method needs a new approval


def test_no_method_left_stops_with_the_reason(machine, tmp_path):
    _seed_intent(machine, tmp_path)
    machine.decompose()
    from method_discovery.registry_loader import RegistryLoader
    machine.context.failed_methods = [{"method": e.name.lower(), "reason": "broken",
                                       "last_attempt": 1} for e in RegistryLoader().entries()]
    with pytest.raises(Exception, match="no other method fits"):
        machine.plan()


def test_the_deterministic_pick_says_why(machine, tmp_path):
    _seed_intent(machine, tmp_path)
    machine.decompose()
    machine.plan()
    notes = machine._load_artifact("execution_plan")["safety_notes"]
    assert any(n.startswith("Chosen by the discovery ranking:") for n in notes)


# -- prefer what has worked here (#188) -----------------------------------------

def test_the_llm_is_told_what_has_worked_here():
    from method_discovery import llm_discovery as LD
    kw = dict(objective="solubility", material="caffeine", domain=None,
              requested_property="logS", platform="linux-64", libraries=[], calculators=[])
    assert "WHAT HAS WORKED HERE" not in LD.build_prompt(**kw)
    prompt = LD.build_prompt(**kw, proven=[{"method": "rdkit", "calculator": None,
                                            "libraries": ["RDKit"], "completed": 3,
                                            "accepted": 2, "failed": 0}])
    assert "WHAT HAS WORKED HERE" in prompt and "rdkit: completed 3, accepted 2" in prompt


@pytest.fixture
def history():
    yield SM.use_method_history
    SM.use_method_history(None)


def test_the_deterministic_pick_prefers_a_proven_method(machine, tmp_path, history):
    _seed_intent(machine, tmp_path)
    machine.decompose()
    machine.discover()
    machine.plan()
    first = SM.StateMachine._method_key(machine._load_artifact("execution_plan"))
    asked = []
    # Any other candidate that is installed here can be the proven one.
    for cand in machine._load_artifact("discovery")["candidates"]:
        other = cand["name"].lower()
        if other == first:
            continue
        history(lambda prop, other=other: asked.append(prop) or [
            {"method": other, "calculator": None, "libraries": [cand["name"]],
             "completed": 4, "accepted": 3, "failed": 0}])
        machine.plan()
        plan = machine._load_artifact("execution_plan")
        if SM.StateMachine._method_key(plan) == other:
            break
    else:
        pytest.skip("no second installed candidate in this environment")
    assert any(n.startswith(f"Track record: {other} has completed 4") for n in plan["safety_notes"])
    # The seed asks for solubility, which has no canonical property: the run is
    # filed under its acceptance metric's family.
    assert asked[-1] == "aqueous_solubility"


def test_planning_goes_on_without_history(machine, tmp_path, history):
    _seed_intent(machine, tmp_path)
    machine.decompose()
    history(lambda prop: 1 / 0)
    assert machine.plan() == State.BUILD


def test_history_keys_are_normalized():
    assert SM.history_key("Band gap") == SM.history_key("band_gap") == "band_gap"
    assert SM.history_key(None, [{"metric_name": "logS_MAE"}]) == "aqueous_solubility"
    assert SM.history_key(None) == ""


def test_the_runner_keys_methods_the_way_planning_does():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from runner import method_history as MH
    for plan in ({"requested_property": "Band gap", "selected_method": {"calculator": "GPAW",
                                                                        "libraries": ["ASE"]}},
                 {"requested_property": "logS (25 C)", "selected_method": {"libraries": ["RDKit"]}},
                 {"selected_method": {"tool_name": "Psi4"},
                  "acceptance_metrics": [{"metric_name": "logS MAE"}]},
                 {"selected_method": {"libraries": ["xtb"]},
                  "acceptance_metrics": [{"metric_name": "aqueous_solubility_at_25C"}]},
                 {"selected_method": {"libraries": ["Psi4"]},
                  "acceptance_metrics": [{"metric_name": "standard_heat_of_formation_kJ_per_mol"}]},
                 {"selected_method": {"tool_name": "Psi4"}}):
        assert MH.method_key(plan) == SM.StateMachine._method_key(plan)
        assert MH.history_key(plan) == SM.history_key(plan.get("requested_property"),
                                                      plan.get("acceptance_metrics"))


# -- one job, one cluster env: aspirin solubility -----------------------------------

ASPIRIN_PICK = dict(libraries=["OpenMM", "RDKit", "OpenFF Toolkit", "ASE"], calculator="xtb",
                    calculator_library="ASE", reasoning="GFN2-xTB with ALPB water")


def test_a_toolset_is_trimmed_to_fit_one_env():
    # openff-toolkit is only in nwchem; xtb-python only in default.
    assert SM.StateMachine._fit_one_env(["xtb", "ASE", "OpenMM"],
                                        ["RDKit", "OpenFF Toolkit", "ASE"]) == ["OpenFF Toolkit"]
    assert SM.StateMachine._fit_one_env(["RDKit"], ["ASE"]) == []


def test_a_core_that_fits_no_env_is_none():
    assert SM.StateMachine._fit_one_env(["xtb", "OpenFF Toolkit"], []) is None


def _plan_with_pick(machine, tmp_path, monkeypatch, **pick):
    from method_discovery.llm_discovery import ToolRecommendation
    _seed_intent(machine, tmp_path)
    machine.decompose()
    machine.discover()
    machine.execute_slurm = True
    monkeypatch.setattr(machine, "_llm_recommend",
                        lambda *a, **k: ToolRecommendation(**{**ASPIRIN_PICK, **pick}))
    monkeypatch.setattr(machine, "_installed_libraries", lambda ranked, names: (list(names), []))
    assert machine.plan() == State.BUILD
    return machine._load_artifact("execution_plan")


def test_aspirin_solubility_plans_instead_of_stopping(machine, tmp_path, monkeypatch):
    plan = _plan_with_pick(machine, tmp_path, monkeypatch)
    libs = plan["selected_method"]["libraries"]
    assert "OpenFF Toolkit" not in libs and libs[0] == "OpenMM"
    assert SM.cluster_env_candidates(SM._selected_toolset(plan)) is not None
    assert any(n.startswith("Dropped from the toolset: OpenFF Toolkit") for n in plan["safety_notes"])


def test_an_unrunnable_core_falls_back_to_the_ranking(machine, tmp_path, monkeypatch):
    plan = _plan_with_pick(machine, tmp_path, monkeypatch,
                           libraries=["OpenFF Toolkit", "RDKit"], calculator="xtb",
                           calculator_library="OpenFF Toolkit")
    assert "OpenFF Toolkit" not in plan["selected_method"]["libraries"]
    assert any("used the discovery ranking's pick instead" in n for n in plan["safety_notes"])
    assert SM.cluster_env_candidates(SM._selected_toolset(plan)) is not None


def test_the_refusal_names_each_tool_once():
    gap = SM._cluster_env_gap(["xtb", "OpenMM", "OpenFF Toolkit", "OpenMM"])
    assert gap.count("OpenMM") == 1


# -- #222: the reviewer judges the method at PLAN ----------------------------------

def _review_machine(machine, tmp_path, monkeypatch, judge):
    _seed_intent(machine, tmp_path)
    machine.decompose()
    machine.discover()
    monkeypatch.delenv("TWAIN_OBSERVER_LLM", raising=False)
    prompts = []

    def agent(prompt):
        prompts.append(prompt)
        tools = prompt.split("Proposed method: ", 1)[1].split(".\n", 1)[0]
        return '{"verdict": "%s", "reason": "judged %s"}' % (judge(tools), tools)
    monkeypatch.setattr(machine, "_reviewer", lambda: agent)
    return prompts


def test_a_method_the_reviewer_rejects_is_replaced_before_approval(machine, tmp_path, monkeypatch):
    first = {}
    def judge(tools):
        first.setdefault("tools", tools)
        return "no" if tools == first["tools"] else "yes"
    _review_machine(machine, tmp_path, monkeypatch, judge)
    assert machine.plan() == State.BUILD
    plan = machine._load_artifact("execution_plan")
    (rejected,) = machine.context.failed_methods
    assert rejected["stage"] == "PLAN"
    assert SM.StateMachine._method_key(plan) != rejected["method"]
    assert plan["safety_notes"][0].startswith("Method replaced before you saw it:")


def test_when_the_reviewer_rejects_everything_the_researcher_decides(machine, tmp_path, monkeypatch):
    _review_machine(machine, tmp_path, monkeypatch, lambda tools: "no")
    assert machine.plan() == State.BUILD                 # never a dead end
    plan = machine._load_artifact("execution_plan")
    assert len([m for m in machine.context.failed_methods if m["stage"] == "PLAN"]) == 2
    assert plan["safety_notes"][0].startswith("Reviewer's concern:")


def test_advise_only_mode(machine, tmp_path, monkeypatch):
    monkeypatch.setenv("TWAIN_PLAN_REVIEW_REPLANS", "0")
    _review_machine(machine, tmp_path, monkeypatch, lambda tools: "no")
    machine.plan()
    assert machine.context.failed_methods == []
    assert machine._load_artifact("execution_plan")["safety_notes"][0].startswith("Reviewer's concern:")


def test_the_reviewer_and_the_method_pick_get_the_solubility_guidance(machine, tmp_path, monkeypatch):
    prompts = _review_machine(machine, tmp_path, monkeypatch, lambda tools: "yes")
    machine.plan()
    assert "ESOL" in prompts[0] and "hydration or solvation free energy alone" in prompts[0]
    from method_discovery import llm_discovery as LD
    assert "ESTABLISHED ROUTES" in LD.build_prompt(
        objective="x", material="y", domain=None, requested_property=None, platform="linux-64",
        libraries=[], calculators=[], guidance=SM.method_guidance("aqueous_solubility"))

# The metric names production actually filed runs under (#216's measurement).
@pytest.mark.parametrize("names, family", [
    (["aqueous_solubility_at_25C", "aqueous_solubility_log_mol_per_L", "aqueous_solubility_25C",
      "logS", "logS_MAE"], "aqueous_solubility"),
    (["standard_heat_of_formation_kJ_per_mol", "standard_enthalpy_of_formation_kJ_per_mol",
      "standard_heat_of_formation", "formation_energy_per_atom"], "formation_enthalpy"),
    (["band_gap", "Band gap (eV)", "bandgap"], "band_gap"),
])
def test_one_quantity_is_one_history_key(names, family):
    assert {SM.history_key(None, [{"metric_name": n}]) for n in names} == {family}


def test_a_name_no_family_knows_is_kept():
    assert SM.history_key(None, [{"metric_name": "Glass transition (K)"}]) == "glass_transition_k"
    assert SM.history_key("lattice_constant") == "lattice_constant"


def test_logs_doesnt_swallow_other_words():
    assert SM.history_key(None, [{"metric_name": "logistics_score"}]) == "logistics_score"
