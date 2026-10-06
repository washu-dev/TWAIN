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
    BudgetTracker, ProjectBudget, RunBudget,
    OverBudget, OverMaxIterations, OverMaxWallTime,
)
from retry_policy import (
    ResilientCaller, CircuitBreaker, RetryPolicy, AlreadyOpenError,
    classify_error, ErrorType,
)


# Pipeline state -> provenance event_type (only the six the schema allows).
# DECOMPOSE/DISCOVER/PLAN are all planning-phase work, so they log as "plan".
class RunCancelled(RuntimeError):
    """The researcher terminated the run (web UI Terminate button).

    Raised out of :meth:`Orchestrator.run` (never converted to an error card)
    so the driver -- the runner service -- can mark the conversation
    'cancelled' instead of 'error'.
    """


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
        # Attempts to ADD after a transient stage failure. The machinery for this
        # was already here -- classification, exponential backoff with jitter, the
        # circuit breaker -- and was wired to zero, so a single blip killed a run
        # outright: a silicon band-gap run died at INTAKE on an HTTP 403 from the
        # model API and the researcher's manual re-run, 22 seconds later, sailed
        # through unchanged (bd0677f6). One retry absorbs that; a genuinely wrong
        # credential still fails, one attempt later, with the same hint.
        step_retries: int = 1,
        # Backstops against a state machine that will not stop looping -- NOT
        # the intended limit. The state machine's own rerun controller bounds
        # the correction loop gracefully (it delivers the flagged result for
        # review); these must stay above its budget, or they abort the run
        # first and the researcher loses the result and the rationale instead
        # of receiving them flagged. See StateMachine._gate_rerun.
        max_replans: int = 6,
        max_corrections: int = 6,
        max_transitions: int = 100,
        budget_tracker: Optional[BudgetTracker] = None,
        run_max_cost: float = 1.0,
        run_max_iterations: int = 50,
        run_wall_time_minutes: int = 30,
        circuit_breaker_max_errors: int = 5,
        circuit_breaker_window: int = 60,
        circuit_breaker_cooldown: int = 300,
        execute_locally: bool = False,
        execute_install_deps: bool = False,
        verify_codegen: bool = False,
        auto_approve: bool = False,
        suspend_exc: Optional[type] = None,
        execute_slurm: bool = False,
        slurm_cluster: Optional[str] = None,
        cancel_check=None,
        job_event_wait=None,
        issue_job_ticket=None,
    ):
        self.session_id = session_id or uuid.uuid4().hex
        self.event_bus = event_bus              # None => events disabled (no-op)
        self.store = store
        self.notifier = notifier
        # Exception type the injected ``ask`` raises to pause a run for user input
        # (see runner.suspend.SuspendRun). When set, run() catches it and returns
        # PAUSED instead of erroring, so the caller can release the process and a
        # later ``resume`` re-enters the same state. None (CLI/demo/tests) => no
        # suspend path: an ask that blocks on stdin behaves exactly as before.
        self._suspend_exc = suspend_exc
        # Terminate seam (web UI's Terminate button): a zero-arg callable
        # returning True once the researcher asked to stop. Checked between
        # stages and before classifying any failure, so a cancelled run raises
        # RunCancelled instead of producing an error card.
        self.cancel_check = cancel_check
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
        # A fresh tracker is scoped to this run, so its global ceiling is the run
        # cost cap (keeps budget.json's "global" tier coherent with "run"); an
        # injected tracker keeps whatever cross-run ceiling the caller set.
        self.budget_tracker = budget_tracker or BudgetTracker(global_budget=run_max_cost)
        self.run_budget = RunBudget(
            max_cost=run_max_cost,
            max_iterations=run_max_iterations,
            wall_time_minutes=run_wall_time_minutes,
        )
        # Minutes added to the wall-time backstop for the plan's approved Slurm
        # allocation; None until a plan exists. See _allow_approved_compute.
        self._compute_allowance_minutes: Optional[float] = None
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
            # Verify LLM-synthesized calculator scripts (run --smoke in the sim env
            # and repair real failures) before handing them off. Best-effort:
            # degrades to a compile-only check when the env/network can't verify.
            verify_codegen=verify_codegen,
            # Unattended mode: skip the heavy-calculation confirmation prompt so a
            # run reaches completion without human input (see the runner's
            # TWAIN_AUTO_RUN). The plan-approval gate is enforced by the driver.
            auto_approve=auto_approve,
            # HPC route (Story 5.4): submit the RunBundle to the Slurm cluster
            # instead of running it locally/in Docker (see the runner's
            # TWAIN_EXECUTE_SLURM / TWAIN_SLURM_CLUSTER).
            execute_slurm=execute_slurm,
            slurm_cluster=slurm_cluster,
            # Terminate seam: lets a long EXECUTE (Slurm poll loop) notice the
            # researcher's terminate request and scancel the cluster job.
            should_abort=cancel_check,
            # RIS webhook seam: the Slurm poll sleep wakes on a job event.
            job_event_wait=job_event_wait,
            # S3 staging seam: per-attempt job tickets (#170).
            issue_job_ticket=issue_job_ticket,
        )

        # Let stages report what they're doing between stage events (the UI's
        # live checklist and job log). Set on injected machines too; one that
        # predates the seam just gains an unused attribute.
        self.sm.publish_progress = self._publish_activity

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

    #: Event types a stage may publish through ``StateMachine.publish_progress``.
    _ACTIVITY_EVENTS = ("stage.progress", "job.log")

    def _publish_activity(self, event_type: str, payload: Dict) -> None:
        """The state machine's activity publisher: in-stage progress + job log."""
        if event_type in self._ACTIVITY_EVENTS:
            self._publish(event_type, payload)

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

    def _allow_approved_compute(self) -> None:
        """Extend the wall-time backstop to cover the allocation the plan asked for.

        Applied once, as soon as an execution_plan exists. Without it the backstop
        measures a Slurm job the researcher approved against a clock sized for
        pipeline overhead: run 6e9c32ae asked for (and was granted) 4 hours,
        computed its bulk modulus in 34 minutes, and was then failed at INTERPRET
        by the 30-minute default -- the exact outcome this class of limit is
        documented as having to avoid, since the result is already paid for.

        Only the plan's own declared walltime is added, so a runaway pipeline is
        still bounded: the ceiling becomes overhead + what was approved, not
        unlimited.
        """
        if self._compute_allowance_minutes is not None:
            return
        try:
            plan = self.sm._load_artifact("execution_plan") or {}
        except Exception:  # noqa: BLE001 - no plan yet is the normal early case
            return
        hours = (plan.get("slurm_request") or {}).get("max_time")
        if not isinstance(hours, (int, float)) or hours <= 0:
            return
        minutes = float(hours) * 60.0
        self.run_budget.extend_wall_time(minutes)
        self._compute_allowance_minutes = minutes
        self._publish("run.budget_extended", {
            "reason": "approved Slurm allocation",
            "added_minutes": round(minutes, 1),
            "wall_time_limit_seconds": self.run_budget.wall_time,
        })

    def _check_budget(self) -> None:
        """Raise if the run has exceeded its cost, iteration, or wall-time limit."""
        self._allow_approved_compute()
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
        # A Slurm-routed EXECUTE legitimately outlives the default 2h stage
        # budget (the plan's wall time can be 4h+): stretch the stage timeout to
        # the machine's own wait budget so the stage isn't killed mid-poll.
        if (timeout is not None and state == State.EXECUTE
                and getattr(self.sm, "execute_slurm", False)):
            try:
                timeout = max(timeout, self.sm.slurm_wait_budget() + 5 * 60)
            except Exception:  # noqa: BLE001 - keep the default budget
                pass
        suspended: Dict[str, BaseException] = {}

        def _step():
            try:
                run_agent(
                    lambda _spec: self.sm.run(),
                    {},
                    state_name=state.name,
                    timeout=timeout,
                    max_retries=0,
                )
            except Exception as exc:  # noqa: BLE001 -- suspend re-raised past breaker
                # Pausing for user input is progress, not a stage failure: capture
                # the suspend and return so the circuit breaker records a success,
                # then re-raise it below (outside the breaker). Otherwise a handful
                # of clarify rounds would trip the breaker and abort the run.
                if self._suspend_exc is not None and isinstance(exc, self._suspend_exc):
                    suspended["exc"] = exc
                    return
                raise

        # Per-stage retry budget, sharing the run's circuit breaker so repeated
        # failures still trip it. EXECUTE gets NONE: its work is submitting a Slurm
        # job, and a second attempt is a second job -- another allocation, another
        # set of results, and a first job still running that nothing is waiting on.
        # Every other stage re-derives from artifacts already on disk, so attempting
        # it again is the same work, not extra work.
        retries = 0 if state == State.EXECUTE else self.step_retries
        policy = RetryPolicy(max_retries=retries,
                             base_delay=self.resilient_caller.retry_policy.base_delay,
                             jitter=self.resilient_caller.retry_policy.jitter)
        self.resilient_caller.circuit_breaker.execute(lambda: policy.execute(_step))
        if suspended:
            raise suspended["exc"]

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
    def _raise_if_cancelled(self) -> None:
        """Raise :class:`RunCancelled` once the researcher asked to terminate."""
        if self.cancel_check is not None and self.cancel_check():
            self.run_session.set_status(RunStatus.PAUSED)
            self._checkpoint()
            self._publish("run.cancelled",
                          {"state": self.sm.current_state.name},
                          priority=Priority.CRITICAL)
            raise RunCancelled(
                f"run {self.session_id} terminated by the researcher"
            )

    def _handle_error(self, exc: Exception, state: State) -> RunStatus:
        # A failure while termination is pending is a consequence of the
        # termination (aborted waits, cancelled Slurm job), not a run error:
        # don't alarm the researcher with an error card for their own action.
        self._raise_if_cancelled()
        classified = error_handler.classify(exc, state.name)
        self.last_error = classified
        # Where it stopped and why, in words the submitter can act on -- the
        # chat's failure card and the failure notification both read this.
        try:
            execution_result = (self.sm._load_artifact("execution_result")
                                if state.name == "EXECUTE" else None)
        except Exception:  # noqa: BLE001 - describing a failure must not fail
            execution_result = None
        self.last_failure = error_handler.describe_failure(
            classified, state.name, execution_result)
        self.run_session.error = classified.to_dict()
        self.run_session.set_status(RunStatus.ERROR)
        self._checkpoint()
        error_handler.notify_researcher(
            classified, session_id=self.session_id, state=state.name, notifier=self.notifier
        )
        self._publish(
            "run.error",
            {"state": state.name, "error": classified.to_dict(),
             "failure": self.last_failure},
            priority=Priority.CRITICAL,
        )
        return RunStatus.ERROR

    # --------------------------------------------------------------- suspend path
    def _suspend(self, state: State, exc: Exception) -> RunStatus:
        """Pause the run pending user input, checkpoint, and release the caller.

        The state-machine handler aborted mid-step -- it asked the researcher a
        question and no answer was waiting -- so :meth:`StateMachine.run` never
        committed a transition and ``current_state`` is unchanged. The run is
        checkpointed as PAUSED (a resumable status) and resumes by re-entering
        this same state once the answer arrives, at which point the ask returns
        it. Nothing is held open in the meantime.
        """
        self.run_session.set_status(RunStatus.PAUSED)
        self.run_session.set_state(self.sm.current_state)
        self.run_session.set_context(self.sm.context)
        self._checkpoint()
        self._publish(
            "run.suspended",
            {"state": self.sm.current_state.name, "reason": getattr(exc, "reason", "input")},
        )
        return RunStatus.PAUSED

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
            self._raise_if_cancelled()
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
                # A pause for user input unwinds the step as an exception; treat it
                # as a clean suspension (checkpoint + release), not an error.
                if self._suspend_exc is not None and isinstance(exc, self._suspend_exc):
                    return self._suspend(state, exc)
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

            # 3) bound replan/correct cycles. Attribute a trip to the state we
            #    just entered (the REPLAN/CORRECT we're now parked in), not the
            #    pre-step state, so the notification names the right stage.
            try:
                self._apply_loop_bounds(entered)
            except Exception as exc:  # noqa: BLE001
                return self._handle_error(exc, entered)

            # 4) checkpoint the new good state + budget, then announce the transition
            self._write_budget_artifact()
            self._checkpoint()
            self._publish("stage.completed", {
                "from": state.name, "to": entered.name,
                "budget": self.run_budget.to_dict(),
            })

            # 4a) park at the approval gate. Entering BUILD without an approved
            #     plan means the run needs the researcher: on a fresh run the
            #     driver already stops here via ``until``, but a replan re-enters
            #     BUILD mid-leg with a *new* plan and a cleared approval. Pausing
            #     hands it back to the driver to post a fresh approval card;
            #     driving on would run build() and then trip the BUILD->REPAIR
            #     guard, failing a run that is merely waiting on a human.
            if entered == State.BUILD and not self.sm.context.plan_approved:
                self.run_session.set_status(RunStatus.PAUSED)
                self._checkpoint()
                self._publish("run.paused", {"state": entered.name})
                return RunStatus.PAUSED

            # 4b) post-step budget gate. The pre-step gate (step 0) only sees the
            #     cost *before* this stage ran; re-check now that this stage's LLM
            #     spend has been synced so a run stops promptly once it hits the
            #     cap, rather than overshooting by up to one stage. A run that just
            #     reached TERMINATE is already done -- don't fail a finished run.
            if entered != State.TERMINATE:
                try:
                    self._check_budget()
                except (OverBudget, OverMaxIterations, OverMaxWallTime) as exc:
                    return self._handle_error(exc, entered)

            # 5) hard safety net against runaway transition counts
            if self.run_session.transition_count > self.max_transitions:
                return self._handle_error(
                    PolicyError(
                        f"exceeded max transitions ({self.max_transitions}); aborting suspected loop",
                        hint="The pipeline kept transitioning without terminating.",
                    ),
                    entered,
                )

    # --------------------------------------------------------------- rewind / rerun
    def rewind_to(self, target: State, *, reseed: Optional[Dict] = None) -> None:
        """Rewind this run to an earlier stage so :meth:`run` re-executes from it.

        Delegates the state/artifact/flag reset to :meth:`StateMachine.rewind_to`,
        then re-applies any guard ``reseed`` the driver relies on for the stubbed
        happy path (the runner seeds ``execution_status``/``validation_result`` so
        a planning-only run still reaches TERMINATE). It mirrors the machine's new
        state + context into the :class:`RunSession`, clears any terminal error,
        resets the loop counters, marks the run RUNNING, and checkpoints -- so the
        store a fresh runner resumes from reflects the rewound run.
        """
        self.sm.rewind_to(target)
        if reseed:
            for key, value in reseed.items():
                setattr(self.sm.context, key, value)
        self.run_session.set_state(self.sm.current_state)
        self.run_session.set_context(self.sm.context)
        self.run_session.set_status(RunStatus.RUNNING)
        self.run_session.error = None
        self.run_session.transition_count = 0
        self.run_session.replan_count = 0
        self.run_session.correct_count = 0
        self._checkpoint()
        self._publish("run.rewound", {"state": self.sm.current_state.name})

    def approve_plan(self, approved: bool = True) -> None:
        """Record the researcher's plan approval so the run may build/execute.

        Delegates to :meth:`StateMachine.approve_plan` (which sets + persists the
        ``plan_approved`` guard flag), then mirrors it into the :class:`RunSession`
        and checkpoints, so a run resumed in a fresh process still sees the
        approval. This is the ONLY way (outside an explicit context seed) that the
        guarded ``BUILD->REPAIR`` / ``REPAIR->EXECUTE`` transitions become allowed:
        without it a driven run halts at the approval gate before anything is
        built or executed.
        """
        self.sm.approve_plan(approved)
        self.run_session.set_context(self.sm.context)
        self._checkpoint()
        self._publish("run.plan_approved", {"approved": approved})

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
        # Real runs verify+repair generated calculator scripts before handoff.
        kwargs.setdefault("verify_codegen", True)
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
    import logging

    # CLI entrypoint: surface the pipeline's INFO-level progress (the stage
    # ``[clarify]``/``[repair]``/``[execute]`` lines) as plain messages. Kept out
    # of import-time so library/embedded use doesn't touch the root logger config.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

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
    parser.add_argument(
        "--slurm", action="store_true",
        help="submit the RunBundle to the Slurm cluster (configs/clusters/, default "
             "compute2) instead of executing locally; needs VPN + SSH key, or run "
             "on a login node with TWAIN_SLURM_HOST=''",
    )
    parser.add_argument(
        "--cluster", default=None,
        help="cluster profile name for --slurm (default: compute2)",
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
        execute_locally=not args.no_execute and not args.slurm,
        execute_install_deps=args.install_deps,
        execute_slurm=args.slurm,
        slurm_cluster=args.cluster,
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
