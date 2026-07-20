"""Capture a finished run's artifacts into Postgres.

The state machine writes stage artifacts to the runner's local disk — JSON specs
(intent_spec, goal_graph, discovery, execution_plan, budget, execution_result)
and the generated RunBundle directory (main.py, config.yaml, requirements.txt,
inline_tests.py). We also capture the files the run *produced* (results.csv,
solver logs, …) from the execution ``artifacts_dir`` under ``output/``. All of it
goes into the ``artifacts`` table so the API can serve it; the API never reads the
runner's filesystem.
"""
import json
import os
from pathlib import Path

MAX_BYTES = 512 * 1024  # generated files are small; cap defensively

# Single-file JSON specs a later stage reads as its input. On a re-run (a fresh
# process) these must be restored to disk from the DB before the pipeline drives.
SPEC_ARTIFACTS = ("intent_spec", "goal_graph", "discovery", "execution_plan")

_KIND_BY_EXT = {
    ".json": "json",
    ".py": "python",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".txt": "text",
    ".cfg": "text",
    ".md": "text",
    ".csv": "text",
    ".log": "text",
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


def rematerialize_inputs(db, session_id: str, orch) -> int:
    """Restore the surviving upstream spec artifacts to disk for a re-run. Returns count.

    A re-run runs in a fresh process whose local filesystem no longer holds the
    original run's artifact files, but the pipeline's stage handlers read their
    inputs from ``context.artifacts`` paths on disk. After a rewind, only the
    stages *before* the rewind target still reference their specs; for each such
    single-file spec, fetch its content from the ``artifacts`` table and rewrite
    it under this run's artifacts dir, repointing ``context.artifacts`` at the
    fresh path. Best-effort: a spec not in the DB is left untouched (in local dev
    the original file is usually still on disk anyway).
    """
    sm = getattr(orch, "sm", None)
    context = getattr(sm, "context", None)
    if context is None:
        return 0
    art_dir = Path(getattr(sm, "artifacts_dir", "."))
    art_dir.mkdir(parents=True, exist_ok=True)
    restored = 0
    for name in list(getattr(context, "artifacts", {}) or {}):
        if name not in SPEC_ARTIFACTS:
            continue
        row = db.get_artifact(session_id, name)
        if not row or row.get("content") is None:
            continue
        path = art_dir / f"{name}_{session_id}.json"
        try:
            path.write_text(row["content"], encoding="utf-8")
        except OSError:
            continue
        context.artifacts[name] = str(path)
        restored += 1
    return restored


def capture_artifacts(db, session_id: str, orch) -> int:
    """Persist every artifact referenced by the run's context. Returns count.

    ``run_bundle`` is a directory — each file inside is stored as
    ``run_bundle/<filename>``. ``script`` is skipped (it duplicates
    run_bundle/main.py). Best-effort: unreadable files are skipped.
    """
    context = getattr(getattr(orch, "sm", None), "context", None)
    artifacts = dict(getattr(context, "artifacts", {}) or {})
    saved = 0
    bundle_names: set[str] = set()  # basenames captured from run_bundle (the inputs)
    exec_result_path: str | None = None
    for name, path in artifacts.items():
        if name == "script" or not isinstance(path, str):
            continue
        if name == "execution_result":
            exec_result_path = path
        if os.path.isdir(path):
            for fname in sorted(os.listdir(path)):
                fpath = os.path.join(path, fname)
                if not os.path.isfile(fpath):
                    continue
                content = _safe_read(fpath)
                if content is not None:
                    db.upsert_artifact(session_id, f"{name}/{fname}", content, _kind(fname))
                    saved += 1
                    if name == "run_bundle":
                        bundle_names.add(fname)
        elif os.path.isfile(path):
            content = _safe_read(path)
            if content is not None:
                db.upsert_artifact(session_id, name, content, _kind(path))
                saved += 1
    saved += _capture_outputs(db, session_id, exec_result_path, bundle_names)
    return saved


def _capture_outputs(db, session_id: str, exec_result_path: str | None, skip: set) -> int:
    """Capture files the run produced (results.csv, solver logs) as ``output/<name>``.

    The execution adapter writes outputs to ``execution_result["artifacts_dir"]``;
    that directory also holds copies of the input bundle, so we skip any filename
    already stored from run_bundle. Best-effort — returns the count saved.
    """
    if not exec_result_path or not os.path.isfile(exec_result_path):
        return 0
    try:
        with open(exec_result_path, encoding="utf-8") as f:
            result = json.load(f)
    except (OSError, ValueError):
        return 0
    out_dir = result.get("artifacts_dir") if isinstance(result, dict) else None
    if not out_dir or not os.path.isdir(out_dir):
        return 0
    saved = 0
    for fname in sorted(os.listdir(out_dir)):
        if fname in skip:
            continue
        fpath = os.path.join(out_dir, fname)
        if not os.path.isfile(fpath):
            continue
        content = _safe_read(fpath)
        if content is not None:
            db.upsert_artifact(session_id, f"output/{fname}", content, _kind(fname))
            saved += 1
    return saved
