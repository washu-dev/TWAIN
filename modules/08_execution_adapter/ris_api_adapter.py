"""RIS API job-control adapter -- the HTTPS sibling of :class:`SlurmAdapter`.

Renders, submits, and tracks one Slurm job through the RIS API instead of SSH
+ ``sbatch``/``squeue``/``sacct``/``scancel``. Shares partition-selection
policy and state parsing with :mod:`slurm_adapter` (:func:`select_partition`,
:func:`parse_slurm_state`, :class:`JobState`) so the two backends make
identical scheduling decisions; only the transport differs.

Two things the RIS API cannot do, by design, are intentionally NOT part of
this adapter:

* **File transfer.** The API's ``script`` field is inline text (<=64KB); there
  is no way to push a RunBundle's files or pull results back. Staging stays on
  :class:`~execution_adapter.staging.Stager` (rsync over SSH) --
  :class:`~execution_adapter.slurm_execution_adapter.SlurmExecutionAdapter`
  still owns that, unchanged.
* **Arbitrary command execution on the login node.** The preflight
  environment probe (does a pre-provisioned env satisfy this bundle before
  queueing a real job?) needs to run a bash command directly on the login
  node; the API only runs structured Slurm jobs. That probe keeps its own SSH
  runner in :class:`SlurmExecutionAdapter`, decoupled from this class.

Known limitation: ``JobSubmitSpec.gpus`` is a plain count with no GPU-model
field, so a GPU job whose profile/job sets ``gpu_type`` (pinning e.g. H100s)
is rejected by :meth:`RisApiAdapter.render_job_spec` rather than silently run
on any GPU -- fall back to ``backend="ssh"`` if that's ever needed. Container/pyxis jobs (``JobSpec.container_image``) are likewise
unsupported (no such field in the API) and rejected at construction time by
the caller (:class:`SlurmExecutionAdapter`).
"""
from __future__ import annotations

import re
import shlex
import time
from typing import Any, Dict, List, Optional

try:  # pragma: no cover - import shim (mirrors slurm_execution_adapter)
    from execution_adapter.cluster_profile import ClusterProfile
    from execution_adapter.ris_api_client import RisApiClient, RisApiError
    from execution_adapter.slurm_adapter import (
        THREAD_ENV_VARS,
        JobSpec,
        JobState,
        SlurmError,
        format_walltime,
        parse_slurm_state,
        select_partition,
    )
except ImportError:  # pragma: no cover
    from cluster_profile import ClusterProfile
    from ris_api_client import RisApiClient, RisApiError
    from slurm_adapter import (
        THREAD_ENV_VARS,
        JobSpec,
        JobState,
        SlurmError,
        format_walltime,
        parse_slurm_state,
        select_partition,
    )

from plan_synthesizer.execution_plan import SlurmRequest


