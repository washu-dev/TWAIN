"""Dependency installation into a temporary virtual environment (Story 5.2).

:class:`DependencyInstaller` creates an isolated venv and ``pip install``s a
bundle's ``requirements.txt`` into it, so the run doesn't perturb the host
interpreter. It enforces a wall-clock timeout (default 5 min -- fail if pip
hangs) and retries *network* failures with exponential backoff, while treating
non-network failures (e.g. "no matching distribution") as immediate, clear
errors.

The two side-effecting steps -- creating the venv and running pip -- are
injectable (``venv_builder`` / ``runner``), so the retry/backoff/timeout logic
is unit-testable offline without touching the network or building real venvs.
Real use gets the stdlib ``venv`` builder and a ``subprocess`` pip runner.
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

DEFAULT_TIMEOUT = 300.0        # 5 minutes
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1.0     # seconds; attempt N waits base * 2**(N-1)

# Substrings that identify a *transient network* failure worth retrying. Kept
# deliberately narrow: resolution errors like "no matching distribution" are NOT
# here, so a genuinely missing package fails fast with a clear message.
NETWORK_ERROR_MARKERS = (
    "temporary failure in name resolution",
    "network is unreachable",
    "connection reset",
    "connection timed out",
    "read timed out",
    "failed to establish a new connection",
    "max retries exceeded with url",
    "newconnectionerror",
    "connectionerror",
    "proxyerror",
    "[errno -2]",
    "[errno -3]",
)

# (returncode, stdout, stderr); may raise subprocess.TimeoutExpired / TimeoutError.
Runner = Callable[[List[str], float], Tuple[int, str, str]]


@dataclass
class InstallResult:
    """Outcome of a dependency install."""

    success: bool
    returncode: Optional[int]
    stdout: str
    stderr: str
    duration_seconds: float
    attempts: int
    timed_out: bool
    python_executable: Optional[str]
    message: str

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_seconds": round(self.duration_seconds, 6),
            "attempts": self.attempts,
            "timed_out": self.timed_out,
            "python_executable": self.python_executable,
            "message": self.message,
        }


def _venv_python(venv_dir: Path) -> str:
    """Path to the python interpreter inside a created venv (per-platform)."""
    if sys.platform.startswith("win"):
        return str(venv_dir / "Scripts" / "python.exe")
    return str(venv_dir / "bin" / "python")


def _default_build_venv(venv_dir: str) -> str:
    """Create a real venv (with pip) and return its python path."""
    import venv as _venv

    _venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    return _venv_python(Path(venv_dir))


def _default_runner(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
    """Run ``cmd`` capturing output; raises subprocess.TimeoutExpired on timeout."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def is_network_error(text: str) -> bool:
    """Heuristic: does pip output look like a retryable network failure?

    >>> is_network_error("Failed to establish a new connection: [Errno -2]")
    True
    >>> is_network_error("ERROR: No matching distribution found for foo==9.9")
    False
    """
    low = (text or "").lower()
    return any(marker in low for marker in NETWORK_ERROR_MARKERS)


class DependencyInstaller:
    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        runner: Optional[Runner] = None,
        venv_builder: Optional[Callable[[str], str]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_base = backoff_base
        self._run = runner or _default_runner
        self._build_venv = venv_builder or _default_build_venv
        self._sleep = sleep or time.sleep

    def install(
        self,
        requirements_path,
        target_dir,
        *,
        use_venv: bool = True,
        python_executable: Optional[str] = None,
    ) -> InstallResult:
        """Install ``requirements_path`` into a venv under ``target_dir``.

        With ``use_venv=True`` a fresh venv is built under ``target_dir`` and its
        python is used (and returned in the result). With ``use_venv=False`` the
        given ``python_executable`` (default: the current interpreter) is used
        as-is -- handy when deps are already present.
        """
        start = time.monotonic()
        requirements_path = Path(requirements_path)

        if use_venv:
            venv_dir = str(Path(target_dir) / ".venv")
            try:
                python_executable = self._build_venv(venv_dir)
            except Exception as exc:  # noqa: BLE001 - surface as a clean result
                return InstallResult(
                    success=False, returncode=None, stdout="", stderr=str(exc),
                    duration_seconds=time.monotonic() - start, attempts=0,
                    timed_out=False, python_executable=None,
                    message=f"virtualenv creation failed: {exc}",
                )
        else:
            python_executable = python_executable or sys.executable

        cmd = [
            python_executable, "-m", "pip", "install",
            "--disable-pip-version-check", "--no-input",
            "-r", str(requirements_path),
        ]

        last_rc: Optional[int] = None
        last_out = last_err = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                rc, out, err = self._run(cmd, self.timeout)
            except (subprocess.TimeoutExpired, TimeoutError):
                return InstallResult(
                    success=False, returncode=None, stdout=last_out, stderr=last_err,
                    duration_seconds=time.monotonic() - start, attempts=attempt,
                    timed_out=True, python_executable=python_executable,
                    message=f"pip install exceeded {self.timeout:.0f}s timeout",
                )

            last_rc, last_out, last_err = rc, out, err
            if rc == 0:
                return InstallResult(
                    success=True, returncode=0, stdout=out, stderr=err,
                    duration_seconds=time.monotonic() - start, attempts=attempt,
                    timed_out=False, python_executable=python_executable,
                    message="dependencies installed",
                )

            # Retry only transient network failures, with exponential backoff.
            if is_network_error(out + err) and attempt < self.max_retries:
                self._sleep(self.backoff_base * (2 ** (attempt - 1)))
                continue
            break

        clear = "network error installing dependencies" if is_network_error(last_out + last_err) \
            else "pip install failed (see stderr)"
        return InstallResult(
            success=False, returncode=last_rc, stdout=last_out, stderr=last_err,
            duration_seconds=time.monotonic() - start, attempts=attempt,
            timed_out=False, python_executable=python_executable,
            message=clear,
        )
