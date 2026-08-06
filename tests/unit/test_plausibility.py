"""Unit tests for the physical-plausibility backstop (cross_validation.plausibility).

The case this exists for, pinned with the real numbers: run 1cd39ffd reported a
CO2 standard heat of formation of -27452.226 kJ/mol (experiment -393.5) because a
generated script pre-converted energies to kJ/mol before handing them to
twain_thermo, which converts again. The plan's acceptance metric carried
target_value: null and no literature baseline covered the molecule, so every
comparative check in VALIDATE honestly had nothing to say and the result was
delivered as "accepted".

Two earlier runs of the same prompt got -395.606 and -397.256. Those must stay
accepted -- a backstop that flags them is worse than no backstop at all, so both
are asserted here alongside the bad one.

Run from the repo root with:  pixi run pytest tests/unit/test_plausibility.py
"""
import json

import pytest

from cross_validation import plausibility as P

# The three CO2 runs, from the production database.
BAD = -27452.226
GOOD = (-395.6064420439893, -397.25633913811987)


class TestTheRegressionItWasWrittenFor:
    def test_the_impossible_value_is_flagged(self):
        found = P.check_metric("standard_heat_of_formation_kJ_mol", BAD)
        assert found is not None
        assert "not physically possible" in found.message()

    @pytest.mark.parametrize("value", GOOD)
    def test_the_runs_that_were_right_are_not_flagged(self, value):
        assert P.check_metric("standard_heat_of_formation_kJ_mol", value) is None
        assert P.check_metric("standard_heat_of_formation", value) is None

    def test_the_true_cause_is_among_the_named_candidates(self):
        """96.485x is what actually happened; the hint must offer it."""
        found = P.check_metric("standard_heat_of_formation_kJ_mol", BAD)
        assert "96.4853" in found.message()
        assert "-284.522" in found.message()  # the value the slip hides

    def test_it_does_not_claim_to_know_which_slip(self):
        """Loose bounds mean several factors fit. An earlier version returned the
        first by factor size and confidently named the wrong one."""
        message = P.check_metric("standard_heat_of_formation_kJ_mol", BAD).message()
        assert message.count("divided by") > 1
        assert "each of those is possible" in message


class TestOtherRealValuesStayClean:
    @pytest.mark.parametrize("name,value,unit", [
        ("bulk_modulus_GPa", 130.981, "GPa"),        # CaPt2, validated to 1.4%
        ("bulk_modulus", 131.0676, None),
        ("band_gap", 0.570224, "eV"),                 # silicon, run bd0677f6
        ("band_gap", 1.1, None),
        ("aqueous_solubility_logS", -1.72, None),     # aspirin
        ("logS", 0.0, None),                          # phenol: a real 0.0
        ("density", 22.59, None),                     # osmium, the densest element
        ("lattice_constant", 5.43, None),             # silicon
        ("formation_energy_per_atom", -3.2, "eV/atom"),
    ])
    def test_a_plausible_value_produces_no_finding(self, name, value, unit):
        assert P.check_metric(name, value, unit) is None


class TestWhatItRefusesToJudge:
    def test_an_unknown_property_is_silent(self):
        """Silence means "no bound is known", never "this looks fine". A total
        energy scales with electron count, so no fixed bound can judge it."""
        assert P.check_metric("total_energy", -5133.98, "eV") is None
        assert P.check_metric("some_new_descriptor", 1e12) is None

    def test_a_stated_unit_that_differs_is_skipped_not_converted(self):
        """-94060 kcal/mol IS -393.5 kJ/mol. Judging it against a kJ/mol bound
        would invent a failure; converting is not this module's job."""
        assert P.check_metric("standard_heat_of_formation", -94060.0,
                              "kcal/mol") is None

    def test_a_matching_stated_unit_is_judged(self):
        assert P.check_metric("standard_heat_of_formation", BAD, "kJ/mol") is not None

    def test_an_absent_unit_is_judged_and_the_assumption_is_stated(self):
        """The dangerous case: the failing run declared unit: null."""
        found = P.check_metric("standard_heat_of_formation_kJ_mol", BAD, None)
        assert "declared no unit" in found.message()
        assert "assumes kJ/mol" in found.message()

    @pytest.mark.parametrize("value", [None, "n/a", True, False, [1], {}])
    def test_a_non_number_is_not_its_problem(self, value):
        assert P.check_metric("band_gap", value) is None

    def test_a_value_too_small_by_a_factor_passes(self):
        """Honest about the limit: an upper bound only sees one direction. A
        lattice constant in nm (0.543) is inside 0..100 and cannot be caught."""
        assert P.check_metric("lattice_constant", 0.543) is None


class TestNonFiniteValues:
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_is_flagged_without_needing_a_bound(self, value):
        found = P.check_metric("some_property_with_no_bound", value)
        assert found is not None
        assert "not a number a measurement can take" in found.message()

    def test_the_non_finite_message_does_not_talk_about_ranges(self):
        message = P.check_metric("band_gap", float("nan")).message()
        assert "physically possible" not in message


