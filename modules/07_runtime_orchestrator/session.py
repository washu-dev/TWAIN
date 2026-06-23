"""Session state & persistence for the runtime orchestrator (Story 2.5).

A :class:`RunSession` is the complete, serializable state of one researcher run:
which pipeline ``State`` it is in, its ``RunStatus``, the state-machine
``Context`` (the guard inputs), every artifact produced so far, the loop
counters, and any terminal error. Because it is fully serializable it can be
written to a checkpoint file after every stage and reloaded to resume an
interrupted run from the last good state.

:class:`Session` owns the on-disk checkpoint: one JSON file per session, written
atomically (temp file + ``os.replace``) so a crash mid-write never corrupts the
last good checkpoint. Story 2.5 asks for a checkpoint "every 30s or after each
stage transition"; the orchestrator checkpoints after each stage, and
:meth:`Session.due_for_periodic_checkpoint` supports the time-based interval.

Persistence to the SQLite ``sessions`` table lives in
``modules/14_provenance_memory/store.py`` (:class:`store.Store`); this module is
the in-memory model plus the file checkpoint. The two share the same
:meth:`RunSession.to_dict` representation.
"""
import _bootstrap  # noqa: F401  (sys.path wiring; must come first)

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Optional

from states import State, Context

# Default location for checkpoint files: alongside this module so a run started
# from any working directory persists to a stable, discoverable place.
DEFAULT_CHECKPOINT_DIR = Path(__file__).resolve().parent / "session_logs"

# Story 2.5: "Save to disk every 30s or after each stage transition."
DEFAULT_CHECKPOINT_INTERVAL_S = 30


class RunStatus(str, Enum):
    """Lifecycle status of a run, tracked by the orchestrator.

    This is intentionally *separate* from the pipeline :class:`State` enum:
    ``State`` says *where* in the pipeline we are, ``RunStatus`` says *how* the
    run is doing. Keeping ERROR/PAUSED here means we never have to add states to
    the Story 2.1 enum (whose test suite asserts an exact set of states).
    """

    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunSession:
    """Serializable, resumable state of a single run."""

    session_id: str
    researcher_id: str
    state: str = State.INTAKE.name
    status: str = RunStatus.RUNNING.value
    # The state-machine guard inputs (clarified / plan_approved / ...).
    context: Dict = field(default_factory=lambda: asdict(Context()))
    # stage name -> produced artifact (already JSON-serializable dicts).
    artifacts: Dict = field(default_factory=dict)
    # Loop-prevention counters (Story 6.3 / "prevents infinite loops").
    transition_count: int = 0
    replan_count: int = 0
    correct_count: int = 0
    # Populated only when status == ERROR; the classified, actionable error.
    error: Optional[Dict] = None
    # Path to the append-only provenance event log for this run, if any.
    provenance_log_path: Optional[str] = None
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)

    # --------------------------------------------------------------- live views
    def get_state(self) -> State:
        return State[self.state]

    def set_state(self, state: State) -> None:
        self.state = state.name

    def get_status(self) -> RunStatus:
        return RunStatus(self.status)

    def set_status(self, status: RunStatus) -> None:
        self.status = status.value

    def get_context(self) -> Context:
        """Rebuild a live :class:`Context` from the serialized dict."""
        return Context(**self.context)

    def set_context(self, context: Context) -> None:
        self.context = asdict(context)

    # ----------------------------------------------------------- serialization
    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "RunSession":
        # Tolerate unknown keys so a newer checkpoint can be read by older code.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class Session:
    """Owns one run's :class:`RunSession` plus its on-disk JSON checkpoint."""

    def __init__(
        self,
        session_id: str,
        researcher_id: str = "",
        checkpoint_dir: Optional[str] = None,
        clock: Callable[[], float] = None,
    ):
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else DEFAULT_CHECKPOINT_DIR
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_interval = DEFAULT_CHECKPOINT_INTERVAL_S
        # Injectable monotonic-ish clock keeps the periodic-checkpoint logic
        # testable without sleeping.
        import time as _time
        self._clock = clock or _time.time
        self._last_checkpoint = self._clock()

        if self.path(session_id).is_file():
            self.run_session = self.load(session_id)
        else:
            self.run_session = RunSession(session_id=session_id, researcher_id=researcher_id)
            self.checkpoint()

    # --------------------------------------------------------------- file paths
    def path(self, session_id: Optional[str] = None) -> Path:
        sid = session_id or self.run_session.session_id
        return self.checkpoint_dir / f"{sid}.json"

    @classmethod
    def exists(cls, session_id: str, checkpoint_dir: Optional[str] = None) -> bool:
        directory = Path(checkpoint_dir) if checkpoint_dir else DEFAULT_CHECKPOINT_DIR
        return (directory / f"{session_id}.json").is_file()

    # ----------------------------------------------------------------- IO
    def load(self, session_id: Optional[str] = None) -> RunSession:
        with open(self.path(session_id), "r", encoding="utf-8") as f:
            return RunSession.from_dict(json.load(f))

    def checkpoint(self) -> None:
        """Atomically persist the current RunSession to its JSON file.

        Writes to a temp file in the same directory then ``os.replace`` (atomic
        on POSIX/NTFS), so the previous good checkpoint is never half-overwritten.
        """
        self.run_session.updated_at = _utcnow_iso()
        target = self.path()
        fd, tmp = tempfile.mkstemp(dir=str(self.checkpoint_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.run_session.to_dict(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self._last_checkpoint = self._clock()

    def due_for_periodic_checkpoint(self) -> bool:
        """True once ``checkpoint_interval`` seconds have elapsed (Story 2.5)."""
        return (self._clock() - self._last_checkpoint) >= self.checkpoint_interval
