"""Unit tests for Story 4.4: plan synthesis, cost estimation, risk assessment, validation.

Covers:
  * CostEstimator: tokens, CPU-hours, and USD calculations.
  * RiskAssessor: maturity/reproducibility/license/capability flags + score bounds.
  * PlanSynthesizer: composes a schema-valid ExecutionPlan from a ranked candidate.
  * PlanValidator: cost/time/input-format/acceptance feasibility checks.

Run from the repo root with:  pixi run pytest tests/unit/test_plan_synthesis.py
"""
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from method_discovery.registry_loader import RegistryEntry
from method_discovery.scorers import DiscoveryQuery, score_entry
from plan_synthesizer.cost_estimator import CostEstimator, CostModel
from plan_synthesizer.execution_plan import ExecutionPlan
from plan_synthesizer.plan_synthesizer import PlanSynthesizer, version_to_number
from plan_synthesizer.plan_validator import PlanValidator
from plan_synthesizer.risk_assessor import RiskAssessor

REPO_ROOT = Path(__file__).resolve().parents[2]
EXEC_PLAN_SCHEMA = REPO_ROOT / "schemas" / "execution_plan.schema.json"


def make_entry(**overrides) -> RegistryEntry:
    base = dict(
        id="rdkit",
        name="RDKit",
        version="2024.03.5",
        description="Cheminformatics toolkit.",
        capability_tags=["property_prediction"],
        input_formats=["SMILES", "SDF"],
        output_properties=["logP"],
        license="BSD-3-Clause",
        license_class="permissive",
        maturity="stable",
        trust_tier=1,
        stars=2800,
        citations=1200,
        paper_doi="10.0/x",
        has_tests=True,
        has_examples=True,
    )
    base.update(overrides)
    return RegistryEntry(**base)


def make_candidate(**overrides):
    entry = make_entry(**overrides)
    q = DiscoveryQuery(capability_tags=["property_prediction"])
    cand = score_entry(entry, q)
    cand.rank = 1
    return cand


ACCEPTANCE = [{"metric_name": "MSE", "target_value": 0.5, "tolerance": 0.1}]


# --------------------------------------------------------------------------- #
# version_to_number
# --------------------------------------------------------------------------- #
class TestVersionToNumber:
    @pytest.mark.parametrize(
        "version,expected",
        [("2.8.0", 2.8), ("2024.03.5", 2024.3), ("6", 6.0), ("unknown", 1.0), ("v1.2", 1.2)],
    )
    def test_parsing(self, version, expected):
        assert version_to_number(version) == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# CostEstimator
# --------------------------------------------------------------------------- #
class TestCostEstimator:
    def test_tokens_sum(self):
        est = CostEstimator(CostModel(base_synthesis_tokens=2000, clarification_tokens=6000))
        assert est.estimate_tokens() == 8000
        assert est.estimate_tokens(extra_tokens=1000) == 9000

    def test_cpu_hours(self):
        est = CostEstimator()
        # 10 minutes on 8 cpus -> 10/60*8 = 1.3333 cpu-hours
        assert est.estimate_cpu_hours(wall_minutes=10, cpu_count=8) == pytest.approx(1.3333, abs=1e-3)

    def test_estimate_full_breakdown(self):
        model = CostModel(
            usd_per_1k_tokens=0.01,
            usd_per_cpu_hour=0.05,
            base_synthesis_tokens=2000,
            clarification_tokens=6000,
        )
        est = CostEstimator(model)
        b = est.estimate(wall_minutes=10, cpu_count=8)
        assert b.tokens == 8000
        # usd = 8000/1000*0.01 + 1.3333*0.05 = 0.08 + 0.0667 = 0.1467
        assert b.usd == pytest.approx(0.1467, abs=1e-3)

    def test_cpu_count_must_be_positive(self):
        with pytest.raises(ValueError):
            CostEstimator().estimate_cpu_hours(wall_minutes=10, cpu_count=0)


# --------------------------------------------------------------------------- #
# RiskAssessor
# --------------------------------------------------------------------------- #
class TestRiskAssessor:
    def test_stable_well_evidenced_low_risk(self):
        r = RiskAssessor().assess(make_entry())
        assert r.score == pytest.approx(0.0)
        assert r.notes == []

    def test_deprecated_flagged(self):
        r = RiskAssessor().assess(make_entry(maturity="deprecated"))
        assert r.score > 0
        assert any("deprecated" in n.lower() for n in r.notes)

    def test_low_reproducibility_flagged(self):
        r = RiskAssessor().assess(
            make_entry(paper_doi=None, has_tests=False, has_examples=False)
        )
        assert any("reproducibility" in n.lower() for n in r.notes)

    def test_capability_mismatch_flagged(self):
        r = RiskAssessor().assess(
            make_entry(capability_tags=["molecular_dynamics"]),
            requested_capability="property_prediction",
        )
        assert any("not tagged" in n.lower() for n in r.notes)

    def test_score_clamped_to_one(self):
        r = RiskAssessor().assess(
            make_entry(
                maturity="deprecated",
                paper_doi=None,
                has_tests=False,
                trust_tier=3,
                license_class="restrictive",
                capability_tags=["x"],
            ),
            requested_capability="property_prediction",
        )
        assert r.score == 1.0


