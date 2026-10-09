"""Check a researcher's target against a run's result as a person would (#237).

The target is what the researcher typed on the approval card ("log S = -1.72",
"0.75 log units"); the result is whatever fields the run printed, each in the
form its script chose (0.0101886 mol/L, logS_log10_mol_per_L = -1.99, ...). A
person reads the target's unit and scale, finds the matching result, puts both
on one scale and compares. Here:

* :func:`read_target` decides the target's unit, the tolerance's unit, and which
  result field (and unit) it is about. An LLM reads it when one is available,
  and code checks every part of that reading -- the number must be the one the
  researcher typed, each unit must convert, the field must exist -- before it
  is used; otherwise (or offline) plain rules read it. Which one was used, and
  why an LLM reading was refused, is recorded.
* :func:`compare` does the arithmetic with :mod:`harmonize`: both sides on the
  family's canonical scale, the tolerance carried as an interval (a log scale
  is not linear), every step shown.

The verdict comes from the numbers. The LLM never supplies a value.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field

from cross_validation import harmonize as H

_NUMBER = re.compile(r"[-+]?(?:\d+(?:,\d{3})*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_MINUS = str.maketrans({"−": "-", "‒": "-", "–": "-", "—": "-"})
_LOG_WORDS = re.compile(r"\blog\s*(?:10)?\s*s\b|\blog\s*10\b|\blog\b", re.IGNORECASE)


def numbers_in(text) -> list:
    """The numbers written in ``text`` ("log S = −1.72" -> [-1.72]; "4,600 mg/L" -> [4600])."""
    found = []
    for token in _NUMBER.findall(str(text or "").translate(_MINUS)):
        try:
            found.append(float(token.replace(",", "")))
        except ValueError:
            continue
    # "log10" and "log S" carry a 10 / nothing that isn't the value.
    return found


def _unit_after_number(text) -> str | None:
    """The unit written after the number: "0.0102 mol/L" -> "mol/L"."""
    text = str(text or "").translate(_MINUS)
    match = re.search(r"[-+]?(?:\d+(?:,\d{3})*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?\s*(.+)$", text)
    return match.group(1).strip() if match else None


def _converts(family, unit, formula) -> bool:
    try:
        H.to_canonical(family, 1.0, unit, formula=formula)
        return True
    except H.NotConvertible:
        return False


@dataclass
class Reading:
    """What the target means, and which result it is about."""
    target_unit: str | None
    tolerance_unit: str | None
    result_field: str | None
    result_unit: str | None
    source: str                         # "llm" | "rules"
    notes: list = field(default_factory=list)


# ------------------------------------------------------------------ reading
def _rules_unit(text, family, formula) -> str | None:
    """A unit read from typed text: the one after the number, else "log" words."""
    after = _unit_after_number(text)
    if after and _converts(family, after, formula):
        return after
    if family == "aqueous_solubility" and text and _LOG_WORDS.search(str(text)):
        return H.CANONICAL[family]
    return None


def _rules_reading(criterion, metrics, family, formula, pick_field) -> Reading:
    target_text = criterion.get("target_text") or ""
    tolerance_text = criterion.get("tolerance_text") or ""
    target_unit = _rules_unit(target_text, family, formula)
    tolerance_unit = _rules_unit(tolerance_text, family, formula) or target_unit
    names = [m["name"] for m in metrics]
    field_name = pick_field(names, [criterion.get("metric_name")])
    metric = next((m for m in metrics if m["name"] == field_name), None)
    result_unit = (metric or {}).get("unit")
    if result_unit is None and field_name and family == "aqueous_solubility" \
            and "log" in field_name.lower():
        result_unit = H.CANONICAL[family]
    return Reading(target_unit, tolerance_unit, field_name, result_unit, "rules")


_PROMPT = (
    "A researcher set an acceptance target for a computational-chemistry result. Read it "
    "the way a careful scientist would. Reply with JSON only: {{\"target_unit\": unit or "
    "null, \"tolerance_unit\": unit or null, \"result_field\": one of the result fields "
    "below, \"result_unit\": unit or null, \"reason\": one sentence}}.\n"
    "Units must be written plainly (e.g. \"log10(mol/L)\", \"mol/L\", \"mg/L\", \"g/L\", "
    "\"kJ/mol\", \"kcal/mol\", \"eV\"). Use null when the text does not say; never "
    "invent a value.\n\n"
    "Quantity: {family} (compared as {canonical})\n"
    "Metric name: {metric}\n"
    "Target as typed: {target_text!r} (read as the number {target_value})\n"
    "Tolerance as typed: {tolerance_text!r} (read as {tolerance})\n"
    "Result fields (name = value [unit]):\n{fields}\n")


def _llm_reading(criterion, metrics, family, formula, agent) -> tuple:
    """(Reading or None, why it was refused)."""
    fields = "\n".join(f"  {m['name']} = {m['value']:.6g} [{m.get('unit') or 'no unit'}]"
                       for m in metrics)
    prompt = _PROMPT.format(
        family=family, canonical=H.CANONICAL[family], metric=criterion.get("metric_name"),
        target_text=criterion.get("target_text") or "", target_value=criterion.get("target_value"),
        tolerance_text=criterion.get("tolerance_text") or "",
        tolerance=criterion.get("tolerance"), fields=fields)
    try:
        raw = agent(prompt) or ""
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:  # noqa: BLE001 - an unreadable answer is refused, not guessed at
        return None, "the LLM's answer was not readable JSON"
    # Code checks every part of the reading before any of it is used.
    names = {m["name"] for m in metrics}
    if data.get("result_field") not in names:
        return None, f"it named a result field that doesn't exist ({data.get('result_field')!r})"
    for key in ("target_unit", "tolerance_unit", "result_unit"):
        unit = data.get(key)
        if unit is not None and not _converts(family, unit, formula):
            return None, f"its {key.replace('_', ' ')} {unit!r} doesn't convert to {H.CANONICAL[family]}"
    typed = criterion.get("target_text")
    if typed and criterion.get("target_value") is not None and not any(
            math.isclose(n, float(criterion["target_value"]), rel_tol=1e-9, abs_tol=1e-12)
            for n in numbers_in(typed)):
        return None, "the target number isn't the one the researcher typed"
    reason = " ".join(str(data.get("reason") or "").split())[:300]
    return Reading(data.get("target_unit"), data.get("tolerance_unit") or data.get("target_unit"),
                   data["result_field"], data.get("result_unit"), "llm",
                   [f"read by the LLM: {reason}"] if reason else []), None


def read_target(criterion: dict, metrics: list, family: str, *, formula=None, agent=None,
                pick_field) -> Reading:
    """How to read ``criterion`` against ``metrics`` (LLM if it passes the checks, else rules)."""
    if agent is not None:
        reading, refused = _llm_reading(criterion, metrics, family, formula, agent)
        if reading is not None:
            return reading
        fallback = _rules_reading(criterion, metrics, family, formula, pick_field)
        fallback.notes.append(f"the LLM's reading was refused: {refused}")
        return fallback
    return _rules_reading(criterion, metrics, family, formula, pick_field)


# ------------------------------------------------------------------ comparing
@dataclass
class TargetCheck:
    metric_name: str
    status: str                       # accepted | needs_review | rejected
    result: float
    target: float
    low: float
    high: float
    unit: str
    working: list
    reading: dict
    gap: float                        # the miss in tolerances (0 inside the band)

    def summary(self) -> str:
        return (f"{self.metric_name}: {self.result:.4g} vs target {self.target:.4g} "
                f"({self.low:.4g} to {self.high:.4g}) {self.unit} -> {self.status}")

    def as_dict(self) -> dict:
        return asdict(self)


def compare(criterion: dict, metrics: list, family: str, reading: Reading, *,
            formula=None) -> TargetCheck | None:
    """The target and the result on one scale, or None when either can't be put there."""
    metric = next((m for m in metrics if m["name"] == reading.result_field), None)
    try:
        target = float(criterion["target_value"])
        tolerance = abs(float(criterion["tolerance"]))
    except (KeyError, TypeError, ValueError):
        return None
    if metric is None:
        return None
    canonical = H.CANONICAL[family]
    target_unit = reading.target_unit or reading.result_unit
    result_unit = reading.result_unit or reading.target_unit
    if target_unit is None or result_unit is None:
        return None                   # nothing says what either is in: the caller's old path
    try:
        result = H.to_canonical(family, float(metric["value"]), result_unit, formula=formula)
        centre = H.to_canonical(family, target, target_unit, formula=formula)
        tol_unit = reading.tolerance_unit or target_unit
        if H.normalize_unit(tol_unit) == H.normalize_unit(canonical) \
                or H.normalize_unit(tol_unit) in H._LOG_MOLAR:
            low, high = centre.value - tolerance, centre.value + tolerance
            band = f"tolerance {tolerance:g} {canonical}"
        else:
            # The band is target +/- tolerance in the target's own unit, mapped
            # end by end (a log scale isn't linear, so the tolerance can't be).
            lo_raw = target - tolerance
            low = (H.to_canonical(family, lo_raw, tol_unit, formula=formula).value
                   if lo_raw > 0 or family != "aqueous_solubility" else -math.inf)
            high = H.to_canonical(family, target + tolerance, tol_unit, formula=formula).value
            band = f"{target:g} +/- {tolerance:g} {tol_unit} -> {low:.4g} to {high:.4g} {canonical}"
    except H.NotConvertible:
        return None
    width_low, width_high = centre.value - low, high - centre.value
    if low <= result.value <= high:
        status, gap = "accepted", 0.0
    elif (result.value < low and low - result.value <= width_low) or \
            (result.value > high and result.value - high <= width_high):
        status = "needs_review"
        gap = 1.0 + (low - result.value) / width_low if result.value < low \
            else 1.0 + (result.value - high) / width_high
    else:
        status = "rejected"
        span = width_low if result.value < low else width_high
        gap = (abs(result.value - centre.value) / span) if span and math.isfinite(span) else math.inf
    working = ([f"result {metric['name']}: " + "; ".join(result.chain)]
               + [f"target {criterion.get('target_text') or target}: " + "; ".join(centre.chain)]
               + [band] + reading.notes)
    return TargetCheck(str(criterion.get("metric_name")), status, result.value, centre.value,
                       low, high, canonical, working, asdict(reading), gap)
