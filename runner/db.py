"""PostgreSQL access for the runner service.

The runner shares one database with the API (see `docs/architecture/web_ui_plan.md`
§4). It writes assistant messages, run_events, and session snapshots, and reads
back the user's replies. Connection settings mirror `api/database.py`: DB_* env
vars, with the password resolved from AWS Secrets Manager in the cloud and from
``DB_PASSWORD`` locally.
"""
import hashlib
import json
import os
import secrets
import select
import time

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from psycopg2.extras import Json, RealDictCursor

from runner import dispatch

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "twaindb")
DB_USER = os.getenv("DB_USER", "postgres")

RESUMABLE_STATUSES = ("running", "paused", "error")

# Postgres channel the jobs-insert trigger NOTIFYs (see migration 003). The runner
# LISTENs on it so a newly queued job wakes it immediately instead of on the next
# poll tick — no always-on 1s spin (Phase 2).
JOBS_CHANNEL = "twain_jobs"
# NOTIFYed (payload: Slurm job id) when a RIS API webhook lands -- migration 012.
RIS_EVENTS_CHANNEL = "ris_job_events"


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


#: Re-queue status for a job row: 'dispatching' if it was ever sent through SQS,
#: else this process's default (the %s parameter). See reap_stale_jobs.
_REQUEUE_STATUS_SQL = "CASE WHEN published_at IS NOT NULL THEN 'dispatching' ELSE %s END"


