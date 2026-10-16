"""Literature values from sources, never from memory (#238).

VALIDATE needs the experimental value a result should agree with. The curated
``configs/baselines.json`` covers 14 molecules; PubChem holds measured values,
with their references, for far more -- but as text written by many hands:
aspirin's solubility is "In water, 4,600 mg/L at 25 °C" (HSDB), "1 g sol in:
300 mL water at 25 °C, ... 5 mL alcohol" (HSDB), "(77 °F): 0.3%" (NIOSH), "less
than 1 mg/mL at 73 °F" (CAMEO), "g/100ml at 15 °C: 0.25" (ICSC) and a bare
"10 mg/mL" (DrugBank).

A person reads each one, keeps those measured in water at the requested
temperature, puts them on one scale and looks at the spread. Here:

* :func:`fetch_records` pulls the records and their references from PubChem.
* :func:`extract` reads each into {value, unit, temperature, qualifier} -- an
  LLM when available, rules otherwise -- and **code checks every reading**:
  each number it used must appear verbatim in that record's text, the unit
  must convert, the temperature must be stated. Nothing comes from a model's
  memory; a reading that fails a check is dropped, with the reason.
* :func:`assess` puts the comparable values on the canonical scale
  (harmonize.py) and reports all of them -- comparable or not, and why --
  with the median and the range. No value is preferred over another.

:class:`LiteratureBaselines` serves the result to VALIDATE like a baseline DB.
"""
from __future__ import annotations

import json
import logging
import math
import re
import statistics
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from cross_validation import harmonize as H
from cross_validation.baseline_validator import BaselineRecord

log = logging.getLogger("twain.literature")

PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUG_VIEW = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound"
TIMEOUT_SECONDS = 15
_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "literature_sources.json"


def sources_config() -> dict:
    try:
        return json.loads(_CONFIG.read_text(encoding="utf-8")).get("families") or {}
    except (OSError, ValueError):
        return {}


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "TWAIN (WashU)",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310 - fixed host
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------------------------------ fetch
@dataclass
class Record:
    """One statement in a source, as published."""
    text: str
    source: str
    url: str = ""
    reference: str = ""


def resolve_cid(*, smiles: str | None = None, name: str | None = None,
                fetch: Callable[[str], dict] = _http_json) -> Optional[int]:
    """PubChem's compound id for the run's molecule (SMILES first, then name)."""
    for kind, value in (("smiles", smiles), ("name", name)):
        if not value:
            continue
        try:
            data = fetch(f"{PUG}/compound/{kind}/{urllib.parse.quote(str(value), safe='')}/cids/JSON")
        except Exception as exc:  # noqa: BLE001 - unknown or unreachable: try the next key
            log.info("[literature] no PubChem CID for %s %r: %s", kind, value, exc)
            continue
        cids = (data.get("IdentifierList") or {}).get("CID") or []
        if cids:
            return int(cids[0])
    return None


def fetch_records(cid: int, heading: str, *, fetch: Callable[[str], dict] = _http_json) -> list:
    """Every statement under ``heading`` for ``cid``, with its reference."""
    data = fetch(f"{PUG_VIEW}/{cid}/JSON?heading={urllib.parse.quote(heading)}")
    record = data.get("Record") or {}
    refs = {r.get("ReferenceNumber"): r for r in record.get("Reference") or []}
    out = []

    def walk(node):
        if isinstance(node, dict):
            for info in node.get("Information") or []:
                ref = refs.get(info.get("ReferenceNumber")) or {}
                value = info.get("Value") or {}
                texts = [s.get("String") for s in value.get("StringWithMarkup") or []
                         if s.get("String")]
                if not texts and value.get("Number"):
                    # A structured entry: "10 mg/mL" is the text it stands for.
                    texts = [" ".join([*(f"{n:g}" for n in value["Number"]),
                                       value.get("Unit") or ""]).strip()]
                for text in texts:
                    out.append(Record(text, ref.get("SourceName") or "PubChem",
                                      ref.get("URL") or f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
                                      str(ref.get("Name") or "")))
            for key, child in node.items():
                if key not in ("Information", "Reference"):
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(record)
    return out


# ------------------------------------------------------------------ extract
@dataclass
class Reading:
    """What one record says, as read; ``numbers`` are the values it was built from."""
    index: int
    value: float | None
    unit: str | None
    temperature_c: float | None
    qualifier: str = "="                   # =, ~, <, >, <=, >=
    solvent: str | None = None
    per_volume_ml: float | None = None     # "1 g in 300 mL": value 1 g, per 300 mL
    temperature_text: str | None = None    # the temperature as written ("77 °F")


