"""Unit tests for the async runner — fakes stand in for the DB and the engine, so
no Postgres or pixi environment is needed. These exercise the suspend/resume
model: a run advances until it needs the user, then *releases* the process (it
never blocks polling), and a ``resume`` job picks it back up.
"""
import json
import threading
import types

import pytest

from runner import runner
from runner.artifacts import capture_artifacts, rehydrate_artifacts
from runner.bridges import (
    DbAsk,
    PgEventSink,
    consume_approval,
    post_plan_for_approval,
)
from runner.pg_store import PgStore
from runner.suspend import SuspendRun

SESSION = "conv-1"


class FakeDB:
    """In-memory stand-in for RunnerDB with a faithful message log."""

    def __init__(self, jobs=None):
        self.messages = []  # {id, role, content, kind, state}
        self.events = []
        self.status = None
        self.state = None
        self.jobs_done = []
        self._jobs = list(jobs or [])
        self.sessions = {}
        self.artifacts = []
        self._reap_batches = []  # each run_loop reap() call pops one batch of dead jobs
        self._next_id = 1

    # -- message helpers --------------------------------------------------------
    def _add(self, role, content, kind, state=None):
        mid = self._next_id
        self._next_id += 1
        self.messages.append(
            {"id": mid, "role": role, "content": content, "kind": kind, "state": state}
        )
        return mid

    def add_assistant_message(self, sid, content, *, kind="chat", state=None):
        return self._add("assistant", content, kind, state)

    def add_user(self, content, kind="chat"):  # test-only helper
        return self._add("user", content, kind)

    def max_message_id(self, sid):
        return self.messages[-1]["id"] if self.messages else 0

    def last_question_id(self, sid, kinds=("clarification",)):
        ids = [m["id"] for m in self.messages if m["role"] == "assistant" and m["kind"] in kinds]
        return max(ids) if ids else None

    def user_replies_after(self, sid, after_id, kind=None):
        return [
            m for m in self.messages
            if m["role"] == "user" and m["id"] > after_id and (kind is None or m["kind"] == kind)
        ]

    # -- status / state / events ------------------------------------------------
    def set_conversation_status(self, sid, status):
        self.status = status

    def set_conversation_state(self, sid, state):
        self.state = state

    def insert_run_event(self, sid, event_type, payload, seq=None):
        self.events.append({"event_type": event_type, "payload": payload})

    def upsert_artifact(self, sid, name, content, kind):
        self.artifacts.append({"name": name, "content": content, "kind": kind})

    def get_artifacts(self, sid):
        return [{"name": a["name"], "content": a["content"]} for a in self.artifacts]

    # -- jobs -------------------------------------------------------------------
    def claim_job(self):
        return self._jobs.pop(0) if self._jobs else None

    def mark_job(self, job_id, status):
        self.jobs_done.append((job_id, status))

    def heartbeat_job(self, job_id):
        pass

    def reap_stale_jobs(self, lease_seconds, max_attempts):
        return self._reap_batches.pop(0) if self._reap_batches else []

    # -- sessions ---------------------------------------------------------------
    def session_get(self, sid):
        return self.sessions.get(sid)

    def session_save(self, record):
        self.sessions[record["session_id"]] = record

    def session_list(self, researcher_id=None):
        return list(self.sessions.values())

    # -- convenience for assertions ---------------------------------------------
    def kinds(self):
        return [m["kind"] for m in self.messages if m["role"] == "assistant"]


def _event(event_type, payload):
    return types.SimpleNamespace(event_type=event_type, payload=json.dumps(payload))


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def __call__(self, session_id, reason, message):
        self.calls.append((session_id, reason, message))


