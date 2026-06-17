from enum import Enum, auto
from dataclasses import dataclass

class State(Enum):
     INTAKE = auto(); CLARIFY = auto(); DECOMPOSE = auto(); DISCOVER = auto()
     PLAN = auto(); BUILD = auto(); EXECUTE = auto(); INTERPRET = auto()
     VALIDATE = auto(); ACCEPT = auto(); CORRECT = auto(); REPLAN = auto()
     TERMINATE = auto()

@dataclass
class Context:
    plan_approved: bool = False
    execution_status: bool | None = None
    validation_result: str | None = None

GUARDS: dict[tuple[State,State], "Callable[[Context], bool]"] = {
    (State.INTAKE, State.CLARIFY) : lambda : True,
    (State.CLARIFY, State.DECOMPOSE) : lambda : True,
    (State.DECOMPOSE, State.DISCOVER) : lambda : True,
    (State.DISCOVER, State.PLAN) : lambda : True,
    (State.PLAN, State.BUILD) : lambda c: c.plan_approved,
    (State.BUILD, State.EXECUTE) : lambda : True,
    (State.EXECUTE, State.INTERPRET) : lambda c : c.execution_status,
    (State.INTERPRET, State.VALIDATE) : lambda : True,
    (State.VALIDATE, State.ACCEPT) : lambda c : c.validation_result == "accepted",
    (State.VALIDATE, State.REPLAN) : lambda c : c.validation_result == "rejected",
    (State.VALIDATE, State.CORRECT) : lambda c : c.validation_result == "needs_review",

}

class state_machine:
    def __init__(self):
        self.current_state = State.INTAKE
        self.iterations = 0
        self.plan_path = ""
        self.intent_spec_path = ""
        self.approvedPlan = False
        self.MAX_ITERATIONS = 10
        self.context = Context()

    def run(self):
        while True:
            handler = getattr(self, self.current_state.name.lower())

            next_state = handler()
            if(next_state == State.TERMINATE):
                break
            if(self.canTransition()):
                self.newState(next_state)
            else:
                self.iterations+=1

        return 0

    def newState(self, next_state: State):
        self.current_state = next_state
        self.iterations = 0



    def approveState(self):
        self.completed.add(self.current_state)

    def canTransition(self):
        if(self.current_state in self.completed):
            return True
        else:
            return False

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
        pass
    def replan(self) -> State:
        pass