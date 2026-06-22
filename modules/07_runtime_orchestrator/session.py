from dataclasses import dataclass
from plan_synthesizer import execution_plan
from .._16_agent_mesh_control_plane import states
@dataclass
class RunSession:
    idTag: str
    researchId: str
    executionState: execution_plan.ExecutionPlan
    executionState: states.State

class Session:
    pass
