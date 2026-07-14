import doctest
from dataclasses import dataclass,field
from enum import Enum
from typing import List, Dict, Optional, Union

class Domain(str, Enum):
    MATERIALS = "materials"
    QUANTUM = "quantum"

# Allowed system-representation discriminators. A discrete molecule is described
# by a SMILES; a periodic solid (bulk crystal or a surface/slab of one) is
# described by a formula + polymorph/structure -- SMILES cannot encode a
# periodic lattice, so the two representations are kept distinct.
SYSTEM_KINDS = ("molecule", "crystal", "surface")

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
class Crystal:
    """A periodic solid (bulk crystal or surface/slab).

    Only ``formula`` is required; the remaining fields pin the polymorph and the
    structure source when they are known. ``phase`` names the polymorph (rutile
    vs anatase TiO2), and exactly one of ``mp_id`` / ``cif`` / ``space_group``
    (+ ``crystal_system``) is enough to resolve an unambiguous structure. These
    field names mirror what the code-configuration builder already reads.
    """
    formula: str
    name: Optional[str] = None
    phase: Optional[str] = None
    crystal_system: Optional[str] = None
    space_group: Optional[str] = None
    space_group_number: Optional[int] = None
    mp_id: Optional[str] = None
    cif: Optional[str] = None
    def __post_init__(self):
        if not self.formula or type(self.formula) is not str:
            raise ValueError("Crystal formula must be a non-empty str")

@dataclass
class SystemDescriptors:
    """The physical system, described as EITHER a molecule OR a crystal.

    ``formula`` is always present. Molecular systems carry a ``molecule``
    (SMILES); periodic solids carry a ``crystal``. ``kind`` is the explicit
    discriminator -- inferred from whichever sub-object is present when omitted.
    """
    formula: str
    molecule: Optional[Union[Molecule, dict]] = None
    crystal: Optional[Union[Crystal, dict]] = None
    kind: Optional[str] = None
    def __post_init__(self):
        if self.formula is None or type(self.formula) is not str:
            raise ValueError("Formula must be of type str")
        if self.molecule is not None:
            if type(self.molecule) is dict:
                self.molecule = Molecule(**self.molecule)
            elif type(self.molecule) is not Molecule:
                raise ValueError("Molecule must be of type Molecule or dict")
        if self.crystal is not None:
            if type(self.crystal) is dict:
                self.crystal = Crystal(**self.crystal)
            elif type(self.crystal) is not Crystal:
                raise ValueError("Crystal must be of type Crystal or dict")
        if self.molecule is None and self.crystal is None:
            raise ValueError("System descriptors must include a molecule or a crystal")
        # Infer the discriminator from the populated sub-object, or validate an
        # explicit one. A crystal wins if somehow both are supplied.
        if self.kind is None:
            self.kind = "crystal" if self.crystal is not None else "molecule"
        elif self.kind not in SYSTEM_KINDS:
            raise ValueError(f"kind must be one of {SYSTEM_KINDS}")

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