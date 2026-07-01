"""Runtime orchestrator -- the RunSession coordinator (Story 5.3).

Coordinates the full lifecycle of one run (intake -> ... -> execute -> validate
-> accept) so the stages happen in the right order and no intermediate state is
lost.

The division of labour is deliberate: **the state machine runs itself.** The
``StateMachine`` from the agent-mesh control plane (Story 2.1) owns the states,
the transition handlers (``intake()`` / ``clarify()`` / ...), the ``GUARDS``, and
its own crash-recovery file. The orchestrator's job is to *drive* that machine
and combine it with the session layer:

* It **steps** the machine one transition at a time (``StateMachine.run()``),
  letting each handler do its own work and decide the next state.
* Around every step it **publishes** a lifecycle event (event bus, Story 2.2),
  **checkpoints** the :class:`RunSession` to a JSON file *and* upserts it into the
  SQLite store (Story 2.5), and appends mapped stages to the hash-chained
  provenance log (Story 7.1) -- so an interrupted run resumes deterministically
  from the last good state.
* Each step runs through :func:`agent_runner.run_agent`, which applies the
  per-stage **timeout** (EXECUTE = 20 min, CLARIFY = 5 min, ...) and
  transient-retry / permanent-fail-fast policy. A handler is free to invoke its
  agent module inside itself (optionally via ``run_agent`` too).
* A failing step is classified by :mod:`error_handler`, the researcher is
  notified with an actionable message + fallback, and the run stops in ERROR --
  it never spins. Replan / correct cycles are bounded to prevent infinite loops.

Run it directly to drive the real ``StateMachine`` end to end::

    pixi run python modules/07_runtime_orchestrator/orchestrator.py

(That executes your ``StateMachine.intake()`` etc. -- so its ``print`` and the
``ExampleFileTest.txt`` it writes will appear.)
"""
import _bootstrap  # noqa: F401  (sys.path wiring; must come first)

import json
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from states import State, Context, GuardsBroken, InvalidTransition  # noqa: F401
from statemachine import StateMachine
from event import Event
from event_bus import Priority

import error_handler
from error_handler import PolicyError
from agent_runner import run_agent, timeout_for
from session import Session, RunSession, RunStatus
from store import Store
from provenance_memory import event_log
from budget_tracker import (
    Budget_Tracker, ProjectBudget, RunBudget,
    OverBudget, OverMaxIterations, OverMaxWallTime,
)
from retry_policy import (
    ResilientCaller, CircuitBreaker, AlreadyOpenError, classify_error, ErrorType,
)


# Pipeline state -> provenance event_type (only the six the schema allows).
# DECOMPOSE/DISCOVER/PLAN are all planning-phase work, so they log as "plan".
_PROVENANCE_EVENT_TYPE = {
    State.INTAKE: "request",
    State.DECOMPOSE: "plan",
    State.DISCOVER: "plan",
    State.PLAN: "plan",
    State.EXECUTE: "execute",
    State.VALIDATE: "validate",
    State.CORRECT: "correct",
    State.ACCEPT: "approve",
}

# A fully-satisfied context: every guard precondition met. Useful as a seed while
# the StateMachine's handlers are still stubs that don't set these flags
# themselves (see ``Orchestrator.demo``).
HAPPY_CONTEXT = dict(
    clarified=True, plan_approved=True, execution_status=True,
    validation_result="accepted",
)


def _jsonable(obj):
    if isinstance(obj, dict):
        return json.loads(json.dumps(obj, default=str))
    return {"value": json.loads(json.dumps(obj, default=str))}


