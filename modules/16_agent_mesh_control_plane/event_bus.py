from event import Event
from typing import Dict
from collections import deque
from schemas import schema_validator
from enum import Enum, auto
from dataclasses import dataclass

class Priority(Enum):
    DEFAULT = auto()
    CRITICAL = auto()

class EventBus:
    def __init__(self):
        self.handlers = {}
        self.schemas = {}
        self.schemaValidator = schema_validator.SchemaValidator()
        self.history = deque(maxlen=1000)
        self.batch = []
        self.log = open("event_bus.log", "a")
    def subscribe(self, event_type: str, schema: str, handler):
        if event_type not in self.handlers:
            self.handlers[event_type] = []
            self.schemas[event_type] = schema
        self.handlers[event_type].append(handler)
        if(self.schemas[event_type] != schema):
            return False
        return True

    def publish(self, event: Event, priority = Priority.DEFAULT):
        self.history.append(event)
        if(priority == Priority.CRITICAL):
            self.deliver(event)
        else:
            self.batch.append(event)

    def flush(self):
        for e in self.batch:
            self.deliver(e)
        self.batch.clear()

    def deliver(self, event: Event):
        schema = self.schemas.get(event.event_type)
        if schema:
            schemaValidated = self.schemaValidator.validateFromString(schema, event.payload)
        if (not schema or not schemaValidated):
            self.log.write(f"SCHEMA FAILURE \n ------------------------------- \n {event.event_type}: {event.payload}\n")
            return False
        self.log.write(f"SCHEMA SUCCESS \n -------------------------------- \n {event.event_type}: {event.payload}\n")
        for handler in self.handlers.get(event.event_type, []):
            handler(event)
        return True
    def __del__(self):
        self.log.flush()
        self.log.close()