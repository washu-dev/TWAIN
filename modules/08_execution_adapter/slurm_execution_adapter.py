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

import os
import re
import shlex
import time
import uuid
from pathlib import Path
from typing import List, Optional, Union

try:  # pragma: no cover - import shim (mirrors local_adapter)
    from execution_adapter.cluster_profile import ClusterProfile
    from execution_adapter.execution_result import ExecutionResult, ExecutionStatus
    from execution_adapter.job_activity import JobActivity
    from execution_adapter.local_adapter import _DEP_ERROR_MARKERS, _safe_name
    from execution_adapter.ris_api_adapter import RisApiAdapter
    from execution_adapter.ris_api_client import RisApiClient, RisApiError
    from execution_adapter.slurm_adapter import (
        THREAD_ENV_VARS,
        JobSpec,
        JobState,
        Runner,
        SlurmAdapter,
        SlurmError,
        ssh_runner,
        subprocess_runner,
    )
    from execution_adapter.staging import SECRETS_FILE, Stager, StagingError
except ImportError:  # pragma: no cover
    from cluster_profile import ClusterProfile
    from execution_result import ExecutionResult, ExecutionStatus
    from job_activity import JobActivity
    from local_adapter import _DEP_ERROR_MARKERS, _safe_name
    from ris_api_adapter import RisApiAdapter
    from ris_api_client import RisApiClient, RisApiError
    from slurm_adapter import (
        THREAD_ENV_VARS,
        JobSpec,
        JobState,
        Runner,
        SlurmAdapter,
        SlurmError,
        ssh_runner,
        subprocess_runner,
    )
    from staging import SECRETS_FILE, Stager, StagingError

from plan_synthesizer.execution_plan import SlurmRequest

# A generous default: DFT jobs routinely take an hour; beyond this the job is
# left running on the cluster and the result reports how to check on it.
DEFAULT_MAX_WAIT = 7200.0
DEFAULT_POLL_INTERVAL = 30.0

# Runner-environment secrets forwarded into every job (when set). Compute
# nodes get a fresh shell, so anything a generated script reads from the
# environment must be exported in the sbatch script explicitly. MP_API_KEY
# backs Materials Project database-retrieval tasks (MPRester).
_PASSTHROUGH_ENV = ("MP_API_KEY",)

_STATE_TO_STATUS = {
    JobState.COMPLETED: ExecutionStatus.SUCCESS,
    JobState.TIMEOUT: ExecutionStatus.TIMEOUT,
    JobState.FAILED: ExecutionStatus.FAILED,
    JobState.CANCELLED: ExecutionStatus.FAILED,
    JobState.UNKNOWN: ExecutionStatus.FAILED,
}


