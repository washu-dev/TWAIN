"""Unit tests for the runner — fakes stand in for the DB and the engine, so no
Postgres or pixi environment is needed. These exercise the job loop, the
approval gate, and the chat/event bridges end to end in-process.
"""
import json
import types
from pathlib import Path

from runner import runner
from runner.artifacts import capture_artifacts, rematerialize_inputs
from runner.bridges import DbAsk, PgEventSink, RunCancelled, request_plan_approval
from runner.pg_store import PgStore

SESSION = "conv-1"


class FakeDB:
    """In-memory stand-in for RunnerDB."""

    def __init__(self, approval="approve", clarify="25 degrees C", jobs=None,
                 terminate=False):
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
        self.terminate = terminate

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

    def terminate_requested(self, sid):
        return self.terminate

    def set_conversation_state(self, sid, state):
        self.state = state

    def insert_run_event(self, sid, event_type, payload, seq=None):
        self.events.append({"event_type": event_type, "payload": payload})

    def upsert_artifact(self, sid, name, content, kind):
        self.artifacts.append({"name": name, "content": content, "kind": kind})

    def get_artifact(self, sid, name):
        for a in self.artifacts:
            if a["name"] == name:
                return a
        return None

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

    def __init__(self, ask, sink, max_cost=1.0):
        self.ask = ask
        self.sink = sink
        self.sm = types.SimpleNamespace(
            context=types.SimpleNamespace(artifacts={}),
            current_state=types.SimpleNamespace(name="TERMINATE"),
        )
        # Mirror the real orchestrator's run_budget so the pre-flight budget
        # warning in _drive_run has a cap to compare the plan estimate against.
        self.run_budget = types.SimpleNamespace(max_cost=max_cost)
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

    def __init__(self, plan=None, *, compute_target="local", slurm_cluster="compute2"):
        self._plan = plan or {"selected_method": {"name": "demo-tool"}, "cost": 0.1}
        self.compute_targets = []  # what process_job forwarded from job params
        self._compute_target = compute_target
        self._slurm_cluster = slurm_cluster
        self.applied_overrides = []
        self.built_with = None  # records build_orchestrator kwargs for assertions
        self.rewound_to = None  # records the rewind target for rerun assertions
        self.approved = False   # set when approve_plan() is called

    def build_orchestrator(
        self, *, session_id, researcher_id, request, ask, sink, store,
        compute_target=None, cancel=None, max_cost=None,
    ):
        self.compute_targets.append(compute_target)
        if compute_target is not None:
            self._compute_target = compute_target
        self.built_with = {
            "session_id": session_id, "researcher_id": researcher_id,
            "request": request, "max_cost": max_cost,
        }
        return FakeOrchestrator(ask, sink, max_cost=max_cost if max_cost is not None else 1.0)

    def rewind(self, orch, target_state):
        # A real rewind resets the run to `target_state`; the fake just records it
        # (the fresh FakeOrchestrator already starts at leg 0, i.e. the top).
        self.rewound_to = target_state

    def approve_plan(self, orch):
        # A real approve_plan flips the plan_approved guard flag; the fake records
        # that it happened so a test can assert the run was actually approved.
        self.approved = True

    def compute_target_of(self, orch):
        return self._compute_target

    def slurm_cluster_of(self, orch):
        return self._slurm_cluster if self._compute_target == "slurm" else None

    def apply_slurm_overrides(self, orch, overrides):
        self.applied_overrides.append(overrides)

    def read_execution_plan(self, orch):
        return self._plan

    def final_summary(self, orch):
        return "Run complete."


