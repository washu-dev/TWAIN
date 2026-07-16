"""Bridges between the running pipeline and the chat database.

Three collaborators the runner injects into the orchestrator:

* :class:`DbAsk` — the ``ask`` callable. The state machine calls it during
  CLARIFY with a question string; we post it as an assistant message, then block
  until the user replies via ``POST /conversations/{id}/messages``.
* :func:`request_plan_approval` — after the pipeline pauses at BUILD (plan
  generated, nothing built yet), post the plan and block for the user's
  approve/reject via ``POST /conversations/{id}/approval``.
* :class:`PgEventSink` — a duck-typed event bus: the orchestrator calls
  ``publish(Event, priority)``; we append to ``run_events`` (tailed by the SSE
  endpoint) and mirror state/status onto the conversation row for the UI.
"""
import json
import time

from runner.db import RunnerDB

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_WAIT_TIMEOUT = 3600.0  # 1h: how long a run blocks waiting for the user


class ReplyTimeout(TimeoutError):
    """Raised when the user does not reply within the wait window."""


def _wait_for_reply(
    db: RunnerDB, session_id: str, after_id: int, kind: str | None,
    poll: float, timeout: float, sleep=time.sleep, monotonic=time.monotonic,
) -> str:
    deadline = monotonic() + timeout
    while True:
        replies = db.user_replies_after(session_id, after_id, kind=kind)
        if replies:
            return replies[0]["content"]
        if monotonic() >= deadline:
            raise ReplyTimeout(f"no user reply for {session_id} within {timeout}s")
        sleep(poll)


class DbAsk:
    """The ``ask`` callable: post a question, block for the user's chat reply."""

    def __init__(
        self, db: RunnerDB, session_id: str,
        poll: float = DEFAULT_POLL_SECONDS, timeout: float = DEFAULT_WAIT_TIMEOUT,
        sleep=time.sleep,
    ):
        self.db = db
        self.session_id = session_id
        self.poll = poll
        self.timeout = timeout
        self._sleep = sleep

    def __call__(self, message: str) -> str:
        baseline = self.db.max_message_id(self.session_id)
        self.db.add_assistant_message(
            self.session_id, message, kind="clarification", state="CLARIFY"
        )
        self.db.set_conversation_status(self.session_id, "awaiting_input")
        answer = _wait_for_reply(
            self.db, self.session_id, baseline, kind=None,
            poll=self.poll, timeout=self.timeout, sleep=self._sleep,
        )
        self.db.set_conversation_status(self.session_id, "running")
        return answer


def request_plan_approval(
    db: RunnerDB, session_id: str, plan: dict | None,
    poll: float = DEFAULT_POLL_SECONDS, timeout: float = DEFAULT_WAIT_TIMEOUT,
    sleep=time.sleep,
) -> str:
    """Post the plan for approval and block for the user's decision.

    Returns the raw decision string ('approve' or 'reject').
    """
    baseline = db.max_message_id(session_id)
    db.add_assistant_message(
        session_id,
        json.dumps(_plan_summary(plan)),
        kind="approval_request",
        state="PLAN",
    )
    db.set_conversation_status(session_id, "awaiting_approval")
    decision = _wait_for_reply(
        db, session_id, baseline, kind="approval_response",
        poll=poll, timeout=timeout, sleep=sleep,
    )
    return decision.strip().lower()


def _plan_summary(plan: dict | None) -> dict:
    """Trim an ExecutionPlan artifact to the fields worth showing for approval.

    Reads the fields where they actually live (goal_id under ``metadata``,
    ``cost_estimate`` / ``compute_estimate`` / ``safety_notes``), and leads with
    the plan's plain-language ``summary`` of what the run will do.
    """
    if not plan:
        return {"note": "No execution plan was produced."}
    metadata = plan.get("metadata") or {}
    return {
        "summary": plan.get("summary"),
        "goal_id": metadata.get("goal_id"),
        "target_system": plan.get("target_system"),
        "requested_property": plan.get("requested_property"),
        "selected_method": plan.get("selected_method"),
        "cost_estimate": plan.get("cost_estimate"),
        "compute_estimate": plan.get("compute_estimate"),
        "slurm_request": plan.get("slurm_request"),
        "acceptance_metrics": plan.get("acceptance_metrics"),
        "safety_notes": plan.get("safety_notes"),
    }


class PgEventSink:
    """Duck-typed event bus: persist events and mirror UI-facing state."""

    def __init__(self, db: RunnerDB, session_id: str):
        self.db = db
        self.session_id = session_id
        self._seq = 0

    def publish(self, event, priority=None) -> None:  # noqa: ARG002 (bus signature)
        try:
            payload = json.loads(event.payload) if isinstance(event.payload, str) else event.payload
        except (ValueError, TypeError):
            payload = {"raw": str(getattr(event, "payload", ""))}
        event_type = getattr(event, "event_type", "unknown")
        self.db.insert_run_event(self.session_id, event_type, payload or {}, seq=self._seq)
        self._seq += 1

        if event_type == "stage.completed" and payload.get("to"):
            self.db.set_conversation_state(self.session_id, payload["to"])
        elif event_type == "run.completed":
            self.db.set_conversation_status(self.session_id, "completed")
        elif event_type == "run.error":
            self.db.set_conversation_status(self.session_id, "error")
        elif event_type in ("run.started", "stage.started"):
            self.db.set_conversation_status(self.session_id, "running")
