"""PostgreSQL access for the runner service.

The runner shares one database with the API (see `docs/architecture/web_ui_plan.md`
§4). It writes assistant messages, run_events, and session snapshots, and reads
back the user's replies. Connection settings mirror `api/database.py`: DB_* env
vars, with the password resolved from AWS Secrets Manager in the cloud and from
``DB_PASSWORD`` locally.
"""
import json
import os
import select

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from psycopg2.extras import Json, RealDictCursor

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "twaindb")
DB_USER = os.getenv("DB_USER", "postgres")

RESUMABLE_STATUSES = ("running", "paused", "error")

# Postgres channel the jobs-insert trigger NOTIFYs (see migration 003). The runner
# LISTENs on it so a newly queued job wakes it immediately instead of on the next
# poll tick — no always-on 1s spin (Phase 2).
JOBS_CHANNEL = "twain_jobs"


def _resolve_db_password() -> str:
    secret_arn = os.getenv("AWS_SECRET_ARN")
    if not secret_arn:
        return os.getenv("DB_PASSWORD", "")
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1"))
    try:
        secret = client.get_secret_value(SecretId=secret_arn).get("SecretString", "")
        try:
            return json.loads(secret).get("password", secret)
        except json.JSONDecodeError:
            return secret
    except ClientError as e:
        raise Exception(f"Failed to retrieve DB secret: {e}") from e


