"""Conversation / chat data access for the TWAIN API.

A *conversation* wraps one engine run: its ``id`` is used verbatim as the
engine's ``session_id``. The API never drives the pipeline itself — it records
the user's turns, enqueues a job in the ``jobs`` table, and reads back the
messages, state, and ``run_events`` that the **runner** service produces. See
`docs/architecture/web_ui_plan.md` §4 for the split.
"""
import json

from psycopg2.extras import RealDictCursor

import dispatch
from database import get_connection

# Conversation lifecycle statuses the UI understands.
TERMINAL_STATUSES = ("completed", "error", "rejected", "cancelled")
# Suspended: checkpointed, process released, waiting on the researcher. No
# runner is driving these and no job is in flight, so they can be redirected.
SUSPENDED_STATUSES = ("awaiting_input", "awaiting_approval")

# Assistant message kinds that are a question to the researcher, one per gate.
# Mirrors runner.bridges (api and runner are separate deployables, so the list is
# duplicated rather than imported) and the messages_kind_check migration.
QUESTION_KINDS = (
    "clarification", "heavy_confirm", "validation_gate",
    "revision_request", "approval_request",
)

# Pipeline stages a finished run can be restarted from, in order (mirrors the
# engine's REWINDABLE_STATES). Kept as plain strings so the light API image needs
# no engine import. A re-run re-does the chosen stage and everything after it.
RERUNNABLE_STATES = (
    "INTAKE", "CLARIFY", "DECOMPOSE", "DISCOVER", "PLAN",
    "BUILD", "EXECUTE", "INTERPRET", "VALIDATE", "ACCEPT",
)

def create_conversation(
    user_id: str,
    request: str,
    title: str | None = None,
    *,
    max_cost: float | None = None,
) -> dict:
    """Create a conversation, store the first user turn, and enqueue a start job.

    All three writes share one transaction so a conversation never exists
    without its opening message and queued job. ``max_cost`` (optional) is the
    per-run LLM cost cap; it rides the job ``params`` to the runner, which passes
    it to the orchestrator (falling back to the deployment default when unset).
    """
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            INSERT INTO conversations (user_id, title, status, current_state)
            VALUES (%s, %s, 'running', 'INTAKE')
            RETURNING id, user_id, title, status, current_state, created_at, updated_at;
            """,
            (user_id, title or _default_title(request)),
        )
        conversation = cursor.fetchone()
        session_id = str(conversation["id"])
        cursor.execute(
            """
            INSERT INTO messages (conversation_id, role, content, kind, state)
            VALUES (%s, 'user', %s, 'chat', 'INTAKE');
            """,
            (session_id, request),
        )
        params = {"request": request, "researcher_id": user_id}
        if max_cost is not None:
            params["max_cost"] = max_cost
        cursor.execute(
            "INSERT INTO jobs (session_id, kind, params, status) VALUES (%s, 'start', %s, %s) "
            "RETURNING id;",
            (session_id, json.dumps(params), dispatch.initial_status()),
        )
        job_id = cursor.fetchone()["id"]
        conn.commit()
        cursor.close()
        # After the commit: a worker must never receive a job it can't see yet.
        dispatch.publish(conn, [(job_id, session_id, "start")])
        return conversation
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to create conversation: {e}") from e
    finally:
        conn.close()


def _default_title(request: str) -> str:
    request = request.strip().replace("\n", " ")
    return request[:60] + ("…" if len(request) > 60 else "")


def _enqueue_resume(cursor, session_id: str) -> list:
    """Queue a ``resume`` job so a runner continues the paused run.

    This is how a user reply / approval *wakes* a run in the async model: the run
    is checkpointed and idle until a job re-drives it. Guarded so a rapid double
    reply doesn't pile up duplicate work — at most one queued resume per session
    (the runner also serializes per session, and a duplicate resume is a safe
    no-op). Runs in the caller's transaction; the jobs-insert trigger NOTIFYs the
    runner (migration 003). Returns ``[(job_id, session_id, "resume")]`` for the
    caller to hand to :func:`dispatch.publish` after it commits (``[]`` if a
    resume was already waiting).
    """
    cursor.execute(
        """
        INSERT INTO jobs (session_id, kind, params, status)
        SELECT %s, 'resume', '{}'::jsonb, %s
        WHERE NOT EXISTS (
            SELECT 1 FROM jobs
            WHERE session_id = %s AND kind = 'resume' AND status = ANY(%s)
        )
        RETURNING id;
        """,
        (session_id, dispatch.initial_status(), session_id, list(dispatch.pending_statuses())),
    )
    row = cursor.fetchone()
    return [(_row_id(row), session_id, "resume")] if row else []


def _row_id(row):
    """A RETURNING id from either a dict cursor or a tuple cursor."""
    return row["id"] if isinstance(row, dict) else row[0]


def get_conversation(conversation_id: str, user_id: str) -> dict | None:
    """Fetch a conversation scoped to its owner; None if missing or not theirs."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT c.id, c.user_id, c.title, c.status, c.current_state,
                   c.created_at, c.updated_at,
                   (SELECT max(e.created_at) FROM run_events e
                     WHERE e.session_id = c.id::text
                       AND e.event_type = 'run.started') AS started_at
            FROM conversations c WHERE c.id = %s AND c.user_id = %s;
            """,
            (conversation_id, user_id),
        )
        row = cursor.fetchone()
        cursor.close()
        return row
    except Exception as e:
        raise Exception(f"Failed to fetch conversation: {e}") from e
    finally:
        conn.close()


def list_library_availability() -> list:
    """The runner's capability snapshot: what TWAIN knows, and what is installed.

    Installed first, then by kind and name, so the app renders "what you can run"
    without sorting client-side. An empty list means the runner has not published
    yet (fresh database, or a runner that has not restarted since the migration).
    """
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT kind, name, import_name, version, description,
                   installed, env, detail, homepage, checked_at
            FROM library_availability
            ORDER BY installed DESC, kind, name;
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Exception as e:
        raise Exception(f"Failed to list library availability: {e}") from e
    finally:
        conn.close()


def get_conversation_status(conversation_id: str) -> str | None:
    """Return just a conversation's status (used by the SSE loop), or None."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT status FROM conversations WHERE id = %s;", (conversation_id,)
        )
        row = cursor.fetchone()
        cursor.close()
        return row["status"] if row else None
    except Exception as e:
        raise Exception(f"Failed to fetch status: {e}") from e
    finally:
        conn.close()


