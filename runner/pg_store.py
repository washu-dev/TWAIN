"""Postgres-backed session store.

Implements the same interface as the engine's SQLite ``Store``
(``modules/14_provenance_memory/store.py``) — ``get_session`` / ``save_session``
/ ``list_sessions`` / ``resume_session`` — so it can be injected straight into
``Orchestrator(store=...)``. Run state then lives in the shared ``twaindb``
instead of a SQLite file on one box.
"""
from runner.db import RESUMABLE_STATUSES, RunnerDB


class PgStore:
    def __init__(self, db: RunnerDB | None = None):
        self.db = db or RunnerDB()

    def get_session(self, session_id: str) -> dict | None:
        return self.db.session_get(session_id)

    def save_session(self, record: dict) -> None:
        if not record.get("session_id"):
            raise ValueError("record must contain a non-empty 'session_id'")
        self.db.session_save(record)

    def list_sessions(self, researcher_id: str | None = None) -> list:
        return self.db.session_list(researcher_id)

    def resume_session(self, session_id: str) -> dict | None:
        record = self.get_session(session_id)
        if record is None or record.get("status") not in RESUMABLE_STATUSES:
            return None
        return record

    def close(self) -> None:  # parity with Store; connections are short-lived
        pass