class RunnerDB:
    """Short-lived-connection helpers for everything the runner reads/writes."""

    def _connect(self):
        # Same libpq options as the API (api/database.py connection_options):
        # fail fast on a dead server, and never negotiate Kerberos encryption --
        # a cached WashU ticket plus a down VPN hung GSS negotiation for good.
        return psycopg2.connect(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=_resolve_db_password(),
            connect_timeout=int(os.environ.get("DB_CONNECT_TIMEOUT", "10")),
            gssencmode=os.environ.get("PGGSSENCMODE", "disable"),
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

    def requeue_job(self, job_id: int) -> None:
        """Back for another attempt, in the world the job came from (see the reaper)."""
        self._execute(
            f"UPDATE jobs SET status = {_REQUEUE_STATUS_SQL}, published_at = NULL WHERE id = %s;",
            (dispatch.requeue_status(), job_id))

    # ---- SQS dispatch (P2, #171) -----------------------------------------------
    def claim_job_by_id(self, job_id: int) -> dict | None:
        """Claim job ``job_id`` from its SQS message, or None if it isn't claimable.

        Idempotent: only a 'dispatching' row is claimed, so a redelivered or
        duplicate message for a job already claimed, done, or dead-lettered is a
        no-op (the caller deletes the message). Same per-run guard as
        :meth:`claim_job`: never while another job of the run is in flight.
        """
        conn = self._connect()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(
                """
                UPDATE jobs SET status = 'claimed', claimed_at = now(),
                                heartbeat_at = now(), attempts = attempts + 1
                WHERE id = %s AND status = 'dispatching'
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs active
                      WHERE active.session_id = jobs.session_id
                        AND active.status IN ('claimed', 'running'))
                RETURNING id, session_id, kind, params, attempts;
                """,
                (int(job_id),),
            )
            job = cursor.fetchone()
            conn.commit()
            cursor.close()
            return job
        finally:
            conn.close()

    def job_status(self, job_id: int) -> str | None:
        row = self._query_one("SELECT status FROM jobs WHERE id = %s;", (int(job_id),))
        return row["status"] if row else None

    def enqueue_resume(self, session_id: str) -> list:
        """Queue a resume for ``session_id`` (none if one is already pending).

        Returns ``[(job_id, session_id, "resume")]`` to hand to
        :func:`runner.dispatch.send`, or ``[]``.
        """
        row = self._query_one(
            """
            INSERT INTO jobs (session_id, kind, params, status)
            SELECT %s, 'resume', '{}'::jsonb, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM jobs WHERE session_id = %s AND kind = 'resume'
                  AND status IN ('queued', 'dispatching'))
            RETURNING id;
            """,
            (session_id, dispatch.requeue_status(), session_id),
            commit=True,
        )
        return [(row["id"], session_id, "resume")] if row else []

    def unpublished_jobs(self, grace_seconds: float, limit: int = 50) -> list:
        """Outbox rows whose SQS send never happened (or was cleared for a re-send)."""
        rows = self._query_all(
            """
            SELECT id, session_id, kind FROM jobs
            WHERE status = 'dispatching' AND published_at IS NULL
              AND created_at < now() - make_interval(secs => %s)
            ORDER BY id LIMIT %s;
            """,
            (grace_seconds, limit),
        )
        return [(r["id"], r["session_id"], r["kind"]) for r in rows]

    def mark_published(self, job_ids) -> None:
        if job_ids:
            self._execute("UPDATE jobs SET published_at = now() WHERE id = ANY(%s);",
                          (list(job_ids),))

    # ---- cluster jobs: Slurm jobs a run is paused on (P2, #171) ------------------
    def latest(self, session_id: str) -> dict | None:
        """The run's newest cluster job (the adapter's detached-mode store API)."""
        return self._query_one(
            "SELECT * FROM cluster_jobs WHERE session_id = %s ORDER BY attempt DESC LIMIT 1;",
            (session_id,))

    def record_submitted(self, session_id: str, attempt: int, ris_job_id: str,
                         s3_prefix: str, detail: dict) -> None:
        self._execute(
            """
            INSERT INTO cluster_jobs (ris_job_id, session_id, attempt, s3_prefix, detail)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (ris_job_id) DO NOTHING;
            """,
            (str(ris_job_id), session_id, int(attempt), s3_prefix, Json(detail or {})))

    # ---- RIS inventory (#185): what the cluster envs actually contain ----------
    def inventory_pending(self) -> dict | None:
        """The inventory job in flight, if any."""
        return self._query_one(
            "SELECT * FROM ris_inventory WHERE status = 'submitted' "
            "ORDER BY id DESC LIMIT 1;", ())

    def inventory_due(self, every_hours: float) -> bool:
        """True when no inventory is in flight and none ended in the last ``every_hours``.

        A failed one counts too: retrying a broken inventory every tick would
        submit a Slurm job every 30 seconds.
        """
        row = self._query_one(
            """
            SELECT
              EXISTS (SELECT 1 FROM ris_inventory WHERE status = 'submitted') AS busy,
              EXISTS (SELECT 1 FROM ris_inventory
                      WHERE status IN ('ingested', 'failed')
                        AND finished_at > now() - make_interval(secs => %s)) AS fresh;
            """, (float(every_hours) * 3600,))
        return bool(row) and not row["busy"] and not row["fresh"]

    def inventory_submitting(self) -> int:
        """A new 'submitted' row (its job id is set once ris-api answers)."""
        return int(self._query_one(
            "INSERT INTO ris_inventory (status) VALUES ('submitted') RETURNING id;",
            (), commit=True)["id"])

    def inventory_set_job(self, inventory_id: int, ris_job_id: str) -> None:
        self._execute("UPDATE ris_inventory SET ris_job_id = %s WHERE id = %s;",
                      (str(ris_job_id), int(inventory_id)))

    def inventory_ingested(self, inventory_id: int, snapshot: dict) -> None:
        self._execute(
            """
            UPDATE ris_inventory SET status = 'ingested', finished_at = now(),
              taken_at = %s, envs_root = %s, envs = %s, modules = %s
            WHERE id = %s;
            """,
            (snapshot.get("taken_at"), snapshot.get("envs_root"),
             Json(snapshot.get("envs") or {}), Json(snapshot.get("modules") or []),
             int(inventory_id)))

    def inventory_failed(self, inventory_id: int, error: str) -> None:
        self._execute(
            "UPDATE ris_inventory SET status = 'failed', finished_at = now(), error = %s "
            "WHERE id = %s;", (str(error)[:2000], int(inventory_id)))

    def latest_inventory(self, max_age_hours: float) -> dict | None:
        """The newest ingested inventory no older than ``max_age_hours``, or None."""
        return self._query_one(
            """
            SELECT id, taken_at, envs_root, envs FROM ris_inventory
            WHERE status = 'ingested'
              AND finished_at > now() - make_interval(secs => %s)
            ORDER BY finished_at DESC LIMIT 1;
            """, (float(max_age_hours) * 3600,))

    def mark(self, ris_job_id: str, status: str) -> None:
        stamp = {"collected": "collected_at", "finished": "finished_at"}.get(status)
        self._execute(
            f"UPDATE cluster_jobs SET status = %s{', ' + stamp + ' = now()' if stamp else ''} "
            "WHERE ris_job_id = %s;", (status, str(ris_job_id)))

    def open_cluster_jobs(self, min_age_seconds: float, limit: int = 100) -> list:
        """Submitted jobs not polled in the last ``min_age_seconds`` (oldest first)."""
        return self._query_all(
            """
            SELECT * FROM cluster_jobs
            WHERE status = 'submitted'
              AND (last_polled_at IS NULL
                   OR last_polled_at < now() - make_interval(secs => %s))
            ORDER BY last_polled_at NULLS FIRST LIMIT %s;
            """,
            (min_age_seconds, limit))

    def cluster_job(self, ris_job_id: str) -> dict | None:
        return self._query_one("SELECT * FROM cluster_jobs WHERE ris_job_id = %s;",
                               (str(ris_job_id),))

    def update_cluster_poll(self, ris_job_id: str, *, slurm_state: str | None,
                            node: str | None, reason: str | None, log_offset: int) -> None:
        self._execute(
            """
            UPDATE cluster_jobs SET last_polled_at = now(), slurm_state = %s,
                   node = %s, reason = %s, log_offset = %s
            WHERE ris_job_id = %s;
            """,
            (slurm_state, node, reason, int(log_offset), str(ris_job_id)))

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
            # Re-queue the recoverable ones for another attempt -- in the world
            # each job came from, whichever runner reaps it: a job that went
            # through SQS (published_at set) goes back to 'dispatching' with
            # published_at cleared, so the relay re-sends it. Deciding by this
            # process's mode instead would let a polling runner reap a crashed
            # worker's job as 'queued' and drive a run paused on a Slurm job --
            # with no detached mode, it would submit a duplicate attempt.
            cursor.execute(
                f"""
                UPDATE jobs SET status = {_REQUEUE_STATUS_SQL}, published_at = NULL
                WHERE status IN ('claimed', 'running')
                  AND COALESCE(heartbeat_at, claimed_at) < now() - make_interval(secs => %s)
                  AND attempts < %s;
                """,
                (dispatch.requeue_status(), lease_seconds, max_attempts),
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

    def conversation_status(self, session_id: str) -> str | None:
        """This run's lifecycle status, or None if the conversation is unknown.

        Read by the driver to tell "settle this run" from "this run was already
        settled" — a terminate request is sticky, so without the check every
        later slice re-announced the cancellation (chat + email).
        """
        row = self._query_one(
            "SELECT status FROM conversations WHERE id = %s;", (session_id,)
        )
        return row.get("status") if row else None

    def set_conversation_state(self, session_id: str, state: str) -> None:
        self._execute(
            "UPDATE conversations SET current_state = %s, updated_at = now() WHERE id = %s;",
            (state, session_id),
        )

    def owner_contact(self, session_id: str) -> dict | None:
        """Contact details of the researcher who owns this run, or None.

        Joins the run's conversation to its owning user (``conversations.id`` is the
        session_id; ``conversations.user_id`` → ``users``). Returns
        ``{"email", "name", "phone", "notify_prefs"}`` so the notifier can reach
        the *specific* researcher who left the session — by email (SES/SendGrid)
        or SMS (SNS to their ``phone``) — and honor their notification
        preferences. Any field may be None (e.g. no phone on file); returns None
        outright when the session or user is unknown, so the caller can fall
        back to the configured default.
        """
        row = self._query_one(
            "SELECT u.email, u.name, u.phone, u.notify_prefs FROM conversations c "
            "JOIN users u ON u.id = c.user_id "
            "WHERE c.id = %s;",
            (session_id,),
        )
        if not row:
            return None
        return {"email": row.get("email"), "name": row.get("name"),
                "phone": row.get("phone"), "notify_prefs": row.get("notify_prefs")}

    def pending_gate(self, session_id: str) -> dict | None:
        """The question message this run is parked on, if it's awaiting the researcher.

        The newest question-kind message of a run whose status is
        awaiting_approval / awaiting_input -- what an email button may answer.
        """
        return self._query_one(
            """
            SELECT m.id, m.kind, m.content
            FROM conversations c
            JOIN messages m ON m.conversation_id = c.id
            WHERE c.id = %s AND c.status IN ('awaiting_approval', 'awaiting_input')
              AND m.kind IN ('clarification', 'heavy_confirm', 'validation_gate',
                             'revision_request', 'approval_request')
            ORDER BY m.id DESC LIMIT 1;
            """, (session_id,))

    def insert_email_actions(self, rows, valid_hours: float) -> None:
        """Store issued tokens: ``[(token_hash, session_id, message_id, kind, choice, label)]``."""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            for row in rows:
                cursor.execute(
                    """
                    INSERT INTO email_actions (token_hash, session_id, gate_message_id,
                                               gate_kind, choice, label, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s));
                    """, (*row, float(valid_hours) * 3600))
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    # ---- shared-environment change proposals (#187) ---------------------------
    def env_proposal_open(self, env: str, package: str) -> dict | None:
        return self._query_one(
            "SELECT * FROM env_proposals WHERE env = %s AND package = %s "
            "AND status IN ('pending', 'approved', 'building') ORDER BY id DESC LIMIT 1;",
            (env, package))

    def env_proposal_insert(self, **fields) -> dict:
        return self._query_one(
            """
            INSERT INTO env_proposals
                (session_id, env, package, module, reason, spec_before, spec_after)
            VALUES (%(session_id)s, %(env)s, %(package)s, %(module)s, %(reason)s,
                    %(spec_before)s, %(spec_after)s)
            RETURNING *;
            """, fields, commit=True)

    def env_proposals_by_status(self, status: str) -> list:
        return self._query_all(
            "SELECT * FROM env_proposals WHERE status = %s ORDER BY id;", (status,))

    def env_proposal_update(self, proposal_id: int, **fields) -> None:
        allowed = {"status", "ris_job_id", "version", "result"}
        sets = [f"{k} = %({k})s" for k in fields if k in allowed]
        if fields.get("status") in ("promoted", "failed"):
            sets.append("finished_at = now()")
        self._execute(f"UPDATE env_proposals SET {', '.join(sets)} WHERE id = %(id)s;",
                      {**fields, "id": int(proposal_id)})

    def inventory_mark_due(self) -> None:
        """Make the next monitor tick re-inventory RIS (an env just changed), while the
        current snapshot stays young enough for planning to keep using it."""
        self._execute(
            "UPDATE ris_inventory SET finished_at = now() - interval '25 hours' "
            "WHERE status IN ('ingested', 'failed') "
            "AND finished_at > now() - interval '25 hours';", ())

    def insert_email_action_rows(self, rows, valid_hours: float) -> None:
        """Tokens not tied to a question: ``[(hash, session_id, kind, choice, label,
        proposal_id, recipient)]``."""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            for row in rows:
                cursor.execute(
                    """
                    INSERT INTO email_actions (token_hash, session_id, gate_kind, choice, label,
                                               proposal_id, recipient, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s));
                    """, (*row, float(valid_hours) * 3600))
            conn.commit()
            cursor.close()
        finally:
            conn.close()

    def run_title(self, session_id: str) -> str | None:
        """The run's title (its originating request), or None if unknown.

        Used by the notifier to put the prompt in the subject line so a researcher
        with several runs can tell the emails apart.
        """
        row = self._query_one(
            "SELECT title FROM conversations WHERE id = %s;", (session_id,)
        )
        return (row or {}).get("title") if row else None

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
        """Id of the most recent *live* assistant question of the given kind(s).

        Used by the bridges to pair an answer with its question: the user's reply
        to a question is a later user message; if none exists yet the run is still
        awaiting input. Returns None when no such question has been asked.

        Questions marked ``state='consumed'`` are retired and skipped. A re-run
        retires the questions of the pass it rewinds past (see
        ``conversations.rerun_conversation``): choosing to re-run *is* the answer,
        and an abandoned question left looking outstanding makes the next gate
        believe it has already asked -- so it suspends the run without posting
        anything and the researcher waits on a question that never arrives.
        """
        placeholders = ", ".join(["%s"] * len(kinds))
        row = self._query_one(
            "SELECT MAX(id) AS m FROM messages "
            "WHERE conversation_id = %s AND role = 'assistant' "
            f"AND kind IN ({placeholders}) "
            "AND (state IS NULL OR state <> 'consumed');",
            (session_id, *kinds),
        )
        return row["m"] if row and row["m"] is not None else None

    def user_replies_after(self, session_id: str, after_id: int, kind: str | None = None) -> list:
        sql = (
            "SELECT id, content, kind, state FROM messages "
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

    def mark_reply_consumed(self, message_id: int) -> None:
        """Record that a gate acted on this user reply (see bridges.consume_approval).

        Reuses the messages.state column, which is NULL on user rows: once a
        reply is 'consumed', a later visit to the same gate (e.g. a re-run
        reaching BUILD again) must not re-apply it and instead asks afresh.
        """
        self._execute(
            "UPDATE messages SET state = 'consumed' WHERE id = %s;",
            (message_id,),
        )

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

    # ---- job tickets (S3 file I/O for Slurm jobs, #170) ---------------------------
    def issue_job_ticket(self, run_id: str, attempt: int, s3_prefix: str,
                         ttl_seconds: float) -> str:
        """A new random ticket for one run attempt's S3 prefix; returns the token.

        Only its SHA-256 is stored (migration 013). The job trades the token at
        POST /api/job-tickets/urls for presigned URLs -- read input/, write
        output/ -- so no AWS credential ever reaches the cluster.
        """
        token = secrets.token_urlsafe(32)
        self._execute(
            """
            INSERT INTO job_tickets (token_hash, run_id, attempt, s3_prefix, expires_at)
            VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s));
            """,
            (hashlib.sha256(token.encode("utf-8")).hexdigest(), run_id, int(attempt),
             s3_prefix, float(ttl_seconds)),
        )
        return token

    # ---- artifacts ------------------------------------------------------------
    def replace_library_availability(self, rows) -> None:
        """Publish the capability snapshot the app's library list reads.

        Upserts rather than truncating: the table is read by the API continuously,
        and a delete-then-insert would serve an empty list to anyone who looked
        mid-refresh. Entries dropped from a registry are cleared afterwards, in the
        same statement set, so a removed library does not linger as capability.
        """
        rows = list(rows or [])
        if not rows:
            return
        for row in rows:
            self._execute(
                """
                INSERT INTO library_availability
                    (kind, name, import_name, version, description,
                     installed, env, detail, homepage, checked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (kind, name) DO UPDATE SET
                    import_name = EXCLUDED.import_name,
                    version     = EXCLUDED.version,
                    description = EXCLUDED.description,
                    installed   = EXCLUDED.installed,
                    env         = EXCLUDED.env,
                    detail      = EXCLUDED.detail,
                    homepage    = EXCLUDED.homepage,
                    checked_at  = now();
                """,
                (row.get("kind"), row.get("name"), row.get("import_name"),
                 row.get("version"), row.get("description"),
                 bool(row.get("installed")), row.get("env"), row.get("detail"),
                 row.get("homepage")),
            )
        # Drop anything no longer in a registry, so a removed library stops being
        # advertised as capability. Placeholders are generated from the row COUNT
        # and every value is still parameterised.
        pairs = [(row.get("kind"), row.get("name")) for row in rows]
        placeholders = ", ".join(["(%s, %s)"] * len(pairs))
        self._execute(
            f"DELETE FROM library_availability WHERE (kind, name) NOT IN ({placeholders});",
            [value for pair in pairs for value in pair],
        )

    def upsert_artifact(self, session_id: str, name: str, content: str, kind: str) -> None:
        # created_at is deliberately NOT refreshed on update. A run captures its
        # artifacts more than once -- once before the terminal event is published,
        # and again from the runner's finally block as a backstop -- so bumping the
        # timestamp on the second write made the column mean "last written" and
        # left no way to audit whether the results really were committed before the
        # run announced itself finished. Keeping the insert time makes that
        # orderable against sessions.updated_at.
        self._execute(
            """
            INSERT INTO artifacts (session_id, name, kind, content)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (session_id, name) DO UPDATE SET
                kind = EXCLUDED.kind, content = EXCLUDED.content;
            """,
            (session_id, name, kind, content),
        )

    def delete_artifact(self, session_id: str, name: str) -> None:
        """Drop one stored artifact.

        Needed because a stage can *un-produce* an artifact: a correction pass
        whose rerun was skipped clears its normalized result, and an upsert-only
        store would keep serving the previous pass's number to the report as
        though this run had produced it.
        """
        self._execute(
            "DELETE FROM artifacts WHERE session_id = %s AND name = %s;",
            (session_id, name),
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

    def _query_one(self, sql: str, params: tuple, *, commit: bool = False) -> dict | None:
        """One row; ``commit=True`` for a write with RETURNING (else it rolls back)."""
        conn = self._connect()
        try:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute(sql, params)
            row = cursor.fetchone()
            if commit:
                conn.commit()
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


class RisJobEventWaiter:
    """Sleeps between Slurm polls, waking early when RIS reports on the job.

    The webhook receiver (api/ris_webhooks.py) records each ris-api job event,
    and migration 012's trigger NOTIFYs :data:`RIS_EVENTS_CHANNEL` with the job
    id. :meth:`wait` is the Slurm adapter's poll sleep: it returns as soon as a
    NOTIFY for *its* job arrives, else after ``timeout`` -- so polling stays the
    source of truth and a missed webhook only costs latency.

    The LISTEN connection opens lazily on the first wait and is reused; call
    :meth:`close` when the run ends. Any database trouble degrades to a plain
    ``sleep`` for the rest of the run rather than failing the job's wait.
    """

    def __init__(self, db: RunnerDB, channel: str = RIS_EVENTS_CHANNEL,
                 sleep=time.sleep, clock=time.monotonic):
        self.db = db
        self.channel = channel
        self._sleep = sleep
        self._clock = clock
        self._conn = None
        self._broken = False

    def _listen(self):
        if self._conn is None:
            self._conn = self.db._connect()
            self._conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cur = self._conn.cursor()
            cur.execute(f"LISTEN {self.channel};")
            cur.close()
        return self._conn

    def wait(self, job_id: str, timeout: float) -> bool:
        """Wait up to ``timeout`` s; True if an event for ``job_id`` cut it short."""
        if self._broken:
            self._sleep(timeout)
            return False
        deadline = self._clock() + timeout
        try:
            conn = self._listen()
            while True:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                if select.select([conn], [], [], remaining) == ([], [], []):
                    return False
                conn.poll()
                hit = any(n.payload == str(job_id) for n in conn.notifies)
                conn.notifies.clear()
                if hit:
                    return True
        except (psycopg2.Error, OSError, ValueError) as exc:
            print(f"[runner] RIS job-event listener unavailable ({exc}); "
                  f"falling back to plain polling")
            self._broken = True
            self.close()
            remaining = deadline - self._clock()
            if remaining > 0:
                self._sleep(remaining)
            return False

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg2.Error:
                pass
            self._conn = None