def list_conversations(user_id: str) -> list:
    """List a user's conversations, newest activity first.

    ``started_at`` is when the run's CURRENT activity began -- the newest
    ``run.started``, which the orchestrator publishes once per slice (start,
    resume, rerun). Neither existing column can stand in for it: ``updated_at`` is
    rewritten on every status change, and ``created_at`` is when the conversation
    was opened, which is wrong for anything resumed or rerun. It drives the live
    elapsed display, so it must mean "running for this long", not "exists since".

    The correlated subquery costs one indexed seek per row (idx_run_events_session
    is on session_id) over a per-user list, which is tens of rows.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT c.id, c.title, c.status, c.current_state,
                   c.created_at, c.updated_at,
                   (SELECT max(e.created_at) FROM run_events e
                     WHERE e.session_id = c.id::text
                       AND e.event_type = 'run.started') AS started_at
            FROM conversations c WHERE c.user_id = %s ORDER BY c.updated_at DESC;
            """,
            (user_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Exception as e:
        raise Exception(f"Failed to list conversations: {e}") from e
    finally:
        conn.close()


def list_messages(conversation_id: str) -> list:
    """Return the conversation transcript in order."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, role, content, kind, state, created_at
            FROM messages WHERE conversation_id = %s ORDER BY id;
            """,
            (conversation_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Exception as e:
        raise Exception(f"Failed to list messages: {e}") from e
    finally:
        conn.close()


def add_message(conversation_id: str, content: str, *, kind: str = "chat") -> dict:
    """Append a user turn (a chat reply or a clarification answer)."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            INSERT INTO messages (conversation_id, role, content, kind)
            VALUES (%s, 'user', %s, %s)
            RETURNING id, role, content, kind, state, created_at;
            """,
            (conversation_id, content, kind),
        )
        row = cursor.fetchone()
        cursor.execute(
            "UPDATE conversations SET status = 'running', updated_at = now() WHERE id = %s;",
            (conversation_id,),
        )
        # Wake the paused run so it consumes this reply (e.g. a clarification answer).
        woken = _enqueue_resume(cursor, conversation_id)
        conn.commit()
        cursor.close()
        dispatch.publish(conn, woken)
        return row
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to add message: {e}") from e
    finally:
        conn.close()


def add_approval_response(
    conversation_id: str,
    decision: str,
    *,
    slurm_request: dict | None = None,
    acceptance_metrics: list | None = None,
) -> dict:
    """Record the user's plan-approval decision ('approve' | 'reject').

    Edits made on the approval card are embedded in the message content so the
    runner can patch the execution plan before BUILD/EXECUTE: ``slurm_request``
    (plan units: ram GB, max_time hours) and ``acceptance_metrics`` (the bar the
    result is judged against). Either may be absent; a decision with neither is
    stored as the bare word, which is what the runner's parser expects.
    """
    edits = {}
    if slurm_request is not None:
        edits["slurm_request"] = slurm_request
    if acceptance_metrics is not None:
        edits["acceptance_metrics"] = acceptance_metrics
    content = json.dumps({"decision": decision, **edits}) if edits else decision
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            INSERT INTO messages (conversation_id, role, content, kind)
            VALUES (%s, 'user', %s, 'approval_response')
            RETURNING id, role, content, kind, state, created_at;
            """,
            (conversation_id, content),
        )
        row = cursor.fetchone()
        # Clear the approval card immediately: without this, a reject leaves the
        # status on 'awaiting_approval' until the runner picks the job up, which
        # looks like the button did nothing. The runner settles the next status
        # (onward past the gate for an approve; 'awaiting_input' for a reject,
        # where the gate asks what should change and revises the plan).
        cursor.execute(
            "UPDATE conversations SET status = 'running', updated_at = now() WHERE id = %s;",
            (conversation_id,),
        )
        # Wake the run parked at the approval gate to act on the decision.
        woken = _enqueue_resume(cursor, conversation_id)
        conn.commit()
        cursor.close()
        dispatch.publish(conn, woken)
        return row
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to record approval: {e}") from e
    finally:
        conn.close()


def request_termination(conversation_id: str) -> dict:
    """Record the user's request to stop the run (Terminate button).

    Inserts a 'terminate' control message (the runner polls for it between
    stages and blocking waits) and flips the status to 'cancelling' so the UI
    shows immediate feedback. The runner settles the final 'cancelled' status.

    Also queues a ``resume``: a run paused on a Slurm job (P2) or on the
    researcher has no process to notice the request, so a worker must wake to
    cancel the cluster job and settle the run. A run that is mid-slice finishes
    its slice first (jobs are serialized per run) and the resume is a no-op.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            INSERT INTO messages (conversation_id, role, content, kind)
            VALUES (%s, 'user', 'terminate', 'terminate')
            RETURNING id, role, content, kind, state, created_at;
            """,
            (conversation_id,),
        )
        row = cursor.fetchone()
        cursor.execute(
            "UPDATE conversations SET status = 'cancelling', updated_at = now() WHERE id = %s;",
            (conversation_id,),
        )
        woken = _enqueue_resume(cursor, conversation_id)
        conn.commit()
        cursor.close()
        dispatch.publish(conn, woken)
        return row
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to request termination: {e}") from e
    finally:
        conn.close()


def rerun_conversation(
    conversation_id: str, user_id: str, target_state: str,
    feedback: str | None = None, request: str | None = None,
    slurm_request: dict | None = None,
    acceptance_metrics: list | None = None,
) -> dict | None:
    """Re-run a conversation from an earlier pipeline stage.

    Allowed when the run is finished, or when it is *suspended* at a gate
    (``awaiting_input`` / ``awaiting_approval``). A suspended run has been
    checkpointed and its process released, so no runner is driving it and no job
    is in flight for the session -- which is what the restriction here was
    protecting against. Letting a suspended run be redirected is what lets the
    researcher answer the accept-or-rerun question with "re-run from this stage"
    instead of only the automatic correction loop. A ``running`` run is still
    refused: that one really is being driven.

    Reuses the original request and per-run budget, flips the conversation back to
    ``running`` at ``target_state`` for immediate UI feedback, records a marker
    message, and enqueues a ``rerun`` job the runner claims to rewind + re-drive
    the run. All writes share one transaction. Returns the refreshed conversation;
    None when it doesn't exist or isn't the caller's; raises ValueError when the
    run is still active.

    ``request`` replaces the opening prompt (re-running from INTAKE with an edit).
    It is recorded on the transcript as the researcher's new request, so what the
    re-run actually read is visible rather than implied.

    ``feedback`` is the mid-session revision path: the researcher's "here's what
    to change" message (typed into the chat of a finished run) is recorded on
    the transcript and carried on the job, where the runner folds it into the
    run's intent before re-planning -- so the revised plan reflects it and
    comes back for a fresh approval.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT status FROM conversations WHERE id = %s AND user_id = %s;",
            (conversation_id, user_id),
        )
        row = cursor.fetchone()
        if row is None:
            cursor.close()
            return None
        if row["status"] not in TERMINAL_STATUSES + SUSPENDED_STATUSES:
            cursor.close()
            raise ValueError(
                "This run is still active — wait for it to finish, or for it to "
                "ask you something, before re-running it from an earlier step."
            )

        # Reuse the opening request + any per-run budget from the original start job.
        cursor.execute(
            """
            SELECT content FROM messages
            WHERE conversation_id = %s AND role = 'user' AND kind = 'chat'
            ORDER BY id LIMIT 1;
            """,
            (conversation_id,),
        )
        first = cursor.fetchone()
        original_request = first["content"] if first else None
        # An edited prompt replaces the original for this re-run. The first
        # message stays as the historical record; the edit is appended below so
        # the transcript shows what intake actually read.
        job_request = request or original_request
        cursor.execute(
            "SELECT params FROM jobs WHERE session_id = %s AND kind = 'start' ORDER BY id LIMIT 1;",
            (conversation_id,),
        )
        start_job = cursor.fetchone()
        max_cost = (start_job["params"] or {}).get("max_cost") if start_job else None

        params = {"researcher_id": user_id, "request": job_request,
                  "target_state": target_state}
        if max_cost is not None:
            params["max_cost"] = max_cost
        if feedback:
            params["feedback"] = feedback
        # Re-run the SAME plan with different resources. The runner patches the
        # surviving plan after the rewind; the API has already refused this for
        # targets at or before PLAN, where a fresh plan would discard it.
        if slurm_request:
            params["slurm_request"] = slurm_request
        # Same reasoning, same restriction: the bar the result is judged against
        # lives on the plan, so it only survives a rewind to a post-PLAN stage.
        if acceptance_metrics:
            params["acceptance_metrics"] = acceptance_metrics

        # Retire the questions of the pass being rewound past. Choosing to re-run
        # IS the answer to whatever was outstanding, and a question left looking
        # unanswered makes the next gate think it has already asked: it suspends
        # without posting anything and the researcher waits on a question that
        # never comes. The runner skips retired questions (db.last_question_id).
        cursor.execute(
            """
            UPDATE messages SET state = 'consumed'
            WHERE conversation_id = %s AND role = 'assistant'
              AND kind = ANY(%s)
              AND (state IS NULL OR state <> 'consumed');
            """,
            (conversation_id, list(QUESTION_KINDS)),
        )
        cursor.execute(
            """
            UPDATE conversations SET status = 'running', current_state = %s, updated_at = now()
            WHERE id = %s
            RETURNING id, user_id, title, status, current_state, created_at, updated_at;
            """,
            (target_state, conversation_id),
        )
        conversation = cursor.fetchone()
        if feedback:
            # Show the researcher's revision request on the transcript, then the
            # marker; the runner folds the feedback into the intent (marked
            # consumed there so it can't be mistaken for a gate answer later).
            cursor.execute(
                """
                INSERT INTO messages (conversation_id, role, content, kind, state)
                VALUES (%s, 'user', %s, 'chat', 'consumed');
                """,
                (conversation_id, feedback),
            )
        if request:
            cursor.execute(
                """
                INSERT INTO messages (conversation_id, role, content, kind, state)
                VALUES (%s, 'user', %s, 'chat', 'consumed');
                """,
                (conversation_id, request),
            )
        if feedback:
            marker = (f"↩︎ Revising the run with your feedback "
                      f"(re-planning from {target_state}).")
        elif request:
            marker = f"↩︎ Re-running from {target_state} with your edited request."
        elif slurm_request or acceptance_metrics:
            edited = " and ".join(filter(None, [
                "resource request" if slurm_request else None,
                "acceptance criteria" if acceptance_metrics else None,
            ]))
            marker = f"↩︎ Re-running from {target_state} with your edited {edited}."
        else:
            marker = f"↩︎ Re-running from {target_state}."
        cursor.execute(
            """
            INSERT INTO messages (conversation_id, role, content, kind, state)
            VALUES (%s, 'assistant', %s, 'chat', %s);
            """,
            (conversation_id, marker, target_state),
        )
        cursor.execute(
            "INSERT INTO jobs (session_id, kind, params, status) VALUES (%s, 'rerun', %s, %s) "
            "RETURNING id;",
            (conversation_id, json.dumps(params), dispatch.initial_status()),
        )
        job_id = _row_id(cursor.fetchone())
        conn.commit()
        cursor.close()
        dispatch.publish(conn, [(job_id, conversation_id, "rerun")])
        return conversation
    except ValueError:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to re-run conversation: {e}") from e
    finally:
        conn.close()


