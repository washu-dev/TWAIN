"""Unit tests for the agent-mesh control-plane state machine.

Covers: state enum completeness, guard coverage, valid transitions,
invalid transitions, guard rejection, crash recovery round-trip,
liveness (no deadlocks), and known bugs in the current implementation.

Run from the repo root with:  pixi run pytest tests/unit/test_state_machine.py
"""
import copy
import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_DIR = Path(__file__).resolve().parents[2] / "modules" / "16_agent_mesh_control_plane"
sys.path.insert(0, str(MODULE_DIR))

from states import State, Context, InvalidTransition, GuardsBroken
from crash_recovery import DataStorage

# statemachine imports cleanly via the conftest package aliases + the MODULE_DIR
# path insert above (this is exactly how tests/unit/test_orchestrator.py imports
# it). Do NOT wrap this import in ``patch.dict(sys.modules, ...)``: on exit
# patch.dict purges every module imported *inside* the block -- including stdlib
# modules like ``urllib.error`` first imported transitively here -- which corrupts
# global import state for later test files (e.g. duplicate exception classes make
# ``except HTTPError`` miss in test_codegen). See the audit for the mechanism.
import statemachine as SM

GUARDS = SM.GUARDS
StateMachine = SM.StateMachine


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_machine(tmp_path, **ctx_overrides) -> StateMachine:
    """Build a StateMachine whose recovery file lives in tmp_path."""
    data_path = str(tmp_path / "state.json")
    with patch.object(DataStorage, "load", return_value=None):
        m = StateMachine(data_path=data_path)
    m.context = Context(**ctx_overrides)
    return m


# A schema-shaped IntentSpec the fake agent emits; high confidence by default so
# clarify() marks the run clarified and advances to DECOMPOSE.
VALID_INTENT = {
    "objective": "Predict the aqueous solubility of aspirin",
    "domain": "materials",
    "system_descriptors": {
        "formula": "C9H8O4",
        "molecule": {"name": "aspirin", "SMILES": "CC(=O)Oc1ccccc1C(=O)O"},
    },
    "acceptance_metrics": [
        {"metric_name": "logS", "target_value": -1.7, "tolerance": 0.5}
    ],
    "metadata": {
        "ambiguity": False,
        "confidence_scores": {
            "objective_confidence": 0.95,
            "domain_confidence": 0.95,
            "name_confidence": 0.95,
            "SMILES_confidence": 0.95,
            "formula_confidence": 0.95,
        },
    },
}


class FakeAgent:
    """Deterministic, offline stand-in for AgentInterface.

    Returns clarifying questions for a clarification prompt and the canned
    IntentSpec JSON for any generate/rewrite prompt, matching the
    ``resp["content"][0]["text"]`` shape the handlers expect.
    """

    def __init__(self, low_confidence: bool = False):
        intent = copy.deepcopy(VALID_INTENT)
        if low_confidence:
            intent["metadata"]["confidence_scores"]["SMILES_confidence"] = 0.4
        self._intent_json = json.dumps(intent)

    def call_agent(self, prompt, **kwargs):
        if "questions" in str(prompt).lower():
            return {"content": [{"text": "1. Which solvent and temperature?"}]}
        return {"content": [{"text": self._intent_json}]}


