from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Union
import json

@dataclass
class Event:
    event_type: str
    timestamp: datetime | str
    source_agent_id: str
    payload: str
    trace_id: str

    def __post_init__(self):
        if(isinstance(self.timestamp, str)):
            self.timestamp = datetime.strptime(self.timestamp, "%Y-%m-%dT%H:%M:%S.%f")
        elif(not isinstance(self.timestamp, datetime)):
            raise TypeError("timestamp must be of type datetime or string")

