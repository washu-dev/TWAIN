"""Slurm execution adapter -- runs a RunBundle on an HPC cluster (Story 5.4).

The HPC sibling of :class:`~execution_adapter.local_adapter.LocalExecutionAdapter`
and :class:`~execution_adapter.docker_adapter.DockerExecutionAdapter`, exposing
the same ``execute(bundle, ...) -> ExecutionResult`` interface so the state
machine picks a backend without special-casing the call site.

One ``execute()`` call performs the full lifecycle against the cluster in the
:class:`~execution_adapter.cluster_profile.ClusterProfile`:

  1. **stage**   -- rsync the bundle into ``<storage_root>/twain-runs/<run_id>/``
     (:class:`~execution_adapter.staging.Stager`);
  2. **render**  -- build the ``#SBATCH`` script from the plan's
     :class:`~plan_synthesizer.execution_plan.SlurmRequest` (partition selection,
     ``--gres``, walltime/memory formats -- :class:`SlurmAdapter`);
  3. **submit**  -- ``sbatch`` on the login node (over SSH or locally);
  4. **wait**    -- bounded ``squeue``/``sacct`` polling. If the budget expires
     the job is LEFT RUNNING and the result says how to check on it -- a
     multi-hour job must not be silently killed because the orchestrator's
     EXECUTE budget is shorter;
  5. **fetch**   -- rsync outputs (``results.csv``, the job log) back into the
     local workspace and map ``sacct`` accounting (Elapsed/MaxRSS) onto the
     result's resource fields.

The job payload creates a venv from the bundle's ``requirements.txt`` when
``install_deps`` is on (compute nodes have no TWAIN pixi env), or runs inside a
pyxis container when ``container_image`` is set. All cluster interaction goes
through injected runners (login-node commands + rsync/ssh transport), so the
whole lifecycle is unit-testable offline; production needs the WashU VPN + an
SSH key for the login node.
"""
from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import List, Optional, Union

try:  # pragma: no cover - import shim (mirrors local_adapter)
    from execution_adapter.cluster_profile import ClusterProfile
    from execution_adapter.execution_result import ExecutionResult, ExecutionStatus
    from execution_adapter.local_adapter import _DEP_ERROR_MARKERS, _safe_name
    from execution_adapter.slurm_adapter import (
        JobSpec,
        JobState,
        Runner,
        SlurmAdapter,
        SlurmError,
        ssh_runner,
        subprocess_runner,
    )
    from execution_adapter.staging import Stager, StagingError
except ImportError:  # pragma: no cover
    from cluster_profile import ClusterProfile
    from execution_result import ExecutionResult, ExecutionStatus
    from local_adapter import _DEP_ERROR_MARKERS, _safe_name
    from slurm_adapter import (
        JobSpec,
        JobState,
        Runner,
        SlurmAdapter,
        SlurmError,
        ssh_runner,
        subprocess_runner,
    )
    from staging import Stager, StagingError

from plan_synthesizer.execution_plan import SlurmRequest

# A generous default: DFT jobs routinely take an hour; beyond this the job is
# left running on the cluster and the result reports how to check on it.
DEFAULT_MAX_WAIT = 7200.0
DEFAULT_POLL_INTERVAL = 30.0

_STATE_TO_STATUS = {
    JobState.COMPLETED: ExecutionStatus.SUCCESS,
    JobState.TIMEOUT: ExecutionStatus.TIMEOUT,
    JobState.FAILED: ExecutionStatus.FAILED,
    JobState.CANCELLED: ExecutionStatus.FAILED,
    JobState.UNKNOWN: ExecutionStatus.FAILED,
}


def _elapsed_seconds(text: str) -> float:
    """``sacct`` Elapsed (``[D-]HH:MM:SS``) -> seconds (0.0 when unparseable)."""
    match = re.match(r"(?:(\d+)-)?(\d+):(\d+):(\d+)$", (text or "").strip())
    if not match:
        return 0.0
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return float(((days * 24 + hours) * 60 + minutes) * 60 + seconds)


