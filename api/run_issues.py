"""Run-issue data access: the run snapshot, and the record of what was reported.

Two jobs:

  * :func:`collect_run_context` builds the snapshot that rides along with a
    user-submitted GitHub issue -- everything a maintainer needs to understand a
    run they didn't watch. The same function backs the preview the run window
    shows before submitting, so the user sees exactly what will be attached
    rather than being asked to trust a description of it.
  * :func:`record_issue` / :func:`list_issues` persist submissions, so the run
    window can show what has already been reported and the record survives a
    failed GitHub call.

Attachments are bounded (see the ``MAX_*`` constants): a run can hold a long
transcript and a large generated script, and an issue nobody can read is worse
than a short one plus the run id. ``run_context["truncated"]`` records when
anything was cut.

The API never reaches into the runner's filesystem -- everything here comes from
the tables the runner writes (see ``conversations``).
"""
import json
import logging

from psycopg2.extras import Json, RealDictCursor

from database import get_connection

logger = logging.getLogger(__name__)

# How much of each attachment survives into the issue.
MAX_MESSAGES = 12          # transcript tail, newest turns
MAX_MESSAGE_CHARS = 1200   # per turn
MAX_ERRORS = 5
MAX_SAFETY_NOTES = 20
MAX_ARTIFACTS = 40

# Event types that mean "something went wrong" (see runner/bridges.PgEventSink).
ERROR_EVENT_TYPES = ("run.error", "stage.failed", "run.failed")

# One run can't turn into an unbounded stream of issues.
MAX_ISSUES_PER_RUN = 10


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n… [truncated, {len(text) - limit} more characters]", True


def _json_artifact(cursor, conversation_id: str, name: str):
    """One artifact parsed as JSON, or its raw text, or None if absent."""
    cursor.execute(
        "SELECT content FROM artifacts WHERE session_id = %s AND name = %s;",
        (str(conversation_id), name),
    )
    row = cursor.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["content"])
    except (ValueError, TypeError):
        return row["content"]


def collect_run_context(conversation: dict) -> dict:
    """Assemble the snapshot attached to an issue filed against this run.

    Takes the already-authorized ``conversation`` row (the caller has confirmed
    ownership) so this never widens access to someone else's run.
    """
    conversation_id = str(conversation["id"])
    context: dict = {
        "run_id": conversation_id,
        "title": conversation.get("title"),
        "status": conversation.get("status"),
        "current_state": conversation.get("current_state"),
        "created_at": conversation.get("created_at"),
        "updated_at": conversation.get("updated_at"),
        "selected_method": None,
        "requested_property": None,
        "target_system": None,
        "safety_notes": [],
        "library_requests": [],
        "execution_result": None,
        "errors": [],
        "recent_messages": [],
        "artifacts": [],
        "truncated": False,
    }
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # What planning decided, and what it had to tell the researcher about it.
        plan = _json_artifact(cursor, conversation_id, "execution_plan")
        if isinstance(plan, dict):
            context["selected_method"] = plan.get("selected_method")
            context["requested_property"] = plan.get("requested_property")
            context["target_system"] = plan.get("target_system")
            notes = plan.get("safety_notes") or []
            context["safety_notes"] = notes[:MAX_SAFETY_NOTES]
            context["truncated"] |= len(notes) > MAX_SAFETY_NOTES
            context["library_requests"] = plan.get("library_requests") or []

        execution_result = _json_artifact(cursor, conversation_id, "execution_result")
        if execution_result is not None:
            context["execution_result"] = execution_result

        # Errors first: for a run that broke, this is the whole point of the report.
        cursor.execute(
            """
            SELECT event_type, payload, created_at FROM run_events
            WHERE session_id = %s AND event_type = ANY(%s)
            ORDER BY id DESC LIMIT %s;
            """,
            (conversation_id, list(ERROR_EVENT_TYPES), MAX_ERRORS),
        )
        context["errors"] = list(reversed(cursor.fetchall()))

        # The transcript tail, oldest-first for reading once selected newest-first.
        cursor.execute(
            """
            SELECT role, content, kind, state, created_at FROM messages
            WHERE conversation_id = %s ORDER BY id DESC LIMIT %s;
            """,
            (conversation_id, MAX_MESSAGES),
        )
        messages = []
        for row in reversed(cursor.fetchall()):
            content, cut = _truncate(row["content"], MAX_MESSAGE_CHARS)
            context["truncated"] |= cut
            messages.append({**row, "content": content})
        context["recent_messages"] = messages

        cursor.execute(
            """
            SELECT name, kind, length(content) AS size FROM artifacts
            WHERE session_id = %s ORDER BY name LIMIT %s;
            """,
            (conversation_id, MAX_ARTIFACTS),
        )
        context["artifacts"] = cursor.fetchall()
        cursor.close()
        return context
    except Exception as e:
        raise Exception(f"Failed to collect run context: {e}") from e
    finally:
        conn.close()


def count_issues(conversation_id: str) -> int:
    """How many issues have already been filed against this run."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT count(*) AS n FROM run_issues WHERE conversation_id = %s;",
            (str(conversation_id),),
        )
        row = cursor.fetchone()
        cursor.close()
        return int(row["n"]) if row else 0
    except Exception as e:
        raise Exception(f"Failed to count run issues: {e}") from e
    finally:
        conn.close()


def record_issue(conversation_id: str, user_id: str | None, *, category: str, title: str,
                 description: str, run_context: dict, result: dict) -> dict:
    """Persist one submission and its outcome; returns the stored row.

    Written after the GitHub call either way, so a report is never lost because
    the tracker was unreachable -- ``status`` says which happened.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            INSERT INTO run_issues (conversation_id, user_id, category, title, description,
                                    run_context, status, issue_number, issue_url, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, conversation_id, category, title, description, status,
                      issue_number, issue_url, error, created_at;
            """,
            (
                str(conversation_id), user_id, category, title, description,
                Json(run_context, dumps=lambda v: json.dumps(v, default=str)),
                result.get("status", "queued"), result.get("issue_number"),
                result.get("issue_url"), result.get("error"),
            ),
        )
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        return row
    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to record run issue: {e}") from e
    finally:
        conn.close()


def list_issues(conversation_id: str) -> list:
    """Issues already filed against this run, newest first (no snapshot payload)."""
    conn = get_connection()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            """
            SELECT id, category, title, description, status, issue_number, issue_url,
                   error, created_at
            FROM run_issues WHERE conversation_id = %s ORDER BY id DESC;
            """,
            (str(conversation_id),),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    except Exception as e:
        raise Exception(f"Failed to list run issues: {e}") from e
    finally:
        conn.close()
