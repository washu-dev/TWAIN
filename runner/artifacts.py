"""Capture a run's artifacts into Postgres — and restore them on resume.

The state machine writes stage artifacts to the runner's local disk — JSON specs
(intent_spec, goal_graph, discovery, execution_plan, budget, execution_result)
and the generated RunBundle directory (main.py, config.yaml, requirements.txt,
inline_tests.py). :func:`capture_artifacts` copies their contents into the
``artifacts`` table so the API can serve them (the API never reads the runner's
filesystem) *and* so a run's working files are durable.

That durability is what makes resume work on any box. The run's state + context
resume from the Postgres session store, but ``context.artifacts`` holds file
*paths*, not contents — so on a fresh runner (or after ``logs/`` was cleaned) the
files those paths point to are gone and the state machine would silently lose
context. :func:`rehydrate_artifacts` is the inverse of capture: before a resume
drives the run, it writes the stored contents back to the paths the context
expects, so ``_load_artifact`` and EXECUTE find their files.
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


def _restore_file(path: str, content: str) -> bool:
    """Write ``content`` to ``path`` only if the file is missing. True if written.

    Write-if-missing keeps rehydration idempotent and safe: a same-box resume
    (files still present) is a no-op, and a partially-present bundle is filled in
    without clobbering anything already on disk.
    """
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return True


def rehydrate_artifacts(db, session_id: str, orch) -> int:
    """Restore a run's stage-artifact files from Postgres onto local disk.

    Inverse of :func:`capture_artifacts`, run before a resume drives the run. Uses
    the run's ``context.artifacts`` (restored from the session checkpoint) as the
    authoritative name→path map, and the ``artifacts`` table as the content store:

    * a simple file artifact (``intent_spec``, ``execution_plan``, …) is stored
      under its own name and restored to its path;
    * ``run_bundle`` is a directory whose files ``capture_artifacts`` flattened to
      ``run_bundle/<file>`` — each is restored under the bundle dir;
    * ``script`` is skipped (it duplicates ``run_bundle/<entrypoint>``, restored
      via the directory).

    Writing to the path the context already points at means the read path
    (``_load_artifact`` / EXECUTE) and this write path can never disagree, whatever
    the box. Returns the number of files restored. Best-effort per file.
    """
    context = getattr(getattr(orch, "sm", None), "context", None)
    artifacts = dict(getattr(context, "artifacts", {}) or {})
    if not artifacts:
        return 0
    stored = {row["name"]: row["content"] for row in db.get_artifacts(session_id)}
    if not stored:
        return 0
    restored = 0
    for name, path in artifacts.items():
        if name == "script" or not isinstance(path, str):
            continue
        if name in stored:  # simple file artifact
            if _restore_file(path, stored[name]):
                restored += 1
        else:  # directory artifact (e.g. run_bundle): files stored as "<name>/<file>"
            prefix = f"{name}/"
            for stored_name, content in stored.items():
                if stored_name.startswith(prefix):
                    rel = stored_name[len(prefix):]
                    if _restore_file(os.path.join(path, rel), content):
                        restored += 1
    return restored