class FakeOrchestrator:
    """Emulates the real orchestrator's suspend contract for driver tests.

    Drives INTAKE → (CLARIFY) → pause at BUILD on leg 1, then → (EXECUTE heavy) →
    TERMINATE on leg 2. Calls the injected ``ask`` at CLARIFY / heavy-calc; if it
    raises :class:`SuspendRun` we stay in the current state and return "paused"
    (exactly what ``Orchestrator.run`` does).
    """

    def __init__(self, ask, sink, *, clarifies=False, heavy=False):
        self.ask = ask
        self.sink = sink
        self.sm = types.SimpleNamespace(
            context=types.SimpleNamespace(artifacts={}),
            current_state=types.SimpleNamespace(name="INTAKE"),
        )
        self._clarified = not clarifies
        self._heavy = heavy
        self._heavy_done = False

    def _set(self, name):
        self.sm.current_state = types.SimpleNamespace(name=name)

    def run(self, until=None):
        try:
            return self._run(until)
        except SuspendRun as s:
            self.sink.publish(_event("run.suspended", {"state": self.sm.current_state.name, "reason": s.reason}))
            return "paused"

    def _run(self, until):
        if not self._clarified:
            self._set("CLARIFY")
            self.ask("What temperature?")  # may raise SuspendRun (suspend) or return
            self._clarified = True
        if until is not None:  # leg 1 stops at the approval gate
            self._set("BUILD")
            self.sink.publish(_event("stage.completed", {"from": "PLAN", "to": "BUILD"}))
            return "paused"
        if self._heavy and not self._heavy_done:
            self._set("EXECUTE")
            self.ask("Run the heavy calc? [y/N]")  # may raise SuspendRun or return
            self._heavy_done = True
        self._set("TERMINATE")
        self.sink.publish(_event("run.completed", {"state": "TERMINATE"}))
        return "completed"


class FakeEngine:
    STATE_BUILD = "BUILD"

    def __init__(self, plan=None, clarifies=False, heavy=False):
        self._plan = plan or {"selected_method": {"name": "demo-tool"}, "cost": 0.1}
        self._clarifies = clarifies
        self._heavy = heavy

    def build_orchestrator(self, *, session_id, researcher_id, request, ask, sink, store):
        return FakeOrchestrator(ask, sink, clarifies=self._clarifies, heavy=self._heavy)

    def current_state_name(self, orch):
        return orch.sm.current_state.name

    def read_execution_plan(self, orch):
        return self._plan

    def final_summary(self, orch):
        return "Run complete."


# ── DbAsk (the clarify / heavy-calc ask bridge) ───────────────────────────────
class TestDbAsk:
    def test_first_ask_posts_question_and_suspends(self):
        db = FakeDB()
        notifier = RecordingNotifier()
        ask = DbAsk(db, SESSION, notifier=notifier)
        with pytest.raises(SuspendRun):
            ask("Which solvent?")
        assert db.kinds() == ["clarification"]
        assert db.status == "awaiting_input"
        assert notifier.calls == [(SESSION, "input", "Which solvent?")]

    def test_resume_returns_the_waiting_answer(self):
        db = FakeDB()
        db.add_assistant_message(SESSION, "Which solvent?", kind="clarification", state="CLARIFY")
        db.add_user("use water")
        ask = DbAsk(db, SESSION)
        assert ask("Which solvent?") == "use water"
        assert db.status == "running"  # reset once the answer is consumed

    def test_outstanding_question_is_not_reposted(self):
        db = FakeDB()
        db.add_assistant_message(SESSION, "Which solvent?", kind="clarification", state="CLARIFY")
        ask = DbAsk(db, SESSION)
        with pytest.raises(SuspendRun):
            ask("Which solvent?")
        assert len(db.messages) == 1  # no duplicate question
        assert db.status == "awaiting_input"

    def test_consumed_answer_is_not_reused_for_a_new_round(self):
        db = FakeDB()
        db.add_assistant_message(SESSION, "Q1", kind="clarification", state="CLARIFY")
        db.add_user("A1")
        ask = DbAsk(db, SESSION)
        assert ask("Q1") == "A1"
        with pytest.raises(SuspendRun):  # a second round posts a fresh question
            ask("Q2")
        assert [m["content"] for m in db.messages if m["kind"] == "clarification"] == ["Q1", "Q2"]

    def test_stale_answer_ignored_once_a_newer_question_exists(self):
        # CLARIFY was answered, then a newer (approval) question was posted: the
        # old clarify answer must NOT be handed to a later heavy-calc ask.
        db = FakeDB()
        db.add_assistant_message(SESSION, "Q1", kind="clarification", state="CLARIFY")
        db.add_user("A1")
        db.add_assistant_message(SESSION, "{}", kind="approval_request", state="PLAN")
        ask = DbAsk(db, SESSION)
        with pytest.raises(SuspendRun):
            ask("Run the heavy calc? [y/N]")
        heavy_qs = [m for m in db.messages if m["kind"] == "clarification" and "heavy" in m["content"]]
        assert len(heavy_qs) == 1  # a fresh question was posted, not the stale answer returned


