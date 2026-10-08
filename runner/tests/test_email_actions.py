"""Email gate buttons, end to end against a real Postgres: the worker issues them,
the API's confirmation page and POST answer the gate exactly once."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from runner import email_actions as issuer

REPO = Path(__file__).resolve().parents[2]


def _pg_available():
    try:
        return subprocess.run(["pg_isready", "-h", "localhost"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


class FakeDB:
    def __init__(self, gate):
        self.gate, self.rows = gate, []

    def pending_gate(self, session_id):
        return self.gate

    def insert_email_actions(self, rows, hours):
        self.rows += rows


class TestIssue:
    def test_one_button_per_choice_and_only_hashes_are_stored(self):
        db = FakeDB({"id": 7, "kind": "approval_request"})
        buttons = issuer.issue(db, "s1", base_url="https://twain.example")
        assert [b["label"] for b in buttons] == ["Approve plan", "Reject"]
        token = buttons[0]["url"].rsplit("/", 1)[1]
        assert buttons[0]["url"].startswith("https://twain.example/api/actions/")
        stored = {r[0] for r in db.rows}
        assert token not in stored and issuer.token_hash(token) in stored

    @pytest.mark.parametrize("gate", [None, {"id": 3, "kind": "clarification"}])
    def test_no_buttons_without_a_yes_no_gate(self, gate):
        assert issuer.issue(FakeDB(gate), "s1", base_url="https://x") == []

    def test_no_buttons_without_a_public_url(self, monkeypatch):
        monkeypatch.delenv("TWAIN_API_PUBLIC_URL", raising=False)
        assert issuer.issue(FakeDB({"id": 7, "kind": "heavy_confirm"}), "s1") == []

    def test_the_email_carries_the_buttons(self):
        from runner import notifications as N
        html = N._html_body("s", "Your plan is ready.\n\nDetails", [
            {"label": "Approve plan", "url": "https://x/api/actions/t1", "primary": True},
            {"label": "Reject", "url": "https://x/api/actions/t2", "primary": False}])
        assert 'href="https://x/api/actions/t1"' in html and ">Reject</a>" in html


@pytest.mark.skipif(not _pg_available(), reason="no local Postgres")
class TestAgainstPostgres:
    @pytest.fixture
    def env(self, monkeypatch):
        name = f"twain_actions_test_{uuid.uuid4().hex[:8]}"
        pg = {**os.environ, "PGGSSENCMODE": "disable"}
        subprocess.run(["createdb", "-h", "localhost", name], check=True, env=pg)
        for f in sorted((REPO / "api" / "migrations").glob("*.sql")):
            subprocess.run(["psql", "-h", "localhost", "-d", name, "-q", "-v", "ON_ERROR_STOP=1",
                            "-f", str(f)], check=True, capture_output=True, env=pg)
        user = os.environ.get("USER", "postgres")
        for k, v in {"DB_HOST": "localhost", "DB_NAME": name, "DB_USER": user, "DB_PASSWORD": "",
                     "DB_PORT": "5432", "TWAIN_DB_FROM_ENV": "true", "TWAIN_DISPATCH": "db"}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("AWS_SECRET_ARN", raising=False)
        from runner import db as runner_db
        monkeypatch.setattr(runner_db, "DB_HOST", "localhost")
        monkeypatch.setattr(runner_db, "DB_NAME", name)
        monkeypatch.setattr(runner_db, "DB_USER", user)
        monkeypatch.syspath_prepend(str(REPO / "api"))
        for mod in ("database", "conversations", "dispatch", "email_actions"):
            sys.modules.pop(mod, None)
        api_actions = importlib.import_module("email_actions")
        db = runner_db.RunnerDB()
        yield db, api_actions
        for mod in ("database", "conversations", "dispatch", "email_actions"):
            sys.modules.pop(mod, None)
        subprocess.run(["dropdb", "-h", "localhost", name], env=pg)

    def _run(self, db, kind, status):
        uid = db._query_one("INSERT INTO users (subject, email) VALUES (%s, 'r@wustl.edu') "
                            "RETURNING id;", (uuid.uuid4().hex,), commit=True)["id"]
        sid = db._query_one("INSERT INTO conversations (user_id, title, status) VALUES "
                            "(%s, 'silicon band gap', %s) RETURNING id;", (uid, status),
                            commit=True)["id"]
        mid = db._query_one("INSERT INTO messages (conversation_id, role, content, kind) VALUES "
                            "(%s, 'assistant', 'q', %s) RETURNING id;", (sid, kind),
                            commit=True)["id"]
        return str(sid), mid

    @staticmethod
    def _tokens(buttons):
        return [b["url"].rsplit("/", 1)[1] for b in buttons]

    def test_approve_from_email_answers_the_gate_once(self, env):
        db, api = env
        sid, _ = self._run(db, "approval_request", "awaiting_approval")
        approve, reject = self._tokens(issuer.issue(db, sid, base_url="https://x"))
        assert api.peek(approve).state == "ok"                 # GET: shows, doesn't act
        assert api.consume(approve).state == "ok"
        reply = db._query_one("SELECT content, kind FROM messages WHERE conversation_id = %s "
                              "ORDER BY id DESC LIMIT 1;", (sid,))
        assert (reply["content"], reply["kind"]) == ("approve", "approval_response")
        status = db._query_one("SELECT status FROM conversations WHERE id = %s;", (sid,))["status"]
        assert status == "running"
        jobs = db._query_one("SELECT count(*) AS n FROM jobs WHERE session_id = %s AND kind = 'resume';",
                             (sid,))["n"]
        assert jobs == 1                                        # the run was woken
        assert api.consume(approve).state == "used"             # single use
        assert api.consume(reject).state == "used"              # the sibling is spent too

    def test_a_yes_no_gate_sends_the_answer_as_a_reply(self, env):
        db, api = env
        sid, _ = self._run(db, "heavy_confirm", "awaiting_input")
        yes, _no = self._tokens(issuer.issue(db, sid, base_url="https://x"))
        assert api.consume(yes).state == "ok"
        reply = db._query_one("SELECT content FROM messages WHERE conversation_id = %s AND role = 'user' "
                              "ORDER BY id DESC LIMIT 1;", (sid,))
        assert reply["content"] == "yes"

    def test_a_button_for_an_old_question_does_nothing(self, env):
        db, api = env
        sid, _ = self._run(db, "approval_request", "awaiting_approval")
        approve, _ = self._tokens(issuer.issue(db, sid, base_url="https://x"))
        db._execute("INSERT INTO messages (conversation_id, role, content, kind) VALUES "
                    "(%s, 'assistant', 'a newer plan', 'approval_request');", (sid,))
        assert api.consume(approve).state == "answered"         # bound to its own gate

    def test_an_expired_or_unknown_button_does_nothing(self, env):
        db, api = env
        sid, _ = self._run(db, "approval_request", "awaiting_approval")
        approve, _ = self._tokens(issuer.issue(db, sid, base_url="https://x"))
        db._execute("UPDATE email_actions SET expires_at = now() - interval '1 minute';", ())
        assert api.consume(approve).state == "expired"
        assert api.peek("not-a-token").state == "unknown"