class Orchestrator:
    """Drives one :class:`StateMachine` and combines it with the session layer."""

    def __init__(
        self,
        session_id: Optional[str] = None,
        researcher_id: str = "",
        state_machine: Optional[StateMachine] = None,
        *,
        context=None,
        request: Optional[str] = None,
        agent=None,
        ask=None,
        artifacts_dir: Optional[str] = None,
        event_bus=None,
        store: Optional[Store] = None,
        notifier=error_handler.default_notifier,
        checkpoint_dir: Optional[str] = None,
        provenance: bool = True,
        step_timeouts: bool = True,
        step_retries: int = 0,
        max_replans: int = 3,
        max_corrections: int = 3,
        max_transitions: int = 100,
        budget_tracker: Optional[Budget_Tracker] = None,
        run_max_cost: float = 1.0,
        run_max_iterations: int = 50,
        run_wall_time_minutes: int = 30,
        circuit_breaker_max_errors: int = 5,
        circuit_breaker_window: int = 60,
        circuit_breaker_cooldown: int = 300,
        execute_locally: bool = False,
        execute_install_deps: bool = False,
    ):
        self.session_id = session_id or uuid.uuid4().hex
        self.event_bus = event_bus              # None => events disabled (no-op)
        self.store = store
        self.notifier = notifier
        self.step_timeouts = step_timeouts
        self.step_retries = step_retries
        self.max_replans = max_replans
        self.max_corrections = max_corrections
        self.max_transitions = max_transitions
        self.last_error: Optional[error_handler.ClassifiedError] = None

        # ---- retry / circuit breaker ----------------------------------------
        self.resilient_caller = ResilientCaller(
            max_retries=step_retries,
            max_errors=circuit_breaker_max_errors,
            error_window=circuit_breaker_window,
            cooldown=circuit_breaker_cooldown,
        )

        # ---- budget tracking ------------------------------------------------
        self.budget_tracker = budget_tracker or Budget_Tracker()
        self.run_budget = RunBudget(
            max_cost=run_max_cost,
            max_iterations=run_max_iterations,
            wall_time_minutes=run_wall_time_minutes,
        )
        project_budget = ProjectBudget(self.budget_tracker)
        project_budget.add_run(self.run_budget)
        self.budget_tracker.add_project(project_budget)

        # ---- load-or-create the session (resume from file, else store) -------
        existed = Session.exists(self.session_id, checkpoint_dir)
        self.session = Session(self.session_id, researcher_id, checkpoint_dir)
        resumed_from_store = False
        if not existed and self.store is not None:
            record = self.store.get_session(self.session_id)
            if record is not None:
                self.session.run_session = RunSession.from_dict(record)
                resumed_from_store = True
        resuming = existed or resumed_from_store

        # ---- the state machine we drive --------------------------------------
        sm_path = str(self.session.checkpoint_dir / f"{self.session_id}.sm.json")
        # Pass the session id as the machine's run_id so its artifacts are named
        # ``<name>_<session_id>.json`` and trace straight back to this run. The
        # researcher's request + the NLU agent are forwarded so intake/clarify run
        # without prompting on stdin (an injected ``state_machine`` is used as-is).
        # Artifacts default to a per-session subdir of the checkpoint dir so they
        # stay scoped to this run (and so tests with a tmp checkpoint dir don't
        # write into the shared logs/ tree).
        sm_artifacts_dir = artifacts_dir or str(self.session.checkpoint_dir / "artifacts")
        self.sm = state_machine or StateMachine(
            data_path=sm_path,
            run_id=self.session_id,
            request=request,
            agent=agent,
            ask=ask,
            artifacts_dir=sm_artifacts_dir,
            # Wire local execution (Story 5.2): when enabled, the EXECUTE stage
            # actually runs the RunBundle built in BUILD. Off by default so
            # injected/seeded test machines keep EXECUTE a no-op.
            execute_locally=execute_locally,
            execute_install_deps=execute_install_deps,
        )

        if resuming:
            # The session is the orchestrator's record of truth; align the SM to it.
            self.sm.current_state = self.run_session.get_state()
            self.sm.context = self.run_session.get_context()
        else:
            # Fresh run: optionally seed the SM's context so guard preconditions
            # can be satisfied (until handlers set those flags themselves).
            if context is not None:
                self.sm.context = (
                    context if isinstance(context, Context) else Context(**context)
                )
            if researcher_id:
                self.run_session.researcher_id = researcher_id
            self.run_session.set_state(self.sm.current_state)
            self.run_session.set_context(self.sm.context)

        # ---- provenance log (best-effort audit trail) ------------------------
        self.event_log = None
        if provenance:
            path = self.run_session.provenance_log_path
            if not path:
                path = str(self.session.checkpoint_dir / f"{self.session_id}.events.jsonl")
                self.run_session.provenance_log_path = path
            try:
                self.event_log = event_log.EventLog(path)
            except Exception:
                self.event_log = None

    # ------------------------------------------------------------------ helpers
    @property
    def run_session(self) -> RunSession:
        return self.session.run_session

    def _checkpoint(self) -> None:
        self.session.checkpoint()
        if self.store is not None:
            self.store.save_session(self.run_session.to_dict())

    def _publish(self, event_type: str, payload: Dict, priority=Priority.DEFAULT) -> None:
        if self.event_bus is None:
            return
        try:
            event = Event(
                event_type=event_type,
                timestamp=datetime.now(timezone.utc),
                source_agent_id=f"orchestrator:{self.session_id}",
                payload=json.dumps(payload, default=str),
                trace_id=self.session_id,
            )
            self.event_bus.publish(event, priority)
        except Exception:
            pass  # a flaky bus must never break the run

    def _provenance(self, state: State) -> None:
        event_type = _PROVENANCE_EVENT_TYPE.get(state)
        if not event_type or self.event_log is None:
            return
        try:
            self.event_log.append(
                event_type=event_type,
                agent_id=f"statemachine:{state.name}",
                inputs={"state": state.name},
                outputs=_jsonable(dict(self.sm.context.artifacts)),
                decision_rationale=f"handler {state.name.lower()}() executed",
            )
        except Exception:
            pass  # provenance is best-effort; never fail the run for the audit log

    def _sync_agent_cost(self) -> None:
        """Pull accumulated cost and API quota from the live agent."""
        agent = getattr(self.sm, "_agent", None)
        if agent is None:
            return
        if hasattr(agent, "total_cost"):
            new_cost = agent.total_cost - self.run_budget.cost
            if new_cost > 0:
                self.run_budget.add_cost(new_cost)
        if hasattr(agent, "api_quota_remaining"):
            self.budget_tracker.update_quota(
                agent.api_quota_prior,
                agent.api_quota_remaining,
            )

    def _check_budget(self) -> None:
        """Raise if the run has exceeded its cost, iteration, or wall-time limit."""
        self.run_budget.check()

    def _write_budget_artifact(self) -> None:
        """Persist the current budget snapshot as an artifact."""
        data = {
            "run": self.run_budget.to_dict(),
            "global": self.budget_tracker.to_dict(),
        }
        path = self.sm._write_artifact("budget", data)
        self.sm.context.artifacts["budget"] = path

    # --------------------------------------------------------------------- step
    def _advance(self, state: State) -> None:
        """Run exactly one StateMachine transition under the stage timeout.

        ``StateMachine.run()`` invokes the handler for the current state (its
        work + chosen next state), checks the guard, and commits the transition.
        We wrap it in :func:`run_agent` so the stage gets its timeout, then the
        whole call goes through the :class:`ResilientCaller` (retry policy +
        circuit breaker) so transient failures get exponential-backoff retries
        and repeated failures trip the breaker before we burn the budget.
        """
        timeout = timeout_for(state.name) if self.step_timeouts else None

        def _step():
            run_agent(
                lambda _spec: self.sm.run(),
                {},
                state_name=state.name,
                timeout=timeout,
                max_retries=0,
            )

        self.resilient_caller.execute(_step)

    def _apply_loop_bounds(self, entered: State) -> None:
        """Bound replan/correct cycles, observed from the machine's transitions."""
        if entered == State.REPLAN:
            self.run_session.replan_count += 1
            if self.run_session.replan_count > self.max_replans:
                raise PolicyError(
                    f"replan limit reached ({self.max_replans}); aborting to prevent an infinite loop",
                    hint="Validation kept rejecting the result. Adjust the plan, the acceptance "
                         "criteria, or the input data before re-running.",
                )
        elif entered == State.CORRECT:
            self.run_session.correct_count += 1
            if self.run_session.correct_count > self.max_corrections:
                raise PolicyError(
                    f"correction limit reached ({self.max_corrections}); aborting to prevent an infinite loop",
                    hint="Self-correction is not converging. Escalate to the researcher.",
                )

    # ----------------------------------------------------------------- error path
    def _handle_error(self, exc: Exception, state: State) -> RunStatus:
        classified = error_handler.classify(exc, state.name)
        self.last_error = classified
        self.run_session.error = classified.to_dict()
        self.run_session.set_status(RunStatus.ERROR)
        self._checkpoint()
        error_handler.notify_researcher(
            classified, session_id=self.session_id, state=state.name, notifier=self.notifier
        )
        self._publish(
            "run.error",
            {"state": state.name, "error": classified.to_dict()},
            priority=Priority.CRITICAL,
        )
        return RunStatus.ERROR

    # ----------------------------------------------------------------------- run
    def run(self, until: Optional[State] = None) -> RunStatus:
        """Drive the machine until TERMINATE, an error, or ``until`` (exclusive).

        Returns the terminal :class:`RunStatus`. Safe to call again on the same
        instance (or a fresh one built from the same ``session_id``) to resume.
        """
        if self.run_session.get_status() == RunStatus.COMPLETED:
            return RunStatus.COMPLETED

        self.run_session.set_status(RunStatus.RUNNING)
        self.run_session.error = None
        self._checkpoint()
        self._publish("run.started", {"state": self.sm.current_state.name})

        while True:
            state = self.sm.current_state

            if state == State.TERMINATE:
                self.run_session.set_status(RunStatus.COMPLETED)
                self._checkpoint()
                self._publish("run.completed", {"state": state.name})
                return RunStatus.COMPLETED

            if until is not None and state == until:
                self.run_session.set_status(RunStatus.PAUSED)
                self._checkpoint()
                self._publish("run.paused", {"state": state.name})
                return RunStatus.PAUSED

            self._publish("stage.started", {"state": state.name})

            # 0) pre-step budget gate
            try:
                self._check_budget()
            except (OverBudget, OverMaxIterations, OverMaxWallTime) as exc:
                return self._handle_error(exc, state)

            # 1) step the state machine (retries transient errors, circuit-breaks
            #    on repeated failures)
            try:
                self._advance(state)
            except AlreadyOpenError as exc:
                return self._handle_error(
                    PolicyError(
                        str(exc),
                        hint="Too many consecutive stage failures tripped the circuit "
                             "breaker. Investigate the root cause, then resume.",
                    ),
                    state,
                )
            except Exception as exc:  # noqa: BLE001 -- classified & surfaced below
                return self._handle_error(exc, state)

            entered = self.sm.current_state

            # 1b) post-step: sync agent cost and count the iteration
            self._sync_agent_cost()
            self.run_budget.add_iteration()

            # 2) mirror the machine's new state/context into the session
            self.run_session.set_state(entered)
            self.run_session.set_context(self.sm.context)
            self.run_session.transition_count += 1
            self._provenance(state)

            # 3) bound replan/correct cycles
            try:
                self._apply_loop_bounds(entered)
            except Exception as exc:  # noqa: BLE001
                return self._handle_error(exc, state)

            # 4) checkpoint the new good state + budget, then announce the transition
            self._write_budget_artifact()
            self._checkpoint()
            self._publish("stage.completed", {
                "from": state.name, "to": entered.name,
                "budget": self.run_budget.to_dict(),
            })

            # 5) hard safety net against runaway transition counts
            if self.run_session.transition_count > self.max_transitions:
                return self._handle_error(
                    PolicyError(
                        f"exceeded max transitions ({self.max_transitions}); aborting suspected loop",
                        hint="The pipeline kept transitioning without terminating.",
                    ),
                    entered,
                )

    # ----------------------------------------------------------------- demo entry
    @classmethod
    def demo(cls, session_id: Optional[str] = None, **kwargs) -> "Orchestrator":
        """Drive the real ``StateMachine`` with a seeded happy context + real bus.

        The seed lets the stub handlers clear their guards so the run reaches
        TERMINATE today; as handlers gain logic that sets ``clarified`` /
        ``plan_approved`` / ... themselves, the seed becomes unnecessary.

        Local execution is enabled by default here (Story 5.2), so a real run
        actually executes the RunBundle built in BUILD and records an
        ``execution_result`` artifact. Callers may override via kwargs.
        """
        from event_bus import EventBus
        kwargs.setdefault("execute_locally", True)
        return cls(
            session_id=session_id,
            researcher_id="demo@twain.local",
            context=dict(HAPPY_CONTEXT),
            event_bus=EventBus(),
            store=Store(),
            **kwargs,
        )


