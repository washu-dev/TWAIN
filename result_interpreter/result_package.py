from dataclasses import dataclass, field
from typing import List, Dict, Optional, Union

Number = (int, float)


@dataclass
class Certainty:
    confidence_interval: List[float]
    expected: float
    mean_squared_error: float

    def __post_init__(self):
        if self.confidence_interval is None or not isinstance(self.confidence_interval, list) or len(self.confidence_interval) != 2:
            raise ValueError("Confidence interval must be a list of two elements")
        for element in self.confidence_interval:
            if not isinstance(element, Number) or element < 0 or element > 1:
                raise ValueError("Confidence interval values must be numbers between 0 and 1")
        if self.confidence_interval[0] > self.confidence_interval[1]:
            raise ValueError("Confidence interval must be in ascending numerical order")
        if self.expected is None or not isinstance(self.expected, Number) or self.expected < 0 or self.expected > 1:
            raise ValueError("Expected must be a number between 0 and 1")
        if self.mean_squared_error is None or not isinstance(self.mean_squared_error, Number):
            raise ValueError("Mean squared error must be a number")


@dataclass
class Result:
    experiment_name: str
    conclusion: str
    exit_code: int
    certainty: Union[Certainty, Dict]

    def __post_init__(self):
        if self.experiment_name is None or not isinstance(self.experiment_name, str):
            raise ValueError("Experiment name must be a string")
        if self.conclusion is None or not isinstance(self.conclusion, str):
            raise ValueError("Conclusion must be a string")
        if self.exit_code is None or not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool):
            raise ValueError("Exit code must be an integer")
        if isinstance(self.certainty, dict):
            self.certainty = Certainty(**self.certainty)
        elif self.certainty is None or not isinstance(self.certainty, Certainty):
            raise ValueError("Certainty must be a Certainty object or a Dict")


@dataclass
class OutputLog:
    name: str
    path_to_file: str

    def __post_init__(self):
        if self.name is None or not isinstance(self.name, str):
            raise ValueError("Name must be a string")
        if self.path_to_file is None or not isinstance(self.path_to_file, str):
            raise ValueError("Path to file must be a string")


@dataclass
class ResourceUsage:
    total_cost: Optional[float] = None
    tokens_used: Optional[int] = None
    token_cost: Optional[float] = None
    cpu_hours: Optional[float] = None
    gpu_hours: Optional[float] = None
    slurm_cost: Optional[float] = None
    total_time: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None

    def __post_init__(self):
        for name in ("total_cost", "token_cost", "cpu_hours", "gpu_hours", "slurm_cost"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Number) or isinstance(value, bool) or value < 0):
                raise ValueError(f"{name} must be a non-negative number")
        if self.tokens_used is not None and (not isinstance(self.tokens_used, int) or isinstance(self.tokens_used, bool) or self.tokens_used < 0):
            raise ValueError("tokens_used must be a non-negative integer")
        for name in ("total_time", "start_time", "end_time"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string")


@dataclass
class ToolUsed:
    name: str
    version: float

    def __post_init__(self):
        if self.name is None or not isinstance(self.name, str):
            raise ValueError("Tool name must be a string")
        if self.version is None or not isinstance(self.version, Number) or isinstance(self.version, bool):
            raise ValueError("Tool version must be a number")


@dataclass
class Metadata:
    timestamp: Optional[str] = None
    ID: Optional[str] = None
    tools_used: Optional[List[Union[ToolUsed, Dict]]] = None

    def __post_init__(self):
        if self.timestamp is not None and not isinstance(self.timestamp, str):
            raise ValueError("Timestamp must be a string")
        if self.ID is not None and not isinstance(self.ID, str):
            raise ValueError("ID must be a string")
        if self.tools_used is not None:
            if not isinstance(self.tools_used, list):
                raise ValueError("tools_used must be a list of ToolUsed objects or dicts")
            validated_tools: List[ToolUsed] = []
            for tool in self.tools_used:
                if isinstance(tool, dict):
                    validated_tools.append(ToolUsed(**tool))
                elif isinstance(tool, ToolUsed):
                    validated_tools.append(tool)
                else:
                    raise ValueError("tools_used must be a list of ToolUsed objects or dicts")
            self.tools_used = validated_tools


@dataclass
class ResultPackage:
    result: Union[Result, Dict]
    output: List[Union[OutputLog, Dict]]
    resource_usage: Optional[Union[ResourceUsage, Dict]] = None
    metadata: Optional[Union[Metadata, Dict]] = None

    def __post_init__(self):
        if isinstance(self.result, dict):
            self.result = Result(**self.result)
        elif self.result is None or not isinstance(self.result, Result):
            raise ValueError("Result must be a Result object or a Dict")

        if self.output is None or not isinstance(self.output, list):
            raise ValueError("Output must be a list of OutputLog objects or dicts")
        validated_outputs: List[OutputLog] = []
        for element in self.output:
            if isinstance(element, dict):
                validated_outputs.append(OutputLog(**element))
            elif isinstance(element, OutputLog):
                validated_outputs.append(element)
            else:
                raise ValueError("Output must be a list of OutputLog objects or dicts")
        self.output = validated_outputs

        if isinstance(self.resource_usage, dict):
            self.resource_usage = ResourceUsage(**self.resource_usage)
        elif self.resource_usage is not None and not isinstance(self.resource_usage, ResourceUsage):
            raise ValueError("Resource usage must be a ResourceUsage object or a Dict")

        if isinstance(self.metadata, dict):
            self.metadata = Metadata(**self.metadata)
        elif self.metadata is not None and not isinstance(self.metadata, Metadata):
            raise ValueError("Metadata must be a Metadata object or a Dict")