# ── plan-approval gate ────────────────────────────────────────────────────────
class TestApprovalGate:
    def test_post_then_consume(self):
        db = FakeDB()
        notifier = RecordingNotifier()
        post_plan_for_approval(db, SESSION, {"cost": 1.0}, notifier=notifier)
        assert db.status == "awaiting_approval"
        assert db.kinds() == ["approval_request"]
        assert notifier.calls and notifier.calls[0][1] == "approval"
        assert consume_approval(db, SESSION) is None  # no decision yet
        db.add_user("approve", kind="approval_response")
        assert consume_approval(db, SESSION) == "approve"

    def test_post_is_idempotent(self):
        db = FakeDB()
        post_plan_for_approval(db, SESSION, {"cost": 1.0})
        post_plan_for_approval(db, SESSION, {"cost": 1.0})  # redundant resume
        assert db.kinds().count("approval_request") == 1

    def test_decision_normalized(self):
        db = FakeDB()
        post_plan_for_approval(db, SESSION, {"cost": 1.0})
        db.add_user("APPROVE", kind="approval_response")
        assert consume_approval(db, SESSION) == "approve"


# ── process_job / the drive loop ──────────────────────────────────────────────
class TestProcessJob:
    def _job(self, kind="start"):
        return {"session_id": SESSION, "kind": kind, "params": {}}

    def test_clarify_suspends_and_releases(self):
        db = FakeDB()
        runner.process_job(self._job(), db, FakeEngine(clarifies=True))
        assert "clarification" in db.kinds()
        assert "approval_request" not in db.kinds()
        assert db.status == "awaiting_input"
        assert not any(e["event_type"] == "run.completed" for e in db.events)

    def test_approval_gate_posts_plan_and_releases(self):
        db = FakeDB()
        runner.process_job(self._job(), db, FakeEngine())
        assert "approval_request" in db.kinds()
        assert db.status == "awaiting_approval"
        assert not any(e["event_type"] == "run.completed" for e in db.events)

    def test_approved_run_completes(self):
        # Decision already recorded (e.g. arrived before the runner reached BUILD,
        # or this is the resume that carries it): the run crosses the gate + finishes.
        db = FakeDB()
        db.add_assistant_message(SESSION, "{}", kind="approval_request", state="PLAN")
        db.add_user("approve", kind="approval_response")
        runner.process_job(self._job(), db, FakeEngine())
        assert db.status == "completed"
        assert any(e["event_type"] == "run.completed" for e in db.events)
        assert db.messages[-1]["content"] == "Run complete."

    def test_rejected_run_stops_before_build(self):
        db = FakeDB()
        db.add_assistant_message(SESSION, "{}", kind="approval_request", state="PLAN")
        db.add_user("reject", kind="approval_response")
        runner.process_job(self._job(), db, FakeEngine())
        assert db.status == "rejected"
        assert not any(e["event_type"] == "run.completed" for e in db.events)
        assert "rejected" in db.messages[-1]["content"].lower()

    def test_auto_run_skips_approval_gate(self, monkeypatch):
        monkeypatch.setenv("TWAIN_AUTO_RUN", "1")
        db = FakeDB()
        runner.process_job(self._job(), db, FakeEngine())
        assert "approval_request" not in db.kinds()
        assert db.status == "completed"
        assert any(e["event_type"] == "run.completed" for e in db.events)

    def test_resume_kind_is_supported(self):
        db = FakeDB()
        db.add_assistant_message(SESSION, "{}", kind="approval_request", state="PLAN")
        db.add_user("approve", kind="approval_response")
        runner.process_job(self._job(kind="resume"), db, FakeEngine())
        assert db.status == "completed"

    def test_unsupported_kind_raises(self):
        db = FakeDB()
        with pytest.raises(NotImplementedError):
            runner.process_job(self._job(kind="rerun"), db, FakeEngine())


