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
  approve/reject decision (and any Slurm overrides the user edited) on resume.
* :class:`PgEventSink` — a duck-typed event bus: the orchestrator calls
  ``publish(Event, priority)``; we append to ``run_events`` (tailed by the SSE
  endpoint) and mirror state/status onto the conversation row for the UI.

Pairing answers with questions
------------------------------
Several gates post an assistant *question* and read back a later user reply:
CLARIFY, the heavy-calculation confirmation, the accept-or-rerun validation
gate, the "what should change?" after a rejection, and plan approval. Each posts
its **own message kind**, so a gate recognises its question by identity.

They used to share ``clarification`` and be told apart by position — whichever
question was most recent was assumed to be the asker's own. That held only while
every question was answered before the run moved on. Once one could be abandoned
(re-running from the validation gate instead of answering it), the next gate read
that stale question as its own, suspended without posting anything, and claimed
the next thing the researcher typed as its answer.

Position is still checked *in addition*: a reply counts as "for this gate" only
when the gate's own question is also the most recent question of any kind, so an
answer that arrives after the pipeline has moved on is not applied late. See
:func:`_fresh_reply`.
"""
import json
import os
from functools import lru_cache
from pathlib import Path

from runner.db import RunnerDB
from runner.notifications import default_notifier
from runner.suspend import SuspendRun

# One kind per gate that can ask the researcher something. Mirrored by the
# ``ASK_*`` constants in the state machine (which name the asker), by
# ``api.conversations.QUESTION_KINDS`` (which retires them on a re-run), and by
# the app, which keys its per-gate buttons off them. Adding a gate means adding
# its kind here, in the messages_kind_check migration, and nowhere else.
KIND_CLARIFY = "clarification"
KIND_HEAVY_CONFIRM = "heavy_confirm"
KIND_VALIDATION_GATE = "validation_gate"
KIND_REVISION_REQUEST = "revision_request"
KIND_APPROVAL_REQUEST = "approval_request"

# Every kind that represents an outstanding question to the researcher.
_QUESTION_KINDS = (
    KIND_CLARIFY, KIND_HEAVY_CONFIRM, KIND_VALIDATION_GATE,
    KIND_REVISION_REQUEST, KIND_APPROVAL_REQUEST,
)

# The pipeline stage each question belongs to, for the transcript's state column.
_ASK_STATE = {
    KIND_CLARIFY: "CLARIFY",
    KIND_HEAVY_CONFIRM: "EXECUTE",
    KIND_VALIDATION_GATE: "VALIDATE",
}


# messages.state value marking a user reply a gate has already acted on. A
# consumed reply must never be re-applied when the same gate is reached again
# (e.g. a re-run rewinding to BUILD): the gate asks afresh instead.
_CONSUMED = "consumed"


def _fresh_reply_row(db: RunnerDB, session_id: str, question_kind: str, reply_kind):
    """The user's pending reply row for ``question_kind``, or None.

    Returns the reply only when (a) the latest question of *any* kind is a
    ``question_kind`` question, (b) a user reply exists after it, and (c) that
    reply has not already been consumed by an earlier drive of the run. (a) is
    what stops a stale CLARIFY answer being read as a heavy-calc or approval
    answer, and vice-versa.
    """
    latest_any = db.last_question_id(session_id, kinds=_QUESTION_KINDS)
    if latest_any is None:
        return None
    mine = db.last_question_id(session_id, kinds=(question_kind,))
    if mine is None or mine != latest_any:
        return None  # the current outstanding question belongs to a different gate
    for reply in db.user_replies_after(session_id, mine, kind=reply_kind):
        if reply.get("state") != _CONSUMED:
            return reply
    return None


def _fresh_reply(db: RunnerDB, session_id: str, question_kind: str, reply_kind):
    """Content of the user's pending reply for ``question_kind``, or None."""
    row = _fresh_reply_row(db, session_id, question_kind, reply_kind)
    return row["content"] if row else None


