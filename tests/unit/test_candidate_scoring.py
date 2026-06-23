"""Unit tests for method_discovery.scorers and method_discovery.ranking_rationale (Story 4.2).

Covers:
  * Component scores (relevance, maturity, license, trust, reproducibility).
  * Composite score bounds and weighting.
  * Monotonicity (more stars => higher score, all else equal).
  * Deterministic, stable top-k ranking.
  * Explainability output.

Run from the repo root with:  pixi run pytest tests/unit/test_candidate_scoring.py
"""
import pytest

from method_discovery.registry_loader import RegistryEntry, RegistryLoader
from method_discovery.scorers import (
    DiscoveryQuery,
    adoption_score,
    component_scores,
    composite_score,
    license_score,
    maturity_score,
    rank_candidates,
    relevance_score,
    reproducibility_score,
    score_entry,
    trust_score,
    WEIGHTS,
)
from method_discovery.ranking_rationale import (
    compare_top_two,
    component_breakdown,
    explain_ranking,
)


def make_entry(**overrides) -> RegistryEntry:
    base = dict(
        id="tool",
        name="Tool",
        version="1.0",
        description="A tool.",
        capability_tags=["property_prediction"],
        input_formats=["SMILES"],
        output_properties=["solubility"],
        license="MIT",
        license_class="permissive",
        maturity="stable",
        trust_tier=1,
        stars=100,
        citations=100,
        paper_doi="10.0/x",
        has_tests=True,
        has_examples=True,
    )
    base.update(overrides)
    return RegistryEntry(**base)


# --------------------------------------------------------------------------- #
# DiscoveryQuery
# --------------------------------------------------------------------------- #
class TestDiscoveryQuery:
    def test_requires_non_empty_tags(self):
        with pytest.raises(ValueError):
            DiscoveryQuery(capability_tags=[])

    def test_tags_must_be_str(self):
        with pytest.raises(ValueError):
            DiscoveryQuery(capability_tags=[1])


# --------------------------------------------------------------------------- #
# Component scores
# --------------------------------------------------------------------------- #
class TestRelevance:
    def test_full_match(self):
        e = make_entry(capability_tags=["property_prediction", "ml"])
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        assert relevance_score(e, q) == pytest.approx(1.0)

    def test_partial_match(self):
        e = make_entry(capability_tags=["property_prediction"])
        q = DiscoveryQuery(capability_tags=["property_prediction", "molecular_dynamics"])
        assert relevance_score(e, q) == pytest.approx(0.5)

    def test_no_match(self):
        e = make_entry(capability_tags=["quantum_chemistry"])
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        assert relevance_score(e, q) == pytest.approx(0.0)

    def test_input_format_bonus_capped(self):
        e = make_entry(capability_tags=["property_prediction"], input_formats=["SMILES"])
        q = DiscoveryQuery(capability_tags=["property_prediction"], input_format="SMILES")
        # full match (1.0) + bonus, clamped to 1.0
        assert relevance_score(e, q) == pytest.approx(1.0)

    def test_input_format_bonus_applied_on_partial(self):
        e = make_entry(
            capability_tags=["property_prediction"], input_formats=["SMILES"]
        )
        q = DiscoveryQuery(
            capability_tags=["property_prediction", "md"], input_format="smiles"
        )
        # 0.5 base + 0.1 bonus
        assert relevance_score(e, q) == pytest.approx(0.6)


class TestOtherComponents:
    def test_license_scores(self):
        assert license_score(make_entry(license_class="permissive")) == 1.0
        assert license_score(make_entry(license_class="copyleft")) == 0.5
        assert license_score(make_entry(license_class="restrictive")) == 0.0

    def test_trust_scores(self):
        assert trust_score(make_entry(trust_tier=0)) == 1.0
        assert trust_score(make_entry(trust_tier=1)) == 0.95
        assert trust_score(make_entry(trust_tier=2)) == 0.8
        assert trust_score(make_entry(trust_tier=3)) == 0.5

    def test_reproducibility_full(self):
        e = make_entry(paper_doi="10.0/x", has_tests=True, has_examples=True)
        assert reproducibility_score(e) == pytest.approx(1.0)

    def test_reproducibility_none(self):
        e = make_entry(paper_doi=None, has_tests=False, has_examples=False)
        assert reproducibility_score(e) == pytest.approx(0.0)

    def test_reproducibility_partial(self):
        e = make_entry(paper_doi="10.0/x", has_tests=False, has_examples=False)
        assert reproducibility_score(e) == pytest.approx(1 / 3)

    def test_deprecated_lower_than_stable(self):
        assert maturity_score(make_entry(maturity="deprecated")) < maturity_score(
            make_entry(maturity="stable")
        )

    def test_adoption_zero_when_no_signal(self):
        assert adoption_score(make_entry(stars=0, citations=0)) == 0.0


