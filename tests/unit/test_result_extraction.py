"""Unit tests for the result interpreter's parsing + normalization (Story 6.1).

Covers:
  * the four built-in parsers (csv/tsv, json, log, rdkit) over diverse inputs,
  * the pluggable parser registry,
  * uncertainty estimation (reported / convergence / repeats / fallback), and
  * metric normalization -- primary/secondary selection, unit handling, and
    cross-tool consistency (same number in, same normalized metric out).

Run from the repo root with:  pixi run pytest tests/unit/test_result_extraction.py
"""
import math

import pytest

from result_interpreter import confidence_estimator as ce
from result_interpreter.extractors.base import (
    OutputParser,
    ParsedField,
    ParsedOutput,
    ParserError,
    available_parsers,
    get_parser,
    register,
)
from result_interpreter.metric_normalizer import (
    NormalizedResult,
    interpret,
    normalize,
)


# ── registry ─────────────────────────────────────────────────────────────────

def test_builtin_parsers_are_registered():
    assert {"csv", "json", "log", "rdkit"}.issubset(set(available_parsers()))


def test_unknown_parser_raises():
    with pytest.raises(ParserError):
        get_parser("does-not-exist")


def test_registry_is_pluggable():
    class DummyExtractor(OutputParser):
        name = "dummy-test-parser"

        def parse(self, content, **options):
            return ParsedOutput(fields=[ParsedField("x", [float(content)], source=self.name)])

    register(DummyExtractor())
    assert "dummy-test-parser" in available_parsers()
    parsed = get_parser("dummy-test-parser").parse("42")
    assert parsed.get("x").values == [42.0]


# ── CSV / TSV parser ─────────────────────────────────────────────────────────

def test_csv_extracts_numeric_columns_and_units():
    text = "molecule,logS (mol/L),weight\naspirin,-1.72,180.16\ncaffeine,-0.87,194.19\n"
    out = get_parser("csv").parse(text)
    assert set(out.field_names()) == {"logS", "weight"}  # non-numeric 'molecule' dropped
    logs = out.get("logS")
    assert logs.values == [-1.72, -0.87]
    assert logs.unit == "mol/L"


def test_csv_column_selection_and_missing_column():
    text = "a,b\n1,2\n3,4\n"
    out = get_parser("csv").parse(text, columns=["b"])
    assert out.field_names() == ["b"]
    with pytest.raises(ParserError):
        get_parser("csv").parse(text, columns=["nope"])


def test_tsv_delimiter_is_sniffed():
    text = "x\ty\n1\t10\n2\t20\n"
    out = get_parser("csv").parse(text)
    assert out.get("y").values == [10.0, 20.0]
    assert out.metadata["delimiter"] == "\t"


def test_csv_empty_content_raises():
    with pytest.raises(ParserError):
        get_parser("csv").parse("   ")


# ── JSON parser ──────────────────────────────────────────────────────────────

def test_json_flattens_nested_fields():
    text = '{"result": {"logS": -1.7}, "meta": {"n": 5}}'
    out = get_parser("json").parse(text)
    assert out.get("result.logS").values == [-1.7]
    assert out.get("meta.n").values == [5.0]


def test_json_numeric_array_becomes_series():
    text = '{"loss": [1.0, 0.5, 0.25, 0.24]}'
    out = get_parser("json").parse(text)
    assert out.get("loss").values == [1.0, 0.5, 0.25, 0.24]


def test_json_field_selection_and_units():
    text = '{"a": 1, "b": 2}'
    out = get_parser("json").parse(text, fields=["b"], units={"b": "eV"})
    assert out.field_names() == ["b"]
    assert out.get("b").unit == "eV"
    with pytest.raises(ParserError):
        get_parser("json").parse(text, fields=["missing"])


def test_json_invalid_raises():
    with pytest.raises(ParserError):
        get_parser("json").parse("{not json}")


# ── log parser ───────────────────────────────────────────────────────────────

def test_log_default_key_value_pairs():
    text = "starting run\nlogS = -1.70 mol/L\nRMSE: 0.35\ndone\n"
    out = get_parser("log").parse(text)
    assert out.get("logS").values == [-1.70]
    assert out.get("logS").unit == "mol/L"
    assert out.get("RMSE").values == [0.35]


def test_log_pattern_matches_repeated_lines():
    text = "epoch 1 loss=0.9\nepoch 2 loss=0.4\nepoch 3 loss=0.11\n"
    out = get_parser("log").parse(text, patterns={"loss": r"loss=(?P<value>[-+]?\d*\.?\d+)"})
    assert out.get("loss").values == [0.9, 0.4, 0.11]


def test_log_no_match_raises():
    with pytest.raises(ParserError):
        get_parser("log").parse("nothing numeric here", patterns={"x": r"x=(\d+)"})


# ── rdkit tool-specific parser ───────────────────────────────────────────────

