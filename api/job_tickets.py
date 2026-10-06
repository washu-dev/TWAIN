"""Job tickets: presigned S3 URLs for a Slurm job, without AWS credentials on RIS.

``POST /api/job-tickets/urls`` (header ``X-TWAIN-Ticket: <token>``) with
``{"objects": [{"name": "input/bundle.tar.gz", "method": "GET"},
               {"name": "output/outputs.tar.gz", "method": "PUT"}]}``
returns ``{"urls": {name: url}, "expires_in": seconds}``.

The runner/worker issues a ticket per run attempt when it submits the job
(runner/db.py ``issue_job_ticket``; table: migration 013). Its scope is one
attempt's prefix, ``runs/<run>/attempt-<n>/``, and within it the job may only
READ ``input/`` and WRITE ``output/`` -- it can't overwrite its own bundle or
touch another run. Only the token's SHA-256 is stored.

URLs are signed per request with the API's own credentials (the ECS task
role), so they are always fresh: a URL signed at submit time with temporary
credentials would expire with them, hours before a job that queued for a
while gets to use it.

Config: ``TWAIN_RUN_BUCKET`` (the bucket), ``AWS_REGION``.
"""
from __future__ import annotations

import hashlib
import os
import re

import boto3
from botocore.config import Config

from database import get_connection

#: How long each minted URL stays valid. A job uses them immediately.
URL_TTL_SECONDS = 3600
#: Objects a job may ask for: a relative name, no traversal.
_NAME = re.compile(r"^(input|output)/[A-Za-z0-9._-][A-Za-z0-9._/-]{0,199}$")
#: method -> the only folder it may touch.
_ALLOWED = {"GET": "input/", "PUT": "output/"}
MAX_OBJECTS = 16


class TicketError(Exception):
    """A request to refuse; ``status`` is the HTTP code to return."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bucket() -> str:
    bucket = os.getenv("TWAIN_RUN_BUCKET", "").strip()
    if not bucket:
        raise TicketError(503, "run storage is not configured (TWAIN_RUN_BUCKET)")
    return bucket


def _s3():
    # SigV4 and the regional endpoint: required for presigned PUTs to work
    # from outside AWS without a redirect.
    region = os.getenv("AWS_REGION", "us-east-1")
    return boto3.client("s3", region_name=region,
                        endpoint_url=f"https://s3.{region}.amazonaws.com",
                        config=Config(signature_version="s3v4"))


def redeem(token: str) -> dict:
    """The live ticket for ``token`` (recording its use); TicketError 401 otherwise."""
    if not token or len(token) > 200:
        raise TicketError(401, "missing or malformed job ticket")
    conn = get_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_tickets
                   SET use_count = use_count + 1, last_used_at = now()
                 WHERE token_hash = %s AND expires_at > now()
             RETURNING run_id, attempt, s3_prefix;
                """,
                (token_hash(token),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        raise TicketError(401, "unknown or expired job ticket")
    return {"run_id": row[0], "attempt": row[1], "s3_prefix": row[2]}


def presign(ticket: dict, objects: list, *, client=None) -> dict:
    """Presigned URLs for ``objects`` within ``ticket``'s prefix."""
    if not isinstance(objects, list) or not 0 < len(objects) <= MAX_OBJECTS:
        raise TicketError(400, f"objects must be a list of 1..{MAX_OBJECTS}")
    bucket = _bucket()
    client = client or _s3()
    urls = {}
    for obj in objects:
        name = (obj or {}).get("name") if isinstance(obj, dict) else None
        method = (obj or {}).get("method", "GET") if isinstance(obj, dict) else None
        if not isinstance(name, str) or not _NAME.match(name) or ".." in name:
            raise TicketError(400, f"invalid object name: {name!r}")
        if method not in _ALLOWED or not name.startswith(_ALLOWED[method]):
            raise TicketError(403, f"{method} is not allowed on {name!r} "
                                   f"(GET reads input/, PUT writes output/)")
        key = f"{ticket['s3_prefix'].rstrip('/')}/{name}"
        urls[name] = client.generate_presigned_url(
            "get_object" if method == "GET" else "put_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=URL_TTL_SECONDS,
        )
    return {"urls": urls, "expires_in": URL_TTL_SECONDS}


def handle(token: str, body: dict) -> dict:
    """Validate the ticket and mint the requested URLs (TicketError on refusal)."""
    if not isinstance(body, dict):
        raise TicketError(400, "body must be a JSON object")
    return presign(redeem(token), body.get("objects"))
