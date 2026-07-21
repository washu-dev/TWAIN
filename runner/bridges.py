"""Bridges between the running pipeline and the chat database.

These collaborators the runner injects into the orchestrator turn a run's need
for the researcher into a *pause*, not a blocked thread. The old model posted a
question and then spun on the DB (``sleep(1)``) for up to an hour; this one posts
the question, records that the run is waiting, and **suspends** — the runner
checkpoints and releases the process, and a ``resume`` job (enqueued by the API
when the user replies) drives the run onward.

* :class:`DbAsk` — the ``ask`` callable the state machine calls during CLARIFY
  (and for the heavy-calc confirmation). On the first ask it posts the question
  and raises :class:`~runner.suspend.SuspendRun`; on the resume it returns the
  answer the user gave.
* :func:`post_plan_for_approval` / :func:`consume_approval` — the plan-approval
  gate, driven at the runner level: post the plan and release, then consume the
  approve/reject decision on resume.
* :class:`PgEventSink` — a duck-typed event bus: the orchestrator calls
  ``publish(Event, priority)``; we append to ``run_events`` (tailed by the SSE
  endpoint) and mirror state/status onto the conversation row for the UI.

Pairing answers with questions
------------------------------
CLARIFY, the heavy-calc confirmation, and plan approval all post an assistant
*question* and read back a later user reply. To keep them from stealing each
other's answers (CLARIFY and heavy-calc even share the ``clarification`` kind),
a reply counts as "for this gate" only when the gate's question is the *most
recent question of any kind* in the transcript — i.e. nothing has been asked
since. Once the pipeline moves on and asks something else, an older answer is no
longer pending. See :func:`_fresh_reply`.
"""
import json

from runner.db import RunnerDB
from runner.notifications import default_notifier
from runner.suspend import SuspendRun

# The assistant message kinds that represent an outstanding question to the user.
_QUESTION_KINDS = ("clarification", "approval_request")


def _fresh_reply(db: RunnerDB, session_id: str, question_kind: str, reply_kind):
    """The user's pending reply for ``question_kind``, or None.

    Returns the reply content only when (a) the latest question of *any* kind is a
    ``question_kind`` question and (b) a user reply exists after it. This is what
    stops a stale CLARIFY answer being read as a heavy-calc or approval answer,
    and vice-versa.
    """
    latest_any = db.last_question_id(session_id, kinds=_QUESTION_KINDS)
    if latest_any is None:
        return None
    mine = db.last_question_id(session_id, kinds=(question_kind,))
    if mine is None or mine != latest_any:
        return None  # the current outstanding question belongs to a different gate
    replies = db.user_replies_after(session_id, mine, kind=reply_kind)
    return replies[0]["content"] if replies else None


def _has_outstanding_clarification(db: RunnerDB, session_id: str) -> bool:
    """True when a clarification question is the latest question and unanswered."""
    latest_any = db.last_question_id(session_id, kinds=_QUESTION_KINDS)
    mine = db.last_question_id(session_id, kinds=("clarification",))
    if mine is None or mine != latest_any:
        return False
    return not db.user_replies_after(session_id, mine, kind=None)


class DbAsk:
    """The ``ask`` callable: return a waiting answer, or post the question + suspend.

    One instance per run (per ``process_job``). ``_consumed`` guards against using
    one pending answer twice in the same process: after it's consumed, a *further*
    clarify round posts a fresh question and suspends again rather than reusing the
    previous answer.
    """

    def __init__(self, db: RunnerDB, session_id: str, notifier=default_notifier):
        self.db = db
        self.session_id = session_id
        self._notify = notifier
        self._consumed = False

    def __call__(self, message: str) -> str:
        if not self._consumed:
            answer = _fresh_reply(
                self.db, self.session_id, question_kind="clarification", reply_kind=None
            )
            if answer is not None:
                # Resume: hand the researcher's answer back so the paused handler
                # continues from where it stopped.
                self._consumed = True
                self.db.set_conversation_status(self.session_id, "running")
                return answer
            if _has_outstanding_clarification(self.db, self.session_id):
                # A question is already posted and unanswered (a redundant resume):
                # stay suspended without re-posting, so the user sees it only once.
                self.db.set_conversation_status(self.session_id, "awaiting_input")
                raise SuspendRun(reason="input")
        # First question of the run, or a fresh round after consuming the last
        # answer: post it, mark the run as waiting, notify, and suspend.
        self.db.add_assistant_message(
            self.session_id, message, kind="clarification", state="CLARIFY"
        )
        self.db.set_conversation_status(self.session_id, "awaiting_input")
        self._notify(self.session_id, "input", message)
        raise SuspendRun(reason="input")


def post_plan_for_approval(
    db: RunnerDB, session_id: str, plan: dict | None, notifier=default_notifier
) -> None:
    """Post the plan for the user's decision, mark the run waiting, and release.

    Idempotent per run: if the plan was already posted (a redundant resume) we
    don't post it again, so the user sees one approval request.
    """
    if db.last_question_id(session_id, kinds=("approval_request",)) is not None:
        db.set_conversation_status(session_id, "awaiting_approval")
        return
    db.add_assistant_message(
        session_id, json.dumps(_plan_summary(plan)), kind="approval_request", state="PLAN"
    )
    db.set_conversation_status(session_id, "awaiting_approval")
    notifier(session_id, "approval", "Your plan is ready to review and approve.")


def consume_approval(db: RunnerDB, session_id: str) -> str | None:
    """The user's plan decision ('approve'/'reject') if made, else None.

    None means the gate hasn't been answered yet — no plan posted, or one posted
    and still awaiting the response.
    """
    decision = _fresh_reply(
        db, session_id, question_kind="approval_request", reply_kind="approval_response"
    )
    return decision.strip().lower() if decision else None


def _plan_summary(plan: dict | None) -> dict:
    """Trim an ExecutionPlan artifact to the fields worth showing for approval."""
    if not plan:
        return {"note": "No execution plan was produced."}
    return {
        "goal_id": plan.get("goal_id"),
        "selected_method": plan.get("selected_method"),
        "cost": plan.get("cost"),
        "compute_resources": plan.get("compute_resources"),
        "risk_assessment": plan.get("risk_assessment"),
        "acceptance_metrics": plan.get("acceptance_metrics"),
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
        # run.suspended: keep the awaiting_input / awaiting_approval status the ask
        # bridge just set — the run is paused, not running.
