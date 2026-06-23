from dataclasses import dataclass
from typing import Dict, Union

Number = (int, float)

ACCEPTANCE_STATUSES = ("accepted", "rejected", "needs_review")


@dataclass
class Comparison:
    literature_results: str
    agreement: float
    difference_analysis: str

    def __post_init__(self):
        if self.literature_results is None or not isinstance(self.literature_results, str):
            raise ValueError("literature_results must be a string")
        if self.agreement is None or not isinstance(self.agreement, Number) or isinstance(self.agreement, bool) or self.agreement < 0 or self.agreement > 1:
            raise ValueError("agreement must be a number between 0 and 1")
        if self.difference_analysis is None or not isinstance(self.difference_analysis, str):
            raise ValueError("difference_analysis must be a string")


@dataclass
class ValidationReportMetadata:
    ID: str
    timestamp: str

    def __post_init__(self):
        if self.ID is None or not isinstance(self.ID, str):
            raise ValueError("ID must be a string")
        if self.timestamp is None or not isinstance(self.timestamp, str):
            raise ValueError("timestamp must be a string")


@dataclass
class ValidationReport:
    comparison: Union[Comparison, Dict]
    acceptance_status: str
    metadata: Union[ValidationReportMetadata, Dict]

    def __post_init__(self):
        if isinstance(self.comparison, dict):
            self.comparison = Comparison(**self.comparison)
        elif self.comparison is None or not isinstance(self.comparison, Comparison):
            raise ValueError("comparison must be a Comparison object or a Dict")

        if self.acceptance_status not in ACCEPTANCE_STATUSES:
            raise ValueError(f"acceptance_status must be one of {ACCEPTANCE_STATUSES}")

        if isinstance(self.metadata, dict):
            self.metadata = ValidationReportMetadata(**self.metadata)
        elif self.metadata is None or not isinstance(self.metadata, ValidationReportMetadata):
            raise ValueError("metadata must be a ValidationReportMetadata object or a Dict")
