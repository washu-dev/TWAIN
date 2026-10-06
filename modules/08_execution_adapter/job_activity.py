"""Live activity reporting for a Slurm EXECUTE (#160, #161).

Between "Plan approved" and the result, a run used to publish nothing but
``stage.started``/``stage.completed`` -- the UI could only say "Working…" for
however long staging, the queue, and the job took. :class:`JobActivity` turns
the adapter's milestones into ``stage.progress`` events, and the running job's
stdout into ``job.log`` events, both published through the same run-event
stream the UI already follows.

Event shapes (the ``payload`` of a run_events row):

``stage.progress``
    ``{"stage": "EXECUTE", "step": <key>, "status": "active"|"done"|"failed",
    "label": <one line for a human>, "detail": {...}}`` -- ``step`` is one of
    :data:`EXECUTE_STEPS`; a later event for the same step replaces the earlier
    one, so the UI keeps the newest per step.

``job.log``
    ``{"job_id": ..., "text": <new stdout>, "skipped_bytes": n, "truncated": bool}``
    -- appended in order; ``skipped_bytes`` > 0 means output came faster than
    one poll's budget and the middle was skipped to stay at the live end.

Reporting is best effort by construction: every publish is guarded, and a
failure to read the log only stops the log, never the run.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Dict, Optional

#: The EXECUTE checklist, in order -- also what the UI shows as upcoming steps.
EXECUTE_STEPS = ("stage", "preflight", "submit", "queue", "run", "fetch")

#: Slurm pending reasons, in words a researcher can act on (or wait out).
_PENDING_REASONS = {
    "priority": "other jobs are ahead of it in the queue",
    "resources": "waiting for a node with enough free CPUs/memory",
    "dependency": "waiting for another job to finish",
    "begintime": "scheduled to start later",
    "reqnodenotavail": "a needed node is unavailable (maintenance?)",
    "partitionnodelimit": "the request is larger than the partition allows",
    "partitiontimelimit": "the time limit is longer than the partition allows",
    "qosmaxjobsperuserlimit": "you already have the most jobs allowed running",
    "assocgrpcpulimit": "the account's CPU allowance is in use",
    "assocgrpmemlimit": "the account's memory allowance is in use",
    "assocgrpgrestres": "the account's GPU allowance is in use",
    "none": "just submitted",
}

Publish = Callable[[str, Dict[str, Any]], None]


def plain_reason(reason: Optional[str]) -> Optional[str]:
    """Slurm's pending ``reason`` code in plain words (None when there is none)."""
    if not reason:
        return None
    key = str(reason).strip().strip("()").replace(" ", "").lower()
    return _PENDING_REASONS.get(key, f"Slurm reason: {reason}")


def _iso(epoch) -> Optional[str]:
    """ris-api reports times as epoch seconds; the UI wants ISO 8601."""
    try:
        value = int(str(epoch))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return _dt.datetime.fromtimestamp(value, _dt.timezone.utc).isoformat()


