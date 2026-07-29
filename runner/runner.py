"""The runner service: claim queued jobs and drive the pipeline.

One *job* is one slice of work on a run, not a whole run. A run advances until it
needs the researcher — a CLARIFY question, the heavy-calc confirmation, or the
plan-approval gate — at which point it is checkpointed to the shared session
store and the process is **released** (the job is done). Nothing blocks waiting
on a human. When the user replies, the API enqueues a ``resume`` job and a runner
picks the run back up from its checkpoint. A ``start`` job runs the first slice;
each ``resume`` runs the next. A ``rerun`` job rewinds a finished run to an
earlier stage and drives it forward again.

Because no process is pinned to a waiting run, one runner serves many runs, and
any idle runner can resume any run — no work is wasted spinning on ``sleep``.

Resume durability: the run's state + context resume from the Postgres session
store, and stage artifacts (intent_spec, the run bundle, …) are made durable too
— ``capture_artifacts`` writes their contents to Postgres each slice and
``rehydrate_artifacts`` restores them to local disk before a resume drives the
run. So any runner can resume any run, even on a fresh box or after ``logs/`` was
cleaned; no shared ``logs/`` volume is required.

Terminate: pressing Terminate posts a ``kind='terminate'`` message; the
orchestrator checks it between stages (raising RunCancelled) and the Slurm poll
loop scancels the job. process_job treats a run that ends while terminate is set
as *cancelled* (not failed).

Usage::

    pixi run python -m runner.runner            # loop forever (LISTEN/NOTIFY)
    pixi run python -m runner.runner --once     # process at most one job (dev/CI)
"""
import argparse
import os
import threading
import time

from runner.artifacts import (
    capture_artifacts,
    rehydrate_artifacts,
    rematerialize_inputs,
)
from runner.bridges import DbAsk, PgEventSink, consume_approval, post_plan_for_approval
from runner.db import JobNotifyWaiter, RunnerDB
from runner.engine import _env_flag, default_engine
from runner.notifications import default_notifier, make_notifier
from runner.pg_store import PgStore

# Safety-net poll cadence for the loop. With LISTEN/NOTIFY the runner wakes the
# instant a job is queued, so this only bounds how long a *missed* notification
# could sit unclaimed — it no longer gates latency, so it can be generous.
DEFAULT_POLL_SECONDS = 30.0


def _env_int(name: str, default: int) -> int:
    """Read a positive-int env override, falling back to ``default`` if unset/bad."""
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


# Job-lease / crash-recovery knobs. While a job runs, the runner bumps its
# heartbeat every HEARTBEAT seconds. If a runner dies, its heartbeat goes stale
# and after LEASE seconds the reaper re-queues the job (or dead-letters it once it
# has been attempted MAX_ATTEMPTS times). LEASE only has to outlast a few missed
# heartbeats — NOT the longest slice — because a healthy multi-hour EXECUTE keeps
# beating, so recovery after a real crash takes ~LEASE rather than hours.
DEFAULT_LEASE_SECONDS = _env_int("TWAIN_JOB_LEASE_SECONDS", 600)
DEFAULT_HEARTBEAT_SECONDS = _env_int("TWAIN_JOB_HEARTBEAT_SECONDS", 60)
DEFAULT_MAX_ATTEMPTS = _env_int("TWAIN_JOB_MAX_ATTEMPTS", 3)

SUPPORTED_JOB_KINDS = ("start", "resume", "rerun")


def _state_name(state_obj) -> str:
    """Normalize a State enum (real engine) or bare string (tests) to its name."""
    return getattr(state_obj, "name", state_obj)


def _budget_warning(plan: dict | None, run_budget) -> str | None:
    """A warn-only heads-up when a plan's estimated cost exceeds the run budget.

    The budget caps *actual* LLM spend at runtime; the plan's ``cost_estimate``
    is a pre-run figure, so this only flags the risk — it never blocks the run.
    Returns the message to post, or ``None`` when there's nothing to warn about.
    """
    if not plan or run_budget is None:
        return None
    max_cost = getattr(run_budget, "max_cost", None)
    if not max_cost:
        return None
    estimate = (plan.get("cost_estimate") or {}).get("min_cost")
    if not isinstance(estimate, (int, float)) or estimate <= max_cost:
        return None
    return (
        f"⚠️ Heads up: the estimated cost (~${estimate:.2f}) is above this "
        f"run's budget of ${max_cost:.2f}. You can still approve it, but the run "
        f"will stop automatically if actual LLM spend reaches the budget."
    )


def _cancel_check(db: RunnerDB, session_id: str):
    """Zero-arg callable: True once the user pressed Terminate for this run."""
    return lambda: db.terminate_requested(session_id)


