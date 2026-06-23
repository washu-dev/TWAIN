import json
from dataclasses import dataclass, asdict
from plan_synthesizer import execution_plan
from .._16_agent_mesh_control_plane import states
from provenance_memory import event_log
import time
@dataclass
class RunSession:
    session_id: str
    research_id: str
    execution_plan: execution_plan.ExecutionPlan
    execution_state: states.State
    provenance_log: event_log.EventLog




class Session:
    def __init__(self, session_id: str = None):
        self.runSession = None
        self.lastSave = 0
        self.saveInterval = 30
        if session_id is None:
            self.runSession = RunSession() # FIGURE OUT HOW TO FILL IN
        else:
            with open(f"sessionLogs/{session_id}.json", "r") as f:
                data = json.load(f)
            self.runSession = RunSession(**data)

    def update(self):
        if(time.time() - self.lastSave) > self.saveInterval:
            self.saveOnDisk()



    def saveOnDisk(self):
        with open(f"sessionLogs/{self.runSession.session_id}.json", "w") as f:
            json.dump(asdict(self.runSession), f)


