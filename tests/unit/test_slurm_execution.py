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


def test_wait_survives_transient_poll_failures():
    # A VPN drop makes squeue-over-SSH fail; the loop must keep polling (the
    # job is still running on the cluster) and pick up the terminal state once
    # contact returns.
    runner = ScriptedRunner()
    runner.on(_is("squeue"), [CommandResult(0, "RUNNING\n"),
                              CommandResult(255, "", "ssh: connect timed out"),
                              CommandResult(255, "", "ssh: connect timed out"),
                              CommandResult(0, "")])
    runner.on(_sacct_state, CommandResult(0, "COMPLETED\n"))
    adapter = SlurmAdapter(_profile(), runner=runner)

    state = adapter.wait("42", poll_interval=10.0, sleep=lambda _s: None)
    assert state == JobState.COMPLETED


def test_wait_raises_lost_contact_after_tolerance_without_cancelling():
    runner = ScriptedRunner()
    runner.on(_is("squeue"), CommandResult(255, "", "ssh: connect timed out"))
    adapter = SlurmAdapter(_profile(), runner=runner)
    adapter.CONTACT_LOSS_TOLERANCE = 25.0  # ~2 failed polls at 10s

    with pytest.raises(SlurmError, match="lost contact"):
        adapter.wait("42", poll_interval=10.0, sleep=lambda _s: None)
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


def test_mp_api_key_is_forwarded_into_the_job_env(tmp_path, monkeypatch):
    # A compute node gets a fresh shell, and an SSH-submitted job doesn't
    # inherit the runner's environment -- the Materials Project key a lookup
    # script reads must be exported in the sbatch script explicitly.
    monkeypatch.setenv("MP_API_KEY", "test-mp-key-123")
    adapter = _exec_adapter(tmp_path, _happy_cluster_runner())
    bundle = _bundle(tmp_path)

    adapter.execute(str(bundle), run_id="sess-mp")

    script = (bundle / "job.slurm").read_text()
    assert "export MP_API_KEY=test-mp-key-123" in script


def test_no_mp_api_key_means_no_export(tmp_path, monkeypatch):
    monkeypatch.delenv("MP_API_KEY", raising=False)
    adapter = _exec_adapter(tmp_path, _happy_cluster_runner())
    bundle = _bundle(tmp_path)

    adapter.execute(str(bundle), run_id="sess-mp")

    assert "MP_API_KEY" not in (bundle / "job.slurm").read_text()


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


def test_payload_prefers_preprovisioned_envs_with_venv_fallback(tmp_path):
    # env_pythons are tried in order at job start; only if none is USABLE does
    # the payload build a venv (which can't handle compiled calculators like
    # GPAW). Usable = executable AND passes the bundle's smoke test: the
    # `default` env always exists but lacks specialty tools (OpenMM), so a
    # bare existence check would wrongly skip the pip fallback.
    adapter = _exec_adapter(
        tmp_path, _happy_cluster_runner(),
        env_pythons=["/envs/gpaw/bin/python", "/envs/default/bin/python"],
    )
    bundle = _bundle(tmp_path)
    payload = adapter._payload(bundle, install_deps=True, run_smoke=True)
    assert payload.startswith("set -e")
    assert 'for CAND in /envs/gpaw/bin/python /envs/default/bin/python' in payload
    assert '[ -x "$CAND" ] || continue' in payload
    # Probed with the bundle's own smoke test, inside a subshell: the candidate's
    # env is put on PATH first (an ASE calculator shells out to its engine
    # binary), and a rejected candidate must not leave that PATH behind for the
    # next one. See TestEnvActivationInThePayload.
    assert '( twain_use_env "$CAND"' in payload
    assert ('    "$CAND" inline_tests.py >/dev/null 2>&1 ) '
            '&& { PY="$CAND"; break; }') in payload
    assert "python3 -m venv .venv" in payload      # fallback still present
    assert '"$PY" inline_tests.py' in payload
    # main.py runs under mpirun when the env ships it (openmpi GPAW build) --
    # one rank per allocated CPU, single-threaded -- else plain python.
    # GPAW's parallel guard requires launching through its `gpaw python`
    # equivalent (`-m gpaw python`) under MPI; other envs use the plain
    # interpreter. -m avoids the entry script's (possibly broken) shebang.
    assert 'if [ -x "$BIN/gpaw" ]; then LAUNCH="$PY -m gpaw python";' in payload
    assert ('"$BIN/mpirun" -np "${SLURM_CPUS_PER_TASK:-1}"'
            ' --map-by :OVERSUBSCRIBE --bind-to none $LAUNCH main.py') in payload
    assert "export OMP_NUM_THREADS=1" in payload
    # OpenMPI needs OPAL_PREFIX when invoked by path without env activation.
    assert 'export OPAL_PREFIX="$(dirname "$BIN")"' in payload
    assert '  "$PY" main.py' in payload            # serial fallback branch