# ── run_loop ──────────────────────────────────────────────────────────────────
class _StopLoop(Exception):
    pass


class TestRunLoop:
    def test_processes_one_job_then_marks_done(self):
        db = FakeDB(jobs=[{"id": 7, "session_id": SESSION, "kind": "start", "params": {}}])
        runner.run_loop(once=True, db=db, engine_factory=FakeEngine, sleep=lambda _s: None)
        assert (7, "running") in db.jobs_done
        assert (7, "done") in db.jobs_done

    def test_resume_job_is_processed(self):
        db = FakeDB(jobs=[{"id": 8, "session_id": SESSION, "kind": "resume", "params": {}}])
        db.add_assistant_message(SESSION, "{}", kind="approval_request", state="PLAN")
        db.add_user("approve", kind="approval_response")
        runner.run_loop(once=True, db=db, engine_factory=FakeEngine, sleep=lambda _s: None)
        assert (8, "done") in db.jobs_done

    def test_failing_job_is_marked_error(self):
        class Boom(FakeEngine):
            def build_orchestrator(self, **_kwargs):
                raise RuntimeError("kaboom")

        db = FakeDB(jobs=[{"id": 9, "session_id": SESSION, "kind": "start", "params": {}}])
        # max_attempts=1 → the first failure is terminal (no retry) and dead-letters.
        runner.run_loop(
            once=True, db=db, engine_factory=Boom, sleep=lambda _s: None, max_attempts=1
        )
        assert (9, "error") in db.jobs_done
        assert db.status == "error"

    def test_idle_loop_waits_on_the_waiter_then_processes(self):
        # No jobs initially: the loop must block on the waiter (not sleep-spin),
        # wake when one is queued, process it, then wait again.
        db = FakeDB()
        job = {"id": 11, "session_id": SESSION, "kind": "start", "params": {}}

        class WakeThenStop:
            def __init__(self):
                self.calls = 0

            def wait(self, timeout):
                self.calls += 1
                if self.calls == 1:
                    db._jobs.append(job)
                    return True
                raise _StopLoop()

        waiter = WakeThenStop()

        def boom_sleep(_s):
            raise AssertionError("idle loop must use the waiter, not sleep")

        with pytest.raises(_StopLoop):
            runner.run_loop(db=db, engine_factory=FakeEngine, sleep=boom_sleep, waiter=waiter)
        assert (11, "done") in db.jobs_done
        assert waiter.calls == 2

    def test_transient_failure_is_requeued_under_max_attempts(self):
        # A failing job with attempts below the cap is re-queued (retried), not
        # dead-lettered — a transient blip must not kill the run.
        class Boom(FakeEngine):
            def build_orchestrator(self, **_kwargs):
                raise RuntimeError("blip")

        db = FakeDB(jobs=[{"id": 3, "session_id": SESSION, "kind": "start", "params": {}}])
        runner.run_loop(
            once=True, db=db, engine_factory=Boom, sleep=lambda _s: None, max_attempts=3
        )
        assert (3, "queued") in db.jobs_done  # re-queued for another attempt
        assert (3, "error") not in db.jobs_done
        assert db.status != "error"

    def test_reaper_dead_letters_orphaned_job(self):
        # A job whose runner died and whose attempts are exhausted is surfaced to
        # the user (conversation errored) instead of being left silently wedged.
        db = FakeDB()
        db._reap_batches = [[{"id": 5, "session_id": SESSION, "attempts": 3}]]
        runner.run_loop(once=True, db=db, engine_factory=FakeEngine, sleep=lambda _s: None)
        assert db.status == "error"
        assert any(
            "recover" in m["content"].lower()
            for m in db.messages
            if m["role"] == "assistant"
        )


# ── heartbeat ─────────────────────────────────────────────────────────────────
class TestHeartbeat:
    def test_ticks_until_stopped(self):
        seen = []
        beat = threading.Event()

        class HbDB:
            def heartbeat_job(self, job_id):
                seen.append(job_id)
                beat.set()

        hb = runner._Heartbeat(HbDB(), 42, interval=0.01)
        hb.start()
        assert beat.wait(2.0)  # a beat lands promptly
        hb.stop()
        assert 42 in seen
        assert not hb._thread.is_alive()  # stop() joined the thread

    def test_disabled_when_interval_not_positive(self):
        seen = []

        class HbDB:
            def heartbeat_job(self, job_id):
                seen.append(job_id)

        hb = runner._Heartbeat(HbDB(), 1, interval=0)
        hb.start()
        hb.stop()
        assert seen == []  # never started, so never beats


