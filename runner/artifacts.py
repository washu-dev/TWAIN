"""Capture a finished run's artifacts into Postgres.

The state machine writes stage artifacts to the runner's local disk — JSON specs
(intent_spec, goal_graph, discovery, execution_plan, budget, execution_result)
and the generated RunBundle directory (main.py, config.yaml, requirements.txt,
inline_tests.py). We copy their contents into the ``artifacts`` table so the API
can serve them; the API never reads the runner's filesystem.
"""
import os

MAX_BYTES = 512 * 1024  # generated files are small; cap defensively

_KIND_BY_EXT = {
    ".json": "json",
    ".py": "python",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".txt": "text",
    ".cfg": "text",
    ".md": "text",
}


def _kind(path: str) -> str:
    return _KIND_BY_EXT.get(os.path.splitext(path)[1].lower(), "text")


def _safe_read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = f.read(MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_BYTES:
        data = data[:MAX_BYTES] + "\n… [truncated]"
    return data


def capture_artifacts(db, session_id: str, orch) -> int:
    """Persist every artifact referenced by the run's context. Returns count.

    ``run_bundle`` is a directory — each file inside is stored as
    ``run_bundle/<filename>``. ``script`` is skipped (it duplicates
    run_bundle/main.py). Best-effort: unreadable files are skipped.
    """
    context = getattr(getattr(orch, "sm", None), "context", None)
    artifacts = dict(getattr(context, "artifacts", {}) or {})
    saved = 0
    for name, path in artifacts.items():
        if name == "script" or not isinstance(path, str):
            continue
        if os.path.isdir(path):
            for fname in sorted(os.listdir(path)):
                fpath = os.path.join(path, fname)
                if not os.path.isfile(fpath):
                    continue
                content = _safe_read(fpath)
                if content is not None:
                    db.upsert_artifact(session_id, f"{name}/{fname}", content, _kind(fname))
                    saved += 1
        elif os.path.isfile(path):
            content = _safe_read(path)
            if content is not None:
                db.upsert_artifact(session_id, name, content, _kind(path))
                saved += 1
    return saved
