"""Metric normalization for the result interpreter (Story 6.1).

Tool outputs differ wildly in shape, but downstream stages need one consistent
view: a single **primary metric**, zero or more **secondary metrics**, and
metadata -- each metric carrying a value, a unit, and an uncertainty. This module
collapses a parser's :class:`ParsedOutput` into that :class:`NormalizedResult`.

Aggregation of a multi-value series mirrors the confidence estimator: a
converging series (e.g. a loss curve) is represented by its settled tail mean;
otherwise independent repeats are represented by their mean. Uncertainty is
delegated to :mod:`confidence_estimator`.

The result is a plain, JSON-serializable structure (via ``to_dict``) so it can
be persisted as an artifact and consumed by cross-validation (Story 6.2).
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence

from result_interpreter import confidence_estimator as ce
from result_interpreter.extractors.base import (
    ParsedField,
    ParsedOutput,
    ParserError,
    get_parser,
)


@dataclass
class NormalizedMetric:
    name: str
    value: float
    uncertainty: float
    uncertainty_method: str
    unit: Optional[str] = None
    source: str = ""

    def relative_uncertainty(self) -> Optional[float]:
        """Uncertainty as a fraction of |value| (None when value is 0)."""
        return None if self.value == 0 else abs(self.uncertainty) / abs(self.value)

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class NormalizedResult:
    primary_metric: NormalizedMetric
    secondary_metrics: List[NormalizedMetric] = field(default_factory=list)
    metadata: Dict[str, object] = field(default_factory=dict)

    def all_metrics(self) -> List[NormalizedMetric]:
        return [self.primary_metric, *self.secondary_metrics]

    def get(self, name: str) -> Optional[NormalizedMetric]:
        return next((m for m in self.all_metrics() if m.name == name), None)

    def to_dict(self) -> Dict:
        return {
            "primary_metric": self.primary_metric.to_dict(),
            "secondary_metrics": [m.to_dict() for m in self.secondary_metrics],
            "metadata": dict(self.metadata),
        }


def _aggregate(values: Sequence[float]) -> float:
    """Representative value of a series (settled-tail mean if converging)."""
    if len(values) == 1:
        return values[0]
    if ce._looks_convergent(values):
        n = len(values)
        tail = list(values[max(1, 2 * n // 3):]) or list(values[-2:])
        return statistics.mean(tail)
    return statistics.mean(values)


def _metric_from_field(
    fld: ParsedField,
    unit_override: Optional[str],
    reported: Optional[Dict],
) -> NormalizedMetric:
    value = _aggregate(fld.values)
    reported = reported or {}
    unc = ce.estimate(
        value,
        fld.values,
        reported_std=reported.get("std"),
        confidence_interval=reported.get("ci"),
    )
    return NormalizedMetric(
        name=fld.name,
        value=value,
        uncertainty=unc.std,
        uncertainty_method=unc.method.value,
        unit=unit_override if unit_override is not None else fld.unit,
        source=fld.source,
    )


def normalize(
    parsed: ParsedOutput,
    *,
    primary: Optional[str] = None,
    units: Optional[Dict[str, str]] = None,
    reported: Optional[Dict[str, Dict]] = None,
) -> NormalizedResult:
    """Collapse a :class:`ParsedOutput` into a :class:`NormalizedResult`.

    Args:
        primary: field name to treat as the primary metric (default: the first
            field in the parsed output).
        units: optional {field_name: unit} overrides.
        reported: optional {field_name: {"std": ..} | {"ci": [lo, hi]}} to feed
            tool-reported uncertainty into the estimator.
    """
    units = units or {}
    reported = reported or {}

    if primary is not None and parsed.get(primary) is None:
        raise ParserError(f"primary metric {primary!r} not among fields {parsed.field_names()}")

    primary_name = primary if primary is not None else parsed.fields[0].name

    primary_metric: Optional[NormalizedMetric] = None
    secondary: List[NormalizedMetric] = []
    for fld in parsed.fields:
        metric = _metric_from_field(fld, units.get(fld.name), reported.get(fld.name))
        if fld.name == primary_name and primary_metric is None:
            primary_metric = metric
        else:
            secondary.append(metric)

    metadata = dict(parsed.metadata)
    metadata["field_count"] = len(parsed.fields)
    return NormalizedResult(
        primary_metric=primary_metric,
        secondary_metrics=secondary,
        metadata=metadata,
    )


def interpret(
    content: str,
    parser: str,
    *,
    primary: Optional[str] = None,
    parser_options: Optional[Dict] = None,
    units: Optional[Dict[str, str]] = None,
    reported: Optional[Dict[str, Dict]] = None,
) -> NormalizedResult:
    """One-shot: parse ``content`` with the named parser, then normalize.

    Example::

        interpret(csv_text, "csv", primary="logS", units={"logS": "log10(mol/L)"})
    """
    parsed = get_parser(parser).parse(content, **(parser_options or {}))
    return normalize(parsed, primary=primary, units=units, reported=reported)