def list_artifacts(session_id: str) -> list:
    """List available artifacts (name/kind/size) for the expandable file menu."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT name, kind, length(content) AS size
            FROM artifacts WHERE session_id = %s ORDER BY name;
            """,
            (session_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Exception as e:
        raise Exception(f"Failed to list artifacts: {e}") from e
    finally:
        conn.close()


def get_artifact(session_id: str, name: str) -> dict | None:
    """Fetch one artifact's full content by name (may contain a '/')."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT name, kind, content FROM artifacts WHERE session_id = %s AND name = %s;",
            (session_id, name),
        )
        row = cursor.fetchone()
        cursor.close()
        return row
    except Exception as e:
        raise Exception(f"Failed to fetch artifact: {e}") from e
    finally:
        conn.close()


def all_artifacts(session_id: str) -> list:
    """Every artifact's name, kind and content, for the report's zip download."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT name, kind, content FROM artifacts WHERE session_id = %s ORDER BY name;",
            (session_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        conn.close()


def cluster_attempt(session_id: str, attempt: int | None = None) -> dict | None:
    """The run's Slurm attempt (its newest when ``attempt`` is None), or None."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT ris_job_id, attempt, s3_prefix, status FROM cluster_jobs "
            "WHERE session_id = %s AND (%s::int IS NULL OR attempt = %s::int) "
            "ORDER BY attempt DESC LIMIT 1;",
            (session_id, attempt, attempt),
        )
        row = cursor.fetchone()
        cursor.close()
    finally:
        conn.close()
    if row is None:
        return None
    return {"job_id": row[0], "attempt": row[1], "s3_prefix": row[2], "status": row[3]}


def owns_conversation(conversation_id: str, user_id: str) -> bool:
    """Cheap ownership check for endpoints polled every few seconds.

    ``get_conversation`` loads the whole transcript; the activity feed only
    needs to know the caller may see this run.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM conversations WHERE id = %s AND user_id = %s;",
            (conversation_id, user_id),
        )
        found = cursor.fetchone() is not None
        cursor.close()
        return found
    except Exception as e:
        raise Exception(f"Failed to check conversation ownership: {e}") from e
    finally:
        conn.close()


def get_activity(session_id: str, after_id: int, types: tuple, limit: int) -> list:
    """run_events of ``types`` with id > ``after_id``, oldest first, at most ``limit``."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, event_type, payload, created_at
            FROM run_events
            WHERE session_id = %s AND id > %s AND event_type = ANY(%s)
            ORDER BY id LIMIT %s;
            """,
            (session_id, after_id, list(types), limit),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Exception as e:
        raise Exception(f"Failed to read activity: {e}") from e
    finally:
        conn.close()


def get_events(session_id: str, after_id: int = 0) -> list:
    """Return run_events for a session with id greater than ``after_id``."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, seq, event_type, payload, created_at
            FROM run_events WHERE session_id = %s AND id > %s ORDER BY id;
            """,
            (session_id, after_id),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Exception as e:
        raise Exception(f"Failed to read events: {e}") from e
    finally:
        conn.close()


def delete_conversation(conversation_id: str, user_id: str) -> bool:
    """Delete a conversation the caller owns, plus everything keyed to its run.

    ``messages`` cascade via their FK; the engine/runner tables (``run_events``,
    ``artifacts``, ``jobs``, ``sessions``) key off the session id (= the
    conversation id as text) with no FK, so they are removed explicitly in the
    same transaction. Ownership is checked first, so a foreign id deletes nothing.
    Returns False when the conversation doesn't exist or isn't the caller's.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM conversations WHERE id = %s AND user_id = %s;",
            (conversation_id, user_id),
        )
        if cursor.fetchone() is None:
            cursor.close()
            return False
        cursor.execute("DELETE FROM run_events WHERE session_id = %s;", (conversation_id,))
        cursor.execute("DELETE FROM artifacts WHERE session_id = %s;", (conversation_id,))
        cursor.execute("DELETE FROM jobs WHERE session_id = %s;", (conversation_id,))
        cursor.execute("DELETE FROM sessions WHERE session_id = %s;", (conversation_id,))
        cursor.execute("DELETE FROM conversations WHERE id = %s;", (conversation_id,))
        conn.commit()
        cursor.close()
        return True
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to delete conversation: {e}") from e
    finally:
        conn.close()
