"""Triage of failed calculations (#186), on this project's real failures."""
from __future__ import annotations

import pytest
import triage as T


def ev(status="failed", exit_code=1, stdout="", stderr=""):
    return T.evidence({"status": status, "exit_code": exit_code, "stdout": stdout, "stderr": stderr})


PIP_OK = lambda module: module.lower() in {"mdanalysis", "pandas"}

AM1BCC = (  # run cb1a625e
    "Traceback (most recent call last):\n"
    '  File "/tmp/twain-cb1a/main.py", line 67, in make_systems\n'
    "    inter = Interchange.from_smirnoff(force_field=ff, topology=off_top)\n"
    '  File "/storage2/x/twain-envs/nwchem/lib/python3.11/site-packages/openff/x.py", line 280\n'
    "    raise ValueError(msg)\n"
    'ValueError: No registered toolkits can provide the capability "assign_partial_charges"\n')


class TestRules:
    def test_a_setup_failure_is_the_operators(self):
        d = T.diagnose(ev("setup_failed", 6, stderr="TWAIN_BUNDLE_FETCH_FAILED: 403"),
                       pip_gettable=PIP_OK)
        assert (d.cls, d.action) == (T.OPERATOR, T.STOP)

    def test_run_75f06090s_wrong_crystal_is_a_script_fix(self):
        out = ("Traceback (most recent call last):\n  File \"main.py\", line 40\n"
               "twain_structure_guard.StructureMismatch: TWAIN_STRUCTURE_MISMATCH: the structure "
               "given to the calculator is not Silicon: it has 4 atoms per primitive cell\n")
        d = T.diagnose(ev(stderr=out), pip_gettable=PIP_OK)
        assert (d.cls, d.action) == (T.SCRIPT, T.PATCH) and "4 atoms" in d.detail

    def test_run_cb1a625es_traceback_is_a_script_fix(self):
        d = T.diagnose(ev(stdout=AM1BCC), pip_gettable=PIP_OK)
        assert (d.cls, d.action) == (T.SCRIPT, T.PATCH)
        assert "assign_partial_charges" in d.reason

    def test_a_pip_installable_module_is_added_for_this_run(self):
        d = T.diagnose(ev("dependency_error", 2, stdout="[smoke] MISSING DEPENDENCY: MDAnalysis"),
                       pip_gettable=PIP_OK)
        assert (d.cls, d.action, d.detail) == (T.ENVIRONMENT, T.ADD_REQUIREMENT, "MDAnalysis")

    def test_run_e825d5eds_conda_only_package_needs_a_shared_env_change(self):
        d = T.diagnose(ev("dependency_error", 2, stdout="[smoke] MISSING DEPENDENCY: openff.toolkit"),
                       pip_gettable=PIP_OK)
        assert (d.cls, d.action) == (T.ENVIRONMENT, T.STOP)
        assert "shared environment" in d.reason

    @pytest.mark.parametrize("e", [ev("timeout", None), ev(exit_code=124),
                                   ev(stdout="slurmstepd: error: Detected 1 oom-kill event")])
    def test_time_and_memory_need_the_researcher(self, e):
        assert T.diagnose(e, pip_gettable=PIP_OK).cls == T.RESOURCES

    def test_no_output_at_all_stops(self):
        assert T.diagnose(ev(), pip_gettable=PIP_OK).action == T.STOP


class TestTheLlmPicksFromTheMenu:
    UNCLEAR = ev(stdout="Segmentation fault (core dumped)\nsomething odd happened\n")

    def test_a_valid_choice(self):
        agent = lambda p: '{"action": "stop_shared_environment", "reason": "the binary is missing"}'
        d = T.diagnose(self.UNCLEAR, pip_gettable=PIP_OK, agent=agent)
        assert (d.cls, d.action, d.reason) == (T.ENVIRONMENT, T.STOP, "the binary is missing")

    @pytest.mark.parametrize("answer", ["no json here", '{"action": "rm -rf /"}', ""])
    def test_anything_else_stops_rather_than_guesses(self, answer):
        d = T.diagnose(self.UNCLEAR, pip_gettable=PIP_OK, agent=lambda p: answer)
        assert (d.cls, d.action) == (T.UNKNOWN, T.STOP)


class TestSignature:
    def test_paths_and_numbers_dont_change_it(self):
        a = ev(stdout=AM1BCC)
        b = ev(stdout=AM1BCC.replace("/tmp/twain-cb1a", "/tmp/twain-ffff").replace("line 67", "line 71"))
        assert T.signature(a) == T.signature(b)

    def test_a_different_error_does(self):
        assert T.signature(ev(stdout=AM1BCC)) != T.signature(
            ev(stdout=AM1BCC.replace("ValueError", "KeyError")))

    def test_mpi_rank_prefixes_are_ignored(self):
        plain = "Traceback (most recent call last):\n  File \"main.py\"\nKeyError: 'W'\n"
        mpi = "".join(f"rank=4 L0{i}: {ln}\n" for i, ln in enumerate(plain.splitlines()))
        assert T.evidence({"status": "failed", "stdout": mpi}).exception == "KeyError: 'W'"
