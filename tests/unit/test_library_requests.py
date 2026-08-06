"""Unit tests for the LibraryAddition install-request feature.

TWAIN still may only *use* libraries that are installed. What these tests pin
down is what now happens to the ones it can't use:

  * method_discovery.library_requests -- the ledger (dedup across runs), the
    GitHub issue payload + label, marker-based dedup against an existing issue,
    and graceful degradation with no credentials.
  * StateMachine.plan()/discover() -- an LLM pick, a top-ranked candidate, and a
    user-named library that aren't installed each produce a request, a plan note,
    and a plan that still runs on a preset library.

Every network/env seam is injected, so the whole suite is offline.

Run from the repo root with:  pixi run pytest tests/unit/test_library_requests.py
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO_ROOT / "modules" / "16_agent_mesh_control_plane"
sys.path.insert(0, str(MODULE_DIR))

from states import State  # noqa: E402
from crash_recovery import DataStorage  # noqa: E402
import statemachine as SM  # noqa: E402
from method_discovery import library_requests as LR  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ═══════════════════════════════════════════════════════════════════════════
class FakeGitHub:
    """Stands in for the GitHub REST transport; records every call."""

    def __init__(self, *, existing=None, create_status=201, search_status=200):
        self.existing = existing          # (number, url) an earlier issue to "find"
        self.create_status = create_status
        self.search_status = search_status
        self.calls = []                   # (method, url, payload)
        self.next_number = 101

    def __call__(self, method, url, token, payload):
        self.calls.append((method, url, payload))
        if "/search/issues" in url:
            if self.search_status != 200:
                return self.search_status, {"message": "boom"}
            if not self.existing:
                return 200, {"items": []}
            number, html = self.existing
            return 200, {"items": [{"number": number, "html_url": html}]}
        if url.endswith("/labels"):
            return 201, {"name": LR.LIBRARY_ADDITION_LABEL}
        if url.endswith("/issues"):
            if self.create_status not in (200, 201):
                return self.create_status, {"message": "no"}
            number = self.next_number
            self.next_number += 1
            return self.create_status, {
                "number": number,
                "html_url": f"https://github.com/washu-dev/TWAIN/issues/{number}",
            }
        raise AssertionError(f"unexpected call: {method} {url}")

    @property
    def created_payloads(self):
        return [p for m, u, p in self.calls if m == "POST" and u.endswith("/issues")]


def tracker(tmp_path, transport=None, *, run_id="run-1", objective="band gap of silicon",
            enabled=True, **kw):
    filer = LR.GitHubIssueFiler(repo="washu-dev/TWAIN", token="tok",
                               transport=transport or FakeGitHub(), enabled=enabled)
    return LR.LibraryRequestTracker(run_id=run_id, objective=objective, filer=filer,
                                    ledger_path=tmp_path / "library_requests.json", **kw)


# ═══════════════════════════════════════════════════════════════════════════
# canonical_name / LibraryRequest
# ═══════════════════════════════════════════════════════════════════════════
class TestCanonicalName:
    @pytest.mark.parametrize("raw,expected", [
        ("VASP", "vasp"),
        ("Open Babel", "open-babel"),
        ("  scikit_learn ", "scikit-learn"),
        ("Quantum   ESPRESSO", "quantum-espresso"),
    ])
    def test_normalizes(self, raw, expected):
        assert LR.canonical_name(raw) == expected

    def test_case_and_separator_variants_are_one_ask(self):
        assert LR.canonical_name("Open_Babel") == LR.canonical_name("open babel")
        assert LR.canonical_name("GPAW") == LR.canonical_name("gpaw")


class TestLibraryRequestNote:
    def test_note_states_fallback_and_the_recorded_request(self):
        req = LR.LibraryRequest(library="VASP", source=LR.SOURCE_USER,
                                alternative="ASE", status=LR.STATUS_CREATED,
                                issue_url="https://github.com/o/r/issues/7")
        note = req.note()
        assert "VASP" in note and "not installed" in note
        assert "'ASE' instead" in note                 # stuck to a preset library
        assert LR.LIBRARY_ADDITION_LABEL in note       # the unique tag
        assert "https://github.com/o/r/issues/7" in note

    def test_note_without_credentials_says_recorded_locally(self):
        req = LR.LibraryRequest(library="VASP", alternative="ASE")
        assert req.status == LR.STATUS_QUEUED
        assert "recorded locally" in req.note()
        assert "no GitHub credentials" in req.note()

    def test_empty_library_rejected(self):
        with pytest.raises(ValueError):
            LR.LibraryRequest(library="")


# ═══════════════════════════════════════════════════════════════════════════
# Issue payload
# ═══════════════════════════════════════════════════════════════════════════
class TestIssuePayload:
    def test_title_and_body(self):
        req = LR.LibraryRequest(library="VASP", source=LR.SOURCE_LLM,
                                alternative="ASE", objective="band gap of Si",
                                run_id="r1", reason="best for periodic DFT")
        assert LR.issue_title(req) == (
            f"[{LR.LIBRARY_ADDITION_LABEL}] Install 'VASP' in the TWAIN environment")
        body = LR.issue_body(req)
        assert "`VASP`" in body and "`ASE`" in body
        assert "best for periodic DFT" in body
        assert "band gap of Si" in body and "`r1`" in body
        # Actionable: names the files a maintainer has to touch.
        assert "pixi.toml" in body
        assert "configs/discovery_registry.json" in body
        assert "dependency_inferencer.py" in body
        # The dedup marker is what a later run searches for.
        assert "<!-- twain-library-request:vasp -->" in body

    def test_label_is_attached_to_the_created_issue(self, tmp_path):
        gh = FakeGitHub()
        tracker(tmp_path, gh).record("VASP", alternative="ASE")
        payload = gh.created_payloads[0]
        assert payload["labels"] == [LR.LIBRARY_ADDITION_LABEL]
        assert payload["title"].startswith(f"[{LR.LIBRARY_ADDITION_LABEL}]")

    def test_label_is_created_with_colour_and_description(self, tmp_path):
        gh = FakeGitHub()
        tracker(tmp_path, gh).record("VASP")
        label_calls = [p for m, u, p in gh.calls if u.endswith("/labels")]
        assert label_calls and label_calls[0]["name"] == LR.LIBRARY_ADDITION_LABEL
        assert label_calls[0]["color"] == LR.LABEL_COLOR


# ═══════════════════════════════════════════════════════════════════════════
# Tracker: ledger, dedup, degradation
# ═══════════════════════════════════════════════════════════════════════════
class TestTracker:
    def test_records_files_and_ledgers_a_new_request(self, tmp_path):
        gh = FakeGitHub()
        t = tracker(tmp_path, gh)
        req = t.record("VASP", source=LR.SOURCE_USER, alternative="ASE")

        assert req.status == LR.STATUS_CREATED
        assert req.issue_number == 101
        assert req.occurrences == 1
        ledger = json.loads((tmp_path / "library_requests.json").read_text())
        assert ledger["vasp"]["issue_number"] == 101
        assert ledger["vasp"]["source"] == LR.SOURCE_USER
        assert ledger["vasp"]["run_id"] == "run-1"

    def test_repeat_within_one_run_is_one_request(self, tmp_path):
        gh = FakeGitHub()
        t = tracker(tmp_path, gh)
        first = t.record("VASP", alternative="ASE")
        again = t.record("vasp")            # same ask, different casing

        assert again is first
        assert len(gh.created_payloads) == 1
        assert len(t.records) == 1          # one note, not two
        assert first.occurrences == 1       # occurrences counts runs, not asks

    def test_second_run_reuses_the_ledgered_issue(self, tmp_path):
        gh1 = FakeGitHub()
        tracker(tmp_path, gh1, run_id="run-1").record("VASP", alternative="ASE")

        gh2 = FakeGitHub()
        req = tracker(tmp_path, gh2, run_id="run-2").record("VASP", alternative="ASE")

        assert gh2.calls == []              # no GitHub traffic at all
        assert req.issue_number == 101      # carried from the ledger
        assert req.occurrences == 2         # but the demand count grows
        assert req.first_requested_at  # preserved from the first run

    def test_wiped_ledger_finds_the_existing_issue_instead_of_duplicating(self, tmp_path):
        gh = FakeGitHub(existing=(55, "https://github.com/o/r/issues/55"))
        req = tracker(tmp_path, gh).record("VASP", alternative="ASE")

        assert req.status == LR.STATUS_EXISTS
        assert (req.issue_number, req.issue_url) == (55, "https://github.com/o/r/issues/55")
        assert gh.created_payloads == []    # nothing new was filed

    def test_no_credentials_still_records_and_notes(self, tmp_path):
        filer = LR.GitHubIssueFiler(repo=None, token=None, transport=FakeGitHub())
        t = LR.LibraryRequestTracker(run_id="r", filer=filer,
                                     ledger_path=tmp_path / "l.json")
        req = t.record("VASP", alternative="ASE")

        assert filer.enabled is False
        assert req.status == LR.STATUS_QUEUED
        assert json.loads((tmp_path / "l.json").read_text())["vasp"]["library"] == "VASP"
        assert "recorded locally" in t.notes()[0]

    def test_previously_queued_request_is_retried_once_credentials_exist(self, tmp_path):
        offline = LR.GitHubIssueFiler(repo=None, token=None, transport=FakeGitHub())
        LR.LibraryRequestTracker(run_id="r1", filer=offline,
                                 ledger_path=tmp_path / "l.json").record("VASP")

        gh = FakeGitHub()
        online = LR.GitHubIssueFiler(repo="o/r", token="t", transport=gh, enabled=True)
        req = LR.LibraryRequestTracker(run_id="r2", filer=online,
                                       ledger_path=tmp_path / "l.json").record("VASP")

        assert req.status == LR.STATUS_CREATED
        assert req.issue_number == 101

    def test_github_failure_does_not_raise_and_is_recorded(self, tmp_path):
        gh = FakeGitHub(create_status=403)
        req = tracker(tmp_path, gh).record("VASP", alternative="ASE")

        assert req.status == LR.STATUS_FAILED
        assert req.issue_url is None
        assert json.loads((tmp_path / "library_requests.json").read_text())["vasp"]
        assert "failed" in req.note()

    def test_transport_exception_is_swallowed(self, tmp_path):
        def boom(*a, **kw):
            raise OSError("network down")

        req = tracker(tmp_path, boom).record("VASP", alternative="ASE")
        assert req.status == LR.STATUS_FAILED

    def test_blank_library_is_ignored(self, tmp_path):
        t = tracker(tmp_path)
        assert t.record("  ") is None
        assert t.records == []

    def test_disabled_by_env_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TWAIN_LIBRARY_REQUEST_ISSUES", "0")
        monkeypatch.setenv("TWAIN_GITHUB_REPO", "o/r")
        monkeypatch.setenv("TWAIN_GITHUB_TOKEN", "tok")
        gh = FakeGitHub()
        filer = LR.GitHubIssueFiler(transport=gh)
        assert filer.enabled is False
        LR.LibraryRequestTracker(filer=filer, ledger_path=tmp_path / "l.json").record("VASP")
        assert gh.calls == []


# ═══════════════════════════════════════════════════════════════════════════
# StateMachine integration
# ═══════════════════════════════════════════════════════════════════════════
SILICON_INTENT = {
    "objective": "what is the band gap of silicon",
    "domain": "materials",
    "system_descriptors": {"formula": "Si", "kind": "crystal",
                           "crystal": {"formula": "Si", "phase": "diamond"}},
    "acceptance_metrics": [{"metric_name": "band_gap", "target_value": 1.12, "tolerance": 0.2}],
    "metadata": {"ambiguity": False, "confidence_scores": {"objective_confidence": 0.95}},
}


@pytest.fixture(autouse=True)
def _no_docker():
    """Pin Docker OFF so platform-dependent planning is deterministic here."""
    with patch.object(SM, "docker_available", return_value=False):
        yield


def machine(tmp_path, intent=None, *, requested=None, uninstalled=(), agent=None,
            transport=None):
    """A hermetic StateMachine with an injected library-request tracker.

    ``uninstalled`` names libraries the probe reports as missing; everything else
    reads as installed, so these tests never depend on the host environment.
    """
    missing = {n.lower() for n in uninstalled}
    gh = transport if transport is not None else FakeGitHub()
    tr = tracker(tmp_path, gh, objective=(intent or SILICON_INTENT).get("objective"))
    with patch.object(DataStorage, "load", return_value=None):
        m = SM.StateMachine(
            data_path=str(tmp_path / "state.json"), run_id="t", agent=agent,
            sim_available=lambda names: set(),
            library_available=lambda name: name.lower() not in missing,
            library_request_tracker=tr,
        )
    m.artifacts_dir = tmp_path
    spec = dict(intent or SILICON_INTENT)
    if requested is not None:
        spec["requested_libraries"] = requested
    path = tmp_path / "intent_spec_seed.json"
    path.write_text(json.dumps(spec))
    m.context.artifacts["intent_spec"] = str(path)
    return m, gh


def plan_of(m):
    return m._load_artifact("execution_plan")


class TestUserRequestedLibrary:
    def test_uninstalled_user_request_files_an_issue_and_falls_back(self, tmp_path):
        m, gh = machine(tmp_path, requested=["VASP"], uninstalled=["VASP"])
        assert m.plan() == State.BUILD
        plan = plan_of(m)

        # 1) The plan sticks to installed, preset libraries -- VASP is nowhere in it.
        libs = plan["selected_method"]["libraries"]
        assert libs and not any(l.lower() == "vasp" for l in libs)
        # 2) The request was recorded, tagged, and filed.
        assert [r["library"] for r in plan["library_requests"]] == ["VASP"]
        assert plan["library_requests"][0]["source"] == LR.SOURCE_USER
        assert plan["library_requests"][0]["status"] == LR.STATUS_CREATED
        assert gh.created_payloads[0]["labels"] == [LR.LIBRARY_ADDITION_LABEL]
        # 3) The researcher is told, on the plan they approve.
        note = next(n for n in plan["safety_notes"] if "VASP" in n)
        assert "not installed" in note and "preset" in note
        assert plan["library_requests"][0]["alternative"] == libs[0]

    def test_installed_user_request_is_honoured_not_requested(self, tmp_path):
        m, gh = machine(tmp_path, requested=["Pymatgen"])
        assert m.plan() == State.BUILD
        plan = plan_of(m)

        assert plan["selected_method"]["libraries"][0].lower() == "pymatgen"
        assert plan.get("library_requests") == []
        assert gh.calls == []       # nothing to ask for

    def test_no_request_means_no_tracker_traffic(self, tmp_path):
        m, gh = machine(tmp_path)
        m.plan()
        assert plan_of(m).get("library_requests") == []
        assert gh.calls == []

    def test_unknown_requested_name_is_requested_too(self, tmp_path):
        """A tool that isn't in any registry is still a legitimate install ask."""
        m, gh = machine(tmp_path, requested=["CASTEP"], uninstalled=["CASTEP"])
        m.plan()
        assert [r["library"] for r in plan_of(m)["library_requests"]] == ["CASTEP"]


