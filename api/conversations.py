"""Conversation / chat data access for the TWAIN API.

A *conversation* wraps one engine run: its ``id`` is used verbatim as the
engine's ``session_id``. The API never drives the pipeline itself — it records
the user's turns, enqueues a job in the ``jobs`` table, and reads back the
messages, state, and ``run_events`` that the **runner** service produces. See
`docs/architecture/web_ui_plan.md` §4 for the split.
"""
import json

from psycopg2.extras import RealDictCursor

from database import get_connection

# Conversation lifecycle statuses the UI understands.
TERMINAL_STATUSES = ("completed", "error", "rejected")

# Pipeline stages a finished run can be restarted from, in order (mirrors the
# engine's REWINDABLE_STATES). Kept as plain strings so the light API image needs
# no engine import. A re-run re-does the chosen stage and everything after it.
RERUNNABLE_STATES = (
    "INTAKE", "CLARIFY", "DECOMPOSE", "DISCOVER", "PLAN",
    "BUILD", "EXECUTE", "INTERPRET", "VALIDATE", "ACCEPT",
)


def create_conversation(
    user_id: str, request: str, title: str | None = None, max_cost: float | None = None,
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
            "INSERT INTO jobs (session_id, kind, params) VALUES (%s, 'start', %s);",
            (session_id, json.dumps(params)),
        )
        conn.commit()
        cursor.close()
        return conversation
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to create conversation: {e}") from e
    finally:
        conn.close()


def _default_title(request: str) -> str:
    request = request.strip().replace("\n", " ")
    return request[:60] + ("…" if len(request) > 60 else "")


def _enqueue_resume(cursor, session_id: str) -> None:
    """Queue a ``resume`` job so a runner continues the paused run.

    This is how a user reply / approval *wakes* a run in the async model: the run
    is checkpointed and idle until a job re-drives it. Guarded so a rapid double
    reply doesn't pile up duplicate work — at most one queued resume per session
    (the runner also serializes per session, and a duplicate resume is a safe
    no-op). Runs in the caller's transaction; the jobs-insert trigger NOTIFYs the
    runner (migration 003).
    """
    cursor.execute(
        """
        INSERT INTO jobs (session_id, kind, params)
        SELECT %s, 'resume', '{}'::jsonb
        WHERE NOT EXISTS (
            SELECT 1 FROM jobs
            WHERE session_id = %s AND kind = 'resume' AND status = 'queued'
        );
        """,
        (session_id, session_id),
    )


def get_conversation(conversation_id: str, user_id: str) -> dict | None:
    """Fetch a conversation scoped to its owner; None if missing or not theirs."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, user_id, title, status, current_state, created_at, updated_at
            FROM conversations WHERE id = %s AND user_id = %s;
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
    """List a user's conversations, newest activity first."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, title, status, current_state, created_at, updated_at
            FROM conversations WHERE user_id = %s ORDER BY updated_at DESC;
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
        _enqueue_resume(cursor, conversation_id)
        conn.commit()
        cursor.close()
        return row
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to add message: {e}") from e
    finally:
        conn.close()


def add_approval_response(conversation_id: str, decision: str) -> dict:
    """Record the user's plan-approval decision ('approve' | 'reject')."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            INSERT INTO messages (conversation_id, role, content, kind)
            VALUES (%s, 'user', %s, 'approval_response')
            RETURNING id, role, content, kind, state, created_at;
            """,
            (conversation_id, decision),
        )
        row = cursor.fetchone()
        # Wake the run parked at the approval gate to act on the decision.
        _enqueue_resume(cursor, conversation_id)
        conn.commit()
        cursor.close()
        return row
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to record approval: {e}") from e
    finally:
        conn.close()


def rerun_conversation(conversation_id: str, user_id: str, target_state: str) -> dict | None:
    """Re-run a finished conversation from an earlier pipeline stage.

    Requires the run to be finished: re-running an in-flight run would race the
    runner still driving it (and orphan the job blocked on the user). Reuses the
    original request and per-run budget, flips the conversation back to
    ``running`` at ``target_state`` for immediate UI feedback, records a marker
    message, and enqueues a ``rerun`` job the runner claims to rewind + re-drive
    the run. All writes share one transaction. Returns the refreshed conversation;
    None when it doesn't exist or isn't the caller's; raises ValueError when the
    run is still active.
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
        if row["status"] not in TERMINAL_STATUSES:
            cursor.close()
            raise ValueError(
                "This run is still active — wait for it to finish (or reject the "
                "current plan) before re-running it from an earlier step."
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
        request = first["content"] if first else None
        cursor.execute(
            "SELECT params FROM jobs WHERE session_id = %s AND kind = 'start' ORDER BY id LIMIT 1;",
            (conversation_id,),
        )
        start_job = cursor.fetchone()
        max_cost = (start_job["params"] or {}).get("max_cost") if start_job else None

        params = {"researcher_id": user_id, "request": request, "target_state": target_state}
        if max_cost is not None:
            params["max_cost"] = max_cost

        cursor.execute(
            """
            UPDATE conversations SET status = 'running', current_state = %s, updated_at = now()
            WHERE id = %s
            RETURNING id, user_id, title, status, current_state, created_at, updated_at;
            """,
            (target_state, conversation_id),
        )
        conversation = cursor.fetchone()
        cursor.execute(
            """
            INSERT INTO messages (conversation_id, role, content, kind, state)
            VALUES (%s, 'assistant', %s, 'chat', %s);
            """,
            (conversation_id, f"↩︎ Re-running from {target_state}.", target_state),
        )
        cursor.execute(
            "INSERT INTO jobs (session_id, kind, params) VALUES (%s, 'rerun', %s);",
            (conversation_id, json.dumps(params)),
        )
        conn.commit()
        cursor.close()
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
