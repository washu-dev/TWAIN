from enum import Enum, auto
from dataclasses import dataclass, field

from pygments.lexer import default

from intake.intent_spec import IntentSpec
from result_interpreter.result_package import ResultPackage


class State(Enum):
     INTAKE = auto(); CLARIFY = auto(); DECOMPOSE = auto(); DISCOVER = auto()
     PLAN = auto(); BUILD = auto(); EXECUTE = auto(); INTERPRET = auto()
     VALIDATE = auto(); ACCEPT = auto(); CORRECT = auto(); REPLAN = auto()
     TERMINATE = auto()

@dataclass
class Context:
    clarified: bool = False
    plan_approved: bool = False
    execution_status: bool | None = None
    validation_result: str | None = None
    current_state: State | None = None

    artifacts: dict[str, str] = field(default_factory=dict)
    #{"intent_spec" : /Users/danielschwammlein/git/TWAIN/schemas/examples/intent_spec_example.json"}


GUARDS: dict[tuple[State,State], "Callable[[Context], bool]"] = {
    (State.INTAKE, State.CLARIFY) : lambda c : True,
    (State.CLARIFY, State.DECOMPOSE) : lambda c: c.clarified,
    (State.DECOMPOSE, State.DISCOVER) : lambda c : True,
    (State.DISCOVER, State.PLAN) : lambda c : True,
    (State.PLAN, State.BUILD) : lambda c: c.plan_approved,
    (State.BUILD, State.EXECUTE) : lambda c : True,
    (State.EXECUTE, State.INTERPRET) : lambda c : c.execution_status,
    (State.INTERPRET, State.VALIDATE) : lambda c : True,
    (State.VALIDATE, State.ACCEPT) : lambda c : c.validation_result == "accepted",
    (State.VALIDATE, State.REPLAN) : lambda c : c.validation_result == "rejected",
    (State.VALIDATE, State.CORRECT) : lambda c : c.validation_result == "needs_review",
}

class InvalidTransition(Exception):
    pass

class GuardsBroken(Exception):
    pass

class state_machine:
    def __init__(self):
        self.iterations = 0
        self.plan_path = ""
        self.intent_spec_path = ""
        self.approvedPlan = False
        self.MAX_ITERATIONS = 10
        self.context = Context()



    def run(self):
        while True:
            handler = getattr(self, self.context.current_state.name.lower())
            next_state = handler()

            key = (self.context.current_state, next_state)
            if(key not in GUARDS):
                raise InvalidTransition("Invalid transition, inter node travel must be explicitly defined in GUARDS")
            func = GUARDS[key]
            permission = func(self.context)

            if(not permission):
                raise GuardsBroken("Transition not allowed, incomplete context")
            else:
                self.context.current_state = self.newState(next_state)
                

            if(next_state == State.TERMINATE):
                break

        return 0

    def newState(self, next_state: State):
        self.context.current_state = next_state
        self.iterations = 0



    def approveState(self):
        self.completed.add(self.context.current_state)

    def canTransition(self):
        if(self.context.current_state in self.completed):
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