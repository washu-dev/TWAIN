"""Docker execution adapter -- runs a RunBundle inside the linux-64 runner image.

Companion to :class:`~execution_adapter.local_adapter.LocalExecutionAdapter`.
The local adapter runs a bundle in a local pixi env; this one runs it inside the
``twain-runner`` container (``runner/Dockerfile`` -- a full linux-64 image with
both pixi envs, GPAW, and the DFTB+ Slater-Koster params baked in). That is how a
Mac (osx-arm64) executes a Linux-only engine like **GPAW**: discovery selects it
(see :func:`method_discovery.calculator_registry.planning_platform`) and the
state machine routes the run here when the calculator ``needs_docker`` the host.

The mechanism mirrors ``docker run`` from ``runner/README.md`` "Run in Docker":

    docker run --rm --platform linux/amd64 --name twain-exec-<id> \
      -v <host-workdir>:/work -w /app -e DFTB_PREFIX=/app/slako/ \
      twain-runner  pixi run -e sim python /work/main.py

The bundle is materialized into a **host** working directory that is bind-mounted
at ``/work``, so outputs (``results.csv``) the container writes appear back on the
host with no copy-out step. We invoke ``pixi run -e sim`` (not the bare
interpreter) so conda activation sets ``DFTB_PREFIX`` and GPAW's dataset path.

The command runner is injectable (``command_runner``) so the argv construction
and result handling are unit-testable without a Docker daemon.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional, Union

try:  # pragma: no cover - import shim (mirrors local_adapter)
    from execution_adapter.execution_result import ExecutionResult, ExecutionStatus
    from execution_adapter.local_adapter import (
        DEFAULT_EXEC_TIMEOUT,
        DEFAULT_SMOKE_TIMEOUT,
        _DEP_ERROR_MARKERS,
        _safe_name,
    )
except ImportError:  # pragma: no cover
    from execution_result import ExecutionResult, ExecutionStatus
    from local_adapter import (
        DEFAULT_EXEC_TIMEOUT,
        DEFAULT_SMOKE_TIMEOUT,
        _DEP_ERROR_MARKERS,
        _safe_name,
    )

DEFAULT_IMAGE = os.environ.get("TWAIN_DOCKER_IMAGE", "twain-runner")
# The runner image is linux-64 only; on Apple Silicon `docker run` needs an
# explicit platform (Rosetta/qemu runs the amd64 image). Overridable for hosts
# that build a native arm64 image.
DEFAULT_DOCKER_PLATFORM = os.environ.get("TWAIN_DOCKER_PLATFORM", "linux/amd64")
DEFAULT_SIM_ENV = "sim"
_CONTAINER_MOUNT = "/work"      # where the bundle is bind-mounted in the container
_CONTAINER_WORKROOT = "/app"    # image WORKDIR (holds pixi.toml so `pixi run` resolves)


class DockerExecutionAdapter:
    """Executes RunBundles inside the twain-runner container; reports ExecutionResult.

    Interface-compatible with :class:`LocalExecutionAdapter.execute` so the state
    machine can pick a backend without special-casing the call site. Docker-only
    knobs (image, platform, sim env, docker executable) are constructor args.
    """

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        docker_platform: str = DEFAULT_DOCKER_PLATFORM,
        sim_env: str = DEFAULT_SIM_ENV,
        docker_exe: Optional[str] = None,
        workspace_root: Optional[Union[str, Path]] = None,
        exec_timeout: float = DEFAULT_EXEC_TIMEOUT,
        sigterm_wait: float = 10.0,
        command_runner: Optional[Callable[..., dict]] = None,
    ):
        self.image = image
        self.docker_platform = docker_platform
        self.sim_env = sim_env
        self.docker_exe = docker_exe or os.environ.get("TWAIN_DOCKER", "docker")
        self.workspace_root = str(workspace_root) if workspace_root else None
        self.exec_timeout = exec_timeout
        self.sigterm_wait = sigterm_wait
        # Seam: (argv, *, timeout, container_name) -> dict(exit_code, stdout,
        # stderr, duration, timed_out). Default shells out; tests inject a fake.
        self._run = command_runner or self._default_run

    # ---------------------------------------------------------------- execute
    def execute(
        self,
        bundle,
        *,
        keep_artifacts: bool = False,
        install_deps: bool = False,   # ignored: the image already has the stack
        run_smoke: bool = True,
        python_executable: Optional[str] = None,  # ignored: env fixed by the image
        timeout: Optional[float] = None,
        env: Optional[dict] = None,
        run_id: Optional[str] = None,
    ) -> ExecutionResult:
        """Run ``bundle`` in the container and return an :class:`ExecutionResult`.

        ``bundle`` is a RunBundle (has ``write(dir)``) or a path to a bundle dir.
        The dir is bind-mounted, so the container's ``results.csv`` lands under
        ``artifacts_dir`` on the host. ``install_deps``/``python_executable`` are
        accepted for interface parity but ignored -- the image is self-contained.
        """
        timeout = timeout if timeout is not None else self.exec_timeout
        tool_name = getattr(bundle, "tool_name", None)
        entrypoint = getattr(bundle, "entrypoint", "main.py")

        # 1) setup: materialize the bundle into a host workdir we can bind-mount.
        try:
            workdir = self._setup_workdir(bundle, run_id=run_id)
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(
                status=ExecutionStatus.SETUP_FAILED,
                message=f"failed to prepare working directory: {exc}",
                tool_name=tool_name,
            )

        try:
            if not (workdir / entrypoint).is_file():
                return ExecutionResult(
                    status=ExecutionStatus.SETUP_FAILED,
                    message=f"bundle has no entrypoint {entrypoint!r}",
                    tool_name=tool_name,
                    artifacts_dir=str(workdir) if keep_artifacts else None,
                )

            extra_env = dict(env or {})
            # `pixi run -e sim` applies the sim env's activation (DFTB_PREFIX,
            # GPAW dataset path). Set DFTB_PREFIX explicitly too so a bundle run
            # resolves .skf files even if activation is bypassed. Path is the
            # image's baked-in slako dir (runner/Dockerfile fetch-slako layer).
            extra_env.setdefault("DFTB_PREFIX", "/app/slako/")

            # 2) smoke tests before the real run (optional) ------------------
            smoke_log = None
            if run_smoke and (workdir / "inline_tests.py").is_file():
                smoke = self._run(
                    self._build_argv(workdir, "inline_tests.py", extra_env,
                                     name=self._container_name(run_id, "smoke")),
                    timeout=min(timeout, DEFAULT_SMOKE_TIMEOUT),
                    container_name=self._container_name(run_id, "smoke"),
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
                        message="smoke tests failed before execution (in Docker)",
                        tool_name=tool_name,
                        smoke_log=smoke_log,
                        artifacts_dir=str(workdir) if keep_artifacts else None,
                    )

            # 3-4) execute + capture ----------------------------------------
            name = self._container_name(run_id, "run")
            argv = self._build_argv(workdir, entrypoint, extra_env, name=name)
            run = self._run(argv, timeout=timeout, container_name=name)

            status, message = self._classify(run)
            return ExecutionResult(
                status=status,
                exit_code=run["exit_code"],
                stdout=run["stdout"],
                stderr=run["stderr"],
                duration_seconds=run["duration"],
                artifacts_dir=str(workdir) if keep_artifacts else None,
                tool_name=tool_name,
                command=argv,
                message=message,
                smoke_log=smoke_log,
            )
        finally:
            # 5) cleanup -----------------------------------------------------
            if not keep_artifacts:
                shutil.rmtree(workdir, ignore_errors=True)

    # ---------------------------------------------------------------- helpers
    def _build_argv(self, workdir: Path, entrypoint: str, extra_env: dict, *, name: str) -> List[str]:
        """The full ``docker run`` argv for one bundle entrypoint (pure/testable).

        >>> a = DockerExecutionAdapter(image="img", docker_platform="linux/amd64",
        ...                            command_runner=lambda *a, **k: {})
        >>> argv = a._build_argv(Path("/tmp/exec_x"), "main.py", {"DFTB_PREFIX": "/app/slako/"}, name="c")
        >>> argv[:7]
        ['docker', 'run', '--rm', '--name', 'c', '--platform', 'linux/amd64']
        >>> argv[-7:]
        ['img', 'pixi', 'run', '-e', 'sim', 'python', '/work/main.py']
        >>> '-v' in argv and '/tmp/exec_x:/work' in argv
        True
        """
        argv = [
            self.docker_exe, "run", "--rm",
            "--name", name,
            "--platform", self.docker_platform,
            "-v", f"{workdir}:{_CONTAINER_MOUNT}",
            "-w", _CONTAINER_WORKROOT,
        ]
        for key, val in extra_env.items():
            argv += ["-e", f"{key}={val}"]
        argv += [
            self.image,
            "pixi", "run", "-e", self.sim_env, "python",
            f"{_CONTAINER_MOUNT}/{entrypoint}",
        ]
        return argv

    @staticmethod
    def _container_name(run_id, phase: str) -> str:
        """Deterministic, filesystem/docker-safe container name for kill-on-timeout."""
        return f"twain-exec-{_safe_name(run_id or 'run')}-{phase}"[:120]

    def _setup_workdir(self, bundle, run_id=None) -> Path:
        """Create the host working dir and materialize the bundle into it.

        Named ``exec_<run_id>`` under ``workspace_root`` (traceable, replaces a
        prior run's dir) when an id is given, else a random temp dir. The dir is
        what gets bind-mounted, so the container's outputs land here.
        """
        import tempfile
        if run_id:
            root = Path(self.workspace_root) if self.workspace_root else Path(tempfile.gettempdir())
            root.mkdir(parents=True, exist_ok=True)
            workdir = root / f"exec_{_safe_name(run_id)}"
            if workdir.exists():
                shutil.rmtree(workdir, ignore_errors=True)
            workdir.mkdir(parents=True)
        else:
            workdir = Path(tempfile.mkdtemp(prefix="twain_docker_exec_", dir=self.workspace_root))
        try:
            if hasattr(bundle, "write") and callable(bundle.write):
                bundle.write(workdir)
            else:
                src = Path(bundle)
                if not src.is_dir():
                    raise FileNotFoundError(f"bundle path is not a directory: {src}")
                shutil.copytree(src, workdir, dirs_exist_ok=True)
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        return workdir

    def _default_run(self, argv: List[str], *, timeout: float, container_name: str) -> dict:
        """Run ``docker run`` under a wall-clock timeout; ``docker kill`` on expiry."""
        start = time.monotonic()
        popen = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Stop the container by name (killing the client alone can orphan it),
            # then drain the client's buffered output.
            try:
                subprocess.run([self.docker_exe, "kill", container_name],
                               capture_output=True, timeout=self.sigterm_wait)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                popen.kill()
            except OSError:
                pass
            stdout, stderr = popen.communicate()
        return {
            "exit_code": popen.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "duration": time.monotonic() - start,
            "timed_out": timed_out,
        }

    @staticmethod
    def _classify(run: dict):
        if run.get("timed_out"):
            return ExecutionStatus.TIMEOUT, "execution exceeded the timeout and the container was killed"
        if run["exit_code"] == 0:
            return ExecutionStatus.SUCCESS, "execution completed successfully (in Docker)"
        blob = ((run.get("stderr") or "") + (run.get("stdout") or "")).lower()
        if any(marker in blob for marker in _DEP_ERROR_MARKERS):
            return (
                ExecutionStatus.DEPENDENCY_ERROR,
                "execution failed on a missing/broken dependency in the container (see stderr)",
            )
        return ExecutionStatus.FAILED, f"execution exited with code {run['exit_code']} (in Docker)"
