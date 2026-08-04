from dataclasses import dataclass, field
from typing import List, Optional, Union


@dataclass
class SelectedMethod:
    tool_name: str
    tool_version: float
    # A method is a *toolset*, not a single tool. ``tool_name`` is the primary
    # library (discovery's #1, kept for the original two-field contract);
    # ``libraries`` is the full ordered set the run uses together -- e.g.
    # ["Pymatgen", "ASE"] when Pymatgen is primary but the DFT calculator plugs
    # into ASE. A python ``calculator`` (e.g. "GPAW") may provide the physics;
    # ``calculator_library`` names the library in the toolset it is driven
    # through. All optional so plain single-library runs are unchanged.
    calculator: Optional[str] = None
    calculator_import: Optional[str] = None
    calculator_library: Optional[str] = None
    libraries: List[str] = field(default_factory=list)
    def __post_init__(self):
        if self.tool_name is None or type(self.tool_name) is not str:
            raise ValueError("SelectedMethod tool_name must be of type str")
        if self.tool_version is None or type(self.tool_version) not in (int, float):
            raise ValueError("SelectedMethod tool_version must be of type number")
        for attr in ("calculator", "calculator_import", "calculator_library"):
            val = getattr(self, attr)
            if val is not None and type(val) is not str:
                raise ValueError(f"SelectedMethod {attr} must be a str when provided")
        if type(self.libraries) is not list or any(type(x) is not str for x in self.libraries):
            raise ValueError("SelectedMethod libraries must be a list of str")


@dataclass
class ComputeEstimate:
    cpu_hours: float
    def __post_init__(self):
        if self.cpu_hours is None or type(self.cpu_hours) not in (int, float):
            raise ValueError("ComputeEstimate cpu_hours must be of type number")


@dataclass
class SlurmRequest:
    cpu_count: int
    gpu_count: int
    max_time: float
    ram: int
    def __post_init__(self):
        if self.cpu_count is None or type(self.cpu_count) is not int:
            raise ValueError("SlurmRequest cpu_count must be of type int")
        if self.gpu_count is None or type(self.gpu_count) is not int:
            raise ValueError("SlurmRequest gpu_count must be of type int")
        if self.max_time is None or type(self.max_time) not in (int, float):
            raise ValueError("SlurmRequest max_time must be of type number")
        if self.ram is None or type(self.ram) is not int:
            raise ValueError("SlurmRequest ram must be of type int")


@dataclass
class CostEstimate:
    min_tokens: int
    min_cost: float
    def __post_init__(self):
        if self.min_tokens is None or type(self.min_tokens) is not int:
            raise ValueError("CostEstimate min_tokens must be of type int")
        if self.min_cost is None or type(self.min_cost) not in (int, float):
            raise ValueError("CostEstimate min_cost must be of type number (USD)")


@dataclass
class ExecutionPlanMetadata:
    timestamp: str
    goal_id: str
    candidate_rank: int
    def __post_init__(self):
        if self.timestamp is None or type(self.timestamp) is not str:
            raise ValueError("ExecutionPlanMetadata timestamp must be of type str (date-time)")
        if self.goal_id is None or type(self.goal_id) is not str:
            raise ValueError("ExecutionPlanMetadata goal_id must be of type str")
        if self.candidate_rank is None or type(self.candidate_rank) is not int:
            raise ValueError("ExecutionPlanMetadata candidate_rank must be of type int")


@dataclass
class AcceptanceMetric:
    metric_name: str
    target_value: float
    tolerance: float
    def __post_init__(self):
        if self.metric_name is None or type(self.metric_name) is not str:
            raise ValueError("AcceptanceMetric metric_name must be of type str")
        if self.target_value is None or type(self.target_value) not in (int, float):
            raise ValueError("AcceptanceMetric target_value must be of type number")
        if self.tolerance is None or type(self.tolerance) not in (int, float):
            raise ValueError("AcceptanceMetric tolerance must be of type number")


