"""Receiver for RIS API job webhooks (``POST /api/ris/webhooks``).

ris-api POSTs a signed JSON event when one of our Slurm jobs changes state
(``job.running``, ``job.completed``, ``job.failed``, ``job.cancelled``,
``job.retrying``, ...). Each delivery is recorded in ``ris_job_events``, whose
trigger NOTIFYs the runner waiting on that job so it re-polls immediately.
Polling stays the source of truth; a webhook only cuts the wait.

Signing follows Standard Webhooks (https://www.standardwebhooks.com), as
ris-api implements it (``app/webhooks/signing.py``):

* ``webhook-id``         delivery id, stable across retries (the dedupe key)
* ``webhook-timestamp``  unix seconds; stale ones are refused (replay guard)
* ``webhook-signature``  space-separated ``v1,<base64 HMAC-SHA256>`` entries over
                         ``"<id>.<timestamp>.<body>"``, keyed by the base64 part
                         of the ``whsec_...`` secret

The secret is the one ris-api showed once when the endpoint was registered.
Resolution mirrors the GitHub PAT (github_issues.py):

1. ``RIS_WEBHOOK_SECRET`` environment variable (local/dev), else
2. Secrets Manager ``TWAIN_RIS_WEBHOOK_SECRET_ID``
   (default ``TWAIN/ris_api/WEBHOOK_SECRET``), via :func:`database.read_secret`.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time

from psycopg2.extras import Json

from database import get_connection, read_secret

SECRET_PREFIX = "whsec_"
WEBHOOK_SECRET_ID = os.getenv("TWAIN_RIS_WEBHOOK_SECRET_ID", "TWAIN/ris_api/WEBHOOK_SECRET")
#: Seconds a delivery's timestamp may differ from now (the Standard Webhooks default).
TOLERANCE_SECONDS = 5 * 60


class WebhookError(Exception):
    """A delivery that must be refused; ``status`` is the HTTP code to return."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def webhook_secret() -> str:
    """The ``whsec_...`` signing secret; :class:`WebhookError` 503 if unset.

    503 rather than 401 on purpose: ris-api retries a non-2xx for ~23 hours, so a
    deploy that forgot the secret loses nothing once it's fixed.
    """
    secret = os.getenv("RIS_WEBHOOK_SECRET")
    if not secret:
        try:
            secret = read_secret(WEBHOOK_SECRET_ID)
        except RuntimeError as exc:
            raise WebhookError(503, "RIS webhook secret is not configured") from exc
    return secret.strip()


def _key(secret: str) -> bytes:
    try:
        return base64.b64decode(secret.removeprefix(SECRET_PREFIX), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WebhookError(503, "RIS webhook secret is malformed") from exc


def verify(secret: str, headers, body: bytes, *, now: float | None = None) -> None:
    """Raise :class:`WebhookError` 401 unless ``body`` is signed by ``secret``.

    ``headers`` is any case-insensitive mapping (Starlette's ``Headers``) or a
    plain dict with lowercase keys.
    """
    msg_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    signatures = headers.get("webhook-signature")
    if not (msg_id and timestamp and signatures):
        raise WebhookError(401, "missing webhook signature headers")
    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise WebhookError(401, "malformed webhook-timestamp") from exc
    if abs((time.time() if now is None else now) - sent_at) > TOLERANCE_SECONDS:
        raise WebhookError(401, "webhook-timestamp outside the tolerance window")

    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(_key(secret), signed, hashlib.sha256).digest())
    for entry in signatures.split():
        version, _, sig = entry.partition(",")
        if version == "v1" and hmac.compare_digest(sig.encode(), expected):
            return
    raise WebhookError(401, "webhook signature does not match")


def parse_event(body: bytes) -> dict:
    """The event as a dict; :class:`WebhookError` 400 for a body that isn't one."""
    try:
        event = json.loads(body)
    except ValueError as exc:
        raise WebhookError(400, "webhook body is not JSON") from exc
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise WebhookError(400, "webhook body has no event type")
    return event


def record_event(webhook_id: str, event: dict) -> bool:
    """Store one delivery; False when this ``webhook_id`` was already recorded."""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    job_id = data.get("job_id")
    conn = get_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ris_job_events (webhook_id, event_type, job_id, state, payload)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (webhook_id) DO NOTHING;
                """,
                (webhook_id, event["type"],
                 str(job_id) if job_id is not None else None,
                 data.get("state"), Json(event)),
            )
            return cur.rowcount == 1
    finally:
        conn.close()


def handle(headers, body: bytes) -> bool:
    """Verify, parse, and record a delivery. True if it was new.

    Raises :class:`WebhookError` for anything to refuse; the caller maps its
    ``status`` onto the HTTP response.
    """
    verify(webhook_secret(), headers, body)
    event = parse_event(body)
    return record_event(headers.get("webhook-id"), event)
