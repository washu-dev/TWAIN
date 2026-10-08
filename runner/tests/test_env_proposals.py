"""Shared-environment change proposals (#187): drafted from a triage stop, approved
from email, built + verified + promoted by one RIS job, the owner offered a re-run."""
from __future__ import annotations

import subprocess
import uuid
from types import SimpleNamespace

import pytest

from runner import env_proposals as EP
from runner.tests.conftest import pg_available

NWCHEM = EP.SPECS / "nwchem.yml"
PROFILE = SimpleNamespace(short_partition="general-short", default_partition="general-cpu",
                          account="compute2-mdan")
FAILURE = {"env": "nwchem", "self_heal": [
    {"attempt": 1, "class": "environment", "action": "stop",
     "reason": "'openff.toolkit' is missing and pip can't install it on the cluster: a shared "
               "environment needs it (an approved change)"}]}


class TestSpec:
    def test_the_package_is_appended_to_the_dependencies(self):
        spec = "name: x\nchannels:\n  - conda-forge\ndependencies:\n  - python=3.11\n  - numpy\n"
        out = EP.spec_with(spec, "xtb")
        assert out.endswith("  - numpy\n  # added by TWAIN proposal (#187): needed by a run that "
                            "failed without it\n  - xtb\n")
        assert EP.declared(out, "xtb") and not EP.declared(spec, "xtb")

    def test_a_pinned_package_counts_as_declared(self):
        assert EP.declared("dependencies:\n  - openmm>=8.1\n", "openmm")
        assert not EP.declared("dependencies:\n  - openmmforcefields\n", "openmm")


class TestFromFailure:
    def test_an_environment_stop_names_the_module_and_env(self):
        assert EP.proposal_from_failure(FAILURE)["module"] == "openff.toolkit"
        assert EP.proposal_from_failure(FAILURE)["env"] == "nwchem"

    @pytest.mark.parametrize("failure", [
        None, {"env": "nwchem", "self_heal": []},
        {"env": "nwchem", "self_heal": [{"class": "script", "action": "patch_script"}]},
        {**FAILURE, "env": None}])
    def test_anything_else_proposes_nothing(self, failure):
        assert EP.proposal_from_failure(failure) is None


class FakeDB:
    def __init__(self):
        self.proposals, self.updates, self.rows, self.marked = {}, [], [], 0

    def env_proposal_open(self, env, package):
        return next((p for p in self.proposals.values() if (p["env"], p["package"]) == (env, package)
                     and p["status"] in ("pending", "approved", "building")), None)

    def env_proposal_insert(self, **fields):
        p = {"id": len(self.proposals) + 1, "status": "pending", **fields}
        self.proposals[p["id"]] = p
        return p

    def env_proposals_by_status(self, status):
        return [dict(p) for p in self.proposals.values() if p["status"] == status]

    def env_proposal_update(self, pid, **fields):
        self.updates.append((pid, fields))
        self.proposals[pid].update(fields)

    def insert_email_action_rows(self, rows, hours):
        self.rows += rows

    def inventory_mark_due(self):
        self.marked += 1

    def owner_contact(self, session_id):
        return {"email": "researcher@wustl.edu"}


def _propose(db, sent, package="xtb", check=lambda p: True):
    return EP.propose(db, session_id="s1", env="nwchem", package=package, module=package,
                      reason="needed", base_url="https://x", check=check,
                      send=lambda *a: sent.append(a))


class TestPropose:
    def test_a_proposal_emails_each_approver_their_own_buttons(self, monkeypatch):
        monkeypatch.setenv("TWAIN_ENV_APPROVERS", "a@wustl.edu, b@wustl.edu")
        db, sent = FakeDB(), []
        p = _propose(db, sent)
        assert p["spec_before"] == NWCHEM.read_text() and "  - xtb" in p["spec_after"]
        assert [s[0] for s in sent] == ["a@wustl.edu", "b@wustl.edu"]
        assert [b["label"] for b in sent[0][3]] == ["Approve the change", "Reject"]
        assert {r[6] for r in db.rows} == {"a@wustl.edu", "b@wustl.edu"}   # recipient recorded
        assert "Rollback" in sent[0][2] and "every TWAIN run" in sent[0][2]

    def test_the_same_need_again_reuses_the_open_proposal_quietly(self):
        db, sent = FakeDB(), []
        first = _propose(db, sent)
        assert _propose(db, sent)["id"] == first["id"] and len(sent) == 1

    def test_nothing_is_proposed_for_a_package_conda_forge_lacks(self):
        assert _propose(FakeDB(), [], check=lambda p: False) is None

    def test_an_unreachable_conda_forge_doesnt_block_the_proposal(self):
        assert _propose(FakeDB(), [], check=lambda p: None) is not None

    def test_nothing_is_proposed_for_a_package_already_declared(self):
        assert _propose(FakeDB(), [], package="openmm") is None

    def test_nothing_is_proposed_for_an_unknown_env(self):
        assert EP.propose(FakeDB(), session_id="s1", env="nope", package="xtb", module="xtb",
                          reason="r", check=lambda p: True, send=lambda *a: None) is None


