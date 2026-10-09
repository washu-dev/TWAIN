"""A run's activity log: every step it took, in order, kept after it ends.

The chat's live checklist folds ``stage.progress`` events per step, so a step
shows only its latest state: an observer check that failed and later passed,
or a self-heal attempt, disappears. The checklist is also hidden once the run
ends. The events are all in ``run_events``. This replays them as a log: one
line per step change, plus stage transitions and the failure. The report shows
it, and it ships in the run's zip, whether the run succeeded or not.
"""
from __future__ import annotations

import conversations as convo

#: What the log is made of. job.log (the job's stdout) is in the run files already.
LOG_EVENT_TYPES = ("stage.progress", "stage.completed", "run.error")
_PAGE = 1000
_MAX_PAGES = 50
_MARK = {"done": "✓", "failed": "✕", "active": "…", "warn": "⚠"}


def entries(events) -> list:
    """Log entries from run events (oldest first).

    Repeated updates of a step that is still active (a queue wait re-labelled
    every poll) collapse into the first; every other change is kept.
    """
    out, last_active = [], {}
    for event in events:
        kind, payload, at = event["event_type"], event.get("payload") or {}, event["created_at"]
        at = at.isoformat() if hasattr(at, "isoformat") else str(at)
        if kind == "stage.progress":
            key = (payload.get("stage"), payload.get("step"))
            status = payload.get("status") or ""
            label = str(payload.get("label") or payload.get("step") or "")
            if label.startswith("Observer ") and label[9:10] in ("✓", "✕", "⚠"):
                # The observer's lines carry their own mark ("Observer ✕ ...").
                status = {"✓": "done", "✕": "failed", "⚠": "warn"}[label[9]]
                label = "Observer: " + label[11:]
            if status == "active":
                if last_active.get(key):
                    continue
                last_active[key] = True
            else:
                last_active.pop(key, None)
            out.append({"at": at, "stage": key[0] or "", "status": status, "label": label})
        elif kind == "stage.completed":
            out.append({"at": at, "stage": payload.get("from") or "", "status": "stage",
                        "label": f"{payload.get('from')} → {payload.get('to')}"})
        elif kind == "run.error":
            failure = payload.get("failure") or {}
            text = failure.get("headline") or (payload.get("error") or {}).get("message") or "failed"
            out.append({"at": at, "stage": payload.get("state") or failure.get("state") or "",
                        "status": "failed", "label": f"Run stopped: {text}"})
    return out


def for_run(conversation_id: str) -> list:
    """The whole log for a run, read in pages."""
    events, after = [], 0
    for _ in range(_MAX_PAGES):
        page = convo.get_activity(conversation_id, after, LOG_EVENT_TYPES, _PAGE)
        events += page
        if len(page) < _PAGE:
            break
        after = page[-1]["id"]
    return entries(events)


def as_text(log: list, title: str | None = None) -> str:
    """The log as plain text, for the zip."""
    lines = [f"TWAIN activity log{': ' + title if title else ''}", ""]
    for e in log:
        mark = "→" if e["status"] == "stage" else _MARK.get(e["status"], " ")
        lines.append(f"{e['at'][:19].replace('T', ' ')}  {e['stage']:<9}  {mark} {e['label']}")
    return "\n".join(lines) + "\n"
