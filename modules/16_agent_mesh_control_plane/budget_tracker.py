from dataclasses import dataclass
from typing import List
import time


class OverBudget(Exception):
    pass

class OverMaxIterations(Exception):
    pass

class OverMaxWallTime(Exception):
    pass
class RunBudget:
    def __init__(self, max_tokens = 10000, max_iterations = 5, wall_time = 30):
        self.max_tokens = max_tokens
        self.tokens = 0
        self.cost = 0
        self.max_iterations = max_iterations
        self.iterations = 0
        self.wall_time = wall_time * 60
        self.terminateTime = time.time() + wall_time
    def getCost(self):
        return self.cost



class ProjectBudget:
    def __init__(self, tracker, max_cost = 1):
        self.tracker = tracker
        self.max_cost = max_cost
        self.cost = 0
        self.runBudgets = [] # List of runBudget objects
    def getCost(self):
        self.cost = 0
        for RunBudget in self.runBudgets:
            self.cost += RunBudget.getCost()
        return self.cost

    def addRun(self, RunBudget):
        self.runBudgets.append(RunBudget)

    def request_iteration(self):
        if(self.getCost() > self.max_cost or self.tracker.budget_exceeded()):
            raise OverBudget()
        if(self.iterations >= self.max_iterations):
            raise OverMaxIterations()
        if(self.wall_time >= time.time() - self.start_time):
            raise OverMaxWallTime()
        return True


class Budget_Tracker:
    def __init__(self, globalBudget = 1):
        self.projectBudgets = [] # List of ProjectBudget objects
        self.globalBudget = 1
    def add_project(self, Project):
        self.projectBudgets.append(Project)
    def set_budget(self,budget):
        self.globalBudget = budgetB
    def request_project(self, project):
        if(self.budget_exceeded()):
            raise OverBudget()
        else:
            self.add_project(project)

    def budget_used(self):
        budgetUsed = 0
        for Project in self.projectBudgets:
            budgetUsed = budgetUsed + Project.getCost()
        return budgetUsed
    def budget_exceeded(self):
        return (self.budget_used() >= self.globalBudget)
    def budget_remaining(self):
        return max(self.globalBudget-self.budget_used(),0)