class TestNameMatching:
    @pytest.mark.parametrize("name,expected", [
        ("standard_heat_of_formation_kJ_mol", "standard_heat_of_formation"),
        ("heat_of_formation", "standard_heat_of_formation"),
        ("bulk_modulus_GPa", "bulk_modulus"),
        ("band_gap_eV", "band_gap"),
        ("BandGap", "band_gap"),
        ("density_g_cm3", "density"),
        ("lattice_constant_ang", "lattice_constant"),
        ("formation_energy_per_atom_eV", "formation_energy_per_atom"),
        # The spelling that actually reached VALIDATE in production, mirroring
        # _BASELINE_PROPERTY_ALIASES in the control plane.
        ("aqueous_solubility_logS", "logS"),
        ("solubility_logs", "logS"),
    ])
    def test_a_metric_name_resolves_to_its_bound(self, name, expected):
        found = P.load_table().lookup(name)
        assert found is not None and found.property == expected

    def test_an_unrelated_name_resolves_to_nothing(self):
        assert P.load_table().lookup("wall_time_seconds") is None


class TestHintsAreDimensionallySensible:
    def test_an_energy_conversion_is_not_offered_for_a_pressure(self):
        """A bulk modulus is not converted by Hartrees; a wrong hint is worse
        than none."""
        message = P.check_metric("bulk_modulus", 130981.0).message()
        assert "Hartree" not in message
        assert "1000" in message  # GPa read as MPa: the one that does apply

    def test_a_length_gets_its_own_prefix_hint(self):
        message = P.check_metric("lattice_constant", 543.0).message()
        assert "picometre" in message

    def test_no_hint_when_no_factor_explains_it(self):
        found = P.check_metric("band_gap", -1.2, "eV")
        assert found is not None
        assert "unit slip" not in found.message()


class TestItIsNeverAHazard:
    def test_a_missing_config_disables_the_check(self, tmp_path):
        table = P.load_table(tmp_path / "does-not-exist.json")
        assert P.check_metric("standard_heat_of_formation", BAD, table=table) is None

    def test_a_corrupt_config_disables_the_check(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json at all")
        table = P.load_table(path)
        assert P.check_metric("standard_heat_of_formation", BAD, table=table) is None

    def test_an_entry_missing_its_bounds_is_skipped(self, tmp_path):
        path = tmp_path / "partial.json"
        path.write_text(json.dumps({"ranges": [
            {"property": "band_gap", "unit": "eV"},          # no min/max
            {"unit": "eV", "min": 0, "max": 1},              # no property
        ]}))
        table = P.load_table(path)
        assert P.check_metric("band_gap", 1e9, table=table) is None

    def test_the_env_switch_turns_it_off(self, monkeypatch):
        monkeypatch.setenv("TWAIN_PLAUSIBILITY_CHECK", "0")
        assert P.enabled() is False
        monkeypatch.setenv("TWAIN_PLAUSIBILITY_CHECK", "1")
        assert P.enabled() is True

    def test_it_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("TWAIN_PLAUSIBILITY_CHECK", raising=False)
        assert P.enabled() is True


class TestTheShippedTableIsCoherent:
    """The bounds are the whole check, so they get asserted rather than trusted."""

    def test_every_entry_is_complete(self):
        data = json.loads(P.CONFIG_PATH.read_text(encoding="utf-8"))
        assert data["ranges"], "the shipped table must not be empty"
        for entry in data["ranges"]:
            name = entry.get("property")
            assert name, f"an entry has no property name: {entry}"
            assert entry.get("unit"), f"{name} has no unit"
            assert entry.get("reason"), f"{name} has no short reason for the verdict"
            assert entry.get("why"), f"{name} has no justification for its bounds"
            low, high = entry.get("min"), entry.get("max")
            assert low is not None and high is not None, f"{name} is unbounded"
            assert low < high, f"{name} has an inverted range"

    def test_every_hint_factor_can_actually_fire(self):
        """A hint scoped to a unit no range uses is dead code that reads as
        coverage. Every factor must be >1 (a factor of 1 explains nothing) and
        name at least one unit some range is expressed in."""
        data = json.loads(P.CONFIG_PATH.read_text(encoding="utf-8"))
        range_units = {e["unit"] for e in data["ranges"]}
        factors = data["conversion_hints"]["factors"]
        assert factors, "the hint table must not be empty"
        for entry in factors:
            label, factor = entry.get("label"), entry.get("factor")
            assert label, f"a hint factor has no label: {entry}"
            assert factor and factor > 1, f"{label} has a useless factor {factor}"
            units = entry.get("units")
            assert units, f"{label} is unscoped; scope it or use '*'"
            if units != "*":
                live = set(units) & range_units
                assert live, (f"{label} applies only to {units}, and no range "
                              f"uses any of those ({sorted(range_units)})")

    def test_no_range_uses_a_unit_the_validator_cannot_canonicalise(self):
        """The unit gate compares canonical spellings, so a unit that
        canonicalises to None would silently make every stated unit mismatch and
        turn the check off for that property."""
        for entry in json.loads(P.CONFIG_PATH.read_text(encoding="utf-8"))["ranges"]:
            assert P._canonical_unit(entry["unit"]), entry["property"]

    def test_deliberately_excluded_properties_stay_excluded(self):
        """total_energy has no defensible bound -- its magnitude scales with
        electron count and basis set. Adding one would flag big molecules."""
        assert P.load_table().lookup("total_energy") is None
        assert P.load_table().lookup("cohesive_energy") is None