# --------------------------------------------------------------------------- #
# PlanSynthesizer
# --------------------------------------------------------------------------- #
class TestPlanSynthesizer:
    @pytest.fixture
    def schema(self):
        with open(EXEC_PLAN_SCHEMA, "r") as f:
            return json.load(f)

    def test_produces_execution_plan(self):
        plan = PlanSynthesizer().synthesize(
            make_candidate(), goal_id="goal-1", acceptance_metrics=ACCEPTANCE
        )
        assert isinstance(plan, ExecutionPlan)
        assert plan.selected_method.tool_name == "RDKit"
        assert plan.metadata.goal_id == "goal-1"
        assert plan.metadata.candidate_rank == 1

    def test_plan_matches_schema(self, schema):
        plan = PlanSynthesizer().synthesize(
            make_candidate(), goal_id="goal-1", acceptance_metrics=ACCEPTANCE
        )
        Draft202012Validator(schema).validate(asdict(plan))

    def test_safety_notes_include_risk_score(self):
        plan = PlanSynthesizer().synthesize(
            make_candidate(maturity="deprecated"),
            goal_id="goal-1",
            acceptance_metrics=ACCEPTANCE,
            requested_capability="property_prediction",
        )
        assert any("risk score" in n.lower() for n in plan.safety_notes)
        assert any("deprecated" in n.lower() for n in plan.safety_notes)

    def test_rejects_non_candidate(self):
        with pytest.raises(ValueError):
            PlanSynthesizer().synthesize(
                make_entry(), goal_id="goal-1", acceptance_metrics=ACCEPTANCE
            )

    def test_timestamp_passthrough(self):
        plan = PlanSynthesizer().synthesize(
            make_candidate(),
            goal_id="goal-1",
            acceptance_metrics=ACCEPTANCE,
            timestamp="2026-06-15T12:00:00Z",
        )
        assert plan.metadata.timestamp == "2026-06-15T12:00:00Z"


# --------------------------------------------------------------------------- #
# PlanValidator
# --------------------------------------------------------------------------- #
class TestPlanValidator:
    @pytest.fixture
    def plan(self):
        return PlanSynthesizer().synthesize(
            make_candidate(), goal_id="goal-1", acceptance_metrics=ACCEPTANCE,
            wall_minutes=10, cpu_count=8,
        )

    def test_valid_plan_passes(self, plan):
        result = PlanValidator(max_cost_usd=10.0, wall_clock_limit_hours=24.0).validate_plan(plan)
        assert result.is_valid
        assert result.errors == []

    def test_over_budget_rejected(self, plan):
        result = PlanValidator(max_cost_usd=0.01).validate_plan(plan)
        assert not result.is_valid
        assert any("budget" in e.lower() for e in result.errors)

    def test_over_time_rejected(self, plan):
        result = PlanValidator(wall_clock_limit_hours=0.01).validate_plan(plan)
        assert not result.is_valid
        assert any("wall-clock" in e.lower() for e in result.errors)

    def test_input_format_mismatch_rejected(self, plan):
        result = PlanValidator(
            required_input_format="PDB", tool_input_formats=["SMILES", "SDF"]
        ).validate_plan(plan)
        assert not result.is_valid
        assert any("ingest" in e.lower() for e in result.errors)

    def test_input_format_match_ok(self, plan):
        result = PlanValidator(
            required_input_format="smiles", tool_input_formats=["SMILES", "SDF"]
        ).validate_plan(plan)
        assert result.is_valid

    def test_missing_acceptance_metrics_rejected(self):
        plan = PlanSynthesizer().synthesize(
            make_candidate(), goal_id="goal-1", acceptance_metrics=[]
        )
        result = PlanValidator().validate_plan(plan)
        assert not result.is_valid
        assert any("acceptance" in e.lower() for e in result.errors)

    def test_validate_or_raise(self, plan):
        with pytest.raises(ValueError):
            PlanValidator(max_cost_usd=0.0).validate_or_raise(plan)