def test_env_payload_without_requirements_falls_back_to_system_python(tmp_path):
    adapter = _exec_adapter(
        tmp_path, _happy_cluster_runner(), env_pythons=["/envs/xtb/bin/python"],
    )
    bundle = _bundle(tmp_path, with_requirements=False, with_smoke=False)
    payload = adapter._payload(bundle, install_deps=True, run_smoke=True)
    assert "venv" not in payload
    assert 'PY="python3"' in payload
    assert '"$PY" main.py' in payload
    # No smoke test in the bundle -> nothing to probe with; the bare
    # existence check picks the first executable candidate.
    assert 'PY="$CAND"; break' in payload
    assert "inline_tests.py" not in payload


def test_env_payload_skips_probe_when_smoke_disabled(tmp_path):
    adapter = _exec_adapter(
        tmp_path, _happy_cluster_runner(), env_pythons=["/envs/gpaw/bin/python"],
    )
    bundle = _bundle(tmp_path)  # has inline_tests.py, but smoke is off
    payload = adapter._payload(bundle, install_deps=True, run_smoke=False)
    assert "inline_tests.py" not in payload
    assert 'PY="$CAND"; break' in payload


# -- preflight: fail fast before sbatch when no env can serve the bundle ---------

def _is_probe(argv):
    return argv[0] == "bash" and "inline_tests.py" in argv[2]


def _is_pip_dry_run(argv):
    return argv[0] == "bash" and "pip install --dry-run" in argv[2]


def test_preflight_env_pass_stops_probing_and_submits(tmp_path):
    cluster = _happy_cluster_runner()
    cluster.on(_is_probe, CommandResult(0, ""))
    adapter = _exec_adapter(
        tmp_path, cluster,
        env_pythons=["/envs/xtb/bin/python", "/envs/default/bin/python"],
    )
    result = adapter.execute(str(_bundle(tmp_path)), run_id="s1")
    assert result.status == ExecutionStatus.SUCCESS
    # first candidate passed -> no second probe, no pip check, sbatch ran
    assert len([c for c in cluster.calls if _is_probe(c)]) == 1
    assert not any(_is_pip_dry_run(c) for c in cluster.calls)
    assert any(c[0] == "sbatch" for c in cluster.calls)


def test_preflight_blocks_submission_when_nothing_can_run_the_bundle(tmp_path):
    # Fingerprint of Slurm job 2459489: every env fails the smoke probe AND
    # pip's resolver cannot install the requirements (conda-only xtb-python).
    # The old flow burned a stage + queue round-trip to learn this.
    cluster = _happy_cluster_runner()
    cluster.on(_is_probe, CommandResult(2, "[smoke] MISSING DEPENDENCY: xtb"))
    cluster.on(_is_pip_dry_run, CommandResult(
        1, "", "ERROR: No matching distribution found for xtb-python==22.1"))
    adapter = _exec_adapter(
        tmp_path, cluster, env_pythons=["/envs/default/bin/python"])
    result = adapter.execute(str(_bundle(tmp_path)), run_id="s2")
    assert result.status == ExecutionStatus.DEPENDENCY_ERROR
    assert "provision" in result.message.lower()
    assert "xtb-python" in result.message
    assert not any(c[0] == "sbatch" for c in cluster.calls)  # never queued


