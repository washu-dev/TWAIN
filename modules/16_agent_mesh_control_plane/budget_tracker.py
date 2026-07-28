"""Cost / iteration / wall-time budgets for the control plane.

Three nested scopes guard a run against runaway cost or loops:

* :class:`RunBudget` -- one pipeline run: caps dollar cost, iteration count, and
  wall-clock time, raising the matching ``Over*`` exception when a limit trips.
* :class:`ProjectBudget` -- a group of runs sharing a cost ceiling.
* :class:`BudgetTracker` -- the top-level ledger across projects, enforcing a
  global spend cap and tracking remaining API quota.
"""
import time


class OverBudget(Exception):
    pass


class OverMaxIterations(Exception):
    pass


class OverMaxWallTime(Exception):
    pass


class RunBudget:
    def __init__(self, max_cost=1.0, max_iterations=50, wall_time_minutes=30):
        self.max_cost = max_cost
        self.cost = 0.0
        self.max_iterations = max_iterations
        self.iterations = 0
        self.wall_time = wall_time_minutes * 60
        self.start_time = time.time()

    def add_cost(self, amount):
        self.cost += amount

    def add_iteration(self):
        self.iterations += 1

    def get_cost(self):
        return self.cost

    def elapsed(self):
        return time.time() - self.start_time

    def check(self):
        if self.cost >= self.max_cost:
            raise OverBudget(
                f"Run cost ${self.cost:.4f} exceeds limit ${self.max_cost:.4f}"
            )
        if self.iterations >= self.max_iterations:
            raise OverMaxIterations(
                f"Run used {self.iterations}/{self.max_iterations} iterations"
            )
        if self.elapsed() >= self.wall_time:
            raise OverMaxWallTime(
                f"Run exceeded wall-time limit of {self.wall_time / 60:.0f} minutes"
            )

    def to_dict(self):
        return {
            "cost": self.cost,
            "max_cost": self.max_cost,
            "iterations": self.iterations,
            "max_iterations": self.max_iterations,
            "elapsed_seconds": round(self.elapsed(), 1),
            "wall_time_limit_seconds": self.wall_time,
        }


class ProjectBudget:
    def __init__(self, tracker, max_cost=1.0):
        self.tracker = tracker
        self.max_cost = max_cost
        self.run_budgets = []

    def get_cost(self):
        return sum(rb.get_cost() for rb in self.run_budgets)

    def add_run(self, run_budget):
        self.run_budgets.append(run_budget)

    def check(self):
        # ``>=`` matches RunBudget.check / BudgetTracker.budget_exceeded: reaching
        # the ceiling trips it, so a pre-step gate refuses to start more work.
        if self.get_cost() >= self.max_cost:
            raise OverBudget(
                f"Project cost ${self.get_cost():.4f} exceeds limit ${self.max_cost:.4f}"
            )
        if self.tracker.budget_exceeded():
            raise OverBudget("Global budget exceeded")


class BudgetTracker:
    def __init__(self, global_budget=1.0):
        self.project_budgets = []
        self.global_budget = global_budget
        self.api_quota_prior = None
        self.api_quota_remaining = None

    def add_project(self, project):
        self.project_budgets.append(project)

    def set_budget(self, budget):
        self.global_budget = budget

    def request_project(self, project):
        if self.budget_exceeded():
            raise OverBudget("Global budget exceeded; cannot start new project")
        self.add_project(project)

    def budget_used(self):
        return sum(p.get_cost() for p in self.project_budgets)

    def budget_exceeded(self):
        return self.budget_used() >= self.global_budget

    def budget_remaining(self):
        return max(self.global_budget - self.budget_used(), 0)

    def update_quota(self, quota_prior, quota_remaining):
        self.api_quota_prior = quota_prior
        self.api_quota_remaining = quota_remaining

    def to_dict(self):
        return {
            "global_budget": self.global_budget,
            "budget_used": round(self.budget_used(), 6),
            "budget_remaining": round(self.budget_remaining(), 6),
            "api_quota_prior": self.api_quota_prior,
            "api_quota_remaining": self.api_quota_remaining,
            "projects": len(self.project_budgets),
        }