def test_rdkit_parses_descriptors_with_units():
    text = '{"MolWt": 180.16, "MolLogP": 1.31, "TPSA": 63.6, "name": "aspirin"}'
    out = get_parser("rdkit").parse(text)
    assert out.get("MolWt").values == [180.16]
    assert out.get("MolWt").unit == "g/mol"
    assert out.get("MolLogP").unit is None  # logP dimensionless
    assert out.get("name") is None  # non-numeric ignored


def test_rdkit_accepts_dict_and_property_subset():
    data = {"MolWt": 180.16, "TPSA": 63.6}
    out = get_parser("rdkit").parse(data, properties=["TPSA"])
    assert out.field_names() == ["TPSA"]


# ── confidence estimation ────────────────────────────────────────────────────

def test_uncertainty_from_reported_std():
    unc = ce.estimate(1.0, reported_std=0.25)
    assert unc.std == 0.25
    assert unc.method == ce.UncertaintyMethod.REPORTED_STD


def test_uncertainty_from_confidence_interval():
    # 95% CI half-width / 1.96 -> sigma
    unc = ce.estimate(1.0, confidence_interval=[0.804, 1.196])
    assert unc.method == ce.UncertaintyMethod.REPORTED_CI
    assert unc.std == pytest.approx(0.1, abs=1e-3)


def test_uncertainty_from_convergence_uses_tail():
    values = [1.0, 0.5, 0.2, 0.11, 0.101, 0.1005, 0.1001]
    unc = ce.estimate(0.1002, values)
    assert unc.method == ce.UncertaintyMethod.CONVERGENCE
    assert unc.std < 0.01  # settled tail is tight


def test_uncertainty_from_repeats_is_standard_error():
    values = [2.0, 2.1, 1.9, 2.05, 1.95]
    unc = ce.estimate(2.0, values)
    assert unc.method == ce.UncertaintyMethod.REPEATS
    assert unc.std > 0


def test_uncertainty_fallback_is_ten_percent():
    unc = ce.estimate(-1.7)
    assert unc.method == ce.UncertaintyMethod.FALLBACK
    assert unc.std == pytest.approx(0.17)


# ── normalization ────────────────────────────────────────────────────────────

def test_normalize_selects_primary_and_secondary():
    text = '{"logS": -1.7, "MolWt": 180.16, "TPSA": 63.6}'
    result = interpret(text, "json", primary="logS")
    assert isinstance(result, NormalizedResult)
    assert result.primary_metric.name == "logS"
    assert {m.name for m in result.secondary_metrics} == {"MolWt", "TPSA"}


def test_normalize_defaults_primary_to_first_field():
    out = get_parser("json").parse('{"a": 1, "b": 2}')
    result = normalize(out)
    assert result.primary_metric.name == "a"


def test_normalize_unit_override_and_reported_uncertainty():
    text = '{"logS": -1.7}'
    result = interpret(
        text, "json",
        units={"logS": "log10(mol/L)"},
        reported={"logS": {"std": 0.3}},
    )
    assert result.primary_metric.unit == "log10(mol/L)"
    assert result.primary_metric.uncertainty == 0.3
    assert result.primary_metric.uncertainty_method == "reported_std"


def test_normalize_bad_primary_raises():
    out = get_parser("json").parse('{"a": 1}')
    with pytest.raises(ParserError):
        normalize(out, primary="missing")


def test_convergent_series_value_is_tail_mean():
    text = '{"loss": [1.0, 0.5, 0.2, 0.11, 0.101, 0.1005, 0.1001]}'
    result = interpret(text, "json", primary="loss")
    assert result.primary_metric.value == pytest.approx(0.1002, abs=5e-3)
    assert result.primary_metric.uncertainty_method == "convergence"


def test_relative_uncertainty():
    text = '{"x": 2.0}'
    result = interpret(text, "json", reported={"x": {"std": 0.2}})
    assert result.primary_metric.relative_uncertainty() == pytest.approx(0.1)


def test_result_is_json_serializable():
    import json
    result = interpret('{"logS": -1.7}', "json")
    dumped = json.dumps(result.to_dict())
    assert json.loads(dumped)["primary_metric"]["name"] == "logS"


# ── cross-tool consistency ───────────────────────────────────────────────────

def test_same_value_normalizes_consistently_across_parsers():
    """A single logS reading yields the same normalized metric regardless of the
    output format it arrived in (CSV, JSON, or log)."""
    csv_result = interpret("logS\n-1.7\n", "csv", primary="logS")
    json_result = interpret('{"logS": -1.7}', "json", primary="logS")
    log_result = interpret("logS = -1.7\n", "log", primary="logS")

    for result in (csv_result, json_result, log_result):
        assert result.primary_metric.name == "logS"
        assert result.primary_metric.value == pytest.approx(-1.7)
        # no series / no reported uncertainty -> 10% fallback
        assert result.primary_metric.uncertainty == pytest.approx(0.17)
        assert result.primary_metric.uncertainty_method == "fallback"