def _finalize_cancelled(db: RunnerDB, session_id: str) -> None:
    db.set_conversation_status(session_id, "cancelled")
    db.add_assistant_message(
        session_id,
        "Run terminated by user. Nothing further will be built or executed.",
        kind="chat",
    )


def _announce_compute_target(db: RunnerDB, session_id: str, engine, orch) -> None:
    """Announce the backend early so the chat shows RIS vs local before planning."""
    if engine.compute_target_of(orch) == "slurm":
        cluster = engine.slurm_cluster_of(orch) or "compute2"
        db.add_assistant_message(
            session_id,
            f"Compute target: RIS cluster via Slurm ({cluster}). "
            "The run will be submitted to the HPC queue after you approve the plan.",
            kind="chat",
        )
    else:
        db.add_assistant_message(
            session_id, "Compute target: this server (local / Docker).", kind="chat",
        )


def _drive_run(db: RunnerDB, session_id: str, orch, engine, notifier=default_notifier) -> None:
    """Advance the run until it completes, errors, or suspends for the user.

    Suspensions (CLARIFY input, heavy-calc confirmation) and the plan-approval
    gate each *release the process*: the run is checkpointed and we return. A
    ``resume`` job — enqueued by the API when the user responds — continues it.
    The loop lets a single resume cross the approval gate straight into execution
    (e.g. an already-approved plan, or unattended mode) without a second job.
    A terminate request surfaces as RunCancelled from ``orch.run`` and propagates
    to ``process_job``, which records the cancellation.
    """
    build_state = _state_name(engine.STATE_BUILD)
    while True:
        state = engine.current_state_name(orch)

        # ---- plan-approval gate (driver-level, at BUILD) ---------------------
        if state == build_state:
            if not _cross_approval_gate(db, session_id, orch, engine, notifier):
                return  # awaiting the user's decision (or rejected); released
            status = orch.run()  # leg 2: build → execute → … → terminate
        else:
            status = orch.run(until=engine.STATE_BUILD)  # leg 1 / continue to gate

        state = engine.current_state_name(orch)
        if status == "paused" and state == build_state:
            continue  # reached the approval gate; handle it on the next iteration
        if status == "paused":
            return  # suspended for user input (clarify / heavy-calc); released
        if status == "completed":
            summary = engine.final_summary(orch)
            db.add_assistant_message(session_id, summary, kind="chat", state="TERMINATE")
            notifier(session_id, "completed", summary)
            return
        if status == "error":
            fail_msg = "The run failed — see the run log for details."
            db.add_assistant_message(session_id, fail_msg, kind="chat")
            notifier(session_id, "failed", fail_msg)
            return
        # Reached a terminal state without pausing (e.g. empty discovery).
        summary = engine.final_summary(orch)
        db.add_assistant_message(session_id, summary, kind="chat")
        notifier(session_id, "completed", summary)
        return


def _cross_approval_gate(db, session_id, orch, engine, notifier) -> bool:
    """Handle the BUILD approval gate. True to proceed into execution, else release.

    Unattended mode (TWAIN_AUTO_RUN) approves automatically. Otherwise: consume
    the user's decision if it's in; if not, post the plan (with the compute target
    + any budget heads-up) and release; a 'reject' stops the run. Crossing the gate
    records a real plan approval (``engine.approve_plan``) so the BUILD→REPAIR /
    REPAIR→EXECUTE guards let the run proceed, and applies any Slurm resource
    overrides the user edited on the approval card.
    """
    if _env_flag("TWAIN_AUTO_RUN"):
        engine.approve_plan(orch)
        db.add_assistant_message(
            session_id, "Plan auto-approved (unattended mode). Building and executing…",
            kind="chat", state="BUILD",
        )
        return True

    decision, slurm_overrides = consume_approval(db, session_id)
    if decision is None:
        # No decision yet: show the plan (idempotently) with any budget warning,
        # mark awaiting, release.
        plan = engine.read_execution_plan(orch)
        warning = _budget_warning(plan, getattr(orch, "run_budget", None))
        if warning:
            db.add_assistant_message(session_id, warning, kind="chat", state="PLAN")
        post_plan_for_approval(
            db, session_id, plan, notifier=notifier,
            compute_target=engine.compute_target_of(orch),
            slurm_cluster=engine.slurm_cluster_of(orch),
        )
        return False
    if decision != "approve":
        db.set_conversation_status(session_id, "rejected")
        db.add_assistant_message(
            session_id,
            "Plan rejected — nothing was built or executed. "
            "Start a new run, or re-run this one from an earlier step "
            "(e.g. Discover or Plan) to try a different approach.",
            kind="chat",
        )
        return False
    engine.approve_plan(orch)
    if slurm_overrides:
        engine.apply_slurm_overrides(orch, slurm_overrides)
        db.add_assistant_message(
            session_id, "Plan approved with updated Slurm settings. Building and executing…",
            kind="chat", state="BUILD",
        )
    else:
        db.add_assistant_message(
            session_id, "Plan approved. Building and executing…", kind="chat", state="BUILD"
        )
    return True