def _has_outstanding_question(db: RunnerDB, session_id: str, kind: str) -> bool:
    """True when ``kind``'s own question is the latest one and still unanswered.

    Keyed on ``kind`` so one gate's abandoned question cannot convince another
    that it has already asked.
    """
    latest_any = db.last_question_id(session_id, kinds=_QUESTION_KINDS)
    mine = db.last_question_id(session_id, kinds=(kind,))
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

    def __call__(self, message: str, purpose: str = KIND_CLARIFY) -> str:
        """Return a waiting answer to ``purpose``'s question, or post + suspend.

        ``purpose`` is the asking gate's message kind (the state machine's
        ``ASK_*`` constants), so this only ever consumes a reply to its own
        question and only stays silent for its own outstanding one.
        """
        kind = purpose if purpose in _QUESTION_KINDS else KIND_CLARIFY
        if not self._consumed:
            reply = _fresh_reply_row(
                self.db, self.session_id, question_kind=kind, reply_kind=None
            )
            if reply is not None:
                # Resume: hand the researcher's answer back so the paused handler
                # continues from where it stopped. Marked consumed so a later
                # re-run through CLARIFY asks afresh instead of replaying it.
                self._consumed = True
                self.db.mark_reply_consumed(reply["id"])
                self.db.set_conversation_status(self.session_id, "running")
                return reply["content"]
            if _has_outstanding_question(self.db, self.session_id, kind):
                # A question is already posted and unanswered (a redundant resume):
                # stay suspended without re-posting, so the user sees it only once.
                self.db.set_conversation_status(self.session_id, "awaiting_input")
                raise SuspendRun(reason="input")
        # First question of the run, or a fresh round after consuming the last
        # answer: post it, mark the run as waiting, notify, and suspend.
        self.db.add_assistant_message(
            self.session_id, message, kind=kind,
            state=_ASK_STATE.get(kind, "CLARIFY"),
        )
        self.db.set_conversation_status(self.session_id, "awaiting_input")
        self._notify(self.session_id, "input", message)
        raise SuspendRun(reason="input")


REJECT_FEEDBACK_PROMPT = (
    "You rejected the plan. What should change? Describe what you'd like done "
    "differently — a different method or tool, other settings, or a different "
    "property — and I'll revise the plan and post a new one for your approval. "
    "If you'd rather stop this run entirely, use the Terminate button."
)


def post_reject_feedback_question(
    db: RunnerDB, session_id: str, notifier=default_notifier
) -> None:
    """Ask what should change after a rejection, mark the run waiting, release.

    A rejection no longer ends the run: the gate asks for revision feedback and
    suspends. The reply is consumed by :func:`consume_reject_feedback` on the
    resume, folded into the run, and a fresh plan is posted for approval.
    """
    db.add_assistant_message(
        session_id, REJECT_FEEDBACK_PROMPT, kind=KIND_REVISION_REQUEST, state="BUILD"
    )
    db.set_conversation_status(session_id, "awaiting_input")
    notifier(session_id, "input", REJECT_FEEDBACK_PROMPT)


def consume_reject_feedback(db: RunnerDB, session_id: str) -> str | None:
    """The user's pending plan-revision feedback (one-shot), or None.

    Matched on the gate's own question kind, so no other gate's answer -- or
    abandoned question -- can be mistaken for revision feedback. Like an approval
    decision, the reply is consumed so a later visit asks afresh instead of
    replaying it.
    """
    reply = _fresh_reply_row(
        db, session_id, question_kind=KIND_REVISION_REQUEST, reply_kind=None
    )
    if reply is None:
        return None
    db.mark_reply_consumed(reply["id"])
    return reply["content"]


def awaiting_reject_feedback(db: RunnerDB, session_id: str) -> bool:
    """True while the gate's "what should change?" question is unanswered."""
    return _has_outstanding_question(db, session_id, KIND_REVISION_REQUEST)


def post_plan_for_approval(
    db: RunnerDB, session_id: str, plan: dict | None, notifier=default_notifier,
    *, compute_target: str | None = None, slurm_cluster: str | None = None,
) -> None:
    """Post the plan for the user's decision, mark the run waiting, and release.

    ``compute_target`` / ``slurm_cluster`` enrich the approval card so the UI can
    show where the run will execute (and, for Slurm, offer editable resources).
    Idempotent per *round*: while the latest posted plan is still awaiting its
    decision (a redundant resume), we don't post it again, so the user sees one
    approval request. But when the previous round was already decided and
    consumed — a re-run rewound to BUILD, or a fresh plan after a rejection —
    a NEW approval request is posted so the user gets a fresh card instead of
    the run silently replaying their old decision.
    """
    pending = db.last_question_id(session_id, kinds=("approval_request",))
    if pending is not None and not db.user_replies_after(
        session_id, pending, kind="approval_response"
    ):
        db.set_conversation_status(session_id, "awaiting_approval")
        return
    db.add_assistant_message(
        session_id,
        json.dumps(_plan_summary(plan, compute_target=compute_target,
                                 slurm_cluster=slurm_cluster)),
        kind="approval_request", state="PLAN",
    )
    db.set_conversation_status(session_id, "awaiting_approval")
    notifier(session_id, "approval", "Your plan is ready to review and approve.")


