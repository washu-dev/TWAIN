"""Detached EXECUTE: submit, pause, collect on resume (P2, #171).

No process waits on a Slurm job any more: execute() submits, records the job
in cluster_jobs and raises the run's pause signal; the resumed execute() --
possibly on another worker -- collects it.

Run from the repo root with:  pixi run pytest tests/unit/test_detached_execute.py
"""
import io
import tarfile
from pathlib import Path

import pytest

from execution_adapter.cluster_profile import ClusterProfile
from execution_adapter.execution_result import ExecutionStatus
from execution_adapter.s3_transport import OUTPUTS_KEY, S3Transport
from execution_adapter.slurm_execution_adapter import SlurmExecutionAdapter
from plan_synthesizer.execution_plan import SlurmRequest


class Paused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _suspend(reason):
    raise Paused(reason)


class Store:
    """In-memory cluster_jobs."""

    def __init__(self):
        self.rows = {}

    def latest(self, session_id):
        mine = [r for r in self.rows.values() if r["session_id"] == session_id]
        return max(mine, key=lambda r: r["attempt"]) if mine else None

    def record_submitted(self, session_id, attempt, ris_job_id, s3_prefix, detail):
        self.rows[ris_job_id] = {"ris_job_id": ris_job_id, "session_id": session_id,
                                 "attempt": attempt, "s3_prefix": s3_prefix,
                                 "status": "submitted", "detail": detail}

    def mark(self, ris_job_id, status):
        self.rows[ris_job_id]["status"] = status


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


class Ris:
    base_url = "https://d3n2m687w2hvtj.cloudfront.net/api/v1"

    def __init__(self):
        self.submitted, self.cancelled, self.state = [], [], "PENDING"
        self.polls = 0

    def submit_job(self, spec, *, idempotency_key=None):
        self.submitted.append(spec)
        return str(500 + len(self.submitted))

    def get_job(self, job_id):
        self.polls += 1
        return {"job_id": job_id, "state": self.state}

    def accounting(self, job_id):
        return {"job_id": job_id, "state": self.state, "exit_code": "0:0", "elapsed": "42"}

    def cancel_job(self, job_id, *, signal=None):
        self.cancelled.append(job_id)

    def stdout(self, job_id):
        return "done\n"

    def output_tail(self, job_id, stream, nbytes):
        return ""

    def output_page(self, job_id, stream, offset=0, limit=0):
        return {"content": "", "next_offset": 0, "size": 0}


def _profile():
    return ClusterProfile(
        name="compute2", login_nodes=["c2-login-001.ris.wustl.edu"], account="compute2-mdan",
        accounts=["compute2-mdan"], default_partition="general-cpu",
        storage_root="/storage2/x", envs_root="/storage2/x/twain-envs",
        partitions=[{"name": "general-cpu", "max_minutes": 21600, "gpus": False}])


@pytest.fixture
def world(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "main.py").write_text("print(1)")
    ris, s3, store, abort = Ris(), FakeS3(), Store(), {"now": False}

    def make():
        def no_ssh(argv):
            raise AssertionError(f"detached S3 runs never use SSH: {argv}")
        return SlurmExecutionAdapter(
            _profile(), request=SlurmRequest(cpu_count=2, gpu_count=0, max_time=30, ram=4096),
            workspace_root=str(tmp_path / "ws"), backend="api", ris_api_client=ris,
            staging="s3", s3_transport=S3Transport("bkt", client=s3),
            issue_job_ticket=lambda *a: "tkt", api_public_url="https://twain.example",
            env_file="/x/twain.sh", transfer_runner=no_ssh, cluster_runner=no_ssh,
            cluster_jobs=store, suspend=_suspend, should_abort=lambda: abort["now"],
            poll_interval=0.0, sleep=lambda _s: pytest.fail("a detached run must not wait"))
    return {"bundle": bundle, "ris": ris, "s3": s3, "store": store, "abort": abort, "make": make}


def _outputs(world, attempt):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"gap,1.12\n"
        info = tarfile.TarInfo("results.csv")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    world["s3"].objects[("bkt", f"runs/s/attempt-{attempt}/{OUTPUTS_KEY}")] = buf.getvalue()


def test_submit_records_the_job_and_pauses_without_waiting(world):
    with pytest.raises(Paused) as info:
        world["make"]().execute(str(world["bundle"]), run_id="s")
    assert info.value.reason == "cluster"
    row = world["store"].latest("s")
    assert (row["ris_job_id"], row["attempt"], row["status"]) == ("501", 1, "submitted")
    assert world["ris"].polls == 0                       # nobody polled


def test_a_wake_up_before_the_job_finished_pauses_again(world):
    with pytest.raises(Paused):
        world["make"]().execute(str(world["bundle"]), run_id="s")
    world["ris"].state = "RUNNING"
    with pytest.raises(Paused):                          # a fresh worker, same store
        world["make"]().execute(str(world["bundle"]), run_id="s")
    assert len(world["ris"].submitted) == 1              # not submitted twice


def test_the_resume_after_the_job_finished_collects_it(world):
    with pytest.raises(Paused):
        world["make"]().execute(str(world["bundle"]), run_id="s")
    world["ris"].state = "COMPLETED"
    _outputs(world, 1)
    result = world["make"]().execute(str(world["bundle"]), run_id="s")
    assert result.status == ExecutionStatus.SUCCESS
    assert (Path(result.artifacts_dir) / "results.csv").read_text() == "gap,1.12\n"
    assert result.install_log["attempt"] == 1 and result.duration_seconds == 42.0
    assert world["store"].latest("s")["status"] == "collected"


def test_terminate_while_paused_cancels_the_slurm_job(world):
    with pytest.raises(Paused):
        world["make"]().execute(str(world["bundle"]), run_id="s")
    world["abort"]["now"] = True
    result = world["make"]().execute(str(world["bundle"]), run_id="s")
    assert world["ris"].cancelled == ["501"]
    assert "terminated by the researcher" in result.message
    assert world["store"].latest("s")["status"] == "cancelled"


def test_the_next_attempt_is_numbered_from_the_store(world):
    # Attempt 1 collected, then a self-heal re-run on another worker: attempt 2.
    with pytest.raises(Paused):
        world["make"]().execute(str(world["bundle"]), run_id="s")
    world["ris"].state = "COMPLETED"
    _outputs(world, 1)
    world["make"]().execute(str(world["bundle"]), run_id="s")
    world["ris"].state = "PENDING"
    with pytest.raises(Paused):
        world["make"]().execute(str(world["bundle"]), run_id="s")
    assert world["store"].latest("s")["attempt"] == 2


def test_detached_needs_s3_and_a_suspend(tmp_path):
    with pytest.raises(ValueError, match="staging='s3'"):
        SlurmExecutionAdapter(_profile(), backend="api", ris_api_client=Ris(),
                              workspace_root=str(tmp_path), cluster_jobs=Store(), suspend=_suspend)
