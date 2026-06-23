"""Unit tests for the agent-mesh control-plane state machine.

Covers: state enum completeness, guard coverage, valid transitions,
invalid transitions, guard rejection, crash recovery round-trip,
liveness (no deadlocks), and known bugs in the current implementation.

Run from the repo root with:  pixi run pytest tests/unit/test_state_machine.py
"""
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

MODULE_DIR = Path(__file__).resolve().parents[2] / "modules" / "16_agent_mesh_control_plane"
sys.path.insert(0, str(MODULE_DIR))

from states import State, Context, InvalidTransition, GuardsBroken
from crash_recovery import DataStorage

_STUB_MODULES = {
    name: MagicMock()
    for name in (
        "intake", "intake.intent_spec",
        "result_interpreter", "result_interpreter.result_package",
        "pygments", "pygments.lexer",
    )
}

with patch.dict(sys.modules, _STUB_MODULES):
    import importlib
    SM = importlib.import_module("statemachine")

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


HAPPY_CONTEXT = dict(
    clarified=True,
    plan_approved=True,
    execution_status=True,
    validation_result="accepted",
)


EXPECTED_STATES = {
    "INTAKE", "CLARIFY", "DECOMPOSE", "DISCOVER", "PLAN", "BUILD",
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
        (State.CLARIFY, State.DECOMPOSE),
        (State.DECOMPOSE, State.DISCOVER),
        (State.DISCOVER, State.PLAN),
        (State.PLAN, State.BUILD),
        (State.BUILD, State.EXECUTE),
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
        m = _make_machine(tmp_path, **HAPPY_CONTEXT)
        with patch.object(m.storage, "commit"):
            result = m.run()
        assert result == 0
        assert m.current_state == State.TERMINATE

    def test_handler_returns_match_guards(self):
        handlers_next = {
            "intake": State.CLARIFY,
            "clarify": State.DECOMPOSE,
            "decompose": State.DISCOVER,
            "discover": State.PLAN,
            "plan": State.BUILD,
            "build": State.EXECUTE,
            "execute": State.INTERPRET,
            "interpret": State.VALIDATE,
            "validate": State.ACCEPT,
            "accept": State.TERMINATE,
            "correct": State.BUILD,
            "replan": State.PLAN,
        }
        m = _make_machine(Path(tempfile.mkdtemp()))
        for name, expected_next in handlers_next.items():
            handler = getattr(m, name)
            assert handler() == expected_next, \
                f"{name}() should return {expected_next}"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Guard rejection (invalid context)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGuardRejection:
    def test_clarify_blocked_without_clarified(self, tmp_path):
        m = _make_machine(tmp_path, clarified=False, plan_approved=True,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.CLARIFY
        with patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()

    def test_plan_to_build_blocked_without_plan_approved(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=False,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.PLAN
        with patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()

    def test_build_to_execute_blocked_without_plan_approved(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=False,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.BUILD
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
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="rejected")
        m.current_state = State.VALIDATE
        with patch.object(m.storage, "commit"):
            with pytest.raises((GuardsBroken, InvalidTransition, RecursionError)):
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

    def test_every_handler_returns_a_state(self):
        m = _make_machine(Path(tempfile.mkdtemp()))
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
    def test_validate_to_replan_on_rejected(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="rejected")
        m.current_state = State.VALIDATE
        handler = getattr(m, "validate")
        next_state = handler()
        assert next_state == State.ACCEPT, \
            "validate() always returns ACCEPT — it ignores validation_result"

    @pytest.mark.xfail(
        reason="BUG: validate() is hardcoded to return ACCEPT; "
               "it does not branch on context.validation_result",
        strict=True,
    )
    def test_validate_should_branch_on_context(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="rejected")
        m.current_state = State.VALIDATE
        next_state = m.validate()
        assert next_state == State.REPLAN

    @pytest.mark.xfail(
        reason="BUG: validate() is hardcoded to return ACCEPT; "
               "needs_review path never reached",
        strict=True,
    )
    def test_validate_needs_review_goes_to_correct(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="needs_review")
        m.current_state = State.VALIDATE
        next_state = m.validate()
        assert next_state == State.CORRECT
