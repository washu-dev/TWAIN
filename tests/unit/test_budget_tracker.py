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
