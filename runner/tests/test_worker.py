"""The SQS worker, the cluster monitor, and their SQL (P2, #171).

Unit tests use fakes; the integration class runs RunnerDB's new queries against
a scratch Postgres with every migration applied (skipped when none is reachable,
e.g. in CI).
"""
import importlib.util
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from runner import dispatch, worker
from runner.monitor import ClusterMonitor

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def sqs_env(monkeypatch):
    monkeypatch.setenv("TWAIN_DISPATCH", "sqs")
    monkeypatch.setenv("TWAIN_JOB_QUEUE_URL", "https://sqs.example/q.fifo")


class FakeSQS:
    def __init__(self):
        self.deleted, self.sent, self.visibility = [], [], []

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)

    def send_message(self, QueueUrl, **kw):
        self.sent.append(json.loads(kw["MessageBody"]) | {"group": kw["MessageGroupId"]})

    def change_message_visibility(self, **kw):
        self.visibility.append(kw)


class FakeDB:
    def __init__(self, claimable=None, status=None):
        self.claimable, self.status, self.marks, self.requeued = claimable, status, [], []

    def claim_job_by_id(self, job_id):
        return self.claimable

    def job_status(self, job_id):
        return self.status

    def mark_job(self, job_id, status):
        self.marks.append((job_id, status))

    def requeue_job(self, job_id):
        self.requeued.append(job_id)

    def heartbeat_job(self, job_id):
        pass

    def set_conversation_status(self, *a):
        pass

    def add_assistant_message(self, *a, **k):
        pass


def _msg(job_id=7):
    return {"ReceiptHandle": f"rh-{job_id}", "Body": json.dumps({"job_id": job_id})}


class TestWorkerMessages:
    def test_malformed_is_dropped(self):
        sqs = FakeSQS()
        assert worker.handle_message(FakeDB(), sqs, "q", {"ReceiptHandle": "x", "Body": "{"}) == "malformed"
        assert sqs.deleted == ["x"]

    @pytest.mark.parametrize("status", ["done", "error", "claimed", None])
    def test_a_job_that_is_not_dispatching_is_acknowledged(self, status):
        sqs = FakeSQS()
        assert worker.handle_message(FakeDB(status=status), sqs, "q", _msg()).startswith("skipped")
        assert sqs.deleted == ["rh-7"]

    def test_a_job_blocked_by_its_runs_other_job_is_left_to_reappear(self):
        sqs = FakeSQS()
        assert worker.handle_message(FakeDB(status="dispatching"), sqs, "q", _msg()) == "deferred"
        assert sqs.deleted == []

    def test_a_claimed_job_is_driven_marked_done_and_acknowledged(self, monkeypatch):
        ran = []
        monkeypatch.setattr(worker, "process_job", lambda job, db, engine=None: ran.append(job["id"]))
        db, sqs = FakeDB(claimable={"id": 7, "session_id": "s", "kind": "start", "attempts": 1}), FakeSQS()
        assert worker.handle_message(db, sqs, "q", _msg(), engine_factory=lambda: None,
                                     heartbeat_seconds=0) == "done"
        assert ran == [7] and db.marks == [(7, "running"), (7, "done")] and sqs.deleted == ["rh-7"]

    def test_a_failing_job_is_requeued_for_the_relay_and_this_message_dropped(self, monkeypatch):
        def boom(job, db, engine=None):
            raise RuntimeError("LLM gateway 503")
        monkeypatch.setattr(worker, "process_job", boom)
        db, sqs = FakeDB(claimable={"id": 7, "session_id": "s", "kind": "start", "attempts": 1}), FakeSQS()
        out = worker.handle_message(db, sqs, "q", _msg(), engine_factory=lambda: None,
                                    heartbeat_seconds=0, max_attempts=3)
        assert out.startswith("failed") and db.requeued == [7] and sqs.deleted == ["rh-7"]


def test_api_and_runner_send_the_same_message():
    spec = importlib.util.spec_from_file_location("api_dispatch", REPO / "api" / "dispatch.py")
    api_dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api_dispatch)
    assert api_dispatch.message(41, "run-a", "resume") == dispatch.message(41, "run-a", "resume")


# ── monitor ──────────────────────────────────────────────────────────────────

