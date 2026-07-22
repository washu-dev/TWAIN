"""Tests for the runtime orchestrator (Story 5.3).

The orchestrator's contract is: *drive the real ``StateMachine`` and combine it
with the session layer*. These tests assert exactly that, against the acceptance
criteria:

* Complete happy-path flow intake -> ... -> accept, driving the real machine
  (and proving its handlers actually run).
* Resume from a checkpoint is deterministic.
* Error handling stops the run and prevents infinite loops.

Plus the supporting pieces: the generic agent/step runner (timeout / validation
/ retry) and the error classifier.

Run from the repo root with:  pixi run pytest tests/unit/test_orchestrator.py
"""
import json
import sys
import time
from pathlib import Path

import pytest

# The orchestrator package lives in a digit-prefixed directory that cannot be
# imported by dotted name; put it on sys.path and let _bootstrap wire the rest.
ORCH_DIR = Path(__file__).resolve().parents[2] / "modules" / "07_runtime_orchestrator"
sys.path.insert(0, str(ORCH_DIR))

import _bootstrap  # noqa: E402,F401  (sets up sys.path for the bare imports below)

from states import State, Context  # noqa: E402
from statemachine import StateMachine  # noqa: E402
from session import RunStatus  # noqa: E402
from store import Store  # noqa: E402
import agent_runner  # noqa: E402
from agent_runner import run_agent  # noqa: E402
import error_handler  # noqa: E402
from error_handler import (  # noqa: E402
    ErrorCategory, classify, AgentTimeout, PolicyError, ConfigError, LLMError,
)
from orchestrator import Orchestrator  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

# Every guard precondition satisfied (the StateMachine handlers are stubs that
# don't set these themselves yet), so a seeded run can reach TERMINATE.
HAPPY = dict(clarified=True, plan_approved=True, execution_status=True,
             validation_result="accepted")

HANDLER_NAMES = ["intake", "clarify", "decompose", "discover", "plan", "build",
                 "repair", "execute", "interpret", "validate", "accept"]

# A valid, already-confident IntentSpec the fake LLM returns. Confidence is above
# the StateMachine's 0.8 threshold so clarify() needs no follow-up questions.
_SPEC_JSON = json.dumps({
    "objective": "Predict the aqueous solubility of aspirin at 25C",
    "domain": "materials",
    "system_descriptors": {
        "formula": "C9H8O4",
        "molecule": {"name": "aspirin", "SMILES": "CC(=O)Oc1ccccc1C(=O)O"},
    },
    "acceptance_criteria": [{"metric_name": "logS", "target_value": -1.7, "tolerance": 0.5}],
    "metadata": {"confidence_scores": {"objective": 0.95, "domain": 0.9, "SMILES": 0.9},
                 "ambiguity": False},
})

# Default request + offline agent so the real intake()/clarify() handlers run
# without prompting on stdin or hitting the network.
DEFAULT_REQUEST = "Predict the aqueous solubility of aspirin at 25C"


def fake_llm(_prompt):
    """A prompt -> text agent that always returns the canned (confident) spec."""
    return _SPEC_JSON


def make_sm(tmp_path, name="sm", context=None, request=DEFAULT_REQUEST,
            agent=fake_llm, **handler_overrides):
    """A real StateMachine wired for offline intake/clarify (canned request +
    agent), with a seeded context and optional handler patches."""
    sm = StateMachine(
        data_path=str(tmp_path / f"{name}.sm.json"),
        request=request,
        agent=agent,
        artifacts_dir=str(tmp_path / f"{name}_artifacts"),
    )
    sm.context = Context(**(context or HAPPY))
    for handler, fn in handler_overrides.items():
        setattr(sm, handler, fn)
    return sm


def spy_sm(tmp_path, name="sm", context=None):
    """A real StateMachine whose handlers record the order in which they run."""
    sm = make_sm(tmp_path, name=name, context=context)
    calls = []
    for handler in HANDLER_NAMES:
        original = getattr(sm, handler)

        def wrap(n, orig):
            def spy():
                calls.append(n.upper())
                return orig()
            return spy

        setattr(sm, handler, wrap(handler, original))
    return sm, calls


