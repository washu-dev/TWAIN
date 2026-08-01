"""Unit tests for the Slurm execution adapter (module 08).

All cluster interaction goes through an injected fake runner, so these tests
exercise the real rendering / partition-selection / submit-poll-cancel logic
fully offline -- no SSH, no live Compute2 cluster.

Run from the repo root with:  pixi run pytest tests/unit/test_slurm_adapter.py
"""
import pytest

from execution_adapter.cluster_profile import ClusterProfile, Partition, CLUSTERS_DIR
from execution_adapter.slurm_adapter import (
    SlurmAdapter, JobSpec, JobState, CommandResult, SlurmError,
    parse_slurm_state,
)
from plan_synthesizer.execution_plan import SlurmRequest


# ── fixtures ──────────────────────────────────────────────────────────────────

def _profile():
    return ClusterProfile(
        name="compute2",
        login_nodes=["c2-login-001.ris.wustl.edu"],
        account="compute2-mdan",
        accounts=["compute2-mdan", "compute2-workshop"],
        modules=["ris", "slurm"],
        gpu_modules=["py-torch"],
        default_partition="general-cpu",
        gpu_partition="general-gpu",
        short_partition="general-short",
        partitions=[
            {"name": "general-cpu", "max_minutes": 21600, "gpus": False},
            {"name": "general-gpu", "max_minutes": 21600, "gpus": True},
            {"name": "general-bigmem", "max_minutes": 21600, "gpus": False},
            {"name": "general-short", "max_minutes": 30, "gpus": True},
        ],
    )


class FakeRunner:
    """Records argv calls and returns scripted CommandResults by command name."""

    def __init__(self, responses):
        self.responses = responses          # {"sbatch": CommandResult, ...}
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.responses[argv[0]]


# ── cluster profile ─────────────────────────────────────────────────────────

def test_compute2_profile_loads_from_configs():
    profile = ClusterProfile.load("compute2")
    assert profile.account == "compute2-mdan"
    assert "compute2-workshop" in profile.accounts
    assert profile.partition("general-short").max_minutes == 30
    assert profile.partition("general-bigmem").gpus is False
    assert profile.partition("general-gpu").gpus is True
    assert (CLUSTERS_DIR / "compute2.json").is_file()


def test_profile_rejects_named_partition_not_in_list():
    with pytest.raises(ValueError):
        ClusterProfile(
            name="x", login_nodes=["h"], account="a",
            default_partition="ghost",
            partitions=[{"name": "general"}],
        )


def test_compute2_profile_carries_node_ceilings():
    # Per-node maxima (verified via sinfo on the live cluster) feed the
    # approval card's field labels and clamp what the user can request.
    profile = ClusterProfile.load("compute2")
    assert profile.max_cpus_per_node == 64
    assert profile.max_gpus_per_node == 4
    assert profile.max_ram_gb == 900


def test_profile_rejects_non_positive_node_ceilings():
    for field in ("max_cpus_per_node", "max_gpus_per_node", "max_ram_gb"):
        with pytest.raises(ValueError):
            ClusterProfile(
                name="x", login_nodes=["h"], account="a",
                partitions=[{"name": "general"}],
                **{field: 0},
            )


def test_partition_admits():
    short = Partition(name="general-short", max_minutes=30, gpus=False)
    assert short.admits(minutes=20, needs_gpu=False)
    assert not short.admits(minutes=45, needs_gpu=False)   # over the cap
    assert not short.admits(minutes=10, needs_gpu=True)    # no gpus here


# ── partition selection ──────────────────────────────────────────────────────

def test_select_partition_prefers_gpu_for_gpu_jobs():
    adapter = SlurmAdapter(_profile())
    req = SlurmRequest(cpu_count=4, gpu_count=2, max_time=120, ram=8000)
    assert adapter.select_partition(req).name == "general-gpu"


def test_select_partition_uses_short_when_it_fits():
    adapter = SlurmAdapter(_profile())
    req = SlurmRequest(cpu_count=1, gpu_count=0, max_time=20, ram=2000)
    assert adapter.select_partition(req).name == "general-short"


def test_select_partition_falls_back_to_default_when_over_short_limit():
    adapter = SlurmAdapter(_profile())
    req = SlurmRequest(cpu_count=1, gpu_count=0, max_time=600, ram=2000)
    assert adapter.select_partition(req).name == "general-cpu"


def test_select_partition_override_validated():
    adapter = SlurmAdapter(_profile())
    req = SlurmRequest(cpu_count=1, gpu_count=0, max_time=600, ram=2000)
    with pytest.raises(SlurmError):
        adapter.select_partition(req, override="general-short")  # 600 > 30