_NUM = re.compile(r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?|[-+]?\.\d+")
# "2.16X10+4", "2.16x10^4", "2.16×10⁴", "2.16 x 10-3": one number, not three.
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
_SCI = re.compile(r"(\d(?:\.\d+)?)\s*[xX×*]\s*10\s*\^?\s*\(?\s*([+\-]?\s*\d+)\s*\)?")


def normalize_text(text: str) -> str:
    """Text with its numbers in one plain form: scientific notation collapsed
    ("2.16X10+4" -> "2.16e+4"), minus signs plain. Reading AND checking use it,
    so a stray "4" from "10+4" is never taken as a value written there."""
    t = str(text or "").translate(_SUPERSCRIPT).replace("−", "-")
    return _SCI.sub(lambda m: f"{m.group(1)}e{m.group(2).replace(' ', '')}", t)


def _numbers(text: str) -> list:
    out = []
    for token in _NUM.findall(normalize_text(text)):
        try:
            out.append(float(token.replace(",", "")))
        except ValueError:
            continue
    return out


def _verbatim(number, text) -> bool:
    return number is not None and any(
        math.isclose(float(number), n, rel_tol=1e-9, abs_tol=1e-12) for n in _numbers(text))


_TEMP = re.compile(r"(?:at\s*|\()?\s*([-+]?\d+(?:\.\d+)?)\s*°?\s*([CF])\b", re.IGNORECASE)


def _celsius(value: float, scale: str) -> float:
    return (value - 32.0) * 5.0 / 9.0 if scale.upper() == "F" else value


def _rules_read(index: int, text: str) -> Optional[Reading]:
    """The common phrasings, without an LLM. Returns None when unsure."""
    t = normalize_text(text)
    qualifier = "<" if re.search(r"\bless than\b|<", t, re.I) else (
        ">" if re.search(r"\bgreater than\b|>", t, re.I) else "=")
    # "10 to 50 mg/mL", "10-15 mL": a range is not one measured value.
    if re.search(r"\d\s*(?:to|–)\s*\d+(?:\.\d+)?\s*(?:mg|g|ug|µg|mol|%)", t, re.I):
        qualifier = "range"
    temp = _TEMP.search(t)
    temperature = _celsius(float(temp.group(1)), temp.group(2)) if temp else None
    temp_text = temp.group(0).strip(" ()") if temp else None
    # "1 g sol in: 300 mL water at 25 °C"
    m = re.search(r"([\d.]+)\s*(?:g|gm|gram)\s+(?:sol(?:uble)?|dissolves)\s+in:?\s*([\d.,]+)\s*"
                  r"mL\s+(?:of\s+)?water(?:\s+at\s+([\d.]+)\s*°?\s*([CF]))?", t, re.I)
    if m:
        tc = _celsius(float(m.group(3)), m.group(4)) if m.group(3) else temperature
        return Reading(index, float(m.group(1)), "g", tc, qualifier, "water",
                       float(m.group(2).replace(",", "")),
                       f"{m.group(3)} °{m.group(4)}" if m.group(3) else temp_text)
    # "In water, 4,600 mg/L at 25 °C"; "less than 1 mg/mL at 73 °F"
    m = re.search(r"([-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?)\s*"
                  r"(mg/L|g/L|mg/mL|ug/mL|µg/mL|g/100\s*mL|mol/L|M|%)(?![a-z])", t, re.I)
    if m:
        solvent = "water" if re.search(r"\bwater\b", t, re.I) else None
        return Reading(index, float(m.group(1).replace(",", "")), m.group(2).replace(" ", ""),
                       temperature, qualifier, solvent, None, temp_text)
    # "(77 °F): 0.3%"
    m = re.search(r":\s*([\d.]+)\s*%", t)
    if m:
        return Reading(index, float(m.group(1)), "%", temperature, qualifier, None, None, temp_text)
    return None


_PROMPT = (
    "Below are statements from chemical databases about the {quantity} of one compound. "
    "For EACH statement about the {quantity} in {solvent}, return what it says. Reply with "
    "JSON only: a list of {{\"index\": n, \"value\": number exactly as written (no "
    "arithmetic), \"unit\": as written or null, \"per_volume_ml\": number or null (for "
    "\"1 g dissolves in 300 mL\": value 1, unit \"g\", per_volume_ml 300), \"temperature\": "
    "as written (e.g. \"25 °C\", \"77 °F\") or null, \"qualifier\": \"=\", \"~\", \"<\" or "
    "\">\", \"solvent\": as written}}. Skip statements about other solvents or other "
    "quantities. Never add a value that is not written in the statement.\n\n{records}")


def _llm_read(records: list, family: str, solvent: str, agent) -> Optional[list]:
    listing = "\n".join(f"[{i}] ({r.source}) {r.text}" for i, r in enumerate(records))
    try:
        raw = agent(_PROMPT.format(quantity=family.replace("_", " "), solvent=solvent,
                                   records=listing)) or ""
        items = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
    except Exception:  # noqa: BLE001 - an unreadable answer: the rules read instead
        return None
    readings = []
    for item in items if isinstance(items, list) else []:
        try:
            index = int(item["index"])
            temp_text = item.get("temperature")
            temp = _TEMP.search(str(temp_text or ""))
            readings.append(Reading(
                index, float(item["value"]) if item.get("value") is not None else None,
                item.get("unit"), _celsius(float(temp.group(1)), temp.group(2)) if temp else None,
                str(item.get("qualifier") or "="), item.get("solvent"),
                float(item["per_volume_ml"]) if item.get("per_volume_ml") is not None else None,
                temp_text))
        except (KeyError, TypeError, ValueError):
            continue
    return readings


def extract(records: list, family: str, *, solvent: str = "water", agent=None) -> tuple:
    """(readings, how) -- readings checked against the text they came from."""
    readings, how = None, "rules"
    if agent is not None:
        readings = _llm_read(records, family, solvent, agent)
        how = "llm" if readings is not None else "rules (the LLM's answer was unreadable)"
    if readings is None:
        readings = [r for i, rec in enumerate(records) if (r := _rules_read(i, rec.text))]
    return readings, how


# ------------------------------------------------------------------ assess
@dataclass
class Value:
    """One literature statement, and what became of it."""
    source: str
    url: str
    text: str
    comparable: bool
    reason: str = ""
    value: float | None = None          # on the canonical scale, when comparable
    unit: str | None = None
    working: list = field(default_factory=list)


@dataclass
class Assessment:
    family: str
    unit: str
    values: list
    median: float | None = None
    low: float | None = None
    high: float | None = None
    how_read: str = ""

    @property
    def comparable(self) -> list:
        return [v for v in self.values if v.comparable]

    def as_dict(self) -> dict:
        return {**asdict(self), "comparable_count": len(self.comparable)}

    def summary(self) -> str:
        sources = sorted({v.source for v in self.comparable})
        return (f"{len(self.comparable)} literature value(s), median {self.median:.3g} "
                f"(range {self.low:.3g} to {self.high:.3g}) {self.unit}, from "
                + ", ".join(sources))


def _check(reading: Reading, record: Record, family: str, conditions: dict,
           solvent: str, formula: str | None) -> Value:
    base = Value(record.source, record.url, record.text, False)
    # Every number the reading used must be written in the statement.
    if reading.value is None or not _verbatim(reading.value, record.text):
        base.reason = f"the value {reading.value} is not written in the statement"
        return base
    if reading.per_volume_ml is not None and not _verbatim(reading.per_volume_ml, record.text):
        base.reason = f"the volume {reading.per_volume_ml:g} mL is not written in the statement"
        return base
    if reading.qualifier == "range":
        base.reason = "a range, not one measured value"
        return base
    if reading.qualifier not in ("=", "~", "≈", "about"):
        base.reason = f"a bound ({reading.qualifier} {reading.value:g}), not a measured value"
        return base
    if reading.solvent and solvent and solvent.lower() not in str(reading.solvent).lower():
        base.reason = f"in {reading.solvent}, not {solvent}"
        return base
    want = conditions.get("temperature_c")
    if want is not None:
        if reading.temperature_c is None:
            base.reason = "no temperature stated"
            return base
        written = _numbers(str(reading.temperature_text or ""))
        if not written or not _verbatim(written[0], record.text):
            base.reason = "the temperature is not written in the statement"
            return base
        if abs(reading.temperature_c - want) > float(conditions.get("tolerance_c", 3)):
            base.reason = f"measured at {reading.temperature_c:.0f} °C, not {want:g} °C"
            return base
    try:
        if reading.per_volume_ml is not None:
            grams = reading.value * {"g": 1.0, "mg": 1e-3}.get(str(reading.unit).lower(), math.nan)
            if math.isnan(grams):
                raise H.NotConvertible(f"unknown mass unit {reading.unit!r}")
            g_per_l = H.grams_in_volume(grams, reading.per_volume_ml)
            h = H.to_canonical(family, g_per_l, "g/L", formula=formula)
            h.chain.insert(0, f"{reading.value:g} {reading.unit} in {reading.per_volume_ml:g} mL "
                              f"= {g_per_l:.4g} g/L")
        else:
            h = H.to_canonical(family, reading.value, reading.unit, formula=formula)
    except H.NotConvertible as exc:
        base.reason = f"not convertible: {exc}"
        return base
    return Value(record.source, record.url, record.text, True, "", h.value, h.unit, h.chain)


def assess(records: list, family: str, *, formula: str | None = None, agent=None,
           extra: list | None = None) -> Assessment:
    """Every literature value for ``family``, checked, harmonized and summarized.

    ``extra`` adds already-sourced values (the curated table's) as more voices,
    never as overrides: ``[(value_canonical, source, url, text)]``.
    """
    cfg = sources_config().get(family) or {}
    conditions, solvent = cfg.get("conditions") or {}, cfg.get("solvent") or "water"
    readings, how = extract(records, family, solvent=solvent, agent=agent)
    values, used = [], set()
    for reading in readings:
        if not 0 <= reading.index < len(records):
            continue
        used.add(reading.index)
        values.append(_check(reading, records[reading.index], family, conditions, solvent,
                             formula))
    for i, record in enumerate(records):
        if i not in used:
            values.append(Value(record.source, record.url, record.text, False,
                                f"not a {family.replace('_', ' ')} in {solvent} that could be read"))
    for value, source, url, text in extra or []:
        values.append(Value(source, url, text, True, "", value, H.CANONICAL[family],
                            ["from the curated reference table"]))
    a = Assessment(family, H.CANONICAL[family], values, how_read=how)
    numbers = [v.value for v in a.comparable]
    if numbers:
        a.median, a.low, a.high = statistics.median(numbers), min(numbers), max(numbers)
    return a


# ------------------------------------------------------------------ VALIDATE
class LiteratureBaselines:
    """Sourced literature for ONE molecule, served like a ``BaselineDB``.

    ``lookup`` answers with the median of the comparable values; the full
    :class:`Assessment` (every value, its source and why it counted or not) is
    kept on ``assessments`` for the report.
    """

    def __init__(self, *, smiles=None, name=None, formula=None, agent=None, curated=None,
                 fetch: Callable[[str], dict] = _http_json):
        self.smiles, self.name, self.formula = smiles, name, formula
        self.agent, self.curated, self.fetch = agent, curated, fetch
        self.assessments: dict = {}
        self._cid = None

    def __len__(self) -> int:
        return len(self.assessments)

    def lookup(self, molecule: str, prop: str) -> Optional[BaselineRecord]:
        config = {c.get("baseline_property"): (family, c) for family, c in sources_config().items()}
        family, cfg = config.get(prop, (None, None))
        if family is None or family not in H.CANONICAL:
            return None
        if family not in self.assessments:
            self.assessments[family] = self._assess(molecule, prop, family, cfg)
        a = self.assessments[family]
        if a is None or a.median is None:
            return None
        return BaselineRecord(molecule=molecule, property=prop, literature_value=a.median,
                              literature_source=a.summary(), unit=a.unit)

    def _assess(self, molecule, prop, family, cfg) -> Optional[Assessment]:
        extra = []
        if self.curated is not None:
            rec = self.curated.lookup(molecule, prop)
            if rec is not None:
                extra.append((rec.literature_value, rec.literature_source or "curated table",
                              rec.doi or "", f"{rec.literature_value:g} {rec.unit or ''}".strip()))
        try:
            if self._cid is None:
                self._cid = resolve_cid(smiles=self.smiles, name=self.name or molecule,
                                        fetch=self.fetch)
            records = (fetch_records(self._cid, cfg["pubchem_heading"], fetch=self.fetch)
                       if self._cid else [])
        except Exception as exc:  # noqa: BLE001 - unreachable: the curated table alone
            log.warning("[literature] PubChem unavailable for %s: %s", molecule, exc)
            records = []
        if not records and not extra:
            return None
        return assess(records, family, formula=self.formula, agent=self.agent, extra=extra)
