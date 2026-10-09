"""The researcher's target, read and compared like with like (#237).

Replays run 52019085's real result (aspirin, ESOL: 0.0101886 mol/L and
logS_log10_mol_per_L = -1.99) against targets written the ways people write
them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import statemachine as SM
from cross_validation import target_check as T

RUN = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                  / "run_52019085_normalized.json").read_text())
METRICS = [RUN["primary_metric"], *RUN["secondary_metrics"]]
PICK = SM.StateMachine._pick_metric
FAMILY, FORMULA = "aqueous_solubility", "C9H8O4"


def _check(target_value, tolerance, target_text=None, tolerance_text=None, agent=None):
    criterion = {"metric_name": "aqueous_solubility_at_25C", "target_value": target_value,
                 "tolerance": tolerance, "target_text": target_text,
                 "tolerance_text": tolerance_text}
    reading = T.read_target(criterion, METRICS, FAMILY, formula=FORMULA, agent=agent,
                            pick_field=PICK)
    return T.compare(criterion, METRICS, FAMILY, reading, formula=FORMULA)


def test_numbers_in_typed_text():
    assert T.numbers_in("log S = −1.72") == [-1.72]
    assert T.numbers_in("In water, 4,600 mg/L at 25 °C") == [4600.0, 25.0]


def test_log_s_target_against_a_mol_per_l_result():
    # What the researcher meant on run ec48cda0: log S -1.72 +/- 0.75 log units.
    c = _check(-1.72, 0.75, "log S = −1.72", "0.75 log units")
    assert c.status == "accepted" and round(c.result, 2) == -1.99 and c.unit == "log10(mol/L)"
    assert (round(c.low, 2), round(c.high, 2)) == (-2.47, -0.97)
    assert any("0.0101886 mol/L" in step or "0.010189 mol/L" in step for step in c.working)


def test_a_unitless_target_takes_the_results_unit():
    # Run 52019085 as approved: 0.0101886 +/- 0.005, nothing else typed.
    c = _check(0.0101886, 0.005, "0.0101886", "0.005")
    assert c.status == "accepted"
    assert (round(c.low, 3), round(c.high, 3)) == (-2.285, -1.818)   # the band, end by end


def test_a_mg_per_l_target_outside_its_band_is_rejected():
    # HSDB's 4,600 mg/L with a tight +/- 1,000 mg/L: log S -1.70 to -1.51.
    c = _check(4600, 1000, "4600 mg/L", "1000 mg/L")
    assert c.status == "rejected" and round(c.high, 2) == -1.51


class TestTheLlmReading:
    GOOD = json.dumps({"target_unit": "log10(mol/L)", "tolerance_unit": "log10(mol/L)",
                       "result_field": "logS_log10_mol_per_L", "result_unit": None,
                       "reason": "log S is log10 of mol/L"})

    def test_a_checked_reading_is_used(self):
        c = _check(-1.72, 0.75, "log S = −1.72", "0.75 log units", agent=lambda p: self.GOOD)
        assert c.reading["source"] == "llm" and c.reading["result_field"] == "logS_log10_mol_per_L"
        assert c.status == "accepted"

    @pytest.mark.parametrize("change, why", [
        ({"result_field": "made_up_field"}, "doesn't exist"),
        ({"target_unit": "furlongs"}, "doesn't convert"),
    ])
    def test_a_reading_that_fails_a_check_is_refused(self, change, why):
        answer = json.dumps({**json.loads(self.GOOD), **change})
        c = _check(-1.72, 0.75, "log S = −1.72", "0.75 log units", agent=lambda p: answer)
        assert c.reading["source"] == "rules"
        assert any(why in note for note in c.reading["notes"])

    def test_the_number_must_be_the_one_typed(self):
        c = _check(-1.72, 0.75, "log S = −1.80", "0.75 log units", agent=lambda p: self.GOOD)
        assert any("isn't the one the researcher typed" in n for n in c.reading["notes"])

    def test_garbage_is_refused(self):
        c = _check(-1.72, 0.75, "log S = −1.72", "0.75 log units", agent=lambda p: "sure!")
        assert c.reading["source"] == "rules"


