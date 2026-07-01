"""Unit tests for the local execution adapter (Story 5.2).

Covers the acceptance tests and definition of done:

  * Execute a simple Python script, capture output   (TestLocalAdapterExecute)
  * Handle timeout gracefully                         (TestLocalAdapterExecute)
  * Handle a missing dependency (clear error)         (TestLocalAdapterExecute /
                                                       TestDependencyInstaller)
  * Resource monitoring accuracy                      (TestResourceMonitor)
  * RunBundles execute successfully on the local machine; logs + metrics
    captured; timeouts respected                      (TestEndToEnd)

plus the supporting units: ExecutionResult serialization, graceful shutdown,
and the dependency installer's retry/backoff/timeout logic (exercised offline
via injected runner + venv builder).

Run from the repo root with:  pixi run pytest tests/unit/test_execution_adapter.py
"""
import doctest
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from execution_adapter import dependency_installer as _di_module
from execution_adapter import local_adapter as _la_module
from execution_adapter.dependency_installer import (
    DependencyInstaller,
    InstallResult,
    is_network_error,
)
from execution_adapter.execution_result import ExecutionResult, ExecutionStatus
from execution_adapter.local_adapter import LocalExecutionAdapter
from execution_adapter.resource_monitor import (
    ResourceMonitor,
    terminate_process,
)


def make_bundle_dir(tmp_path, main_src, *, inline_tests_src=None, requirements=None, name="bundle"):
    """Write a minimal bundle directory and return its path."""
    d = tmp_path / name
    d.mkdir()
    (d / "main.py").write_text(main_src, encoding="utf-8")
    if inline_tests_src is not None:
        (d / "inline_tests.py").write_text(inline_tests_src, encoding="utf-8")
    if requirements is not None:
        (d / "requirements.txt").write_text(requirements, encoding="utf-8")
    return d


def make_plan(tool_name):
    return {
        "selected_method": {"tool_name": tool_name, "tool_version": 1.0},
        "compute_estimate": {"cpu_hours": 1.0},
        "slurm_request": {"cpu_count": 8, "gpu_count": 1, "max_time": 24.0, "ram": 16},
        "cost_estimate": {"min_tokens": 100, "min_cost": 1.0},
        "metadata": {"timestamp": "2026-06-15T12:00:00Z", "goal_id": "g1", "candidate_rank": 1},
        "acceptance_metrics": [{"metric_name": "density", "target_value": 7.8, "tolerance": 0.5}],
        "safety_notes": ["Verify SLURM partition limits"],
    }


# ═══════════════════════════════════════════════════════════════════════════
# ExecutionResult
# ═══════════════════════════════════════════════════════════════════════════
class TestModuleDoctests:
    """Run the modules' embedded doctests under a plain ``pytest tests/``."""

    def test_dependency_installer_doctests(self):
        result = doctest.testmod(_di_module, verbose=False)
        assert result.failed == 0

    def test_local_adapter_doctests(self):
        result = doctest.testmod(_la_module, verbose=False)
        assert result.failed == 0


class TestExecutionResult:
    def test_to_dict_is_json_serializable(self):
        r = ExecutionResult(
            status=ExecutionStatus.SUCCESS, exit_code=0, stdout="hi",
            duration_seconds=1.234567, peak_memory_mb=42.0, command=["python", "main.py"],
        )
        d = r.to_dict()
        json.dumps(d)  # must not raise
        assert d["status"] == "success"
        assert d["succeeded"] is True

    def test_status_compares_to_string(self):
        assert ExecutionStatus.TIMEOUT == "timeout"

    def test_succeeded_only_on_success(self):
        assert not ExecutionResult(status=ExecutionStatus.FAILED).succeeded