class TestLLMPickedLibrary:
    @staticmethod
    def _agent(library, supporting=(), calculator=None):
        reply = json.dumps({"library": library, "supporting_libraries": list(supporting),
                            "calculator": calculator, "reasoning": "best fit"})
        return lambda prompt: reply

    def test_uninstalled_llm_primary_is_requested_and_replaced(self, tmp_path):
        m, gh = machine(tmp_path, uninstalled=["Psi4"],
                            agent=self._agent("Psi4"))
        m.plan()
        plan = plan_of(m)

        assert not any(l.lower() == "psi4" for l in plan["selected_method"]["libraries"])
        req = plan["library_requests"][0]
        assert req["library"] == "Psi4" and req["source"] == LR.SOURCE_LLM
        assert "best fit" in req["reason"]
        assert any("Psi4" in n and "not installed" in n for n in plan["safety_notes"])

    def test_uninstalled_supporting_library_is_requested_and_dropped(self, tmp_path):
        # Names a calculator because ASE is a DRIVER, not an engine: a band-gap plan
        # of ASE alone has nothing to compute with, and plan() now refuses it rather
        # than letting codegen invent an engine (see the NaCl2 run, Slurm job
        # 2633871). EMT keeps this hermetic -- pure ASE, available on every platform
        # -- and the assertions here are about library bookkeeping either way.
        m, gh = machine(tmp_path, uninstalled=["Psi4"],
                            agent=self._agent("ASE", supporting=["Psi4"],
                                              calculator="EMT"))
        m.plan()
        plan = plan_of(m)

        libs = [l.lower() for l in plan["selected_method"]["libraries"]]
        assert "ase" in libs and "psi4" not in libs      # primary kept, extra dropped
        assert [r["library"] for r in plan["library_requests"]] == ["Psi4"]

    def test_user_and_llm_asking_for_the_same_library_is_one_request(self, tmp_path):
        m, gh = machine(tmp_path, requested=["Psi4"], uninstalled=["Psi4"],
                            agent=self._agent("Psi4"))
        m.plan()
        plan = plan_of(m)

        assert len(plan["library_requests"]) == 1
        assert len(gh.created_payloads) == 1
        assert sum("Psi4" in n for n in plan["safety_notes"]) == 1
        # The researcher's own ask is the one worth attributing.
        assert plan["library_requests"][0]["source"] == LR.SOURCE_USER