def _build_orchestrator(engine, db: RunnerDB, session_id: str, params: dict, notifier, cancel):
    """Wire an orchestrator for this session with the chat/event/store bridges."""
    return engine.build_orchestrator(
        session_id=session_id,
        researcher_id=params.get("researcher_id", ""),
        request=params.get("request"),
        ask=DbAsk(db, session_id, notifier=notifier),
        sink=PgEventSink(db, session_id),
        store=PgStore(db),
        # Per-run execution backend picked in the UI ('local' | 'slurm');
        # None falls back to the runner's env-configured default.
        compute_target=params.get("compute_target"),
        # Terminate button: checked between stages (raises RunCancelled).
        cancel=cancel,
        # Per-run budget override (falls back to the deployment default in engine).
        max_cost=params.get("max_cost"),
    )


def process_job(job: dict, db: RunnerDB, engine=None) -> None:
    """Drive one slice of a run, then capture its artifacts.

    ``start`` runs the first slice of a fresh pipeline; ``resume`` continues a
    checkpointed run after the user responds. ``rerun`` resumes an existing run,
    rewinds it to the requested pipeline stage (discarding that stage's and every
    later stage's output while keeping the earlier work), restores the upstream
    inputs to disk, and drives it forward again through the same approval gate.
    """
    engine = engine or default_engine()
    session_id = job["session_id"]
    kind = job.get("kind", "start")
    if kind not in SUPPORTED_JOB_KINDS:
        raise NotImplementedError(f"job kind '{kind}' is not supported yet")

    params = job.get("params") or {}
    # Owner-targeted notifications (Phase 2): resolve the run's owner via the DB so
    # a suspend pings the specific researcher who left it. Terminate seam: a
    # zero-arg callable that's True once the user pressed Terminate for this run.
    notifier = make_notifier(db)
    cancel = _cancel_check(db, session_id)
    # On resume/rerun the orchestrator rebuilds its state + context from the session
    # store; request/researcher_id are only needed to *start* a run.
    orch = _build_orchestrator(engine, db, session_id, params, notifier, cancel)

    if kind == "rerun":
        target = params.get("target_state")
        if not target:
            raise ValueError("a 'rerun' job requires a 'target_state' param")
        # Rewind the restored run to `target`, then restore just the surviving
        # upstream specs to disk so the re-run's stages find their inputs (a fresh
        # process has none of the original run's artifact files on disk).
        engine.rewind(orch, target)
        rematerialize_inputs(db, session_id, orch)
        db.add_assistant_message(
            session_id, f"↩︎ Re-running from {target}…", kind="chat", state=target,
        )
    else:
        # A fresh start announces where it will execute (RIS vs local) up front.
        if kind == "start":
            _announce_compute_target(db, session_id, engine, orch)
        # Resume-safety: the orchestrator restored state + context (artifact *paths*)
        # from Postgres, but the files themselves may be absent on this box (fresh
        # runner, or logs/ was cleaned). Restore them from the DB before driving so
        # _load_artifact and EXECUTE find their inputs. No-op on a start (nothing
        # stored yet) and on a same-box resume (files already present).
        rehydrate_artifacts(db, session_id, orch)

    try:
        _drive_run(db, session_id, orch, engine, notifier=notifier)
        if cancel():
            # Terminate arrived too late to interrupt anything; still record it.
            _finalize_cancelled(db, session_id)
    except Exception as exc:  # noqa: BLE001 -- cancelled runs end via exceptions
        # A terminate request aborts stages by raising RunCancelled from the
        # orchestrator. Whatever the exception type, if the user asked to stop,
        # this is a cancellation -- not a run failure -- so don't let the loop
        # dead-letter it.
        if not cancel():
            raise
        print(f"[runner] run {session_id} terminated by user ({exc})")
        _finalize_cancelled(db, session_id)
    finally:
        # Best-effort: persist the specs + generated code so the report can show
        # them (also on a suspend, so partial artifacts are visible while waiting).
        try:
            capture_artifacts(db, session_id, orch)
        except Exception as exc:  # never fail the job over artifact capture
            print(f"[runner] artifact capture failed for {session_id}: {exc}")


