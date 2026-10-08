"""Method outcomes (#188): what a finished run records, and what planning reads back."""
from __future__ import annotations

import uuid

import pytest

from runner import method_history as MH
from runner.tests.conftest import pg_available

PLAN = {"requested_property": "Band gap",
        "selected_method": {"tool_name": "ASE", "libraries": ["ASE"], "calculator": "GPAW"}}


class TestOutcomes:
    def test_a_finished_calculation_is_recorded_with_its_verdict(self):
        (row,) = MH.outcomes("s1", PLAN, {"succeeded": True, "status": "success"}, "accepted", [])
        assert (row["requested_property"], row["method"], row["succeeded"], row["verdict"]) == \
            ("band_gap", "gpaw", True, "accepted")

    def test_an_abandoned_method_is_recorded_as_failed(self):
        rows = MH.outcomes("s1", PLAN, {"succeeded": True, "status": "success"}, "accepted",
                           [{"method": "xtb", "calculator": "xTB", "libraries": ["ASE"]}])
        assert [(r["method"], r["succeeded"]) for r in rows] == [("xtb", False), ("gpaw", True)]

    @pytest.mark.parametrize("result", [None, {"status": "deferred", "succeeded": None},
                                        {"status": "skipped", "succeeded": False}])
    def test_nothing_ran_nothing_recorded(self, result):
        assert MH.outcomes("s1", PLAN, result, None, []) == []

    def test_a_failed_calculation_has_no_verdict(self):
        (row,) = MH.outcomes("s1", PLAN, {"succeeded": False, "status": "failed"}, "rejected", [])
        assert (row["succeeded"], row["verdict"]) == (False, None)

    def test_record_never_raises(self):
        MH.record(object(), "s1", object())


@pytest.mark.skipif(not pg_available(), reason="no local Postgres")
class TestAgainstPostgres:
    def _run(self, db):
        uid = db._query_one("INSERT INTO users (subject, email) VALUES (%s, 'r@wustl.edu') "
                            "RETURNING id;", (uuid.uuid4().hex,), commit=True)["id"]
        return str(db._query_one("INSERT INTO conversations (user_id, title, status) VALUES "
                                 "(%s, 'si gap', 'completed') RETURNING id;", (uid,),
                                 commit=True)["id"])

    def test_history_ranks_what_was_accepted_first(self, pg_actions):
        db, _ = pg_actions
        ok = {"succeeded": True, "status": "success"}
        for verdict in ("accepted", "accepted"):
            db.record_method_outcomes(MH.outcomes(self._run(db), PLAN, ok, verdict, []))
        xtb = {**PLAN, "selected_method": {"libraries": ["ASE"], "calculator": "xTB"}}
        for verdict in ("rejected", "rejected", "rejected"):
            db.record_method_outcomes(MH.outcomes(self._run(db), xtb, ok, verdict, []))
        db.record_method_outcomes(MH.outcomes(self._run(db), {**PLAN, "selected_method": {
            "libraries": ["ASE"], "calculator": "DFTB+"}}, {"succeeded": False}, None, []))
        history = db.method_history("band_gap")
        assert [(h["method"], h["completed"], h["accepted"]) for h in history] == \
            [("gpaw", 2, 2), ("xtb", 3, 0)]                 # never-completed DFTB+ left out
        assert history[0]["libraries"] == ["ASE"]

    def test_a_rerun_overwrites_its_own_row(self, pg_actions):
        db, _ = pg_actions
        sid = self._run(db)
        db.record_method_outcomes(MH.outcomes(sid, PLAN, {"succeeded": False}, None, []))
        db.record_method_outcomes(MH.outcomes(sid, PLAN, {"succeeded": True}, "accepted", []))
        assert [(h["completed"], h["failed"]) for h in db.method_history("band_gap")] == [(1, 0)]