def consume_approval(db: RunnerDB, session_id: str) -> tuple[str | None, dict | None]:
    """The user's plan decision + optional Slurm overrides, or ``(None, None)``.

    ``(None, None)`` means the gate hasn't been answered yet — no plan posted,
    one posted and still awaiting the response, or the only response was already
    consumed by an earlier drive of the run (so a re-run gets a fresh gate
    rather than replaying the old decision). Otherwise the reply is *consumed*
    (marked in the DB, one-shot) and returned as ``(decision, slurm_overrides)``
    where decision is ``'approve'``/``'reject'`` and overrides is the plan-unit
    ``slurm_request`` dict the user edited on the approval card (ram in GB,
    max_time in hours), or None.
    """
    reply = _fresh_reply_row(
        db, session_id, question_kind="approval_request", reply_kind="approval_response"
    )
    if reply is None:
        return None, None
    db.mark_reply_consumed(reply["id"])
    return _parse_approval_reply(reply["content"])


def _parse_approval_reply(raw: str) -> tuple[str, dict | None]:
    """Accept plain ``approve``/``reject`` or a JSON body with optional overrides."""
    text = (raw or "").strip()
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        return text.lower(), None
    if isinstance(body, dict) and "decision" in body:
        decision = str(body.get("decision", "")).strip().lower()
        overrides = body.get("slurm_request")
        if not isinstance(overrides, dict):
            overrides = None
        return decision, overrides
    return text.lower(), None


def _plan_summary(
    plan: dict | None,
    *,
    compute_target: str | None = None,
    slurm_cluster: str | None = None,
) -> dict:
    """Trim an ExecutionPlan artifact to the fields worth showing for approval.

    Reads the fields where they actually live (goal_id under ``metadata``,
    ``cost_estimate`` / ``compute_estimate`` / ``safety_notes``), and leads with
    the plan's plain-language ``summary`` of what the run will do. When the run is
    Slurm-routed it also carries the cluster + units so the UI can offer editable
    resources on the approval card.
    """
    if not plan:
        return {
            "note": "No execution plan was produced.",
            "compute_target": compute_target or "local",
        }
    target = compute_target or "local"
    metadata = plan.get("metadata") or {}
    summary = {
        "compute_target": target,
        "summary": plan.get("summary"),
        "goal_id": metadata.get("goal_id"),
        "target_system": plan.get("target_system"),
        "requested_property": plan.get("requested_property"),
        "selected_method": plan.get("selected_method"),
        "cost_estimate": plan.get("cost_estimate"),
        "compute_estimate": plan.get("compute_estimate"),
        "slurm_request": plan.get("slurm_request"),
        "slurm_rationale": plan.get("slurm_rationale"),
        "acceptance_metrics": plan.get("acceptance_metrics"),
        # The researcher approves the *toolset*, so every substitution planning had
        # to make has to be legible at the gate — which engine it fell back to, and
        # which libraries it wanted but isn't allowed to use because they aren't
        # installed. safety_notes already carries the prose for both.
        "safety_notes": plan.get("safety_notes"),
        # The install requests behind those notes, trimmed to what the researcher
        # can act on (the ledger keeps the rest). See
        # method_discovery.library_requests.
        "library_requests": [
            {k: req.get(k) for k in ("library", "source", "status", "issue_url")}
            for req in (plan.get("library_requests") or [])
        ] or None,
    }
    # The card offers editable resource fields whichever way the run is routed,
    # so it needs the ceilings either way -- and they must be the ceilings of the
    # machine that will actually run it. Sending the cluster's node limits for a
    # run executing locally would tell the researcher they can ask for 900 GB on
    # a laptop.
    summary["slurm_units"] = {
        "ram": "GB",
        "max_time": "hours",
        "cpu_count": "cores",
        "gpu_count": "GPUs",
    }
    if target == "slurm":
        summary["slurm_cluster"] = slurm_cluster or "compute2"
        limits = _cluster_limits(summary["slurm_cluster"])
    else:
        limits = _local_limits()
    if limits:
        summary["slurm_limits"] = limits
        summary["limits_source"] = (
            f"{summary['slurm_cluster']} node" if target == "slurm" else "this machine"
        )
    return summary


