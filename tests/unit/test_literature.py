"""Literature values from sources, checked against the text (#238).

PubChem's real aspirin record (CID 2244, saved in tests/fixtures), read the way
a person would: water, 25 °C, measured values only, every one on one scale.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from cross_validation import literature as L
from cross_validation.baseline_validator import BaselineDB

FIX = Path(__file__).resolve().parents[1] / "fixtures"
SOLUBILITY = json.loads((FIX / "pubchem_2244_solubility.json").read_text())


def fake_fetch(url):
    if "/cids/JSON" in url:
        if "aspirin" in url.lower() or "CC%28%3DO%29Oc1ccccc1C%28%3DO%29O" in url:
            return {"IdentifierList": {"CID": [2244]}}
        raise OSError("404")
    if "/2244/JSON?heading=Solubility" in url:
        return SOLUBILITY
    raise AssertionError(url)


def _records():
    return L.fetch_records(2244, "Solubility", fetch=fake_fetch)


def test_every_statement_is_fetched_with_its_source():
    records = _records()
    assert len(records) == 6
    assert {r.source for r in records} >= {"Hazardous Substances Data Bank (HSDB)", "DrugBank",
                                           "The National Institute for Occupational Safety and Health (NIOSH)"}
    assert any(r.text == "10 mg/mL" for r in records)          # a structured entry, as text
    assert all(r.url.startswith("https://") for r in records)


class TestReadByRules:
    """No LLM: the common phrasings."""

    def test_the_comparable_values_and_why_the_others_are_not(self):
        a = L.assess(_records(), "aqueous_solubility", formula="C9H8O4")
        comparable = sorted(round(v.value, 2) for v in a.comparable)
        assert comparable == [-1.78, -1.73, -1.59]            # NIOSH 0.3 %, HSDB 1 g/300 mL, 4,600 mg/L
        reasons = {v.text: v.reason for v in a.values if not v.comparable}
        assert "bound" in reasons["less than 1 mg/mL at 73 °F (NTP, 1992)"]
        assert reasons["10 mg/mL"] == "no temperature stated"
        assert round(a.median, 2) == -1.73 and (round(a.low, 2), round(a.high, 2)) == (-1.78, -1.59)

    def test_the_working_is_kept(self):
        a = L.assess(_records(), "aqueous_solubility", formula="C9H8O4")
        hsdb = next(v for v in a.comparable if "300 mL" in v.text)
        assert hsdb.working[0] == "1 g in 300 mL = 3.333 g/L"


class TestReadByAnLlm:
    def _answer(self, items):
        return lambda prompt: json.dumps(items)

    def _index(self, text):
        return next(i for i, r in enumerate(_records()) if r.text.startswith(text))

    def test_a_faithful_reading_is_used(self):
        items = [{"index": self._index("In water, 4,600"), "value": 4600, "unit": "mg/L",
                  "temperature": "25 °C", "qualifier": "=", "solvent": "water"},
                 {"index": self._index("Solubility in water, g/100ml"), "value": 0.25,
                  "unit": "g/100ml", "temperature": "15 °C", "qualifier": "=", "solvent": "water"}]
        a = L.assess(_records(), "aqueous_solubility", formula="C9H8O4",
                     agent=self._answer(items))
        assert a.how_read == "llm"
        assert [round(v.value, 2) for v in a.comparable] == [-1.59]
        icsc = next(v for v in a.values if "g/100ml" in v.text)
        assert icsc.reason == "measured at 15 °C, not 25 °C"

    @pytest.mark.parametrize("item, why", [
        ({"value": 3.33, "unit": "g/L", "temperature": "25 °C"}, "not written"),     # arithmetic
        ({"value": -1.72, "unit": "log10(mol/L)", "temperature": "25 °C"}, "not written"),  # memory
        ({"value": 4600, "unit": "mg/L", "temperature": "20 °C"}, "temperature is not written"),
    ])
    def test_anything_not_in_the_text_is_refused(self, item, why):
        index = self._index("In water, 4,600")
        a = L.assess(_records(), "aqueous_solubility", formula="C9H8O4",
                     agent=self._answer([{"index": index, "qualifier": "=", "solvent": "water",
                                          **item}]))
        hsdb = next(v for v in a.values if v.text.startswith("In water, 4,600"))
        assert not hsdb.comparable and why in hsdb.reason

    def test_an_unreadable_answer_falls_back_to_the_rules(self):
        a = L.assess(_records(), "aqueous_solubility", formula="C9H8O4", agent=lambda p: "hmm")
        assert a.how_read.startswith("rules") and len(a.comparable) == 3


class TestAsAReferenceForValidate:
    def test_the_median_of_all_sources_including_the_curated_one(self):
        lit = L.LiteratureBaselines(smiles="CC(=O)Oc1ccccc1C(=O)O", name="aspirin",
                                    formula="C9H8O4", curated=BaselineDB.load(), fetch=fake_fetch)
        record = lit.lookup("aspirin", "logS")
        # -1.78, -1.73, -1.72 (Delaney, curated), -1.59
        assert round(record.literature_value, 3) == -1.726        # mean of the middle two
        assert record.unit == "log10(mol/L)" and "4 literature value(s)" in record.literature_source
        assert lit.assessments["aqueous_solubility"].as_dict()["comparable_count"] == 4

    def test_pubchem_unreachable_leaves_the_curated_value(self):
        def down(url):
            raise OSError("network down")
        lit = L.LiteratureBaselines(name="aspirin", formula="C9H8O4", curated=BaselineDB.load(),
                                    fetch=down)
        assert lit.lookup("aspirin", "logS").literature_value == -1.72

    def test_a_property_with_no_source_is_not_answered(self):
        lit = L.LiteratureBaselines(name="aspirin", fetch=fake_fetch)
        assert lit.lookup("aspirin", "band_gap") is None


CAFFEINE = json.loads((FIX / "pubchem_2519_solubility.json").read_text())


def _caffeine_records():
    return L.fetch_records(2519, "Solubility", fetch=lambda url: CAFFEINE)


def test_scientific_notation_is_one_number():
    assert L._numbers("In water, 2.16X10+4 mg/L at 25 °C") == [21600.0, 25.0]
    assert L._numbers("2.16×10⁴ mg/L") == [21600.0]
    assert L._numbers("1.5 x 10-3 M") == [0.0015]


def test_caffeine_read_the_way_a_person_would():
    # Live PubChem caught two misreadings: "2.16X10+4 mg/L" read as 4 mg/L
    # (log S -4.69), and "10 to 50 mg/mL" read as 50 mg/mL.
    a = L.assess(_caffeine_records(), "aqueous_solubility", formula="C8H10N4O2")
    comparable = sorted(round(v.value, 2) for v in a.comparable)
    assert comparable == [-0.95, -0.95]                     # HSDB 2.16e4 mg/L, HMDB 21.6 mg/mL
    cameo = next(v for v in a.values if v.text.startswith("10 to 50"))
    assert cameo.reason == "a range, not one measured value"


def test_a_misread_exponent_from_an_llm_is_refused():
    index = next(i for i, r in enumerate(_caffeine_records()) if "2.16X10+4" in r.text)
    answer = json.dumps([{"index": index, "value": 4, "unit": "mg/L", "temperature": "25 °C",
                          "qualifier": "=", "solvent": "water"}])
    a = L.assess(_caffeine_records(), "aqueous_solubility", formula="C8H10N4O2",
                 agent=lambda p: answer)
    hsdb = next(v for v in a.values if "2.16X10+4" in v.text)
    assert not hsdb.comparable and "not written" in hsdb.reason
