from enum import Enum, auto
from dataclasses import dataclass, field

from pygments.lexer import default

from intake.intent_spec import IntentSpec
from result_interpreter.result_package import ResultPackage
from states import State, Context, GuardsBroken, InvalidTransition
from crash_recovery import DataStorage

GUARDS: dict[tuple[State, State], "Callable[[Context], bool]"] = {
    (State.INTAKE, State.CLARIFY): lambda c: True,
    (State.CLARIFY, State.DECOMPOSE): lambda c: c.clarified,
    (State.DECOMPOSE, State.DISCOVER): lambda c: True,
    (State.DISCOVER, State.PLAN): lambda c: True,
    (State.PLAN, State.BUILD): lambda c: c.plan_approved,
    (State.BUILD, State.EXECUTE): lambda c: c.plan_approved,
    (State.EXECUTE, State.INTERPRET): lambda c: c.execution_status,
    (State.INTERPRET, State.VALIDATE): lambda c: True,
    (State.VALIDATE, State.ACCEPT): lambda c: c.validation_result == "accepted",
    (State.VALIDATE, State.REPLAN): lambda c: c.validation_result == "rejected",
    (State.VALIDATE, State.CORRECT): lambda c: c.validation_result == "needs_review",
    (State.ACCEPT, State.TERMINATE): lambda c: True,
    (State.REPLAN, State.PLAN): lambda c: True,
    (State.CORRECT, State.BUILD): lambda c: c.plan_approved,

}

class StateMachine:
    def __init__(self, data_path: str = ""):
        self.iterations = 0
        self.approvedPlan = False
        self.MAX_ITERATIONS = 10
        self.context = Context()
        self.current_state = State.INTAKE
        self.storage = DataStorage(data_path)
        self.recoveryData = self.storage.load()
        if not self.recoveryData:
            self.context = Context()
            self.current_state = State.INTAKE
        else:
            self.current_state, self.context = self.recoveryData

    def run(self):
        while True:
            handler = getattr(self, self.current_state.name.lower())
            next_state = handler()

            key = (self.current_state, next_state)
            if(key not in GUARDS):
                raise InvalidTransition("Invalid transition, inter node travel must be explicitly defined in GUARDS")
            func = GUARDS[key]
            permission = func(self.context)

            if(not permission):
                raise GuardsBroken("Transition not allowed, incomplete context")
            else:
                self.newState(next_state)
                

            if(next_state == State.TERMINATE):
                break

        return 0

    def newState(self, next_state: State):
        self.current_state = next_state
        self.storage.commit(self.current_state,self.context)
        self.iterations = 0

    def intake(self) -> State:
        return State.CLARIFY
    def clarify(self) -> State:
        return State.DECOMPOSE
    def decompose(self) -> State:
        return State.DISCOVER
    def discover(self) -> State:
        return State.PLAN
    def plan(self) -> State:
        return State.BUILD
    def build(self) -> State:
        return State.EXECUTE
    def execute(self) -> State:
        return State.INTERPRET
    def interpret(self) -> State:
        return State.VALIDATE
    def validate(self) -> State:
        return State.ACCEPT
    def accept(self) -> State:
        return State.TERMINATE
    def correct(self) -> State:
        return State.BUILD
    def replan(self) -> State:
        return State.PLAN