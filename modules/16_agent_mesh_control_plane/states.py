from enum import Enum, auto
from dataclasses import dataclass, field

class State(Enum):
    INTAKE = auto();
    CLARIFY = auto();
    DECOMPOSE = auto();
    DISCOVER = auto()
    PLAN = auto();
    BUILD = auto();
    EXECUTE = auto();
    INTERPRET = auto()
    VALIDATE = auto();
    ACCEPT = auto();
    CORRECT = auto();
    REPLAN = auto()
    TERMINATE = auto()


@dataclass
class Context:
    clarified: bool = False
    plan_approved: bool = False
    execution_status: bool | None = None
    validation_result: str | None = None

    artifacts: dict[str, str] = field(default_factory=dict)
    # {"intent_spec" : "/Users/danielschwammlein/git/TWAIN/schemas/examples/intent_spec_example.json"}



class InvalidTransition(Exception):
    pass


class GuardsBroken(Exception):
    pass