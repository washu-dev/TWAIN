"""Unit tests for method_discovery.registry_loader and schemas/discovery_registry.schema.json.

Covers:
  * RegistryEntry / DiscoveryRegistry dataclass coercion + validation.
  * RegistryLoader load, mtime-based cache invalidation, and query helpers.
  * JSON Schema 2020-12 validation against the shipped seed registry.

Run from the repo root with:  pixi run pytest tests/unit/test_discovery_registry.py
"""
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from method_discovery.registry_loader import (
    DiscoveryRegistry,
    LicenseClass,
    Maturity,
    RegistryEntry,
    RegistryLoader,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "configs" / "discovery_registry.json"
SCHEMA_PATH = REPO_ROOT / "schemas" / "discovery_registry.schema.json"


@pytest.fixture
def registry_dict():
    with open(REGISTRY_PATH, "r") as f:
        return json.load(f)


@pytest.fixture
def schema():
    with open(SCHEMA_PATH, "r") as f:
        return json.load(f)


@pytest.fixture
def entry_dict(registry_dict):
    """The RDKit seed entry as a fresh dict."""
    return next(e for e in registry_dict["entries"] if e["id"] == "rdkit")


def with_field(base, **overrides):
    return {**base, **overrides}


# --------------------------------------------------------------------------- #
# RegistryEntry
# --------------------------------------------------------------------------- #
class TestRegistryEntry:
    def test_valid(self, entry_dict):
        e = RegistryEntry(**entry_dict)
        assert e.id == "rdkit"
        assert isinstance(e.license_class, LicenseClass)
        assert isinstance(e.maturity, Maturity)

    def test_id_must_be_non_empty_str(self, entry_dict):
        with pytest.raises(ValueError):
            RegistryEntry(**with_field(entry_dict, id=""))

    def test_capability_tags_must_be_non_empty(self, entry_dict):
        with pytest.raises(ValueError):
            RegistryEntry(**with_field(entry_dict, capability_tags=[]))

    def test_capability_tags_must_be_list_of_str(self, entry_dict):
        with pytest.raises(ValueError):
            RegistryEntry(**with_field(entry_dict, capability_tags=[1, 2]))

    def test_invalid_license_class_rejected(self, entry_dict):
        with pytest.raises(ValueError):
            RegistryEntry(**with_field(entry_dict, license_class="freeware"))

    def test_invalid_maturity_rejected(self, entry_dict):
        with pytest.raises(ValueError):
            RegistryEntry(**with_field(entry_dict, maturity="experimental"))

    def test_trust_tier_out_of_range_rejected(self, entry_dict):
        with pytest.raises(ValueError):
            RegistryEntry(**with_field(entry_dict, trust_tier=4))

    def test_trust_tier_wrong_type_rejected(self, entry_dict):
        with pytest.raises(ValueError):
            RegistryEntry(**with_field(entry_dict, trust_tier="1"))

    def test_negative_stars_rejected(self, entry_dict):
        with pytest.raises(ValueError):
            RegistryEntry(**with_field(entry_dict, stars=-5))

    def test_has_paper_true_when_doi_present(self, entry_dict):
        assert RegistryEntry(**entry_dict).has_paper is True

    def test_has_paper_false_without_doi(self, entry_dict):
        e = RegistryEntry(**with_field(entry_dict, paper_doi=None))
        assert e.has_paper is False


# --------------------------------------------------------------------------- #
# DiscoveryRegistry
# --------------------------------------------------------------------------- #
class TestDiscoveryRegistry:
    def test_builds_from_seed(self, registry_dict):
        reg = DiscoveryRegistry(**registry_dict)
        assert len(reg.entries) >= 10
        assert all(isinstance(e, RegistryEntry) for e in reg.entries)

    def test_duplicate_ids_rejected(self, registry_dict):
        dup = registry_dict["entries"][0]
        bad = with_field(registry_dict, entries=[dup, dup])
        with pytest.raises(ValueError):
            DiscoveryRegistry(**bad)

    def test_by_id_index(self, registry_dict):
        reg = DiscoveryRegistry(**registry_dict)
        index = reg.by_id()
        assert "rdkit" in index
        assert index["rdkit"].name == "RDKit"

    def test_registry_version_required(self, registry_dict):
        with pytest.raises(ValueError):
            DiscoveryRegistry(**with_field(registry_dict, registry_version=""))


# --------------------------------------------------------------------------- #
# RegistryLoader
# --------------------------------------------------------------------------- #
class TestRegistryLoader:
    def test_load_seed_registry(self):
        reg = RegistryLoader().load()
        assert len(reg.entries) >= 10

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            RegistryLoader(tmp_path / "nope.json").load()

    def test_find_by_capability(self):
        results = RegistryLoader().find_by_capability("quantum_chemistry")
        ids = {e.id for e in results}
        assert "pyscf" in ids
        assert "rdkit" not in ids

    def test_find_by_input_format_case_insensitive(self):
        results = RegistryLoader().find_by_input_format("smiles")
        ids = {e.id for e in results}
        assert "rdkit" in ids
        assert "openbabel" in ids

    def test_cache_returns_same_object(self, tmp_path, registry_dict):
        path = tmp_path / "registry.json"
        path.write_text(json.dumps(registry_dict))
        loader = RegistryLoader(path)
        first = loader.load()
        second = loader.load()
        assert first is second

    def test_cache_invalidated_on_mtime_change(self, tmp_path, registry_dict):
        import os
        import time

        path = tmp_path / "registry.json"
        path.write_text(json.dumps(registry_dict))
        loader = RegistryLoader(path)
        first = loader.load()

        smaller = with_field(registry_dict, entries=registry_dict["entries"][:3])
        path.write_text(json.dumps(smaller))
        # Bump mtime forward so the change is detectable regardless of fs resolution.
        future = time.time() + 10
        os.utime(path, (future, future))

        second = loader.load()
        assert second is not first
        assert len(second.entries) == 3

    def test_force_reload(self, tmp_path, registry_dict):
        path = tmp_path / "registry.json"
        path.write_text(json.dumps(registry_dict))
        loader = RegistryLoader(path)
        first = loader.load()
        second = loader.load(force=True)
        assert second is not first


# --------------------------------------------------------------------------- #
# JSON Schema validation
# --------------------------------------------------------------------------- #
class TestSchema:
    def test_schema_is_valid(self, schema):
        Draft202012Validator.check_schema(schema)

    def test_seed_registry_matches_schema(self, schema, registry_dict):
        Draft202012Validator(schema).validate(registry_dict)

    def test_missing_required_field_fails_schema(self, schema, registry_dict):
        bad = json.loads(json.dumps(registry_dict))
        del bad["entries"][0]["license"]
        with pytest.raises(Exception):
            Draft202012Validator(schema).validate(bad)


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #
class TestRoundTrip:
    def test_asdict_roundtrips_entry(self, entry_dict):
        """asdict() of an entry reproduces the input (enums serialize to their values)."""
        e = RegistryEntry(**entry_dict)
        dumped = asdict(e)
        assert dumped["license_class"] == entry_dict["license_class"]
        assert dumped["maturity"] == entry_dict["maturity"]
        assert dumped["id"] == entry_dict["id"]