# ── PgEventSink ───────────────────────────────────────────────────────────────
class TestBridges:
    def test_event_sink_persists_and_mirrors_state(self):
        db = FakeDB()
        sink = PgEventSink(db, SESSION)
        sink.publish(_event("stage.completed", {"from": "DISCOVER", "to": "PLAN"}))
        sink.publish(_event("run.completed", {"state": "TERMINATE"}))
        assert db.state == "PLAN"
        assert db.status == "completed"
        assert [e["event_type"] for e in db.events] == ["stage.completed", "run.completed"]

    def test_suspend_event_leaves_awaiting_status(self):
        db = FakeDB()
        db.set_conversation_status(SESSION, "awaiting_input")
        sink = PgEventSink(db, SESSION)
        sink.publish(_event("run.suspended", {"state": "CLARIFY", "reason": "input"}))
        assert db.status == "awaiting_input"  # not flipped back to running
        assert db.events[-1]["event_type"] == "run.suspended"


class TestPgStore:
    def test_save_and_get_roundtrip(self):
        store = PgStore(FakeDB())
        store.save_session({"session_id": "s1", "status": "running", "researcher_id": "u"})
        assert store.get_session("s1")["status"] == "running"

    def test_save_requires_session_id(self):
        store = PgStore(FakeDB())
        with pytest.raises(ValueError):
            store.save_session({"status": "running"})

    def test_resume_only_when_resumable(self):
        store = PgStore(FakeDB())
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


class TestRehydrateArtifacts:
    """Inverse of capture: restore a run's files from the DB before a resume."""

    def _orch(self, artifacts):
        return types.SimpleNamespace(
            sm=types.SimpleNamespace(context=types.SimpleNamespace(artifacts=artifacts))
        )

    def test_round_trip_restores_missing_files(self, tmp_path):
        # Capture a plan + a run_bundle dir, wipe the local files (fresh box),
        # then rehydrate and confirm every file is back with its content.
        plan = tmp_path / "execution_plan_x.json"
        plan.write_text('{"cost": 1}')
        bundle = tmp_path / "run_bundle_x"
        bundle.mkdir()
        (bundle / "main.py").write_text("import pymatgen\n")
        (bundle / "requirements.txt").write_text("pymatgen\n")
        orch = self._orch({
            "execution_plan": str(plan),
            "run_bundle": str(bundle),
            "script": str(bundle / "main.py"),  # duplicate of the bundle entrypoint
        })
        db = FakeDB()
        capture_artifacts(db, "s1", orch)

        (bundle / "main.py").unlink()
        (bundle / "requirements.txt").unlink()
        bundle.rmdir()
        plan.unlink()

        restored = rehydrate_artifacts(db, "s1", orch)
        assert restored == 3  # plan + 2 bundle files; "script" skipped
        assert plan.read_text() == '{"cost": 1}'
        assert (bundle / "main.py").read_text() == "import pymatgen\n"
        assert (bundle / "requirements.txt").read_text() == "pymatgen\n"

    def test_write_if_missing_does_not_clobber_local_files(self, tmp_path):
        plan = tmp_path / "execution_plan_x.json"
        plan.write_text("LOCAL")  # already present (same-box resume)
        db = FakeDB()
        db.upsert_artifact("s1", "execution_plan", "FROM_DB", "json")
        restored = rehydrate_artifacts(db, "s1", self._orch({"execution_plan": str(plan)}))
        assert restored == 0
        assert plan.read_text() == "LOCAL"

    def test_noop_when_nothing_stored(self, tmp_path):
        plan = tmp_path / "execution_plan_x.json"
        orch = self._orch({"execution_plan": str(plan)})
        assert rehydrate_artifacts(FakeDB(), "s1", orch) == 0
        assert not plan.exists()
