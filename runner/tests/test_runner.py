"""Unit tests for the runner — fakes stand in for the DB and the engine, so no
Postgres or pixi environment is needed. These exercise the job loop, the
approval gate, and the chat/event bridges end to end in-process.
"""
import json
import types

from runner import runner
from runner.artifacts import capture_artifacts
from runner.bridges import DbAsk, PgEventSink, request_plan_approval
from runner.pg_store import PgStore

SESSION = "conv-1"


class FakeDB:
    """In-memory stand-in for RunnerDB."""

    def __init__(self, approval="approve", clarify="25 degrees C", jobs=None):
        self.messages = []
        self.events = []
        self.status = None
        self.state = None
        self.jobs_done = []
        self._approval = approval
        self._clarify = clarify
        self._jobs = list(jobs or [])
        self.sessions = {}
        self.artifacts = []

    # messages / status / state
    def add_assistant_message(self, sid, content, *, kind="chat", state=None):
        self.messages.append({"content": content, "kind": kind, "state": state})
        return len(self.messages)

    def max_message_id(self, sid):
        return len(self.messages)

    def user_replies_after(self, sid, after_id, kind=None):
        if kind == "approval_response":
            return [{"content": self._approval, "kind": kind}]
        return [{"content": self._clarify, "kind": "chat"}]

    def set_conversation_status(self, sid, status):
        self.status = status

    def set_conversation_state(self, sid, state):
        self.state = state

    def insert_run_event(self, sid, event_type, payload, seq=None):
        self.events.append({"event_type": event_type, "payload": payload})

    def upsert_artifact(self, sid, name, content, kind):
        self.artifacts.append({"name": name, "content": content, "kind": kind})

    # jobs
    def claim_job(self):
        return self._jobs.pop(0) if self._jobs else None

    def mark_job(self, job_id, status):
        self.jobs_done.append((job_id, status))

    # sessions (for PgStore tests)
    def session_get(self, sid):
        return self.sessions.get(sid)

    def session_save(self, record):
        self.sessions[record["session_id"]] = record

    def session_list(self, researcher_id=None):
        return list(self.sessions.values())


def _event(event_type, payload):
    return types.SimpleNamespace(event_type=event_type, payload=json.dumps(payload))


class FakeOrchestrator:
    """Two-leg run: leg 1 clarifies + pauses at BUILD, leg 2 completes."""

    def __init__(self, ask, sink):
        self.ask = ask
        self.sink = sink
        self.sm = types.SimpleNamespace(
            context=types.SimpleNamespace(artifacts={}),
            current_state=types.SimpleNamespace(name="TERMINATE"),
        )
        self._leg = 0

    def run(self, until=None):
        if self._leg == 0:
            self._leg = 1
            self.ask("What temperature?")  # exercises the clarify bridge
            self.sink.publish(_event("stage.completed", {"from": "PLAN", "to": "BUILD"}))
            return "paused"
        self.sink.publish(_event("run.completed", {"state": "TERMINATE"}))
        return "completed"


class FakeEngine:
    STATE_BUILD = "BUILD"

    def __init__(self, plan=None):
        self._plan = plan or {"selected_method": {"name": "demo-tool"}, "cost": 0.1}

    def build_orchestrator(self, *, session_id, researcher_id, request, ask, sink, store):
        return FakeOrchestrator(ask, sink)

    def read_execution_plan(self, orch):
        return self._plan

    def final_summary(self, orch):
        return "Run complete."


class TestProcessJob:
    def test_approved_run_completes(self):
        db = FakeDB(approval="approve")
        runner.process_job({"session_id": SESSION, "kind": "start", "params": {}}, db, FakeEngine())
        kinds = [m["kind"] for m in db.messages]
        assert "clarification" in kinds
        assert "approval_request" in kinds
        assert db.status == "completed"
        assert any(e["event_type"] == "run.completed" for e in db.events)
        assert db.messages[-1]["content"] == "Run complete."

    def test_rejected_run_stops_before_build(self):
        db = FakeDB(approval="reject")
        runner.process_job({"session_id": SESSION, "kind": "start", "params": {}}, db, FakeEngine())
        assert db.status == "rejected"
        # no completion event because leg 2 never ran
        assert not any(e["event_type"] == "run.completed" for e in db.events)
        assert "rejected" in db.messages[-1]["content"].lower()

    def test_auto_run_skips_approval_gate(self, monkeypatch):
        # Unattended mode (TWAIN_AUTO_RUN) runs to completion without the plan-
        # approval gate. If the gate were reached it would raise (fail fast, no hang).
        monkeypatch.setenv("TWAIN_AUTO_RUN", "1")
        monkeypatch.setattr(
            runner, "request_plan_approval",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("approval must be skipped")),
        )
        db = FakeDB()  # no approval reply provided
        runner.process_job({"session_id": SESSION, "kind": "start", "params": {}}, db, FakeEngine())
        assert "approval_request" not in [m["kind"] for m in db.messages]
        assert db.status == "completed"
        assert any(e["event_type"] == "run.completed" for e in db.events)

    def test_unsupported_kind_raises(self):
        db = FakeDB()
        try:
            runner.process_job({"session_id": SESSION, "kind": "resume", "params": {}}, db, FakeEngine())
            raise AssertionError("expected NotImplementedError")
        except NotImplementedError:
            pass