# ═══════════════════════════════════════════════════════════════════════════
# AC: Resource monitoring accuracy
# ═══════════════════════════════════════════════════════════════════════════
class TestResourceMonitor:
    ALLOC_SCRIPT = (
        "import time\n"
        "b = bytearray(80 * 1024 * 1024)\n"       # 80 MB
        "for i in range(0, len(b), 4096): b[i] = 1\n"  # touch pages so RSS grows
        "time.sleep(0.4)\n"
    )

    def test_peak_memory_tracks_allocation(self):
        pytest.importorskip("psutil")
        proc = subprocess.Popen([sys.executable, "-c", self.ALLOC_SCRIPT])
        mon = ResourceMonitor(proc.pid, interval=0.02, memory_limit_mb=10_000).start()
        proc.wait()
        usage = mon.stop()
        assert usage.monitored is True
        assert usage.samples > 0
        # 80 MB allocation dominates a fresh interpreter's ~15 MB baseline.
        assert usage.peak_memory_mb is not None and usage.peak_memory_mb > 50
        assert usage.anomalies == []

    def test_flags_memory_anomaly(self):
        pytest.importorskip("psutil")
        proc = subprocess.Popen([sys.executable, "-c", self.ALLOC_SCRIPT])
        # A 1 MB ceiling is exceeded by any real process -> anomaly flagged.
        mon = ResourceMonitor(proc.pid, interval=0.02, memory_limit_mb=1).start()
        proc.wait()
        usage = mon.stop()
        assert any("exceeded" in a.lower() for a in usage.anomalies)
        assert any("OOM" in a for a in usage.anomalies)

    def test_missing_process_degrades_gracefully(self):
        pytest.importorskip("psutil")
        mon = ResourceMonitor(2_147_483_646, interval=0.01).start()  # no such pid
        usage = mon.stop()
        assert usage.monitored is False
        assert usage.peak_memory_mb is None
        assert usage.samples == 0


