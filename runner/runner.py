"""The runner service: claim queued jobs and drive the pipeline.

One job == one run. The process blocks (polling the DB) while the state machine
waits on the user — during CLARIFY (via :class:`DbAsk`) and at the plan-approval
gate. Run several runner processes/tasks to handle more concurrent runs.

Usage::

    pixi run python -m runner.runner            # loop forever
    pixi run python -m runner.runner --once     # process at most one job (dev/CI)
"""
import argparse
import time

from runner.artifacts import capture_artifacts, rematerialize_inputs
from runner.bridges import DbAsk, PgEventSink, request_plan_approval
from runner.db import RunnerDB
from runner.engine import _env_flag, default_engine
from runner.pg_store import PgStore

DEFAULT_POLL_SECONDS = 2.0


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


def _drive_run(db: RunnerDB, session_id: str, orch, engine) -> None:
    """Run the two legs of the pipeline, gating on plan approval at BUILD."""
    cancel = _cancel_check(db, session_id)
    # Leg 1: intake → clarify → decompose → discover → plan, pausing at BUILD.
    status = orch.run(until=engine.STATE_BUILD)
    if status == "error":
        db.add_assistant_message(
            session_id, "The run failed before planning — see the run log for details.",
            kind="chat",
        )
        return
    if status != "paused":
        # Reached a terminal state without a plan (e.g. empty discovery); finish up.
        db.add_assistant_message(session_id, engine.final_summary(orch), kind="chat")
        return

    plan = engine.read_execution_plan(orch)

    # Pre-flight budget check (warn only): if the plan's estimated cost is above
    # this run's budget, flag it before the user decides. The budget caps actual
    # LLM spend, so this is an early heads-up, not a hard gate.
    warning = _budget_warning(plan, getattr(orch, "run_budget", None))
    if warning:
        db.add_assistant_message(session_id, warning, kind="chat", state="PLAN")

    # Approval gate: normally show the plan and block for the user's decision.
    # In unattended mode (TWAIN_AUTO_RUN) skip it and run straight through --
    # combined with execution being on, TWAIN runs the calculation automatically.
    # Either way, the run only proceeds past BUILD once the plan is *explicitly*
    # approved: engine.approve_plan sets the plan_approved guard flag, so the
    # BUILD->REPAIR / REPAIR->EXECUTE guards (not just this block) enforce it.
    if _env_flag("TWAIN_AUTO_RUN"):
        db.add_assistant_message(
            session_id, "Plan auto-approved (unattended mode). Building and executing…",
            kind="chat", state="BUILD",
        )
        engine.approve_plan(orch)
    else:
        decision, slurm_overrides = request_plan_approval(
            db, session_id, plan,
            compute_target=engine.compute_target_of(orch),
            slurm_cluster=engine.slurm_cluster_of(orch),
            cancel=cancel,
        )
        if decision != "approve":
            db.set_conversation_status(session_id, "rejected")
            db.add_assistant_message(
                session_id,
                "Plan rejected — nothing was built or executed. "
                "Start a new run, or re-run this one from an earlier step "
                "(e.g. Discover or Plan) to try a different approach.",
                kind="chat",
            )
            return
        engine.approve_plan(orch)
        if slurm_overrides:
            engine.apply_slurm_overrides(orch, slurm_overrides)
            db.add_assistant_message(
                session_id,
                "Plan approved with updated Slurm settings. Building and executing…",
                kind="chat", state="BUILD",
            )
        else:
            db.add_assistant_message(
                session_id, "Plan approved. Building and executing…",
                kind="chat", state="BUILD",
            )

    # Leg 2: build → execute → interpret → validate → accept → terminate.
    # Without the approval above, plan_approved is False and this run() would halt
    # at the BUILD->REPAIR guard (GuardsBroken) — nothing is built or executed.
    status = orch.run()
    if status == "completed":
        db.add_assistant_message(
            session_id, engine.final_summary(orch), kind="chat", state="TERMINATE"
        )
    elif status == "error":
        db.add_assistant_message(
            session_id, "The run failed during execution — see the run log for details.",
            kind="chat",
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


def _build_orchestrator(engine, db: RunnerDB, session_id: str, params: dict, cancel=None):
    """Wire an orchestrator for this session with the chat/event/store bridges."""
    return engine.build_orchestrator(
        session_id=session_id,
        researcher_id=params.get("researcher_id", ""),
        request=params.get("request"),
        ask=DbAsk(db, session_id, cancel=cancel),
        sink=PgEventSink(db, session_id),
        store=PgStore(db),
        # Per-run execution backend picked in the UI ('local' | 'slurm');
        # None falls back to the runner's env-configured default.
        compute_target=params.get("compute_target"),
        # Terminate button: checked between stages and inside blocking waits.
        cancel=cancel,
        # Per-run budget override (falls back to the deployment default in engine).
        max_cost=params.get("max_cost"),
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
            session_id,
            "Compute target: this server (local / Docker).",
            kind="chat",
        )


def process_job(job: dict, db: RunnerDB, engine=None) -> None:
    """Drive a single job's run, then capture its artifacts for the report.

    ``start`` runs a fresh pipeline. ``rerun`` resumes an existing run, rewinds it
    to the requested pipeline stage (discarding that stage's and every later
    stage's output while keeping the earlier work), restores the upstream inputs
    to disk, and drives it forward again through the same approval gate.
    """
    engine = engine or default_engine()
    session_id = job["session_id"]
    kind = job.get("kind", "start")
    params = job.get("params") or {}
    cancel = _cancel_check(db, session_id)

    if kind == "start":
        orch = _build_orchestrator(engine, db, session_id, params, cancel=cancel)
        _announce_compute_target(db, session_id, engine, orch)
    elif kind == "rerun":
        target = params.get("target_state")
        if not target:
            raise ValueError("a 'rerun' job requires a 'target_state' param")
        # Resumes the existing run from the store, then rewinds it to `target`.
        orch = _build_orchestrator(engine, db, session_id, params, cancel=cancel)
        engine.rewind(orch, target)
        # A fresh process has none of the original run's artifact files on disk;
        # restore the surviving upstream specs so the re-run's stages find inputs.
        rematerialize_inputs(db, session_id, orch)
        db.add_assistant_message(
            session_id, f"↩︎ Re-running from {target}…", kind="chat", state=target,
        )
    else:
        raise NotImplementedError(f"job kind '{kind}' is not supported")

    try:
        _drive_run(db, session_id, orch, engine)
        if cancel():
            # Terminate arrived too late to interrupt anything; still record it.
            _finalize_cancelled(db, session_id)
    except Exception as exc:  # noqa: BLE001 -- cancelled runs end via exceptions
        # A terminate request aborts blocking waits / stages by raising
        # (RunCancelled from the bridges or the orchestrator). Whatever the
        # exception type, if the user asked to stop, this is a cancellation --
        # not a run failure.
        if not cancel():
            raise
        print(f"[runner] run {session_id} terminated by user ({exc})")
        _finalize_cancelled(db, session_id)
    finally:
        # Best-effort: persist the specs + generated code so the report can show them.
        try:
            capture_artifacts(db, session_id, orch)
        except Exception as exc:  # never fail the job over artifact capture
            print(f"[runner] artifact capture failed for {session_id}: {exc}")


def run_loop(
    *, once: bool = False, poll: float = DEFAULT_POLL_SECONDS, db: RunnerDB | None = None,
    engine_factory=default_engine, sleep=time.sleep,
) -> None:
    """Claim and process jobs until interrupted (or one job when ``once``)."""
    db = db or RunnerDB()
    while True:
        job = db.claim_job()
        if job is None:
            if once:
                return
            sleep(poll)
            continue
        try:
            db.mark_job(job["id"], "running")
            process_job(job, db, engine=engine_factory())
            db.mark_job(job["id"], "done")
        except Exception as exc:  # noqa: BLE001 -- a bad job must not kill the loop
            db.mark_job(job["id"], "error")
            db.set_conversation_status(job["session_id"], "error")
            db.add_assistant_message(job["session_id"], f"Run failed: {exc}", kind="chat")
        if once:
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="TWAIN pipeline runner")
    parser.add_argument("--once", action="store_true", help="process at most one job then exit")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args()
    run_loop(once=args.once, poll=args.poll)


if __name__ == "__main__":
    main()
