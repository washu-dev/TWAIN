"""Cross-validation against literature baselines (Story 6.2).

Compares predicted values (the normalized metrics produced by the result
interpreter, Story 6.1) against a versioned database of measured literature
values and quantifies agreement:

  * per-molecule absolute error ``|predicted - literature|`` and relative error
    ``|predicted - literature| / |literature|``,
  * aggregate RMSE and Pearson correlation when more than one molecule is
    compared, and
  * a human-readable gap analysis that flags systematic bias and the worst
    outliers.

The rich :class:`CrossValidationResult` is what the acceptance judge
(``acceptance_judge.py``) grades; it also renders down to the schema-conformant
:class:`ValidationReport` (Story 1.5) that the pipeline's VALIDATE guard reads.

The baseline DB is loaded from ``configs/baselines.json`` (a versioned, immutable
snapshot); the path is injectable for tests.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from cross_validation.validation_report import ValidationReport

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINES_PATH = _REPO_ROOT / "configs" / "baselines.json"


# --------------------------------------------------------------------------- #
# baseline database
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BaselineRecord:
    molecule: str
    property: str
    literature_value: float
    literature_source: str = ""
    unit: Optional[str] = None
    doi: Optional[str] = None


class BaselineDB:
    """A loaded, queryable snapshot of literature baselines."""

    def __init__(self, records: Sequence[BaselineRecord], version: str = ""):
        self.version = version
        self._by_key: Dict[tuple, BaselineRecord] = {
            self._key(r.molecule, r.property): r for r in records
        }

    @staticmethod
    def _key(molecule: str, prop: str) -> tuple:
        return (molecule.strip().lower(), prop.strip().lower())

    @classmethod
    def load(cls, path=DEFAULT_BASELINES_PATH) -> "BaselineDB":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        units = data.get("property_units", {})
        default_source = data.get("default_source", "")
        default_doi = data.get("default_doi")
        records = []
        for entry in data.get("baselines", []):
            prop = entry["property"]
            records.append(
                BaselineRecord(
                    molecule=entry["molecule"],
                    property=prop,
                    literature_value=float(entry["literature_value"]),
                    literature_source=entry.get("literature_source", default_source),
                    unit=entry.get("unit", units.get(prop)),
                    doi=entry.get("doi", default_doi),
                )
            )
        return cls(records, version=data.get("version", ""))

    def lookup(self, molecule: str, prop: str) -> Optional[BaselineRecord]:
        return self._by_key.get(self._key(molecule, prop))

    def __len__(self) -> int:
        return len(self._by_key)


# --------------------------------------------------------------------------- #
# predictions + comparisons
# --------------------------------------------------------------------------- #
@dataclass
class Prediction:
    molecule: str
    property: str
    value: float
    unit: Optional[str] = None
    uncertainty: Optional[float] = None


@dataclass
class PairComparison:
    """One predicted-vs-literature comparison."""

    molecule: str
    property: str
    predicted: float
    literature: float
    absolute_error: float
    relative_error: Optional[float]  # None when literature == 0
    literature_source: str = ""
    doi: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CrossValidationResult:
    comparisons: List[PairComparison]
    unmatched: List[str] = field(default_factory=list)  # "molecule/property" with no baseline
    rmse: Optional[float] = None
    pearson: Optional[float] = None
    mean_relative_error: Optional[float] = None
    mean_signed_error: Optional[float] = None

    def agreement(self) -> float:
        """A [0, 1] agreement score: 1 - mean relative error, clamped."""
        if self.mean_relative_error is None:
            return 0.0
        return max(0.0, min(1.0, 1.0 - self.mean_relative_error))

    def gap_analysis(self) -> str:
        if not self.comparisons:
            return "No predictions matched a literature baseline; agreement could not be assessed."
        lines: List[str] = []
        for c in self.comparisons:
            rel = "n/a" if c.relative_error is None else f"{c.relative_error * 100:.1f}%"
            lines.append(
                f"{c.molecule}/{c.property}: predicted {c.predicted:.3g} vs literature "
                f"{c.literature:.3g} (abs err {c.absolute_error:.3g}, rel err {rel})"
            )
        if self.mean_signed_error is not None and len(self.comparisons) > 1:
            direction = "over" if self.mean_signed_error > 0 else "under"
            lines.append(
                f"Systematic bias: mean signed error {self.mean_signed_error:+.3g} "
                f"({direction}-prediction on average)."
            )
        if self.rmse is not None:
            lines.append(f"RMSE {self.rmse:.3g}" + (f", Pearson r {self.pearson:.3f}" if self.pearson is not None else "."))
        if self.unmatched:
            lines.append(f"No baseline for: {', '.join(self.unmatched)}.")
        return " ".join(lines)

    def literature_summary(self) -> str:
        parts = []
        for c in self.comparisons:
            src = f" [{c.doi}]" if c.doi else (f" ({c.literature_source})" if c.literature_source else "")
            parts.append(f"{c.molecule}/{c.property}={c.literature:.3g}{src}")
        return "; ".join(parts) if parts else "no matched literature values"

    def to_validation_report(
        self,
        acceptance_status: str,
        *,
        report_id: str = "",
        timestamp: str = "",
    ) -> ValidationReport:
        return ValidationReport(
            comparison={
                "literature_results": self.literature_summary(),
                "agreement": self.agreement(),
                "difference_analysis": self.gap_analysis(),
            },
            acceptance_status=acceptance_status,
            metadata={"ID": report_id, "timestamp": timestamp},
        )

    def to_dict(self) -> Dict:
        return {
            "comparisons": [c.to_dict() for c in self.comparisons],
            "unmatched": list(self.unmatched),
            "rmse": self.rmse,
            "pearson": self.pearson,
            "mean_relative_error": self.mean_relative_error,
            "mean_signed_error": self.mean_signed_error,
            "agreement": self.agreement(),
        }


# --------------------------------------------------------------------------- #
# comparison logic
# --------------------------------------------------------------------------- #
def _pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None  # no variance -> correlation undefined
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def compare(predictions: Sequence[Prediction], db: BaselineDB) -> CrossValidationResult:
    """Compare each prediction against the baseline DB and aggregate."""
    comparisons: List[PairComparison] = []
    unmatched: List[str] = []

    for pred in predictions:
        baseline = db.lookup(pred.molecule, pred.property)
        if baseline is None:
            unmatched.append(f"{pred.molecule}/{pred.property}")
            continue
        abs_err = abs(pred.value - baseline.literature_value)
        rel_err = abs_err / abs(baseline.literature_value) if baseline.literature_value != 0 else None
        comparisons.append(
            PairComparison(
                molecule=pred.molecule,
                property=pred.property,
                predicted=pred.value,
                literature=baseline.literature_value,
                absolute_error=abs_err,
                relative_error=rel_err,
                literature_source=baseline.literature_source,
                doi=baseline.doi,
            )
        )

    result = CrossValidationResult(comparisons=comparisons, unmatched=unmatched)
    if comparisons:
        preds = [c.predicted for c in comparisons]
        lits = [c.literature for c in comparisons]
        result.mean_signed_error = sum(p - l for p, l in zip(preds, lits)) / len(comparisons)
        rels = [c.relative_error for c in comparisons if c.relative_error is not None]
        result.mean_relative_error = (sum(rels) / len(rels)) if rels else None
        if len(comparisons) > 1:
            result.rmse = math.sqrt(sum((p - l) ** 2 for p, l in zip(preds, lits)) / len(comparisons))
            result.pearson = _pearson(preds, lits)
    return result


def predictions_from_normalized(
    normalized_result,
    molecule: str,
    property_map: Optional[Dict[str, str]] = None,
) -> List[Prediction]:
    """Adapt a Story 6.1 ``NormalizedResult`` into predictions for one molecule.

    Each normalized metric becomes a :class:`Prediction`; ``property_map`` renames
    a metric to its baseline property name (default: the metric name is used).
    """
    property_map = property_map or {}
    predictions = []
    for metric in normalized_result.all_metrics():
        predictions.append(
            Prediction(
                molecule=molecule,
                property=property_map.get(metric.name, metric.name),
                value=metric.value,
                unit=metric.unit,
                uncertainty=metric.uncertainty,
            )
        )
    return predictions