class TruncatingAgent(FakeAgent):
    """First JSON reply is cut mid-string (the 1024-token truncation failure
    mode); subsequent replies are whole. The intake off-topic filter prompt is
    answered in-domain without counting, so ``calls`` counts JSON calls only."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0

    def call_agent(self, prompt, **kwargs):
        if "intake filter" in str(prompt):
            return {"content": [{"text": "SIMULATION"}]}
        self.calls += 1
        if self.calls == 1:
            return {"content": [{"text": self._intent_json[:80]}]}
        return super().call_agent(prompt, **kwargs)


def _offline_machine(tmp_path, *, agent=None, **ctx_overrides) -> StateMachine:
    """A StateMachine wired with a fake agent + tmp artifacts dir, so the
    interactive intake/clarify handlers run fully offline (still needs
    ``patch('builtins.input', ...)`` around any call that reads input)."""
    data_path = str(tmp_path / "state.json")
    with patch.object(DataStorage, "load", return_value=None):
        m = StateMachine(data_path=data_path, run_id="t", agent=agent or FakeAgent())
    m.artifacts_dir = tmp_path
    if ctx_overrides:
        m.context = Context(**ctx_overrides)
    return m


HAPPY_CONTEXT = dict(
    clarified=True,
    plan_approved=True,
    execution_status=True,
    validation_result="accepted",
)


EXPECTED_STATES = {
    "INTAKE", "CLARIFY", "DECOMPOSE", "DISCOVER", "PLAN", "BUILD", "REPAIR",
    "EXECUTE", "INTERPRET", "VALIDATE", "ACCEPT", "CORRECT", "REPLAN",
    "TERMINATE",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. State enum
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateEnum:
    def test_all_required_states_present(self):
        actual = {s.name for s in State}
        assert EXPECTED_STATES <= actual

    def test_no_extra_states(self):
        actual = {s.name for s in State}
        assert actual == EXPECTED_STATES


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Guard table completeness
# ═══════════════════════════════════════════════════════════════════════════════

class TestGuardTable:
    EXPECTED_TRANSITIONS = {
        (State.INTAKE, State.CLARIFY),
        (State.INTAKE, State.TERMINATE),  # off-topic decline at intake
        (State.CLARIFY, State.DECOMPOSE),
        (State.CLARIFY, State.CLARIFY),  # clarification Q&A self-loop
        (State.DECOMPOSE, State.INTAKE),  # no intent yet -> go back to intake
        (State.DECOMPOSE, State.DISCOVER),
        (State.DISCOVER, State.PLAN),
        (State.PLAN, State.BUILD),
        (State.BUILD, State.REPAIR),
        (State.REPAIR, State.EXECUTE),
        (State.EXECUTE, State.INTERPRET),
        (State.INTERPRET, State.VALIDATE),
        (State.VALIDATE, State.ACCEPT),
        (State.VALIDATE, State.REPLAN),
        (State.VALIDATE, State.CORRECT),
        (State.ACCEPT, State.TERMINATE),
        (State.REPLAN, State.PLAN),
        (State.CORRECT, State.BUILD),
    }

    def test_all_expected_transitions_have_guards(self):
        missing = self.EXPECTED_TRANSITIONS - set(GUARDS.keys())
        assert missing == set(), f"Missing guard entries: {missing}"

    def test_no_unexpected_transitions(self):
        extra = set(GUARDS.keys()) - self.EXPECTED_TRANSITIONS
        assert extra == set(), f"Unexpected guard entries: {extra}"

    def test_correct_to_build_transition_exists(self):
        assert (State.CORRECT, State.BUILD) in GUARDS


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Valid transitions (happy path)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHappyPath:
    def test_full_pipeline_accept(self, tmp_path):
        # run() advances one transition at a time (the orchestrator loops it), so
        # we drive it to a fixed point here. A fake agent + canned input let the
        # interactive intake/clarify stages run offline.
        m = _offline_machine(tmp_path, **HAPPY_CONTEXT)
        with patch.object(m.storage, "commit"), \
                patch("builtins.input", return_value="predict the solubility of aspirin"):
            for _ in range(len(State) + 5):
                if m.current_state == State.TERMINATE:
                    break
                m.run()
        assert m.current_state == State.TERMINATE

    def test_handler_returns_match_guards(self, tmp_path):
        handlers_next = {
            "intake": State.CLARIFY,
            "clarify": State.DECOMPOSE,
            "decompose": State.DISCOVER,
            "discover": State.PLAN,
            "plan": State.BUILD,
            "build": State.REPAIR,
            "repair": State.EXECUTE,
            "execute": State.INTERPRET,
            "interpret": State.VALIDATE,
            "validate": State.ACCEPT,
            "accept": State.TERMINATE,
            "correct": State.BUILD,
            "replan": State.PLAN,
        }
        # Called in order: intake() writes the intent_spec that clarify()/
        # decompose() then consume.
        m = _offline_machine(tmp_path)
        with patch("builtins.input", return_value="predict the solubility of aspirin"):
            for name, expected_next in handlers_next.items():
                handler = getattr(m, name)
                assert handler() == expected_next, \
                    f"{name}() should return {expected_next}"


# ═══════════════════════════════════════════════════════════════════════════════
# 3b. BUILD stage produces a runnable RunBundle (Story 5.1)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildProducesRunBundle:
    """build() must turn the execution_plan into a materialized, self-contained
    RunBundle the execution adapter can run without manual edits."""

    PLAN = {
        "selected_method": {"tool_name": "Pymatgen", "tool_version": 2024.1},
        "compute_estimate": {"cpu_hours": 1.0},
        "slurm_request": {"cpu_count": 8, "gpu_count": 1, "max_time": 24.0, "ram": 16},
        "cost_estimate": {"min_tokens": 100, "min_cost": 1.0},
        "metadata": {"timestamp": "2026-06-15T12:00:00Z", "goal_id": "g1", "candidate_rank": 1},
        "acceptance_metrics": [{"metric_name": "density", "target_value": 7.8, "tolerance": 0.5}],
        "safety_notes": ["Verify SLURM partition limits"],
    }

    def test_build_writes_bundle_and_records_artifacts(self, tmp_path):
        m = _offline_machine(tmp_path)
        m.context.artifacts["execution_plan"] = m._write_artifact("execution_plan", self.PLAN)

        next_state = m.build()

        assert next_state == State.REPAIR
        bundle_dir = Path(m.context.artifacts["run_bundle"])
        assert bundle_dir.is_dir()
        for filename in ("main.py", "config.yaml", "requirements.txt", "inline_tests.py"):
            assert (bundle_dir / filename).is_file(), f"bundle missing {filename}"
        assert m.context.artifacts["script"].endswith("main.py")
        # the generated entrypoint must be syntactically valid Python
        compile((bundle_dir / "main.py").read_text(encoding="utf-8"), "main.py", "exec")

    def test_build_without_plan_is_a_noop_to_repair(self, tmp_path):
        # No execution_plan artifact -> build() must not raise; it advances anyway
        # (through REPAIR, which also no-ops with no bundle).
        m = _offline_machine(tmp_path)
        assert m.build() == State.REPAIR
        assert "run_bundle" not in m.context.artifacts

    def test_repair_without_bundle_is_a_noop_to_execute(self, tmp_path):
        # No run_bundle artifact -> repair() passes straight through to EXECUTE.
        m = _offline_machine(tmp_path)
        assert m.repair() == State.EXECUTE
        assert "repair_report" not in m.context.artifacts


# ═══════════════════════════════════════════════════════════════════════════════
# 3c. EXECUTE stage runs the RunBundle via the adapter (Story 5.2)
# ═══════════════════════════════════════════════════════════════════════════════

class _FakeAdapter:
    """Records execute() calls and returns a canned result."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def execute(self, bundle, **kwargs):
        self.calls.append((bundle, kwargs))
        return self._result


