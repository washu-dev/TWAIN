"""Single source of truth for where TWAIN writes runtime files.

Every StateMachine / Orchestrator collaborator that persists something -- session
checkpoints, crash-recovery files, the provenance event log, stage artifacts, the
SQLite session store, the event-bus log -- resolves its *default* location
through here. The result is that all run output lands under one repo-anchored
``logs/`` tree regardless of the process's current working directory, instead of
scattering into CWD-relative ``tempLogs/`` / ``event_bus.log`` and ``~/.twain``.

Anchoring is by ``__file__`` (this file lives at the repo root), so it is
immune to ``cd`` / launch-dir differences. Callers may still pass an explicit
path to override any of these defaults (tests do this with ``tmp_path``).
"""
from pathlib import Path

# This module sits at the repository root, so its parent IS the repo root.
REPO_ROOT = Path(__file__).resolve().parent

# Static, version-controlled inputs (JSON schemas, etc.) shipped with the repo.
SCHEMAS_DIR = REPO_ROOT / "schemas"

# DFTB+ Slater-Koster parameter files (.skf). Not committed (CC-BY-SA data,
# fetched by runner/fetch_slako.sh into this repo-anchored dir); DFTB_PREFIX is
# pointed here via pixi's [activation.env]. Kept here so the execution adapter
# can fall back to it when DFTB_PREFIX is unset in the environment.
SLAKO_DIR = REPO_ROOT / "slako"

# Prompt/constraint text + scratch data used by the semantic-parsing layer.
INTELLIGENCE_DIR = REPO_ROOT / "Intelligence Layer"

# The one place all runtime output goes.
LOGS_ROOT = REPO_ROOT / "logs"

# Per-run bookkeeping: session checkpoints (<sid>.json), state-machine crash
# recovery (<sid>.sm.json), and the provenance event log (<sid>.events.jsonl).
SESSIONS_DIR = LOGS_ROOT / "sessions"

# Stage artifacts produced by the pipeline handlers (intent_spec.json, ...).
# Nested under the sessions dir because that is where the orchestrator scopes a
# run's artifacts (``<checkpoint_dir>/artifacts``); keeping the default here in
# the same place means a single ``logs/sessions/artifacts`` folder is used
# instead of also leaving an empty top-level ``logs/artifacts`` behind.
ARTIFACTS_DIR = SESSIONS_DIR / "artifacts"

# SQLite session store (Story 2.5 / 7.1).
DB_PATH = LOGS_ROOT / "sessions.db"

# Event-bus schema-validation log.
EVENT_BUS_LOG = LOGS_ROOT / "event_bus.log"


def ensure_dirs() -> None:
    """Create the log directories if they don't exist (idempotent)."""
    for directory in (LOGS_ROOT, SESSIONS_DIR, ARTIFACTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
