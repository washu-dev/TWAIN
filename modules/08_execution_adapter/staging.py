"""File staging for Slurm cluster execution (Story 5.4).

Moves a RunBundle to the cluster and its outputs back:

  * ``push()``  -- rsync the local bundle dir into a per-run directory under the
    cluster's writable storage allocation (``ClusterProfile.storage_root``, e.g.
    ``/storage2/fs1/mdan/Active/common/projects/twain/twain-runs/<run_id>/``).
    Home/storage auto-mounts on Compute2 compute nodes, so the submitted job
    reads and writes the same directory.
  * ``pull()``  -- rsync the run directory (outputs, ``results.csv``, job logs)
    back into the local artifacts dir.

All transport goes through an injected ``runner(argv) -> CommandResult`` (the
same contract as :mod:`slurm_adapter`), so staging is unit-testable offline; in
production the default runner shells out to ``ssh``/``rsync``, which requires
the WashU VPN (AnyConnect) + Duo 2FA and an SSH key for the login node.
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

try:  # pragma: no cover - import shim (mirrors local_adapter)
    from execution_adapter.cluster_profile import ClusterProfile
    from execution_adapter.local_adapter import _safe_name
    from execution_adapter.slurm_adapter import Runner, subprocess_runner
except ImportError:  # pragma: no cover
    from cluster_profile import ClusterProfile
    from local_adapter import _safe_name
    from slurm_adapter import Runner, subprocess_runner

# Per-run directories live under <storage_root>/twain-runs/ -- never the
# allocation root itself (the PI root is read-only; the allocation is shared).
RUNS_SUBDIR = "twain-runs"


#: Job-time secrets staged next to the bundle (mode 0600), loaded and deleted
#: by the job itself, so they never appear in a job script -- which the RIS
#: API persists and copies into recipes. Never pulled back.
SECRETS_FILE = ".twain_secrets.env"


class StagingError(RuntimeError):
    """Raised when pushing/pulling the run directory fails."""


@runtime_checkable
class BundleTransport(Protocol):
    """What :class:`~execution_adapter.slurm_execution_adapter.SlurmExecutionAdapter`
    needs to get a RunBundle onto cluster storage and its outputs back.

    :class:`Stager` (rsync over SSH) is the only implementation today -- the
    RIS API has no file-transfer endpoints, so staging can't move to it yet.
    This protocol exists so that boundary is named: a future API-based
    transport can satisfy it without the adapter changing at all.
    """

    def remote_run_dir(self, run_id) -> str: ...
    def push(self, local_dir, run_id) -> str: ...
    def pull(self, run_id, local_dir) -> str: ...
    def cleanup(self, run_id) -> None: ...


class Stager:
    """Pushes bundles to, and pulls results from, one cluster's storage."""

    def __init__(
        self,
        profile: ClusterProfile,
        *,
        host: Optional[str] = None,
        user: Optional[str] = None,
        runner: Optional[Runner] = None,
        remote_root: Optional[str] = None,
    ):
        """``host=None`` targets the profile's first login node over SSH; an
        explicit empty string means the process already runs on the cluster
        (login node), so staging degrades to plain local copies."""
        self.profile = profile
        self.host = profile.login_nodes[0] if host is None else host
        self.user = user
        self.runner = runner or subprocess_runner
        self.remote_root = (remote_root or profile.storage_root or "").rstrip("/")
        if not self.remote_root:
            raise StagingError(
                f"cluster profile {profile.name!r} has no storage_root; set one in "
                f"configs/clusters/{profile.name}.json or pass remote_root explicitly"
            )

    @property
    def local(self) -> bool:
        """True when staging happens on the cluster itself (no SSH hop)."""
        return not self.host

    @property
    def target(self) -> str:
        """The ssh/rsync destination host (``user@host`` when a user is set)."""
        return f"{self.user}@{self.host}" if self.user else self.host

    def remote_run_dir(self, run_id) -> str:
        """The per-run directory on the cluster for ``run_id``."""
        return f"{self.remote_root}/{RUNS_SUBDIR}/{_safe_name(run_id)}"

    def _run(self, argv) -> None:
        result = self.runner(list(argv))
        if result.returncode != 0:
            raise StagingError(
                f"command {' '.join(argv)!r} failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    def _remote_arg(self, path: str) -> str:
        """An rsync source/destination for ``path`` (host-prefixed unless local)."""
        return path if self.local else f"{self.target}:{path}"

    def push(self, local_dir, run_id) -> str:
        """Sync ``local_dir`` to the cluster's per-run dir; returns the remote path."""
        local = Path(local_dir)
        if not local.is_dir():
            raise StagingError(f"bundle path is not a directory: {local}")
        remote = self.remote_run_dir(run_id)
        if self.local:
            self._run(["mkdir", "-p", remote])
        else:
            self._run(["ssh", self.target, f"mkdir -p {shlex.quote(remote)}"])
        # Trailing slashes: copy the *contents* of local into remote (idempotent
        # re-push replaces stale files thanks to --delete).
        self._run(["rsync", "-az", "--delete", f"{str(local).rstrip('/')}/",
                   f"{self._remote_arg(remote)}/"])
        return remote

    def pull(self, run_id, local_dir) -> str:
        """Sync the per-run dir (outputs + job logs) back into ``local_dir``."""
        local = Path(local_dir)
        local.mkdir(parents=True, exist_ok=True)
        remote = self.remote_run_dir(run_id)
        self._run(["rsync", "-az", f"--exclude={SECRETS_FILE}",
                   f"{self._remote_arg(remote)}/", f"{str(local).rstrip('/')}/"])
        return str(local)

    def cleanup(self, run_id) -> None:
        """Remove the per-run directory on the cluster (best effort, guarded).

        Only ever deletes under ``<storage_root>/twain-runs/`` -- the guard makes
        it impossible to point ``rm -rf`` at the (shared) allocation root.
        """
        remote = self.remote_run_dir(run_id)
        prefix = f"{self.remote_root}/{RUNS_SUBDIR}/"
        if not remote.startswith(prefix) or remote == prefix.rstrip("/"):
            raise StagingError(f"refusing to clean up outside {prefix}: {remote}")
        if self.local:
            self._run(["rm", "-rf", remote])
        else:
            self._run(["ssh", self.target, f"rm -rf {shlex.quote(remote)}"])
