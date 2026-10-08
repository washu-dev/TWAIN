"""Is this value physically possible at all? A backstop for VALIDATE.

Every other check in module 11 is *comparative*: it needs a literature baseline
or the plan's own target +/- tolerance. When neither exists -- a novel system, or
a plan whose acceptance metric carries ``target_value: null`` -- there is nothing
to compare against, and ``_acceptance_fallback`` says so honestly and delivers the
result as accepted. That is the right default for a value nobody can grade.

It is the wrong default for a value that cannot exist. A CO2 heat of formation
came back as -27452 kJ/mol (experiment: -393.5) because a generated script
converted energies to kJ/mol before handing them to ``twain_thermo``, which
converts again; both branches of VALIDATE had nothing to say about it, so it
reached the researcher labelled "accepted" (run 1cd39ffd).

This module supplies the one check that needs no reference value: a bound on what
the quantity can physically be. ``configs/physical_ranges.json`` holds the bounds
and the reasoning for each, deliberately loose -- 2-4x the most extreme real value
-- so a finding here is never a judgement about accuracy. It means the number is
not a value of that quantity.

Three deliberate limits, so this is read for what it is:

* **It only knows the properties in the table.** No entry -> no finding. Silence
  is "no bound is known", never "this looks fine".
* **A stated unit that differs from the table's is skipped, not converted.** A run
  reporting kcal/mol must not be graded against a kJ/mol bound. When no unit is
  stated at all the bound IS applied, because that is the dangerous case and the
  finding says which unit it assumed.
* **An upper bound only catches slips in one direction.** A value 100x too small
  passes. Stating units at the source is the fix; this is the backstop.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_PATH = (Path(__file__).resolve().parents[1].parent
               / "configs" / "physical_ranges.json")

# Unit suffixes a metric name carries when the generated script names the column
# after the property AND its unit ("standard_heat_of_formation_kJ_mol"). The plan
# and the baseline DB key on the property alone, which is the same mismatch
# _BASELINE_PROPERTY_ALIASES exists for.
#: "_at_25c", "_at_298k", "_at_298_15k", "_at_1_atm", "_25c" -- conditions inside a metric name.
_CONDITION = re.compile(r"_(?:at_)?\d+(?:_\d+)?_?(?:c|k|degc|degrees_c|atm|bar)(?=_|$)")

_UNIT_SUFFIX = re.compile(
    r"_(?:"
    r"kj_?mol|kj_?per_?mol|kcal_?mol|ev|ev_?per_?atom|ev_?atom|"
    r"gpa|mpa|pa|kbar|g_?cm3|kg_?m3|ang|angstrom|nm|pm|bohr|k|kelvin|"
    r"log10_?mol_?l|log_?mol_?per_?l|mol_?l"
    r")$"
)


@dataclass(frozen=True)
class Range:
    """A physically possible interval for one property."""
    property: str
    unit: str
    minimum: Optional[float]
    maximum: Optional[float]
    reason: str
    why: str

    def contains(self, value: float) -> bool:
        if self.minimum is not None and value < self.minimum:
            return False
        return not (self.maximum is not None and value > self.maximum)

    def describe(self) -> str:
        if self.minimum is not None and self.maximum is not None:
            return f"{self.minimum:g} to {self.maximum:g} {self.unit}"
        if self.maximum is not None:
            return f"at most {self.maximum:g} {self.unit}"
        return f"at least {self.minimum:g} {self.unit}"


_FINITE = "a finite number"


@dataclass(frozen=True)
class Finding:
    """Why a value cannot be what it claims to be."""
    metric: str
    value: float
    expected: str
    reason: str
    likely_cause: Optional[str] = None

    def message(self) -> str:
        if self.expected == _FINITE:
            text = (f"{self.metric} came out as {self.value:.6g}, which is not a "
                    f"number a measurement can take: the calculation did not "
                    f"produce a usable value for this metric.")
        else:
            text = (f"{self.metric} = {self.value:.6g} is not physically possible "
                    f"-- {self.reason}, so a real value lies within "
                    f"{self.expected}. This is not an accuracy judgement: the "
                    f"number is outside what the quantity can be, so it cannot "
                    f"be reported as a result.")
        if self.likely_cause:
            text += f" {self.likely_cause}"
        return text


class _Table:
    """The parsed range table, with property-name matching."""

    def __init__(self, data: Dict[str, Any]):
        self._by_name: Dict[str, Range] = {}
        # label -> (factor, applicable canonical units or None for any)
        self._hints: Dict[str, tuple] = {}
        for entry in data.get("ranges") or []:
            name = entry.get("property")
            if not name:
                continue
            rng = Range(
                property=str(name), unit=str(entry.get("unit") or ""),
                minimum=_as_float(entry.get("min")),
                maximum=_as_float(entry.get("max")),
                # ``reason`` is the one-clause version that goes in the verdict;
                # ``why`` is the long justification, for whoever edits a bound.
                # Two fields rather than truncating one: every ``why`` here
                # contains a decimal point, so splitting on sentences cut
                # "reach ~14 eV (LiF 14.2)" off mid-number.
                reason=str(entry.get("reason") or "").strip(),
                why=str(entry.get("why") or ""),
            )
            for key in [name, *(entry.get("aliases") or [])]:
                self._by_name[_normalise(key)] = rng
        hints = data.get("conversion_hints") or {}
        for entry in hints.get("factors") or []:
            if not isinstance(entry, dict):
                continue
            factor = _as_float(entry.get("factor"))
            label = str(entry.get("label") or "").strip()
            if not factor or factor <= 0 or not label:
                continue
            units = entry.get("units")
            self._hints[label] = (
                factor,
                None if units in (None, "*") else
                {_canonical_unit(u) for u in units if u},
            )

    def lookup(self, metric_name: Any) -> Optional[Range]:
        """The range for a metric name, tolerating a trailing unit suffix."""
        key = _normalise(metric_name)
        if not key:
            return None
        # Conditions name WHEN the value holds, not WHAT it is: drop them first, so
        # "aqueous_solubility_at_25c_mol_per_l" is checked as
        # "aqueous_solubility_mol_per_l". Without this a generated metric name
        # matched nothing and a 4.8e11 mol/L solubility was accepted (6678e7e7).
        key = _CONDITION.sub("", key) or key
        found = self._by_name.get(key)
        if found is not None:
            return found
        # "standard_heat_of_formation_kj_mol" -> "standard_heat_of_formation".
        # Repeated because a name can carry both ("..._ev_per_atom").
        for _ in range(2):
            stripped = _UNIT_SUFFIX.sub("", key)
            if stripped == key:
                break
            key = stripped
            found = self._by_name.get(key)
            if found is not None:
                return found
        return None

    def hint_for(self, value: float, rng: Range) -> Optional[str]:
        """Every known unit slip that would bring ``value`` into range.

        Deliberately ALL of them, not the best one. Because the bounds are loose,
        several factors can land inside: -27452 kJ/mol is explained by the 96.485x
        that actually caused it AND by reading kcal/mol as kJ/mol. Ranking them
        would be inventing a confidence this check does not have -- an earlier
        version returned the first match by factor size and confidently named the
        wrong cause. Listing the candidates says what is certain (this is a unit
        problem) without asserting what is not (which one).

        Capped at three so the verdict stays readable, largest factor first: a
        whole conversion applied twice is the slip that has actually happened.
        """
        matches = []
        unit = _canonical_unit(rng.unit)
        for label, (factor, units) in sorted(self._hints.items(),
                                             key=lambda kv: kv[1][0],
                                             reverse=True):
            if units is not None and unit not in units:
                continue  # this conversion cannot apply to this quantity
            for candidate, direction in ((value / factor, "divided"),
                                         (value * factor, "multiplied")):
                if rng.contains(candidate):
                    matches.append(f"{direction} by {factor:g} it is "
                                   f"{candidate:.6g} {rng.unit} ({label})")
                    break
        if not matches:
            return None
        shown = "; ".join(matches[:3])
        tail = "" if len(matches) <= 3 else f", and {len(matches) - 3} more"
        return (f"This looks like a unit slip rather than a bad calculation: "
                f"{shown}{tail} -- each of those is possible, so check every "
                f"conversion on the way to this number.")


def _as_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _normalise(name: Any) -> str:
    """Metric/property name -> comparison key: lowercase, single underscores."""
    if name is None:
        return ""
    text = re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower())
    return text.strip("_")


def _canonical_unit(unit: Any) -> Optional[str]:
    """Delegate to the validator's unit spellings, with a local fallback.

    Imported lazily so this module works inside a bundle (where module 11 is not
    present) as well as in the pipeline.
    """
    try:
        from cross_validation.baseline_validator import canonical_unit
    except ImportError:  # pragma: no cover - bundle/standalone use
        if unit is None:
            return None
        text = str(unit).strip().lower().replace(" ", "").replace("-", "")
        return text or None
    return canonical_unit(unit)


_TABLE: Optional[_Table] = None


def load_table(path: Optional[Path] = None) -> _Table:
    """The range table, cached. A missing or broken file disables the check."""
    global _TABLE
    if path is None and _TABLE is not None:
        return _TABLE
    target = Path(path) if path is not None else CONFIG_PATH
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    table = _Table(data if isinstance(data, dict) else {})
    if path is None:
        _TABLE = table
    return table


def check_metric(name: Any, value: Any, unit: Any = None, *,
                 table: Optional[_Table] = None) -> Optional[Finding]:
    """A :class:`Finding` when ``value`` cannot be a value of ``name``, else None.

    Returns None -- meaning "nothing to say" -- when the property has no bound in
    the table, when the value is not a number, or when a STATED unit differs from
    the one the bound is expressed in (that comparison would be meaningless).
    """
    table = table if table is not None else load_table()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None  # not a number at all: the extractor's problem, not ours
    # NaN and inf are flagged whatever the property, and without consulting the
    # table: no quantity has a non-finite value, so this needs no bound.
    if not math.isfinite(value):
        return Finding(metric=str(name), value=float(value), expected=_FINITE,
                       reason="the extracted value is not finite")

    rng = table.lookup(name)
    if rng is None:
        return None
    stated, expected = _canonical_unit(unit), _canonical_unit(rng.unit)
    if stated is not None and expected is not None and stated != expected:
        return None  # different unit: not comparable, and not ours to convert
    number = float(value)
    if rng.contains(number):
        return None
    parts = [table.hint_for(number, rng)]
    if stated is None:
        parts.append(f"The result declared no unit, so this assumes {rng.unit}, "
                     f"which is what the property is reported in.")
    return Finding(
        metric=str(name), value=number, expected=rng.describe(),
        reason=rng.reason or f"{rng.property} is bounded",
        likely_cause=" ".join(p for p in parts if p) or None,
    )


def check_metrics(metrics: Any, *, table: Optional[_Table] = None) -> List[Finding]:
    """Findings for a sequence of metric dicts, worst (largest overshoot) first."""
    findings = []
    for metric in metrics or []:
        if not isinstance(metric, dict):
            continue
        found = check_metric(metric.get("name"), metric.get("value"),
                             metric.get("unit"), table=table)
        if found is not None:
            findings.append(found)
    return findings


def enabled() -> bool:
    """Whether the check is on. ``TWAIN_PLAUSIBILITY_CHECK=0`` turns it off.

    An escape hatch for a deployment whose properties legitimately fall outside
    these bounds, so the answer to a bad bound is never "edit the pipeline".
    """
    return os.environ.get("TWAIN_PLAUSIBILITY_CHECK", "1").strip() not in (
        "0", "false", "no", "off")
