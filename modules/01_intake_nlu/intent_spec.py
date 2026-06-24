import doctest
from dataclasses import dataclass,field
from enum import Enum
from typing import List, Dict, Union

class Domain(str, Enum):
    MATERIALS = "materials"
    QUANTUM = "quantum"

@dataclass
class Molecule:
    name:str
    SMILES:str
    def __post_init__(self):
        if self.name is None or type(self.name) is not str:
            raise ValueError("Molecule name must be of type str")
        if self.SMILES is None or type(self.SMILES) is not str:
            raise ValueError("Molecule SMILES must be of type str")

@dataclass
class SystemDescriptors:
    molecule: Union[Molecule,dict]
    formula:str
    def __post_init__(self):
        if self.formula is None or type(self.formula) is not str:
            raise ValueError("Formula must be of type str")
        if type(self.molecule) is dict:
            self.molecule = Molecule(**self.molecule)
        elif type(self.molecule) is not Molecule:
            raise ValueError("Molecule must be of type Molecule or dict")

@dataclass
class AcceptanceCriterion:
    metric_name:str
    target_value:float
    tolerance:float

@dataclass
class IntentSpecMetadata:
    confidence_scores: Dict[str,float]
    ambiguity: bool = True
    def __post_init__(self):
        if self.confidence_scores is None or type(self.confidence_scores) is not dict:
            raise ValueError("Confidence scores must be of type Dict")
        for value in self.confidence_scores.values():
            if(value < 0 or value > 1):
                raise ValueError("Confidence scores must be between 0 and 1")


@dataclass
class IntentSpec:
    objective:str
    domain: Union[str, Domain]
    system_descriptors: Union[dict, SystemDescriptors]
    acceptance_criteria: List[Union[AcceptanceCriterion,dict]]
    metadata:Union[IntentSpecMetadata,dict]
    def __post_init__(self):
        if not self.objective or type(self.objective) is not str:
            raise ValueError("Objective must be of type str")
        if type(self.domain) is str:
            try:
                self.domain = Domain(self.domain)
            except ValueError:
                raise ValueError(f"Invalid domain string. Viable options: {[d.value for d in Domain]}")
        elif not isinstance(self.domain, Domain):
            raise ValueError("Domain must be either a Domain enum or a string corresponding to the domain")
        if self.system_descriptors and type(self.system_descriptors) is dict:
            self.system_descriptors = SystemDescriptors(**self.system_descriptors)
        if not self.system_descriptors or type(self.system_descriptors) is not SystemDescriptors:
            raise ValueError("System descriptors must be of type Dict or SystemDescriptors")
        validated_acceptance_criteria = []
        for criterion in self.acceptance_criteria:
            if type(criterion) is dict:
                validated_acceptance_criteria.append(AcceptanceCriterion(**criterion))
            elif type(criterion) is AcceptanceCriterion:
                validated_acceptance_criteria.append(criterion)
            else:
                raise ValueError("Invalid criterion type, must be a dict or AcceptanceCriterion object")

        self.acceptance_criteria = validated_acceptance_criteria

        if type(self.metadata) is dict:
            self.metadata = IntentSpecMetadata(**self.metadata)