def test_preflight_fails_open_when_pip_can_install(tmp_path):
    # No env passes, but the requirements resolve on PyPI -> the job's
    # venv+pip fallback will work; submission must proceed.
    cluster = _happy_cluster_runner()
    cluster.on(_is_probe, CommandResult(2, ""))
    cluster.on(_is_pip_dry_run, CommandResult(0, "Would install ase-3.23.0"))
    adapter = _exec_adapter(
        tmp_path, cluster, env_pythons=["/envs/default/bin/python"])
    result = adapter.execute(str(_bundle(tmp_path)), run_id="s3")
    assert result.status == ExecutionStatus.SUCCESS
    assert any(c[0] == "sbatch" for c in cluster.calls)


def test_preflight_fails_open_on_ambiguous_pip_verdict(tmp_path):
    # `--dry-run` needs pip >= 22.2; an old pip erroring out is not a
    # dependency verdict and must not block the job.
    cluster = _happy_cluster_runner()
    cluster.on(_is_probe, CommandResult(2, ""))
    cluster.on(_is_pip_dry_run, CommandResult(2, "", "no such option: --dry-run"))
    adapter = _exec_adapter(
        tmp_path, cluster, env_pythons=["/envs/default/bin/python"])
    result = adapter.execute(str(_bundle(tmp_path)), run_id="s4")
    assert result.status == ExecutionStatus.SUCCESS


def test_preflight_pip_dry_run_uses_an_env_python(tmp_path):
    # The login node's bare python3 can be ancient (pip < 22.2: no --dry-run)
    # or off PATH in a non-login shell -- either way the resolver verdict is
    # ambiguous and a conda-only requirement (psi4) sails through to die in
    # the job. The dry-run must prefer a pre-provisioned env's python, whose
    # pip we control.
    cluster = _happy_cluster_runner()
    cluster.on(_is_probe, CommandResult(2, ""))
    cluster.on(_is_pip_dry_run, CommandResult(0, "Would install ase-3.23.0"))
    adapter = _exec_adapter(
        tmp_path, cluster,
        env_pythons=["/envs/gpaw/bin/python", "/envs/default/bin/python"])
    adapter.execute(str(_bundle(tmp_path)), run_id="s6")
    dry_runs = [c for c in cluster.calls if _is_pip_dry_run(c)]
    assert dry_runs, "expected the preflight to consult pip's resolver"
    # The command tries each env python (first existing wins) before falling
    # back to the bare python3.
    assert "/envs/gpaw/bin/python" in dry_runs[0][2]
    assert "/envs/default/bin/python" in dry_runs[0][2]


def test_preflight_skipped_without_env_pythons(tmp_path):
    cluster = _happy_cluster_runner()
    adapter = _exec_adapter(tmp_path, cluster)
    result = adapter.execute(str(_bundle(tmp_path)), run_id="s5")
    assert result.status == ExecutionStatus.SUCCESS
    assert not any(c[0] == "bash" for c in cluster.calls)


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


def test_execute_terminate_scancels_job_and_reports_cleanly(tmp_path):
    # The researcher pressed Terminate mid-poll: the adapter must scancel the
    # job and return a clean "terminated" result instead of polling on.
    cluster = ScriptedRunner()
    cluster.on(_is("sbatch"), CommandResult(0, "Submitted batch job 45\n"))
    cluster.on(_is("squeue"), CommandResult(0, "RUNNING\n"))
    cluster.on(_is("scancel"), CommandResult(0, ""))
    adapter = _exec_adapter(tmp_path, cluster, should_abort=lambda: True)

    result = adapter.execute(str(_bundle(tmp_path)), run_id="s")
    assert result.status == ExecutionStatus.FAILED
    assert "terminated by the researcher" in result.message
    assert "45" in result.message
    assert any(argv[0] == "scancel" for argv in cluster.calls)


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