def _elapsed_seconds(text: str) -> float:
    """Elapsed time -> seconds (0.0 when unparseable).

    ``sacct`` (SSH backend) writes ``[D-]HH:MM:SS``; ris-api's accounting
    (API backend) reports plain seconds, e.g. ``"220"`` -- reading only the
    first form made every API run report a duration of 0 (#163).
    """
    text = (text or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    match = re.match(r"(?:(\d+)-)?(\d+):(\d+):(\d+)$", text)
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


#: How each pre-queue step's failure reads in the activity checklist.
_STEP_FAILED = {
    "stage": "Copying the bundle to the cluster failed",
    "preflight": "The environment check failed",
    "submit": "Submitting the job failed",
}


def _bundle_size(local_dir) -> tuple:
    """``(file count, size in KB)`` of the bundle about to be staged."""
    files = [p for p in Path(local_dir).rglob("*") if p.is_file()]
    return len(files), max(1, round(sum(p.stat().st_size for p in files) / 1024))


def _secrets_prologue(path: str) -> str:
    """Job-script lines that load the staged secrets file, then delete it.

    The delete is an EXIT trap rather than an immediate ``rm`` so a job that
    Slurm requeues after a node failure still finds the file on its next
    attempt; the file is 0600 inside the run dir until then.
    """
    # Via a variable: a quoted path nested inside the trap's own quotes
    # breaks on any path with a space.
    return (f"TWAIN_SECRETS_FILE={shlex.quote(path)}\n"
            """trap 'rm -f "$TWAIN_SECRETS_FILE"' EXIT\n"""
            """if [ -f "$TWAIN_SECRETS_FILE" ]; then . "$TWAIN_SECRETS_FILE"; fi\n""")


def _submission_key(run_id: str) -> str:
    """A fresh RIS API ``Idempotency-Key`` for one genuine submission.

    ris-api stores keys permanently and never compares request bodies, so a
    key derived from ``run_id`` alone would hand every later ``execute()`` of
    the same run -- the self-heal loop's repaired re-runs -- the ORIGINAL
    job back, and the repaired code would never run. One key per call keeps
    the run id readable in the key while making each submission distinct;
    the adapter reuses it only to re-send this same POST after a transient
    failure. Capped at ris-api's 255-character limit.
    """
    return f"{run_id}-{uuid.uuid4().hex[:12]}"[-255:]


class _AbortRequested(Exception):
    """Internal: the terminate seam fired while polling a Slurm job."""


class SlurmExecutionAdapter:
    """Stage, submit, poll, and fetch one RunBundle as a Slurm job.

    Job control (submit/poll/cancel/accounting/logs) goes through one of two
    backends, selected by ``backend``: ``"api"`` (default) talks to the RIS
    API over HTTPS (:class:`RisApiAdapter`); ``"ssh"`` is the legacy
    sbatch/squeue/sacct/scancel path (:class:`SlurmAdapter`), kept as a
    fallback. Either way, staging (rsync) and the login-node preflight probe
    still go over SSH -- the RIS API has no file-transfer or raw-command-exec
    endpoints to replace them with.
    """

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
        parallelism: str = "threads",
        sleep=None,
        should_abort=None,
        job_event_wait=None,
        on_progress=None,
        backend: Optional[str] = None,
        ris_api_client: Optional[RisApiClient] = None,
    ):
        """``cluster_runner`` executes commands on the login node -- sbatch/
        squeue/sacct on the ``"ssh"`` backend, or just the preflight probe on
        ``"api"`` (defaults to SSH to the profile's first login node, or
        locally when ``host`` is falsy -- i.e. the process already runs on a
        login node). ``transfer_runner`` executes the rsync/ssh staging
        commands. Both are injectable for tests.

        ``backend`` is ``"api"`` or ``"ssh"``, falling back to the
        ``TWAIN_SLURM_BACKEND`` env var, then ``"api"``. ``ris_api_client`` is
        injectable (tests pass a fake); production builds a real
        :class:`RisApiClient` from ``RIS_API_TOKEN``/``RIS_API_BASE_URL``.

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
        # Where the calculator's parallelism lives, from the registry:
        # "interpreter" (mpirun the script), "engine" (the script stays serial and
        # the engine command carries the ranks), or "threads" (no MPI). See
        # CalculatorEntry.parallelism and _env_payload.
        self.parallelism = parallelism or "threads"
        self._sleep = sleep
        # Terminate seam: zero-arg callable polled between squeue checks; True
        # means the researcher pressed Terminate -> scancel the job and return.
        self.should_abort = should_abort
        # Webhook seam: ``(job_id, seconds) -> bool`` replacing the plain sleep
        # between polls; it returns early when ris-api reports on the job (see
        # runner/db.py RisJobEventWaiter). None => sleep the full interval.
        self.job_event_wait = job_event_wait
        # Activity seam: ``(event_type, payload)`` publisher for the live
        # checklist + job log the UI shows during EXECUTE (job_activity.py).
        # None => nothing is reported; the run itself is unaffected.
        self.on_progress = on_progress

        self.backend = (backend or os.environ.get("TWAIN_SLURM_BACKEND") or "api").strip().lower()
        if self.backend not in ("api", "ssh"):
            raise ValueError(
                f"unknown Slurm backend {self.backend!r} (expected 'api' or 'ssh')")
        if self.backend == "api" and container_image:
            # JobSubmitSpec has no container/pyxis field -- the API can't run
            # a containerized job, unlike sbatch's `srun --container-image`.
            raise ValueError(
                "container_image is not supported on backend='api' (the RIS "
                "API has no container field) -- use backend='ssh' instead")

        if cluster_runner is None:
            cluster_runner = ssh_runner(
                self.host, user=user, modules=self.profile.modules,
            ) if self.host else subprocess_runner
        # Kept separate from `self.slurm`: the preflight probe needs to run a
        # raw command on the login node, which the API backend can't do.
        self.cluster_runner = cluster_runner
        if self.backend == "ssh":
            self.slurm = SlurmAdapter(self.profile, runner=cluster_runner)
        else:
            self.slurm = RisApiAdapter(self.profile, client=ris_api_client)
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
        job_env = dict(env or {})
        # Secrets a generated script may read at run time (currently the
        # Materials Project key for database-retrieval tasks). A compute node
        # gets a fresh shell, so they must reach the job explicitly -- but
        # never inside the job script: the RIS API stores every submitted
        # spec (script + environment), serves it back from /jobs/{id}/request,
        # and copies it into recipes. They ride the rsync instead, in a 0600
        # file the job sources and deletes (#153).
        secrets = {name: os.environ[name] for name in _PASSTHROUGH_ENV
                   if name not in job_env and os.environ.get(name)}
        secrets_path = self._write_secrets(local_dir, secrets)
        payload = self._payload(local_dir, install_deps=install_deps,
                                run_smoke=run_smoke)
        if secrets_path:
            payload = _secrets_prologue(f"{remote_dir}/{SECRETS_FILE}") + payload
        job = JobSpec(
            job_name=job_name,
            command=payload,
            workdir=remote_dir,
            output_path=f"{remote_dir}/{job_name}-%j.log",
            partition=self.partition,
            container_image=self.container_image,
            container_mounts=[f"{remote_dir}:{remote_dir}"] if self.container_image else [],
            env=job_env,
        )

        # 2) stage + preflight + submit --------------------------------------------
        activity = JobActivity(
            self.on_progress,
            detail=lambda jid: getattr(self.slurm, "last_detail", {}).get(jid, {}),
            read_log=self.slurm.stdout_page if self._via_api else None,
        )
        cluster = self.profile.name
        current = "stage"
        try:
            if self._via_api:
                # The API renders #SBATCH directives from structured fields
                # itself, so there's no job.slurm file to write -- the spec's
                # `script` carries the same shell payload inline.
                to_submit = self.slurm.render_job_spec(job, self.request)
            else:
                script = self.slurm.render_sbatch(job, self.request)
                (Path(local_dir) / "job.slurm").write_text(script, encoding="utf-8")
                to_submit = f"{remote_dir}/job.slurm"
            files, size_kb = _bundle_size(local_dir)
            activity.step("stage", "active", f"Copying the run bundle to {cluster} storage")
            try:
                self.stager.push(local_dir, run_id)
            finally:
                # Only the cluster copy is needed; don't leave the secret in
                # the local workspace (or in artifacts_dir).
                if secrets_path:
                    secrets_path.unlink(missing_ok=True)
            activity.step("stage", "done",
                          f"Bundle staged to {cluster} storage ({files} files, {size_kb} KB)",
                          remote_dir=remote_dir, files=files, size_kb=size_kb)
            current = "preflight"
            activity.step("preflight", "active",
                          "Checking that a cluster environment can run it")
            preflight_error = self._preflight_env_check(
                remote_dir, local_dir, install_deps=install_deps,
                run_smoke=run_smoke)
            if preflight_error:
                activity.step("preflight", "failed",
                              "No cluster environment can run this bundle",
                              message=preflight_error[:2000])
                return ExecutionResult(
                    status=ExecutionStatus.DEPENDENCY_ERROR,
                    message=preflight_error,
                    tool_name=tool_name,
                    artifacts_dir=local_dir if keep_artifacts else None,
                )
            activity.step("preflight", "done", "Environment check passed")
            current = "submit"
            partition = (to_submit.get("partition") if isinstance(to_submit, dict)
                         else self._partition_name())
            activity.step("submit", "active",
                          f"Submitting to Slurm on {cluster}"
                          + (" via the RIS API" if self._via_api else ""))
            job_id = (self.slurm.submit(to_submit,
                                        idempotency_key=_submission_key(run_id),
                                        sleep=self._sleep)
                     if self._via_api
                     else self.slurm.submit(to_submit))
        except (SlurmError, RisApiError, StagingError, KeyError, OSError) as exc:
            activity.step(current, "failed", f"{_STEP_FAILED[current]}: {exc}"[:500])
            return ExecutionResult(
                status=ExecutionStatus.SETUP_FAILED,
                message=f"failed to submit the job: {exc}",
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
            if self.job_event_wait is not None:
                self.job_event_wait(job_id, seconds)
            else:
                base_sleep(seconds)

        activity.step("submit", "done", f"Submitted — Slurm job {job_id}",
                      job_id=job_id, partition=partition, backend=self.backend,
                      cpus=self.request.cpu_count, memory_mb=self.request.ram,
                      time_limit_minutes=round(float(self.request.max_time)),
                      gpus=self.request.gpu_count or None,
                      portal_url=self._portal_url())
        try:
            wait_kwargs = dict(poll_interval=self.poll_interval, max_wait=max_wait,
                               sleep=_abortable_sleep,
                               on_state=lambda st: activity.observe(job_id, st))
            state = self.slurm.wait(job_id, **wait_kwargs)
            activity.finished(job_id, state)
        except _AbortRequested:
            try:
                self.slurm.cancel(job_id)
                note = (f"Slurm job {job_id} was cancelled "
                        f"({'via the RIS API' if self._via_api else 'scancel'})")
            except SlurmError as exc:
                note = (f"cancelling Slurm job {job_id} failed ({exc}) -- "
                        f"cancel it manually with `scancel {job_id}` on a login node"
                        + (" or in the RIS API web app" if self._via_api else ""))
            activity.step("run", "failed", "Stopped at your request", job_id=job_id,
                          note=note)
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                message=f"run terminated by the researcher; {note}",
                tool_name=tool_name,
                command=self._submit_command(remote_dir),
                artifacts_dir=local_dir if keep_artifacts else None,
            )
        except SlurmError as exc:
            # Budget expired (or contact with the cluster stayed lost) with the
            # job still queued/running: leave it alone (a multi-hour DFT run
            # must not die because our wait was shorter) and tell the researcher
            # exactly how to follow up.
            activity.step("run", "failed",
                          "TWAIN stopped waiting — the job is still on the cluster",
                          job_id=job_id, reason=str(exc)[:500])
            return ExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                message=(
                    f"the Slurm job is still running on {self.profile.name} "
                    f"(job id {job_id}); it was NOT cancelled. Check it with "
                    f"{self._status_hint(job_id)} and fetch outputs from {remote_dir} "
                    f"when it completes. (wait ended because: {exc})"
                ),
                tool_name=tool_name,
                command=self._submit_command(remote_dir),
                artifacts_dir=local_dir if keep_artifacts else None,
            )

        # 4) fetch results + accounting ------------------------------------------
        activity.step("fetch", "active", f"Fetching results from {cluster}")
        try:
            self.stager.pull(run_id, local_dir)
        except StagingError as exc:
            activity.step("fetch", "failed", f"Fetching results failed: {exc}"[:500])
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                message=f"job finished ({state.value}) but fetching outputs failed: {exc}",
                tool_name=tool_name,
                artifacts_dir=local_dir if keep_artifacts else None,
            )
        if self._via_api:
            # The API writes stderr to its own file, where the traceback and
            # the OOM/dependency markers land -- fetch it, or self-heal and
            # classification only see stdout (#151).
            stdout = self._safe_stdout(job_id)
            stderr = self._safe_stderr(job_id)
        else:
            # sbatch --output without --error: one log holds both streams.
            stdout = self._read_log(local_dir, job_name, job_id)
            stderr = ""
        accounting = self._safe_accounting(job_id)
        exit_code = self.slurm.exit_code(job_id) if accounting else None

        activity.step("fetch", "done", "Results fetched")
        status, message = self._classify(
            state, exit_code, f"{stdout}\n{stderr}" if stderr else stdout, job_id)
        return ExecutionResult(
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=_elapsed_seconds(accounting.get("Elapsed", "")),
            peak_memory_mb=_maxrss_mb(accounting.get("MaxRSS", "")),
            artifacts_dir=local_dir if keep_artifacts else None,
            tool_name=tool_name,
            command=self._submit_command(remote_dir),
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
            result = self.cluster_runner([
                "bash", "-c",
                f"cd {rd} && [ -x {py} ] && "
                f"timeout {self._PREFLIGHT_TIMEOUT_S} {py} inline_tests.py",
            ])
            if result.returncode == 0:
                return None  # this env serves the bundle; the job will find it
        if not (install_deps and (bundle / "requirements.txt").is_file()):
            return None  # no pip fallback to vet; let the job report precisely
        # Run the resolver with a pre-provisioned env's python: the login node's
        # bare `python3` can be ancient (pip < 22.2 has no --dry-run) or off
        # PATH in a non-login shell, and either way its error text doesn't
        # match the markers below -- so a conda-only requirement (psi4) used to
        # fail open here and die in the job instead.
        pys = " ".join(shlex.quote(p) for p in self.env_pythons)
        result = self.cluster_runner([
            "bash", "-c",
            f'cd {rd} && py=python3; for c in {pys}; do '
            f'[ -x "$c" ] && py="$c" && break; done; '
            f'"$py" -m pip install --dry-run -r requirements.txt',
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
            # Calculation scratch goes on NODE-LOCAL disk, never on the shared
            # filesystem the run directory lives on. quacc defaults its scratch to
            # the results dir (SCRATCH_DIR=None -> RESULTS_DIR -> "." -> here, on
            # GPFS), then moves the whole tmpdir into place when the calculation
            # finishes. On a network filesystem a file unlinked while still open
            # becomes a `.nfsXXXX` silly-rename stub, and moving THAT fails with
            # EBUSY -- so a Psi4 job that had already produced its gradient died in
            # cleanup (Slurm job 2631900). Measured on a compute node: the same
            # unlink-while-open leaves `.nfs...` under /storage2 and nothing at all
            # under /tmp, which is xfs and node-local.
            #
            # Set before the smoke gate on purpose: the smoke run drives the same
            # calculation path and is where that job actually failed. RESULTS_DIR is
            # deliberately left alone -- results must land in the run directory on
            # shared storage, which is the only part the login node can read.
            'export QUACC_SCRATCH_DIR="${TMPDIR:-/tmp}/twain-${SLURM_JOB_ID:-$$}"',
            'mkdir -p "$QUACC_SCRATCH_DIR"',
            # An ASE calculator shells out to its engine binary (nwchem, dftb+,
            # pw.x). Invoking <prefix>/bin/python by absolute path does not
            # activate the env, so its bin/ is absent from PATH and the binary is
            # "command not found" -- which surfaces as the env failing the smoke
            # probe, indistinguishable from the env being wrong. The local adapter
            # already does this; the cluster payload did not, so a correctly
            # provisioned twain-envs/nwchem was rejected and the run fell through
            # to a pip install of a conda-only package.
            "twain_use_env() {",
            '  BIN="$(dirname "$1")"',
            '  export PATH="$BIN:$PATH"',
            '  PREFIX="$(dirname "$BIN")"',
            # Engines read their data directories from variables that the conda
            # activate.d hooks normally set; sourcing them reproduces activation.
            '  if [ -d "$PREFIX/etc/conda/activate.d" ]; then',
            '    for hook in "$PREFIX"/etc/conda/activate.d/*.sh; do',
            '      [ -r "$hook" ] || continue',
            '      CONDA_PREFIX="$PREFIX" . "$hook" >/dev/null 2>&1 || true',
            '    done',
            '  fi',
            '  export CONDA_PREFIX="$PREFIX"',
            "}",
            'PY=""',
            'BASE=""',
            'LAYERED=""',
            f"for CAND in {candidates}; do",
            '  [ -x "$CAND" ] || continue',
            # The first env that EXISTS is the most specific one (the candidate
            # list is engine-first), so it is the right base to layer pip onto if
            # no candidate satisfies the toolset outright.
            '  [ -n "$BASE" ] || BASE="$CAND"',
            # Probe each candidate with its own env set up, in a subshell so a
            # rejected candidate leaves no PATH behind for the next one.
            '  ( twain_use_env "$CAND"'.rstrip(),
        ]
        if have_smoke:
            lines.append('    "$CAND" inline_tests.py >/dev/null 2>&1 ) '
                         '&& { PY="$CAND"; break; }')
        else:
            lines.append('    true ) && { PY="$CAND"; break; }')
        lines += [
            "done",
            'if [ -z "$PY" ]; then',
        ]
        if install_deps and (bundle / "requirements.txt").is_file():
            lines += [
                # No single env satisfies the toolset. That is routine for a mixed
                # one: quacc is pip-only and in no env, psi4 is conda-only and in
                # exactly one, so neither an env nor a bare venv can host both.
                # Layer the venv ON the engine env -- conda-only packages come
                # through --system-site-packages, pip-only ones get installed.
                # (Slurm job 2580169 died here: it built a bare venv, pip
                # correctly refused the conda-only psi4, and the gate failed.)
                '  if [ -n "$BASE" ]; then',
                '    twain_use_env "$BASE"',
                # Remove any existing .venv first: `python -m venv` REUSES an
                # existing directory and does NOT rebase it on the new
                # interpreter, so a venv left by an earlier attempt in a reused
                # workdir would silently keep its old base env -- the toolset
                # would then be satisfied by the wrong python and the job would
                # look fine while running against packages nobody chose.
                '    rm -rf .venv',
                '    "$BASE" -m venv --system-site-packages .venv',
                # Install ONLY what the base env is missing. Installing the whole
                # requirements file would let a pin (numpy==1.26.4) shadow the
                # conda build the engine was compiled against, which breaks the
                # engine in a way that looks like a code bug.
                '    "$BASE" - <<\'TWAIN_MISSING\' > .twain-missing.txt',
                "import importlib.metadata as md",
                "import re",
                'for raw in open("requirements.txt"):',
                '    line = raw.split("#")[0].strip()',
                "    if not line:",
                "        continue",
                '    name = re.split(r"[=<>!~\\[;]", line, maxsplit=1)[0].strip()',
                "    if not name:",
                "        continue",
                "    try:",
                "        md.distribution(name)",
                "    except Exception:",
                "        print(line)",
                "TWAIN_MISSING",
                '    if [ -s .twain-missing.txt ]; then',
                '      echo "[env] layering on $BASE; pip adding: '
                '$(tr \'\\n\' \' \' < .twain-missing.txt)"',
                "      .venv/bin/python -m pip install -q --upgrade pip",
                "      .venv/bin/python -m pip install -q -r .twain-missing.txt",
                "    fi",
                # The venv's python wins for imports, while BASE stays on PATH and
                # keeps its activate.d data vars -- the engine binary and its
                # basis/pseudopotential directories must still resolve.
                '    export PATH="$PWD/.venv/bin:$PATH"',
                '    PY="$PWD/.venv/bin/python"',
                '    LAYERED=1',
                "  else",
                "    python3 -m venv .venv",
                "    .venv/bin/python -m pip install -q --upgrade pip",
                "    .venv/bin/python -m pip install -q -r requirements.txt",
                '    PY=".venv/bin/python"',
                "  fi",
            ]
        else:
            lines.append('  PY="python3"')
        lines.append("fi")
        # Before anything actually runs: the confirmation smoke below invokes
        # main.py --smoke, which shells out to the engine binary just as the real
        # run does, so it needs the same PATH and data directories. A layered venv
        # already activated its base env above; re-running twain_use_env on the
        # venv would put .venv/bin ahead of the engine's bin and drop CONDA_PREFIX.
        lines.append('if [ -z "$LAYERED" ]; then twain_use_env "$PY"; fi')
        if run_smoke and (bundle / "inline_tests.py").is_file():
            lines.append('"$PY" inline_tests.py')
        # Bound the run. A wedged engine or a hung MPI teardown otherwise burns the
        # whole allocation: job 2601849 did about a minute of work, crashed inside
        # ASE's Vibrations, and then idled 2h12m of a 4h limit before being killed
        # by hand. Exiting 124 a little short of the limit turns that into a
        # diagnosable timeout instead of silence.
        budget = max(60, int(self.request.max_time * 60) - 120)
        lines.append(
            f'TWAIN_TIMEOUT="timeout --signal=TERM --kill-after=30 {budget}"')
        # Run the real payload under MPI when the selected env ships mpirun
        # (e.g. the openmpi GPAW build): DFT engines parallelize over k-points
        # via MPI ranks, which scales far better than OpenMP threading. One
        # thread per rank so ranks*threads never oversubscribes the allocation.
        #
        # NOT for an external engine. GPAW is MPI-parallel *in process*, so every
        # rank cooperates in one calculation. An engine driven as a separate binary
        # is different: the script does many sequential calculations (a relaxation,
        # then 6N finite-difference displacements) and each writes fixed filenames
        # in the CWD. N ranks then run N copies of the whole script in one
        # directory and clobber each other -- job 2601849 left nwchem_CO.nwo at 0
        # bytes and an empty vib cache, so ASE read a displacement no rank had
        # written, raised, and the MPI teardown hung for 2h12m of a 4h allocation.
        # The engine parallelizes itself; the driver script must stay serial.
        #
        # The lookup uses the env the interpreter came FROM: with a layered venv
        # $PY lives in .venv/bin, which never contains mpirun, so deriving it from
        # $PY alone would silently disable MPI for every layered run.
        # The env the interpreter came FROM: with a layered venv $PY lives in
        # .venv/bin, which never holds mpirun, so deriving this from $PY alone
        # would silently disable MPI for every layered run.
        lines.append('if [ -n "$LAYERED" ]; then BIN="$(dirname "$BASE")";'
                     ' else BIN="$(dirname "$PY")"; fi')
        # The header exported every thread knob as the full core count, which is
        # right while the allocation belongs to one threaded process and wrong the
        # moment it is handed to MPI ranks: ranks x threads must not exceed the
        # cores we own. 24 ranks that each opened 24 BLAS threads put 576 spinning
        # threads on 24 cores and made a GPAW iteration 250x slower (job 2608808:
        # 185 s/iter against 0.7 s/iter for the same cell) -- and because the
        # spinning threads report ~2400% CPU, the job looks busy rather than
        # broken. Pinning OMP_NUM_THREADS alone does not fix it: OpenBLAS reads
        # OPENBLAS_NUM_THREADS first and would have stayed at 24.
        #
        # Taking the share from the rank count rather than hardcoding 1 keeps this
        # correct if a caller ever launches fewer ranks than it has cores.
        lines += [
            "twain_pin_threads() {",
            '  _twain_per=$(( ${SLURM_CPUS_PER_TASK:-1} / $1 ))',
            '  if [ "$_twain_per" -lt 1 ]; then _twain_per=1; fi',
            f'  for _twain_v in {" ".join(THREAD_ENV_VARS)}; do',
            '    export "$_twain_v=$_twain_per"',
            "  done",
            "}",
        ]
        if self.parallelism != "interpreter":
            # The script stays serial. N ranks of a driver that invokes a separate
            # binary per calculation would be N copies of it in one directory,
            # clobbering the fixed filenames each writes (job 2601849: a 0-byte
            # .nwo, an empty vib cache, then a crash and a 2h12m hung teardown).
            #
            # For "engine" the ranks go to the engine instead, through one uniform
            # variable the generated code prefixes onto its engine command. That is
            # the only portable seam: ASE takes the command as a constructor
            # argument for NWChem and ABINIT, and only as an env var for CP2K and
            # DFTB+, so there is no single env var the payload could set.
            if self.parallelism == "engine":
                lines += [
                    'if [ -n "$BIN" ] && [ -x "$BIN/mpirun" ]; then',
                    '  export TWAIN_ENGINE_LAUNCH="$BIN/mpirun -np'
                    ' ${SLURM_CPUS_PER_TASK:-1} --map-by :OVERSUBSCRIBE'
                    ' --bind-to none"',
                    # The engine gets the ranks, so the engine (and the driver it
                    # inherits from) must stop threading over the same cores.
                    '  twain_pin_threads "${SLURM_CPUS_PER_TASK:-1}"',
                    '  export OPAL_PREFIX="$(dirname "$BIN")"',
                    '  export PMIX_PREFIX="$OPAL_PREFIX"',
                    "fi",
                ]
            lines.append('$TWAIN_TIMEOUT "$PY" main.py')
        else:
            lines += [
                'if [ -x "$BIN/mpirun" ]; then',
                '  twain_pin_threads "${SLURM_CPUS_PER_TASK:-1}"',
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
                # wrapper's documented equivalent and, unlike the $BIN/gpaw entry
                # script, can't be broken by a stale relative shebang.
                '  if [ -x "$BIN/gpaw" ]; then LAUNCH="$PY -m gpaw python";'
                ' else LAUNCH="$PY"; fi',
                # --bind-to none: with OVERSUBSCRIBE over one nominal slot,
                # OpenMPI otherwise stacks every rank on the same core (observed
                # ~40x slowdown); unbound ranks spread over the cgroup's cores.
                '  $TWAIN_TIMEOUT "$BIN/mpirun" -np "${SLURM_CPUS_PER_TASK:-1}"'
                ' --map-by :OVERSUBSCRIBE --bind-to none $LAUNCH main.py',
                "else",
                '  $TWAIN_TIMEOUT "$PY" main.py',
                "fi",
            ]
        return "\n".join(lines)

    def _portal_url(self) -> Optional[str]:
        """The RIS API Portal's jobs page (it has no per-job route), API backend only."""
        if not self._via_api:
            return None
        base = getattr(getattr(self.slurm, "client", None), "base_url", "") or ""
        root = re.sub(r"/api(/v\d+)?/?$", "", base)
        return f"{root}/jobs" if root.startswith("https://") else None

    def _partition_name(self) -> Optional[str]:
        """The partition the SSH path's sbatch script asks for (for display)."""
        try:
            return (self.partition
                    or self.slurm.select_partition(self.request).name)
        except Exception:  # noqa: BLE001 - display only
            return None

    @property
    def _via_api(self) -> bool:
        return isinstance(self.slurm, RisApiAdapter)

    def _submit_command(self, remote_dir: str) -> List[str]:
        """How the job was submitted, for ExecutionResult.command/provenance."""
        if self._via_api:
            return ["ris-api", "POST", "/jobs"]
        return ["sbatch", f"{remote_dir}/job.slurm"]

    def _status_hint(self, job_id: str) -> str:
        if self._via_api:
            return f"the RIS API (`GET /jobs/{job_id}`, or its web app)"
        return f"`squeue --job {job_id}`"

    @staticmethod
    def _write_secrets(local_dir, secrets: dict) -> Optional[Path]:
        """Write ``secrets`` as ``export`` lines to a 0600 :data:`SECRETS_FILE`
        in the bundle; None (and no file) when there are none."""
        if not secrets:
            return None
        path = Path(local_dir) / SECRETS_FILE
        path.unlink(missing_ok=True)  # O_CREAT keeps an old file's mode
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for name, value in secrets.items():
                fh.write(f"export {name}={shlex.quote(value)}\n")
        return path

    def _read_log(self, local_dir, job_name: str, job_id: str) -> str:
        path = Path(local_dir) / f"{job_name}-{job_id}.log"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _safe_stdout(self, job_id: str) -> str:
        try:
            return self.slurm.stdout(job_id)
        except SlurmError:
            return ""

    def _safe_stderr(self, job_id: str) -> str:
        try:
            return self.slurm.stderr(job_id)
        except SlurmError:
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
        # Our own in-job `timeout` fires ~2 min before Slurm's limit so a wedged
        # engine dies with a diagnosable code instead of idling out the
        # allocation. That means Slurm never reports its own TIMEOUT state for
        # this case, and without mapping 124 the run would be classified a
        # generic FAILED -- losing the actionable "it ran out of time" message.
        if exit_code == 124:
            return (ExecutionStatus.TIMEOUT,
                    f"Slurm job {job_id} made no progress within its wall-clock "
                    f"limit and was stopped short of it — raise max_time on the "
                    f"approval card, or check the job log for a wedged engine")
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
