"""Unit tests for intake.intent_spec.

Exercises the IntentSpec dataclass and its nested types (Molecule,
SystemDescriptors, AcceptanceCriterion, IntentSpecMetadata): valid
construction from the shipped example, type coercion of nested dicts,
validation/rejection of bad input, and an asdict round-trip.

Run from the repo root with:  python -m pytest tests/unit/test_intent_spec.py
"""
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from intake.intent_spec import (
    AcceptanceCriterion,
    Domain,
    IntentSpec,
    IntentSpecMetadata,
    Molecule,
    SystemDescriptors,
)

EXAMPLE_PATH = (
    Path(__file__).resolve().parents[2]
    / "schemas"
    / "examples"
    / "intent_spec_example.json"
)


@pytest.fixture
def example_dict():
    """A fresh copy of the canonical example spec for each test."""
    with open(EXAMPLE_PATH, "r") as f:
        return json.load(f)


def with_field(base, **overrides):
    """Return a shallow copy of `base` with `overrides` applied."""
    return {**base, **overrides}


# --------------------------------------------------------------------------- #
# Molecule
# --------------------------------------------------------------------------- #
class TestMolecule:
    def test_valid(self):
        mol = Molecule(name="acetic acid", SMILES="CC(=O)O")
        assert mol.name == "acetic acid"
        assert mol.SMILES == "CC(=O)O"

    def test_name_must_be_str(self):
        with pytest.raises(ValueError):
            Molecule(name=123, SMILES="CC(=O)O")

    def test_name_must_not_be_none(self):
        with pytest.raises(ValueError):
            Molecule(name=None, SMILES="CC(=O)O")

    def test_smiles_must_be_str(self):
        with pytest.raises(ValueError):
            Molecule(name="acetic acid", SMILES=42)


# --------------------------------------------------------------------------- #
# SystemDescriptors
# --------------------------------------------------------------------------- #
class TestSystemDescriptors:
    def test_coerces_molecule_dict(self):
        sd = SystemDescriptors(
            molecule={"name": "graphite", "SMILES": "CC(=O)O"}, formula="some formula"
        )
        assert isinstance(sd.molecule, Molecule)
        assert sd.molecule.name == "graphite"

    def test_accepts_molecule_instance(self):
        mol = Molecule(name="graphite", SMILES="CC(=O)O")
        sd = SystemDescriptors(molecule=mol, formula="some formula")
        assert sd.molecule is mol

    def test_formula_must_be_str(self):
        with pytest.raises(ValueError):
            SystemDescriptors(
                molecule={"name": "graphite", "SMILES": "CC(=O)O"}, formula=None
            )

    def test_molecule_wrong_type_rejected(self):
        with pytest.raises(ValueError):
            SystemDescriptors(molecule=["not", "a", "molecule"], formula="f")


# --------------------------------------------------------------------------- #
# AcceptanceCriterion
# --------------------------------------------------------------------------- #
class TestAcceptanceCriterion:
    def test_valid(self):
        crit = AcceptanceCriterion(metric_name="energy", target_value=1.0, tolerance=0.01)
        assert crit.metric_name == "energy"
        assert crit.target_value == 1.0
        assert crit.tolerance == 0.01


# --------------------------------------------------------------------------- #
# IntentSpecMetadata
# --------------------------------------------------------------------------- #
class TestIntentSpecMetadata:
    def test_valid(self):
        meta = IntentSpecMetadata(confidence_scores={"objective_confidence": 0.9})
        assert meta.confidence_scores["objective_confidence"] == 0.9

    def test_ambiguity_defaults_true(self):
        meta = IntentSpecMetadata(confidence_scores={"x": 0.5})
        assert meta.ambiguity is True

    def test_confidence_scores_must_be_dict(self):
        with pytest.raises(ValueError):
            IntentSpecMetadata(confidence_scores=[0.5])

    def test_confidence_above_one_rejected(self):
        with pytest.raises(ValueError):
            IntentSpecMetadata(confidence_scores={"x": 1.5})

    def test_confidence_below_zero_rejected(self):
        with pytest.raises(ValueError):
            IntentSpecMetadata(confidence_scores={"x": -0.1})

    @pytest.mark.parametrize("value", [0.0, 1.0])
    def test_confidence_bounds_inclusive(self, value):
        meta = IntentSpecMetadata(confidence_scores={"x": value})
        assert meta.confidence_scores["x"] == value


# --------------------------------------------------------------------------- #
# IntentSpec (top-level)
# --------------------------------------------------------------------------- #
class TestIntentSpec:
    def test_builds_from_example(self, example_dict):
        spec = IntentSpec(**example_dict)
        assert spec.objective == example_dict["objective"]

    def test_nested_types_are_coerced(self, example_dict):
        spec = IntentSpec(**example_dict)
        assert isinstance(spec.domain, Domain)
        assert isinstance(spec.system_descriptors, SystemDescriptors)
        assert isinstance(spec.system_descriptors.molecule, Molecule)
        assert isinstance(spec.metadata, IntentSpecMetadata)
        assert all(isinstance(c, AcceptanceCriterion) for c in spec.acceptance_criteria)

    @pytest.mark.parametrize("domain", ["materials", "quantum"])
    def test_valid_domain_strings(self, example_dict, domain):
        spec = IntentSpec(**with_field(example_dict, domain=domain))
        assert spec.domain == Domain(domain)

    def test_invalid_domain_string_rejected(self, example_dict):
        with pytest.raises(ValueError):
            IntentSpec(**with_field(example_dict, domain="not-a-domain"))


    def test_accepts_domain_enum(self, example_dict):
        spec = IntentSpec(**with_field(example_dict, domain=Domain.QUANTUM))
        assert spec.domain is Domain.QUANTUM

    def test_empty_objective_rejected(self, example_dict):
        with pytest.raises(ValueError):
            IntentSpec(**with_field(example_dict, objective=""))

    def test_non_str_objective_rejected(self, example_dict):
        with pytest.raises(ValueError):
            IntentSpec(**with_field(example_dict, objective=5))

    def test_none_system_descriptors_rejected(self, example_dict):
        with pytest.raises(ValueError):
            IntentSpec(**with_field(example_dict, system_descriptors=None))

    def test_metadata_validation_propagates(self, example_dict):
        with pytest.raises(ValueError):
            IntentSpec(
                **with_field(example_dict, metadata={"confidence_scores": {"x": 2.0}})
            )

    def test_empty_acceptance_criteria_allowed(self, example_dict):
        spec = IntentSpec(**with_field(example_dict, acceptance_criteria=[]))
        assert spec.acceptance_criteria == []


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #
class TestRoundTrip:
    def test_asdict_roundtrips_example(self, example_dict):
        """asdict() of a spec built from the example reproduces the example.

        Domain is a str-Enum, so the coerced Domain member compares equal to
        the original "quantum" string under dict equality.
        """
        spec = IntentSpec(**example_dict)
        assert asdict(spec) == example_dict