class MonitorDB:
    def __init__(self, open_jobs, unpublished=()):
        self.open_jobs, self.unpublished = list(open_jobs), list(unpublished)
        self.events, self.polls, self.marks, self.published, self.resumes = [], [], [], [], []

    def unpublished_jobs(self, grace):
        return list(self.unpublished)

    def mark_published(self, ids):
        self.published += list(ids)

    def open_cluster_jobs(self, min_age):
        return list(self.open_jobs)

    def cluster_job(self, job_id):
        return next((r for r in self.open_jobs if r["ris_job_id"] == job_id), None)

    def insert_run_event(self, session_id, event_type, payload, seq=None):
        self.events.append((session_id, event_type, payload))

    def update_cluster_poll(self, job_id, **kw):
        self.polls.append((job_id, kw))

    def mark(self, job_id, status):
        self.marks.append((job_id, status))

    def enqueue_resume(self, session_id):
        self.resumes.append(session_id)
        return [(99, session_id, "resume")]


class _State:
    """Stands in for slurm_adapter.JobState (CI's runner env can't import it)."""
    TERMINAL = {"completed", "failed", "cancelled", "timeout"}

    def __init__(self, slurm_state):
        self.value = slurm_state.lower()
        self.is_terminal = self.value in self.TERMINAL


class FakeAdapter:
    def __init__(self, states):
        self.states, self.last_detail = states, {}

    def poll(self, job_id):
        state, extra = self.states[job_id]
        self.last_detail[job_id] = {"state": state, **extra}
        return _State(state)

    def stdout_page(self, job_id, offset, limit):
        return {"content": "step 1\n" if offset == 0 else "", "next_offset": 7, "size": 7}


def _row(job_id, session="s1"):
    return {"ris_job_id": job_id, "session_id": session, "attempt": 1, "status": "submitted",
            "log_offset": 0}


def test_monitor_publishes_progress_for_a_queued_job_and_keeps_it_open(sqs_env):
    db = MonitorDB([_row("501")])
    mon = ClusterMonitor(db, FakeAdapter({"501": ("PENDING", {"reason": "Priority"})}),
                         sqs_client=FakeSQS())
    assert mon.tick()["finished"] == 0
    step = next(p for _s, t, p in db.events if t == "stage.progress")
    assert step["step"] == "queue" and "other jobs are ahead" in step["label"]
    assert db.polls[0][1]["slurm_state"] == "PENDING" and db.marks == []


def test_monitor_resumes_a_run_when_its_job_finishes(sqs_env):
    db, sqs = MonitorDB([_row("501", "run-7")]), FakeSQS()
    mon = ClusterMonitor(db, FakeAdapter({"501": ("COMPLETED", {"nodes": "c2-node-006"})}), sqs_client=sqs)
    assert mon.tick()["finished"] == 1
    assert db.marks == [("501", "finished")] and db.resumes == ["run-7"]
    assert sqs.sent == [{"job_id": 99, "session_id": "run-7", "kind": "resume", "group": "run-7"}]
    assert 99 in db.published
    assert any(t == "job.log" and p["text"] == "step 1\n" for _s, t, p in db.events)


def test_monitor_relays_outbox_rows_whose_send_failed(sqs_env):
    db, sqs = MonitorDB([], unpublished=[(5, "s", "start")]), FakeSQS()
    assert ClusterMonitor(db, FakeAdapter({}), sqs_client=sqs).tick()["relayed"] == 1
    assert sqs.sent[0]["job_id"] == 5 and db.published == [5]


def test_one_bad_job_does_not_stop_the_others(sqs_env):
    class Flaky(FakeAdapter):
        def poll(self, job_id):
            if job_id == "bad":
                raise RuntimeError("RIS API 502")
            return super().poll(job_id)
    db = MonitorDB([_row("bad"), _row("501")])
    stats = ClusterMonitor(db, Flaky({"501": ("RUNNING", {"nodes": "n1"})}), sqs_client=FakeSQS()).tick()
    assert stats["observed"] == 1 and db.polls[0][0] == "501"


# ── RunnerDB SQL against a real Postgres ─────────────────────────────────────

def _pg_available():
    return subprocess.run(["psql", "-h", "localhost", "-d", "postgres", "-Atc", "select 1"],
                          capture_output=True, env={**os.environ, "PGGSSENCMODE": "disable",
                                                    "PGCONNECT_TIMEOUT": "3"}).returncode == 0