class FakeAdapter:
    def __init__(self, stdout="", terminal=True):
        self.specs, self.stdout, self.terminal = [], stdout, terminal

    def submit(self, spec, idempotency_key=None):
        self.specs.append((spec, idempotency_key))
        return 4242

    def poll(self, job_id):
        return SimpleNamespace(is_terminal=self.terminal, value="COMPLETED")

    def stdout_page(self, job_id, offset, size):
        return {"content": self.stdout, "next_offset": len(self.stdout), "eof": True}


class TestScheduler:
    def _approved(self, db):
        p = _propose(db, [])
        db.proposals[p["id"]]["status"] = "approved"
        return p

    def test_an_approved_proposal_becomes_one_short_maintenance_job(self):
        db, adapter = FakeDB(), FakeAdapter()
        p = self._approved(db)
        assert EP.EnvChangeScheduler(db, adapter, PROFILE, env_file="/s/twain.sh",
                                     send=lambda *a: None).tick() == ["building"]
        spec, key = adapter.specs[0]
        assert (spec["partition"], spec["account"], key) == \
            ("general-short", "compute2-mdan", f"twain-env-change-{p['id']}")
        assert db.proposals[p["id"]]["status"] == "building"
        assert db.proposals[p["id"]]["version"].endswith(f".p{p['id']}")

    def test_the_job_builds_verifies_imports_then_promotes_in_that_order(self):
        p = {"id": 9, "env": "nwchem", "package": "xtb", "module": "xtb",
             "spec_after": EP.spec_with(NWCHEM.read_text(), "xtb")}
        script = EP._job_script(p, "2026-10-08.p9", "/s/twain.sh")
        order = [script.index(s) for s in ("build 2026-10-08.p9 nwchem", "verify 2026-10-08.p9",
                                           'python -c "import xtb"', "promote 2026-10-08.p9")]
        assert order == sorted(order)
        assert "  - xtb" in script                     # the proposed spec, not the shipped one
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)

    def test_promotion_reports_and_offers_the_owner_a_rerun(self, monkeypatch):
        monkeypatch.setenv("TWAIN_API_PUBLIC_URL", "https://x")
        db, sent = FakeDB(), []
        p = self._approved(db)
        sched = EP.EnvChangeScheduler(db, FakeAdapter(), PROFILE, env_file="", send=lambda *a: sent.append(a))
        sched.tick()
        version = db.proposals[p["id"]]["version"]
        sched.adapter = FakeAdapter(f"[verify] ok\nTWAIN_ENV_CHANGE: promoted nwchem -> {version}\n")
        assert sched.tick() == ["promoted"]
        assert db.proposals[p["id"]]["status"] == "promoted" and db.marked == 1
        assert "is now in" in sent[0][1]
        assert sent[-1][0] == "researcher@wustl.edu" and sent[-1][3][0]["label"] == "Re-run from EXECUTE"

    def test_a_failed_job_leaves_the_env_and_says_so(self):
        db, sent = FakeDB(), []
        p = self._approved(db)
        sched = EP.EnvChangeScheduler(db, FakeAdapter(), PROFILE, env_file="", send=lambda *a: sent.append(a))
        sched.tick()
        sched.adapter = FakeAdapter("TWAIN_ENV_CHANGE: verify failed\n")
        assert sched.tick() == ["failed"]
        assert db.proposals[p["id"]]["status"] == "failed" and db.marked == 0
        assert "could NOT be added" in sent[0][1] and len(sent) == 1

    def test_a_running_job_is_left_alone(self):
        db = FakeDB()
        self._approved(db)
        sched = EP.EnvChangeScheduler(db, FakeAdapter(), PROFILE, env_file="", send=lambda *a: None)
        sched.tick()
        sched.adapter = FakeAdapter(terminal=False)
        assert sched.tick() == ["running"]