def test_build_slurm_adapter_wires_env_candidates_from_profile(machine, tmp_path):
    """The engine's env first, the shared default last, and nothing invented.

    This used to assert ``[gpaw, ase, default]``. ``twain-envs/ase`` has no spec
    and has never existed -- ASE is pip-installable and ships inside every engine
    env -- so it was a candidate the payload could only ever skip. Candidates are
    now the toolset entries that a scripts/ris/envs spec actually provisions,
    keyed by conda package name (see statemachine._cluster_env_names).
    """
    plan = {"selected_method": {"tool_name": "ASE", "calculator": "GPAW",
                                "libraries": ["ASE", "Pymatgen"]},
            "slurm_request": {"cpu_count": 4, "gpu_count": 0,
                              "max_time": 0.5, "ram": 8}}
    path = tmp_path / "execution_plan_seed.json"
    path.write_text(json.dumps(plan))
    machine.context.artifacts["execution_plan"] = str(path)

    adapter = machine._build_slurm_adapter()
    root = "/storage2/fs1/mdan/Active/dtrc2026-workshop/twain-envs"
    assert adapter.env_pythons == [
        f"{root}/gpaw/bin/python",
        f"{root}/default/bin/python",
    ]


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


# ── a provisioned env must not be rejected for a PATH it never got ────────────

