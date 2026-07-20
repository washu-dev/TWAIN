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

from runner.artifacts import capture_artifacts
from runner.bridges import DbAsk, PgEventSink, request_plan_approval
from runner.db import RunnerDB
from runner.engine import _env_flag, default_engine
from runner.pg_store import PgStore

DEFAULT_POLL_SECONDS = 2.0


def _drive_run(db: RunnerDB, session_id: str, orch, engine) -> None:
    """Run the two legs of the pipeline, gating on plan approval at BUILD."""
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

    # Approval gate: normally show the plan and block for the user's decision.
    # In unattended mode (TWAIN_AUTO_RUN) skip it and run straight through --
    # combined with execution being on, TWAIN runs the calculation automatically.
    if _env_flag("TWAIN_AUTO_RUN"):
        db.add_assistant_message(
            session_id, "Plan auto-approved (unattended mode). Building and executing…",
            kind="chat", state="BUILD",
        )
    else:
        decision = request_plan_approval(db, session_id, engine.read_execution_plan(orch))
        if decision != "approve":
            db.set_conversation_status(session_id, "rejected")
            db.add_assistant_message(
                session_id,
                "Plan rejected — nothing was built or executed. "
                "Start a new run, or (soon) rerun from an earlier step with changes.",
                kind="chat",
            )
            return
        db.add_assistant_message(
            session_id, "Plan approved. Building and executing…", kind="chat", state="BUILD"
        )

    # Leg 2: build → execute → interpret → validate → accept → terminate.
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


def process_job(job: dict, db: RunnerDB, engine=None) -> None:
    """Drive a single job's run, then capture its artifacts for the report."""
    engine = engine or default_engine()
    session_id = job["session_id"]
    kind = job.get("kind", "start")
    if kind != "start":
        raise NotImplementedError(f"job kind '{kind}' is not supported yet (Phase 3)")

    params = job.get("params") or {}
    orch = engine.build_orchestrator(
        session_id=session_id,
        researcher_id=params.get("researcher_id", ""),
        request=params.get("request"),
        ask=DbAsk(db, session_id),
        sink=PgEventSink(db, session_id),
        store=PgStore(db),
    )
    try:
        _drive_run(db, session_id, orch, engine)
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
