"""Tests for running non-native engines (e.g. GPAW on a Mac) via the Docker runner.

Two layers:

  * :class:`DockerExecutionAdapter` -- argv construction and result handling,
    exercised with an injected fake command runner so no Docker daemon is needed.
  * State-machine routing -- ``execute()`` sends a calculator with no build for
    the host (``needs_docker``) into the linux-64 container when Docker + image
    are present, and degrades gracefully (guided skip) when they are not.

Run from the repo root with:  pixi run pytest tests/unit/test_docker_execution.py
"""
import doctest
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO_ROOT / "modules" / "16_agent_mesh_control_plane"
sys.path.insert(0, str(MODULE_DIR))

import statemachine as SM  # noqa: E402
from states import State  # noqa: E402
from crash_recovery import DataStorage  # noqa: E402
from execution_adapter import docker_adapter as DA  # noqa: E402
from execution_adapter.docker_adapter import DockerExecutionAdapter  # noqa: E402
from execution_adapter.execution_result import ExecutionResult, ExecutionStatus  # noqa: E402
from method_discovery import calculator_registry as CR  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Module doctests (argv builder; planning_platform / needs_docker in registry)
# ─────────────────────────────────────────────────────────────────────────────
def test_docker_adapter_doctests():
    assert doctest.testmod(DA, verbose=False).failed == 0


def test_registry_docker_doctests():
    assert doctest.testmod(CR, verbose=False).failed == 0


# ─────────────────────────────────────────────────────────────────────────────
# DockerExecutionAdapter -- with a fake command runner (no daemon required)
# ─────────────────────────────────────────────────────────────────────────────
def _bundle(tmp_path, *, main="print('ok')", inline_tests=None, name="bundle"):
    d = tmp_path / name
    d.mkdir()
    (d / "main.py").write_text(main, encoding="utf-8")
    if inline_tests is not None:
        (d / "inline_tests.py").write_text(inline_tests, encoding="utf-8")
    return d


def _host_mount(argv):
    """Extract the host side of the ``-v host:/work`` bind mount from a docker argv."""
    i = argv.index("-v")
    return argv[i + 1].rsplit(":", 1)[0]


def _fake_runner(*, exit_by_entrypoint, writes_output=True):
    """A command_runner that classifies by entrypoint and (optionally) writes output.

    ``exit_by_entrypoint`` maps a filename suffix ('main.py'/'inline_tests.py') to
    a dict of {exit_code, stderr, timed_out}. On a zero-exit main run it writes
    results.csv into the bind-mounted host dir, mimicking the container's output.
    """
    def run(argv, *, timeout, container_name):
        entry = argv[-1]
        spec = {}
        for suffix, s in exit_by_entrypoint.items():
            if entry.endswith(suffix):
                spec = s
                break
        rc = spec.get("exit_code", 0)
        if entry.endswith("main.py") and rc == 0 and writes_output:
            Path(_host_mount(argv), "results.csv").write_text("bulk_modulus_voigt\n187.0\n")
        return {
            "exit_code": rc,
            "stdout": spec.get("stdout", ""),
            "stderr": spec.get("stderr", ""),
            "duration": 0.01,
            "timed_out": spec.get("timed_out", False),
        }
    return run


class TestDockerAdapter:
    def test_success_builds_correct_argv_and_reads_output(self, tmp_path):
        b = _bundle(tmp_path)
        adapter = DockerExecutionAdapter(
            image="twain-runner", workspace_root=str(tmp_path / "ws"),
            command_runner=_fake_runner(exit_by_entrypoint={"main.py": {"exit_code": 0}}),
        )
        result = adapter.execute(b, run_id="r1", keep_artifacts=True)
        assert result.status == ExecutionStatus.SUCCESS
        # The command records the docker invocation for provenance.
        assert result.command[:3] == ["docker", "run", "--rm"]
        assert "--platform" in result.command and "linux/amd64" in result.command
        assert result.command[-6:] == ["pixi", "run", "-e", "sim", "python", "/work/main.py"]
        # The container's output landed in the (bind-mounted) artifacts dir.
        assert (Path(result.artifacts_dir) / "results.csv").is_file()

    def test_runs_smoke_before_main_and_maps_dep_error(self, tmp_path):
        b = _bundle(tmp_path, inline_tests="print('smoke')")
        # Smoke exits 2 == missing dependency; main must never run.
        adapter = DockerExecutionAdapter(
            command_runner=_fake_runner(exit_by_entrypoint={"inline_tests.py": {"exit_code": 2}}),
        )
        result = adapter.execute(b, run_id="r2", keep_artifacts=True)
        assert result.status == ExecutionStatus.DEPENDENCY_ERROR
        assert result.smoke_log is not None
        assert not (Path(result.artifacts_dir) / "results.csv").is_file()  # main skipped

    def test_nonzero_exit_is_failed(self, tmp_path):
        b = _bundle(tmp_path)
        adapter = DockerExecutionAdapter(
            command_runner=_fake_runner(exit_by_entrypoint={"main.py": {"exit_code": 1, "stderr": "boom"}}),
        )
        result = adapter.execute(b, run_id="r3")
        assert result.status == ExecutionStatus.FAILED

    def test_dependency_marker_in_stderr_is_dependency_error(self, tmp_path):
        b = _bundle(tmp_path)
        adapter = DockerExecutionAdapter(
            command_runner=_fake_runner(exit_by_entrypoint={
                "main.py": {"exit_code": 1, "stderr": "ModuleNotFoundError: No module named 'gpaw'"}}),
        )
        result = adapter.execute(b, run_id="r4")
        assert result.status == ExecutionStatus.DEPENDENCY_ERROR

    def test_timeout_is_reported(self, tmp_path):
        b = _bundle(tmp_path)
        adapter = DockerExecutionAdapter(
            command_runner=_fake_runner(exit_by_entrypoint={"main.py": {"exit_code": None, "timed_out": True}}),
        )
        result = adapter.execute(b, run_id="r5")
        assert result.status == ExecutionStatus.TIMEOUT

    def test_missing_entrypoint_is_setup_failed(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        adapter = DockerExecutionAdapter(command_runner=_fake_runner(exit_by_entrypoint={}))
        result = adapter.execute(d, run_id="r6", keep_artifacts=True)
        assert result.status == ExecutionStatus.SETUP_FAILED


# ─────────────────────────────────────────────────────────────────────────────
# State-machine routing -- native vs Docker vs guided skip
# ─────────────────────────────────────────────────────────────────────────────
def _machine(tmp_path, **kw):
    kw.setdefault("sim_available", lambda names: set())
    with patch.object(DataStorage, "load", return_value=None):
        m = SM.StateMachine(data_path=str(tmp_path / "state.json"), run_id="dk", **kw)
    m.artifacts_dir = tmp_path
    return m


def _seed_gpaw_run(m, tmp_path):
    """Seed a ready-to-execute GPAW plan + bundle (skips plan()/build())."""
    plan = {
        "selected_method": {
            "libraries": ["ASE"], "calculator": "GPAW",
            "calculator_import": "gpaw", "calculator_library": "ASE",
        },
        "requested_property": "bulk_modulus_voigt", "safety_notes": [],
    }
    m.context.artifacts["execution_plan"] = m._write_artifact("execution_plan", plan)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "main.py").write_text("print('gpaw run')\n", encoding="utf-8")
    m.context.artifacts["run_bundle"] = str(bundle)
    m.execute_locally = True
    m.auto_approve = True  # skip the heavy-calc gate
    return m