class TestRunLoop:
    def test_processes_one_job_then_marks_done(self):
        db = FakeDB(jobs=[{"id": 7, "session_id": SESSION, "kind": "start", "params": {}}])
        runner.run_loop(once=True, db=db, engine_factory=FakeEngine, sleep=lambda _s: None)
        assert (7, "running") in db.jobs_done
        assert (7, "done") in db.jobs_done

    def test_failing_job_is_marked_error(self):
        class Boom(FakeEngine):
            def build_orchestrator(self, **_kwargs):
                raise RuntimeError("kaboom")

        db = FakeDB(jobs=[{"id": 9, "session_id": SESSION, "kind": "start", "params": {}}])
        runner.run_loop(once=True, db=db, engine_factory=Boom, sleep=lambda _s: None)
        assert (9, "error") in db.jobs_done
        assert db.status == "error"


class TestBridges:
    def test_db_ask_posts_question_and_returns_reply(self):
        db = FakeDB(clarify="use water as solvent")
        ask = DbAsk(db, SESSION, sleep=lambda _s: None)
        answer = ask("Which solvent?")
        assert answer == "use water as solvent"
        assert db.messages[0]["kind"] == "clarification"
        assert db.status == "running"  # reset after the reply

    def test_request_plan_approval_returns_decision(self):
        db = FakeDB(approval="APPROVE")
        decision = request_plan_approval(db, SESSION, {"cost": 1.0}, sleep=lambda _s: None)
        assert decision == "approve"  # normalized
        assert db.messages[0]["kind"] == "approval_request"

    def test_event_sink_persists_and_mirrors_state(self):
        db = FakeDB()
        sink = PgEventSink(db, SESSION)
        sink.publish(_event("stage.completed", {"from": "DISCOVER", "to": "PLAN"}))
        sink.publish(_event("run.completed", {"state": "TERMINATE"}))
        assert db.state == "PLAN"
        assert db.status == "completed"
        assert [e["event_type"] for e in db.events] == ["stage.completed", "run.completed"]


class TestPgStore:
    def test_save_and_get_roundtrip(self):
        store = PgStore(FakeDB())
        store.save_session({"session_id": "s1", "status": "running", "researcher_id": "u"})
        assert store.get_session("s1")["status"] == "running"

    def test_save_requires_session_id(self):
        store = PgStore(FakeDB())
        try:
            store.save_session({"status": "running"})
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_resume_only_when_resumable(self):
        db = FakeDB()
        store = PgStore(db)
        store.save_session({"session_id": "s1", "status": "completed"})
        assert store.resume_session("s1") is None
        store.save_session({"session_id": "s2", "status": "paused"})
        assert store.resume_session("s2")["session_id"] == "s2"


class TestCaptureArtifacts:
    def test_captures_specs_and_bundle_files(self, tmp_path):
        plan = tmp_path / "execution_plan_x.json"
        plan.write_text('{"cost": 1}')
        bundle = tmp_path / "run_bundle_x"
        bundle.mkdir()
        (bundle / "main.py").write_text("import pymatgen")
        (bundle / "requirements.txt").write_text("pymatgen")
        orch = types.SimpleNamespace(
            sm=types.SimpleNamespace(
                context=types.SimpleNamespace(
                    artifacts={
                        "execution_plan": str(plan),
                        "run_bundle": str(bundle),
                        "script": str(bundle / "main.py"),  # duplicate → skipped
                    }
                )
            )
        )
        db = FakeDB()
        count = capture_artifacts(db, "s1", orch)
        names = {a["name"] for a in db.artifacts}
        kinds = {a["name"]: a["kind"] for a in db.artifacts}
        assert count == 3
        assert names == {"execution_plan", "run_bundle/main.py", "run_bundle/requirements.txt"}
        assert "script" not in names
        assert kinds["run_bundle/main.py"] == "python"
        assert kinds["execution_plan"] == "json"
