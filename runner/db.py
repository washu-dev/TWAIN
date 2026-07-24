"""PostgreSQL access for the runner service.

The runner shares one database with the API (see `docs/architecture/web_ui_plan.md`
§4). It writes assistant messages, run_events, and session snapshots, and reads
back the user's replies. Connection settings mirror `api/database.py`: DB_* env
vars, with the password resolved from AWS Secrets Manager in the cloud and from
``DB_PASSWORD`` locally.
"""
import json
import os

import psycopg2
from psycopg2.extras import Json, RealDictCursor

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "twaindb")
DB_USER = os.getenv("DB_USER", "postgres")

RESUMABLE_STATUSES = ("running", "paused", "error")


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
        """Atomically claim the oldest queued job (FOR UPDATE SKIP LOCKED)."""
        conn = self._connect()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                """
                SELECT id, session_id, kind, params FROM jobs
                WHERE status = 'queued' ORDER BY id
                FOR UPDATE SKIP LOCKED LIMIT 1;
                """
            )
            job = cursor.fetchone()
            if job is not None:
                cursor.execute(
                    "UPDATE jobs SET status = 'claimed', claimed_at = now() WHERE id = %s;",
                    (job["id"],),
                )
            conn.commit()
            cursor.close()
            return job
        finally:
            conn.close()

    def mark_job(self, job_id: int, status: str) -> None:
        self._execute("UPDATE jobs SET status = %s WHERE id = %s;", (status, job_id))

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

    def user_replies_after(self, session_id: str, after_id: int, kind: str | None = None) -> list:
        sql = (
            "SELECT id, content, kind FROM messages "
            "WHERE conversation_id = %s AND id > %s AND role = 'user'"
        )
        params = [session_id, after_id]
        if kind is not None:
            sql += " AND kind = %s"
            params.append(kind)
        else:
            # A terminate request is a control signal, never a chat/clarify answer.
            sql += " AND kind <> 'terminate'"
        sql += " ORDER BY id;"
        return self._query_all(sql, tuple(params))

    def terminate_requested(self, session_id: str) -> bool:
        """True once the user asked to terminate this run (kind='terminate')."""
        row = self._query_one(
            "SELECT 1 AS t FROM messages "
            "WHERE conversation_id = %s AND role = 'user' AND kind = 'terminate' "
            "LIMIT 1;",
            (session_id,),
        )
        return row is not None

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
