import json, os, tempfile
from states import State, Context
import dataclasses

class DataStorage:
    def __init__(self, save_data_path : str):
        self.path = save_data_path
    def commit(self, current_state: State, context: Context):
        data = {"current_state": current_state.name, "context": dataclasses.asdict(context)}
        fd, tmp = tempfile.mkstemp(dir = os.path.dirname(self.path))
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, self.path)

    def load(self) -> tuple[State, Context] | None:
        if not os.path.exists(self.path):
            return None
        with open(self.path, "r") as f:
            data = json.load(f)
        return State[data["current_state"]], Context(**data["context"])