class _RecordingDocker:
    """Stand-in for DockerExecutionAdapter that records it was used."""
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        _RecordingDocker.instances.append(self)

    def execute(self, bundle, **kwargs):
        self.calls.append((bundle, kwargs))
        return ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0, stdout="{}")


def test_gpaw_on_mac_routes_to_docker_when_available(tmp_path):
    _RecordingDocker.instances = []
    m = _seed_gpaw_run(_machine(tmp_path), tmp_path)
    with patch.object(SM, "current_platform", return_value="osx-arm64"), \
         patch.object(SM, "docker_available", return_value=True), \
         patch.object(SM, "docker_image_available", return_value=True), \
         patch.object(DA, "DockerExecutionAdapter", _RecordingDocker):
        assert m.execute() == State.INTERPRET
    # Routed into the container (not the local sim env), and recorded success.
    assert len(_RecordingDocker.instances) == 1
    assert len(_RecordingDocker.instances[0].calls) == 1
    assert m.context.execution_status is True


def test_gpaw_on_mac_skips_gracefully_when_no_docker_daemon(tmp_path):
    m = _seed_gpaw_run(_machine(tmp_path), tmp_path)
    with patch.object(SM, "current_platform", return_value="osx-arm64"), \
         patch.object(SM, "docker_available", return_value=False):
        assert m.execute() == State.INTERPRET
    result = json.loads(Path(m.context.artifacts["execution_result"]).read_text())
    assert result["status"] == "skipped_missing_dependency"
    assert "Docker" in result["how_to_run"]           # actionable guidance
    assert m.context.execution_status is True           # winds down cleanly


def test_gpaw_on_mac_skips_with_build_hint_when_image_missing(tmp_path):
    m = _seed_gpaw_run(_machine(tmp_path), tmp_path)
    with patch.object(SM, "current_platform", return_value="osx-arm64"), \
         patch.object(SM, "docker_available", return_value=True), \
         patch.object(SM, "docker_image_available", return_value=False):
        assert m.execute() == State.INTERPRET
    result = json.loads(Path(m.context.artifacts["execution_result"]).read_text())
    assert result["status"] == "skipped_missing_dependency"
    assert "docker build" in result["how_to_run"]       # tells the user to build the image


def test_native_calculator_does_not_route_to_docker(tmp_path):
    # A calculator that builds on the host (DFTB+ on osx-arm64) must NOT be sent to
    # Docker even when Docker is available -- native is faster.
    _RecordingDocker.instances = []
    m = _machine(tmp_path)
    plan = {
        "selected_method": {"libraries": ["ASE"], "calculator": "DFTB+",
                            "calculator_import": "ase.calculators.dftb", "calculator_library": "ASE"},
        "requested_property": "total_energy", "safety_notes": [],
    }
    m.context.artifacts["execution_plan"] = m._write_artifact("execution_plan", plan)
    bundle = tmp_path / "b"
    bundle.mkdir()
    (bundle / "main.py").write_text("print('x')\n", encoding="utf-8")
    m.context.artifacts["run_bundle"] = str(bundle)
    m.execute_locally = True
    m.auto_approve = True
    with patch.object(SM, "current_platform", return_value="osx-arm64"), \
         patch.object(SM, "docker_available", return_value=True), \
         patch.object(SM, "docker_image_available", return_value=True), \
         patch.object(DA, "DockerExecutionAdapter", _RecordingDocker), \
         patch.object(SM, "pixi_env_python", return_value=None):
        # sim env not built -> native path takes the graceful skip; crucially it did
        # NOT construct a Docker adapter.
        m.execute()
    assert _RecordingDocker.instances == []