@pytest.mark.skipif(not _pg_available(), reason="no local Postgres")
class TestRunnerDBSql:
    @pytest.fixture
    def db(self, monkeypatch):
        name = f"twain_worker_test_{uuid.uuid4().hex[:8]}"
        env = {**os.environ, "PGGSSENCMODE": "disable"}
        subprocess.run(["createdb", "-h", "localhost", name], check=True, env=env)
        for f in sorted((REPO / "api" / "migrations").glob("*.sql")):
            subprocess.run(["psql", "-h", "localhost", "-d", name, "-q", "-v", "ON_ERROR_STOP=1",
                            "-f", str(f)], check=True, capture_output=True, env=env)
        from runner import db as runner_db
        monkeypatch.setattr(runner_db, "DB_HOST", "localhost")
        monkeypatch.setattr(runner_db, "DB_NAME", name)
        monkeypatch.setattr(runner_db, "DB_USER", os.environ.get("USER", "postgres"))
        monkeypatch.setenv("DB_PASSWORD", "")
        monkeypatch.delenv("AWS_SECRET_ARN", raising=False)
        monkeypatch.setenv("TWAIN_DISPATCH", "sqs")
        yield runner_db.RunnerDB()
        subprocess.run(["dropdb", "-h", "localhost", name], env=env)

    def _insert(self, db, status="dispatching", session="s1", kind="start"):
        return db._query_one("INSERT INTO jobs (session_id, kind, status) VALUES (%s, %s, %s) "
                             "RETURNING id;", (session, kind, status), commit=True)["id"]

    def test_claim_by_id_is_idempotent_and_ignores_queued_rows(self, db):
        job = self._insert(db)
        assert db.claim_job_by_id(job)["kind"] == "start"
        assert db.claim_job_by_id(job) is None                  # a redelivery
        assert db.job_status(job) == "claimed"
        assert db.claim_job_by_id(self._insert(db, status="queued", session="s2")) is None

    def test_one_job_per_run_at_a_time(self, db):
        first, second = self._insert(db), self._insert(db, kind="resume")
        assert db.claim_job_by_id(first) is not None
        assert db.claim_job_by_id(second) is None and db.job_status(second) == "dispatching"

    def test_enqueue_resume_dedupes_and_the_outbox_relays(self, db):
        assert db.enqueue_resume("s9")[0][1:] == ("s9", "resume")
        assert db.enqueue_resume("s9") == []                    # one pending resume per run
        db._execute("UPDATE jobs SET created_at = now() - interval '1 hour';", ())
        pending = db.unpublished_jobs(30)
        assert [p[1:] for p in pending] == [("s9", "resume")]
        db.mark_published([pending[0][0]])
        assert db.unpublished_jobs(30) == []

    def test_requeue_goes_back_to_dispatching_not_queued(self, db):
        job = self._insert(db)
        db.claim_job_by_id(job)
        db.requeue_job(job)
        assert db.job_status(job) == "dispatching"              # the polling runner can't see it

    def test_a_polling_runner_returns_an_sqs_job_to_sqs(self, db, monkeypatch):
        # The login-node runner (TWAIN_DISPATCH=db) reaping a crashed worker's job
        # must not take it: back to 'dispatching', for the relay to re-send.
        job = self._insert(db)
        db.mark_published([job])
        db.claim_job_by_id(job)
        db._execute("UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = %s;", (job,))
        monkeypatch.setenv("TWAIN_DISPATCH", "db")
        db.reap_stale_jobs(lease_seconds=60, max_attempts=5)
        assert db.job_status(job) == "dispatching"
        assert db.unpublished_jobs(0) and db.unpublished_jobs(0)[0][0] == job
        # ...while its own jobs still go back to 'queued'.
        own = self._insert(db, status="queued", session="s-own")
        db.claim_job()
        db._execute("UPDATE jobs SET heartbeat_at = now() - interval '1 hour' WHERE id = %s;", (own,))
        db.reap_stale_jobs(lease_seconds=60, max_attempts=5)
        assert db.job_status(own) == "queued"

    def test_cluster_job_store(self, db):
        db.record_submitted("s1", 1, "501", "runs/s1/attempt-1", {"job_name": "twain-s1"})
        db.record_submitted("s1", 2, "502", "runs/s1/attempt-2", {})
        assert db.latest("s1")["ris_job_id"] == "502"
        assert {r["ris_job_id"] for r in db.open_cluster_jobs(30)} == {"501", "502"}
        db.update_cluster_poll("501", slurm_state="RUNNING", node="n1", reason=None, log_offset=7)
        assert {r["ris_job_id"] for r in db.open_cluster_jobs(30)} == {"502"}   # just polled
        db.mark("501", "finished")
        assert db.cluster_job("501")["finished_at"] is not None
        db.mark("502", "collected")
        assert db.latest("s1")["status"] == "collected"
