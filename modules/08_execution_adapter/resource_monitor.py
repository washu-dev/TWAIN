"""Resource monitoring + graceful process shutdown (Story 5.2).

:class:`ResourceMonitor` polls a running process (and its children) on a fixed
interval in a background thread, recording peak memory, CPU%, and open-file
count, and flags anomalies such as crossing a memory ceiling (default 1 GiB ->
potential OOM). :func:`terminate_process` implements the graceful shutdown the
adapter uses on timeout: SIGTERM the process tree, wait, then SIGKILL whatever
is still alive.

``psutil`` is the measurement backend. It is imported defensively: if it is not
installed the monitor still runs and returns a well-formed :class:`ResourceUsage`
with ``monitored=False`` and ``None`` metrics, and shutdown falls back to
``subprocess.Popen``'s own terminate/kill -- so the adapter degrades instead of
crashing. Real deployments (and this repo's pixi env) have psutil, so metrics
are captured.
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Union

try:  # psutil is the measurement backend; degrade gracefully without it.
    import psutil
except ImportError:  # pragma: no cover - exercised only in a psutil-less env
    psutil = None

DEFAULT_POLL_INTERVAL = 5.0        # seconds between polls (per the story)
DEFAULT_MEMORY_LIMIT_MB = 1024.0   # 1 GiB -> flagged as OOM risk


def psutil_available() -> bool:
    """Whether the psutil measurement backend is importable."""
    return psutil is not None


@dataclass
class ResourceUsage:
    """Peak resource metrics captured over a monitored run."""

    peak_memory_mb: Optional[float] = None
    peak_cpu_percent: Optional[float] = None
    peak_open_files: Optional[int] = None
    samples: int = 0
    monitored: bool = False
    anomalies: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "peak_memory_mb": self.peak_memory_mb,
            "peak_cpu_percent": self.peak_cpu_percent,
            "peak_open_files": self.peak_open_files,
            "samples": self.samples,
            "monitored": self.monitored,
            "anomalies": list(self.anomalies),
        }


class ResourceMonitor:
    """Polls a process tree on ``interval`` and records peak usage.

    Usage::

        mon = ResourceMonitor(popen.pid, interval=5.0, memory_limit_mb=1024)
        mon.start()
        ...                       # run the workload
        usage = mon.stop()        # join the thread, return ResourceUsage
    """

    def __init__(
        self,
        pid: int,
        *,
        interval: float = DEFAULT_POLL_INTERVAL,
        memory_limit_mb: float = DEFAULT_MEMORY_LIMIT_MB,
    ):
        self.pid = pid
        self.interval = interval
        self.memory_limit_mb = memory_limit_mb

        self._peak_memory_mb: Optional[float] = None
        self._peak_cpu_percent: Optional[float] = None
        self._peak_open_files: Optional[int] = None
        self._samples = 0
        self._anomalies: List[str] = []

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc = None
        if psutil is not None:
            try:
                self._proc = psutil.Process(pid)
            except psutil.Error:  # process already gone / inaccessible
                self._proc = None

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> "ResourceMonitor":
        """Begin polling in a daemon thread (no-op if psutil is unavailable)."""
        if self._proc is None:
            return self  # nothing to measure; stop() will report monitored=False
        self._poll_once()  # seed immediately so short-lived runs get a sample
        self._thread = threading.Thread(target=self._loop, name="resource-monitor", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> ResourceUsage:
        """Stop polling, join the thread, and return the captured peaks."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5.0)
            self._thread = None
        return self.usage()

    def usage(self) -> ResourceUsage:
        return ResourceUsage(
            peak_memory_mb=self._peak_memory_mb,
            peak_cpu_percent=self._peak_cpu_percent,
            peak_open_files=self._peak_open_files,
            samples=self._samples,
            monitored=self._proc is not None,
            anomalies=list(self._anomalies),
        )

    # -- polling -------------------------------------------------------------
    def _loop(self) -> None:
        # Event.wait doubles as an interruptible sleep: returns True when stop()
        # is signalled, so shutdown is prompt even with a long interval.
        while not self._stop.wait(self.interval):
            if not self._poll_once():
                break  # process gone -> nothing more to sample

    def _processes(self):
        """The target process plus its live descendants (best-effort)."""
        if self._proc is None:
            return []
        procs = [self._proc]
        try:
            procs.extend(self._proc.children(recursive=True))
        except psutil.Error:
            pass
        return procs

    def _poll_once(self) -> bool:
        """Sample the tree once; update peaks. Returns False if the tree is gone."""
        procs = self._processes()
        if not procs:
            return False

        total_mem = 0.0
        total_cpu = 0.0
        total_files = 0
        alive = False
        for proc in procs:
            try:
                total_mem += proc.memory_info().rss
                total_cpu += proc.cpu_percent(interval=None)
                try:
                    total_files += len(proc.open_files())
                except (psutil.AccessDenied, OSError):
                    pass
                alive = True
            except psutil.Error:
                continue  # this one vanished mid-poll; keep tallying the rest

        if not alive:
            return False

        self._samples += 1
        mem_mb = total_mem / (1024 * 1024)
        self._peak_memory_mb = mem_mb if self._peak_memory_mb is None else max(self._peak_memory_mb, mem_mb)
        self._peak_cpu_percent = total_cpu if self._peak_cpu_percent is None else max(self._peak_cpu_percent, total_cpu)
        self._peak_open_files = total_files if self._peak_open_files is None else max(self._peak_open_files, total_files)

        self._check_anomalies()
        return True

    def _check_anomalies(self) -> None:
        if (
            self._peak_memory_mb is not None
            and self._peak_memory_mb > self.memory_limit_mb
        ):
            note = (
                f"Memory usage exceeded {self.memory_limit_mb:.0f}MB "
                f"(peak {self._peak_memory_mb:.0f}MB) -> potential OOM risk"
            )
            if note not in self._anomalies:
                self._anomalies.append(note)


def terminate_process(
    process: Union["subprocess.Popen", int],
    *,
    sigterm_wait: float = 10.0,
) -> str:
    """Gracefully stop a process (tree): SIGTERM, wait, then SIGKILL.

    Accepts a :class:`subprocess.Popen` or a bare pid. Returns ``"sigterm"`` if
    everything exited on the polite signal, ``"sigkill"`` if a hard kill was
    needed, or ``"already_exited"`` if there was nothing to stop.

    When psutil is available the *whole* process tree is signalled (so children
    orphaned by main.py are not left running); otherwise it falls back to the
    Popen's own ``terminate()``/``kill()``.
    """
    popen = process if isinstance(process, subprocess.Popen) else None
    pid = process.pid if popen is not None else int(process)

    if popen is not None and popen.poll() is not None:
        return "already_exited"

    if psutil is not None:
        try:
            parent = psutil.Process(pid)
            procs = [parent] + parent.children(recursive=True)
        except psutil.NoSuchProcess:
            return "already_exited"

        for proc in procs:
            try:
                proc.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(procs, timeout=sigterm_wait)
        for proc in alive:
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
        if popen is not None:
            try:
                popen.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        return "sigkill" if alive else "sigterm"

    # psutil-less fallback: single process via Popen only.
    if popen is None:
        raise RuntimeError("terminate_process needs a Popen when psutil is unavailable")
    popen.terminate()
    try:
        popen.wait(timeout=sigterm_wait)
        return "sigterm"
    except subprocess.TimeoutExpired:
        popen.kill()
        popen.wait()
        return "sigkill"
