"""Session persistence store (Story 2.5 / Story 7.1).

A thin SQLite layer that persists one row per :class:`RunSession`. The
orchestrator writes a row after every stage transition (a checkpoint), so an
interrupted or crashed run can be located and resumed.

The store deals only in plain ``dict`` records (the ``RunSession.to_dict()``
shape) so it has *no* dependency on the orchestrator package -- the orchestrator
converts between dicts and ``RunSession`` objects. Each record is stored as a
JSON blob plus a few promoted, indexed columns (``researcher_id``, ``state``,
``status``, ``updated_at``) so ``list_sessions``/``resume_session`` can query
without deserializing every row.

Default database: ``<repo>/logs/sessions.db`` (see ``twain_paths``). Pass
``db_path`` to override (tests use a temporary file; ``":memory:"`` is also
accepted).
"""
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

import twain_paths

DEFAULT_DB_PATH = twain_paths.DB_PATH

# Statuses from which a run can still be continued (i.e. it is not COMPLETED).
RESUMABLE_STATUSES = ("running", "paused", "error")


class Store:
    """SQLite-backed session store with get/list/resume/save."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(DEFAULT_DB_PATH)
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.db_path = str(Path(db_path).expanduser())
        # ``:memory:`` databases vanish when their connection closes, so keep a
        # single shared connection for the lifetime of the store.
        self._shared = sqlite3.connect(self.db_path) if self.db_path == ":memory:" else None
        self._init_schema()

    _SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id    TEXT NOT NULL PRIMARY KEY,
            researcher_id TEXT,
            state         TEXT,
            status        TEXT,
            updated_at    TEXT,
            data          TEXT NOT NULL
        )
        """

    # ------------------------------------------------------------------ connection
    def _connect(self) -> sqlite3.Connection:
        conn = self._shared or sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Wait (don't error) if another writer holds the file briefly.
        conn.execute("PRAGMA busy_timeout = 5000")
        # Guarantee the schema on every connection. The checkpoint DB can be
        # truncated or recreated between calls -- an interrupted run leaving a
        # 0-byte file, an external tool, or a stale handle -- which otherwise
        # surfaces mid-run as "no such table: sessions". Re-running an
        # idempotent CREATE TABLE IF NOT EXISTS here (a no-op once it exists) is
        # cheap and makes every read/write self-healing.
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(self._SCHEMA_SQL)
        conn.commit()

    def _init_schema(self) -> None:
        conn = self._connect()  # _connect() already ensures the schema exists
        if self._shared is None:
            conn.close()

    # ---------------------------------------------------------------------- write
    def save_session(self, record: Dict) -> None:
        """Upsert one session record (a ``RunSession.to_dict()`` dict)."""
        session_id = record.get("session_id")
        if not session_id:
            raise ValueError("record must contain a non-empty 'session_id'")
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO sessions (session_id, researcher_id, state, status, updated_at, data)
                VALUES (:session_id, :researcher_id, :state, :status, :updated_at, :data)
                ON CONFLICT(session_id) DO UPDATE SET
                    researcher_id = excluded.researcher_id,
                    state         = excluded.state,
                    status        = excluded.status,
                    updated_at    = excluded.updated_at,
                    data          = excluded.data
                """,
                {
                    "session_id": session_id,
                    "researcher_id": record.get("researcher_id"),
                    "state": record.get("state"),
                    "status": record.get("status"),
                    "updated_at": record.get("updated_at"),
                    "data": json.dumps(record),
                },
            )
            conn.commit()
        finally:
            if self._shared is None:
                conn.close()

    # ----------------------------------------------------------------------- read
    def get_session(self, session_id: str) -> Optional[Dict]:
        """Return the full session record, or ``None`` if unknown."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT data FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        finally:
            if self._shared is None:
                conn.close()
        return json.loads(row["data"]) if row else None

    def list_sessions(self, researcher_id: Optional[str] = None) -> List[Dict]:
        """List sessions (optionally for one researcher), newest update first."""
        conn = self._connect()
        try:
            if researcher_id is None:
                rows = conn.execute(
                    "SELECT data FROM sessions ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT data FROM sessions WHERE researcher_id = ? ORDER BY updated_at DESC",
                    (researcher_id,),
                ).fetchall()
        finally:
            if self._shared is None:
                conn.close()
        return [json.loads(r["data"]) for r in rows]

    def resume_session(self, session_id: str) -> Optional[Dict]:
        """Return a session record only if it is still resumable.

        Returns ``None`` for an unknown session or one already COMPLETED, so the
        caller can distinguish "nothing to resume" from "resume from here".
        """
        record = self.get_session(session_id)
        if record is None:
            return None
        if record.get("status") not in RESUMABLE_STATUSES:
            return None
        return record

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None