def _maxrss_mb(text: str) -> Optional[float]:
    """``sacct`` MaxRSS (``123456K`` / ``1.5G`` / ``800M``) -> MB, or None."""
    match = re.match(r"([\d.]+)([KMGT]?)$", (text or "").strip(), re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    factor = {"": 1 / 1024, "K": 1 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 ** 2}
    return round(value * factor[match.group(2).upper()], 3)


class _AbortRequested(Exception):
    """Internal: the terminate seam fired while polling a Slurm job."""


class SlurmExecutionAdapter:
    """Stage, submit, poll, and fetch one RunBundle as a Slurm job."""

    def __init__(
        self,
        profile: Optional[ClusterProfile] = None,
        *,
        request: Optional[SlurmRequest] = None,
        host: Optional[str] = None,
        user: Optional[str] = None,
        workspace_root: Optional[Union[str, Path]] = None,
        container_image: Optional[str] = None,
        partition: Optional[str] = None,
        max_wait: float = DEFAULT_MAX_WAIT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        cluster_runner: Optional[Runner] = None,
        transfer_runner: Optional[Runner] = None,
        env_pythons: Optional[List[str]] = None,
        sleep=None,
        should_abort=None,
    ):
        """``cluster_runner`` executes sbatch/squeue/sacct (defaults to SSH to the
        profile's first login node, or locally when ``host`` is falsy -- i.e. the
        process already runs on a login node). ``transfer_runner`` executes the
        rsync/ssh staging commands locally. Both are injectable for tests.

        ``env_pythons`` lists pre-provisioned interpreters on cluster storage
        (e.g. ``<envs_root>/gpaw/bin/python``), tried in order at job start;
        the first that exists *and passes the bundle's smoke test* is used
        instead of building a venv -- required for compiled calculators (GPAW
        needs libxc headers) that pip cannot build on bare compute nodes."""
        self.profile = profile or ClusterProfile.load("compute2")
        # Default resource ask when the plan carries none: a small CPU job.
        self.request = request or SlurmRequest(
            cpu_count=4, gpu_count=0, max_time=60, ram=8000)
        self.host = host if host is not None else self.profile.login_nodes[0]
        self.user = user
        self.workspace_root = str(workspace_root) if workspace_root else None
        self.container_image = container_image
        self.partition = partition
        self.max_wait = max_wait
        self.poll_interval = poll_interval
        self.env_pythons = list(env_pythons or [])
        self._sleep = sleep
        # Terminate seam: zero-arg callable polled between squeue checks; True
        # means the researcher pressed Terminate -> scancel the job and return.
        self.should_abort = should_abort

        if cluster_runner is None:
            cluster_runner = ssh_runner(
                self.host, user=user, modules=self.profile.modules,
            ) if self.host else subprocess_runner
        self.slurm = SlurmAdapter(self.profile, runner=cluster_runner)
        # host="" (already on a login node) degrades staging to local copies.
        self.stager = Stager(self.profile, host=self.host, user=user,
                             runner=transfer_runner)

    # ---------------------------------------------------------------- execute
    def execute(
        self,
        bundle,
        *,
        keep_artifacts: bool = True,
        install_deps: bool = True,
        run_smoke: bool = True,
        python_executable: Optional[str] = None,  # ignored: env fixed by the job
        timeout: Optional[float] = None,
        env: Optional[dict] = None,
        run_id: Optional[str] = None,
    ) -> ExecutionResult:
        """Run ``bundle`` on the cluster and return an :class:`ExecutionResult`.

        ``bundle`` is a path to a bundle directory (or a RunBundle with
        ``write(dir)`` -- materialized into the workspace first). ``timeout``
        bounds the *polling wait*, not the job: an expired wait leaves the job
        running and reports where to find it. ``install_deps`` builds a venv from
        requirements.txt inside the job (compute nodes have no TWAIN env);
        ``python_executable`` is accepted for interface parity but ignored.
        """
        run_id = run_id or "run"
        job_name = f"twain-{_safe_name(run_id)}"[:60]
        max_wait = timeout if timeout is not None else self.max_wait
        tool_name = None

        # 1) materialize + render (all local) -------------------------------------
        try:
            local_dir = self._materialize(bundle, run_id)
        except (StagingError, OSError) as exc:
            return ExecutionResult(
                status=ExecutionStatus.SETUP_FAILED,
                message=f"failed to prepare the bundle: {exc}",
                tool_name=tool_name,
            )
        tool_name = getattr(bundle, "tool_name", None)
        remote_dir = self.stager.remote_run_dir(run_id)
        job = JobSpec(
            job_name=job_name,
            command=self._payload(local_dir, install_deps=install_deps,
                                  run_smoke=run_smoke),
            workdir=remote_dir,
            output_path=f"{remote_dir}/{job_name}-%j.log",
            partition=self.partition,
            container_image=self.container_image,
            container_mounts=[f"{remote_dir}:{remote_dir}"] if self.container_image else [],
            env=dict(env or {}),
        )

        # 2) stage + preflight + submit --------------------------------------------
        try:
            script = self.slurm.render_sbatch(job, self.request)
            (Path(local_dir) / "job.slurm").write_text(script, encoding="utf-8")
            self.stager.push(local_dir, run_id)
            preflight_error = self._preflight_env_check(
                remote_dir, local_dir, install_deps=install_deps,
                run_smoke=run_smoke)
            if preflight_error:
                return ExecutionResult(
                    status=ExecutionStatus.DEPENDENCY_ERROR,
                    message=preflight_error,
                    tool_name=tool_name,
                    artifacts_dir=local_dir if keep_artifacts else None,
                )
            job_id = self.slurm.submit(f"{remote_dir}/job.slurm")
        except (SlurmError, StagingError, KeyError, OSError) as exc:
            return ExecutionResult(
                status=ExecutionStatus.SETUP_FAILED,
                message=f"failed to submit the Slurm job: {exc}",
                tool_name=tool_name,
                artifacts_dir=local_dir if keep_artifacts else None,
            )

        # 3) wait (bounded) -----------------------------------------------------
        # Sleep between polls through an abort-aware wrapper: when the
        # researcher presses Terminate, stop waiting, scancel the job, and
        # report a clean "terminated" result instead of burning the wall time.
        base_sleep = self._sleep if self._sleep is not None else time.sleep

        def _abortable_sleep(seconds: float) -> None:
            if self.should_abort is not None and self.should_abort():
                raise _AbortRequested()
            base_sleep(seconds)

        try:
            wait_kwargs = dict(poll_interval=self.poll_interval, max_wait=max_wait,
                               sleep=_abortable_sleep)
            state = self.slurm.wait(job_id, **wait_kwargs)
        except _AbortRequested:
            try:
                self.slurm.cancel(job_id)
                note = f"Slurm job {job_id} was cancelled (scancel)"
            except SlurmError as exc:
                note = (f"cancelling Slurm job {job_id} failed ({exc}) -- "
                        f"cancel it manually with `scancel {job_id}`")
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                message=f"run terminated by the researcher; {note}",
                tool_name=tool_name,
                command=["sbatch", f"{remote_dir}/job.slurm"],
                artifacts_dir=local_dir if keep_artifacts else None,
            )
        except SlurmError as exc:
            # Budget expired (or contact with the cluster stayed lost) with the
            # job still queued/running: leave it alone (a multi-hour DFT run
            # must not die because our wait was shorter) and tell the researcher
            # exactly how to follow up.
            return ExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                message=(
                    f"the Slurm job is still running on {self.profile.name} "
                    f"(job id {job_id}); it was NOT cancelled. Check it with "
                    f"`squeue --job {job_id}` and fetch outputs from {remote_dir} "
                    f"when it completes. (wait ended because: {exc})"
                ),
                tool_name=tool_name,
                command=["sbatch", f"{remote_dir}/job.slurm"],
                artifacts_dir=local_dir if keep_artifacts else None,
            )

        # 4) fetch results + accounting ------------------------------------------
        try:
            self.stager.pull(run_id, local_dir)
        except StagingError as exc:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                message=f"job finished ({state.value}) but fetching outputs failed: {exc}",
                tool_name=tool_name,
                artifacts_dir=local_dir if keep_artifacts else None,
            )
        stdout = self._read_log(local_dir, job_name, job_id)
        accounting = self._safe_accounting(job_id)
        exit_code = self.slurm.exit_code(job_id) if accounting else None

        status, message = self._classify(state, exit_code, stdout, job_id)
        return ExecutionResult(
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            duration_seconds=_elapsed_seconds(accounting.get("Elapsed", "")),
            peak_memory_mb=_maxrss_mb(accounting.get("MaxRSS", "")),
            artifacts_dir=local_dir if keep_artifacts else None,
            tool_name=tool_name,
            command=["sbatch", f"{remote_dir}/job.slurm"],
            message=message,
            # Job identity + accounting for provenance / the budget tracker.
            install_log={"job_id": job_id, "cluster": self.profile.name,
                         "remote_dir": remote_dir, **accounting},
        )

    # ---------------------------------------------------------------- helpers
    def _materialize(self, bundle, run_id) -> str:
        """Ensure the bundle exists as a local directory; returns its path."""
        if hasattr(bundle, "write") and callable(bundle.write):
            import tempfile
            root = Path(self.workspace_root) if self.workspace_root \
                else Path(tempfile.gettempdir())
            root.mkdir(parents=True, exist_ok=True)
            local = root / f"slurm_{_safe_name(run_id)}"
            local.mkdir(parents=True, exist_ok=True)
            bundle.write(local)
            return str(local)
        path = Path(bundle)
        if not path.is_dir():
            raise StagingError(f"bundle path is not a directory: {path}")
        return str(path)

    # A single preflight probe's budget on the login node. Smoke tests are
    # seconds by design; importing a heavy calculator stack dominates.
    _PREFLIGHT_TIMEOUT_S = 300

    def _preflight_env_check(self, remote_dir: str, local_dir: str, *,
                             install_deps: bool, run_smoke: bool) -> Optional[str]:
        """Probe the cluster envs BEFORE sbatch; an error string means fail fast.

        A queued Slurm job is the most expensive place to discover "no
        environment can run this bundle" (observed: an xtb bundle failed every
        pre-provisioned env's smoke probe, then the job's pip fallback died on
        the conda-only xtb-python -- all after staging + a queue wait). The
        preflight reuses the job's own selection logic: run the bundle's smoke
        test under each candidate env, from the login node, where the shared
        storage is already mounted. One pass -> the job will find a working
        env, submit. All fail -> ask pip's resolver whether the fallback can
        even install the requirements; only a definite "no such distribution"
        verdict blocks submission with an actionable message. Anything
        ambiguous (no smoke test, flaky probe, old pip) fails open -- the job
        itself remains the authority.
        """
        if not self.env_pythons:
            return None
        bundle = Path(local_dir)
        if not (run_smoke and (bundle / "inline_tests.py").is_file()):
            return None
        rd = shlex.quote(remote_dir)
        for env_python in self.env_pythons:
            py = shlex.quote(env_python)
            result = self.slurm.runner([
                "bash", "-c",
                f"cd {rd} && [ -x {py} ] && "
                f"timeout {self._PREFLIGHT_TIMEOUT_S} {py} inline_tests.py",
            ])
            if result.returncode == 0:
                return None  # this env serves the bundle; the job will find it
        if not (install_deps and (bundle / "requirements.txt").is_file()):
            return None  # no pip fallback to vet; let the job report precisely
        result = self.slurm.runner([
            "bash", "-c",
            f"cd {rd} && python3 -m pip install --dry-run -r requirements.txt",
        ])
        blob = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        low = blob.lower()
        if result.returncode != 0 and (
                "no matching distribution" in low
                or "could not find a version" in low):
            tail = "\n".join(blob.splitlines()[-4:])
            envs = ", ".join(self.env_pythons)
            return (
                "no runnable environment for this bundle on "
                f"{self.profile.name}: every pre-provisioned env failed the "
                f"bundle's smoke test (tried: {envs}), and pip cannot install "
                f"its requirements there:\n{tail}\n"
                "Provision or extend a shared env for this calculator "
                "(scripts/ris/provision_envs.sh; specs in scripts/ris/envs/ "
                "-- see runner/README.md), then rerun."
            )
        return None

    def _payload(self, local_dir, *, install_deps: bool, run_smoke: bool) -> str:
        """The shell payload the batch script runs inside the remote run dir.

        Containers bring their own stack (pyxis mounts the run dir). Otherwise
        the job prefers a pre-provisioned environment from ``env_pythons``
        (checked in order at runtime -- compiled calculators like GPAW can't be
        pip-built on compute nodes), falling back to a venv from
        requirements.txt. Smoke tests (inline_tests.py) run first so a missing
        dependency fails in seconds, not after queueing the real run.
        """
        bundle = Path(local_dir)
        if not self.container_image and self.env_pythons:
            return self._env_payload(bundle, install_deps=install_deps,
                                     run_smoke=run_smoke)
        steps: List[str] = []
        if self.container_image:
            python = "python3"
        elif install_deps and (bundle / "requirements.txt").is_file():
            steps.append("python3 -m venv .venv")
            steps.append(".venv/bin/python -m pip install -q --upgrade pip")
            steps.append(".venv/bin/python -m pip install -q -r requirements.txt")
            python = ".venv/bin/python"
        else:
            python = "python3"
        if run_smoke and (bundle / "inline_tests.py").is_file():
            steps.append(f"{python} inline_tests.py")
        steps.append(f"{python} main.py")
        return " && ".join(steps)

    def _env_payload(self, bundle: Path, *, install_deps: bool,
                     run_smoke: bool) -> str:
        """Multi-line payload: pick the first *usable* env python, else venv.

        Existence is checked on the compute node at job start (``[ -x ... ]``)
        because the adapter can't cheaply stat cluster storage from here. An
        env that merely exists is not enough: the candidate list always ends
        with ``<envs_root>/default``, which exists but only carries the common
        stack -- accepting it blindly skips the pip fallback and any tool it
        lacks (e.g. OpenMM) dies at the smoke gate. So each candidate is
        probed with the bundle's own smoke test (quietly) and only an env
        that passes is selected; when none does, the venv+pip path takes over.
        ``set -e`` keeps the fail-fast behavior of the ``&&`` chain.
        """
        have_smoke = run_smoke and (bundle / "inline_tests.py").is_file()
        candidates = " ".join(shlex.quote(p) for p in self.env_pythons)
        lines = [
            "set -e",
            'PY=""',
            f"for CAND in {candidates}; do",
            '  [ -x "$CAND" ] || continue',
        ]
        if have_smoke:
            lines.append('  if "$CAND" inline_tests.py >/dev/null 2>&1; '
                         'then PY="$CAND"; break; fi')
        else:
            lines.append('  PY="$CAND"; break')
        lines += [
            "done",
            'if [ -z "$PY" ]; then',
        ]
        if install_deps and (bundle / "requirements.txt").is_file():
            lines += [
                "  python3 -m venv .venv",
                "  .venv/bin/python -m pip install -q --upgrade pip",
                "  .venv/bin/python -m pip install -q -r requirements.txt",
                '  PY=".venv/bin/python"',
            ]
        else:
            lines.append('  PY="python3"')
        lines.append("fi")
        if run_smoke and (bundle / "inline_tests.py").is_file():
            lines.append('"$PY" inline_tests.py')
        # Run the real payload under MPI when the selected env ships mpirun
        # (e.g. the openmpi GPAW build): DFT engines parallelize over k-points
        # via MPI ranks, which scales far better than OpenMP threading. One
        # thread per rank so ranks*threads never oversubscribes the allocation.
        lines += [
            'BIN="$(dirname "$PY")"',
            'if [ -x "$BIN/mpirun" ]; then',
            '  export OMP_NUM_THREADS=1',
            # conda-forge OpenMPI finds its runtime data (PMIx/PRRTE help
            # files, plugins) via OPAL_PREFIX, normally set by env activation
            # -- we invoke by path without activating, so set it explicitly.
            '  export OPAL_PREFIX="$(dirname "$BIN")"',
            '  export PMIX_PREFIX="$OPAL_PREFIX"',
            # The sbatch asks for --ntasks=1 --cpus-per-task=N (the right shape
            # for threaded serial runs), so OpenMPI sees ONE slot and refuses
            # -np N. Oversubscribe the slot count: the job's cgroup still pins
            # us to the N allocated cores, one rank per core in practice.
            # GPAW refuses a plain interpreter with >1 ranks ("Please use
            # gpaw python to run in parallel"). `$PY -m gpaw python` is the
            # wrapper's documented equivalent (gpaw/__main__.py selects the
            # cgpaw MPI backend) and, unlike the $BIN/gpaw entry script, can't
            # be broken by a stale relative shebang in the cluster env.
            '  if [ -x "$BIN/gpaw" ]; then LAUNCH="$PY -m gpaw python";'
            ' else LAUNCH="$PY"; fi',
            # --bind-to none: with OVERSUBSCRIBE over one nominal slot, OpenMPI
            # otherwise stacks every rank on the same core (observed ~40x
            # slowdown); unbound ranks spread over the cgroup's real cores.
            '  "$BIN/mpirun" -np "${SLURM_CPUS_PER_TASK:-1}"'
            ' --map-by :OVERSUBSCRIBE --bind-to none $LAUNCH main.py',
            "else",
            '  "$PY" main.py',
            "fi",
        ]
        return "\n".join(lines)

    def _read_log(self, local_dir, job_name: str, job_id: str) -> str:
        path = Path(local_dir) / f"{job_name}-{job_id}.log"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _safe_accounting(self, job_id: str) -> dict:
        try:
            return self.slurm.accounting(job_id)
        except SlurmError:
            return {}

    @staticmethod
    def _classify(state: JobState, exit_code: Optional[int], stdout: str,
                  job_id: str):
        if state is JobState.COMPLETED and exit_code in (None, 0):
            return ExecutionStatus.SUCCESS, f"Slurm job {job_id} completed successfully"
        if state is JobState.TIMEOUT:
            return (ExecutionStatus.TIMEOUT,
                    f"Slurm job {job_id} hit its wall-clock limit and was killed")
        if state is JobState.CANCELLED:
            return ExecutionStatus.FAILED, f"Slurm job {job_id} was cancelled"
        # OUT_OF_MEMORY maps to FAILED in parse_slurm_state; surface it clearly
        # (sacct often reports ExitCode 0:125 which looks like success otherwise).
        blob = (stdout or "").lower()
        if "oom" in blob or "out_of_memory" in blob or "oom_kill" in blob:
            return (ExecutionStatus.FAILED,
                    f"Slurm job {job_id} was killed (out of memory) — "
                    f"raise RAM on the approval card (floor is 4 GB) and retry")
        # The smoke script exits 2 on a missing dependency; the payload chain
        # propagates it as the job's exit code. pip's resolver failures
        # ("No matching distribution found for xtb-python") are the same
        # class: the environment, not the science, is what broke.
        pip_markers = ("no matching distribution", "could not find a version")
        if exit_code == 2 or any(marker in blob for marker in
                                 _DEP_ERROR_MARKERS + pip_markers):
            return (ExecutionStatus.DEPENDENCY_ERROR,
                    f"Slurm job {job_id} failed on a missing/broken dependency "
                    f"(see the job log)")
        return (ExecutionStatus.FAILED,
                f"Slurm job {job_id} failed ({state.value}"
                + (f", exit {exit_code}" if exit_code is not None else "") + ")")
