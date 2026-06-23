"""Risk assessment for plan synthesis (Story 4.4).

Inspects the selected tool (a discovery RegistryEntry) and the requested task,
flags reproducibility/maturity/domain-fit concerns, and aggregates them into a
risk score in [0, 1]. The notes feed directly into an ExecutionPlan's
`safety_notes` so the researcher sees concerns before approving.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from method_discovery.registry_loader import Maturity, RegistryEntry

# Per-flag risk contributions (aggregated then clamped to 1.0).
_RISK_DEPRECATED = 0.4
_RISK_ALPHA = 0.25
_RISK_BETA = 0.1
_RISK_NO_REPRODUCIBILITY = 0.2
_RISK_UNVETTED_TIER = 0.2
_RISK_CAPABILITY_MISMATCH = 0.3
_RISK_RESTRICTIVE_LICENSE = 0.15


@dataclass
class RiskAssessment:
    score: float
    notes: List[str] = field(default_factory=list)

    def __post_init__(self):
        if type(self.score) not in (int, float) or not (0.0 <= self.score <= 1.0):
            raise ValueError("RiskAssessment score must be a number in [0, 1]")


class RiskAssessor:
    def assess(
        self,
        entry: RegistryEntry,
        requested_capability: Optional[str] = None,
    ) -> RiskAssessment:
        notes: List[str] = []
        risk = 0.0

        if entry.maturity == Maturity.DEPRECATED:
            notes.append(f"{entry.name} is deprecated/unmaintained.")
            risk += _RISK_DEPRECATED
        elif entry.maturity == Maturity.ALPHA:
            notes.append(f"{entry.name} is alpha-stage; APIs and results may be unstable.")
            risk += _RISK_ALPHA
        elif entry.maturity == Maturity.BETA:
            notes.append(f"{entry.name} is beta-stage.")
            risk += _RISK_BETA

        if not entry.has_paper and not entry.has_tests:
            notes.append(
                f"Low reproducibility evidence for {entry.name} (no paper and no test suite)."
            )
            risk += _RISK_NO_REPRODUCIBILITY

        if entry.trust_tier >= 3:
            notes.append(f"{entry.name} is an unvetted candidate (trust tier 3).")
            risk += _RISK_UNVETTED_TIER

        if entry.license_class.value == "restrictive":
            notes.append(f"{entry.name} has a restrictive/unknown license.")
            risk += _RISK_RESTRICTIVE_LICENSE

        if requested_capability is not None:
            tags = {t.lower() for t in entry.capability_tags}
            if requested_capability.lower() not in tags:
                notes.append(
                    f"{entry.name} is not tagged for the requested capability "
                    f"'{requested_capability}'; may be a poor fit."
                )
                risk += _RISK_CAPABILITY_MISMATCH

        return RiskAssessment(score=round(min(1.0, risk), 3), notes=notes)