class TestExecuteRunsBundle:
    PLAN = {
        "selected_method": {"tool_name": "Pymatgen", "tool_version": 2024.1},
        "compute_estimate": {"cpu_hours": 1.0},
        "slurm_request": {"cpu_count": 8, "gpu_count": 1, "max_time": 24.0, "ram": 16},
        "cost_estimate": {"min_tokens": 100, "min_cost": 1.0},
        "metadata": {"timestamp": "2026-06-15T12:00:00Z", "goal_id": "g1", "candidate_rank": 1},
        "acceptance_metrics": [{"metric_name": "density", "target_value": 7.8, "tolerance": 0.5}],
        "safety_notes": ["Verify SLURM partition limits"],
        # A real structure the caller resolved upstream (TWAIN never fabricates one),
        # so the material-aware Pymatgen bundle has something legitimate to analyse.
        "target_system": {"structure": {
            "lattice": [[4.0, 0.0, 0.0], [0.0, 4.0, 0.0], [0.0, 0.0, 4.0]],
            "atoms": [
                {"species": "Na", "coordinates": [0.0, 0.0, 0.0]},
                {"species": "Cl", "coordinates": [0.5, 0.5, 0.5]},
            ],
            "coordinateSystem": "fractional",
        }},
    }

    def _machine_with_bundle(self, tmp_path):
        m = _offline_machine(tmp_path)
        bundle_dir = Path(m.artifacts_dir) / f"run_bundle_{m.run_id}"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "main.py").write_text("print('{}')\n", encoding="utf-8")
        m.context.artifacts["run_bundle"] = str(bundle_dir)
        return m, bundle_dir

    def test_disabled_is_a_noop(self, tmp_path):
        m, _ = self._machine_with_bundle(tmp_path)
        fake = _FakeAdapter(None)  # would explode if used
        m._execution_adapter = fake
        m.execute_locally = False
        assert m.execute() == State.INTERPRET
        assert fake.calls == []                         # adapter never invoked
        assert "execution_result" not in m.context.artifacts

    def test_runs_bundle_and_records_result(self, tmp_path):
        from execution_adapter.execution_result import ExecutionResult, ExecutionStatus

        m, bundle_dir = self._machine_with_bundle(tmp_path)
        result = ExecutionResult(
            status=ExecutionStatus.SUCCESS, exit_code=0, stdout="{}", peak_memory_mb=12.5
        )
        fake = _FakeAdapter(result)
        m._execution_adapter = fake
        m.execute_locally = True

        next_state = m.execute()

        assert next_state == State.INTERPRET
        assert m.context.execution_status is True
        # the adapter was handed the bundle dir + our run options, incl. the
        # session id so the workdir is named exec_<run_id>
        assert fake.calls[0][0] == str(bundle_dir)
        assert fake.calls[0][1]["run_smoke"] is True
        assert fake.calls[0][1]["run_id"] == m.run_id
        # an execution_result artifact was written and is JSON-loadable
        result_path = m.context.artifacts["execution_result"]
        data = json.loads(Path(result_path).read_text(encoding="utf-8"))
        assert data["status"] == "success" and data["peak_memory_mb"] == 12.5

    def test_failed_run_raises_actionable_error(self, tmp_path):
        from execution_adapter.execution_result import ExecutionResult, ExecutionStatus

        m, _ = self._machine_with_bundle(tmp_path)
        m._execution_adapter = _FakeAdapter(
            ExecutionResult(status=ExecutionStatus.DEPENDENCY_ERROR, exit_code=1)
        )
        m.execute_locally = True
        # A failed real execution surfaces an actionable error (with the real
        # reason + next steps) instead of letting the EXECUTE->INTERPRET guard
        # fail downstream as an opaque "incomplete context". execution_status is
        # still recorded False before raising, for provenance/resume.
        with pytest.raises(Exception):
            m.execute()
        assert m.context.execution_status is False

    def test_no_bundle_is_a_noop(self, tmp_path):
        m = _offline_machine(tmp_path)
        m.execute_locally = True
        m._execution_adapter = _FakeAdapter(None)
        assert m.execute() == State.INTERPRET  # no run_bundle artifact -> no-op
        assert "execution_result" not in m.context.artifacts

    def test_real_pymatgen_bundle_executes_end_to_end(self, tmp_path):
        pytest.importorskip("pymatgen")
        m = _offline_machine(tmp_path)
        m.context.artifacts["execution_plan"] = m._write_artifact("execution_plan", self.PLAN)
        m.build()  # writes a real RunBundle for Pymatgen
        m.execute_locally = True

        assert m.execute() == State.INTERPRET
        assert m.context.execution_status is True
        data = json.loads(Path(m.context.artifacts["execution_result"]).read_text(encoding="utf-8"))
        assert data["status"] == "success"
        assert data["peak_memory_mb"] is not None  # metrics captured
        # the working dir is named from the session id (not a random string)
        assert data["artifacts_dir"].endswith(f"exec_{m.run_id}")
        assert (Path(data["artifacts_dir"]) / "results.csv").is_file()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Guard rejection (invalid context)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentJsonRobustness:
    def test_intake_retries_truncated_agent_json(self, tmp_path):
        # A reply cut mid-string (token-cap truncation) must trigger one clean
        # retry instead of surfacing JSONDecodeError from the stage handler.
        agent = TruncatingAgent()
        m = _offline_machine(tmp_path, agent=agent)
        with patch("builtins.input", return_value="predict solubility"):
            assert m.intake() == State.CLARIFY
        assert agent.calls == 2
        assert m._load_artifact("intent_spec")["objective"] == VALID_INTENT["objective"]

    def test_agent_json_raises_after_retries_exhausted(self, tmp_path):
        m = _offline_machine(tmp_path, agent=lambda prompt: '{"unterminated": "trunca')
        with pytest.raises(json.JSONDecodeError):
            m._agent_json("prompt", retries=1)


class TestOffTopicDecline:
    """The intake filter declines non-simulation asks before intent extraction."""

    @staticmethod
    def _routing_agent(verdict):
        """Answers the intake-filter prompt with ``verdict``; everything else
        gets the canned in-domain IntentSpec."""
        def agent(prompt):
            if "intake filter" in str(prompt):
                return verdict
            return json.dumps(VALID_INTENT)
        return agent

    def test_off_topic_request_terminates_with_a_decline(self, tmp_path):
        m = _offline_machine(tmp_path, agent=self._routing_agent("OFF_TOPIC: cryptocurrency"))
        m._request = "explain how bitcoin mining works"
        assert m.intake() == State.TERMINATE
        declined = m._load_artifact("declined")
        assert declined["category"] == "cryptocurrency"
        assert "materials-science" in declined["message"]
        # Nothing was extracted: the run never paid for the intent call.
        assert m._load_artifact("intent_spec") is None

    def test_in_domain_request_proceeds_to_clarify(self, tmp_path):
        m = _offline_machine(tmp_path, agent=self._routing_agent("SIMULATION"))
        m._request = "band gap of silicon with PBE"
        assert m.intake() == State.CLARIFY
        assert "declined" not in m.context.artifacts
        assert m._load_artifact("intent_spec")["objective"] == VALID_INTENT["objective"]

    def test_filter_fails_open_on_agent_error(self, tmp_path):
        # A broken filter must never block real work: an agent error during
        # classification means "proceed" (the JSON call still succeeds).
        def flaky(prompt):
            if "intake filter" in str(prompt):
                raise RuntimeError("LLM down")
            return json.dumps(VALID_INTENT)

        m = _offline_machine(tmp_path, agent=flaky)
        m._request = "band gap of silicon"
        assert m.intake() == State.CLARIFY

    def test_unparseable_verdict_fails_open(self, tmp_path):
        m = _offline_machine(tmp_path, agent=self._routing_agent("I think maybe not?"))
        m._request = "band gap of silicon"
        assert m.intake() == State.CLARIFY

    def test_verdict_without_category_gets_a_generic_one(self, tmp_path):
        m = _offline_machine(tmp_path, agent=self._routing_agent("OFF_TOPIC"))
        m._request = "write me a poem"
        assert m.intake() == State.TERMINATE
        assert m._load_artifact("declined")["category"] == \
            "something other than a simulation"

    def test_intake_to_terminate_is_a_legal_transition(self, tmp_path):
        m = _offline_machine(tmp_path, agent=self._routing_agent("OFF_TOPIC: finance"))
        m._request = "should I invest in gold ETFs"
        m.run()  # drives intake through the guard table -- must not raise
        assert m.current_state == State.TERMINATE


