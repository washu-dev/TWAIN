"""Slurm execution adapter (module 08).

Submits a prepared run to a Slurm cluster (e.g. WashU RIS Compute2) and tracks
it to completion. It is the HPC sibling of the planned local execution adapter
(backlog Story 5.2): both consume what code-generation (module 06) produces and
return an :class:`ExecutionResult` for the result interpreter (module 10).

Design notes
------------
* **Decoupled from codegen.** The adapter takes a small :class:`JobSpec`
  (entrypoint command + working directory + optional container/modules), *not*
  module 06's ``RunBundle`` type. When codegen lands, ``execute()`` builds a
  ``JobSpec`` from the ``RunBundle`` plus ``ExecutionPlan.slurm_request`` and
  calls this adapter -- no shared type, just a thin contract.
* **Resources come from the plan.** CPU/GPU/RAM/wall-time are taken from the
  :class:`SlurmRequest` already synthesized during PLAN; the adapter only maps
  them onto ``#SBATCH`` directives and a concrete partition.
* **Offline-testable.** All cluster interaction goes through an injected
  ``runner(argv) -> CommandResult`` callable. Tests pass a fake runner; in
  production it shells out (locally on a login node, or over SSH). No network or
  live cluster is needed to unit-test rendering, partition selection, and the
  submit/poll/cancel parsing.
* **Submit-and-poll, not block.** ``submit()`` returns immediately with a job id
  and ``poll()``/``wait()`` query ``squeue``/``sacct``. A multi-hour HPC job must
  not be held open inside the orchestrator's EXECUTE timeout -- the caller
  checkpoints a "running on cluster" state and polls on resume.
"""
from __future__ import annotations

import math
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Union

from execution_adapter.cluster_profile import ClusterProfile, Partition

# A SlurmRequest carries the resource ask (cpu_count, gpu_count, max_time, ram).
# Imported via the plan_synthesizer alias so the adapter reuses the plan's type.
from plan_synthesizer.execution_plan import SlurmRequest


# --------------------------------------------------------------------------- IO
@dataclass
class CommandResult:
    """Outcome of running one cluster command (the injected runner's return)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]


def format_walltime(minutes: float) -> str:
    """Render ``minutes`` as a Slurm ``--time`` string (``HH:MM:SS`` / ``D-HH:MM:SS``).

    Compute2 partitions report limits like ``15-00:00:00`` and ``30:00``; emitting
    an explicit ``D-HH:MM:SS`` form avoids the ambiguity of a bare integer.
    """
    total = max(1, math.ceil(float(minutes)))
    days, rem = divmod(total, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}-{hours:02d}:{mins:02d}:00"
    return f"{hours:02d}:{mins:02d}:00"


def subprocess_runner(argv: Sequence[str]) -> CommandResult:
    """Default runner: execute ``argv`` locally (use on a login node)."""
    proc = subprocess.run(list(argv), capture_output=True, text=True)
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def ssh_runner(host: str, *, user: Optional[str] = None,
               modules: Optional[Sequence[str]] = None) -> Runner:
    """Build a runner that executes commands on ``host`` over SSH.

    The remote command is the safely-quoted ``argv`` so it runs unchanged on the
    login node (which is where ``sbatch``/``squeue`` live for Compute2).

    The command runs inside ``bash -lc`` with the profile's ``modules``
    explicitly loaded: a plain ``ssh host sbatch ...`` executes in a
    non-interactive shell whose PATH depends on each user's personal dotfiles
    (and zsh users' ``.zshrc`` is not even read there), so ``sbatch`` is only
    on PATH for users who happen to load the Slurm module themselves. A bash
    login shell initializes Lmod from the system profile for every user, and
    the explicit ``module load`` makes the scheduler commands available
    deterministically -- no per-user shell setup required.
    """
    target = f"{user}@{host}" if user else host
    prefix = (f"module load {' '.join(modules)} >/dev/null 2>&1 || true; "
              if modules else "")

    def _run(argv: Sequence[str]) -> CommandResult:
        remote = " ".join(shlex.quote(a) for a in argv)
        wrapped = f"bash -lc {shlex.quote(prefix + remote)}"
        proc = subprocess.run(
            ["ssh", target, wrapped], capture_output=True, text=True
        )
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)

    return _run


# ------------------------------------------------------------------------- spec
@dataclass
class JobSpec:
    """What to run, independent of how resources are requested.

    ``command`` is the payload executed inside the batch script (e.g.
    ``["python", "main.py"]``); ``workdir`` is the absolute path on the cluster
    where the run bundle lives. ``modules`` overrides the profile's default Lmod
    set; ``container_image`` switches to a pyxis/enroot container run.
    """

    job_name: str
    command: Union[str, List[str]]
    workdir: Optional[str] = None
    output_path: Optional[str] = None          # default: <job_name>-%j.log
    partition: Optional[str] = None            # explicit override; else auto-selected
    account: Optional[str] = None              # override profile account
    modules: Optional[List[str]] = None        # override profile modules
    gpu_type: Optional[str] = None             # e.g. "H100"; else profile default / untyped
    container_image: Optional[str] = None
    container_mounts: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.job_name or type(self.job_name) is not str:
            raise ValueError("JobSpec job_name must be a non-empty str")
        if not self.command or not isinstance(self.command, (str, list)):
            raise ValueError("JobSpec command must be a non-empty str or list")
        if isinstance(self.command, list) and any(type(c) is not str for c in self.command):
            raise ValueError("JobSpec command list items must be str")

    def command_str(self) -> str:
        if isinstance(self.command, str):
            return self.command
        return " ".join(shlex.quote(c) for c in self.command)


# ------------------------------------------------------------------------ state
class JobState(str, Enum):
    """Normalized lifecycle state, mapped from Slurm's squeue/sacct codes."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in (
            JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.TIMEOUT
        )

    @property
    def succeeded(self) -> bool:
        return self is JobState.COMPLETED


