"""RisJobEventWaiter: the Slurm poll sleep that wakes on a RIS webhook (#157).

The LISTEN connection and select() are faked, so these run without Postgres;
the NOTIFY itself comes from migration 012's trigger.
"""
import types

import psycopg2
import pytest

from runner import db as runner_db
from runner.db import RIS_EVENTS_CHANNEL, RisJobEventWaiter


class FakeConn:
    """A LISTEN connection whose notifications arrive in scripted batches."""

    def __init__(self, batches):
        self.batches = list(batches)   # each: list of payload strings
        self.notifies = []
        self.executed = []
        self.closed = False

    def set_isolation_level(self, _level):
        pass

    def cursor(self):
        conn = self

        class Cur:
            def execute(self, sql):
                conn.executed.append(sql)

            def close(self):
                pass
        return Cur()

    def poll(self):
        payloads = self.batches.pop(0)
        self.notifies.extend(types.SimpleNamespace(payload=p) for p in payloads)

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def fake_select(monkeypatch):
    """select() reports readable while batches remain, else times out (advancing the clock)."""
    def install(conn, clock):
        def select(rlist, _w, _x, timeout):
            if conn.batches:
                return (rlist, [], [])
            clock.t += timeout
            return ([], [], [])
        monkeypatch.setattr(runner_db.select, "select", select)
    return install


def _waiter(conn, clock, sleeps):
    db = types.SimpleNamespace(_connect=lambda: conn)
    return RisJobEventWaiter(db, sleep=sleeps.append, clock=clock)


def test_wakes_on_an_event_for_its_job(fake_select):
    conn, clock, sleeps = FakeConn([["42"]]), Clock(), []
    fake_select(conn, clock)
    waiter = _waiter(conn, clock, sleeps)

    assert waiter.wait("42", 30.0) is True
    assert conn.executed == [f"LISTEN {RIS_EVENTS_CHANNEL};"]
    assert clock.t == 0.0 and sleeps == []


def test_ignores_other_jobs_and_times_out(fake_select):
    conn, clock, sleeps = FakeConn([["7"], ["8"]]), Clock(), []
    fake_select(conn, clock)
    waiter = _waiter(conn, clock, sleeps)

    assert waiter.wait("42", 30.0) is False
    assert clock.t == 30.0          # waited out the whole interval


def test_reuses_one_listen_connection_and_close_releases_it(fake_select):
    conn, clock, sleeps = FakeConn([["42"], ["42"]]), Clock(), []
    fake_select(conn, clock)
    waiter = _waiter(conn, clock, sleeps)

    waiter.wait("42", 30.0)
    waiter.wait("42", 30.0)
    assert len(conn.executed) == 1
    waiter.close()
    assert conn.closed


def test_db_trouble_degrades_to_plain_sleep_for_the_rest_of_the_run():
    clock, sleeps = Clock(), []

    def no_db():
        raise psycopg2.OperationalError("connection refused")
    waiter = RisJobEventWaiter(types.SimpleNamespace(_connect=no_db),
                               sleep=sleeps.append, clock=clock)

    assert waiter.wait("42", 30.0) is False
    assert waiter.wait("42", 30.0) is False
    assert sleeps == [30.0, 30.0]