class TestEnvActivationInThePayload:
    """Slurm job on 5c658c50 reported dependency_error: every env "failed the
    bundle's smoke test", including twain-envs/nwchem, which was provisioned and
    correct. An ASE calculator shells out to its engine binary, and invoking
    <prefix>/bin/python by absolute path never activates the env -- so nwchem was
    not on PATH and the probe failed for a reason unrelated to the env.
    """

    def _payload(self, tmp_path, **kw):
        from execution_adapter.cluster_profile import ClusterProfile
        from execution_adapter.slurm_execution_adapter import SlurmExecutionAdapter
        (tmp_path / "requirements.txt").write_text("ase\n")
        (tmp_path / "inline_tests.py").write_text("pass\n")
        adapter = SlurmExecutionAdapter(
            ClusterProfile.load("compute2"),
            env_pythons=["/envs/nwchem/bin/python", "/envs/default/bin/python"])
        return adapter._env_payload(tmp_path, install_deps=True, run_smoke=True, **kw)

    def test_the_env_bin_goes_on_path(self, tmp_path):
        payload = self._payload(tmp_path)
        assert 'export PATH="$BIN:$PATH"' in payload

    def test_activation_hooks_are_sourced(self, tmp_path):
        """NWChem finds its basis sets via NWCHEM_BASIS_LIBRARY, set only by an
        activate.d hook -- the same gap that broke the local adapter."""
        payload = self._payload(tmp_path)
        assert "etc/conda/activate.d" in payload
        assert 'CONDA_PREFIX="$PREFIX" . "$hook"' in payload

    def test_each_candidate_is_probed_in_a_subshell(self, tmp_path):
        """A rejected candidate must not leave its PATH behind for the next one."""
        payload = self._payload(tmp_path)
        assert '( twain_use_env "$CAND"' in payload

    def test_the_env_is_set_up_before_anything_runs(self, tmp_path):
        # Guarded by LAYERED: a venv layered on an engine env activated that env
        # already, and re-activating on the venv would put .venv/bin ahead of the
        # engine's bin and drop CONDA_PREFIX.
        lines = self._payload(tmp_path).splitlines()
        activate = next(i for i, l in enumerate(lines)
                        if 'twain_use_env "$PY"' in l)
        smoke = next(i for i, l in enumerate(lines) if l.strip() == '"$PY" inline_tests.py')
        run = next(i for i, l in enumerate(lines) if "main.py" in l)
        assert activate < smoke < run

    def test_the_payload_is_valid_shell(self, tmp_path):
        import subprocess
        proc = subprocess.run(["bash", "-n"], input=self._payload(tmp_path),
                              text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr


class TestLayeredVenvFallback:
    """When no single env satisfies the toolset, layer pip on the engine env.

    Slurm job 2580169: the plan was quacc+ASE+Psi4. quacc is pip-only and in no
    cluster env; psi4 is conda-only and in exactly one. No env can host both, and
    the old fallback built a BARE venv, where pip correctly refuses the conda-only
    psi4 -- so the gate failed with "MISSING DEPENDENCY: psi4" after a four-minute
    queue wait. Layering the venv on the engine env satisfies both halves.
    """

    def _payload(self, tmp_path, *, requirements="quacc\nnumpy==1.26.4\n"):
        bundle = tmp_path / "bundle"
        bundle.mkdir(exist_ok=True)
        (bundle / "main.py").write_text("print('x')\n")
        (bundle / "inline_tests.py").write_text("print('smoke')\n")
        (bundle / "requirements.txt").write_text(requirements)
        adapter = SlurmExecutionAdapter(
            ClusterProfile.load("compute2"), host="",
            env_pythons=["/envs/psi4/bin/python", "/envs/default/bin/python"])
        return adapter._env_payload(bundle, install_deps=True, run_smoke=True)

    def test_the_venv_inherits_the_engine_env(self, tmp_path):
        payload = self._payload(tmp_path)
        assert '"$BASE" -m venv --system-site-packages .venv' in payload

    def test_the_base_is_the_first_env_that_exists(self, tmp_path):
        """Candidates are engine-first, so the first existing one is the best base."""
        payload = self._payload(tmp_path)
        assert '[ -n "$BASE" ] || BASE="$CAND"' in payload

    def test_the_base_env_is_activated_before_the_venv_is_built(self, tmp_path):
        """Otherwise a compiled package could build against the wrong toolchain."""
        lines = self._payload(tmp_path).splitlines()
        activate = next(i for i, l in enumerate(lines) if 'twain_use_env "$BASE"' in l)
        build = next(i for i, l in enumerate(lines) if '-m venv --system-site-packages' in l)
        assert activate < build

    def test_only_missing_distributions_are_installed(self, tmp_path):
        """Installing the whole file would let a pin shadow the conda build.

        requirements.txt pins numpy/ase for reproducibility; forcing those into a
        venv layered on an engine compiled against the conda numpy breaks the
        engine in a way that reads like a code bug.
        """
        payload = self._payload(tmp_path)
        assert "-r .twain-missing.txt" in payload
        assert "md.distribution(name)" in payload
        # The unfiltered install must NOT be what the layered path runs.
        layered = payload.split('if [ -n "$BASE" ]; then', 1)[1].split("else", 1)[0]
        assert "-r requirements.txt" not in layered

    def test_the_engine_bin_stays_ahead_of_nothing_important(self, tmp_path):
        """The venv python wins for imports; the engine's bin stays on PATH."""
        payload = self._payload(tmp_path)
        assert 'export PATH="$PWD/.venv/bin:$PATH"' in payload
        assert 'PY="$PWD/.venv/bin/python"' in payload

    def test_a_layered_env_is_not_reactivated(self, tmp_path):
        """twain_use_env on the venv would drop the engine's CONDA_PREFIX."""
        payload = self._payload(tmp_path)
        assert 'if [ -z "$LAYERED" ]; then twain_use_env "$PY"; fi' in payload

    def test_a_bare_venv_is_still_the_last_resort(self, tmp_path):
        """With no provisioned env at all, the old behaviour must remain."""
        payload = self._payload(tmp_path)
        tail = payload.split('  else', 1)[1]
        assert "python3 -m venv .venv" in tail

    def test_what_is_layered_is_logged(self, tmp_path):
        """A silent cap on coverage reads as 'it worked'."""
        assert "[env] layering on $BASE" in self._payload(tmp_path)

    def test_the_payload_is_valid_shell(self, tmp_path):
        import subprocess
        proc = subprocess.run(["bash", "-n"], input=self._payload(tmp_path),
                              text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr

    def test_a_stale_venv_is_removed_before_rebasing(self, tmp_path):
        """`python -m venv` reuses an existing dir and does NOT rebase it.

        Found the hard way while verifying this on the cluster: a .venv left by an
        earlier attempt kept its original base interpreter, so the toolset was
        satisfied by the wrong python entirely and the smoke passed for the wrong
        reason. In a reused workdir that is a run against packages nobody chose.
        """
        payload = self._payload(tmp_path)
        lines = payload.splitlines()
        remove = next(i for i, l in enumerate(lines) if l.strip() == "rm -rf .venv")
        create = next(i for i, l in enumerate(lines)
                      if "-m venv --system-site-packages" in l)
        assert remove < create