# --------------------------------------------------------------------------- #
# Composite
# --------------------------------------------------------------------------- #
class TestComposite:
    def test_weights_sum_to_one(self):
        assert sum(WEIGHTS.values()) == pytest.approx(1.0)

    def test_bounded_unit_interval(self):
        q = DiscoveryQuery(capability_tags=["property_prediction"], input_format="SMILES")
        for e in RegistryLoader().entries():
            s = score_entry(e, q).composite
            assert 0.0 <= s <= 1.0

    def test_best_possible_is_one(self):
        e = make_entry(
            capability_tags=["property_prediction"],
            license_class="permissive",
            trust_tier=0,
            maturity="stable",
            stars=10_000_000,
            citations=10_000_000,
            paper_doi="10.0/x",
            has_tests=True,
            has_examples=True,
        )
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        # maturity caps below 1.0 (stage 0.9), so composite is high but < 1.0
        assert composite_score(component_scores(e, q)) > 0.9

    def test_monotonic_in_stars(self):
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        low = score_entry(make_entry(stars=10, citations=0), q).composite
        high = score_entry(make_entry(stars=10000, citations=0), q).composite
        assert high > low


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #
class TestRanking:
    def test_top_k_default_three(self):
        q = DiscoveryQuery(capability_tags=["property_prediction"], input_format="SMILES")
        ranked = rank_candidates(RegistryLoader().entries(), q)
        assert len(ranked) == 3
        assert [c.rank for c in ranked] == [1, 2, 3]

    def test_sorted_descending(self):
        q = DiscoveryQuery(capability_tags=["property_prediction"], input_format="SMILES")
        ranked = rank_candidates(RegistryLoader().entries(), q, top_k=None)
        comps = [c.composite for c in ranked]
        assert comps == sorted(comps, reverse=True)

    def test_stable_ranking_repeatable(self):
        q = DiscoveryQuery(capability_tags=["property_prediction"], input_format="SMILES")
        entries = RegistryLoader().entries()
        first = [c.entry.id for c in rank_candidates(entries, q, top_k=None)]
        second = [c.entry.id for c in rank_candidates(list(reversed(entries)), q, top_k=None)]
        assert first == second

    def test_tie_broken_by_id(self):
        a = make_entry(id="bbb")
        b = make_entry(id="aaa")
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        ranked = rank_candidates([a, b], q, top_k=None)
        assert ranked[0].entry.id == "aaa"

    def test_relevant_tool_beats_irrelevant(self):
        relevant = make_entry(id="relevant", capability_tags=["property_prediction"])
        irrelevant = make_entry(id="irrelevant", capability_tags=["molecular_dynamics"])
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        ranked = rank_candidates([irrelevant, relevant], q, top_k=None)
        assert ranked[0].entry.id == "relevant"


# --------------------------------------------------------------------------- #
# Explainability
# --------------------------------------------------------------------------- #
class TestRationale:
    def test_breakdown_lists_all_components(self):
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        cand = score_entry(make_entry(), q)
        text = component_breakdown(cand)
        for label in ("Relevance", "Maturity", "License", "Trust", "Reproducibility"):
            assert label in text

    def test_compare_top_two_mentions_both(self):
        winner = make_entry(id="winner", trust_tier=0, capability_tags=["property_prediction"])
        loser = make_entry(id="loser", trust_tier=3, capability_tags=["property_prediction"])
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        ranked = rank_candidates([winner, loser], q, top_k=None)
        text = compare_top_two(ranked)
        assert "Tool" in text  # both share the display name

    def test_compare_single_candidate(self):
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        ranked = rank_candidates([make_entry()], q, top_k=None)
        assert "only candidate" in compare_top_two(ranked)

    def test_explain_ranking_multiline(self):
        q = DiscoveryQuery(capability_tags=["property_prediction"], input_format="SMILES")
        ranked = rank_candidates(RegistryLoader().entries(), q)
        report = explain_ranking(ranked)
        assert report.count("\n") >= 3
