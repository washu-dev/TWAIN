"""End-to-end self-correction reflection (Story 6.3).

Ties the three pieces together into one call:

    evidence --classify--> Diagnosis --strategy--> CorrectionPlan

The rerun controller (:mod:`rerun_controller`) then decides whether the plan is
worth executing. Kept separate so callers can diagnose, propose, and gate reruns
independently, but ``reflect`` covers the common path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from self_correction.failure_classifier import Diagnosis, RunEvidence, classify
from self_correction.strategies import CorrectionContext, propose_correction


@dataclass
class Reflection:
    diagnosis: Diagnosis
    correction_plan: Optional[Dict]  # None when the failure mode is UNKNOWN


def reflect(evidence: RunEvidence, context: CorrectionContext) -> Reflection:
    """Diagnose a failed run and propose a correction plan.

    ``context.iteration_count`` should reflect how many corrections have already
    been attempted so the emitted plan's metadata is accurate.
    """
    diagnosis = classify(evidence)
    plan = propose_correction(diagnosis, context)
    return Reflection(diagnosis=diagnosis, correction_plan=plan)