class RunnerDB:
    """Short-lived-connection helpers for everything the runner reads/writes."""

    def _connect(self):
        return psycopg2.connect(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=_resolve_db_password(),
        )

    # ---- jobs -----------------------------------------------------------------
    def claim_job(self) -> dict | None:
        """Atomically claim the oldest queued job (FOR UPDATE SKIP LOCKED).

        Serialized per session: a job is skipped while another job for the *same*
        session is already claimed/running, so at most one runner drives a session
        at a time. That keeps a redundant ``resume`` (e.g. the user replied twice)
        from racing a live run on the same checkpoint. A duplicate that does slip
        through is a safe no-op — DbAsk finds no new answer and re-suspends.

        Claiming stamps ``heartbeat_at`` and bumps ``attempts``; the runner keeps
        the heartbeat fresh while it works so :meth:`reap_stale_jobs` can tell a
        healthy long slice from a crashed one. The returned dict carries the new
        ``attempts`` so the loop can decide retry-vs-dead-letter. The per-session
        skip above is therefore never permanent: a crashed session's stuck job is
        re-queued by the reaper once its lease expires.
        """
        conn = self._connect()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                """
                SELECT id, session_id, kind, params FROM jobs
                WHERE status = 'queued'
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs active
                      WHERE active.session_id = jobs.session_id
                        AND active.status IN ('claimed', 'running')
                  )
                ORDER BY id
                FOR UPDATE SKIP LOCKED LIMIT 1;
                """
            )
            job = cursor.fetchone()
            if job is not None:
                cursor.execute(
                    "UPDATE jobs SET status = 'claimed', claimed_at = now(), "
                    "heartbeat_at = now(), attempts = attempts + 1 "
                    "WHERE id = %s RETURNING attempts;",
                    (job["id"],),
                )
                job["attempts"] = cursor.fetchone()["attempts"]
            conn.commit()
            cursor.close()
            return job
        finally:
            conn.close()

    def mark_job(self, job_id: int, status: str) -> None:
        self._execute("UPDATE jobs SET status = %s WHERE id = %s;", (status, job_id))

    def heartbeat_job(self, job_id: int) -> None:
        """Refresh a claimed job's lease so the reaper doesn't reclaim a healthy,
        long-running slice (e.g. a multi-hour EXECUTE). A no-op once the job leaves
        the in-flight states, so a late beat can't resurrect a finished or
        re-queued job."""
        self._execute(
            "UPDATE jobs SET heartbeat_at = now() "
            "WHERE id = %s AND status IN ('claimed', 'running');",
            (job_id,),
        )

    def reap_stale_jobs(self, lease_seconds: float, max_attempts: int) -> list:
        """Recover jobs orphaned by a dead runner (heartbeat older than the lease).

        A claimed/running job whose heartbeat has gone stale is presumed abandoned
        — the runner crashed, was OOM-killed, or redeployed mid-slice. Re-queue it
        so another runner re-drives it from its checkpoint, unless it has already
        been attempted ``max_attempts`` times, in which case dead-letter it
        (``status='error'``) and return it so the caller can fail the conversation.

        Safe to run from several runners at once: the UPDATEs are atomic and the
        under- vs. at/over-``max_attempts`` sets are disjoint. Returns the
        dead-lettered rows ``[{id, session_id, attempts}]`` (empty when none).
        """
        conn = self._connect()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            # Dead-letter the exhausted ones first, RETURNING them so the caller
            # can post a failure message and mark the conversation errored.
            cursor.execute(
                """
                UPDATE jobs SET status = 'error'
                WHERE status IN ('claimed', 'running')
                  AND COALESCE(heartbeat_at, claimed_at) < now() - make_interval(secs => %s)
                  AND attempts >= %s
                RETURNING id, session_id, attempts;
                """,
                (lease_seconds, max_attempts),
            )
            dead = cursor.fetchall()
            # Re-queue the recoverable ones for another attempt.
            cursor.execute(
                """
                UPDATE jobs SET status = 'queued'
                WHERE status IN ('claimed', 'running')
                  AND COALESCE(heartbeat_at, claimed_at) < now() - make_interval(secs => %s)
                  AND attempts < %s;
                """,
                (lease_seconds, max_attempts),
            )
            conn.commit()
            cursor.close()
            return dead
        finally:
            conn.close()

    # ---- conversations --------------------------------------------------------
    def set_conversation_status(self, session_id: str, status: str) -> None:
        self._execute(
            "UPDATE conversations SET status = %s, updated_at = now() WHERE id = %s;",
            (status, session_id),
        )

    def set_conversation_state(self, session_id: str, state: str) -> None:
        self._execute(
            "UPDATE conversations SET current_state = %s, updated_at = now() WHERE id = %s;",
            (state, session_id),
        )

    def owner_contact(self, session_id: str) -> dict | None:
        """Contact details of the researcher who owns this run, or None.

        Joins the run's conversation to its owning user (``conversations.id`` is the
        session_id; ``conversations.user_id`` → ``users``). Returns
        ``{"email", "name", "phone"}`` so the notifier can reach the *specific*
        researcher who left the session — by email (SES/SendGrid) or SMS (SNS to
        their ``phone``) — instead of one global address/topic. Any field may be
        None (e.g. no phone on file); returns None outright when the session or
        user is unknown, so the caller can fall back to the configured default.
        """
        row = self._query_one(
            "SELECT u.email, u.name, u.phone FROM conversations c "
            "JOIN users u ON u.id = c.user_id "
            "WHERE c.id = %s;",
            (session_id,),
        )
        if not row:
            return None
        return {"email": row.get("email"), "name": row.get("name"), "phone": row.get("phone")}

    # ---- messages -------------------------------------------------------------
    def add_assistant_message(
        self, session_id: str, content: str, *, kind: str = "chat", state: str | None = None
    ) -> int:
        conn = self._connect()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                """
                INSERT INTO messages (conversation_id, role, content, kind, state)
                VALUES (%s, 'assistant', %s, %s, %s) RETURNING id;
                """,
                (session_id, content, kind, state),
            )
            message_id = cursor.fetchone()["id"]
            conn.commit()
            cursor.close()
            return message_id
        finally:
            conn.close()

    def max_message_id(self, session_id: str) -> int:
        row = self._query_one(
            "SELECT COALESCE(MAX(id), 0) AS m FROM messages WHERE conversation_id = %s;",
            (session_id,),
        )
        return row["m"] if row else 0

    def last_question_id(
        self, session_id: str, kinds: tuple[str, ...] = ("clarification",)
    ) -> int | None:
        """Id of the most recent assistant *question* of the given kind(s).

        Used by the bridges to pair an answer with its question: the user's reply
        to a question is a later user message; if none exists yet the run is still
        awaiting input. Returns None when no such question has been asked.
        """
        placeholders = ", ".join(["%s"] * len(kinds))
        row = self._query_one(
            "SELECT MAX(id) AS m FROM messages "
            "WHERE conversation_id = %s AND role = 'assistant' "
            f"AND kind IN ({placeholders});",
            (session_id, *kinds),
        )
        return row["m"] if row and row["m"] is not None else None

    def user_replies_after(self, session_id: str, after_id: int, kind: str | None = None) -> list:
        sql = (
            "SELECT id, content, kind FROM messages "
            "WHERE conversation_id = %s AND id > %s AND role = 'user'"
        )
        params = [session_id, after_id]
        if kind is not None:
            sql += " AND kind = %s"
            params.append(kind)
        sql += " ORDER BY id;"
        return self._query_all(sql, tuple(params))

    # ---- run events -----------------------------------------------------------
    def insert_run_event(
        self, session_id: str, event_type: str, payload: dict, seq: int | None = None
    ) -> None:
        self._execute(
            """
            INSERT INTO run_events (session_id, seq, event_type, payload)
            VALUES (%s, %s, %s, %s);
            """,
            (session_id, seq, event_type, Json(payload)),
        )

    # ---- artifacts ------------------------------------------------------------
    def upsert_artifact(self, session_id: str, name: str, content: str, kind: str) -> None:
        self._execute(
            """
            INSERT INTO artifacts (session_id, name, kind, content)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (session_id, name) DO UPDATE SET
                kind = EXCLUDED.kind, content = EXCLUDED.content, created_at = now();
            """,
            (session_id, name, kind, content),
        )

    def get_artifacts(self, session_id: str) -> list:
        """Every stored artifact (name + content) for a session.

        The read side of :meth:`upsert_artifact`: on resume the runner rehydrates
        these back onto local disk (see ``runner.artifacts.rehydrate_artifacts``)
        so a run can continue on any box even though the state machine reads its
        stage artifacts from files.
        """
        return self._query_all(
            "SELECT name, content FROM artifacts WHERE session_id = %s;", (session_id,)
        )

    def get_artifact(self, session_id: str, name: str) -> dict | None:
        """Fetch one persisted artifact's content by name (for a rerun's inputs)."""
        return self._query_one(
            "SELECT name, kind, content FROM artifacts WHERE session_id = %s AND name = %s;",
            (session_id, name),
        )

    # ---- sessions (backing store for the engine) ------------------------------
    def session_get(self, session_id: str) -> dict | None:
        row = self._query_one(
            "SELECT data FROM sessions WHERE session_id = %s;", (session_id,)
        )
        return row["data"] if row else None

    def session_save(self, record: dict) -> None:
        self._execute(
            """
            INSERT INTO sessions (session_id, researcher_id, state, status, data, updated_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (session_id) DO UPDATE SET
                researcher_id = EXCLUDED.researcher_id,
                state = EXCLUDED.state,
                status = EXCLUDED.status,
                data = EXCLUDED.data,
                updated_at = now();
            """,
            (
                record.get("session_id"), record.get("researcher_id"),
                record.get("state"), record.get("status"), Json(record),
            ),
        )

    def session_list(self, researcher_id: str | None = None) -> list:
        if researcher_id is None:
            return [r["data"] for r in self._query_all(
                "SELECT data FROM sessions ORDER BY updated_at DESC;", ())]
        return [r["data"] for r in self._query_all(
            "SELECT data FROM sessions WHERE researcher_id = %s ORDER BY updated_at DESC;",
            (researcher_id,),
        )]

    # ---- low-level ------------------------------------------------------------
    def _execute(self, sql: str, params: tuple) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def _query_one(self, sql: str, params: tuple) -> dict | None:
        conn = self._connect()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(sql, params)
            row = cursor.fetchone()
            cursor.close()
            return row
        finally:
            conn.close()

    def _query_all(self, sql: str, params: tuple) -> list:
        conn = self._connect()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            cursor.close()
            return rows
        finally:
            conn.close()


