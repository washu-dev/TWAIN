"""What each method did on this cluster (#188): recorded when a run ends, read
back by planning so the same request gets the same, proven method.

The same solubility request chose xtb, RDKit and OpenMM across three runs
(3e30f199, 44ad9f9e, e825d5ed). The ranking is deterministic but the LLM's pick
is not, and neither knew what had actually worked here. :func:`record` writes
one ``method_outcomes`` row per run per method (the method the run ended with,
and any a method fallback gave up on); :func:`apply` hands planning
``statemachine._proven_methods``' source, which passes the track record to the
LLM as a preference and puts proven methods first in the deterministic pick.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("twain.method_history")


# The worker has the pipeline; runner tests don't. These match statemachine's
# property_key / history_key and StateMachine._method_key (a unit test pins it).
def property_key(requested_property) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(requested_property or "").lower()).strip("_")


_FAMILIES = None


def _families() -> list:
    global _FAMILIES
    if _FAMILIES is None:
        try:
            data = json.loads((Path(__file__).resolve().parent.parent / "configs"
                               / "property_families.json").read_text(encoding="utf-8"))
            _FAMILIES = [(f["key"], re.compile(f["pattern"])) for f in data.get("families") or []]
        except (OSError, ValueError, KeyError, re.error) as exc:
            log.warning("[method-history] property families unavailable: %s", exc)
            _FAMILIES = []
    return _FAMILIES


def history_key(plan: dict) -> str:
    key = ""
    if plan.get("requested_property"):
        key = property_key(plan["requested_property"])
    else:
        for metric in plan.get("acceptance_metrics") or []:
            if isinstance(metric, dict) and metric.get("metric_name"):
                key = property_key(metric["metric_name"])
                break
    return next((family for family, pattern in _families() if pattern.search(key)),
                key) if key else ""


def method_key(plan: dict) -> str:
    method = (plan or {}).get("selected_method") or {}
    libraries = method.get("libraries") or []
    name = (method.get("calculator") or (libraries[0] if libraries else None)
            or method.get("tool_name") or "")
    return str(name).strip().lower()


def apply(db) -> None:
    """Point planning at this database's method history."""
    try:
        import statemachine
    except ImportError:          # pipeline modules not on the path (bare tests)
        return
    statemachine.use_method_history(db.method_history if hasattr(db, "method_history") else None)


def outcomes(session_id: str, plan: dict | None, execution_result: dict | None,
             verdict: str | None, failed_methods: list | None) -> list:
    """The rows a finished run contributes, or [] if no calculation ran."""
    plan = plan or {}
    prop = history_key(plan)
    if not prop:
        return []
    rows = [{"session_id": session_id, "requested_property": prop,
             "method": m.get("method"), "calculator": m.get("calculator"),
             "libraries": m.get("libraries") or [], "succeeded": False, "verdict": None}
            for m in failed_methods or [] if m.get("method")]
    result = execution_result or {}
    method = (plan.get("selected_method") or {})
    key = method_key(plan)
    if key and result.get("succeeded") is not None and result.get("status") not in (
            "deferred", "skipped", "skipped_missing_dependency"):
        rows = [r for r in rows if r["method"] != key]
        rows.append({"session_id": session_id, "requested_property": prop, "method": key,
                     "calculator": method.get("calculator"),
                     "libraries": method.get("libraries") or [],
                     "succeeded": bool(result.get("succeeded")),
                     "verdict": verdict if result.get("succeeded") else None})
    return rows


def record(db, session_id: str, orch) -> None:
    """Record what this run's methods did. Never raises."""
    try:
        sm = orch.sm
        rows = outcomes(session_id, sm._load_artifact("execution_plan"),
                        sm._load_artifact("execution_result"),
                        getattr(sm.context, "validation_result", None),
                        getattr(sm.context, "failed_methods", None))
        if rows:
            db.record_method_outcomes(rows)
    except Exception as exc:  # noqa: BLE001 - the history is a bonus, never a failure
        log.warning("[method-history] could not record %s: %s", session_id, exc)
