"""Unit tests for the control-plane budget primitives (budget_tracker.py).

Covers the three nested scopes directly (no orchestrator): RunBudget's cost /
iteration / wall-time gates, ProjectBudget's aggregation + ceiling, and the
top-level BudgetTracker's global cap and quota ledger.

Run from the repo root with:  pixi run pytest tests/unit/test_budget_tracker.py
"""
import sys
from pathlib import Path

import pytest

# The module lives in a digit-prefixed directory that can't be imported by dotted
# name; put the orchestrator dir on sys.path and let _bootstrap wire the rest
# (budget_tracker is imported bare, exactly as orchestrator.py does it).
ORCH_DIR = Path(__file__).resolve().parents[2] / "modules" / "07_runtime_orchestrator"
sys.path.insert(0, str(ORCH_DIR))

import _bootstrap  # noqa: E402,F401

from budget_tracker import (  # noqa: E402
    BudgetTracker, ProjectBudget, RunBudget,
    OverBudget, OverMaxIterations, OverMaxWallTime,
)


class TestRunBudget:
    def test_accumulates_cost_and_iterations(self):
        rb = RunBudget(max_cost=1.0, max_iterations=5)
        rb.add_cost(0.2)
        rb.add_cost(0.3)
        rb.add_iteration()
        assert rb.get_cost() == pytest.approx(0.5)
        assert rb.iterations == 1

    def test_check_passes_under_all_limits(self):
        rb = RunBudget(max_cost=1.0, max_iterations=5, wall_time_minutes=30)
        rb.add_cost(0.5)
        rb.add_iteration()
        rb.check()  # must not raise

    def test_over_cost_raises(self):
        rb = RunBudget(max_cost=0.5)
        rb.add_cost(0.5)                      # reaching the cap trips it (>=)
        with pytest.raises(OverBudget):
            rb.check()

    def test_over_iterations_raises(self):
        rb = RunBudget(max_cost=10.0, max_iterations=2)
        rb.add_iteration()
        rb.add_iteration()
        with pytest.raises(OverMaxIterations):
            rb.check()

    def test_over_wall_time_raises(self):
        rb = RunBudget(max_cost=10.0, max_iterations=100, wall_time_minutes=0)
        # wall_time == 0 => any elapsed time is over the limit.
        with pytest.raises(OverMaxWallTime):
            rb.check()

    def test_cost_checked_before_iterations(self):
        # Both limits blown; cost is checked first, so OverBudget wins.
        rb = RunBudget(max_cost=0.1, max_iterations=1)
        rb.add_cost(1.0)
        rb.add_iteration()
        with pytest.raises(OverBudget):
            rb.check()

    def test_to_dict_shape(self):
        rb = RunBudget(max_cost=2.0, max_iterations=7, wall_time_minutes=15)
        rb.add_cost(0.25)
        rb.add_iteration()
        d = rb.to_dict()
        assert d["cost"] == pytest.approx(0.25)
        assert d["max_cost"] == 2.0
        assert d["iterations"] == 1
        assert d["max_iterations"] == 7
        assert d["wall_time_limit_seconds"] == 15 * 60
        assert d["elapsed_seconds"] >= 0


class TestProjectBudget:
    def test_get_cost_sums_runs(self):
        tracker = BudgetTracker(global_budget=100.0)
        pb = ProjectBudget(tracker, max_cost=100.0)
        a, b = RunBudget(max_cost=100.0), RunBudget(max_cost=100.0)
        a.add_cost(0.4)
        b.add_cost(0.6)
        pb.add_run(a)
        pb.add_run(b)
        assert pb.get_cost() == pytest.approx(1.0)

    def test_check_passes_under_ceiling(self):
        tracker = BudgetTracker(global_budget=100.0)
        pb = ProjectBudget(tracker, max_cost=5.0)
        rb = RunBudget(max_cost=100.0)
        rb.add_cost(1.0)
        pb.add_run(rb)
        pb.check()  # must not raise

    def test_over_project_ceiling_raises(self):
        tracker = BudgetTracker(global_budget=100.0)
        pb = ProjectBudget(tracker, max_cost=1.0)
        rb = RunBudget(max_cost=100.0)
        rb.add_cost(1.0)                      # reaching the ceiling trips it (>=)
        pb.add_run(rb)
        with pytest.raises(OverBudget):
            pb.check()

    def test_global_exhaustion_raises_even_under_project_ceiling(self):
        tracker = BudgetTracker(global_budget=1.0)
        pb = ProjectBudget(tracker, max_cost=100.0)   # roomy project ceiling
        rb = RunBudget(max_cost=100.0)
        rb.add_cost(1.0)                              # but the global cap is hit
        pb.add_run(rb)
        tracker.add_project(pb)
        with pytest.raises(OverBudget):
            pb.check()