@dataclass
class ExecutionPlan:
    selected_method: Union[SelectedMethod, dict]
    compute_estimate: Union[ComputeEstimate, dict]
    slurm_request: Union[SlurmRequest, dict]
    cost_estimate: Union[CostEstimate, dict]
    metadata: Union[ExecutionPlanMetadata, dict]
    acceptance_metrics: List[Union[AcceptanceMetric, dict]] = field(default_factory=list)
    safety_notes: List[str] = field(default_factory=list)
    # The concrete system the plan acts on (formula/name/SMILES, from the
    # IntentSpec's system_descriptors) and the canonical property being computed
    # (e.g. "band_gap"). Optional so existing plans/tests are unaffected; the code
    # builder uses them to generate a script for *this* material instead of a
    # hard-coded sample.
    target_system: Optional[dict] = None
    requested_property: Optional[str] = None
    # Libraries this plan WANTED but could not use, because they are not in the
    # preset install set -- each one recorded and filed as a 'LibraryAddition'
    # GitHub issue (see method_discovery.library_requests). The plan itself always
    # uses installed libraries only; this is the audit trail of what was asked for,
    # mirrored into safety_notes for the researcher. Optional, so existing plans
    # and tests are unaffected.
    library_requests: List[dict] = field(default_factory=list)
    # A plain-language description of what this run will do -- the property, the
    # concrete system, the toolset, and the reasoning behind the approach. Built
    # at plan time so the approval gate shows *what will happen*, not just which
    # tools are used. Optional so existing plans/tests are unaffected.
    summary: Optional[str] = None
    # Why each suggested Slurm figure is what it is, keyed by field name. The
    # approval card shows these so the researcher can see the numbers are TWAIN's
    # suggestion and on what basis, rather than guessing whether they are a hard
    # requirement. Optional so existing plans/tests are unaffected.
    slurm_rationale: Optional[dict] = None

    def __post_init__(self):
        if self.summary is not None and type(self.summary) is not str:
            raise ValueError("summary must be a str when provided")
        if self.slurm_rationale is not None and type(self.slurm_rationale) is not dict:
            raise ValueError("slurm_rationale must be a dict when provided")
        if type(self.selected_method) is dict:
            self.selected_method = SelectedMethod(**self.selected_method)
        elif type(self.selected_method) is not SelectedMethod:
            raise ValueError("selected_method must be of type dict or SelectedMethod")

        if type(self.compute_estimate) is dict:
            self.compute_estimate = ComputeEstimate(**self.compute_estimate)
        elif type(self.compute_estimate) is not ComputeEstimate:
            raise ValueError("compute_estimate must be of type dict or ComputeEstimate")

        if type(self.slurm_request) is dict:
            self.slurm_request = SlurmRequest(**self.slurm_request)
        elif type(self.slurm_request) is not SlurmRequest:
            raise ValueError("slurm_request must be of type dict or SlurmRequest")

        if type(self.cost_estimate) is dict:
            self.cost_estimate = CostEstimate(**self.cost_estimate)
        elif type(self.cost_estimate) is not CostEstimate:
            raise ValueError("cost_estimate must be of type dict or CostEstimate")

        if type(self.metadata) is dict:
            self.metadata = ExecutionPlanMetadata(**self.metadata)
        elif type(self.metadata) is not ExecutionPlanMetadata:
            raise ValueError("metadata must be of type dict or ExecutionPlanMetadata")

        _AM_FIELDS = {f.name for f in AcceptanceMetric.__dataclass_fields__.values()}
        validated_acceptance_metrics = []
        for metric in self.acceptance_metrics:
            if type(metric) is dict:
                validated_acceptance_metrics.append(
                    AcceptanceMetric(**{k: v for k, v in metric.items() if k in _AM_FIELDS}))
            elif type(metric) is AcceptanceMetric:
                validated_acceptance_metrics.append(metric)
            else:
                raise ValueError("acceptance_metrics items must be of type dict or AcceptanceMetric")
        self.acceptance_metrics = validated_acceptance_metrics

        if type(self.safety_notes) is not list or any(type(note) is not str for note in self.safety_notes):
            raise ValueError("safety_notes must be a list of str")

        if type(self.library_requests) is not list or any(
                type(req) is not dict for req in self.library_requests):
            raise ValueError("library_requests must be a list of dict")


if __name__ == "__main__":
    raw_json_data = {
        "selected_method": {
            "tool_name": "VASP",
            "tool_version": 6.3
        },
        "compute_estimate": {
            "cpu_hours": 128.0
        },
        "slurm_request": {
            "cpu_count": 32,
            "gpu_count": 4,
            "max_time": 24.0,
            "ram": 64
        },
        "cost_estimate": {
            "min_tokens": 1500,
            "min_cost": 12.50
        },
        "metadata": {
            "timestamp": "2026-06-15T12:00:00Z",
            "goal_id": "goal-001",
            "candidate_rank": 1
        },
        "acceptance_metrics": [
            {"metric_name": "energy", "target_value": -5.2, "tolerance": 0.1}
        ],
        "safety_notes": ["Verify SLURM partition limits before submission"]
    }

    # The __post_init__ automatically hydrates the nested structures
    plan = ExecutionPlan(**raw_json_data)

    # Downstream modules can now use clean object dot-notation:
    print(plan.selected_method.tool_name)  # Output: VASP
    print(type(plan))  # Output: <class '__main__.ExecutionPlan'>