class JobActivity:
    """Publishes one EXECUTE's checklist and job log.

    ``publish(event_type, payload)`` is the run's event publisher (None turns
    every call into a no-op). ``detail(job_id)`` returns the latest scheduler
    detail for a job (the RIS API's ``GET /jobs/{id}`` body) or ``{}``;
    ``read_log(job_id, offset, limit)`` returns one ris-api output page, or is
    None when the backend can't follow output (SSH).
    """

    #: Most stdout bytes published per poll, and per job in total.
    LOG_BYTES_PER_TICK = 8 * 1024
    LOG_BYTES_PER_JOB = 256 * 1024

    def __init__(self, publish: Optional[Publish], *, detail=None, read_log=None,
                 log_offset: int = 0):
        """``log_offset``: where a previous follower stopped (the cluster monitor
        persists it, so a new monitor neither repeats nor skips output)."""
        self._publish = publish
        self._detail = detail or (lambda _job_id: {})
        self._read_log = read_log
        self._seen = None
        self._log_offset = int(log_offset or 0)
        self._log_sent = 0
        self._log_done = False
        self._ran = False

    # ------------------------------------------------------------------ publish
    def _emit(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._publish is None:
            return
        try:
            self._publish(event_type, payload)
        except Exception:  # noqa: BLE001 - reporting must never break a run
            pass

    def step(self, step: str, status: str, label: str, **detail) -> None:
        self._emit("stage.progress", {
            "stage": "EXECUTE", "step": step, "status": status,
            "label": label, "detail": detail,
        })

    # ------------------------------------------------------------ state changes
    def observe(self, job_id: str, state) -> None:
        """Called on every poll with the job's state; reports what changed."""
        try:
            detail = self._detail(job_id) or {}
        except Exception:  # noqa: BLE001
            detail = {}
        value = getattr(state, "value", str(state))
        key = (value, detail.get("reason"), detail.get("nodes"))
        if key != self._seen:
            self._seen = key
            if value == "pending":
                reason = plain_reason(detail.get("reason"))
                self.step("queue", "active",
                          "Waiting in the Slurm queue" + (f" — {reason}" if reason else ""),
                          job_id=job_id, reason=detail.get("reason"),
                          partition=detail.get("partition"))
            elif value == "running":
                self._ran = True
                node = detail.get("nodes")
                self.step("queue", "done", "Left the queue", job_id=job_id)
                self.step("run", "active",
                          f"Running on {node}" if node else "Running",
                          job_id=job_id, node=node,
                          started_at=_iso(detail.get("start_time")),
                          time_limit_minutes=_minutes(detail.get("time_limit")))
        if value in ("running",) or getattr(state, "is_terminal", False):
            self.follow_log(job_id)

    def finished(self, job_id: str, state) -> None:
        """The job reached a terminal state: close the queue/run steps."""
        value = getattr(state, "value", str(state))
        self.follow_log(job_id)
        if value == "completed":
            if not self._ran:  # finished between two polls: never seen running
                self.step("queue", "done", "Left the queue", job_id=job_id)
            self.step("run", "done", "Job finished", job_id=job_id, state=value)
        elif self._ran:
            self.step("run", "failed", f"Job ended: {value.upper()}",
                      job_id=job_id, state=value)
        else:
            self.step("queue", "failed", f"Left the queue without running: {value.upper()}",
                      job_id=job_id, state=value)

    @property
    def log_offset(self) -> int:
        return self._log_offset

    # --------------------------------------------------------------------- log
    def follow_log(self, job_id: str) -> None:
        """Publish whatever stdout the job has written since the last call."""
        if self._read_log is None or self._log_done:
            return
        try:
            page = self._read_log(job_id, self._log_offset, self.LOG_BYTES_PER_TICK)
        except Exception:  # noqa: BLE001 - "no output yet" and blips alike
            return
        text = page.get("content") or ""
        next_offset = int(page.get("next_offset") or self._log_offset)
        size = int(page.get("size") or next_offset)
        skipped = 0
        # Output arrived faster than one tick's budget: jump to the live end
        # next time rather than falling ever further behind.
        if size - next_offset > self.LOG_BYTES_PER_TICK:
            skipped = size - self.LOG_BYTES_PER_TICK - next_offset
            next_offset = size - self.LOG_BYTES_PER_TICK
        self._log_offset = next_offset
        if not text and not skipped:
            return
        self._log_sent += len(text.encode("utf-8"))
        truncated = self._log_sent >= self.LOG_BYTES_PER_JOB
        self._emit("job.log", {"job_id": job_id, "text": text,
                               "skipped_bytes": max(0, skipped), "truncated": truncated})
        if truncated:
            self._log_done = True


def _minutes(value) -> Optional[int]:
    """ris-api's ``time_limit`` is minutes as a string ("11"); None if absent."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
