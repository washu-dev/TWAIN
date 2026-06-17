from enum import Enum, auto


class State(Enum):
     INTAKE = auto(); CLARIFY = auto(); DECOMPOSE = auto(); DISCOVER = auto()
     PLAN = auto(); BUILD = auto(); EXECUTE = auto(); INTERPRET = auto()
     VALIDATE = auto(); ACCEPT = auto(); CORRECT = auto(); REPLAN = auto()

class state_machine:
    def __init__(self):
        self.current_state = State.INTAKE
        self.completed = set[State] = set()

    def run(self):
        while True:
            handler = getattr(self, self.current_state.name.lower())
            self.current_state = handler()

    def approveState(self, val):
        if(val == True):
            self.completed.add(self.current_state)

    def intake(self) -> State:
        pass
    def clarify(self) -> State:
        pass
    def decompose(self) -> State:
        pass
    def discover(self) -> State:
        pass
    def plan(self) -> State:
        pass
    def build(self) -> State:
        pass
    def execute(self) -> State:
        pass
    def interpret(self) -> State:
        pass
    def validate(self) -> State:
        pass
    def accept(self) -> State:
        pass
    def correct(self) -> State:
        pass
    def replan(self) -> State:
        pass