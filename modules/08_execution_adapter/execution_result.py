"""ExecutionResult -- the output contract of the execution adapter (Story 5.2).

A single, serializable record of *one* local run of a RunBundle: how it ended
(:class:`ExecutionStatus`), the captured logs (stdout/stderr), the timing and
resource metrics observability wants, any flagged anomalies, and where the
artifacts landed. :meth:`ExecutionResult.to_dict` gives a JSON-safe dict the
orchestrator can checkpoint or hand to the result interpreter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ExecutionStatus(str, Enum):
    """Terminal disposition of a local run.

    Subclassing ``str`` makes the value JSON-serializable and comparable to a
    plain string, so ``result.status == "success"`` works too.
    """

    SUCCESS = "success"            # main.py exited 0
    FAILED = "failed"              # main.py exited non-zero (non-dependency)
    TIMEOUT = "timeout"            # exceeded the wall-clock budget; process killed
    DEPENDENCY_ERROR = "dependency_error"  # missing/broken Python dependency
    SETUP_FAILED = "setup_failed"  # temp dir / copy / venv creation failed
    SMOKE_FAILED = "smoke_failed"  # inline_tests.py failed before the real run


@dataclass
class ExecutionResult:
    """Everything the orchestrator learns from one local execution."""

    status: ExecutionStatus
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0

    # Resource metrics (None when psutil is unavailable or the process was too
    # short-lived to sample).
    peak_memory_mb: Optional[float] = None
    peak_cpu_percent: Optional[float] = None
    peak_open_files: Optional[int] = None

    anomalies: List[str] = field(default_factory=list)
    artifacts_dir: Optional[str] = None
    tool_name: Optional[str] = None
    command: Optional[List[str]] = None
    message: str = ""

    # Sub-step logs, kept for observability/debugging (dicts, not objects, so
    # the whole result stays trivially serializable).
    install_log: Optional[dict] = None
    smoke_log: Optional[dict] = None

    @property
    def succeeded(self) -> bool:
        """True only on a clean, zero-exit run."""
        return self.status == ExecutionStatus.SUCCESS

    def to_dict(self) -> dict:
        """A JSON-safe dict of the result (status rendered as its string value)."""
        return {
            "status": self.status.value,
            "succeeded": self.succeeded,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": round(self.duration_seconds, 6),
            "peak_memory_mb": self.peak_memory_mb,
            "peak_cpu_percent": self.peak_cpu_percent,
            "peak_open_files": self.peak_open_files,
            "anomalies": list(self.anomalies),
            "artifacts_dir": self.artifacts_dir,
            "tool_name": self.tool_name,
            "command": list(self.command) if self.command else None,
            "message": self.message,
            "install_log": self.install_log,
            "smoke_log": self.smoke_log,
        }