@lru_cache(maxsize=1)
def _local_limits() -> dict | None:
    """Resource ceilings of the box the runner is on, or None.

    A locally-routed run is bounded by this machine, not by the cluster profile.
    No wall-clock ceiling is reported because a local run has none, and GPUs are
    omitted rather than guessed at.
    """
    limits: dict = {}
    cores = os.cpu_count()
    if cores:
        limits["cpu_count"] = cores
    try:
        import psutil
        limits["ram"] = int(psutil.virtual_memory().total / (1024 ** 3))
    except Exception as exc:  # noqa: BLE001 -- psutil absent or unreadable
        # Omit RAM rather than guess: the card shows no ceiling for a field it
        # cannot bound, which is honest, so this is not worth failing over.
        print(f"[runner] local RAM ceiling unavailable ({exc}); omitting it")
    return limits or None


@lru_cache(maxsize=4)
def _cluster_limits(cluster: str) -> dict | None:
    """Per-node resource ceilings for the editable Slurm fields, or None.

    Read straight from the cluster profile JSON (configs/clusters/<name>.json,
    same units as the plan's slurm_request: cores / GPUs / GB / hours) so the
    approval card can label each field with how far it can go. ``max_time`` is
    the longest partition wall limit. Missing profile or fields -> None/absent;
    the card simply shows no maximum then.
    """
    path = (Path(__file__).resolve().parents[1]
            / "configs" / "clusters" / f"{cluster}.json")
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    minutes = [p.get("max_minutes") for p in profile.get("partitions") or []
               if isinstance(p, dict) and isinstance(p.get("max_minutes"), (int, float))]
    limits = {
        "cpu_count": profile.get("max_cpus_per_node"),
        "gpu_count": profile.get("max_gpus_per_node"),
        "ram": profile.get("max_ram_gb"),
        "max_time": round(max(minutes) / 60.0, 2) if minutes else None,
    }
    limits = {k: v for k, v in limits.items() if v is not None}
    return limits or None


# Events that tell every client the run is over: the SSE stream refetches on them
# and each one flips sessions.status to a terminal value.
_TERMINAL_EVENTS = ("run.completed", "run.error")


class PgEventSink:
    """Duck-typed event bus: persist events and mirror UI-facing state.

    ``flush_artifacts`` is set by the runner once the orchestrator exists (the
    sink is built first and handed *into* it). See :meth:`publish` for why the
    sink, rather than the runner's finally block, has to be the one to call it.
    """

    def __init__(self, db: RunnerDB, session_id: str):
        self.db = db
        self.session_id = session_id
        self._seq = 0
        self.flush_artifacts = None

    def publish(self, event, priority=None) -> None:  # noqa: ARG002 (bus signature)
        try:
            payload = json.loads(event.payload) if isinstance(event.payload, str) else event.payload
        except (ValueError, TypeError):
            payload = {"raw": str(getattr(event, "payload", ""))}
        event_type = getattr(event, "event_type", "unknown")
        # Persist the run's outputs BEFORE anything announces that it finished.
        # Artifact capture used to happen only in the runner's finally block, i.e.
        # after this event and after the status flip, so for 3-8 seconds a run
        # advertised itself as done while its results did not exist yet (measured
        # on e496cf22: status 'completed' at 05:22:40.19, last artifact at
        # 05:22:47.86). Every consumer saw the gap -- a report opened in that
        # window showed no result and never re-fetched, and the completion email
        # went out inside it too. The runner still captures in its finally as a
        # backstop for suspends and hard failures; upsert_artifact is idempotent.
        if event_type in _TERMINAL_EVENTS:
            self._flush()
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

    def _flush(self) -> None:
        """Commit artifacts, never failing the run over it (as the finally does)."""
        if self.flush_artifacts is None:
            return
        try:
            self.flush_artifacts()
        except Exception as exc:  # noqa: BLE001 - a run must not die over capture
            print(f"[runner] pre-terminal artifact capture failed for "
                  f"{self.session_id}: {exc}")
