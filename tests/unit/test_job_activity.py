"""Live EXECUTE activity: the checklist events and the job-log follower (#160, #161).

Run from the repo root with:  pixi run pytest tests/unit/test_job_activity.py
"""
from execution_adapter.job_activity import JobActivity, plain_reason
from execution_adapter.slurm_adapter import JobState


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, event_type, payload):
        self.events.append((event_type, payload))

    def steps(self):
        return [(p["step"], p["status"], p["label"]) for t, p in self.events
                if t == "stage.progress"]

    def logs(self):
        return [p for t, p in self.events if t == "job.log"]


def test_queue_then_running_then_finished_reads_as_a_checklist():
    rec = Recorder()
    details = iter([
        {"state": "PENDING", "reason": "Resources"},
        {"state": "PENDING", "reason": "Resources"},          # unchanged: silent
        {"state": "RUNNING", "nodes": "c2-node-006", "start_time": "1791237760",
         "time_limit": "11"},
    ])
    current = {}
    activity = JobActivity(rec, detail=lambda _j: current)
    for state in (JobState.PENDING, JobState.PENDING, JobState.RUNNING):
        current = next(details)
        activity.observe("42", state)
    activity.finished("42", JobState.COMPLETED)

    assert rec.steps() == [
        ("queue", "active", "Waiting in the Slurm queue — waiting for a node with enough free CPUs/memory"),
        ("queue", "done", "Left the queue"),
        ("run", "active", "Running on c2-node-006"),
        ("run", "done", "Job finished"),
    ]
    running = next(p for t, p in rec.events if p.get("step") == "run" and p["status"] == "active")
    assert running["stage"] == "EXECUTE"
    assert running["detail"]["started_at"].startswith("2026-10-05T")
    assert running["detail"]["time_limit_minutes"] == 11


def test_a_job_cancelled_in_the_queue_fails_the_queue_step_not_the_run():
    rec = Recorder()
    activity = JobActivity(rec)
    activity.observe("42", JobState.PENDING)
    activity.finished("42", JobState.CANCELLED)
    assert rec.steps()[-1] == ("queue", "failed", "Left the queue without running: CANCELLED")


def test_plain_reasons():
    assert plain_reason("Priority") == "other jobs are ahead of it in the queue"
    assert plain_reason("(QOSMaxJobsPerUserLimit)") == "you already have the most jobs allowed running"
    assert plain_reason("SomethingNew") == "Slurm reason: SomethingNew"
    assert plain_reason(None) is None


def test_log_follower_publishes_only_new_output_from_its_cursor():
    rec = Recorder()
    pages = {0: {"content": "step 1\n", "next_offset": 7, "size": 7},
             7: {"content": "step 2\n", "next_offset": 14, "size": 14},
             14: {"content": "", "next_offset": 14, "size": 14}}
    asked = []

    def read_log(_job, offset, _limit):
        asked.append(offset)
        return pages[offset]

    activity = JobActivity(rec, read_log=read_log)
    for _ in range(3):
        activity.follow_log("42")

    assert asked == [0, 7, 14]
    assert [p["text"] for p in rec.logs()] == ["step 1\n", "step 2\n"]   # empty page: silent


def test_log_follower_jumps_to_the_live_end_when_output_outruns_a_tick():
    rec = Recorder()
    big = JobActivity.LOG_BYTES_PER_TICK
    asked = []

    def read_log(_job, offset, limit):
        asked.append(offset)
        return {"content": "x" * limit, "next_offset": offset + limit, "size": 10 * big}

    activity = JobActivity(rec, read_log=read_log)
    activity.follow_log("42")
    activity.follow_log("42")

    assert rec.logs()[0]["skipped_bytes"] == 10 * big - big - big
    assert asked[1] == 10 * big - big          # resumed at the tail, not mid-file


def test_log_follower_stops_at_the_per_job_cap():
    rec = Recorder()
    activity = JobActivity(rec, read_log=lambda _j, off, lim: {
        "content": "y" * lim, "next_offset": off + lim, "size": off + lim})
    for _ in range(200):
        activity.follow_log("42")
    logs = rec.logs()
    assert logs[-1]["truncated"] is True
    assert sum(len(p["text"]) for p in logs) <= JobActivity.LOG_BYTES_PER_JOB + JobActivity.LOG_BYTES_PER_TICK


def test_reporting_failures_never_reach_the_run():
    def broken_publish(*_a):
        raise RuntimeError("bus down")

    def broken_read(*_a):
        raise RuntimeError("No output yet")

    activity = JobActivity(broken_publish, detail=lambda _j: 1 / 0, read_log=broken_read)
    activity.step("stage", "active", "x")
    activity.observe("42", JobState.RUNNING)
    activity.finished("42", JobState.FAILED)       # no exception escapes


def test_silent_without_a_publisher():
    JobActivity(None).step("stage", "done", "nothing listens")
