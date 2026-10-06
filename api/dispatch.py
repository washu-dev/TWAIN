"""How a queued job reaches a worker (P2, #171).

``TWAIN_DISPATCH=db`` (default): the job row is inserted ``queued`` and a
polling runner claims it -- the pre-P2 model, unchanged.

``TWAIN_DISPATCH=sqs``: the row is inserted ``dispatching`` (a transactional
outbox: the row is the event), and after the transaction commits it is sent to
the SQS FIFO queue ``TWAIN_JOB_QUEUE_URL``. Workers (runner/worker.py) claim
from the message, by id. A send that fails is not lost: the runner's relay
re-sends any ``dispatching`` row still unpublished after a grace period. The
login-node runner only ever claims ``queued`` rows, so it never sees these.

Message: ``{"job_id": <int>, "session_id": <str>, "kind": <str>}`` with
``MessageGroupId = session_id`` (one run's jobs in order, runs in parallel) and
``MessageDeduplicationId = "job-<id>"`` (a re-send within 5 minutes is
dropped by SQS; outside it the worker's claim is a no-op). Keep in lockstep
with runner/dispatch.py.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)


def mode() -> str:
    return (os.getenv("TWAIN_DISPATCH") or "db").strip().lower()


def initial_status() -> str:
    """The status a new job row is inserted with."""
    return "dispatching" if mode() == "sqs" else "queued"


def pending_statuses() -> tuple:
    """Statuses meaning "waiting for a worker" (for the duplicate-resume guard)."""
    return ("queued", "dispatching")


def message(job_id: int, session_id: str, kind: str) -> dict:
    return {
        "MessageBody": json.dumps({"job_id": int(job_id), "session_id": session_id, "kind": kind}),
        "MessageGroupId": session_id,
        "MessageDeduplicationId": f"job-{int(job_id)}",
    }


def publish(conn, jobs: list, *, client=None) -> int:
    """Send committed ``jobs`` -- ``[(job_id, session_id, kind), ...]`` -- to SQS.

    Call AFTER the inserting transaction commits (a message for an uncommitted
    row could reach a worker first). Marks each sent row ``published_at``.
    Best effort by design: a failure is logged and left to the relay. Returns
    how many were sent.
    """
    if mode() != "sqs" or not jobs:
        return 0
    queue_url = os.getenv("TWAIN_JOB_QUEUE_URL", "")
    if not queue_url:
        log.error("TWAIN_DISPATCH=sqs but TWAIN_JOB_QUEUE_URL is unset; the relay "
                  "will dispatch jobs %s", [j[0] for j in jobs])
        return 0
    if client is None:
        import boto3
        client = boto3.client("sqs", region_name=os.getenv("AWS_REGION", "us-east-1"))
    sent = []
    for job_id, session_id, kind in jobs:
        try:
            client.send_message(QueueUrl=queue_url, **message(job_id, session_id, kind))
            sent.append(job_id)
        except Exception as exc:  # noqa: BLE001 - the relay retries
            log.warning("SQS send for job %s failed (%s); the relay will retry", job_id, exc)
    if sent:
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET published_at = now() WHERE id = ANY(%s);", (sent,))
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - a re-send is deduplicated/no-op
            log.warning("could not mark jobs %s published (%s)", sent, exc)
    return len(sent)