# Slurm state codes (squeue long form / sacct) -> normalized JobState.
_SLURM_STATE_MAP = {
    "PD": JobState.PENDING, "PENDING": JobState.PENDING,
    "CF": JobState.PENDING, "CONFIGURING": JobState.PENDING,
    "R": JobState.RUNNING, "RUNNING": JobState.RUNNING,
    "CG": JobState.RUNNING, "COMPLETING": JobState.RUNNING,
    "CD": JobState.COMPLETED, "COMPLETED": JobState.COMPLETED,
    "F": JobState.FAILED, "FAILED": JobState.FAILED,
    "NF": JobState.FAILED, "NODE_FAIL": JobState.FAILED,
    "OOM": JobState.FAILED, "OUT_OF_MEMORY": JobState.FAILED,
    "CA": JobState.CANCELLED, "CANCELLED": JobState.CANCELLED,
    "TO": JobState.TIMEOUT, "TIMEOUT": JobState.TIMEOUT,
}


def parse_slurm_state(code: str) -> JobState:
    """Map a raw Slurm state token to a normalized :class:`JobState`."""
    if not code:
        return JobState.UNKNOWN
    # sacct decorates cancellations as "CANCELLED by 12345"; keep the first token.
    token = code.strip().split()[0].upper()
    return _SLURM_STATE_MAP.get(token, JobState.UNKNOWN)


@dataclass
class ExecutionResult:
    """Terminal outcome of a submitted job."""

    job_id: str
    state: JobState
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""

    @property
    def succeeded(self) -> bool:
        return self.state.succeeded and (self.exit_code in (None, 0))


class SlurmError(RuntimeError):
    """Raised when a Slurm command fails (non-zero exit) or output is unparseable."""


# Every knob that decides how many threads a numeric library opens. They are set
# together, and anything that hands the allocation to MPI ranks must re-pin all
# of them together: OpenBLAS reads OPENBLAS_NUM_THREADS in preference to
# OMP_NUM_THREADS, so pinning OMP alone leaves BLAS at the full core count.
THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)

# ----------------------------------------------------------------------- adapter
_SUBMITTED_RE = re.compile(r"Submitted batch job (\d+)")