# ── sbatch rendering ─────────────────────────────────────────────────────────

def test_render_sbatch_cpu_job():
    adapter = SlurmAdapter(_profile())
    job = JobSpec(job_name="solub", command=["python", "main.py"], workdir="/storage2/fs1/me/run")
    req = SlurmRequest(cpu_count=8, gpu_count=0, max_time=45, ram=16000)
    script = adapter.render_sbatch(job, req)

    assert script.startswith("#!/bin/bash\n")
    assert "#SBATCH --job-name=solub" in script
    assert "#SBATCH --partition=general-cpu" in script     # 45 > short cap -> general-cpu
    assert "#SBATCH --account=compute2-mdan" in script
    assert "#SBATCH --cpus-per-task=8" in script
    assert "#SBATCH --mem=16000M" in script
    assert "#SBATCH --time=00:45:00" in script
    assert "--gres" not in script and "--gpus" not in script  # no GPU directive for cpu job
    # Module loads are best-effort and quiet: `slurm` refuses to load on
    # compute nodes, and that Lmod error must not fail or pollute the job.
    assert "module load ris slurm >/dev/null 2>&1 || true" in script
    assert "cd /storage2/fs1/me/run" in script
    assert script.rstrip().endswith("python main.py")
    # Threading env pins OpenMP/BLAS to the allocated cores so the payload
    # actually uses every requested CPU (GPAW etc. default to 1 thread).
    assert 'export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-1}"' in script
    assert 'export OPENBLAS_NUM_THREADS="${SLURM_CPUS_PER_TASK:-1}"' in script


def test_render_sbatch_job_env_overrides_threading_defaults():
    adapter = SlurmAdapter(_profile())
    job = JobSpec(job_name="solub", command="python main.py",
                  env={"OMP_NUM_THREADS": "2"})
    req = SlurmRequest(cpu_count=8, gpu_count=0, max_time=45, ram=16000)
    script = adapter.render_sbatch(job, req)
    assert "export OMP_NUM_THREADS=2" in script
    assert 'export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-1}"' not in script
    # Untouched vars still default to the allocation.
    assert 'export MKL_NUM_THREADS="${SLURM_CPUS_PER_TASK:-1}"' in script


def test_render_sbatch_gpu_job_adds_gres_and_modules():
    adapter = SlurmAdapter(_profile())
    job = JobSpec(job_name="md", command="python md.py")
    req = SlurmRequest(cpu_count=4, gpu_count=2, max_time=120, ram=32000)
    script = adapter.render_sbatch(job, req)

    assert "#SBATCH --partition=general-gpu" in script
    assert "#SBATCH --gres=gpu:2" in script                 # untyped by default
    assert "py-torch" in script                             # gpu module folded in


def test_render_sbatch_gpu_type_pins_model():
    adapter = SlurmAdapter(_profile())
    job = JobSpec(job_name="md", command="python md.py", gpu_type="H100")
    req = SlurmRequest(cpu_count=4, gpu_count=4, max_time=120, ram=32000)
    assert "#SBATCH --gres=gpu:H100:4" in adapter.render_sbatch(job, req)


def test_render_sbatch_formats_walltime_over_a_day():
    adapter = SlurmAdapter(_profile())
    job = JobSpec(job_name="j", command="true")
    # 10.2 minutes rounds up to 11; 1500 minutes is 1 day + 1 hour.
    short = SlurmRequest(cpu_count=1, gpu_count=0, max_time=10.2, ram=1000)
    long = SlurmRequest(cpu_count=1, gpu_count=0, max_time=1500, ram=1000)
    assert "#SBATCH --time=00:11:00" in adapter.render_sbatch(job, short)
    assert "#SBATCH --time=1-01:00:00" in adapter.render_sbatch(job, long)


def test_render_sbatch_container_uses_srun():
    adapter = SlurmAdapter(_profile())
    job = JobSpec(
        job_name="c", command="python /home/me/hello.py", workdir="/home/me",
        container_image="python:3.9.21-alpine",
        container_mounts=["/storage2/fs1/me/Active:/storage2/fs1/me/Active"],
    )
    req = SlurmRequest(cpu_count=1, gpu_count=0, max_time=10, ram=1000)
    script = adapter.render_sbatch(job, req)
    assert "srun --container-image=python:3.9.21-alpine" in script
    assert "--container-workdir=/home/me" in script
    assert "--container-mounts=" in script


# ── submit / poll / cancel ───────────────────────────────────────────────────