class TestProcessJob:
    def test_approved_run_completes(self):
        db = FakeDB(approval="approve")
        engine = FakeEngine()
        runner.process_job({"session_id": SESSION, "kind": "start", "params": {}}, db, engine)
        kinds = [m["kind"] for m in db.messages]
        assert "clarification" in kinds
        assert "approval_request" in kinds
        assert engine.approved is True          # plan_approved guard flag was set
        assert db.status == "completed"
        assert any(e["event_type"] == "run.completed" for e in db.events)
        assert db.messages[-1]["content"] == "Run complete."

    def test_rejected_run_stops_before_build(self):
        db = FakeDB(approval="reject")
        engine = FakeEngine()
        runner.process_job({"session_id": SESSION, "kind": "start", "params": {}}, db, engine)
        assert db.status == "rejected"
        assert engine.approved is False         # never approved -> guard stays closed
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

    def test_compute_target_forwarded_from_job_params(self):
        db = FakeDB(approval="approve")
        engine = FakeEngine()
        runner.process_job(
            {"session_id": SESSION, "kind": "start",
             "params": {"compute_target": "slurm"}},
            db, engine,
        )
        assert engine.compute_targets == ["slurm"]
        assert any("RIS cluster" in m["content"] for m in db.messages)

    def test_slurm_overrides_applied_on_approve(self):
        payload = json.dumps({
            "decision": "approve",
            "slurm_request": {"cpu_count": 16, "ram": 32, "max_time": 1.0, "gpu_count": 0},
        })
        db = FakeDB(approval=payload)
        engine = FakeEngine(compute_target="slurm")
        runner.process_job(
            {"session_id": SESSION, "kind": "start",
             "params": {"compute_target": "slurm"}},
            db, engine,
        )
        assert engine.applied_overrides == [
            {"cpu_count": 16, "ram": 32, "max_time": 1.0, "gpu_count": 0}
        ]
        assert any("updated Slurm settings" in m["content"] for m in db.messages)

    def test_terminate_during_approval_wait_cancels_run(self):
        # The user pressed Terminate while the run sat at the approval gate:
        # the wait must abort, the conversation settle as 'cancelled' (not
        # 'error'), and the job finish normally.
        class TerminatedDB(FakeDB):
            def user_replies_after(self, sid, after_id, kind=None):
                if kind == "approval_response":
                    # Instead of answering the approval card, the user presses
                    # Terminate; the next cancel check aborts the wait.
                    self.terminate = True
                    return []
                return super().user_replies_after(sid, after_id, kind)

        db = TerminatedDB()
        runner.process_job({"session_id": SESSION, "kind": "start", "params": {}}, db, FakeEngine())
        assert db.status == "cancelled"
        assert "terminated by user" in db.messages[-1]["content"].lower()
        # leg 2 never ran
        assert not any(e["event_type"] == "run.completed" for e in db.events)

    def test_unsupported_kind_raises(self):
        db = FakeDB()
        try:
            runner.process_job({"session_id": SESSION, "kind": "resume", "params": {}}, db, FakeEngine())
            raise AssertionError("expected NotImplementedError")
        except NotImplementedError:
            pass

    def test_rerun_rewinds_then_drives_the_run(self):
        # A 'rerun' job rewinds the run to the requested stage, posts a marker
        # message, and drives it forward again through the approval gate.
        db = FakeDB(approval="approve")
        engine = FakeEngine()
        runner.process_job(
            {"session_id": SESSION, "kind": "rerun",
             "params": {"target_state": "CLARIFY", "researcher_id": "u", "request": "r"}},
            db, engine,
        )
        assert engine.rewound_to == "CLARIFY"
        assert any("CLARIFY" in m["content"] for m in db.messages)  # marker message
        assert "approval_request" in [m["kind"] for m in db.messages]
        assert db.status == "completed"

    def test_rerun_requires_target_state(self):
        db = FakeDB()
        try:
            runner.process_job({"session_id": SESSION, "kind": "rerun", "params": {}}, db, FakeEngine())
            raise AssertionError("expected ValueError")
        except ValueError:
            pass

    def test_max_cost_forwarded_from_params(self):
        # A per-run budget in the job params must reach build_orchestrator so the
        # orchestrator caps this run's spend (rather than the deployment default).
        db = FakeDB(approval="approve")
        engine = FakeEngine()
        runner.process_job(
            {"session_id": SESSION, "kind": "start",
             "params": {"request": "r", "researcher_id": "u", "max_cost": 2.5}},
            db, engine,
        )
        assert engine.built_with["max_cost"] == 2.5

    def test_no_max_cost_forwards_none(self):
        # Absent from params => None, so the engine applies the deployment default.
        db = FakeDB(approval="approve")
        engine = FakeEngine()
        runner.process_job({"session_id": SESSION, "kind": "start", "params": {}}, db, engine)
        assert engine.built_with["max_cost"] is None

    def test_pre_flight_warns_when_estimate_over_budget(self):
        # Plan estimate ($5) above the run budget ($1) => a warn-only heads-up
        # posted before the approval gate (the run is not blocked).
        db = FakeDB(approval="approve")
        engine = FakeEngine(plan={"cost_estimate": {"min_cost": 5.0}})
        runner.process_job(
            {"session_id": SESSION, "kind": "start", "params": {"max_cost": 1.0}}, db, engine,
        )
        assert any("budget" in m["content"].lower() for m in db.messages)
        assert db.status == "completed"  # warned, but still ran to completion

    def test_pre_flight_silent_when_estimate_within_budget(self):
        db = FakeDB(approval="approve")
        engine = FakeEngine(plan={"cost_estimate": {"min_cost": 0.5}})
        runner.process_job(
            {"session_id": SESSION, "kind": "start", "params": {"max_cost": 1.0}}, db, engine,
        )
        assert not any("heads up" in m["content"].lower() for m in db.messages)


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
        decision, overrides = request_plan_approval(
            db, SESSION, {"cost_estimate": {"min_cost": 1.0}}, sleep=lambda _s: None
        )
        assert decision == "approve"  # normalized
        assert overrides is None
        assert db.messages[0]["kind"] == "approval_request"
        summary = json.loads(db.messages[0]["content"])
        assert summary["compute_target"] == "local"
        assert "slurm_request" in summary

    def test_request_plan_approval_parses_slurm_overrides(self):
        payload = json.dumps({
            "decision": "approve",
            "slurm_request": {"cpu_count": 16, "ram": 32, "max_time": 1.0, "gpu_count": 0},
        })
        db = FakeDB(approval=payload)
        decision, overrides = request_plan_approval(
            db, SESSION, {"slurm_request": {"ram": 16}},
            compute_target="slurm", slurm_cluster="compute2",
            sleep=lambda _s: None,
        )
        assert decision == "approve"
        assert overrides["ram"] == 32
        summary = json.loads(db.messages[0]["content"])
        assert summary["compute_target"] == "slurm"
        assert summary["slurm_cluster"] == "compute2"

    def test_wait_aborts_with_run_cancelled_when_terminate_requested(self):
        db = FakeDB(terminate=True)
        db.user_replies_after = lambda sid, after_id, kind=None: []
        ask = DbAsk(db, SESSION, sleep=lambda _s: None,
                    cancel=lambda: db.terminate_requested(SESSION))
        try:
            ask("Which solvent?")
            raise AssertionError("expected RunCancelled")
        except RunCancelled:
            pass

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

    def test_nul_bytes_stripped_and_bad_upsert_does_not_abort(self, tmp_path):
        # Binary outputs (e.g. a fetched .gpw file) carry NUL bytes, which
        # Postgres TEXT rejects; they must be stripped and one failing upsert
        # must not lose the remaining artifacts (like execution_result).
        bundle = tmp_path / "run_bundle_x"
        bundle.mkdir()
        (bundle / "aaa.gpw").write_bytes(b"BIN\x00ARY\x00")
        (bundle / "main.py").write_text("print('hi')")
        result = tmp_path / "execution_result_x.json"
        result.write_text('{"status": "success"}')
        orch = types.SimpleNamespace(
            sm=types.SimpleNamespace(
                context=types.SimpleNamespace(
                    artifacts={"run_bundle": str(bundle), "execution_result": str(result)}
                )
            )
        )

        class PickyDB(FakeDB):
            def upsert_artifact(self, sid, name, content, kind):
                assert "\x00" not in content  # sanitized before the DB sees it
                if name.endswith("main.py"):
                    raise RuntimeError("simulated db failure")
                super().upsert_artifact(sid, name, content, kind)

        db = PickyDB()
        count = capture_artifacts(db, "s1", orch)
        names = {a["name"] for a in db.artifacts}
        assert count == 2  # gpw (sanitized) + execution_result; main.py skipped
        assert "execution_result" in names
        assert "run_bundle/aaa.gpw" in names


