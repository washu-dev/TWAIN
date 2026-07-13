"""Unit tests for method_discovery.calculator_registry.

A *calculator* is the compute engine (GPAW) attached to a driver library (ASE).
These tests cover property canonicalization, calculator selection by capability,
and lookup -- the pieces that let "what is the band gap of silicon" pick a DFT
calculator instead of a cheminformatics library.

Run from the repo root with:  pixi run pytest tests/unit/test_calculator_selection.py
"""
import doctest
from pathlib import Path

import pytest

from method_discovery import calculator_registry as cr
from method_discovery.calculator_registry import (
    canonical_property,
    find_calculator,
    load_calculators,
    select_calculator,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestModuleDoctests:
    def test_doctests_pass(self):
        result = doctest.testmod(cr, verbose=False)
        assert result.failed == 0, f"{result.failed} doctest failure(s)"


class TestRegistryLoads:
    def test_ships_gpaw_and_emt(self):
        ids = {c.id for c in load_calculators()}
        assert {"gpaw", "emt"} <= ids

    def test_gpaw_is_a_heavy_ase_driven_dft_engine(self):
        gpaw = find_calculator("GPAW")
        assert gpaw.heavy is True
        assert gpaw.driver_library == "ASE"
        assert gpaw.import_name == "gpaw"
        assert "band_gap" in gpaw.capabilities

    def test_config_file_is_shipped(self):
        assert (REPO_ROOT / "configs" / "calculator_registry.json").is_file()


class TestCanonicalProperty:
    @pytest.mark.parametrize("text,expected", [
        ("what is the band gap of silicon", "band_gap"),
        ("compute the BANDGAP of Si", "band_gap"),
        ("electronic band structure of GaAs", "band_structure"),
        ("density of states for copper", "density_of_states"),
        ("cohesive energy of aluminium", "total_energy"),
    ])
    def test_recognized(self, text, expected):
        assert canonical_property(text) == expected

    @pytest.mark.parametrize("text", [
        "predict the aqueous solubility of aspirin",
        "run molecular dynamics of argon",
        "",
    ])
    def test_unrecognized_is_none(self, text):
        assert canonical_property(text) is None

    @pytest.mark.parametrize("text", [
        "do a DFT calculation of GaAs",
        "compute the electronic properties of silicon",
        "run an ab initio simulation",
    ])
    def test_vague_electronic_ask_is_not_forced_to_band_gap(self, text):
        # De-hardcoding: a vague electronic/DFT ask that names no observable is no
        # longer forced onto band_gap. It returns None here so the LLM discoverer
        # reasons about the actual task instead of assuming one property.
        assert canonical_property(text) is None


class TestSelectCalculator:
    def test_band_gap_on_linux_selects_full_dft_gpaw(self):
        # Where GPAW builds exist (linux-64), the full-DFT engine wins on fidelity.
        assert select_calculator("band_gap", platform="linux-64").id == "gpaw"

    def test_band_gap_on_mac_avoids_gpaw_prefers_real_over_ml(self):
        # GPAW has no osx-arm64 build, so discovery must NOT pick it on a Mac. Of
        # the platform-available band-gap engines it prefers a REAL calculation --
        # DFTB+ (approximate DFT) -- over the MatGL ML surrogate, even though DFTB+
        # needs external Slater-Koster params. (The user wants actual calculations.)
        pick = select_calculator("band_gap", platform="osx-arm64")
        assert pick.id != "gpaw"
        assert pick.available_on("osx-arm64")
        assert pick.id == "dftbplus"
        assert not pick.ml_surrogate

    def test_no_calculator_when_none_builds_for_platform(self):
        # If the only band-gap engines were GPAW (linux-only), a Mac gets None --
        # never an unrunnable pick. (Constructed with a GPAW-only catalog.)
        from method_discovery.calculator_registry import load_calculators
        gpaw_only = [c for c in load_calculators() if c.id == "gpaw"]
        assert select_calculator("band_gap", gpaw_only, platform="osx-arm64") is None
        assert select_calculator("band_gap", gpaw_only, platform="linux-64").id == "gpaw"

    def test_no_property_no_calculator(self):
        assert select_calculator(None) is None

    def test_property_without_a_calculator_is_none(self):
        # A cheminformatics property no catalogued calculator provides.
        assert select_calculator("aqueous_solubility", platform="linux-64") is None

    def test_calculator_follows_the_library_it_is_compatible_with(self):
        # GPAW plugs into ASE, not Pymatgen -> selection is gated by the library
        # discovery chose (data-driven), so nothing is force-paired.
        assert select_calculator("band_gap", library="ASE", platform="linux-64").id == "gpaw"
        # GPAW/DFTB+ plug into ASE, not Pymatgen -- but MatGL is Pymatgen-native:
        assert select_calculator("band_gap", library="Pymatgen", platform="linux-64").id == "matgl"

    def test_empty_compatible_libraries_means_no_constraint(self):
        gpaw = find_calculator("GPAW")
        assert gpaw.supports_library("ASE") is True
        assert gpaw.supports_library("Pymatgen") is False
        # A calculator with no declared compatibility is usable anywhere.
        gpaw.compatible_libraries = []
        assert gpaw.supports_library("anything") is True


class TestFindCalculator:
    @pytest.mark.parametrize("ident", ["gpaw", "GPAW", "Gpaw"])
    def test_case_insensitive_by_name_or_id(self, ident):
        assert find_calculator(ident).id == "gpaw"

    def test_missing_is_none(self):
        assert find_calculator("does-not-exist") is None
        assert find_calculator(None) is None
