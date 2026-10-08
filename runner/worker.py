"""The TWAIN worker: an SQS consumer driving runs, plus the cluster monitor (P2, #171).

Replaces the always-on polling runner for TWAIN_DISPATCH=sqs. Stateless and
horizontally scalable (an ECS Fargate service): everything a run needs lives in
RDS and S3, and no slice holds a Slurm wait -- EXECUTE submits and pauses
(detached), and the cluster monitor enqueues the resume when the job finishes.

Each consumer thread long-polls the FIFO queue for one message at a time:

1. claim the job by id (only a 'dispatching' row; a duplicate or stale message
   is acknowledged and dropped);
2. drive it with the existing ``process_job`` while a thread extends the
   message's visibility and the job's heartbeat keeps its lease;
3. mark it done and delete the message -- or, on failure, re-queue it through
   the usual retry/dead-letter path (the relay sends the retry's message) and
   delete this one.

    pixi run python -m runner.worker          # TWAIN_DISPATCH=sqs, TWAIN_JOB_QUEUE_URL, ...
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading

from runner import dispatch
from runner.db import RunnerDB
from runner.runner import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    _handle_job_failure,
    _Heartbeat,
    _reap_orphans,
    default_engine,
    process_job,
)

log = logging.getLogger("twain.worker")

#: How long a received message stays hidden; extended while its job runs.
VISIBILITY_SECONDS = int(os.getenv("TWAIN_SQS_VISIBILITY_SECONDS", "900"))


class _VisibilityExtender:
    """Keeps a message invisible while its job runs (re-sets it every half period)."""

    def __init__(self, client, queue_url, receipt, seconds=VISIBILITY_SECONDS):
        self._args = (client, queue_url, receipt, seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()

    def _run(self):
        client, queue_url, receipt, seconds = self._args
        while not self._stop.wait(seconds / 2):
            try:
                client.change_message_visibility(QueueUrl=queue_url, ReceiptHandle=receipt,
                                                 VisibilityTimeout=seconds)
            except Exception as exc:  # noqa: BLE001 - the job's lease still protects it
                log.warning("could not extend message visibility: %s", exc)


def handle_message(db, client, queue_url, msg, *, engine_factory=default_engine,
                   heartbeat_seconds=DEFAULT_HEARTBEAT_SECONDS,
                   max_attempts=DEFAULT_MAX_ATTEMPTS) -> str:
    """Process one SQS message; returns what happened (for logs and tests)."""
    receipt = msg["ReceiptHandle"]
    try:
        job_id = int(json.loads(msg["Body"])["job_id"])
    except (ValueError, KeyError, TypeError):
        client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
        return "malformed"
    job = db.claim_job_by_id(job_id)
    if job is None:
        status = db.job_status(job_id)
        if status == "dispatching":
            # Another job of the same run is in flight; leave the message to
            # reappear after its visibility timeout rather than drop the work.
            return "deferred"
        client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
        return f"skipped ({status or 'missing'})"
    heartbeat = _Heartbeat(db, job["id"], heartbeat_seconds)
    try:
        with _VisibilityExtender(client, queue_url, receipt):
            db.mark_job(job["id"], "running")
            heartbeat.start()
            process_job(job, db, engine=engine_factory())
            db.mark_job(job["id"], "done")
            outcome = "done"
    except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
        _handle_job_failure(db, job, exc, max_attempts)
        outcome = f"failed ({exc})"
    finally:
        heartbeat.stop()
    # The retry (if any) is a re-queued row the relay sends afresh.
    client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
    return outcome


def consume(db, client, queue_url, stop: threading.Event, **kw) -> None:
    while not stop.is_set():
        try:
            resp = client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1,
                                          WaitTimeSeconds=20, VisibilityTimeout=VISIBILITY_SECONDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("receive_message failed: %s", exc)
            stop.wait(5)
            continue
        for msg in resp.get("Messages", []):
            outcome = handle_message(db, client, queue_url, msg, **kw)
            log.info("job message %s: %s", msg.get("MessageId"), outcome)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if dispatch.mode() != "sqs" or not dispatch.queue_url():
        raise SystemExit("runner.worker needs TWAIN_DISPATCH=sqs and TWAIN_JOB_QUEUE_URL")
    db, client, queue_url = RunnerDB(), dispatch.sqs_client(), dispatch.queue_url()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):   # ECS stops a task with SIGTERM
        signal.signal(sig, lambda *_: stop.set())

    from runner.engine import _load
    _load()  # wire the module paths the monitor's imports need
    from execution_adapter.cluster_profile import ClusterProfile
    from execution_adapter.ris_api_adapter import RisApiAdapter

    from runner import env_proposals, inventory
    from runner.capabilities import publish_from_specs
    from runner.monitor import ClusterMonitor

    def refresh_cluster_view():
        # Planning and the capability list both follow the newest inventory.
        log.info("planning from %s", inventory.apply_latest(db))
        publish_from_specs(db)

    profile = ClusterProfile.load(os.getenv("TWAIN_SLURM_CLUSTER") or "compute2")
    adapter = RisApiAdapter(profile)
    monitor = ClusterMonitor(
        db, adapter,
        poll_seconds=float(os.getenv("TWAIN_MONITOR_POLL_SECONDS", "30")), sqs_client=client,
        reap=lambda: _reap_orphans(db, DEFAULT_LEASE_SECONDS, DEFAULT_MAX_ATTEMPTS),
        inventory=inventory.InventoryScheduler(db, adapter, profile,
                                               on_ingest=refresh_cluster_view),
        env_changes=env_proposals.EnvChangeScheduler(db, adapter, profile))
    threads = [threading.Thread(target=monitor.run, args=(stop,), name="monitor", daemon=True)]
    for i in range(max(1, int(os.getenv("TWAIN_WORKER_CONCURRENCY", "2")))):
        threads.append(threading.Thread(target=consume, args=(db, client, queue_url, stop),
                                        name=f"consumer-{i}", daemon=True))
    # Not publish(): it probes env directories that exist only on RIS storage
    # and would mark every library unavailable from here. The latest RIS
    # inventory (#185) says what the envs hold; the specs stand in until one exists.
    refresh_cluster_view()
    for t in threads:
        t.start()
    log.info("worker up: %d consumers + cluster monitor on %s", len(threads) - 1, queue_url)
    stop.wait()
    for t in threads:
        t.join(timeout=30)


if __name__ == "__main__":
    main()