class TestRematerializeInputs:
    def test_restores_surviving_upstream_specs_to_disk(self, tmp_path):
        # A re-run runs in a fresh process: the original artifact files are gone,
        # so the surviving upstream specs must be rewritten to disk from the DB and
        # context.artifacts repointed at the fresh paths.
        db = FakeDB()
        db.upsert_artifact("s1", "intent_spec", '{"objective": "x"}', "json")
        orch = types.SimpleNamespace(
            sm=types.SimpleNamespace(
                artifacts_dir=str(tmp_path),
                context=types.SimpleNamespace(
                    artifacts={"intent_spec": "/gone/intent_spec.json"}
                ),
            )
        )
        restored = rematerialize_inputs(db, "s1", orch)
        assert restored == 1
        new_path = orch.sm.context.artifacts["intent_spec"]
        assert Path(new_path).is_file()
        assert "objective" in Path(new_path).read_text(encoding="utf-8")

    def test_skips_specs_absent_from_db(self, tmp_path):
        # A spec that was trimmed (or never captured) is left untouched.
        db = FakeDB()  # no artifacts stored
        orch = types.SimpleNamespace(
            sm=types.SimpleNamespace(
                artifacts_dir=str(tmp_path),
                context=types.SimpleNamespace(artifacts={"intent_spec": "/gone.json"}),
            )
        )
        assert rematerialize_inputs(db, "s1", orch) == 0
        assert orch.sm.context.artifacts["intent_spec"] == "/gone.json"  # unchanged
