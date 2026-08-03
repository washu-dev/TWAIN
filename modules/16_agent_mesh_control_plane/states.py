from enum import Enum, auto
from dataclasses import dataclass, field

class State(Enum):
    INTAKE = auto();
    CLARIFY = auto();
    DECOMPOSE = auto();
    DISCOVER = auto()
    PLAN = auto();
    BUILD = auto();
    REPAIR = auto();
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

    # Fingerprint of the plan the researcher actually approved (see
    # StateMachine._plan_fingerprint). A re-plan that lands on the same method
    # and resources is still covered by that approval; one that changes them is
    # not, and must go back to the gate.
    approved_plan: str | None = None

    # Calculator whose heavy run the researcher already confirmed. A correction
    # or re-plan loop re-enters EXECUTE, and asking again for the same engine in
    # the same run is noise -- they already agreed to spend that compute.
    heavy_confirmed: str | None = None

    artifacts: dict[str, str] = field(default_factory=dict)
    # {"intent_spec": "<repo>/logs/artifacts/intent_spec_<run_id>.json"}



class InvalidTransition(Exception):
    pass


class GuardsBroken(Exception):
    pass