"""The runner's half of job dispatch (P2, #171) -- mirror of api/dispatch.py.

With ``TWAIN_DISPATCH=sqs`` jobs live as ``dispatching`` rows (the outbox) and
travel to workers as SQS FIFO messages. The runner side re-queues failed /
orphaned jobs as ``dispatching`` (never ``queued``, which the login-node
runner would claim), enqueues the resume of a run whose Slurm job finished,
and relays any row whose send never happened. Message format and the queue's
FIFO keys must match api/dispatch.py; tests pin both.
"""
from __future__ import annotations

import json
import os


def mode() -> str:
    return (os.getenv("TWAIN_DISPATCH") or "db").strip().lower()


def requeue_status() -> str:
    """The status a job goes back to for another attempt."""
    return "dispatching" if mode() == "sqs" else "queued"


def message(job_id: int, session_id: str, kind: str) -> dict:
    return {
        "MessageBody": json.dumps({"job_id": int(job_id), "session_id": session_id, "kind": kind}),
        "MessageGroupId": session_id,
        "MessageDeduplicationId": f"job-{int(job_id)}",
    }


def queue_url() -> str:
    return os.getenv("TWAIN_JOB_QUEUE_URL", "")


def sqs_client():
    import boto3
    return boto3.client("sqs", region_name=os.getenv("AWS_REGION", "us-east-1"))


def send(jobs, *, client=None) -> list:
    """Send ``[(job_id, session_id, kind), ...]``; returns the ids that went out."""
    if mode() != "sqs" or not jobs or not queue_url():
        return []
    client = client or sqs_client()
    sent = []
    for job_id, session_id, kind in jobs:
        try:
            client.send_message(QueueUrl=queue_url(), **message(job_id, session_id, kind))
            sent.append(job_id)
        except Exception as exc:  # noqa: BLE001 - the relay retries
            print(f"[dispatch] SQS send for job {job_id} failed ({exc}); relay will retry")
    return sent