class TestRankingPreemption:
    def test_top_ranked_uninstalled_candidate_is_requested(self, tmp_path):
        """The tool discovery would have picked, had it been installed."""
        m, gh = machine(tmp_path, uninstalled=["ASE"])
        assert m.discover() == State.PLAN
        discovery = m._load_artifact("discovery")

        assert "ASE" in discovery["unavailable_candidates"]
        assert [r["library"] for r in discovery["library_requests"]] == ["ASE"]
        assert discovery["library_requests"][0]["source"] == LR.SOURCE_RANKING
        assert all(c["name"] != "ASE" for c in discovery["candidates"])

    def test_lower_ranked_uninstalled_candidate_is_noted_but_not_requested(self, tmp_path):
        """Only a tool that out-ranked everything installed is worth an issue."""
        m, gh = machine(tmp_path, uninstalled=["Psi4"])
        m.discover()
        discovery = m._load_artifact("discovery")

        assert "Psi4" in discovery["unavailable_candidates"]     # still disclosed
        assert discovery["library_requests"] == []               # but no issue
        assert gh.calls == []


class TestPlanArtifactStaysValid:
    def test_plan_with_requests_validates_against_the_schema(self, tmp_path):
        m, gh = machine(tmp_path, requested=["VASP"], uninstalled=["VASP"])
        m.plan()
        schema = json.loads((REPO_ROOT / "schemas" / "execution_plan.schema.json").read_text())
        Draft202012Validator(schema).validate(plan_of(m))

    def test_recording_failure_never_breaks_planning(self, tmp_path):
        class Broken:
            def record(self, *a, **kw):
                raise RuntimeError("ledger on fire")

        with patch.object(DataStorage, "load", return_value=None):
            m = SM.StateMachine(data_path=str(tmp_path / "s.json"), run_id="t",
                                sim_available=lambda names: set(),
                                library_available=lambda n: n.lower() != "vasp",
                                library_request_tracker=Broken())
        m.artifacts_dir = tmp_path
        spec = {**SILICON_INTENT, "requested_libraries": ["VASP"]}
        (tmp_path / "seed.json").write_text(json.dumps(spec))
        m.context.artifacts["intent_spec"] = str(tmp_path / "seed.json")

        assert m.plan() == State.BUILD          # planning still completes
        assert plan_of(m)["selected_method"]["libraries"]


class TestDocstrings:
    def test_module_doctests(self):
        import doctest
        assert doctest.testmod(LR).failed == 0