class TestGuardRejection:
    def test_clarify_loops_when_not_confident(self, tmp_path):
        # Low-confidence intent: clarify() must stay in the CLARIFY Q&A loop
        # (return CLARIFY, leave clarified False) rather than advance to DECOMPOSE.
        m = _offline_machine(tmp_path, agent=FakeAgent(low_confidence=True))
        with patch("builtins.input", return_value="answer"):
            m.intake()                      # seed a low-confidence intent_spec
            next_state = m.clarify()
        assert next_state == State.CLARIFY
        assert m.context.clarified is False

    def test_clarify_force_continues_after_max_rounds(self, tmp_path):
        # Persistently low-confidence intent: clarify() must not self-loop forever.
        # It is bounded to max_clarify_rounds rounds, then force-continues to
        # DECOMPOSE on the best-effort spec (max_clarify_rounds is now honoured).
        m = _offline_machine(tmp_path, agent=FakeAgent(low_confidence=True))
        m.max_clarify_rounds = 2
        with patch("builtins.input", return_value="answer"):
            m.intake()
            assert m.clarify() == State.CLARIFY      # round 1: still below threshold
            assert m.context.clarified is False
            assert m.clarify() == State.DECOMPOSE    # round 2 hits the bound
        assert m.context.clarified is True

    def test_plan_to_build_allowed_without_approval(self, tmp_path):
        # PLAN->BUILD only *reaches* the approval-gate state (plan generated,
        # nothing built), so it needs no approval; the real gate is BUILD->REPAIR.
        # With no intent_spec, plan() no-ops straight to BUILD.
        m = _make_machine(tmp_path, clarified=True, plan_approved=False,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.PLAN
        with patch.object(m.storage, "commit"):
            m.run()
        assert m.current_state == State.BUILD

    def test_execution_gate_requires_real_approval(self, tmp_path):
        # The core guarantee: a run cannot cross BUILD->REPAIR (and so can never
        # reach EXECUTE) until plan_approved is set by an explicit approve_plan().
        m = _make_machine(tmp_path, clarified=True, plan_approved=False,
                          execution_status=True, validation_result="accepted")
        with patch.object(m, "build", return_value=State.REPAIR), \
                patch.object(m.storage, "commit"):
            m.current_state = State.BUILD
            with pytest.raises(GuardsBroken):
                m.run()                      # not approved -> blocked before REPAIR
            m.approve_plan()                 # researcher approves the plan
            m.current_state = State.BUILD
            m.run()                          # now the guard passes
        assert m.context.plan_approved is True
        assert m.current_state == State.REPAIR

    def test_build_to_repair_blocked_without_plan_approved(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=False,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.BUILD
        with patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()

    def test_repair_to_execute_blocked_without_plan_approved(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=False,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.REPAIR
        with patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()

    def test_execute_to_interpret_blocked_without_execution_status(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=False, validation_result="accepted")
        m.current_state = State.EXECUTE
        with patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()

    def test_validate_to_accept_blocked_when_rejected(self, tmp_path):
        # The guard must block VALIDATE->ACCEPT when the verdict is "rejected".
        # validate() now correctly routes rejected->REPLAN, so force the (invalid)
        # ACCEPT target to exercise the guard-rejection path directly.
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="rejected")
        m.current_state = State.VALIDATE
        with patch.object(m, "validate", return_value=State.ACCEPT), \
                patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Invalid transitions (no guard entry)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInvalidTransitions:
    @pytest.mark.parametrize("src,dst", [
        (State.INTAKE, State.EXECUTE),
        (State.PLAN, State.VALIDATE),
        (State.BUILD, State.ACCEPT),
        (State.CLARIFY, State.TERMINATE),
    ])
    def test_undefined_transition_raises(self, src, dst, tmp_path):
        m = _make_machine(tmp_path, **HAPPY_CONTEXT)
        m.current_state = src
        with patch.object(m, src.name.lower(), return_value=dst):
            with patch.object(m.storage, "commit"):
                with pytest.raises(InvalidTransition):
                    m.run()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Liveness – every non-terminal state has at least one outgoing transition
# ═══════════════════════════════════════════════════════════════════════════════

class TestLiveness:
    def test_every_nonterminal_state_has_outgoing_guard(self):
        terminal = {State.TERMINATE}
        sources = {src for (src, _) in GUARDS.keys()}
        nonterminal_states = {s for s in State if s not in terminal}
        missing = nonterminal_states - sources
        assert missing == set(), \
            f"States with no outgoing transition (potential deadlock): {missing}"

    def test_every_handler_returns_a_state(self, tmp_path):
        m = _offline_machine(tmp_path)
        with patch("builtins.input", return_value="predict the solubility of aspirin"):
            for s in State:
                if s == State.TERMINATE:
                    continue
                handler = getattr(m, s.name.lower(), None)
                assert handler is not None, f"No handler for {s.name}"
                result = handler()
                assert isinstance(result, State), \
                    f"{s.name} handler returned {result!r}, not a State"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Crash recovery
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrashRecovery:
    def test_commit_creates_file(self, tmp_path):
        path = str(tmp_path / "state.json")
        ds = DataStorage(path)
        ds.commit(State.PLAN, Context(clarified=True, plan_approved=True))
        assert Path(path).exists()

    def test_commit_produces_valid_json(self, tmp_path):
        path = str(tmp_path / "state.json")
        ds = DataStorage(path)
        ds.commit(State.PLAN, Context(clarified=True, plan_approved=True))
        with open(path) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_commit_can_serialize_context_with_artifacts(self, tmp_path):
        path = str(tmp_path / "state.json")
        ds = DataStorage(path)
        ctx = Context(
            clarified=True,
            plan_approved=True,
            execution_status=True,
            validation_result="accepted",
            artifacts={"intent_spec": "/some/path.json"},
        )
        ds.commit(State.PLAN, ctx)

    def test_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "state.json")
        ds = DataStorage(path)
        ctx = Context(clarified=True, plan_approved=True)
        ds.commit(State.PLAN, ctx)
        state, loaded_ctx = ds.load()
        assert state == State.PLAN
        assert loaded_ctx.clarified is True
        assert loaded_ctx.plan_approved is True

    def test_load_returns_none_when_no_file(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        ds = DataStorage(path)
        assert ds.load() is None

    def test_load_with_manually_written_file(self, tmp_path):
        path = str(tmp_path / "state.json")
        ctx = Context(clarified=True, plan_approved=True)
        with open(path, "w") as f:
            json.dump({"current_state": State.PLAN.name, "context": asdict(ctx)}, f)
        ds = DataStorage(path)
        state, loaded_ctx = ds.load()
        assert state == State.PLAN
        assert loaded_ctx.clarified is True
        assert loaded_ctx.plan_approved is True

    def test_machine_resumes_from_saved_state(self, tmp_path):
        path = str(tmp_path / "state.json")
        ctx = Context(clarified=True, plan_approved=True,
                      execution_status=True, validation_result="accepted")
        with open(path, "w") as f:
            json.dump({"current_state": State.VALIDATE.name, "context": asdict(ctx)}, f)
        m = StateMachine(data_path=path)
        assert m.current_state == State.VALIDATE
        assert m.context.plan_approved is True


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Branching paths from VALIDATE
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidateBranching:
    """validate() routes on context.validation_result to the three targets the
    guard table allows out of VALIDATE (accepted->ACCEPT, rejected->REPLAN,
    needs_review->CORRECT)."""

    def test_validate_to_accept_on_accepted(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.VALIDATE
        assert m.validate() == State.ACCEPT

    def test_validate_to_replan_on_rejected(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="rejected")
        m.current_state = State.VALIDATE
        assert m.validate() == State.REPLAN

    def test_validate_needs_review_goes_to_correct(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="needs_review")
        m.current_state = State.VALIDATE
        assert m.validate() == State.CORRECT


# A crystal (periodic-solid) IntentSpec: no molecule/SMILES, described by
# formula + polymorph. This is the "bulk modulus of TiO2" shape.
CRYSTAL_INTENT = {
    "objective": "Compute the bulk modulus of rutile TiO2",
    "domain": "materials",
    "system_descriptors": {
        "kind": "crystal",
        "formula": "TiO2",
        "crystal": {"formula": "TiO2", "name": "titanium dioxide", "phase": "rutile"},
    },
    "acceptance_metrics": [
        {"metric_name": "bulk_modulus", "target_value": 210, "tolerance": 30}
    ],
    "metadata": {
        "ambiguity": False,
        "confidence_scores": {
            "objective_confidence": 0.95,
            "domain_confidence": 0.95,
            "formula_confidence": 0.95,
            "phase_confidence": 0.9,
        },
    },
}


class TestSystemRepresentation:
    """The intent ontology distinguishes discrete molecules (SMILES) from
    periodic solids (formula + polymorph), so a crystal request is never gated
    on -- or asked to clarify -- a SMILES it cannot have."""

    def test_kind_crystal_from_explicit_discriminator(self, tmp_path):
        m = _make_machine(tmp_path)
        assert m._system_kind(CRYSTAL_INTENT) == "crystal"

    def test_kind_crystal_inferred_without_discriminator(self, tmp_path):
        m = _make_machine(tmp_path)
        intent = copy.deepcopy(CRYSTAL_INTENT)
        del intent["system_descriptors"]["kind"]
        assert m._system_kind(intent) == "crystal"

    def test_kind_molecule_default(self, tmp_path):
        m = _make_machine(tmp_path)
        assert m._system_kind(VALID_INTENT) == "molecule"

    def test_crystal_confident_without_smiles_score(self, tmp_path):
        """The core fix: a crystal spec clears the confidence gate on its own
        (formula/phase) scores -- it does not need a SMILES_confidence."""
        m = _make_machine(tmp_path)
        assert m._is_confident(CRYSTAL_INTENT) is True

    def test_crystal_not_gated_on_stray_low_smiles_score(self, tmp_path):
        """A stray low SMILES_confidence on a crystal is irrelevant and must not
        block -- this is what previously forced endless SMILES clarification."""
        m = _make_machine(tmp_path)
        intent = copy.deepcopy(CRYSTAL_INTENT)
        intent["metadata"]["confidence_scores"]["SMILES_confidence"] = 0.1
        assert m._is_confident(intent) is True

    def test_molecule_still_gated_on_smiles_score(self, tmp_path):
        """Molecules must still gate on SMILES_confidence (regression guard)."""
        m = _make_machine(tmp_path)
        intent = copy.deepcopy(VALID_INTENT)
        intent["metadata"]["confidence_scores"]["SMILES_confidence"] = 0.1
        assert m._is_confident(intent) is False

    def test_discovery_input_format_cif_for_crystal(self, tmp_path):
        m = _make_machine(tmp_path)
        assert m._discovery_query(CRYSTAL_INTENT).input_format == "CIF"

    def test_discovery_input_format_smiles_for_molecule(self, tmp_path):
        m = _make_machine(tmp_path)
        assert m._discovery_query(VALID_INTENT).input_format == "SMILES"


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Rewind / re-run from an earlier stage
# ═══════════════════════════════════════════════════════════════════════════════

class TestRewind:
    """rewind_to() switches the machine to an earlier stage so it can be re-run,
    discarding exactly that stage's + every later stage's output while keeping the
    upstream work (its artifacts) as input."""

    def _seed_full_run(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="accepted")
        m.context.artifacts.update({
            "intent_spec": "/x/intent_spec.json",       # INTAKE
            "goal_graph": "/x/goal_graph.json",         # DECOMPOSE
            "discovery": "/x/discovery.json",           # DISCOVER
            "execution_plan": "/x/execution_plan.json",  # PLAN
            "run_bundle": "/x/run_bundle",              # BUILD
            "script": "/x/run_bundle/main.py",          # BUILD
            "repair_report": "/x/repair_report.json",   # REPAIR (folded into BUILD)
            "execution_result": "/x/execution_result.json",  # EXECUTE
            "normalized_result": "/x/normalized_result.json",  # INTERPRET
            "validation_report": "/x/validation_report.json",  # VALIDATE
            "correction_plan": "/x/correction_plan.json",  # CORRECT (folded into VALIDATE)
        })
        m.current_state = State.TERMINATE
        return m

    def test_rewind_to_clarify_keeps_intake_output_drops_the_rest(self, tmp_path):
        m = self._seed_full_run(tmp_path)
        m.rewind_to(State.CLARIFY)
        assert m.current_state == State.CLARIFY
        assert m.context.clarified is False                 # CLARIFY's flag reset
        assert m.context.plan_approved is False             # must re-approve on re-run
        assert m.context.artifacts == {"intent_spec": "/x/intent_spec.json"}

    def test_rewind_to_intake_drops_intent_spec_too(self, tmp_path):
        m = self._seed_full_run(tmp_path)
        m.rewind_to(State.INTAKE)
        assert m.current_state == State.INTAKE
        assert m.context.artifacts == {}

    def test_rewind_to_plan_keeps_decompose_and_discover(self, tmp_path):
        m = self._seed_full_run(tmp_path)
        m.rewind_to(State.PLAN)
        assert set(m.context.artifacts) == {"intent_spec", "goal_graph", "discovery"}

    def test_rewind_to_execute_keeps_build_but_resets_execution(self, tmp_path):
        m = self._seed_full_run(tmp_path)
        m.rewind_to(State.EXECUTE)
        # BUILD/REPAIR ran before EXECUTE, so their artifacts survive ...
        assert m.context.artifacts["run_bundle"] == "/x/run_bundle"
        assert m.context.artifacts["repair_report"] == "/x/repair_report.json"
        # ... but EXECUTE's own output + guard flag are cleared for the re-run.
        assert "execution_result" not in m.context.artifacts
        assert m.context.execution_status is None
        # BUILD ran before EXECUTE, so the plan stays approved (no re-approval
        # needed to re-run only the calculation).
        assert m.context.plan_approved is True

    def test_rewind_to_interpret_drops_the_epic6_artifacts(self, tmp_path):
        """A re-interpretation must not read the previous pass's normalized
        result, validation report, or correction plan -- they are re-derived."""
        m = self._seed_full_run(tmp_path)
        m.rewind_to(State.INTERPRET)
        assert m.context.artifacts["execution_result"] == "/x/execution_result.json"
        for stale in ("normalized_result", "validation_report", "correction_plan"):
            assert stale not in m.context.artifacts
        assert m.context.validation_result is None

    def test_rewind_resets_clarify_round_counter(self, tmp_path):
        m = self._seed_full_run(tmp_path)
        m._clarify_rounds = 3
        m.rewind_to(State.DISCOVER)
        assert m._clarify_rounds == 0

    def test_rewind_resets_the_correction_loop_counter(self, tmp_path):
        """A rerun that inherited the finished run's iteration count would hit
        the cap immediately and refuse to correct anything."""
        m = self._seed_full_run(tmp_path)
        m._rerun.iteration = m._rerun.policy.max_iterations
        m._rerun.record_metric(0.4)
        m.rewind_to(State.EXECUTE)
        assert m._rerun.iteration == 0
        assert m._rerun.metric_history == []
        assert m._rerun.decide(expected_benefit=1.0,
                               estimated_cost=0.0).should_rerun is True

    def test_rewind_persists_new_state_to_storage(self, tmp_path):
        m = self._seed_full_run(tmp_path)
        m.rewind_to(State.DISCOVER)
        state, ctx = m.storage.load()   # crash-recovery file now reflects the rewind
        assert state == State.DISCOVER
        assert "discovery" not in ctx.artifacts

    @pytest.mark.parametrize("bad", [State.TERMINATE, State.REPAIR, State.CORRECT, State.REPLAN])
    def test_rewind_to_non_rewindable_state_raises(self, bad, tmp_path):
        m = self._seed_full_run(tmp_path)
        with pytest.raises(InvalidTransition):
            m.rewind_to(bad)


# ═══════════════════════════════════════════════════════════════════════════════
# 10. CLARIFY asks as few, targeted, concise questions as possible
# ═══════════════════════════════════════════════════════════════════════════════

class _RecordingAgent:
    """Captures the clarification prompt and returns a scripted questions reply.

    Any prompt mentioning "questions" is the clarification prompt (records it +
    returns ``questions``); every other prompt (intake / rewrite) returns the
    canned IntentSpec so the offline handlers run end to end.
    """

    def __init__(self, questions="What temperature and solvent?", low_confidence=True):
        intent = copy.deepcopy(VALID_INTENT)
        if low_confidence:
            intent["metadata"]["confidence_scores"]["SMILES_confidence"] = 0.4
        self._intent_json = json.dumps(intent)
        self._questions = questions
        self.clarification_prompt = None

    def call_agent(self, prompt, **kwargs):
        text = str(prompt)
        if "questions" in text.lower():
            self.clarification_prompt = text
            return {"content": [{"text": self._questions}]}
        return {"content": [{"text": self._intent_json}]}


class TestClarifyConcise:
    """CLARIFY targets only genuinely-uncertain fields and asks the fewest,
    most concise questions -- and skips the exchange entirely when nothing needs
    clarifying."""

    def test_uncertain_fields_lists_low_scores_most_uncertain_first(self, tmp_path):
        m = _make_machine(tmp_path)
        intent = copy.deepcopy(VALID_INTENT)
        intent["metadata"]["confidence_scores"]["SMILES_confidence"] = 0.4
        intent["metadata"]["confidence_scores"]["formula_confidence"] = 0.2
        # both below 0.8; formula (0.2) is more uncertain than SMILES (0.4) -> first.
        # Confident fields (objective/domain/name at 0.95) are excluded.
        assert m._uncertain_fields(intent) == ["formula", "SMILES"]

    def test_uncertain_fields_confident_spec_is_empty(self, tmp_path):
        m = _make_machine(tmp_path)
        assert m._uncertain_fields(VALID_INTENT) == []

    def test_uncertain_fields_crystal_ignores_smiles(self, tmp_path):
        # A stray low SMILES score on a crystal is irrelevant and must never be
        # asked about; the real gap (phase) is what surfaces.
        m = _make_machine(tmp_path)
        intent = copy.deepcopy(CRYSTAL_INTENT)
        intent["metadata"]["confidence_scores"]["phase_confidence"] = 0.3
        intent["metadata"]["confidence_scores"]["SMILES_confidence"] = 0.1
        fields = m._uncertain_fields(intent)
        assert "phase" in fields
        assert "SMILES" not in fields

    def test_clarify_prompt_is_scoped_to_the_uncertain_field(self, tmp_path):
        agent = _RecordingAgent(low_confidence=True)  # SMILES below threshold
        m = _offline_machine(tmp_path, agent=agent)
        m._request = "predict the aqueous solubility of aspirin"
        with patch("builtins.input", return_value="aspirin, SMILES CC(=O)Oc1ccccc1C(=O)O"):
            m.intake()
            m.clarify()
        assert agent.clarification_prompt is not None
        instruction = agent.clarification_prompt.split("IntentSpec:")[0]
        # the field list drives the questions, and it names only the uncertain field
        assert "unresolved fields" in instruction
        assert "most important first: SMILES" in instruction
        # the confident objective is NOT injected into the ask list
        assert "objective" not in instruction.lower()

    def test_clarify_advances_when_agent_asks_nothing(self, tmp_path):
        # "No questions." means no genuine gap: clarify must advance without
        # prompting the researcher (input() would raise if it tried).
        agent = _RecordingAgent(questions="No questions.", low_confidence=True)
        m = _offline_machine(tmp_path, agent=agent)
        m._request = "predict the aqueous solubility of aspirin"
        with patch("builtins.input", side_effect=AssertionError("clarify must not prompt")):
            m.intake()
            assert m.clarify() == State.DECOMPOSE
        assert m.context.clarified is True


class TestSlurmClusterGrounding:
    """Slurm execution must never plan around a library the cluster can't run.

    Fingerprint of Slurm job 2487046: psi4 is importable locally (pixi
    installs it from conda-forge) so the old grounding kept it -- but psi4 has
    no PyPI distribution and no cluster env spec provides it, so the job died
    in `pip install` after staging + a queue wait.
    """

    @pytest.fixture(autouse=True)
    def _fresh_caches(self):
        SM._CLUSTER_ENV_PKGS_CACHE = None
        SM._PYPI_VERDICTS.clear()
        yield
        SM._CLUSTER_ENV_PKGS_CACHE = None
        SM._PYPI_VERDICTS.clear()

    def test_env_specs_parse_to_package_names(self):
        pkgs = SM._cluster_env_packages()
        # From the real scripts/ris/envs specs: default.yml carries xtb-python,
        # gpaw.yml carries gpaw (version/build pins stripped), psi4.yml psi4.
        assert "xtb-python" in pkgs
        assert "gpaw" in pkgs
        assert "psi4" in pkgs
        # Only what a spec actually declares: openmm is in pixi's sim env but no
        # cluster spec carries it, so it must not appear here.
        assert "openmm" not in pkgs

    def test_every_conda_only_engine_has_an_env_spec(self):
        """No conda-only engine may be left without a spec.

        A missing spec is invisible: _cluster_cannot_run vetoes the engine at
        planning time and the plan is quietly rerouted to a different tool, so the
        researcher gets a less appropriate method with no error anywhere. Adding a
        conda-only package to the registry therefore obliges you to add its spec.
        """
        provided = SM._cluster_env_packages()
        missing = sorted(p for p in SM._depinf.CONDA_ONLY_PACKAGES if p not in provided)
        assert not missing, (
            f"conda-only packages with no scripts/ris/envs spec: {missing} -- "
            f"plans needing them will be silently rerouted")

    def test_conda_only_library_without_env_spec_is_blocked(self):
        # Simulated rather than taken from the real specs: every conda-only engine
        # now HAS a spec (see the test above), so the only way to exercise the veto
        # is to stand in an inventory that lacks one. The fixture resets the cache.
        SM._CLUSTER_ENV_PKGS_CACHE = frozenset({"ase", "numpy", "pandas"})
        assert SM._cluster_cannot_run("cp2k") is True

    def test_engines_with_a_spec_are_runnable_on_the_cluster(self):
        # The four engines provisioned for RIS: a spec is exactly what flips the
        # veto, so each must now plan as runnable.
        for library in ("DFTB+", "Quantum ESPRESSO", "ABINIT", "CP2K"):
            assert SM._cluster_cannot_run(library) is False, library

    def test_conda_only_library_covered_by_a_spec_is_allowed(self):
        # These need conda-only packages, but a spec provisions each: xtb via
        # default.yml, gpaw via gpaw.yml, psi4 via psi4.yml.
        assert SM._cluster_cannot_run("xtb") is False
        assert SM._cluster_cannot_run("gpaw") is False
        assert SM._cluster_cannot_run("psi4") is False

    def test_pip_installable_library_is_never_blocked(self):
        assert SM._cluster_cannot_run("pymatgen") is False

    def test_library_importable_vetoes_blocked_candidates(self, tmp_path):
        # The veto beats even an injected "it's installed here" answer: local
        # importability is irrelevant when the cluster can't run the library.
        # Inventory simulated for the same reason as above -- every conda-only
        # engine now ships a spec.
        SM._CLUSTER_ENV_PKGS_CACHE = frozenset({"ase", "numpy", "pandas"})
        m = _make_machine(tmp_path)
        m.execute_slurm = True
        m._library_available = lambda name: True
        assert m._library_importable("cp2k") is False
        assert m._library_importable("pymatgen") is True

    def test_gate_is_inert_off_slurm(self, tmp_path):
        # The gate keys off the machine's own routing flag, not the env.
        m = _make_machine(tmp_path)
        m.execute_slurm = False
        m._library_available = lambda name: True
        assert m._library_importable("cp2k") is True

    def test_package_absent_from_pypi_is_blocked_without_curation(self, monkeypatch):
        # The general (derived) arm: a tool nobody hand-listed anywhere, whose
        # package simply does not exist on PyPI, is vetoed by asking PyPI --
        # this is what keeps the gate from being incident-specific.
        monkeypatch.setattr(SM._depinf, "is_available_on_pypi",
                            lambda pkg, ver=None, **kw: False)
        assert SM._cluster_cannot_run("somenewtool") is True

    def test_ambiguous_pypi_verdict_fails_open(self, monkeypatch):
        # Offline / network error -> None -> the candidate survives; the
        # pre-submit preflight remains the authority for the final bundle.
        monkeypatch.setattr(SM._depinf, "is_available_on_pypi",
                            lambda pkg, ver=None, **kw: None)
        assert SM._cluster_cannot_run("somenewtool") is False

    def test_pypi_verdicts_are_cached(self, monkeypatch):
        calls = []
        monkeypatch.setattr(SM._depinf, "is_available_on_pypi",
                            lambda pkg, ver=None, **kw: calls.append(pkg) or False)
        SM._cluster_cannot_run("somenewtool")
        SM._cluster_cannot_run("somenewtool")
        assert len(calls) == 1


class TestAtomCountSuggestion:
    """_atom_count feeds the suggested Slurm CPU request (~1 CPU per atom)."""

    def test_structure_atom_list_wins_over_formula(self):
        sd = {"formula": "Si", "structure": {"atoms": [{}, {}, {}, {}]}}
        assert SM._atom_count(sd) == 4

    def test_formula_multiplicities_are_counted(self):
        assert SM._atom_count({"formula": "CaPt2"}) == 3
        assert SM._atom_count({"crystal": {"formula": "C9H8O4"}}) == 21

    def test_unknown_system_yields_none(self):
        assert SM._atom_count(None) is None
        assert SM._atom_count({}) is None
        assert SM._atom_count({"formula": ""}) is None


class TestRuntimeTracebackExtraction:
    """_runtime_traceback: the trigger for EXECUTE's general self-heal loop."""

    def _result(self, status, stdout="", stderr=""):
        from execution_adapter.execution_result import ExecutionResult
        return ExecutionResult(status=status, exit_code=1,
                               stdout=stdout, stderr=stderr)

    MPI_CRASH = (
        "Relaxed lattice constant a: 5.475\n"
        "rank=4 L00: Traceback (most recent call last):\n"
        'rank=4 L01:   File "main.py", line 91, in compute_band_gaps\n'
        "rank=4 L02:     path = atoms.cell.bandpath('GXWKGL', npoints=200)\n"
        "rank=4 L03: KeyError: 'W'\n"
        "GPAW CLEANUP (node 4): <class 'KeyError'> occurred.  Calling MPI_Abort!\n"
    )

    def test_mpi_rank_prefixed_traceback_is_extracted(self):
        from execution_adapter.execution_result import ExecutionStatus
        tb = SM._runtime_traceback(self._result(ExecutionStatus.FAILED,
                                                stdout=self.MPI_CRASH))
        assert tb is not None
        assert "KeyError: 'W'" in tb and "main.py" in tb
        assert "rank=" not in tb  # prefixes stripped so the block reads plainly

    def test_plain_traceback_in_stderr_is_extracted(self):
        from execution_adapter.execution_result import ExecutionStatus
        stderr = ('Traceback (most recent call last):\n'
                  '  File "main.py", line 5, in <module>\n'
                  "TypeError: bad call\n")
        tb = SM._runtime_traceback(self._result(ExecutionStatus.FAILED,
                                                stderr=stderr))
        assert tb is not None and "TypeError" in tb

    def test_non_failed_statuses_never_qualify(self):
        # Dependency/timeout/setup failures have their own handling -- a code
        # repair can't fix them, so the loop must not burn attempts on them.
        from execution_adapter.execution_result import ExecutionStatus
        for status in (ExecutionStatus.DEPENDENCY_ERROR, ExecutionStatus.TIMEOUT,
                       ExecutionStatus.SETUP_FAILED, ExecutionStatus.SMOKE_FAILED):
            assert SM._runtime_traceback(
                self._result(status, stdout=self.MPI_CRASH)) is None

    def test_traceback_outside_main_py_does_not_qualify(self):
        # A crash whose frames never touch main.py happened outside the code
        # we can rewrite (e.g. inside the calculator itself).
        from execution_adapter.execution_result import ExecutionStatus
        stdout = ('Traceback (most recent call last):\n'
                  '  File "/envs/gpaw/lib/python3.11/site-packages/gpaw/core.py", '
                  'line 10, in solve\n'
                  "RuntimeError: SCF not converged\n")
        assert SM._runtime_traceback(
            self._result(ExecutionStatus.FAILED, stdout=stdout)) is None

    def test_failure_without_traceback_does_not_qualify(self):
        from execution_adapter.execution_result import ExecutionStatus
        assert SM._runtime_traceback(
            self._result(ExecutionStatus.FAILED, stdout="exit 42, no output")) is None


class _SeqAdapter:
    """An execution adapter returning queued results (last one repeats)."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def execute(self, bundle, **kwargs):
        self.calls.append((bundle, kwargs))
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


class _FakeDoctor:
    """Stands in for the ScriptDoctor in the self-heal loop tests."""

    agent = object()  # non-None: the LLM repair channel is "available"

    def __init__(self, fixed):
        self.fixed = fixed
        self.failures = []

    def repair_runtime(self, source, failure):
        self.failures.append(failure)
        return self.fixed


class TestExecuteSelfHeals:
    """A run that crashes in the generated script is repaired and re-executed."""

    CRASH = TestRuntimeTracebackExtraction.MPI_CRASH

    def _machine(self, tmp_path, results, fixed="print('fixed')\n"):
        m = _offline_machine(tmp_path)
        bundle_dir = Path(m.artifacts_dir) / f"run_bundle_{m.run_id}"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "main.py").write_text("print('broken')\n", encoding="utf-8")
        # Mark the bundle LLM-synthesized: only synthesized scripts are healed.
        (bundle_dir / "config.yaml").write_text("template: llm_synthesized\n",
                                                encoding="utf-8")
        m.context.artifacts["run_bundle"] = str(bundle_dir)
        m.execute_locally = True
        m._execution_adapter = _SeqAdapter(results)
        m._script_doctor = _FakeDoctor(fixed)
        return m, bundle_dir

    def _failed(self):
        from execution_adapter.execution_result import ExecutionResult, ExecutionStatus
        return ExecutionResult(status=ExecutionStatus.FAILED, exit_code=42,
                               stdout=self.CRASH)

    def _ok(self):
        from execution_adapter.execution_result import ExecutionResult, ExecutionStatus
        return ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0, stdout="{}")

    def test_crash_is_repaired_and_rerun_to_success(self, tmp_path):
        m, bundle_dir = self._machine(tmp_path, [self._failed(), self._ok()])
        assert m.execute() == State.INTERPRET
        assert m.context.execution_status is True
        assert len(m._execution_adapter.calls) == 2         # failed, then re-ran
        # the doctor got the real (de-prefixed) traceback, and main.py was healed
        assert "KeyError: 'W'" in m._script_doctor.failures[0]
        assert (bundle_dir / "main.py").read_text(encoding="utf-8") == "print('fixed')\n"

    def test_unrepairable_crash_fails_after_one_attempt(self, tmp_path):
        m, bundle_dir = self._machine(tmp_path, [self._failed()], fixed=None)
        with pytest.raises(Exception):
            m.execute()
        assert len(m._execution_adapter.calls) == 1  # no blind resubmission
        assert (bundle_dir / "main.py").read_text(encoding="utf-8") == "print('broken')\n"

    def test_non_code_failures_are_never_retried(self, tmp_path):
        from execution_adapter.execution_result import ExecutionResult, ExecutionStatus
        dep = ExecutionResult(status=ExecutionStatus.DEPENDENCY_ERROR, exit_code=1,
                              stdout="No matching distribution found for psi4")
        m, _ = self._machine(tmp_path, [dep])
        with pytest.raises(Exception):
            m.execute()
        assert len(m._execution_adapter.calls) == 1
        assert m._script_doctor.failures == []  # repair never consulted

    def test_budget_env_var_disables_the_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TWAIN_RUNTIME_REPAIR_ATTEMPTS", "0")
        m, _ = self._machine(tmp_path, [self._failed(), self._ok()])
        with pytest.raises(Exception):
            m.execute()
        assert len(m._execution_adapter.calls) == 1
        assert m._script_doctor.failures == []

    def test_repair_budget_is_bounded(self, tmp_path):
        # Every attempt fails and every repair "succeeds": the loop must stop
        # at the budget (default 2 repairs -> 3 executions), then surface the
        # failure.
        m, _ = self._machine(tmp_path, [self._failed()])
        with pytest.raises(Exception):
            m.execute()
        assert len(m._execution_adapter.calls) == 3
        assert len(m._script_doctor.failures) == 2

    def test_template_bundles_are_not_healed(self, tmp_path):
        # A deterministic template didn't invent API calls; a crash there is
        # not the LLM's doing, so the loop stands down.
        m, bundle_dir = self._machine(tmp_path, [self._failed()])
        (bundle_dir / "config.yaml").write_text("template: pymatgen_analysis\n",
                                                encoding="utf-8")
        with pytest.raises(Exception):
            m.execute()
        assert len(m._execution_adapter.calls) == 1
        assert m._script_doctor.failures == []


class TestClusterEngineDataIsProvisioned:
    """Every engine needing external data must have both a spec and a data hook.

    These two files have to agree, and nothing at run time notices when they
    don't: a spec with no hook produces an env holding the binary but no way to
    find its parameters, which surfaces on the compute node as "SK file not found"
    or "cannot open ...UPF" -- indistinguishable from a broken install, after a
    queue wait. The registry knows which engines need data; assert the shell
    scripts cover each one.
    """

    RIS = Path(__file__).resolve().parents[2] / "scripts" / "ris"

    def _entries_needing_data(self):
        from method_discovery.calculator_registry import load_calculators
        return [c for c in load_calculators() if c.needs_external_data]

    def test_each_data_engine_has_an_env_spec(self):
        specs = {p.stem for p in (self.RIS / "envs").glob("*.yml")}
        # Env spec names follow the conda package, which is what the payload's
        # candidate list is built from (statemachine's env_pythons).
        expected = {"dftbplus": "DFTB+", "qe": "Quantum ESPRESSO",
                    "abinit": "ABINIT", "cp2k": "CP2K"}
        missing = sorted(env for env in expected if env not in specs)
        assert not missing, f"engines needing data with no env spec: {missing}"

    def test_each_data_engine_has_a_hook_case_in_provision_envs(self):
        provision = (self.RIS / "provision_envs.sh").read_text()
        for env, var in (("dftbplus", "DFTB_PREFIX"),
                         ("qe", "ESPRESSO_PSEUDO"),
                         ("abinit", "ABINIT_PP_PATH"),
                         ("cp2k", "CP2K_DATA_DIR")):
            assert f"{env})" in provision, f"no write_data_hook case for {env}"
            assert var in provision, f"{env}'s hook does not export {var}"

    def test_the_payload_sources_activate_hooks(self):
        """The hook mechanism only works because the Slurm payload sources them.

        This is load-bearing: it is what lets a data variable reach the engine
        without any per-engine code in the execution adapter.
        """
        adapter = (Path(__file__).resolve().parents[2] / "modules"
                   / "08_execution_adapter" / "slurm_execution_adapter.py").read_text()
        assert "etc/conda/activate.d" in adapter

    def test_pseudopotential_engines_are_fetchable(self):
        """A pseudo_library value must correspond to a set fetch_data.sh knows."""
        from method_discovery.calculator_registry import load_calculators
        fetch = (self.RIS / "fetch_data.sh").read_text()
        for calc in load_calculators():
            if calc.pseudo_library:
                assert f"fetch_{calc.pseudo_library}" in fetch, (
                    f"{calc.name} wants the '{calc.pseudo_library}' library but "
                    f"fetch_data.sh has no fetch_{calc.pseudo_library} function")