def _main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Drive the TWAIN StateMachine end to end (Story 5.3). "
                    "Pass a SESSION_ID to resume a previous run from its last "
                    "checkpoint; omit it to start a fresh run.",
    )
    parser.add_argument(
        "session_id", nargs="?", default=None,
        help="Resume the run with this id (from logs/sessions/<id>.json or the "
             "session store). Omit to start a new run.",
    )
    parser.add_argument(
        "--no-execute", action="store_true",
        help="skip local execution of the RunBundle (BUILD still generates it)",
    )
    parser.add_argument(
        "--install-deps", action="store_true",
        help="build a venv and pip-install the bundle's requirements before running "
             "(needed when the selected tool isn't already importable)",
    )
    args = parser.parse_args(argv)

    if args.session_id and Session.exists(args.session_id):
        print(f"▶ resuming run {args.session_id} from its last checkpoint")
    elif args.session_id:
        print(f"▶ no checkpoint for {args.session_id}; starting a new run under that id")
    else:
        print("▶ starting a new run (driving StateMachine)")

    orch = Orchestrator.demo(
        session_id=args.session_id,
        execute_locally=not args.no_execute,
        execute_install_deps=args.install_deps,
    )
    status = orch.run()
    rs = orch.run_session
    print(f"\n■ run {orch.session_id} finished: {status.value}")
    print(f"  final state : {rs.state}")
    print(f"  transitions : {rs.transition_count}")
    b = orch.run_budget.to_dict()
    print(f"  budget      : ${b['cost']:.4f} / ${b['max_cost']:.4f} "
          f"({b['iterations']} iterations, {b['elapsed_seconds']:.0f}s elapsed)")
    if orch.event_bus is not None:
        print(f"  events      : {len(orch.event_bus.history)} published")
    if orch.event_log is not None:
        print(f"  provenance  : {len(orch.event_log.read_all())} events @ {rs.provenance_log_path}")
    print(f"  checkpoint  : {orch.session.path()}")
    print(f"  sm recovery : {orch.sm.storage.path}")
    return 0 if status == RunStatus.COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(_main())
