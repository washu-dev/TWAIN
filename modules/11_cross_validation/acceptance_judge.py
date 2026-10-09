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

from dataclasses import dataclass, field
from typing import Dict, List, Sequence, Tuple

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
    # Properties graded by ABSOLUTE difference, {property: (accept, review)}, in
    # the property's own unit -- a log-scale quantity like logS, where relative
    # error is meaningless (configs/baselines.json "agreement_bands").
    absolute: Dict[str, Tuple[float, float]] = field(default_factory=dict)

    def __post_init__(self):
        if not 0 < self.accept_below <= self.review_below:
            raise ValueError("require 0 < accept_below <= review_below")


@dataclass
class Verdict:
    status: str
    rationale: str


def _verdict_for(comparison: PairComparison, thresholds: AcceptanceThresholds) -> str:
    band = thresholds.absolute.get(comparison.property)
    if band:
        accept, review = band
        err = comparison.absolute_error
        return ACCEPTED if err < accept else NEEDS_REVIEW if err < review else REJECTED
    rel = comparison.relative_error
    if rel is None:
        # The literature value is 0, so there is no relative error to compare --
        # but there IS an answer: matching a zero reference exactly is the best
        # possible agreement, not a marginal one. A band gap of 0 for silver (a
        # metal, and 0.0 in Materials Project mp-124) came back "needs_review"
        # because dividing by zero is undefined, which read as doubt about a
        # result that was exactly right.
        #
        # Anything else against a zero reference stays NEEDS_REVIEW: without a
        # scale there is no way to say whether an absolute error of 0.3 is close.
        return ACCEPTED if comparison.absolute_error == 0 else NEEDS_REVIEW
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

    def describe(c: PairComparison) -> str:
        if c.property in thresholds.absolute:
            accept, review = thresholds.absolute[c.property]
            return (f"{c.molecule}/{c.property} {c.predicted:.3g} vs {c.literature:.3g}, "
                    f"off by {c.absolute_error:.2f} (accept below {accept:g}, "
                    f"review below {review:g})")
        if c.relative_error is not None:
            return f"{c.molecule}/{c.property} rel err {c.relative_error * 100:.1f}%"
        # Say what is actually known. The old text read "rel err n/a (literature
        # 0)" inside a sentence claiming the result was "within 30% but not 15%"
        # -- a band it had not computed and could not have.
        return (f"{c.molecule}/{c.property} matched against a reference of 0, so "
                f"there is no relative error; absolute error "
                f"{c.absolute_error:.4g}")

    banded = [c for c, _ in per if c.property in thresholds.absolute]
    if worst == ACCEPTED and banded:
        rationale = (f"All {len(per)} molecule(s) agree with their reference: "
                     + "; ".join(describe(c) for c in banded) + ".")
    elif worst == ACCEPTED:
        exact = [c for c, v in per if v == ACCEPTED and c.relative_error is None]
        rationale = f"All {len(per)} molecule(s) agree within {ap} relative error."
        if exact:
            # Otherwise "agree within 15%" is asserted about a comparison whose
            # relative error was never defined.
            names = ", ".join(f"{c.molecule}/{c.property}" for c in exact)
            rationale = (
                f"All {len(per)} molecule(s) agree with their reference"
                + (f" within {ap} relative error" if len(exact) < len(per) else "")
                + f". {names} matched a reference of 0 exactly."
            )
    elif worst == REJECTED:
        relative = [c for c in offenders if c.property not in thresholds.absolute]
        lead = (f"Rejected: at least one molecule exceeds {rp} relative error. " if relative
                else "Rejected: at least one molecule is outside its agreement band. ")
        rationale = lead + "; ".join(describe(c) for c in offenders)
    else:
        quantified = [c for c in offenders if c.relative_error is not None
                      and c.property not in thresholds.absolute]
        lead = ("Needs review: agreement is marginal." if not quantified
                and any(c.property in thresholds.absolute for c in offenders) else
                f"Needs review: agreement is marginal (within {rp} but not {ap})."
                if quantified else
                "Needs review: agreement could not be quantified.")
        rationale = lead + " " + "; ".join(describe(c) for c in offenders)

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