def test_submit_parses_job_id():
    runner = FakeRunner({"sbatch": CommandResult(0, "Submitted batch job 2451\n")})
    adapter = SlurmAdapter(_profile(), runner=runner)
    assert adapter.submit("/tmp/job.slurm") == "2451"
    assert runner.calls[-1] == ["sbatch", "/tmp/job.slurm"]


def test_submit_raises_on_unparseable_output():
    runner = FakeRunner({"sbatch": CommandResult(0, "weird output")})
    adapter = SlurmAdapter(_profile(), runner=runner)
    with pytest.raises(SlurmError):
        adapter.submit("/tmp/job.slurm")


def test_submit_raises_on_nonzero_exit():
    runner = FakeRunner({"sbatch": CommandResult(1, "", "sbatch: error: invalid account")})
    adapter = SlurmAdapter(_profile(), runner=runner)
    with pytest.raises(SlurmError):
        adapter.submit("/tmp/job.slurm")


def test_poll_running_from_squeue():
    runner = FakeRunner({"squeue": CommandResult(0, "RUNNING\n")})
    adapter = SlurmAdapter(_profile(), runner=runner)
    assert adapter.poll("2451") == JobState.RUNNING


def test_poll_falls_back_to_sacct_when_squeue_empty():
    runner = FakeRunner({
        "squeue": CommandResult(0, "\n"),          # gone from the queue
        "sacct": CommandResult(0, "COMPLETED\n"),
    })
    adapter = SlurmAdapter(_profile(), runner=runner)
    assert adapter.poll("2451") == JobState.COMPLETED


def test_poll_unknown_when_both_empty():
    runner = FakeRunner({
        "squeue": CommandResult(0, ""),
        "sacct": CommandResult(0, ""),
    })
    adapter = SlurmAdapter(_profile(), runner=runner)
    assert adapter.poll("999") == JobState.UNKNOWN


def test_cancel_invokes_scancel():
    runner = FakeRunner({"scancel": CommandResult(0, "")})
    adapter = SlurmAdapter(_profile(), runner=runner)
    adapter.cancel("2451")
    assert runner.calls[-1] == ["scancel", "2451"]


# ── state mapping ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    ("PD", JobState.PENDING),
    ("R", JobState.RUNNING),
    ("CD", JobState.COMPLETED),
    ("COMPLETED", JobState.COMPLETED),
    ("OUT_OF_MEMORY", JobState.FAILED),
    ("TIMEOUT", JobState.TIMEOUT),
    ("CANCELLED by 12345", JobState.CANCELLED),
    ("weird", JobState.UNKNOWN),
    ("", JobState.UNKNOWN),
])
def test_parse_slurm_state(code, expected):
    assert parse_slurm_state(code) == expected


def test_jobstate_terminal_and_success_flags():
    assert JobState.COMPLETED.is_terminal and JobState.COMPLETED.succeeded
    assert JobState.FAILED.is_terminal and not JobState.FAILED.succeeded
    assert not JobState.RUNNING.is_terminal


class TestSshRunner:
    """The SSH runner must not depend on the remote user's dotfiles: sbatch is
    only on PATH after `module load`, which non-interactive shells (and zsh
    users' .zshrc) never run -- so the command is wrapped in `bash -lc` with
    the profile's modules loaded explicitly."""

    def _capture(self, monkeypatch):
        calls = []

        def fake_run(argv, capture_output, text):
            calls.append(argv)
            class P:
                returncode, stdout, stderr = 0, "", ""
            return P()

        from execution_adapter import slurm_adapter as mod
        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        return calls

    def test_wraps_in_bash_login_shell_with_module_load(self, monkeypatch):
        from execution_adapter.slurm_adapter import ssh_runner
        calls = self._capture(monkeypatch)
        run = ssh_runner("login.example.edu", user="alice",
                         modules=["ris", "slurm"])
        run(["sbatch", "/runs/job.slurm"])
        ssh_argv = calls[0]
        assert ssh_argv[:2] == ["ssh", "alice@login.example.edu"]
        remote = ssh_argv[2]
        assert remote.startswith("bash -lc ")
        assert "module load ris slurm" in remote
        assert "sbatch /runs/job.slurm" in remote

    def test_no_modules_still_uses_login_shell(self, monkeypatch):
        from execution_adapter.slurm_adapter import ssh_runner
        calls = self._capture(monkeypatch)
        run = ssh_runner("login.example.edu")
        run(["squeue", "--job", "42"])
        remote = calls[0][2]
        assert remote.startswith("bash -lc ")
        assert "module load" not in remote