class RecordingBus:
    """Minimal event-bus double: records what was published."""

    def __init__(self):
        self.published = []
        self.history = []

    def publish(self, event, priority=None):
        self.published.append((event.event_type, priority))
        self.history.append(event)

    def types(self):
        return [t for t, _ in self.published]


@pytest.fixture
def env(tmp_path, monkeypatch):
    # chdir into a scratch dir so handler side effects (StateMachine.intake writes
    # ExampleFileTest.txt; a real EventBus writes event_bus.log) stay in tmp.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    store = Store(":memory:")
    bus = RecordingBus()
    notes = []
    try:
        yield {
            "tmp": tmp_path,
            "cwd": cwd,
            "checkpoint_dir": str(tmp_path / "ckpt"),
            "store": store,
            "bus": bus,
            "notes": notes,
        }
    finally:
        store.close()


def build(env, session_id, sm=None, context=None, **kwargs):
    return Orchestrator(
        session_id,
        kwargs.pop("researcher_id", "researcher@lab"),
        state_machine=sm,
        context=context if context is not None else (None if sm else dict(HAPPY)),
        # Forwarded to the SM the orchestrator builds (ignored when sm is given),
        # so its intake()/clarify() run offline.
        request=kwargs.pop("request", DEFAULT_REQUEST),
        agent=kwargs.pop("agent", fake_llm),
        event_bus=kwargs.pop("event_bus", env["bus"]),
        store=kwargs.pop("store", env["store"]),
        notifier=kwargs.pop("notifier", env["notes"].append),
        checkpoint_dir=kwargs.pop("checkpoint_dir", env["checkpoint_dir"]),
        **kwargs,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Drives the real StateMachine (handlers actually run)
# ═══════════════════════════════════════════════════════════════════════════════

class TestDrivesStateMachine:
    def test_intake_and_clarify_run_through_orchestrator(self, env):
        # Wire a StateMachine with a (fake) LLM + request and drive it via the
        # orchestrator: intake() must parse & persist a real IntentSpec, and
        # clarify() must clear the guard by setting `clarified`.
        sm = StateMachine(
            data_path=str(env["tmp"] / "nlu.sm.json"),
            agent=lambda _prompt: _SPEC_JSON,
            request="Predict the aqueous solubility of aspirin",
            artifacts_dir=str(env["tmp"] / "artifacts"),
        )
        sm.context = Context(clarified=False, plan_approved=True,
                             execution_status=True, validation_result="accepted")
        o = build(env, "nlu", sm=sm)
        assert o.run() == RunStatus.COMPLETED
        assert o.sm.current_state == State.TERMINATE
        spec_path = sm.context.artifacts["intent_spec"]
        assert json.loads(Path(spec_path).read_text())["objective"]
        assert sm.context.clarified is True   # set by clarify(), not pre-seeded

    def test_handlers_called_in_pipeline_order(self, env):
        sm, calls = spy_sm(env["tmp"])
        o = build(env, "spy", sm=sm)
        assert o.run() == RunStatus.COMPLETED
        assert calls == [n.upper() for n in HANDLER_NAMES]  # intake..accept, in order

    def test_reaches_terminate_with_expected_transition_count(self, env):
        o = build(env, "count")
        o.run()
        # INTAKE->CLARIFY->...->BUILD->REPAIR->EXECUTE->...->ACCEPT->TERMINATE
        # is 11 transitions.
        assert o.run_session.transition_count == 11
        assert o.run_session.get_state() == State.TERMINATE

    def test_no_notification_and_events_published(self, env):
        o = build(env, "events")
        o.run()
        assert env["notes"] == []
        types = env["bus"].types()
        assert "run.started" in types and "run.completed" in types
        assert types.count("stage.started") == 11
        assert types.count("stage.completed") == 11

    def test_provenance_records_mapped_stages(self, env):
        o = build(env, "prov")
        o.run()
        events = o.event_log.read_all()
        # DECOMPOSE, DISCOVER and PLAN are all planning-phase stages, so each logs
        # a "plan" provenance event.
        assert [e["event_type"] for e in events] == [
            "request", "plan", "plan", "plan", "execute", "validate", "approve"
        ]
        assert o.event_log.verify_chain() is True


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Persistence + deterministic resume from checkpoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestPersistenceAndResume:
    def test_store_has_completed_session(self, env):
        o = build(env, "persist", researcher_id="alice")
        o.run()
        record = env["store"].get_session("persist")
        assert record is not None
        assert record["status"] == "completed"
        assert record["state"] == "TERMINATE"
        assert any(r["session_id"] == "persist"
                   for r in env["store"].list_sessions("alice"))
        assert env["store"].resume_session("persist") is None  # completed != resumable

    def test_resume_from_checkpoint_is_deterministic(self, env):
        ck, store = env["checkpoint_dir"], env["store"]

        # (a) a clean, uninterrupted run
        clean = Orchestrator("clean", "r", context=dict(HAPPY), event_bus=None,
                             store=store, checkpoint_dir=ck,
                             request=DEFAULT_REQUEST, agent=fake_llm)
        assert clean.run() == RunStatus.COMPLETED

        # (b) an interrupted run: pause *before* EXECUTE, persist, drop the instance
        part = Orchestrator("job", "r", context=dict(HAPPY), event_bus=None,
                            store=store, checkpoint_dir=ck,
                            request=DEFAULT_REQUEST, agent=fake_llm)
        assert part.run(until=State.EXECUTE) == RunStatus.PAUSED
        assert part.sm.current_state == State.EXECUTE
        del part

        # (c) a brand-new instance loads the checkpoint and finishes the job
        resumed = Orchestrator("job", "r", event_bus=None, store=store,
                               checkpoint_dir=ck)
        assert resumed.sm.current_state == State.EXECUTE  # resumed, not restarted
        assert resumed.run() == RunStatus.COMPLETED

        # Identical outcome to the clean run. Artifact *paths* are namespaced by
        # session id ("clean" vs "job"), so compare the control-flow context and
        # the produced artifact *keys* rather than the session-specific paths.
        assert resumed.run_session.get_state() == State.TERMINATE
        assert resumed.run_session.transition_count == clean.run_session.transition_count

        def _without_artifacts(ctx):
            return {k: v for k, v in dict(ctx).items() if k != "artifacts"}

        def _artifact_keys(ctx):
            return set(dict(ctx).get("artifacts", {}))

        assert _without_artifacts(resumed.run_session.context) == \
            _without_artifacts(clean.run_session.context)
        assert _artifact_keys(resumed.run_session.context) == \
            _artifact_keys(clean.run_session.context)

    def test_resume_reads_from_store_when_no_local_checkpoint(self, env):
        tmp = env["tmp"]
        dir_a, dir_b = str(tmp / "a"), str(tmp / "b")
        o1 = Orchestrator("xfer", "r", context=dict(HAPPY), event_bus=None,
                          store=env["store"], checkpoint_dir=dir_a,
                          request=DEFAULT_REQUEST, agent=fake_llm)
        o1.run(until=State.PLAN)
        # "Move machines": fresh checkpoint dir, same store + id -> hydrate from store.
        o2 = Orchestrator("xfer", "r", event_bus=None, store=env["store"],
                          checkpoint_dir=dir_b)
        assert o2.sm.current_state == State.PLAN
        assert o2.run() == RunStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Error handling prevents infinite loops
# ═══════════════════════════════════════════════════════════════════════════════

class TestErrorHandling:
    def test_persistent_rejection_is_bounded(self, env):
        # Force the VALIDATE handler down the REPLAN branch forever; the run must
        # not loop. (validate -> REPLAN needs validation_result == "rejected".)
        ctx = dict(HAPPY, validation_result="rejected")
        sm = make_sm(env["tmp"], context=ctx, validate=lambda: State.REPLAN)
        o = build(env, "loop", sm=sm, max_replans=3)
        assert o.run() == RunStatus.ERROR
        assert o.run_session.replan_count == 4            # 3 allowed; the 4th trips it
        assert o.run_session.transition_count < o.max_transitions
        assert o.last_error.category == ErrorCategory.POLICY
        assert env["notes"], "researcher must be notified"

    def test_correction_loop_is_bounded(self, env):
        ctx = dict(HAPPY, validation_result="needs_review")
        sm = make_sm(env["tmp"], context=ctx, validate=lambda: State.CORRECT)
        o = build(env, "correct", sm=sm, max_corrections=2)
        assert o.run() == RunStatus.ERROR
        assert o.run_session.correct_count == 3
        assert o.last_error.category == ErrorCategory.POLICY

    def test_handler_failure_stops_with_actionable_error(self, env):
        def boom():
            raise RuntimeError("registry unreachable")

        sm = make_sm(env["tmp"], discover=boom)
        o = build(env, "fail", sm=sm)
        assert o.run() == RunStatus.ERROR
        assert o.sm.current_state == State.DISCOVER     # stopped where it failed
        assert o.run_session.error is not None
        assert env["notes"] and "DISCOVER" in env["notes"][0]
        assert "manually" in o.last_error.fallback.lower()   # DISCOVER fallback
        assert env["store"].resume_session("fail") is not None  # errored => resumable

    def test_unmet_guard_is_caught_and_classified(self, env):
        # Force clarify() to advance while clarified is still False, so the
        # CLARIFY->DECOMPOSE guard rejects the transition. (Real clarify() now
        # self-loops instead of advancing when it can't reach confidence, so we
        # override it here to exercise the guard-rejection path directly.)
        sm = make_sm(env["tmp"], context=dict(HAPPY, clarified=False),
                     clarify=lambda: State.DECOMPOSE)
        o = build(env, "guard", sm=sm)
        assert o.run() == RunStatus.ERROR
        assert o.sm.current_state == State.CLARIFY
        assert o.last_error.category == ErrorCategory.CONFIG

    def test_completed_run_is_idempotent(self, env):
        o = build(env, "idem")
        assert o.run() == RunStatus.COMPLETED
        transitions = o.run_session.transition_count
        assert o.run() == RunStatus.COMPLETED          # re-run is a no-op
        assert o.run_session.transition_count == transitions


# ═══════════════════════════════════════════════════════════════════════════════
# 3b. Cost budget: tracking, enforcement, and configurability
# ═══════════════════════════════════════════════════════════════════════════════

class CostingAgent:
    """A ``prompt -> str`` agent (like ``fake_llm``) that also books a fixed cost
    per call, so the orchestrator's cost sync + budget gate run end to end."""

    def __init__(self, per_call=0.5):
        self.per_call = per_call
        self.total_cost = 0.0
        self.call_count = 0
        self.api_quota_prior = None
        self.api_quota_remaining = None

    def __call__(self, _prompt):
        self.total_cost += self.per_call
        self.call_count += 1
        return _SPEC_JSON


class TestBudget:
    def test_run_max_cost_stops_the_run(self, env):
        # A tiny budget with a costing agent: the first stage's LLM spend blows the
        # cap, so the run stops with a POLICY (budget) error rather than completing.
        # Proves cost is actually tracked AND the gate fires.
        agent = CostingAgent(per_call=0.5)
        o = build(env, "overbudget", agent=agent, run_max_cost=0.1)
        assert o.run() == RunStatus.ERROR
        assert o.last_error.category == ErrorCategory.POLICY
        assert "budget" in json.dumps(o.run_session.error).lower()
        assert o.run_budget.cost >= 0.5      # the agent's spend was synced in

    def test_generous_budget_completes_and_tracks_cost(self, env):
        # With headroom the run completes, the actual spend is tracked on the run
        # budget (non-zero), and the budget.json artifact mirrors it exactly.
        agent = CostingAgent(per_call=0.5)
        o = build(env, "underbudget", agent=agent, run_max_cost=100.0)
        assert o.run() == RunStatus.COMPLETED
        assert o.run_budget.cost > 0
        assert agent.call_count > 0
        budget_path = o.sm.context.artifacts.get("budget")
        assert budget_path and Path(budget_path).is_file()
        snap = json.loads(Path(budget_path).read_text())
        assert snap["run"]["cost"] == o.run_budget.cost
        assert snap["run"]["max_cost"] == 100.0

    def test_default_budget_when_unset(self, env):
        # No run_max_cost passed => the orchestrator's documented $1.00 default,
        # and the (per-run) global tracker ceiling matches it.
        o = build(env, "defaultbudget")
        assert o.run_budget.max_cost == 1.0
        assert o.budget_tracker.global_budget == 1.0

    def test_configured_budget_flows_to_tracker(self, env):
        o = build(env, "configured", run_max_cost=7.5)
        assert o.run_budget.max_cost == 7.5
        assert o.budget_tracker.global_budget == 7.5


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Agent/step runner: timeout / validation / retry
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentRunner:
    def test_retries_transient_then_succeeds(self):
        calls = {"n": 0}

        def agent(_spec):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("flaky network")
            return {"ok": True}

        out = run_agent(agent, {}, timeout=None, max_retries=3,
                        base_delay=0, jitter=0, sleep=lambda _d: None)
        assert out == {"ok": True}
        assert calls["n"] == 3

    def test_fails_fast_on_permanent(self):
        calls = {"n": 0}

        def agent(_spec):
            calls["n"] += 1
            raise ValueError("malformed request")

        with pytest.raises(ValueError):
            run_agent(agent, {}, timeout=None, max_retries=5, sleep=lambda _d: None)
        assert calls["n"] == 1

    def test_exhausts_transient_budget(self):
        calls = {"n": 0}

        def agent(_spec):
            calls["n"] += 1
            raise ConnectionError("always down")

        with pytest.raises(ConnectionError):
            run_agent(agent, {}, timeout=None, max_retries=2,
                      base_delay=0, jitter=0, sleep=lambda _d: None)
        assert calls["n"] == 3  # initial + 2 retries

    def test_timeout_raises_agent_timeout(self):
        def slow(_spec):
            time.sleep(0.3)
            return {"ok": True}

        with pytest.raises(AgentTimeout):
            run_agent(slow, {}, state_name="EXECUTE", timeout=0.02,
                      max_retries=0, sleep=lambda _d: None)

    def test_schema_validation_rejects_bad_output(self):
        schema = {"type": "object", "required": ["metric"],
                  "properties": {"metric": {"type": "number"}}}
        assert run_agent(lambda _s: {"metric": 1.5}, {}, timeout=None, validator=schema) \
            == {"metric": 1.5}
        with pytest.raises(ConfigError):
            run_agent(lambda _s: {"wrong": 1}, {}, timeout=None, validator=schema)

    def test_predicate_validator(self):
        with pytest.raises(ConfigError):
            run_agent(lambda _s: {"x": 1}, {}, timeout=None, validator=lambda o: "y" in o)

    def test_timeout_table_matches_criteria(self):
        assert agent_runner.timeout_for("EXECUTE") == 20 * 60
        assert agent_runner.timeout_for("CLARIFY") == 5 * 60


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Error classification + researcher notification
# ═══════════════════════════════════════════════════════════════════════════════

class TestErrorClassifier:
    @pytest.mark.parametrize("exc,category", [
        (TimeoutError("t"), ErrorCategory.TIMEOUT),
        (MemoryError("m"), ErrorCategory.RESOURCE),
        (ConnectionError("c"), ErrorCategory.RESOURCE),
        (KeyError("k"), ErrorCategory.CODE),
        (AttributeError("a"), ErrorCategory.CODE),
        (FileNotFoundError("f"), ErrorCategory.CONFIG),
    ])
    def test_heuristic_categories(self, exc, category):
        assert classify(exc).category == category

    def test_typed_errors_keep_declared_category(self):
        assert classify(PolicyError("over budget")).category == ErrorCategory.POLICY
        assert classify(LLMError("rate limited")).category == ErrorCategory.LLM
        assert classify(AgentTimeout("slow")).category == ErrorCategory.TIMEOUT

    @pytest.mark.parametrize("status", [401, 403])
    def test_http_auth_errors_are_llm_not_resource(self, status):
        """A 401/403 from the model API must classify as LLM (auth), not RESOURCE.

        requests.HTTPError subclasses OSError, so without status-aware handling it
        would fall through to the OSError -> RESOURCE rule.
        """
        class _Resp:
            status_code = status

        class _HTTPError(OSError):  # mimics requests.exceptions.HTTPError
            response = _Resp()

        classified = classify(_HTTPError("Forbidden"), state="INTAKE")
        assert classified.category == ErrorCategory.LLM
        assert "vpn" in classified.hint.lower() or "credentials" in classified.hint.lower() \
            or ".env" in classified.hint.lower()

    def test_http_rate_limit_is_llm(self):
        class _Resp:
            status_code = 429

        class _HTTPError(OSError):
            response = _Resp()

        assert classify(_HTTPError("Too Many Requests")).category == ErrorCategory.LLM

    def test_discover_failure_suggests_manual_tool(self):
        classified = classify(RuntimeError("no candidates"), state="DISCOVER")
        assert "manually" in classified.fallback.lower()

    def test_recoverable_flag(self):
        assert classify(TimeoutError("t")).recoverable is True
        assert classify(KeyError("k")).recoverable is False

    def test_notify_researcher_is_actionable(self):
        notes = []
        classified = classify(RuntimeError("boom"), state="EXECUTE")
        msg = error_handler.notify_researcher(
            classified, session_id="s1", state="EXECUTE", notifier=notes.append
        )
        assert notes == [msg]
        assert "do next" in msg
        assert "fallback" in msg
        assert "EXECUTE" in msg


# ═══════════════════════════════════════════════════════════════════════════════
# Local execution wiring (Story 5.2): the orchestrator forwards the flag to the
# StateMachine it builds, and demo() enables it by default.
# ═══════════════════════════════════════════════════════════════════════════════

class TestLocalExecutionWiring:
    def test_orchestrator_forwards_execute_locally(self, env):
        o = build(env, "exec-on", execute_locally=True, execute_install_deps=True)
        assert o.sm.execute_locally is True
        assert o.sm.execute_install_deps is True

    def test_default_is_off(self, env):
        o = build(env, "exec-off")
        assert o.sm.execute_locally is False

    def test_demo_enables_local_execution(self, monkeypatch):
        # demo() opts into local execution by default (the user's entry point).
        # Patch __init__ + the eagerly-built Store/EventBus so no real DB/log is
        # touched; just assert the kwarg demo forwards.
        import event_bus as eb_mod
        import orchestrator as orch_mod

        captured = {}
        monkeypatch.setattr(orch_mod.Orchestrator, "__init__",
                            lambda self, *a, **k: captured.update(k) or None)
        monkeypatch.setattr(orch_mod, "Store", lambda *a, **k: None)
        monkeypatch.setattr(eb_mod, "EventBus", lambda *a, **k: None)

        orch_mod.Orchestrator.demo(session_id="demo-exec")
        assert captured.get("execute_locally") is True

    def test_demo_allows_override(self, monkeypatch):
        import event_bus as eb_mod
        import orchestrator as orch_mod

        captured = {}
        monkeypatch.setattr(orch_mod.Orchestrator, "__init__",
                            lambda self, *a, **k: captured.update(k) or None)
        monkeypatch.setattr(orch_mod, "Store", lambda *a, **k: None)
        monkeypatch.setattr(eb_mod, "EventBus", lambda *a, **k: None)

        orch_mod.Orchestrator.demo(session_id="d", execute_locally=False)
        assert captured.get("execute_locally") is False