# ═══════════════════════════════════════════════════════════════════════════
# Graceful shutdown
# ═══════════════════════════════════════════════════════════════════════════
class TestTerminateProcess:
    def test_terminates_normal_process_with_sigterm(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        method = terminate_process(proc, sigterm_wait=5.0)
        assert method == "sigterm"
        assert proc.poll() is not None

    def test_already_exited(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert terminate_process(proc) == "already_exited"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
    def test_escalates_to_sigkill_when_sigterm_ignored(self):
        script = (
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(30)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(0.5)  # let the child install its SIGTERM handler
        method = terminate_process(proc, sigterm_wait=0.5)
        assert method == "sigkill"
        assert proc.poll() is not None


# ═══════════════════════════════════════════════════════════════════════════
# Dependency installer (retry / backoff / timeout via injected runner)
# ═══════════════════════════════════════════════════════════════════════════
def _fake_venv(_dir):
    return "/fake/venv/bin/python"


class TestDependencyInstaller:
    def test_is_network_error_classification(self):
        assert is_network_error("Failed to establish a new connection: [Errno -2]")
        assert not is_network_error("ERROR: No matching distribution found for foo")

    def test_success_first_try(self, tmp_path):
        inst = DependencyInstaller(runner=lambda cmd, to: (0, "ok", ""), venv_builder=_fake_venv)
        res = inst.install(tmp_path / "requirements.txt", tmp_path)
        assert res.success and res.attempts == 1
        assert res.python_executable == "/fake/venv/bin/python"

    def test_retries_network_error_then_succeeds(self, tmp_path):
        seq = [(1, "", "Connection reset by peer"), (0, "installed", "")]
        calls = {"n": 0}

        def runner(cmd, to):
            r = seq[calls["n"]]
            calls["n"] += 1
            return r

        sleeps = []
        inst = DependencyInstaller(
            runner=runner, venv_builder=_fake_venv, sleep=sleeps.append,
            backoff_base=0.01, max_retries=3,
        )
        res = inst.install(tmp_path / "requirements.txt", tmp_path)
        assert res.success and res.attempts == 2
        assert len(sleeps) == 1  # one backoff between the two attempts

    def test_persistent_network_error_exhausts_retries(self, tmp_path):
        sleeps = []
        inst = DependencyInstaller(
            runner=lambda cmd, to: (1, "", "Max retries exceeded with url"),
            venv_builder=_fake_venv, sleep=sleeps.append, backoff_base=0.01, max_retries=3,
        )
        res = inst.install(tmp_path / "requirements.txt", tmp_path)
        assert not res.success and res.attempts == 3
        assert len(sleeps) == 2  # backoff after attempts 1 and 2
        assert "network" in res.message.lower()

    def test_missing_distribution_fails_fast_with_clear_error(self, tmp_path):
        sleeps = []
        inst = DependencyInstaller(
            runner=lambda cmd, to: (1, "", "ERROR: No matching distribution found for foo==9.9"),
            venv_builder=_fake_venv, sleep=sleeps.append, max_retries=3,
        )
        res = inst.install(tmp_path / "requirements.txt", tmp_path)
        assert not res.success and res.attempts == 1  # non-network -> no retry
        assert sleeps == []
        assert "pip install failed" in res.message

    def test_timeout_is_reported(self, tmp_path):
        def runner(cmd, to):
            raise subprocess.TimeoutExpired(cmd, to)

        inst = DependencyInstaller(runner=runner, venv_builder=_fake_venv, timeout=5.0)
        res = inst.install(tmp_path / "requirements.txt", tmp_path)
        assert not res.success and res.timed_out
        assert "timeout" in res.message.lower()

    def test_venv_creation_failure_is_clean(self, tmp_path):
        def bad_venv(_dir):
            raise RuntimeError("boom")

        inst = DependencyInstaller(runner=lambda cmd, to: (0, "", ""), venv_builder=bad_venv)
        res = inst.install(tmp_path / "requirements.txt", tmp_path)
        assert not res.success and "virtualenv" in res.message.lower()

    def test_use_venv_false_uses_given_python(self, tmp_path):
        inst = DependencyInstaller(runner=lambda cmd, to: (0, "ok", ""))
        res = inst.install(
            tmp_path / "requirements.txt", tmp_path,
            use_venv=False, python_executable="/usr/bin/python3",
        )
        assert res.success and res.python_executable == "/usr/bin/python3"

    def test_install_result_serializable(self):
        r = InstallResult(True, 0, "o", "e", 1.0, 1, False, "/py", "ok")
        json.dumps(r.to_dict())


# ═══════════════════════════════════════════════════════════════════════════
# AC: execute simple script / timeout / missing dependency
# ═══════════════════════════════════════════════════════════════════════════
class TestLocalAdapterExecute:
    def test_simple_script_capture(self, tmp_path):
        d = make_bundle_dir(
            tmp_path,
            "print('hello world')\n"
            "open('produced.txt', 'w').write('done')\n",
        )
        adapter = LocalExecutionAdapter(poll_interval=0.02)
        res = adapter.execute(d, run_smoke=False, keep_artifacts=True)
        assert res.status == ExecutionStatus.SUCCESS
        assert res.exit_code == 0
        assert "hello world" in res.stdout
        assert (Path(res.artifacts_dir) / "produced.txt").is_file()

    def test_runs_in_copied_tempdir_not_source(self, tmp_path):
        # The adapter copies into a temp dir; the source bundle stays untouched.
        d = make_bundle_dir(tmp_path, "open('sideeffect.txt', 'w').write('x')\n")
        adapter = LocalExecutionAdapter(poll_interval=0.02)
        res = adapter.execute(d, run_smoke=False, keep_artifacts=True)
        assert res.status == ExecutionStatus.SUCCESS
        assert not (d / "sideeffect.txt").exists()          # source untouched
        assert (Path(res.artifacts_dir) / "sideeffect.txt").is_file()  # temp copy has it

    def test_timeout_is_handled_gracefully(self, tmp_path):
        d = make_bundle_dir(tmp_path, "import time\ntime.sleep(60)\n")
        adapter = LocalExecutionAdapter(poll_interval=0.05, sigterm_wait=1.0)
        start = time.monotonic()
        res = adapter.execute(d, run_smoke=False, timeout=1.0)
        elapsed = time.monotonic() - start
        assert res.status == ExecutionStatus.TIMEOUT
        assert elapsed < 20.0  # terminated promptly, did not wait out the 60s sleep
        assert "timeout" in res.message.lower()

    def test_missing_dependency_gives_clear_error(self, tmp_path):
        d = make_bundle_dir(tmp_path, "import a_module_that_does_not_exist_zzz\n")
        adapter = LocalExecutionAdapter(poll_interval=0.05)
        res = adapter.execute(d, run_smoke=False)
        assert res.status == ExecutionStatus.DEPENDENCY_ERROR
        assert "No module named" in res.stderr or "ModuleNotFoundError" in res.stderr
        assert "dependency" in res.message.lower()

    def test_nonzero_exit_is_failed(self, tmp_path):
        d = make_bundle_dir(tmp_path, "import sys\nsys.exit(3)\n")
        res = LocalExecutionAdapter(poll_interval=0.05).execute(d, run_smoke=False)
        assert res.status == ExecutionStatus.FAILED
        assert res.exit_code == 3

    def test_smoke_failure_short_circuits_before_main(self, tmp_path):
        # inline_tests.py exits 2 (missing dep) -> main.py must never run.
        d = make_bundle_dir(
            tmp_path,
            main_src="open('MAIN_RAN.txt', 'w').write('x')\n",
            inline_tests_src="import sys\nprint('MISSING DEPENDENCY: foo')\nsys.exit(2)\n",
        )
        res = LocalExecutionAdapter(poll_interval=0.05).execute(
            d, run_smoke=True, keep_artifacts=True
        )
        assert res.status == ExecutionStatus.DEPENDENCY_ERROR
        assert res.smoke_log["exit_code"] == 2
        assert not (Path(res.artifacts_dir) / "MAIN_RAN.txt").exists()

    def test_missing_entrypoint_is_setup_failed(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        res = LocalExecutionAdapter().execute(empty, run_smoke=False)
        assert res.status == ExecutionStatus.SETUP_FAILED

    def test_cleanup_removes_tempdir_by_default(self, tmp_path):
        d = make_bundle_dir(tmp_path, "print('ok')\n")
        res = LocalExecutionAdapter(poll_interval=0.05).execute(d, run_smoke=False)
        assert res.status == ExecutionStatus.SUCCESS
        assert res.artifacts_dir is None  # temp dir removed

    def test_accepts_runbundle_object(self, tmp_path):
        class FakeBundle:
            tool_name = "Faux"
            entrypoint = "main.py"

            def write(self, dest):
                (Path(dest) / "main.py").write_text("print('from bundle')\n", encoding="utf-8")

        res = LocalExecutionAdapter(poll_interval=0.05).execute(FakeBundle(), run_smoke=False)
        assert res.status == ExecutionStatus.SUCCESS
        assert "from bundle" in res.stdout
        assert res.tool_name == "Faux"


# ═══════════════════════════════════════════════════════════════════════════
# Working-directory naming: deterministic exec_<run_id>, so outputs trace back
# to the session (rather than a random twain_exec_<rand> dir).
# ═══════════════════════════════════════════════════════════════════════════
class TestWorkdirNaming:
    def test_run_id_names_workdir_deterministically(self, tmp_path):
        d = make_bundle_dir(tmp_path, "open('out.txt', 'w').write('x')\n")
        adapter = LocalExecutionAdapter(workspace_root=str(tmp_path), poll_interval=0.05)
        res = adapter.execute(d, run_smoke=False, keep_artifacts=True, run_id="sess123")
        assert res.artifacts_dir == str(tmp_path / "exec_sess123")
        assert (tmp_path / "exec_sess123" / "out.txt").is_file()

    def test_rerun_same_id_reuses_and_replaces_dir(self, tmp_path):
        d = make_bundle_dir(tmp_path, "open('out.txt', 'w').write('x')\n")
        adapter = LocalExecutionAdapter(workspace_root=str(tmp_path), poll_interval=0.05)
        r1 = adapter.execute(d, run_smoke=False, keep_artifacts=True, run_id="sess")
        r2 = adapter.execute(d, run_smoke=False, keep_artifacts=True, run_id="sess")
        assert r1.artifacts_dir == r2.artifacts_dir == str(tmp_path / "exec_sess")

    def test_run_id_is_sanitized(self, tmp_path):
        d = make_bundle_dir(tmp_path, "print(1)\n")
        adapter = LocalExecutionAdapter(workspace_root=str(tmp_path), poll_interval=0.05)
        res = adapter.execute(d, run_smoke=False, keep_artifacts=True, run_id="a/b c:d")
        assert res.artifacts_dir == str(tmp_path / "exec_a_b_c_d")

    def test_without_run_id_falls_back_to_random(self, tmp_path):
        d = make_bundle_dir(tmp_path, "print(1)\n")
        adapter = LocalExecutionAdapter(workspace_root=str(tmp_path), poll_interval=0.05)
        res = adapter.execute(d, run_smoke=False, keep_artifacts=True)
        assert Path(res.artifacts_dir).name.startswith("twain_exec_")


# ═══════════════════════════════════════════════════════════════════════════
# Definition of Done: real RunBundle executes locally; logs + metrics captured
# ═══════════════════════════════════════════════════════════════════════════
class TestEndToEnd:
    def test_pymatgen_runbundle_executes_locally(self, tmp_path):
        pytest.importorskip("pymatgen")
        from code_gen.codegen_engine import CodegenEngine

        bundle = CodegenEngine().generate(make_plan("Pymatgen"))
        adapter = LocalExecutionAdapter(poll_interval=0.02, workspace_root=str(tmp_path))
        res = adapter.execute(bundle, install_deps=False, run_smoke=True, keep_artifacts=True)

        assert res.status == ExecutionStatus.SUCCESS, res.stderr
        # logs captured
        assert res.stdout and json.loads(res.stdout)["tool"] == "Pymatgen"
        # metrics captured
        assert res.peak_memory_mb is not None and res.peak_memory_mb > 0
        # smoke ran before the real execution
        assert res.smoke_log is not None and res.smoke_log["exit_code"] == 0
        # artifact produced
        assert (Path(res.artifacts_dir) / "results.csv").is_file()
        # fully serializable for observability
        json.dumps(res.to_dict())