class JobNotifyWaiter:
    """Blocks until a job is queued, using Postgres LISTEN/NOTIFY (Phase 2).

    Holds one long-lived autocommit connection that ``LISTEN``s on
    :data:`JOBS_CHANNEL`; the jobs-insert trigger (migration 003) ``NOTIFY``s it.
    :meth:`wait` sleeps on the socket and returns the moment a job arrives —
    replacing the old always-on ``sleep(poll)`` spin. The ``timeout`` is only a
    safety-net poll cadence (so a missed NOTIFY still gets picked up eventually),
    so it can be generous rather than 1s.

    Use as a context manager so the dedicated connection is always closed::

        with JobNotifyWaiter(db) as waiter:
            waiter.wait(timeout=30.0)
    """

    def __init__(self, db: RunnerDB, channel: str = JOBS_CHANNEL):
        self.db = db
        self.channel = channel
        self._conn = None

    def __enter__(self) -> "JobNotifyWaiter":
        self._conn = self.db._connect()
        self._conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = self._conn.cursor()
        cur.execute(f"LISTEN {self.channel};")
        cur.close()
        return self

    def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for a NOTIFY. True if one arrived."""
        if self._conn is None:
            raise RuntimeError("JobNotifyWaiter must be used as a context manager")
        if select.select([self._conn], [], [], timeout) == ([], [], []):
            return False  # timed out; caller re-polls as a safety net
        self._conn.poll()
        notified = bool(self._conn.notifies)
        self._conn.notifies.clear()
        return notified

    def __exit__(self, *exc) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