class _Heartbeat:
    """Keeps a claimed job's lease alive for as long as its slice runs.

    A daemon thread bumps ``jobs.heartbeat_at`` every ``interval`` seconds so the
    reaper won't reclaim a healthy but long-running slice (e.g. a multi-hour
    EXECUTE that has shelled out to a DFT engine, releasing the GIL). When the
    runner process dies the beats stop, the lease expires, and
    :meth:`RunnerDB.reap_stale_jobs` re-queues the job. Best-effort: a failed beat
    is swallowed (the next beat, or the reaper, covers it) and never disturbs the
    run. ``interval <= 0`` disables it (used by tests).
    """

    def __init__(self, db, job_id, interval: float):
        self._db = db
        self._job_id = job_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._interval <= 0:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # Event.wait doubles as the sleep, so stop() interrupts it immediately.
        while not self._stop.wait(self._interval):
            try:
                self._db.heartbeat_job(self._job_id)
            except Exception:  # noqa: BLE001, S110 -- a missed beat is not fatal
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


def _reap_orphans(db: RunnerDB, lease_seconds: float, max_attempts: int) -> None:
    """Recover jobs whose runner died mid-slice; fail the ones out of attempts.

    Re-queued jobs are picked up by the next ``claim_job`` (this runner on its
    next turn, or any peer within the fallback poll). Dead-lettered ones are
    surfaced to the user here so the conversation doesn't sit silently wedged.
    """
    for dead in db.reap_stale_jobs(lease_seconds, max_attempts):
        db.set_conversation_status(dead["session_id"], "error")
        db.add_assistant_message(
            dead["session_id"],
            f"Run failed to recover after {dead['attempts']} attempts — please retry.",
            kind="chat",
        )


def _handle_job_failure(db: RunnerDB, job: dict, exc: Exception, max_attempts: int) -> None:
    """Retry a failed job (re-queue) until its attempts are exhausted, then fail it.

    A transient failure — a VPN/LLM-gateway blip, a stage timeout — should not kill
    a run. We re-queue it (``claim_job`` will re-drive from the last checkpoint and
    bump ``attempts``); only once it has been attempted ``max_attempts`` times do we
    dead-letter it and tell the user.
    """
    attempt = job.get("attempts", 1)
    if attempt >= max_attempts:
        db.mark_job(job["id"], "error")
        db.set_conversation_status(job["session_id"], "error")
        db.add_assistant_message(
            job["session_id"], f"Run failed after {attempt} attempt(s): {exc}", kind="chat"
        )
    else:
        db.mark_job(job["id"], "queued")


def run_loop(
    *, once: bool = False, poll: float = DEFAULT_POLL_SECONDS, db: RunnerDB | None = None,
    engine_factory=default_engine, sleep=time.sleep, waiter=None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> None:
    """Claim and process jobs until interrupted (or one job when ``once``).

    When idle, the loop waits on ``waiter`` (Postgres LISTEN/NOTIFY) so a newly
    queued job wakes it immediately; ``poll`` is only the fallback cadence. If no
    ``waiter`` is given it falls back to ``sleep(poll)`` (used by ``--once`` and
    the unit tests, which never idle).

    Crash recovery: each iteration first reaps jobs orphaned by a dead runner
    (heartbeat older than ``lease_seconds``) — re-queuing them, or dead-lettering
    ones already tried ``max_attempts`` times. While a job runs a heartbeat keeps
    its lease alive; an in-process failure is re-queued until it has been attempted
    ``max_attempts`` times, then the conversation is failed.
    """
    db = db or RunnerDB()
    # None forces a reap on the first iteration. (time.monotonic() is seconds
    # since an arbitrary epoch -- boot on Linux -- so seeding with 0.0 skips
    # the first reap on a freshly booted machine, e.g. a CI VM.)
    last_reap = None
    reap_interval = max(heartbeat_seconds, 1.0)
    while True:
        now = time.monotonic()
        if last_reap is None or now - last_reap >= reap_interval:
            _reap_orphans(db, lease_seconds, max_attempts)
            last_reap = now

        job = db.claim_job()
        if job is None:
            if once:
                return
            if waiter is not None:
                waiter.wait(poll)
            else:
                sleep(poll)
            continue

        heartbeat = _Heartbeat(db, job["id"], heartbeat_seconds)
        try:
            db.mark_job(job["id"], "running")
            heartbeat.start()
            process_job(job, db, engine=engine_factory())
            db.mark_job(job["id"], "done")
        except Exception as exc:  # noqa: BLE001 -- a bad job must not kill the loop
            _handle_job_failure(db, job, exc, max_attempts)
        finally:
            heartbeat.stop()
        if once:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="TWAIN pipeline runner")
    parser.add_argument("--once", action="store_true", help="process at most one job then exit")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS,
                        help="fallback poll cadence in seconds (LISTEN/NOTIFY handles latency)")
    args = parser.parse_args()
    db = RunnerDB()
    if args.once:
        run_loop(once=True, poll=args.poll, db=db)
        return
    # Loop forever, waking on a NOTIFY the instant a job is queued.
    with JobNotifyWaiter(db) as waiter:
        run_loop(once=False, poll=args.poll, db=db, waiter=waiter)


if __name__ == "__main__":
    main()