class RisApiAdapter:
    """Render, submit, and track Slurm jobs via the RIS API."""

    def __init__(self, profile: ClusterProfile, client: Optional[RisApiClient] = None):
        self.profile = profile
        self.client = client or RisApiClient()
        #: The latest ``GET /jobs/{id}`` body per job (node, Slurm's pending
        #: ``reason``, start time ...), kept so progress reporting can say more
        #: than the bare state :meth:`poll` returns.
        self.last_detail: Dict[str, Dict[str, Any]] = {}

    # -------------------------------------------------------- partition selection
    def select_partition(self, request: SlurmRequest, override: Optional[str] = None):
        return select_partition(self.profile, request, override)

    # ---------------------------------------------------------- job-spec rendering
    def render_job_spec(self, job: JobSpec, request: SlurmRequest) -> Dict[str, Any]:
        """Build the ``JobSubmitSpec`` body for ``job``/``request``.

        The API renders the ``#SBATCH`` directives itself from structured
        fields (partition/time_limit/nodes/.../gpus), so this replaces
        :meth:`SlurmAdapter.render_sbatch`'s hand-written directive lines.
        ``script`` still carries the same shell prologue (module load, thread
        pinning, env exports, cd into the workdir) followed by the job's
        payload command -- unchanged from what the SSH path runs, so the
        payload itself (built by :class:`SlurmExecutionAdapter`) needs no
        RIS-API-specific logic.
        """
        gpu_type = job.gpu_type or self.profile.gpu_type
        if request.gpu_count > 0 and gpu_type:
            # JobSubmitSpec.gpus is a bare count (--gpus N); dropping the type
            # would quietly run on whatever GPU Slurm picks.
            raise SlurmError(
                f"GPU type {gpu_type!r} can't be requested through the RIS API "
                f"(it takes only a GPU count) -- use TWAIN_SLURM_BACKEND=ssh, or "
                f"clear gpu_type on the cluster profile")
        partition = self.profile.partition(job.partition) if job.partition \
            else self.select_partition(request)
        account = job.account or self.profile.account

        # sbatch refuses a script whose first line isn't a #! interpreter line
        # ("This does not look like a batch script") -- the API passes `script`
        # to sbatch as the batch file, exactly like the SSH path's job.slurm.
        lines: List[str] = ["#!/bin/bash"]
        modules = job.modules if job.modules is not None else list(self.profile.modules)
        if request.gpu_count > 0:
            modules += [m for m in self.profile.gpu_modules if m not in modules]
        if modules:
            lines.append(f"module load {' '.join(modules)} >/dev/null 2>&1 || true")
        for var in THREAD_ENV_VARS:
            if var not in job.env:
                lines.append(f'export {var}="${{SLURM_CPUS_PER_TASK:-1}}"')
        for key, value in job.env.items():
            lines.append(f"export {key}={shlex.quote(str(value))}")
        if job.workdir:
            lines.append(f"cd {shlex.quote(job.workdir)}")
        lines.append(job.command_str())

        spec: Dict[str, Any] = {
            "script": "\n".join(lines) + "\n",
            "job_name": job.job_name,
            "partition": partition.name,
            "time_limit": format_walltime(request.max_time),
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": request.cpu_count,
            "memory": f"{request.ram}M",
            "account": account,
        }
        if request.gpu_count > 0:
            spec["gpus"] = request.gpu_count
        if job.workdir:
            spec["working_dir"] = job.workdir
        return spec

    # ----------------------------------------------------------------- operations
    #: Seconds to wait before each re-send of a submit that failed transiently.
    SUBMIT_RETRY_BACKOFF = (5.0, 15.0)

    def submit(
        self,
        spec: Dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        sleep=None,
    ) -> str:
        """Submit ``spec`` (from :meth:`render_job_spec`) and return the job id.

        A transient failure (network error, 429, 5xx) is re-sent with the SAME
        ``idempotency_key``: if the first POST actually reached Slurm, ris-api
        hands back that job instead of queueing a second one. That is the only
        safe reuse of a key -- ris-api keeps keys forever and never compares
        bodies, so a genuinely new submission must bring a fresh key (see
        :meth:`SlurmExecutionAdapter.execute`). Without a key, nothing is
        retried, since a re-send could double-submit.
        """
        sleep = sleep or time.sleep
        delays = self.SUBMIT_RETRY_BACKOFF if idempotency_key else ()
        for attempt in range(len(delays) + 1):
            try:
                return self.client.submit_job(spec, idempotency_key=idempotency_key)
            except RisApiError as exc:
                if attempt == len(delays) or not exc.transient:
                    raise SlurmError(str(exc)) from exc
                sleep(delays[attempt])

    def poll(self, job_id: str) -> JobState:
        """Return the current :class:`JobState` for ``job_id`` via ``GET /jobs/{id}``."""
        try:
            detail = self.client.get_job(job_id)
        except RisApiError as exc:
            if exc.status != 404:
                raise SlurmError(str(exc)) from exc
            # A finished job ages out of the controller (MinJobAge) and
            # GET /jobs/{id} 404s; its final state lives on in accounting.
            try:
                detail = self.client.accounting(job_id)
            except RisApiError as acct_exc:
                raise SlurmError(str(acct_exc)) from acct_exc
        self.last_detail[job_id] = detail
        return parse_slurm_state(detail.get("state", ""))

    #: Seconds of *consecutive* failed polls tolerated before giving up -- see
    #: SlurmAdapter.CONTACT_LOSS_TOLERANCE for the rationale (a transient API
    #: blip must not abort a multi-hour wait for a job that's still running).
    CONTACT_LOSS_TOLERANCE = 30 * 60.0

    def wait(
        self,
        job_id: str,
        *,
        poll_interval: float = 30.0,
        max_wait: Optional[float] = None,
        on_state=None,
        sleep=None,
    ) -> JobState:
        """Poll ``job_id`` until terminal (bounded) -- the API equivalent of
        :meth:`SlurmAdapter.wait`, with messages that point at the RIS API
        rather than squeue/VPN."""
        sleep = sleep or time.sleep
        waited = 0.0
        contact_lost = 0.0
        last_state: Optional[JobState] = None
        while True:
            try:
                state = self.poll(job_id)
                contact_lost = 0.0
            except SlurmError as exc:
                cause = exc.__cause__
                if isinstance(cause, RisApiError) and cause.auth_failure:
                    # A rejected token won't fix itself mid-wait; fail now
                    # rather than after the 30-minute contact-loss window.
                    raise
                contact_lost += poll_interval
                if contact_lost > self.CONTACT_LOSS_TOLERANCE:
                    raise SlurmError(
                        f"lost contact with the RIS API while polling job "
                        f"{job_id} (polls failing for {int(contact_lost)}s, "
                        f"last error: {exc}); the job was NOT cancelled -- "
                        f"check RIS_API_TOKEN/connectivity and retry"
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
        """Final accounting for ``job_id``, normalized to the same
        ``{State, ExitCode, Elapsed, MaxRSS}`` shape :meth:`SlurmAdapter.accounting`
        returns, so downstream parsing (elapsed/MaxRSS regexes, ``install_log``)
        needs no changes. ``JobAccounting`` has no partition field, so unlike
        the SSH path this omits ``Partition``."""
        try:
            raw = self.client.accounting(job_id)
        except RisApiError as exc:
            raise SlurmError(str(exc)) from exc
        mapped = {
            "State": raw.get("state") or "",
            "ExitCode": raw.get("exit_code") or "",
            "Elapsed": raw.get("elapsed") or "",
            "MaxRSS": raw.get("max_rss") or "",
        }
        return {k: v for k, v in mapped.items() if v}

    def exit_code(self, job_id: str) -> Optional[int]:
        """The job's exit code from accounting (``"1:0"`` -> 1), or None."""
        raw = self.accounting(job_id).get("ExitCode", "")
        match = re.match(r"(\d+):", raw)
        return int(match.group(1)) if match else None

    def cancel(self, job_id: str) -> None:
        """Cancel ``job_id`` via ``DELETE /jobs/{id}``."""
        try:
            self.client.cancel_job(job_id)
        except RisApiError as exc:
            raise SlurmError(str(exc)) from exc

    def stdout(self, job_id: str) -> str:
        """The job's captured standard output, fetched directly via the API."""
        try:
            return self.client.stdout(job_id)
        except RisApiError as exc:
            raise SlurmError(str(exc)) from exc

    def stdout_page(self, job_id: str, offset: int, limit: int) -> Dict[str, Any]:
        """One page of the job's stdout from byte ``offset`` (see
        :meth:`RisApiClient.output_page`) -- how a running job's log is followed."""
        try:
            return self.client.output_page(job_id, "stdout", offset=offset, limit=limit)
        except RisApiError as exc:
            raise SlurmError(str(exc)) from exc

    #: Bytes of stderr kept per job. The API always writes stderr to its own
    #: file (the SSH path merged it into the log), and what matters there --
    #: the crash traceback, the OOM/dependency marker -- is at the end.
    STDERR_TAIL_BYTES = 65_536

    def stderr(self, job_id: str) -> str:
        """The tail of the job's standard error (:data:`STDERR_TAIL_BYTES`)."""
        try:
            return self.client.output_tail(job_id, "stderr", self.STDERR_TAIL_BYTES)
        except RisApiError as exc:
            raise SlurmError(str(exc)) from exc