class TestBudgetTracker:
    def _tracker_with_spend(self, spend, global_budget=1.0):
        tracker = BudgetTracker(global_budget=global_budget)
        pb = ProjectBudget(tracker, max_cost=global_budget)
        rb = RunBudget(max_cost=global_budget)
        rb.add_cost(spend)
        pb.add_run(rb)
        tracker.add_project(pb)
        return tracker

    def test_budget_used_and_remaining(self):
        tracker = self._tracker_with_spend(0.3, global_budget=1.0)
        assert tracker.budget_used() == pytest.approx(0.3)
        assert tracker.budget_remaining() == pytest.approx(0.7)

    def test_remaining_floors_at_zero(self):
        tracker = self._tracker_with_spend(1.5, global_budget=1.0)
        assert tracker.budget_remaining() == 0

    def test_budget_exceeded(self):
        assert self._tracker_with_spend(0.5).budget_exceeded() is False
        assert self._tracker_with_spend(1.0).budget_exceeded() is True   # >=

    def test_request_project_rejects_when_exceeded(self):
        tracker = self._tracker_with_spend(1.0)
        with pytest.raises(OverBudget):
            tracker.request_project(ProjectBudget(tracker))

    def test_request_project_allowed_with_headroom(self):
        tracker = self._tracker_with_spend(0.5)
        before = len(tracker.project_budgets)
        tracker.request_project(ProjectBudget(tracker))
        assert len(tracker.project_budgets) == before + 1

    def test_set_budget_and_quota_ledger(self):
        tracker = BudgetTracker(global_budget=1.0)
        tracker.set_budget(5.0)
        tracker.update_quota(10.0, 8.0)
        d = tracker.to_dict()
        assert d["global_budget"] == 5.0
        assert d["api_quota_prior"] == 10.0
        assert d["api_quota_remaining"] == 8.0
        assert d["projects"] == 0


class TestApprovedComputeAllowance:
    """The wall-time ceiling is a runaway backstop, not a cap on approved compute.

    Run 6e9c32ae asked for 4 hours, was approved, computed its bulk modulus in 34
    minutes, and was then failed at INTERPRET by the 30-minute default -- on a
    Slurm job that had COMPLETED with exit 0. The result was already paid for.
    """

    def test_extending_raises_the_ceiling(self):
        rb = RunBudget(wall_time_minutes=30)
        assert rb.wall_time == 30 * 60
        rb.extend_wall_time(4 * 60)
        assert rb.wall_time == (30 + 240) * 60

    def test_a_long_job_no_longer_trips_the_default(self):
        """34 minutes elapsed, 30-minute default, 4-hour approved allocation."""
        rb = RunBudget(max_cost=10.0, max_iterations=100, wall_time_minutes=30)
        rb.start_time -= 34 * 60                     # pretend 34 minutes passed
        with pytest.raises(OverMaxWallTime):
            rb.check()
        rb.extend_wall_time(4 * 60)
        rb.check()                                   # must not raise

    def test_it_never_lowers_the_ceiling(self):
        rb = RunBudget(wall_time_minutes=30)
        for bad in (0, -60, None, "", "abc", float("nan")):
            rb.extend_wall_time(bad)
            assert rb.wall_time >= 30 * 60

    def test_a_runaway_pipeline_is_still_bounded(self):
        """Only the approved allocation is added -- the limit is not removed."""
        rb = RunBudget(max_cost=10.0, max_iterations=100, wall_time_minutes=30)
        rb.extend_wall_time(60)
        rb.start_time -= (30 + 60 + 1) * 60
        with pytest.raises(OverMaxWallTime):
            rb.check()

    def test_the_snapshot_reports_the_extended_limit(self):
        rb = RunBudget(wall_time_minutes=30)
        rb.extend_wall_time(90)
        assert rb.to_dict()["wall_time_limit_seconds"] == (30 + 90) * 60


class TestOrchestratorAppliesTheAllowance:
    """_allow_approved_compute reads the plan and extends once."""

    def _harness(self, plan):
        from orchestrator import Orchestrator

        class Sm:
            @staticmethod
            def _load_artifact(name):
                return plan if name == "execution_plan" else None

        class Stub:
            _allow_approved_compute = Orchestrator._allow_approved_compute

            def __init__(self):
                self.run_budget = RunBudget(wall_time_minutes=30)
                self.sm = Sm()
                self._compute_allowance_minutes = None
                self.published = []

            def _publish(self, event, payload):
                self.published.append((event, payload))

        return Stub()

    def test_it_extends_from_the_plans_walltime(self):
        s = self._harness({"slurm_request": {"max_time": 4.0}})
        s._allow_approved_compute()
        assert s.run_budget.wall_time == (30 + 240) * 60
        assert s._compute_allowance_minutes == 240
        assert s.published[0][0] == "run.budget_extended"

    def test_it_applies_only_once(self):
        s = self._harness({"slurm_request": {"max_time": 4.0}})
        for _ in range(5):
            s._allow_approved_compute()
        assert s.run_budget.wall_time == (30 + 240) * 60
        assert len(s.published) == 1

    @pytest.mark.parametrize("plan", [
        None, {}, {"slurm_request": None}, {"slurm_request": {}},
        {"slurm_request": {"max_time": 0}},
        {"slurm_request": {"max_time": -1}},
        {"slurm_request": {"max_time": "4"}},      # wrong type, not trusted
    ])
    def test_no_usable_walltime_changes_nothing(self, plan):
        """Before PLAN there is no allocation to allow for; stay at the default."""
        s = self._harness(plan)
        s._allow_approved_compute()
        assert s.run_budget.wall_time == 30 * 60
        assert s._compute_allowance_minutes is None
        assert s.published == []