class SlurmAdapter:
    """Render, submit, and track Slurm jobs against a :class:`ClusterProfile`."""

    def __init__(self, profile: ClusterProfile, runner: Optional[Runner] = None):
        self.profile = profile
        self.runner = runner or subprocess_runner

    # -------------------------------------------------------- partition selection
    def select_partition(self, request: SlurmRequest, override: Optional[str] = None) -> Partition:
        """Choose a partition for ``request`` (or validate an explicit ``override``).

        Preference order, all constrained by GPU need and wall-clock limit:
          1. an explicit override (validated against the request),
          2. the dedicated GPU partition when GPUs are requested,
          3. the short partition when the job fits its limit (faster scheduling),
          4. the default partition,
          5. any partition that admits the job.
        Raises :class:`SlurmError` if nothing admits the request.
        """
        needs_gpu = request.gpu_count > 0
        minutes = float(request.max_time)

        if override is not None:
            part = self.profile.partition(override)  # KeyError if unknown
            if not part.admits(minutes=minutes, needs_gpu=needs_gpu):
                raise SlurmError(
                    f"partition {override!r} cannot satisfy the request "
                    f"(gpu={needs_gpu}, minutes={minutes})"
                )
            return part

        candidates: List[str] = []
        if needs_gpu and self.profile.gpu_partition:
            candidates.append(self.profile.gpu_partition)
        if not needs_gpu and self.profile.short_partition:
            candidates.append(self.profile.short_partition)
        if self.profile.default_partition:
            candidates.append(self.profile.default_partition)
        candidates += [p.name for p in self.profile.partitions]

        for name in candidates:
            part = self.profile.partition(name)
            if part.admits(minutes=minutes, needs_gpu=needs_gpu):
                return part

        raise SlurmError(
            f"no partition on cluster {self.profile.name!r} admits the request "
            f"(gpu={needs_gpu}, minutes={minutes})"
        )

    # ------------------------------------------------------------ script rendering
    def render_sbatch(self, job: JobSpec, request: SlurmRequest) -> str:
        """Render the ``#SBATCH`` batch script for ``job`` under ``request``.

        Resource directives come from ``request``; account/partition/modules fall
        back to the cluster profile. Wall time (``request.max_time``) is treated
        as minutes, matching Slurm's ``--time=<minutes>``.
        """
        partition = self.profile.partition(job.partition) if job.partition \
            else self.select_partition(request)
        account = job.account or self.profile.account
        output = job.output_path or f"{job.job_name}-%j.log"

        lines = ["#!/bin/bash"]
        directives = [
            ("--job-name", job.job_name),
            ("--output", output),
            ("--partition", partition.name),
            ("--account", account),
            ("--ntasks", "1"),
            ("--cpus-per-task", str(request.cpu_count)),
            ("--mem", f"{request.ram}M"),
            ("--time", format_walltime(request.max_time)),
        ]
        if request.gpu_count > 0:
            # Compute2 uses --gres=gpu[:type]:N (H100s available); a type, when
            # set on the job or the cluster profile, pins the GPU model.
            gpu_type = job.gpu_type or self.profile.gpu_type
            gres = f"gpu:{gpu_type}:{request.gpu_count}" if gpu_type \
                else f"gpu:{request.gpu_count}"
            directives.append(("--gres", gres))
        for flag, value in directives:
            lines.append(f"#SBATCH {flag}={value}")

        lines.append("")
        modules = job.modules if job.modules is not None else list(self.profile.modules)
        if request.gpu_count > 0:
            modules += [m for m in self.profile.gpu_modules if m not in modules]
        if modules:
            # Best-effort: some profile modules (e.g. `slurm`) only load on
            # login nodes -- on a compute node Lmod refuses them with a loud
            # error, which must neither fail the job nor pollute its log.
            lines.append(
                f"module load {' '.join(modules)} >/dev/null 2>&1 || true")
        # Use every allocated core: scientific Python parallelizes through
        # OpenMP/BLAS threading, but those libraries default to 1 thread (or to
        # the node's full core count, oversubscribing a shared node) unless told
        # otherwise. Job-specific env can still override any of these.
        for var in THREAD_ENV_VARS:
            if var not in job.env:
                lines.append(f'export {var}="${{SLURM_CPUS_PER_TASK:-1}}"')
        for key, value in job.env.items():
            lines.append(f"export {key}={shlex.quote(str(value))}")
        if job.workdir:
            lines.append(f"cd {shlex.quote(job.workdir)}")

        payload = job.command_str()
        if job.container_image:
            parts = [f"srun --container-image={shlex.quote(job.container_image)}"]
            if job.workdir:
                parts.append(f"--container-workdir={shlex.quote(job.workdir)}")
            for mount in job.container_mounts:
                parts.append(f"--container-mounts={shlex.quote(mount)}")
            parts.append(payload)
            lines.append(" ".join(parts))
        else:
            lines.append(payload)

        return "\n".join(lines) + "\n"

    # ----------------------------------------------------------------- operations
    def _run(self, argv: Sequence[str]) -> CommandResult:
        result = self.runner(argv)
        if result.returncode != 0:
            raise SlurmError(
                f"command {' '.join(argv)!r} failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def submit(self, script_path: str) -> str:
        """Submit a batch script via ``sbatch`` and return the parsed job id."""
        result = self._run(["sbatch", script_path])
        match = _SUBMITTED_RE.search(result.stdout)
        if not match:
            raise SlurmError(f"could not parse job id from sbatch output: {result.stdout!r}")
        return match.group(1)

    def poll(self, job_id: str) -> JobState:
        """Return the current :class:`JobState` for ``job_id``.

        Queries ``squeue`` first (the job is still queued/running); if it's no
        longer there it has finished, so fall back to ``sacct`` for the final
        state. An empty result from both means the job is unknown.
        """
        squeue = self._run(["squeue", "--job", job_id, "--noheader", "--format=%T"])
        token = squeue.stdout.strip()
        if token:
            return parse_slurm_state(token)

        sacct = self._run(
            ["sacct", "-j", job_id, "--noheader", "--parsable2", "--format=State"]
        )
        first = sacct.stdout.strip().splitlines()
        if first:
            return parse_slurm_state(first[0])
        return JobState.UNKNOWN

    #: Seconds of *consecutive* failed polls tolerated before giving up.
    #: A poll runs squeue/sacct over SSH, so a VPN drop or login-node blip
    #: makes it fail while the job itself keeps running on the cluster -- one
    #: bad poll must not abort a multi-hour wait. Submission already proved the
    #: SSH path works, so sustained failure here means lost connectivity.
    CONTACT_LOSS_TOLERANCE = 30 * 60.0

    def wait(
        self,
        job_id: str,
        *,
        poll_interval: float = 30.0,
        max_wait: Optional[float] = None,
        on_state: Optional[Callable[[JobState], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> JobState:
        """Poll ``job_id`` until it reaches a terminal state (bounded).

        ``max_wait`` (seconds) bounds the loop; on expiry the job is left running
        and a :class:`SlurmError` is raised so the caller can checkpoint a
        "running on cluster" state and poll again on resume rather than blocking
        forever. Transient poll failures (e.g. the VPN dropped, so squeue over
        SSH fails) are tolerated for up to :data:`CONTACT_LOSS_TOLERANCE`
        consecutive seconds before raising. ``on_state`` is called on every
        observed state (progress events); ``sleep`` is injectable so tests run
        instantly.
        """
        waited = 0.0
        contact_lost = 0.0
        last_state: Optional[JobState] = None
        while True:
            try:
                state = self.poll(job_id)
                contact_lost = 0.0
            except SlurmError as exc:
                # Job status unknown, not bad: keep waiting on the assumption
                # the job is still running, unless contact stays lost too long.
                contact_lost += poll_interval
                if contact_lost > self.CONTACT_LOSS_TOLERANCE:
                    raise SlurmError(
                        f"lost contact with the cluster while polling job "
                        f"{job_id} (polls failing for {int(contact_lost)}s, "
                        f"last error: {exc}); the job was NOT cancelled -- "
                        f"reconnect (VPN?) and check it with squeue"
                    ) from exc
                state = None
            if state is not None:
                last_state = state
                if on_state is not None:
                    on_state(state)
                if state.is_terminal:
                    return state
            if max_wait is not None and waited >= max_wait:
                seen = last_state.value if last_state is not None else "unknown"
                raise SlurmError(
                    f"job {job_id} still {seen} after {int(waited)}s; "
                    f"checkpoint and poll again later (the job keeps running)"
                )
            sleep(poll_interval)
            waited += poll_interval

    def accounting(self, job_id: str) -> Dict[str, str]:
        """Final accounting for ``job_id`` from ``sacct`` (State/ExitCode/Elapsed/MaxRSS).

        Returns the first (parent) record's fields keyed by name; values are the
        raw sacct strings (e.g. ``ExitCode`` is ``"0:0"``). Empty dict when sacct
        has no record (job too recent or accounting disabled).
        """
        fields = ["State", "ExitCode", "Elapsed", "MaxRSS", "Partition"]
        result = self._run(
            ["sacct", "-j", job_id, "--noheader", "--parsable2",
             f"--format={','.join(fields)}"]
        )
        lines = result.stdout.strip().splitlines()
        if not lines:
            return {}
        values = lines[0].split("|")
        return dict(zip(fields, values))

    def exit_code(self, job_id: str) -> Optional[int]:
        """The job's exit code from accounting (``"1:0"`` -> 1), or None."""
        raw = self.accounting(job_id).get("ExitCode", "")
        match = re.match(r"(\d+):", raw)
        return int(match.group(1)) if match else None

    def cancel(self, job_id: str) -> None:
        """Cancel ``job_id`` via ``scancel``."""
        self._run(["scancel", job_id])
