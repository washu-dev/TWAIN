"""Local execution adapter (Story 5.2).

Runs a RunBundle on the local machine so the MVP needs no HPC. Given a bundle
(the Story 5.1 :class:`~code_gen.codegen_engine.RunBundle`, or a path to an
already-written bundle directory) :meth:`LocalExecutionAdapter.execute`:

    1. Setup     -- create a temp working directory and copy the bundle in.
    2. Install   -- (optional) build a venv and ``pip install`` requirements.
    3. Smoke     -- (optional) run ``inline_tests.py`` first, so a missing
                    dependency or syntax error fails cheaply *before* the real
                    run (Story 5.1's "smoke tests pass before real execution").
    4. Execute   -- run ``python main.py`` as a subprocess with the working dir
                    as CWD, under a wall-clock timeout and a ResourceMonitor.
    5. Capture   -- stdout, stderr, exit code, duration, peak memory/CPU/files.
    6. Cleanup   -- remove the temp dir (kept when ``keep_artifacts=True``).

The result is an :class:`ExecutionResult`. A ``__main__`` CLI runs a bundle
directory directly and honours ``--keep-artifacts``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Union

# Sibling modules: prefer the package alias (tests / orchestrator), fall back to
# bare imports when run as a script from this directory.
try:  # pragma: no cover - import shim
    from execution_adapter.dependency_installer import DependencyInstaller
    from execution_adapter.execution_result import ExecutionResult, ExecutionStatus
    from execution_adapter.resource_monitor import ResourceMonitor, terminate_process
except ImportError:  # pragma: no cover
    from dependency_installer import DependencyInstaller
    from execution_result import ExecutionResult, ExecutionStatus
    from resource_monitor import ResourceMonitor, terminate_process

DEFAULT_EXEC_TIMEOUT = 7200.0   # 2 hr, matching the orchestrator's EXECUTE budget
DEFAULT_SMOKE_TIMEOUT = 300.0   # smoke should be quick
_DEP_ERROR_MARKERS = ("modulenotfounderror", "importerror", "no module named")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _activation_vars(run_python: str) -> dict:
    """Environment variables the interpreter's conda activation hooks would set.

    Running ``<prefix>/bin/python`` directly does not activate the environment, so
    its ``etc/conda/activate.d`` hooks never fire -- and several engines get
    essential runtime configuration from exactly there. NWChem reads its basis
    sets from ``NWCHEM_BASIS_LIBRARY`` and dies with "failed opening basis file"
    without it, which reads like a broken install rather than an unset variable;
    conda-forge OpenMPI locates its PMIx/PRRTE data the same way. Sourcing the
    hooks reproduces what activation would have done.

    Returns only the variables activation *changes*, so the caller keeps control
    of precedence. Empty when there are no hooks, or when they cannot be run --
    the run then proceeds exactly as before rather than failing here.
    """
    # Absolute, not resolved: the hooks build paths from CONDA_PREFIX and the
    # bundle runs with cwd set to its own workdir, so a relative prefix would
    # produce variables pointing at nothing. absolute() rather than resolve()
    # keeps a venv interpreter pointing at its own prefix, not the base env's.
    prefix = Path(run_python).absolute().parent.parent
    hooks = sorted((prefix / "etc" / "conda" / "activate.d").glob("*.sh"))
    if not hooks:
        return {}
    script = (
        'export CONDA_PREFIX="$1"; py="$2"; shift 2; '
        'for hook in "$@"; do . "$hook" >/dev/null 2>&1 || true; done; '
        '"$py" -c "import json,os,sys; sys.stdout.write(json.dumps(dict(os.environ)))"'
    )
    try:
        proc = subprocess.run(
            ["bash", "-c", script, "bash", str(prefix), str(run_python),
             *[str(h) for h in hooks]],
            capture_output=True, text=True, timeout=60, env={**os.environ},
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        activated = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    return {k: v for k, v in activated.items() if os.environ.get(k) != v}


def _space_free_root(configured: Path) -> Path:
    """``configured``, or a whitespace-free root when it contains a space.

    Most scientific engines are Fortran and truncate paths at whitespace. NWChem
    exits 143 with "Both permanent and scratch directory not accessible" when its
    scratch directory sits under a path like ``.../TWAIN 2026/...``, and CP2K,
    Quantum ESPRESSO and ABINIT are the same family -- the run dies in the engine
    with an error that looks nothing like its cause. Where the checkout lives is
    not ours to choose, so a workspace with a space in it is relocated rather
    than left to fail. The result records the directory actually used, so
    artifact capture and the report follow it.

    Returns ``configured`` unchanged when it is already safe, or when the
    fallback would be no better (nothing to gain by moving).
    """
    if " " not in str(configured):
        return configured
    fallback = Path(tempfile.gettempdir()) / "twain-exec"
    if " " in str(fallback):
        return configured
    print(f"[execute] the configured workspace {configured} contains a space, "
          f"which Fortran engines (NWChem, CP2K, Quantum ESPRESSO, ABINIT) "
          f"cannot use; running in {fallback} instead")
    return fallback


def _safe_name(run_id) -> str:
    """A filesystem-safe token from a run/session id (for naming the workdir).

    >>> _safe_name("995128d088574758b3a5ef634907f744")
    '995128d088574758b3a5ef634907f744'
    >>> _safe_name("a/b c:d")
    'a_b_c_d'
    """
    return _UNSAFE_NAME.sub("_", str(run_id))[:96] or "run"


class LocalExecutionAdapter:
    """Executes RunBundles on the local machine and reports an ExecutionResult."""

    def __init__(
        self,
        *,
        workspace_root: Optional[Union[str, Path]] = None,
        poll_interval: float = 5.0,
        memory_limit_mb: float = 1024.0,
        exec_timeout: float = DEFAULT_EXEC_TIMEOUT,
        sigterm_wait: float = 10.0,
        installer: Optional[DependencyInstaller] = None,
    ):
        self.workspace_root = str(workspace_root) if workspace_root else None
        self.poll_interval = poll_interval
        self.memory_limit_mb = memory_limit_mb
        self.exec_timeout = exec_timeout
        self.sigterm_wait = sigterm_wait
        self.installer = installer or DependencyInstaller()

    # ---------------------------------------------------------------- execute
    def execute(
        self,
        bundle,
        *,
        keep_artifacts: bool = False,
        install_deps: bool = False,
        run_smoke: bool = True,
        python_executable: Optional[str] = None,
        timeout: Optional[float] = None,
        env: Optional[dict] = None,
        run_id: Optional[str] = None,
    ) -> ExecutionResult:
        """Run ``bundle`` locally and return an :class:`ExecutionResult`.

        ``bundle`` is a RunBundle (anything with ``write(dir)``) or a path to a
        bundle directory. ``install_deps`` builds a venv and installs
        requirements; otherwise ``python_executable`` (default: this
        interpreter) is used. ``timeout`` overrides the adapter's exec budget.
        ``run_id`` (e.g. the session id) names the working directory
        deterministically (``exec_<run_id>``) so outputs trace back to the run;
        omit it for a unique random temp dir.
        """
        timeout = timeout if timeout is not None else self.exec_timeout
        tool_name = getattr(bundle, "tool_name", None)
        entrypoint = getattr(bundle, "entrypoint", "main.py")

        # 1) setup -----------------------------------------------------------
        try:
            workdir = self._setup_workdir(bundle, run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(
                status=ExecutionStatus.SETUP_FAILED,
                message=f"failed to prepare working directory: {exc}",
                tool_name=tool_name,
            )

        try:
            main_path = workdir / entrypoint
            if not main_path.is_file():
                return ExecutionResult(
                    status=ExecutionStatus.SETUP_FAILED,
                    message=f"bundle has no entrypoint {entrypoint!r}",
                    tool_name=tool_name,
                    artifacts_dir=str(workdir) if keep_artifacts else None,
                )

            run_python = python_executable or sys.executable
            install_log = None

            # 2) install dependencies (optional) -----------------------------
            requirements = workdir / "requirements.txt"
            if install_deps and requirements.is_file():
                install = self.installer.install(requirements, workdir, use_venv=True)
                install_log = install.to_dict()
                if not install.success:
                    status = (
                        ExecutionStatus.SETUP_FAILED if install.timed_out
                        else ExecutionStatus.DEPENDENCY_ERROR
                    )
                    return ExecutionResult(
                        status=status,
                        message=install.message,
                        tool_name=tool_name,
                        install_log=install_log,
                        artifacts_dir=str(workdir) if keep_artifacts else None,
                    )
                run_python = install.python_executable or run_python

            # os.environ < the env's activation hooks < the caller's overrides.
            run_env = {**os.environ, **_activation_vars(run_python), **(env or {})}
            # A generated bundle may shell out to an env-local binary: an ASE
            # calculator like DFTB+/NWChem/Quantum ESPRESSO runs `dftb+`/`nwchem`/…
            # as a subprocess. Running a pixi-env interpreter by its bare path does
            # NOT activate the env, so its bin/ dir isn't on PATH and the binary is
            # "command not found" (exit 127). Put the interpreter's own directory
            # first on PATH so sibling binaries resolve -- mirroring env activation.
            # Use the interpreter path AS GIVEN (don't resolve the symlink: a venv
            # python links to the base interpreter, but its console scripts live in
            # the venv's own bin/).
            _bindir = str(Path(run_python).parent)
            run_env["PATH"] = _bindir + os.pathsep + run_env.get("PATH", "")

            # Use every available core: OpenMP-based calculators (e.g. GPAW)
            # run single-threaded unless OMP_NUM_THREADS is set. Only defaults —
            # anything already in the environment or caller ``env`` wins.
            _cpus = str(os.cpu_count() or 1)
            for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                         "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
                run_env.setdefault(_var, _cpus)

            # DFTB+ locates its Slater-Koster (.skf) parameter files via
            # DFTB_PREFIX. pixi's [activation.env] sets it, but if the runner was
            # launched outside pixi it may be unset -- fall back to the repo's
            # fetched slako/ dir so a DFTB+ bundle resolves its parameters instead
            # of aborting "SK file ... not found" on the first energy evaluation.
            if not run_env.get("DFTB_PREFIX"):
                try:
                    from twain_paths import SLAKO_DIR
                    if SLAKO_DIR.is_dir():
                        run_env["DFTB_PREFIX"] = str(SLAKO_DIR) + os.sep
                except Exception:  # noqa: BLE001 - best-effort default only
                    pass

            # 3) smoke tests before the real run (optional) ------------------
            smoke_log = None
            smoke_path = workdir / "inline_tests.py"
            if run_smoke and smoke_path.is_file():
                smoke = self._run_subprocess(
                    [run_python, "inline_tests.py"], workdir, run_env,
                    timeout=min(timeout, DEFAULT_SMOKE_TIMEOUT), monitor=False,
                )
                smoke_log = {
                    "exit_code": smoke["exit_code"],
                    "stdout": smoke["stdout"],
                    "stderr": smoke["stderr"],
                }
                if smoke["exit_code"] != 0:
                    # exit 2 (from the generated smoke script) == missing dependency.
                    status = (
                        ExecutionStatus.DEPENDENCY_ERROR if smoke["exit_code"] == 2
                        else ExecutionStatus.SMOKE_FAILED
                    )
                    return ExecutionResult(
                        status=status,
                        exit_code=smoke["exit_code"],
                        stdout=smoke["stdout"],
                        stderr=smoke["stderr"],
                        duration_seconds=smoke["duration"],
                        message="smoke tests failed before execution",
                        tool_name=tool_name,
                        install_log=install_log,
                        smoke_log=smoke_log,
                        artifacts_dir=str(workdir) if keep_artifacts else None,
                    )

            # 4-5) execute + capture ----------------------------------------
            run = self._run_subprocess(
                [run_python, entrypoint], workdir, run_env, timeout=timeout, monitor=True,
            )

            status, message = self._classify(run)
            return ExecutionResult(
                status=status,
                exit_code=run["exit_code"],
                stdout=run["stdout"],
                stderr=run["stderr"],
                duration_seconds=run["duration"],
                peak_memory_mb=run["usage"].peak_memory_mb,
                peak_cpu_percent=run["usage"].peak_cpu_percent,
                peak_open_files=run["usage"].peak_open_files,
                anomalies=run["usage"].anomalies,
                artifacts_dir=str(workdir) if keep_artifacts else None,
                tool_name=tool_name,
                command=[run_python, entrypoint],
                message=message,
                install_log=install_log,
                smoke_log=smoke_log,
            )
        finally:
            # 6) cleanup -----------------------------------------------------
            if not keep_artifacts:
                shutil.rmtree(workdir, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def _setup_workdir(self, bundle, run_id=None) -> Path:
        """Create the working directory and materialize the bundle into it.

        With ``run_id`` the directory is named ``exec_<run_id>`` under
        ``workspace_root`` so its outputs trace back to the session; a prior
        directory for the same id is replaced (a re-run supersedes it). Without
        a ``run_id`` a unique random temp dir is used.
        """
        workdir = self._make_workdir(run_id)
        try:
            if hasattr(bundle, "write") and callable(bundle.write):
                bundle.write(workdir)
            else:
                src = Path(bundle)
                if not src.is_dir():
                    raise FileNotFoundError(f"bundle path is not a directory: {src}")
                shutil.copytree(src, workdir, dirs_exist_ok=True)
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)  # don't leak the workdir
            raise
        return workdir

    def _make_workdir(self, run_id) -> Path:
        """A deterministic ``exec_<run_id>`` dir when an id is given, else random."""
        configured = (Path(self.workspace_root) if self.workspace_root
                      else Path(tempfile.gettempdir()))
        root = _space_free_root(configured)
        if run_id:
            root.mkdir(parents=True, exist_ok=True)
            workdir = root / f"exec_{_safe_name(run_id)}"
            if workdir.exists():
                shutil.rmtree(workdir, ignore_errors=True)  # re-run replaces prior
            workdir.mkdir(parents=True)
            return workdir
        root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="twain_exec_", dir=str(root)))

    def _run_subprocess(self, cmd: List[str], cwd: Path, env: dict, *, timeout: float, monitor: bool) -> dict:
        """Run ``cmd`` under an optional ResourceMonitor + timeout w/ graceful kill."""
        start = time.monotonic()
        popen = subprocess.Popen(
            cmd, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,  # own process group -> we can signal the tree
        )
        mon = None
        if monitor:
            mon = ResourceMonitor(
                popen.pid, interval=self.poll_interval, memory_limit_mb=self.memory_limit_mb
            ).start()

        timed_out = False
        try:
            stdout, stderr = popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process(popen, sigterm_wait=self.sigterm_wait)
            stdout, stderr = popen.communicate()  # drain whatever was buffered

        usage = mon.stop() if mon is not None else None
        return {
            "exit_code": popen.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "duration": time.monotonic() - start,
            "timed_out": timed_out,
            "usage": usage,
        }

    @staticmethod
    def _classify(run: dict):
        if run["timed_out"]:
            return ExecutionStatus.TIMEOUT, "execution exceeded the timeout and was terminated"
        if run["exit_code"] == 0:
            return ExecutionStatus.SUCCESS, "execution completed successfully"
        if any(marker in (run["stderr"] or "").lower() for marker in _DEP_ERROR_MARKERS):
            return (
                ExecutionStatus.DEPENDENCY_ERROR,
                "execution failed on a missing/broken dependency (see stderr)",
            )
        return ExecutionStatus.FAILED, f"execution exited with code {run['exit_code']}"


def _main(argv=None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run a RunBundle directory locally (Story 5.2).")
    parser.add_argument("bundle_dir", help="path to a bundle directory (contains main.py)")
    parser.add_argument("--keep-artifacts", action="store_true", help="keep the temp working dir")
    parser.add_argument("--install", action="store_true", help="build a venv and install requirements.txt")
    parser.add_argument("--no-smoke", action="store_true", help="skip inline_tests.py before running")
    parser.add_argument("--timeout", type=float, default=DEFAULT_EXEC_TIMEOUT, help="execution timeout (s)")
    parser.add_argument("--python", default=None, help="python executable to run main.py with")
    parser.add_argument("--run-id", default=None,
                        help="name the working dir exec_<run-id> instead of a random temp dir")
    args = parser.parse_args(argv)

    adapter = LocalExecutionAdapter()
    result = adapter.execute(
        args.bundle_dir,
        keep_artifacts=args.keep_artifacts,
        install_deps=args.install,
        run_smoke=not args.no_smoke,
        python_executable=args.python,
        timeout=args.timeout,
        run_id=args.run_id,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(_main())
