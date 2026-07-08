"""Acceptance verdict for a cross-validation result (Story 6.2).

Turns the quantitative comparison from ``baseline_validator`` into an objective,
explainable verdict:

  * **accepted**     -- every molecule agrees within ``accept_below`` relative error
  * **needs_review** -- every molecule agrees within ``review_below`` (marginal)
  * **rejected**     -- at least one molecule exceeds ``review_below`` (poor agreement)

Thresholds default to 15% / 30% and are configurable so a researcher can tighten
or loosen the bar. A comparison whose literature value is 0 has no relative error;
it can never clear ACCEPT on its own and caps the verdict at NEEDS_REVIEW. If no
predictions matched a baseline, the verdict is NEEDS_REVIEW (nothing to judge).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from cross_validation.baseline_validator import CrossValidationResult, PairComparison

# verdict severities, worst-first for aggregation
ACCEPTED = "accepted"
NEEDS_REVIEW = "needs_review"
REJECTED = "rejected"
_SEVERITY = {ACCEPTED: 0, NEEDS_REVIEW: 1, REJECTED: 2}


@dataclass
class AcceptanceThresholds:
    accept_below: float = 0.15  # relative error < 15% -> ACCEPT
    review_below: float = 0.30  # relative error < 30% -> NEEDS_REVIEW

    def __post_init__(self):
        if not 0 < self.accept_below <= self.review_below:
            raise ValueError("require 0 < accept_below <= review_below")


@dataclass
class Verdict:
    status: str
    rationale: str


def _verdict_for(comparison: PairComparison, thresholds: AcceptanceThresholds) -> str:
    rel = comparison.relative_error
    if rel is None:
        # literature value is 0: relative error undefined -> can't confirm ACCEPT
        return NEEDS_REVIEW
    if rel < thresholds.accept_below:
        return ACCEPTED
    if rel < thresholds.review_below:
        return NEEDS_REVIEW
    return REJECTED


def judge(
    result: CrossValidationResult,
    thresholds: AcceptanceThresholds = None,
) -> Verdict:
    """Grade a :class:`CrossValidationResult`. Overall status is the worst
    per-molecule verdict; the rationale names the drivers."""
    thresholds = thresholds or AcceptanceThresholds()

    if not result.comparisons:
        return Verdict(
            NEEDS_REVIEW,
            "No predictions matched a literature baseline, so agreement could not be judged.",
        )

    per: List[tuple] = [(c, _verdict_for(c, thresholds)) for c in result.comparisons]
    worst = max((v for _, v in per), key=lambda s: _SEVERITY[s])

    ap = f"{thresholds.accept_below * 100:.0f}%"
    rp = f"{thresholds.review_below * 100:.0f}%"
    offenders = [c for c, v in per if v != ACCEPTED]

    if worst == ACCEPTED:
        rationale = f"All {len(per)} molecule(s) agree within {ap} relative error."
    else:
        detail = "; ".join(
            f"{c.molecule}/{c.property} "
            + ("rel err n/a (literature 0)" if c.relative_error is None
               else f"rel err {c.relative_error * 100:.1f}%")
            for c in offenders
        )
        if worst == REJECTED:
            rationale = f"Rejected: at least one molecule exceeds {rp} relative error. {detail}"
        else:
            rationale = f"Needs review: agreement is marginal (within {rp} but not {ap}). {detail}"

    return Verdict(worst, rationale)


def cross_validate(
    predictions: Sequence,
    db=None,
    thresholds: AcceptanceThresholds = None,
    *,
    report_id: str = "",
    timestamp: str = "",
):
    """Convenience end-to-end: compare predictions to baselines, grade them, and
    return ``(CrossValidationResult, Verdict, ValidationReport)``.

    ``db`` defaults to the shipped ``configs/baselines.json`` snapshot.
    """
    from cross_validation.baseline_validator import BaselineDB, compare

    if db is None:
        db = BaselineDB.load()
    result = compare(predictions, db)
    verdict = judge(result, thresholds)
    report = result.to_validation_report(
        verdict.status, report_id=report_id, timestamp=timestamp
    )
    return result, verdict, report
