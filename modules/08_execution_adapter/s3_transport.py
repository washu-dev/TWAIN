"""S3 file I/O for Slurm jobs submitted through the RIS API (#170).

The RIS API has no file-transfer endpoints, and an ECS worker has no SSH into
RIS storage, so a run's files travel through S3 instead of rsync:

* ``push``  -- tar the bundle, upload ``runs/<run>/attempt-<n>/input/bundle.tar.gz``
* the job   -- ``scripts/ris/job_wrapper.sh`` on the compute node trades its
               job ticket for presigned URLs, downloads + unpacks the bundle,
               runs it, then uploads ``output/outputs.tar.gz``
* ``pull``  -- download that archive and unpack it into the local run dir

Only the submitting side (worker / runner) holds AWS credentials -- its own
role or profile, via boto3's default chain. The cluster holds none: the job's
ticket is scoped to one attempt's prefix and expires (api/job_tickets.py).
"""
from __future__ import annotations

import io
import os
import tarfile
from pathlib import Path
from typing import Optional

try:  # pragma: no cover - import shim (mirrors the other adapter modules)
    from execution_adapter.staging import StagingError
except ImportError:  # pragma: no cover
    from staging import StagingError

BUNDLE_KEY = "input/bundle.tar.gz"
OUTPUTS_KEY = "output/outputs.tar.gz"
#: Never shipped up: rebuilt on the node (venv) or would leak (secrets).
_SKIP_UP = {".venv", "__pycache__", ".twain_secrets.env"}


def attempt_prefix(run_id: str, attempt: int) -> str:
    return f"runs/{run_id}/attempt-{int(attempt)}"


class S3Transport:
    """Pushes bundles to, and pulls outputs from, the run bucket."""

    def __init__(self, bucket: Optional[str] = None, *, client=None,
                 region: Optional[str] = None):
        self.bucket = bucket or os.environ.get("TWAIN_RUN_BUCKET", "")
        if not self.bucket:
            raise StagingError("S3 staging needs TWAIN_RUN_BUCKET")
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3  # deferred: SSH-staged runs never need it
            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    # ------------------------------------------------------------------ up
    def push(self, local_dir, run_id: str, attempt: int) -> str:
        """Upload the bundle in ``local_dir``; returns the attempt's S3 prefix."""
        local = Path(local_dir)
        if not local.is_dir():
            raise StagingError(f"bundle path is not a directory: {local}")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path in sorted(local.rglob("*")):
                rel = path.relative_to(local)
                if rel.parts and rel.parts[0] in _SKIP_UP:
                    continue
                if path.is_file():
                    tar.add(path, arcname=str(rel), recursive=False)
        prefix = attempt_prefix(run_id, attempt)
        try:
            self.client.put_object(Bucket=self.bucket, Key=f"{prefix}/{BUNDLE_KEY}",
                                   Body=buf.getvalue(), ServerSideEncryption="AES256")
        except Exception as exc:  # noqa: BLE001 - boto raises many types
            raise StagingError(f"uploading the bundle to s3://{self.bucket}/{prefix} "
                               f"failed: {exc}") from exc
        return prefix

    # ---------------------------------------------------------------- down
    def pull(self, run_id: str, attempt: int, local_dir) -> str:
        """Download and unpack the attempt's outputs into ``local_dir``."""
        local = Path(local_dir)
        local.mkdir(parents=True, exist_ok=True)
        key = f"{attempt_prefix(run_id, attempt)}/{OUTPUTS_KEY}"
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:  # noqa: BLE001
            raise StagingError(
                f"the job left no outputs at s3://{self.bucket}/{key} ({exc}) -- it "
                f"likely failed before its upload step; see its stderr") from exc
        root = local.resolve()
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            for member in tar.getmembers():
                target = (local / member.name).resolve()
                # tar-slip: an entry may never land outside the run dir.
                if root != target and root not in target.parents:
                    raise StagingError(f"refusing archive entry outside the run dir: {member.name}")
                if member.issym() or member.islnk() or member.isdev():
                    raise StagingError(f"refusing link/device entry in outputs: {member.name}")
            tar.extractall(local, filter="data") if hasattr(tarfile, "data_filter") \
                else tar.extractall(local)  # noqa: S202 - members validated above
        return str(local)
