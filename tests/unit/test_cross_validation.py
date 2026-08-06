"""Unit tests for cross-validation against baselines (Story 6.2).

Covers:
  * the shipped baselines.json snapshot: loads, and validates against its schema,
  * baseline lookup (case-insensitive) and default source/doi inheritance,
  * comparison metrics (absolute / relative error, RMSE, Pearson),
  * acceptance verdicts (accept / needs_review / reject) incl. configurable
    thresholds and the literature==0 edge case,
  * the emitted ValidationReport is schema-conformant, and
  * the Story 6.1 -> 6.2 handoff via predictions_from_normalized.

Run from the repo root with:  pixi run pytest tests/unit/test_cross_validation.py
"""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from cross_validation.baseline_validator import (
    BaselineDB,
    BaselineRecord,
    CrossValidationResult,
    PairComparison,
    Prediction,
    compare,
    predictions_from_normalized,
)
from cross_validation.acceptance_judge import (
    ACCEPTED,
    NEEDS_REVIEW,
    REJECTED,
    AcceptanceThresholds,
    cross_validate,
    judge,
)
from cross_validation.validation_report import ValidationReport

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATION_SCHEMA = REPO_ROOT / "schemas" / "validation_report.schema.json"


# ── baseline DB ──────────────────────────────────────────────────────────────

def test_shipped_db_loads_with_enough_baselines():
    db = BaselineDB.load()
    assert len(db) >= 10  # backlog asks for 10-20 seed molecules
    assert db.version


def test_lookup_is_case_insensitive_and_inherits_defaults():
    db = BaselineDB.load()
    rec = db.lookup("Aspirin", "logS")
    assert rec is not None
    assert rec.literature_value == pytest.approx(-1.72)
    assert rec.unit == "log10(mol/L)"      # from property_units
    assert rec.doi == "10.1021/ci034243x"  # from default_doi
    assert db.lookup("nonexistent", "logS") is None


# ── comparison metrics ───────────────────────────────────────────────────────

def _db():
    return BaselineDB(
        [
            BaselineRecord("aspirin", "logS", -2.0),
            BaselineRecord("benzene", "logS", -1.0),
            BaselineRecord("water", "logS", 0.0),
        ],
        version="test",
    )


def test_absolute_and_relative_error():
    result = compare([Prediction("aspirin", "logS", -2.2)], _db())
    c = result.comparisons[0]
    assert c.absolute_error == pytest.approx(0.2)
    assert c.relative_error == pytest.approx(0.1)  # 0.2 / 2.0


def test_relative_error_none_when_literature_zero():
    result = compare([Prediction("water", "logS", 0.3)], _db())
    assert result.comparisons[0].relative_error is None
    assert result.comparisons[0].absolute_error == pytest.approx(0.3)


def test_rmse_and_pearson_for_multiple_molecules():
    preds = [Prediction("aspirin", "logS", -1.8), Prediction("benzene", "logS", -1.2)]
    result = compare(preds, _db())
    # errors: (-1.8 - -2.0)=0.2, (-1.2 - -1.0)=-0.2 -> RMSE = 0.2
    assert result.rmse == pytest.approx(0.2)
    # predictions track literature order perfectly -> strong positive correlation
    assert result.pearson == pytest.approx(1.0, abs=1e-9)


def test_unmatched_predictions_are_reported():
    result = compare([Prediction("unobtainium", "logS", 1.0)], _db())
    assert result.comparisons == []
    assert result.unmatched == ["unobtainium/logS"]


def test_mean_signed_error_detects_bias():
    # both predictions above literature -> positive signed error
    preds = [Prediction("aspirin", "logS", -1.5), Prediction("benzene", "logS", -0.5)]
    result = compare(preds, _db())
    assert result.mean_signed_error == pytest.approx(0.5)


# ── acceptance verdict ───────────────────────────────────────────────────────

def test_verdict_accept_when_all_within_15pct():
    result = compare([Prediction("aspirin", "logS", -2.1)], _db())  # 5% error
    assert judge(result).status == ACCEPTED


def test_verdict_needs_review_when_marginal():
    result = compare([Prediction("aspirin", "logS", -2.4)], _db())  # 20% error
    assert judge(result).status == NEEDS_REVIEW


def test_verdict_reject_when_poor():
    result = compare([Prediction("aspirin", "logS", -3.0)], _db())  # 50% error
    v = judge(result)
    assert v.status == REJECTED
    assert "aspirin" in v.rationale  # explainable: names the offender


def test_worst_molecule_drives_overall_verdict():
    preds = [Prediction("aspirin", "logS", -2.05), Prediction("benzene", "logS", -2.0)]  # 2.5% and 100%
    assert judge(compare(preds, _db())).status == REJECTED


def test_configurable_thresholds():
    result = compare([Prediction("aspirin", "logS", -2.2)], _db())  # 10% error
    strict = AcceptanceThresholds(accept_below=0.05, review_below=0.08)
    assert judge(result, strict).status == REJECTED  # 10% > 8%
    lenient = AcceptanceThresholds(accept_below=0.20, review_below=0.40)
    assert judge(result, lenient).status == ACCEPTED


def test_zero_literature_caps_at_needs_review():
    result = compare([Prediction("water", "logS", 0.001)], _db())
    assert judge(result).status == NEEDS_REVIEW


