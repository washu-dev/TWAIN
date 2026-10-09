"""Values written with their units (run ec48cda0): read as numbers, not lost.

The same ESOL result came out as a float in one run (4e51dd7d, completed) and
as "-1.9919 log10(mol/L)" in the next (ec48cda0, "no finite value"). How the
generated script formats its summary is not the researcher's problem.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import statemachine as SM
from result_interpreter.extractors import base
from result_interpreter.extractors.base import get_parser

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.mark.parametrize("text, expected", [
    ("-1.9919 log10(mol/L)", (-1.9919, "log10(mol/L)")),
    ("1.3101 (dimensionless)", (1.3101, "dimensionless")),
    ("180.1590 g/mol", (180.159, "g/mol")),
    ("2 (count)", (2.0, "count")),
    ("−0.6 eV", (-0.6, "eV")),
    ("1e-3 mol/L", (0.001, "mol/L")),
    ("42", (42.0, None)),
])
def test_a_number_with_its_unit(text, expected):
    assert base.quantity(text) == expected


@pytest.mark.parametrize("text", ["C9H8O4", "rdkit (Crippen logP)", "1 to 2", "between 1 and 2",
                                  "False", "", "3 4"])
def test_anything_else_is_not_a_value(text):
    assert base.quantity(text) is None


def test_run_ec48cda0_is_read():
    m = SM.StateMachine.__new__(SM.StateMachine)
    artifacts = {"execution_plan": {"acceptance_metrics": [
        {"metric_name": "aqueous_solubility_at_25C"}]}, "intent_spec": {}}
    m._load_artifact = lambda name: artifacts.get(name)
    stdout = (FIXTURES / "run_ec48cda0_stdout.txt").read_text()
    metric = m._normalize_run_output({"stdout": stdout, "succeeded": True}).primary_metric
    assert metric.name == "aqueous_solubility_at_25C"
    assert round(metric.value, 4) == -1.9919 and metric.unit == "log10(mol/L)"


def test_a_csv_with_units_in_its_cells():
    parsed = get_parser("csv").parse(
        "property,aqueous_solubility_at_25C,formula\nlogS,-1.9919 log10(mol/L),C9H8O4\n")
    (field,) = parsed.fields
    assert (field.name, field.values, field.unit) == (
        "aqueous_solubility_at_25C", [-1.9919], "log10(mol/L)")
