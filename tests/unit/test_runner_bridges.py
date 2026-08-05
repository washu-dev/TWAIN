"""Unit tests for runner.bridges.PgEventSink ordering.

The invariant under test: nothing may tell a client a run has finished before
that run's artifacts are committed. Both signals a client watches -- the
``run.completed`` / ``run.error`` row in ``run_events`` (which the SSE stream
refetches on) and the terminal ``sessions.status`` -- must come after the flush.

The bug this pins: artifact capture used to live only in the runner's finally
block, so it ran after the event and after the status flip. Measured on run
e496cf22, status 'completed' was written at 05:22:40.19 and the last artifact at
05:22:47.86 -- a 7.7s window in which the API reported a finished run with no
result. A report opened in that window showed nothing and never re-fetched, and
the completion email was sent inside it.

Run from the repo root with:  pixi run pytest tests/unit/test_runner_bridges.py
"""
import pytest

from runner.bridges import PgEventSink


class RecordingDb:
    """Records the order of the calls the sink makes."""

    def __init__(self):
        self.calls = []

    def insert_run_event(self, session_id, event_type, payload, seq=None):
        self.calls.append(("event", event_type))

    def set_conversation_status(self, session_id, status):
        self.calls.append(("status", status))

    def set_conversation_state(self, session_id, state):
        self.calls.append(("state", state))


class Event:
    def __init__(self, event_type, payload=None):
        self.event_type = event_type
        self.payload = payload or {}


def _sink(db=None, *, flush=True, boom=None):
    sink = PgEventSink(db or RecordingDb(), "sess-1")
    if flush:
        def do_flush():
            if boom is not None:
                raise boom
            sink.db.calls.append(("flush", "artifacts"))
        sink.flush_artifacts = do_flush
    return sink


@pytest.mark.parametrize("event_type,status", [
    ("run.completed", "completed"),
    ("run.error", "error"),
])
def test_artifacts_are_committed_before_the_run_looks_finished(event_type, status):
    db = RecordingDb()
    _sink(db).publish(Event(event_type))
    assert db.calls == [
        ("flush", "artifacts"),      # results exist first...
        ("event", event_type),       # ...then the SSE stream is told
        ("status", status),          # ...and only then does status say finished
    ]


def test_a_mid_run_event_does_not_flush():
    """Capture is not free; only the terminal announcements pay for it."""
    db = RecordingDb()
    sink = _sink(db)
    sink.publish(Event("stage.started"))
    sink.publish(Event("stage.completed", {"to": "VALIDATE"}))
    sink.publish(Event("run.suspended"))
    assert ("flush", "artifacts") not in db.calls


def test_a_failing_flush_never_fails_the_run(capsys):
    """A capture problem must not cost a run that already has its answer."""
    db = RecordingDb()
    _sink(db, boom=RuntimeError("disk full")).publish(Event("run.completed"))
    assert db.calls == [("event", "run.completed"), ("status", "completed")]
    assert "artifact capture failed" in capsys.readouterr().out


def test_no_hook_is_harmless():
    """The sink is constructed before the orchestrator it flushes from exists."""
    db = RecordingDb()
    sink = PgEventSink(db, "sess-1")
    assert sink.flush_artifacts is None
    sink.publish(Event("run.completed"))
    assert db.calls == [("event", "run.completed"), ("status", "completed")]


def test_the_sequence_number_still_advances():
    db = RecordingDb()
    sink = _sink(db)
    sink.publish(Event("stage.started"))
    sink.publish(Event("run.completed"))
    assert sink._seq == 2


def test_the_runner_wires_the_hook_to_the_orchestrator():
    """_build_orchestrator must set the hook -- it is the whole delivery path.

    The sink is passed *into* build_orchestrator, so the closure can only be
    attached afterwards; forgetting to would silently restore the old ordering.
    """
    from unittest.mock import patch

    import runner.runner as rr

    captured = {}

    class Engine:
        @staticmethod
        def build_orchestrator(**kwargs):
            captured["sink"] = kwargs["sink"]
            return "the-orchestrator"

    with patch.object(rr, "capture_artifacts") as capture:
        orch = rr._build_orchestrator(
            Engine, RecordingDb(), "sess-1", {}, notifier=None, cancel=lambda: False)
        assert orch == "the-orchestrator"
        sink = captured["sink"]
        assert sink.flush_artifacts is not None
        sink.flush_artifacts()
        # bound to THIS run's orchestrator, not a stale or global one
        assert capture.call_args.args[1:] == ("sess-1", "the-orchestrator")


def test_recapturing_an_artifact_keeps_its_original_timestamp():
    """created_at must mean "created", so the ordering stays auditable.

    A run captures twice by design -- once before the terminal event, once from
    the runner's finally block as a backstop. While the upsert refreshed
    created_at, the second write reset every timestamp, so the column reported
    when the backstop ran and there was no way to check from data whether the
    results were committed before the run announced itself finished. A real run
    (460a1260) therefore still read as "status 13.7s before the last artifact"
    after the ordering was fixed.
    """
    from runner.db import RunnerDB

    sql = []

    class Db(RunnerDB):
        def __init__(self):
            pass

        def _execute(self, statement, params=None):
            sql.append(" ".join(statement.split()))

    Db().upsert_artifact("s", "execution_result", "{}", "json")
    assert len(sql) == 1
    assert "ON CONFLICT (session_id, name) DO UPDATE SET" in sql[0]
    assert "kind = EXCLUDED.kind" in sql[0]
    assert "content = EXCLUDED.content" in sql[0]
    assert "created_at" not in sql[0]