def test_no_matches_is_needs_review():
    result = compare([Prediction("unobtainium", "logS", 1.0)], _db())
    assert judge(result).status == NEEDS_REVIEW


def test_bad_thresholds_rejected():
    with pytest.raises(ValueError):
        AcceptanceThresholds(accept_below=0.4, review_below=0.2)


# ── ValidationReport emission ────────────────────────────────────────────────

def test_emitted_validation_report_is_schema_valid():
    result = compare([Prediction("aspirin", "logS", -2.1)], _db())
    report = result.to_validation_report(judge(result).status, report_id="run-1", timestamp="12:00:00")
    assert isinstance(report, ValidationReport)

    from dataclasses import asdict
    schema = json.loads(VALIDATION_SCHEMA.read_text())
    Draft202012Validator(schema).validate(asdict(report))


def test_agreement_score_tracks_error():
    good = compare([Prediction("aspirin", "logS", -2.0)], _db())     # 0% error
    poor = compare([Prediction("aspirin", "logS", -3.0)], _db())     # 50% error
    assert good.agreement() == pytest.approx(1.0)
    assert poor.agreement() == pytest.approx(0.5)


# ── end-to-end convenience + 6.1 handoff ─────────────────────────────────────

def test_cross_validate_end_to_end_with_shipped_db():
    result, verdict, report = cross_validate([Prediction("aspirin", "logS", -1.70)])
    assert verdict.status == ACCEPTED
    assert report.acceptance_status == ACCEPTED
    assert result.comparisons[0].literature == pytest.approx(-1.72)


def test_predictions_from_normalized_result():
    from result_interpreter.metric_normalizer import interpret

    normalized = interpret('{"logS": -1.7, "MolWt": 180.16}', "json", primary="logS")
    preds = predictions_from_normalized(normalized, molecule="aspirin")
    props = {p.property for p in preds}
    assert "logS" in props and "MolWt" in props
    # the logS prediction can be graded against the shipped baseline
    result, verdict, _ = cross_validate([p for p in preds if p.property == "logS"])
    assert verdict.status == ACCEPTED


class TestAZeroReferenceIsNotAMarginalResult:
    """Silver's band gap, from run 913c1ee9.

    Ag is a metal: fcc silver has bands crossing the Fermi level, so it has no
    gap. GPAW computed 0.0 and Materials Project mp-124 gives 0.0 -- an exact
    match, and the best outcome available. It was reported as "needs_review:
    agreement is marginal (within 30% but not 15%)", because relative error is
    undefined when the reference is 0, and the fallback both downgraded the
    verdict and asserted a percentage band it had never computed.
    """

    def _result(self, predicted, literature, absolute_error, relative_error):
        return CrossValidationResult(
            comparisons=[PairComparison(
                molecule="Silver", property="bandgap", predicted=predicted,
                literature=literature, absolute_error=absolute_error,
                relative_error=relative_error,
                literature_source="Materials Project mp-124 (Ag) [MP entry]")],
            mean_relative_error=relative_error)

    def test_matching_a_zero_reference_exactly_is_accepted(self):
        verdict = judge(self._result(0.0, 0.0, 0.0, None))
        assert verdict.status == ACCEPTED

    def test_the_rationale_says_what_actually_happened(self):
        verdict = judge(self._result(0.0, 0.0, 0.0, None))
        assert "matched a reference of 0 exactly" in verdict.rationale
        # It must not claim a relative-error band it could not compute.
        assert "within 15%" not in verdict.rationale
        assert "marginal" not in verdict.rationale

    def test_a_nonzero_value_against_a_zero_reference_still_needs_review(self):
        """No scale exists, so there is no way to call 0.3 eV close or far."""
        verdict = judge(self._result(0.3, 0.0, 0.3, None))
        assert verdict.status == NEEDS_REVIEW
        assert "could not be quantified" in verdict.rationale
        assert "absolute error 0.3" in verdict.rationale
        assert "marginal" not in verdict.rationale

    def test_an_ordinary_marginal_result_still_says_marginal(self):
        """The honest wording for the zero case must not cost the normal case its
        explanation."""
        verdict = judge(self._result(1.4, 1.17, 0.23, 0.197))
        assert verdict.status == NEEDS_REVIEW
        assert "marginal (within 30% but not 15%)" in verdict.rationale

    def test_a_mixed_batch_does_not_overclaim(self):
        """One exact zero match plus one ordinary pass: the sentence has to cover
        both without asserting a percentage about the zero one."""
        result = CrossValidationResult(comparisons=[
            PairComparison(molecule="Silver", property="bandgap", predicted=0.0,
                           literature=0.0, absolute_error=0.0, relative_error=None),
            PairComparison(molecule="Silicon", property="bandgap", predicted=1.15,
                           literature=1.17, absolute_error=0.02, relative_error=0.017),
        ], mean_relative_error=0.017)
        verdict = judge(result)
        assert verdict.status == ACCEPTED
        assert "Silver/bandgap matched a reference of 0 exactly" in verdict.rationale
        assert "within 15% relative error" in verdict.rationale

    def test_a_rejected_batch_still_names_its_offenders(self):
        verdict = judge(self._result(3.0, 1.17, 1.83, 1.56))
        assert verdict.status == REJECTED
        assert "rel err 156.0%" in verdict.rationale
