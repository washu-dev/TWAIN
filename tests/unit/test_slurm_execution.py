"""Unit tests for Slurm execution: staging, lifecycle, and the EXECUTE route.

Covers what Story 5.4 added on top of the low-level sbatch adapter:
  * SlurmAdapter.wait()/accounting()/exit_code() -- bounded polling + sacct
  * staging.Stager -- rsync/ssh push/pull/cleanup (remote and on-cluster modes)
  * SlurmExecutionAdapter -- the full stage/submit/wait/fetch lifecycle behind
    the same execute() interface as the local/Docker adapters
  * the state machine's execute() routing when execute_slurm is on

Everything runs offline through injected fake runners -- no SSH, no cluster.

Run from the repo root with:  pixi run pytest tests/unit/test_slurm_execution.py
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from execution_adapter.cluster_profile import ClusterProfile
from execution_adapter.execution_result import ExecutionStatus
from execution_adapter.slurm_adapter import (
    CommandResult,
    JobState,
    SlurmAdapter,
    SlurmError,
)
from execution_adapter.slurm_execution_adapter import (
    SlurmExecutionAdapter,
    _elapsed_seconds,
    _maxrss_mb,
)
from execution_adapter.staging import Stager, StagingError
from plan_synthesizer.execution_plan import SlurmRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO_ROOT / "modules" / "16_agent_mesh_control_plane"
sys.path.insert(0, str(MODULE_DIR))

from states import State  # noqa: E402
from crash_recovery import DataStorage  # noqa: E402
import statemachine as SM  # noqa: E402


# -- fixtures -------------------------------------------------------------------

def _profile():
    return ClusterProfile(
        name="compute2",
        login_nodes=["c2-login-001.ris.wustl.edu"],
        account="compute2-mdan",
        modules=["ris", "slurm"],
        storage_root="/storage2/fs1/mdan/Active/dtrc2026-workshop",
        default_partition="general-cpu",
        partitions=[{"name": "general-cpu", "max_minutes": 21600, "gpus": False}],
    )


class ScriptedRunner:
    """Returns scripted CommandResults matched by predicate over the argv."""

    def __init__(self):
        self.calls = []
        self.rules = []  # (predicate, CommandResult | list to pop from)

    def on(self, predicate, result):
        self.rules.append((predicate, result))
        return self

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        for predicate, result in self.rules:
            if predicate(argv):
                if isinstance(result, list):
                    return result.pop(0) if len(result) > 1 else result[0]
                return result
        return CommandResult(0, "")


def _is(cmd):
    return lambda argv: argv[0] == cmd


def _sacct_accounting(argv):
    return argv[0] == "sacct" and any("Elapsed" in a for a in argv)


def _sacct_state(argv):
    return argv[0] == "sacct" and not any("Elapsed" in a for a in argv)


def _bundle(tmp_path, with_requirements=True, with_smoke=True):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "main.py").write_text("print('logS=-1.7')\n")
    if with_requirements:
        (bundle / "requirements.txt").write_text("ase==3.23.0\n")
    if with_smoke:
        (bundle / "inline_tests.py").write_text("raise SystemExit(0)\n")
    return bundle


# -- SlurmAdapter.wait / accounting ----------------------------------------------

def test_wait_polls_until_terminal_and_reports_states():
    runner = ScriptedRunner()
    runner.on(_is("squeue"), [CommandResult(0, "PENDING\n"),
                              CommandResult(0, "RUNNING\n"),
                              CommandResult(0, "")])
    runner.on(_sacct_state, CommandResult(0, "COMPLETED\n"))
    adapter = SlurmAdapter(_profile(), runner=runner)

    seen = []
    state = adapter.wait("42", poll_interval=0.0, on_state=seen.append,
                         sleep=lambda _s: None)
    assert state == JobState.COMPLETED
    assert seen[:2] == [JobState.PENDING, JobState.RUNNING]


def test_wait_raises_when_budget_expires_without_cancelling():
    runner = ScriptedRunner()
    runner.on(_is("squeue"), CommandResult(0, "RUNNING\n"))
    adapter = SlurmAdapter(_profile(), runner=runner)

    with pytest.raises(SlurmError, match="still running"):
        adapter.wait("42", poll_interval=10.0, max_wait=25.0, sleep=lambda _s: None)
    assert not any(argv[0] == "scancel" for argv in runner.calls)


def test_accounting_and_exit_code_parse_sacct():
    runner = ScriptedRunner()
    runner.on(_is("sacct"), CommandResult(0, "FAILED|2:0|00:01:23|123456K|general-cpu\n"))
    adapter = SlurmAdapter(_profile(), runner=runner)

    accounting = adapter.accounting("42")
    assert accounting["State"] == "FAILED"
    assert accounting["Elapsed"] == "00:01:23"
    assert adapter.exit_code("42") == 2


@pytest.mark.parametrize("text,expected", [
    ("00:01:23", 83.0), ("1-01:00:00", 90000.0), ("", 0.0), ("garbage", 0.0),
])
def test_elapsed_seconds(text, expected):
    assert _elapsed_seconds(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("123456K", pytest.approx(120.562, abs=0.01)),
    ("800M", 800.0), ("1.5G", 1536.0), ("", None), ("n/a", None),
])
def test_maxrss_mb(text, expected):
    assert _maxrss_mb(text) == expected


# -- staging ---------------------------------------------------------------------

def test_push_creates_remote_dir_and_rsyncs(tmp_path):
    runner = ScriptedRunner()
    stager = Stager(_profile(), user="timmy", runner=runner)
    bundle = _bundle(tmp_path)

    remote = stager.push(bundle, run_id="sess1")
    assert remote == ("/storage2/fs1/mdan/Active/dtrc2026-workshop/"
                      "twain-runs/sess1")
    mkdir, rsync = runner.calls
    assert mkdir[:2] == ["ssh", "timmy@c2-login-001.ris.wustl.edu"]
    assert "mkdir -p" in mkdir[2]
    assert rsync[0] == "rsync" and rsync[-1].endswith(":" + remote + "/")


def test_pull_rsyncs_back_into_local_dir(tmp_path):
    runner = ScriptedRunner()
    stager = Stager(_profile(), runner=runner)
    local = tmp_path / "out"
    assert stager.pull("sess1", local) == str(local)
    rsync = runner.calls[-1]
    assert rsync[0] == "rsync"
    assert rsync[-2].startswith("c2-login-001.ris.wustl.edu:")
    assert local.is_dir()


def test_local_mode_uses_plain_commands(tmp_path):
    """host='' (already on a login node): no ssh hop, plain mkdir/rsync."""
    runner = ScriptedRunner()
    stager = Stager(_profile(), host="", runner=runner)
    stager.push(_bundle(tmp_path), run_id="s")
    mkdir, rsync = runner.calls
    assert mkdir[0] == "mkdir"
    assert not any(a.startswith("ssh") for a in rsync)
    assert ":" not in rsync[-1]  # local destination, no host prefix


def test_cleanup_is_guarded_to_the_runs_subdir():
    runner = ScriptedRunner()
    stager = Stager(_profile(), runner=runner)
    stager.cleanup("sess1")  # fine: under twain-runs/
    rm = runner.calls[-1]
    assert rm[0] == "ssh" and "rm -rf" in rm[2] and "twain-runs/sess1" in rm[2]

    # the guard refuses anything resolving outside <storage_root>/twain-runs/
    with patch.object(Stager, "remote_run_dir", return_value=stager.remote_root):
        with pytest.raises(StagingError, match="refusing"):
            stager.cleanup("sess1")


def test_stager_requires_a_storage_root():
    profile = _profile()
    profile.storage_root = None
    with pytest.raises(StagingError, match="storage_root"):
        Stager(profile, runner=ScriptedRunner())


# -- SlurmExecutionAdapter lifecycle ----------------------------------------------

def _exec_adapter(tmp_path, cluster_runner, transfer_runner=None, **kwargs):
    kwargs.setdefault("poll_interval", 0.0)
    return SlurmExecutionAdapter(
        _profile(),
        request=SlurmRequest(cpu_count=8, gpu_count=0, max_time=45, ram=16000),
        user="timmy",
        workspace_root=str(tmp_path),
        cluster_runner=cluster_runner,
        transfer_runner=transfer_runner or ScriptedRunner(),
        sleep=lambda _s: None,
        **kwargs,
    )


def _happy_cluster_runner():
    runner = ScriptedRunner()
    runner.on(_is("sbatch"), CommandResult(0, "Submitted batch job 42\n"))
    runner.on(_is("squeue"), CommandResult(0, ""))
    runner.on(_sacct_accounting, CommandResult(0, "COMPLETED|0:0|00:02:00|800M|general-cpu\n"))
    runner.on(_sacct_state, CommandResult(0, "COMPLETED\n"))
    return runner


def test_execute_happy_path_stages_submits_and_fetches(tmp_path):
    cluster = _happy_cluster_runner()
    transfer = ScriptedRunner()
    adapter = _exec_adapter(tmp_path, cluster, transfer)
    bundle = _bundle(tmp_path)

    result = adapter.execute(str(bundle), run_id="sess1")

    assert result.status == ExecutionStatus.SUCCESS
    assert result.succeeded
    assert result.exit_code == 0
    assert result.duration_seconds == 120.0
    assert result.peak_memory_mb == 800.0
    assert result.install_log["job_id"] == "42"
    assert result.install_log["cluster"] == "compute2"
    assert result.artifacts_dir == str(bundle)

    # the sbatch script was rendered into the bundle and submitted remotely
    script = (bundle / "job.slurm").read_text()
    assert "#SBATCH --cpus-per-task=8" in script
    assert "#SBATCH --mem=16000M" in script
    assert "twain-runs/sess1" in script          # workdir + log land in the run dir
    remote_script = "/storage2/fs1/mdan/Active/dtrc2026-workshop/twain-runs/sess1/job.slurm"
    assert ["sbatch", remote_script] in cluster.calls
    # staged before submit; pulled after completion
    assert transfer.calls[0][0] in ("ssh", "mkdir")
    assert transfer.calls[-1][0] == "rsync"


def test_payload_builds_venv_runs_smoke_then_main(tmp_path):
    adapter = _exec_adapter(tmp_path, _happy_cluster_runner())
    bundle = _bundle(tmp_path)
    payload = adapter._payload(bundle, install_deps=True, run_smoke=True)
    steps = payload.split(" && ")
    assert steps[0] == "python3 -m venv .venv"
    assert any("-r requirements.txt" in s for s in steps)
    assert steps[-2] == ".venv/bin/python inline_tests.py"
    assert steps[-1] == ".venv/bin/python main.py"


def test_payload_without_deps_or_smoke_is_bare_python(tmp_path):
    adapter = _exec_adapter(tmp_path, _happy_cluster_runner())
    bundle = _bundle(tmp_path, with_requirements=False, with_smoke=False)
    assert adapter._payload(bundle, install_deps=True, run_smoke=True) == "python3 main.py"


def test_execute_reads_job_log_as_stdout(tmp_path):
    cluster = _happy_cluster_runner()
    adapter = _exec_adapter(tmp_path, cluster)
    bundle = _bundle(tmp_path)
    # simulate the pulled job log (the fake transfer runner copies nothing)
    (bundle / "twain-sess1-42.log").write_text("logS=-1.7\n")

    result = adapter.execute(str(bundle), run_id="sess1")
    assert "logS=-1.7" in result.stdout


def test_execute_maps_exit_2_to_dependency_error(tmp_path):
    cluster = ScriptedRunner()
    cluster.on(_is("sbatch"), CommandResult(0, "Submitted batch job 43\n"))
    cluster.on(_is("squeue"), CommandResult(0, ""))
    cluster.on(_sacct_accounting, CommandResult(0, "FAILED|2:0|00:00:10|10M|general-cpu\n"))
    cluster.on(_sacct_state, CommandResult(0, "FAILED\n"))
    adapter = _exec_adapter(tmp_path, cluster)

    result = adapter.execute(str(_bundle(tmp_path)), run_id="s")
    assert result.status == ExecutionStatus.DEPENDENCY_ERROR
    assert not result.succeeded


def test_execute_leaves_long_job_running_on_wait_expiry(tmp_path):
    cluster = ScriptedRunner()
    cluster.on(_is("sbatch"), CommandResult(0, "Submitted batch job 44\n"))
    cluster.on(_is("squeue"), CommandResult(0, "RUNNING\n"))
    adapter = _exec_adapter(tmp_path, cluster, max_wait=0.0, poll_interval=1.0)

    result = adapter.execute(str(_bundle(tmp_path)), run_id="s")
    assert result.status == ExecutionStatus.TIMEOUT
    assert "NOT cancelled" in result.message
    assert "44" in result.message
    assert not any(argv[0] == "scancel" for argv in cluster.calls)


def test_execute_submit_failure_is_setup_failed(tmp_path):
    cluster = ScriptedRunner()
    cluster.on(_is("sbatch"), CommandResult(1, "", "sbatch: error: invalid account"))
    adapter = _exec_adapter(tmp_path, cluster)

    result = adapter.execute(str(_bundle(tmp_path)), run_id="s")
    assert result.status == ExecutionStatus.SETUP_FAILED
    assert "invalid account" in result.message


def test_execute_missing_bundle_is_setup_failed(tmp_path):
    adapter = _exec_adapter(tmp_path, _happy_cluster_runner())
    result = adapter.execute(str(tmp_path / "nope"), run_id="s")
    assert result.status == ExecutionStatus.SETUP_FAILED


# -- state machine routing ---------------------------------------------------------

@pytest.fixture
def machine(tmp_path):
    with patch.object(DataStorage, "load", return_value=None):
        m = SM.StateMachine(data_path=str(tmp_path / "state.json"),
                            run_id="testrun", execute_slurm=True)
    m.artifacts_dir = tmp_path
    return m


class FakeExecAdapter:
    def __init__(self, result_dict):
        self.result_dict = result_dict
        self.kwargs = None

    def execute(self, bundle_dir, **kwargs):
        self.kwargs = kwargs
        class _R:
            succeeded = True
            def to_dict(self_inner):
                return dict(self.result_dict)
        return _R()


def test_execute_routes_to_slurm_adapter(machine, tmp_path):
    bundle = _bundle(tmp_path)
    machine.context.artifacts["run_bundle"] = str(bundle)
    fake = FakeExecAdapter({"status": "success", "succeeded": True})
    with patch.object(machine, "_build_slurm_adapter", return_value=fake):
        assert machine.execute() == State.INTERPRET

    assert machine.context.execution_status is True
    # Slurm jobs install their own deps in-job and fix the interpreter themselves.
    assert fake.kwargs["install_deps"] is True
    assert fake.kwargs["python_executable"] is None
    result = machine._load_artifact("execution_result")
    assert result == {"status": "success", "succeeded": True}


def test_execute_skips_gracefully_when_profile_unusable(machine, tmp_path):
    bundle = _bundle(tmp_path)
    machine.context.artifacts["run_bundle"] = str(bundle)
    with patch.object(machine, "_build_slurm_adapter", return_value=None):
        assert machine.execute() == State.INTERPRET

    assert machine.context.execution_status is True  # graceful, complete outcome
    result = machine._load_artifact("execution_result")
    assert result["status"] == "skipped_missing_dependency"


def test_build_slurm_adapter_uses_plan_request(machine, tmp_path):
    # Plan contract: ram in GB, max_time in hours. Adapter receives MB + minutes.
    plan = {"slurm_request": {"cpu_count": 16, "gpu_count": 1,
                              "max_time": 1.5, "ram": 32}}
    path = tmp_path / "execution_plan_seed.json"
    path.write_text(json.dumps(plan))
    machine.context.artifacts["execution_plan"] = str(path)

    adapter = machine._build_slurm_adapter()
    assert adapter is not None
    assert adapter.request.cpu_count == 16
    assert adapter.request.gpu_count == 1
    assert adapter.request.ram == 32 * 1024  # GB -> MB
    assert adapter.request.max_time == 90.0  # hours -> minutes
    assert adapter.workspace_root == str(tmp_path)


def test_build_slurm_adapter_applies_ram_floor(machine, tmp_path):
    # A planner that asks for 1 GB must still submit with the 4 GB floor.
    plan = {"slurm_request": {"cpu_count": 4, "gpu_count": 0,
                              "max_time": 0.05, "ram": 1}}
    path = tmp_path / "execution_plan_seed.json"
    path.write_text(json.dumps(plan))
    machine.context.artifacts["execution_plan"] = str(path)

    adapter = machine._build_slurm_adapter()
    assert adapter.request.ram == 4 * 1024
    assert adapter.request.max_time == 10.0  # MIN_WALL_MINUTES


def test_execute_slurm_off_keeps_execute_a_noop(tmp_path):
    with patch.object(DataStorage, "load", return_value=None):
        m = SM.StateMachine(data_path=str(tmp_path / "s.json"), run_id="t")
    m.artifacts_dir = tmp_path
    assert m.execute() == State.INTERPRET
    assert "execution_result" not in m.context.artifacts