@pytest.mark.skipif(not pg_available(), reason="no local Postgres")
class TestAgainstPostgres:
    def _run(self, db, status="error"):
        uid = db._query_one("INSERT INTO users (subject, email) VALUES (%s, 'r@wustl.edu') "
                            "RETURNING id;", (uuid.uuid4().hex,), commit=True)["id"]
        return str(db._query_one("INSERT INTO conversations (user_id, title, status) VALUES "
                                 "(%s, 'am1-bcc solvation', %s) RETURNING id;", (uid, status),
                                 commit=True)["id"])

    @staticmethod
    def _tokens(sent):
        return [[b["url"].rsplit("/", 1)[1] for b in s[3]] for s in sent]

    def _propose(self, db, sid, sent):
        return EP.propose(db, session_id=sid, env="nwchem", package="xtb", module="xtb",
                          reason="needed", base_url="https://x", check=lambda p: True,
                          send=lambda *a: sent.append(a))

    def test_one_approvers_yes_decides_for_everyone(self, pg_actions, monkeypatch):
        monkeypatch.setenv("TWAIN_ENV_APPROVERS", "a@wustl.edu,b@wustl.edu")
        db, api = pg_actions
        sid, sent = self._run(db), []
        p = self._propose(db, sid, sent)
        (a_yes, a_no), (b_yes, _b_no) = self._tokens(sent)
        assert api.peek(b_yes).state == "ok" and "add xtb" in api.peek(b_yes).proposal
        assert api.consume(b_yes).state == "ok"
        row = db._query_one("SELECT status, decided_by FROM env_proposals WHERE id = %s;", (p["id"],))
        assert (row["status"], row["decided_by"]) == ("approved", "b@wustl.edu")
        assert api.consume(a_no).state == "used"           # the other approver's buttons too
        assert [x["id"] for x in db.env_proposals_by_status("approved")] == [p["id"]]

    def test_reject_closes_it_and_a_new_need_can_propose_again(self, pg_actions):
        db, api = pg_actions
        sid, sent = self._run(db), []
        p = self._propose(db, sid, sent)
        assert api.consume(self._tokens(sent)[0][1]).state == "ok"
        assert db._query_one("SELECT status FROM env_proposals WHERE id = %s;",
                             (p["id"],))["status"] == "rejected"
        assert self._propose(db, sid, [])["id"] != p["id"]

    def test_the_rerun_button_restarts_a_finished_run_from_execute(self, pg_actions):
        from runner import email_actions as issuer
        db, api = pg_actions
        sid = self._run(db, "error")
        (token,) = [b["url"].rsplit("/", 1)[1]
                    for b in issuer.issue_rerun(db, sid, "r@wustl.edu", base_url="https://x")]
        assert api.consume(token).state == "ok"
        job = db._query_one("SELECT kind, params FROM jobs WHERE session_id = %s "
                            "ORDER BY id DESC LIMIT 1;", (sid,))
        assert job["kind"] == "rerun" and "EXECUTE" in str(job["params"])
        assert api.consume(token).state == "used"

    def test_the_rerun_button_does_nothing_while_the_run_is_active(self, pg_actions):
        from runner import email_actions as issuer
        db, api = pg_actions
        sid = self._run(db, "running")
        (token,) = [b["url"].rsplit("/", 1)[1]
                    for b in issuer.issue_rerun(db, sid, "r@wustl.edu", base_url="https://x")]
        assert api.consume(token).state == "answered"


class TestRunnerHook:
    def test_a_failed_run_proposes_the_package_and_tells_the_researcher(self, monkeypatch):
        from runner import runner
        calls, said = [], []
        monkeypatch.setattr(EP, "propose", lambda db, **kw: calls.append(kw) or {"id": 3})
        db = SimpleNamespace(add_assistant_message=lambda sid, text, kind: said.append(text))
        runner._propose_env_change(db, "s1", FAILURE)
        assert (calls[0]["env"], calls[0]["module"], calls[0]["package"]) == \
            ("nwchem", "openff.toolkit", "openff-toolkit")
        assert "proposal #3" in said[0]

    def test_it_never_raises(self, monkeypatch):
        from runner import runner
        monkeypatch.setattr(EP, "propose", lambda db, **kw: 1 / 0)
        runner._propose_env_change(SimpleNamespace(), "s1", FAILURE)
