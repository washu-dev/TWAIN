"""The cluster monitor: one watcher for every Slurm job a run is paused on (P2, #171).

Detached EXECUTE submits a job, records it in ``cluster_jobs`` and pauses the
run -- no process waits on it. This loop does the waiting for all of them:

* polls each open job through the RIS API (at most every ``poll_seconds``),
  woken at once by ris-api webhooks (the receiver's NOTIFY on ris_job_events);
* publishes what it sees as the run's activity -- queued (with Slurm's reason),
  running on its node, finished -- plus the job's stdout tail, the same events
  the blocking path published, so the chat checklist works unchanged;
* when a job finishes, marks it and enqueues + sends the run's ``resume``,
  which collects it on whichever worker takes the message;
* relays outbox jobs whose SQS send never happened, and reaps jobs held by a
  worker that died.

Exactly one monitor is active however many workers run: each tick needs the
Postgres advisory lock held on its own connection, so a second worker's monitor
idles until the first dies -- then takes over from the state in the DB.
"""
from __future__ import annotations

import select
import threading
import time
from collections.abc import Callable

from runner import dispatch


def _job_activity_class():
    """JobActivity, whether or not the module aliases are wired (it is pure stdlib).

    The worker wires them (runner.engine._load) before the monitor runs; a bare
    environment -- CI's runner job installs only psycopg2 and pytest -- has not.
    """
    try:
        from execution_adapter.job_activity import JobActivity
    except ImportError:
        import sys
        from pathlib import Path
        adapter_dir = str(Path(__file__).resolve().parent.parent
                          / "modules" / "08_execution_adapter")
        if adapter_dir not in sys.path:
            sys.path.insert(0, adapter_dir)
        from job_activity import JobActivity
    return JobActivity


#: pg_advisory_lock key for the single active monitor.
MONITOR_LOCK = "twain-cluster-monitor"


class ClusterMonitor:
    def __init__(self, db, adapter, *, poll_seconds: float = 30.0,
                 relay_grace_seconds: float = 30.0, sqs_client=None,
                 reap: Callable[[], None] | None = None, clock=time.monotonic):
        """``adapter``: a RisApiAdapter (poll with its 404 -> accounting fallback,
        ``last_detail``, ``stdout_page``). ``reap``: the runner's orphan reaper."""
        self.db = db
        self.adapter = adapter
        self.poll_seconds = poll_seconds
        self.relay_grace_seconds = relay_grace_seconds
        self.sqs_client = sqs_client
        self.reap = reap
        self._clock = clock
        self._activities: dict = {}

    # ------------------------------------------------------------------ one pass
    def tick(self, *, force=()) -> dict:
        """One pass: relay, reap, then observe due jobs (and any in ``force``)."""
        stats = {"relayed": 0, "observed": 0, "finished": 0}
        stats["relayed"] = self._relay()
        if self.reap is not None:
            try:
                self.reap()
            except Exception as exc:  # noqa: BLE001 - one bad pass must not stop the loop
                print(f"[monitor] reaper failed: {exc}")
        due = {r["ris_job_id"]: r for r in self.db.open_cluster_jobs(self.poll_seconds)}
        for job_id in force:
            row = due.get(str(job_id)) or self.db.cluster_job(str(job_id))
            if row and row.get("status") == "submitted":
                due[row["ris_job_id"]] = row
        for row in due.values():
            try:
                finished = self._observe(row)
            except Exception as exc:  # noqa: BLE001
                print(f"[monitor] observing Slurm job {row['ris_job_id']} failed: {exc}")
                continue
            stats["observed"] += 1
            stats["finished"] += int(finished)
        return stats

    def _relay(self) -> int:
        jobs = self.db.unpublished_jobs(self.relay_grace_seconds)
        sent = dispatch.send(jobs, client=self.sqs_client) if jobs else []
        self.db.mark_published(sent)
        return len(sent)

    def _activity(self, row):
        JobActivity = _job_activity_class()
        job_id, session_id = row["ris_job_id"], row["session_id"]
        if job_id not in self._activities:
            self._activities[job_id] = JobActivity(
                lambda t, p, sid=session_id: self.db.insert_run_event(sid, t, p),
                detail=lambda jid: self.adapter.last_detail.get(jid, {}),
                read_log=self.adapter.stdout_page,
                log_offset=row.get("log_offset") or 0)
        return self._activities[job_id]

    def _observe(self, row) -> bool:
        """Poll one job, publish what changed; True if it just finished."""
        job_id, session_id = row["ris_job_id"], row["session_id"]
        state = self.adapter.poll(job_id)          # SlurmError -> retried next tick
        activity = self._activity(row)
        activity.observe(job_id, state)
        detail = self.adapter.last_detail.get(job_id, {})
        self.db.update_cluster_poll(job_id, slurm_state=str(detail.get("state") or state.value),
                                    node=detail.get("nodes"), reason=detail.get("reason"),
                                    log_offset=activity.log_offset)
        if not state.is_terminal:
            return False
        self.db.mark(job_id, "finished")
        sent = dispatch.send(self.db.enqueue_resume(session_id), client=self.sqs_client)
        self.db.mark_published(sent)
        self._activities.pop(job_id, None)
        return True

    # ------------------------------------------------------------------ the loop
    def run(self, stop: threading.Event) -> None:
        """Hold the leader lock and tick until ``stop``; idle while another leads."""
        conn = None
        while not stop.is_set():
            try:
                if conn is None:
                    conn = self._leader_connection()
                if conn is None:                     # another worker's monitor leads
                    stop.wait(self.poll_seconds)
                    continue
                woken = self._wait(conn, self.poll_seconds, stop)
                self.tick(force=woken)
            except Exception as exc:  # noqa: BLE001 - reconnect and carry on
                print(f"[monitor] {exc}; reconnecting")
                try:
                    if conn is not None:
                        conn.close()
                except Exception as close_exc:  # noqa: BLE001 - already reconnecting
                    print(f"[monitor] closing the old connection failed: {close_exc}")
                conn = None
                stop.wait(5)
        if conn is not None:
            conn.close()

    def _leader_connection(self):
        """A LISTENing connection holding the leader lock, or None if it's taken."""
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
        conn = self.db._connect()
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(hashtext(%s));", (MONITOR_LOCK,))
        if not cur.fetchone()[0]:
            cur.close()
            conn.close()
            return None
        cur.execute("LISTEN ris_job_events;")
        cur.close()
        print("[monitor] leading: watching cluster jobs")
        return conn

    @staticmethod
    def _wait(conn, timeout: float, stop: threading.Event) -> list:
        """Sleep up to ``timeout`` or until a webhook NOTIFY; return the job ids."""
        deadline = time.monotonic() + timeout
        woken = []
        while not stop.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if select.select([conn], [], [], min(left, 5.0)) != ([], [], []):
                conn.poll()
                woken += [n.payload for n in conn.notifies]
                conn.notifies.clear()
                if woken:
                    break
        return woken
