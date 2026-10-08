"""RIS inventory (#185): the job, its parsing, the scheduler, and planning's use of it."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from runner import inventory

REPO = Path(__file__).resolve().parents[2]

SNAPSHOT = {
    "taken_at": "2026-10-08T15:08:06+00:00", "host": "c2-node-1",
    "envs_root": "/storage2/x/twain-envs", "modules": ["gcc/14.2.0", "python3"],
    "envs": {
        "nwchem": {"version": "2026-10-08", "python": "3.11.17", "error": None,
                   "packages": {"nwchem": "7.3.1", "openmm": "8.6.1", "openff-toolkit": "0.18.0",
                                "ambertools": "24", "rdkit": "2026.09.1"}},
        "psi4": {"version": "2026-10-07.2", "python": "3.11.17", "error": "TimeoutExpired: ...",
                 "packages": {}},
    },
}


def _stdout(snapshot=SNAPSHOT):
    return "noise\n" + inventory.MARKER + json.dumps(snapshot) + "\n"


class TestParse:
    def test_reads_the_marker_line(self):
        assert inventory.parse(_stdout())["envs"]["nwchem"]["version"] == "2026-10-08"

    @pytest.mark.parametrize("text", ["", "no marker here\n", inventory.MARKER + '{"envs": []}\n'])
    def test_refuses_output_without_a_usable_snapshot(self, text):
        with pytest.raises(ValueError):
            inventory.parse(text)

    def test_an_unreadable_env_is_none_not_empty(self):
        envs = inventory.envs_for_planning(SNAPSHOT["envs"])
        assert "openmm" in envs["nwchem"] and envs["psi4"] is None


class TestJobSpec:
    def test_short_partition_profile_account_and_twain_sh(self):
        profile = SimpleNamespace(short_partition="general-short", default_partition="general-cpu",
                                  account="compute2-mdan")
        spec = inventory.job_spec(profile, "/storage2/x/twain.sh")
        assert (spec["partition"], spec["account"], spec["time_limit"]) == \
            ("general-short", "compute2-mdan", "00:10:00")
        assert spec["script"].startswith("#!/bin/bash\nexport TWAIN_ENV_FILE='/storage2/x/twain.sh'\n")
        assert "TWAIN_INVENTORY_JSON" in spec["script"]

    def test_the_script_is_valid_bash(self):
        subprocess.run(["bash", "-n", str(inventory.SCRIPT)], check=True)


class _State:
    def __init__(self, terminal, value="COMPLETED"):
        self.is_terminal, self.value = terminal, value


class FakeDB:
    def __init__(self, pending=None, due=True):
        self.pending, self.due = pending, due
        self.calls = []

    def inventory_pending(self):
        return self.pending

    def inventory_due(self, hours):
        return self.due

    def inventory_submitting(self):
        self.calls.append("submitting")
        return 7

    def inventory_set_job(self, i, job):
        self.calls.append(("job", i, job))

    def inventory_ingested(self, i, snapshot):
        self.calls.append(("ingested", i, sorted(snapshot["envs"])))

    def inventory_failed(self, i, error):
        self.calls.append(("failed", i, error))


class FakeAdapter:
    def __init__(self, state=None, stdout="", submit_error=None):
        self.state, self.text, self.submit_error = state, stdout, submit_error
        self.specs = []

    def submit(self, spec, idempotency_key=None):
        if self.submit_error:
            raise self.submit_error
        self.specs.append((spec, idempotency_key))
        return "3365760"

    def poll(self, job_id):
        return self.state

    def stdout_page(self, job_id, offset, limit):
        chunk = self.text[offset:offset + limit]
        return {"content": chunk, "next_offset": offset + len(chunk),
                "eof": offset + len(chunk) >= len(self.text)}


PROFILE = SimpleNamespace(short_partition="general-short", default_partition="general-cpu",
                          account="compute2-mdan")


class TestScheduler:
    def test_submits_when_due(self):
        db, adapter = FakeDB(), FakeAdapter()
        assert inventory.InventoryScheduler(db, adapter, PROFILE, env_file="").tick() == "submitted"
        assert db.calls == ["submitting", ("job", 7, "3365760")]
        assert adapter.specs[0][1].startswith("twain-inventory-7-")

    def test_does_nothing_while_fresh(self):
        db = FakeDB(due=False)
        assert inventory.InventoryScheduler(db, FakeAdapter(), PROFILE).tick() == "fresh"
        assert db.calls == []

    def test_a_failed_submit_is_recorded_not_raised(self):
        db = FakeDB()
        out = inventory.InventoryScheduler(db, FakeAdapter(submit_error=RuntimeError("503")), PROFILE).tick()
        assert out == "submit-failed" and db.calls[-1][0] == "failed"

    def test_waits_for_a_running_job(self):
        db = FakeDB(pending={"id": 7, "ris_job_id": "1"})
        assert inventory.InventoryScheduler(db, FakeAdapter(state=_State(False)), PROFILE).tick() == "running"

    def test_ingests_a_long_output_across_pages_and_refreshes(self):
        seen = []
        db = FakeDB(pending={"id": 7, "ris_job_id": "1"})
        sched = inventory.InventoryScheduler(db, FakeAdapter(state=_State(True), stdout=_stdout()),
                                             PROFILE, on_ingest=lambda: seen.append(1), page_bytes=100)
        assert sched.tick() == "ingested"
        assert db.calls == [("ingested", 7, ["nwchem", "psi4"])] and seen == [1]

    def test_a_job_without_a_snapshot_fails(self):
        db = FakeDB(pending={"id": 7, "ris_job_id": "1"})
        out = inventory.InventoryScheduler(db, FakeAdapter(state=_State(True, "FAILED"), stdout="boom"),
                                           PROFILE).tick()
        assert out == "failed" and "FAILED" in db.calls[-1][2]

    def test_a_submit_that_never_answered_is_failed(self):
        db = FakeDB(pending={"id": 7, "ris_job_id": None})
        assert inventory.InventoryScheduler(db, FakeAdapter(), PROFILE).tick() == "failed"


def _pg_available():
    try:
        return subprocess.run(["pg_isready", "-h", "localhost"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not _pg_available(), reason="no local Postgres")
class TestInventorySql:
    @pytest.fixture
    def db(self, monkeypatch):
        name = f"twain_inventory_test_{uuid.uuid4().hex[:8]}"
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
        yield runner_db.RunnerDB()
        subprocess.run(["dropdb", "-h", "localhost", name], env=env)

    def test_lifecycle(self, db):
        assert db.inventory_due(24) and db.latest_inventory(168) is None
        i = db.inventory_submitting()
        assert not db.inventory_due(24)                       # one in flight
        db.inventory_set_job(i, "3365760")
        assert db.inventory_pending()["ris_job_id"] == "3365760"
        db.inventory_ingested(i, SNAPSHOT)
        assert db.inventory_pending() is None and not db.inventory_due(24)
        latest = db.latest_inventory(168)
        assert latest["envs"]["nwchem"]["packages"]["openmm"] == "8.6.1"
        db._execute("UPDATE ris_inventory SET finished_at = now() - interval '2 days';", ())
        assert db.inventory_due(24) and db.latest_inventory(24) is None

    def test_a_recent_failure_also_waits_for_the_interval(self, db):
        db.inventory_failed(db.inventory_submitting(), "boom")
        assert not db.inventory_due(24)
