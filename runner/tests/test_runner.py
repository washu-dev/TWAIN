"""Unit tests for the async runner — fakes stand in for the DB and the engine, so
no Postgres or pixi environment is needed. These exercise the suspend/resume
model: a run advances until it needs the user, then *releases* the process (it
never blocks polling), and a ``resume`` job picks it back up. They also cover the
``rerun`` path (rewind to an earlier stage) and the per-run budget heads-up.
"""
import json
import threading
import types
from pathlib import Path

import pytest

from runner import runner
from runner.artifacts import (
    capture_artifacts,
    rehydrate_artifacts,
    rematerialize_inputs,
)
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
        self.owner = None  # {"email", "name", "phone"} that owner_contact returns
        self._reap_batches = []  # each run_loop reap() call pops one batch of dead jobs
        self._next_id = 1
        self.terminate = False  # flip True to simulate the user pressing Terminate

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

    def preload_approval(self, decision="approve"):  # test-only helper
        """Seed an already-recorded approval decision (as if the user replied)."""
        self.add_assistant_message(SESSION, "{}", kind="approval_request", state="PLAN")
        self.add_user(decision, kind="approval_response")

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

    def mark_reply_consumed(self, message_id):
        for m in self.messages:
            if m["id"] == message_id:
                m["state"] = "consumed"

    # -- status / state / events ------------------------------------------------
    def set_conversation_status(self, sid, status):
        self.status = status

    def set_conversation_state(self, sid, state):
        self.state = state

    def owner_contact(self, sid):
        return self.owner

    def run_title(self, sid):
        return "Predict the band gap of silicon"

    def terminate_requested(self, sid):
        return self.terminate

    def insert_run_event(self, sid, event_type, payload, seq=None):
        self.events.append({"event_type": event_type, "payload": payload})

    def upsert_artifact(self, sid, name, content, kind):
        self.artifacts.append({"name": name, "content": content, "kind": kind})

    def get_artifacts(self, sid):
        return [{"name": a["name"], "content": a["content"]} for a in self.artifacts]

    def get_artifact(self, sid, name):
        for a in self.artifacts:
            if a["name"] == name:
                return a
        return None

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
    (exactly what ``Orchestrator.run`` does). ``run_budget`` mirrors the real
    orchestrator's per-run cap so the pre-flight budget warning has something to
    compare the plan estimate against.
    """

    def __init__(self, ask, sink, *, clarifies=False, heavy=False, max_cost=1.0,
                 declines=False):
        self.ask = ask
        self.sink = sink
        self.sm = types.SimpleNamespace(
            context=types.SimpleNamespace(artifacts={}),
            current_state=types.SimpleNamespace(name="INTAKE"),
        )
        self.run_budget = types.SimpleNamespace(max_cost=max_cost)
        self._clarified = not clarifies
        self._heavy = heavy
        self._heavy_done = False
        self._declines = declines

    def _set(self, name):
        self.sm.current_state = types.SimpleNamespace(name=name)

    def run(self, until=None):
        try:
            return self._run(until)
        except SuspendRun as s:
            self.sink.publish(_event("run.suspended", {"state": self.sm.current_state.name, "reason": s.reason}))
            return "paused"

    def _run(self, until):
        # An off-topic decline ends the run at INTAKE, before clarification or
        # the approval gate -- leg 1 completes immediately instead of pausing.
        if self._declines:
            self._set("TERMINATE")
            self.sink.publish(_event("run.completed", {"state": "TERMINATE"}))
            return "completed"
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

    def __init__(self, plan=None, clarifies=False, heavy=False,
                 compute_target="local", slurm_cluster="compute2", decline=None):
        self._plan = plan or {"selected_method": {"name": "demo-tool"}, "cost": 0.1}
        self._clarifies = clarifies
        self._heavy = heavy
        self._decline = decline     # off-topic decline message, or None
        self._compute_target = compute_target
        self._slurm_cluster = slurm_cluster
        self.built_with = None       # records build_orchestrator kwargs for assertions
        self.applied_overrides = []  # slurm overrides applied on approve
        self.rewound_to = None       # records the rewind target for rerun assertions
        self.approved = False        # set when approve_plan() is called
        self.replanned_with = []     # rejection feedback passed to replan_with_feedback

    def build_orchestrator(
        self, *, session_id, researcher_id, request, ask, sink, store,
        cancel=None, max_cost=None,
    ):
        self.built_with = {
            "session_id": session_id, "researcher_id": researcher_id,
            "request": request, "max_cost": max_cost,
        }
        return FakeOrchestrator(
            ask, sink, clarifies=self._clarifies, heavy=self._heavy,
            max_cost=max_cost if max_cost is not None else 1.0,
            declines=self._decline is not None,
        )

    def current_state_name(self, orch):
        return orch.sm.current_state.name

    def compute_target_of(self, orch):
        return self._compute_target

    def slurm_cluster_of(self, orch):
        return self._slurm_cluster

    def apply_slurm_overrides(self, orch, overrides):
        self.applied_overrides.append(overrides)

    def rewind(self, orch, target_state):
        # A real rewind resets the run to `target_state`; the fake just records it
        # (the fresh FakeOrchestrator already starts at leg 0, i.e. the top).
        self.rewound_to = target_state

    def replan_with_feedback(self, orch, feedback):
        # The real engine folds the feedback into the intent and rewinds to
        # DISCOVER; the fake records both so tests can assert the round-trip.
        self.replanned_with.append(feedback)
        self.rewound_to = "DISCOVER"

    def approve_plan(self, orch):
        # A real approve_plan flips the plan_approved guard flag; the fake records
        # that it happened so a test can assert the run was actually approved.
        self.approved = True

    def read_execution_plan(self, orch):
        return self._plan

    def decline_reason(self, orch):
        return self._decline

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
        assert consume_approval(db, SESSION) == (None, None)  # no decision yet
        db.add_user("approve", kind="approval_response")
        assert consume_approval(db, SESSION) == ("approve", None)

    def test_post_is_idempotent(self):
        db = FakeDB()
        post_plan_for_approval(db, SESSION, {"cost": 1.0})
        post_plan_for_approval(db, SESSION, {"cost": 1.0})  # redundant resume
        assert db.kinds().count("approval_request") == 1

    def test_decision_normalized(self):
        db = FakeDB()
        post_plan_for_approval(db, SESSION, {"cost": 1.0})
        db.add_user("APPROVE", kind="approval_response")
        assert consume_approval(db, SESSION) == ("approve", None)

    def test_slurm_card_carries_node_ceilings(self):
        # A Slurm-routed plan's approval card includes the cluster's per-node
        # maxima (from configs/clusters/compute2.json) so the editable resource
        # fields can show and enforce how far a request can go.
        from runner.bridges import _plan_summary
        summary = _plan_summary(
            {"metadata": {}}, compute_target="slurm", slurm_cluster="compute2")
        limits = summary["slurm_limits"]
        assert limits["cpu_count"] == 64
        assert limits["gpu_count"] == 4
        assert limits["ram"] == 900
        assert limits["max_time"] == 360.0  # longest partition wall, in hours

    def test_local_card_has_no_slurm_limits(self):
        from runner.bridges import _plan_summary
        summary = _plan_summary({"metadata": {}}, compute_target="local")
        assert "slurm_limits" not in summary


# ── process_job / the drive loop ──────────────────────────────────────────────
class TestProcessJob:
    def _job(self, kind="start", **params):
        return {"session_id": SESSION, "kind": kind, "params": params}

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

    def test_off_topic_run_declines_before_the_gate(self):
        # Intake refused the request (e.g. "explain bitcoin"): the decline
        # message is the final chat post, the conversation is marked rejected
        # (nothing planned or executed), and no approval card ever appears.
        db = FakeDB()
        msg = "This doesn't look like a computational chemistry request."
        runner.process_job(self._job(), db, FakeEngine(decline=msg))
        assert db.status == "rejected"
        assert db.messages[-1]["content"] == msg
        assert db.messages[-1]["kind"] == "chat"
        assert "approval_request" not in db.kinds()
        assert "clarification" not in db.kinds()

    def test_approved_run_completes(self):
        # Decision already recorded (e.g. arrived before the runner reached BUILD,
        # or this is the resume that carries it): the run crosses the gate + finishes.
        db = FakeDB()
        db.preload_approval("approve")
        engine = FakeEngine()
        runner.process_job(self._job(), db, engine)
        assert engine.approved is True          # plan_approved guard flag was set
        assert db.status == "completed"
        assert any(e["event_type"] == "run.completed" for e in db.events)
        assert db.messages[-1]["content"] == "Run complete."

    def test_reject_asks_what_to_change_instead_of_stopping(self):
        # A rejection no longer ends the run: the gate asks for revision
        # feedback and waits, with nothing built or executed.
        db = FakeDB()
        db.preload_approval("reject")
        engine = FakeEngine()
        runner.process_job(self._job(), db, engine)
        assert db.status == "awaiting_input"
        assert engine.approved is False         # never approved -> guard stays closed
        assert not any(e["event_type"] == "run.completed" for e in db.events)
        assert db.messages[-1]["kind"] == "clarification"
        assert "what should change" in db.messages[-1]["content"].lower()

    def test_auto_run_skips_approval_gate(self, monkeypatch):
        monkeypatch.setenv("TWAIN_AUTO_RUN", "1")
        db = FakeDB()
        engine = FakeEngine()
        runner.process_job(self._job(), db, engine)
        assert "approval_request" not in db.kinds()
        assert engine.approved is True          # auto-approve still sets the guard
        assert db.status == "completed"
        assert any(e["event_type"] == "run.completed" for e in db.events)

    def test_resume_kind_is_supported(self):
        db = FakeDB()
        db.preload_approval("approve")
        runner.process_job(self._job(kind="resume"), db, FakeEngine())
        assert db.status == "completed"

    def test_unsupported_kind_raises(self):
        db = FakeDB()
        with pytest.raises(NotImplementedError):
            runner.process_job(self._job(kind="frobnicate"), db, FakeEngine())

    def test_rerun_rewinds_then_drives_the_run(self):
        # A 'rerun' job rewinds the run to the requested stage, posts a marker
        # message, and drives it forward again through the approval gate.
        db = FakeDB()
        db.preload_approval("approve")
        engine = FakeEngine()
        runner.process_job(
            self._job(kind="rerun", target_state="CLARIFY", researcher_id="u", request="r"),
            db, engine,
        )
        assert engine.rewound_to == "CLARIFY"
        assert any("CLARIFY" in m["content"] for m in db.messages)  # marker message
        assert "approval_request" in [m["kind"] for m in db.messages]
        assert db.status == "completed"

    def test_rerun_requires_target_state(self):
        db = FakeDB()
        with pytest.raises(ValueError):
            runner.process_job(self._job(kind="rerun"), db, FakeEngine())

    def test_rerun_with_feedback_folds_it_in_before_replanning(self):
        # The mid-session revision path: a 'rerun' job carrying the researcher's
        # "here's what to change" folds it into the intent (replan_with_feedback,
        # same machinery as a plan rejection) and re-drives to a fresh approval
        # card, all within the same conversation.
        db = FakeDB()
        engine = FakeEngine()
        runner.process_job(
            self._job(kind="rerun", target_state="DISCOVER",
                      feedback="use xtb instead of DFT",
                      researcher_id="u", request="r"),
            db, engine,
        )
        assert engine.replanned_with == ["use xtb instead of DFT"]
        assert engine.rewound_to == "DISCOVER"
        assert any("Revising" in m["content"] for m in db.messages)  # marker
        # The run re-drove to the gate and posted a fresh plan for approval.
        assert "approval_request" in [m["kind"] for m in db.messages]
        assert db.status == "awaiting_approval"

    def test_feedback_reply_triggers_replan_and_a_fresh_card(self):
        # Reject → the gate asks what to change → the reply is folded into the
        # run (replan_with_feedback) and a NEW approval card is posted.
        db = FakeDB()
        db.preload_approval("reject")
        engine = FakeEngine()
        runner.process_job(self._job(), db, engine)          # asks what to change
        db.add_user("use xtb instead of GPAW")               # the revision feedback
        runner.process_job(self._job(kind="resume"), db, engine)
        assert engine.replanned_with == ["use xtb instead of GPAW"]
        assert engine.rewound_to == "DISCOVER"
        requests = [m for m in db.messages if m["kind"] == "approval_request"]
        assert len(requests) == 2          # a fresh card for the revised plan
        assert db.status == "awaiting_approval"

    def test_approving_the_revised_plan_completes_the_run(self):
        # Full round-trip: reject, give feedback, approve the revised plan.
        db = FakeDB()
        db.preload_approval("reject")
        engine = FakeEngine()
        runner.process_job(self._job(), db, engine)
        db.add_user("skip the geometry optimization")
        runner.process_job(self._job(kind="resume"), db, engine)
        db.add_user("approve", kind="approval_response")     # approve the new card
        runner.process_job(self._job(kind="resume"), db, engine)
        assert engine.approved is True
        assert db.status == "completed"

    def test_redundant_resume_while_awaiting_feedback_does_not_repost(self):
        # A resume that arrives before the user answers the what-should-change
        # question must not post a second question or a second plan card.
        db = FakeDB()
        db.preload_approval("reject")
        engine = FakeEngine()
        runner.process_job(self._job(), db, engine)
        runner.process_job(self._job(kind="resume"), db, engine)
        assert db.kinds().count("clarification") == 1
        assert db.kinds().count("approval_request") == 1
        assert db.status == "awaiting_input"

    def test_rerun_after_completion_posts_a_fresh_approval_card(self):
        # The regression behind "Re-run from … does nothing": the first run's
        # decision was replayed forever, so a re-run reaching BUILD crossed the
        # gate on the old answer instead of asking again. Now the consumed
        # decision is one-shot and the re-run posts a NEW approval request.
        db = FakeDB()
        db.preload_approval("approve")
        runner.process_job(self._job(), db, FakeEngine())
        assert db.status == "completed"

        engine = FakeEngine()
        runner.process_job(
            self._job(kind="rerun", target_state="PLAN", researcher_id="u", request="r"),
            db, engine,
        )
        requests = [m for m in db.messages if m["kind"] == "approval_request"]
        assert len(requests) == 2          # a fresh card, not the old one reused
        assert db.status == "awaiting_approval"
        assert engine.approved is False    # old 'approve' was not replayed as-is

    def test_max_cost_forwarded_from_params(self):
        # A per-run budget in the job params must reach build_orchestrator so the
        # orchestrator caps this run's spend (rather than the deployment default).
        db = FakeDB()
        db.preload_approval("approve")
        engine = FakeEngine()
        runner.process_job(self._job(request="r", researcher_id="u", max_cost=2.5), db, engine)
        assert engine.built_with["max_cost"] == 2.5

    def test_no_max_cost_forwards_none(self):
        # Absent from params => None, so the engine applies the deployment default.
        db = FakeDB()
        db.preload_approval("approve")
        engine = FakeEngine()
        runner.process_job(self._job(), db, engine)
        assert engine.built_with["max_cost"] is None

    def test_pre_flight_warns_when_estimate_over_budget(self):
        # Plan estimate ($5) above the run budget ($1) => a warn-only heads-up
        # posted alongside the plan at the approval gate (the run is not blocked).
        db = FakeDB()
        engine = FakeEngine(plan={"cost_estimate": {"min_cost": 5.0}})
        runner.process_job(self._job(max_cost=1.0), db, engine)
        assert any("budget" in m["content"].lower() for m in db.messages)
        assert db.status == "awaiting_approval"  # warned, plan posted, released

    def test_pre_flight_silent_when_estimate_within_budget(self):
        db = FakeDB()
        engine = FakeEngine(plan={"cost_estimate": {"min_cost": 0.5}})
        runner.process_job(self._job(max_cost=1.0), db, engine)
        assert not any("heads up" in m["content"].lower() for m in db.messages)
        assert db.status == "awaiting_approval"

    def test_completion_fires_a_notification(self):
        # A run that reaches TERMINATE notifies the owner it finished (not just a
        # suspend). Drive _drive_run directly so we can inject a recording notifier.
        db = FakeDB()
        db.preload_approval("approve")
        engine = FakeEngine()
        orch = engine.build_orchestrator(
            session_id=SESSION, researcher_id="", request="r",
            ask=DbAsk(db, SESSION), sink=PgEventSink(db, SESSION), store=None,
        )
        notes = RecordingNotifier()
        runner._drive_run(db, SESSION, orch, engine, notifier=notes)
        assert "completed" in [reason for _sid, reason, _msg in notes.calls]

    def test_failure_fires_a_notification(self):
        db = FakeDB()
        notes = RecordingNotifier()

        class FailOrch:
            def __init__(self):
                self.sm = types.SimpleNamespace(
                    context=types.SimpleNamespace(artifacts={}),
                    current_state=types.SimpleNamespace(name="INTAKE"),
                )

            def run(self, until=None):
                self.sm.current_state = types.SimpleNamespace(name="DISCOVER")
                return "error"

        runner._drive_run(db, SESSION, FailOrch(), FakeEngine(), notifier=notes)
        assert "failed" in [reason for _sid, reason, _msg in notes.calls]

    # ── Slurm / compute target ────────────────────────────────────────────────
    def test_slurm_target_announced_on_start(self):
        # A fresh start announces where it will execute (RIS vs local) up front.
        db = FakeDB()
        db.preload_approval("approve")
        engine = FakeEngine(compute_target="slurm")
        runner.process_job(self._job(), db, engine)
        assert any("RIS cluster" in m["content"] for m in db.messages)

    def test_slurm_overrides_applied_on_approve(self):
        # The approval reply carries edited Slurm resources; the gate applies them
        # (engine.apply_slurm_overrides) and says so in chat.
        overrides = {"cpu_count": 16, "ram": 32, "max_time": 1.0, "gpu_count": 0}
        db = FakeDB()
        db.add_assistant_message(SESSION, "{}", kind="approval_request", state="PLAN")
        db.add_user(
            json.dumps({"decision": "approve", "slurm_request": overrides}),
            kind="approval_response",
        )
        engine = FakeEngine(compute_target="slurm")
        runner.process_job(self._job(), db, engine)
        assert engine.applied_overrides == [overrides]
        assert any("updated Slurm settings" in m["content"] for m in db.messages)

    # ── Terminate ─────────────────────────────────────────────────────────────
    def test_terminate_during_run_settles_cancelled_not_error(self):
        # Terminate pressed: the orchestrator aborts a stage by raising; because
        # the cancel flag is set, process_job records a cancellation (not a failure)
        # and the conversation settles as 'cancelled'.
        class RaisingOrch:
            def __init__(self):
                self.sm = types.SimpleNamespace(
                    context=types.SimpleNamespace(artifacts={}),
                    current_state=types.SimpleNamespace(name="INTAKE"),
                )

            def run(self, until=None):
                raise RuntimeError("aborted between stages")

        class CancellingEngine(FakeEngine):
            def build_orchestrator(self, **kwargs):
                super().build_orchestrator(**kwargs)  # record compute_target etc.
                return RaisingOrch()

        db = FakeDB()
        db.terminate = True
        runner.process_job(self._job(), db, CancellingEngine())
        assert db.status == "cancelled"
        assert "terminated by user" in db.messages[-1]["content"].lower()
        assert not any(e["event_type"] == "run.completed" for e in db.events)


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
        db.preload_approval("approve")
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
