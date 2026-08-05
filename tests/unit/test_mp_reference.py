"""Unit tests for cross_validation.mp_reference (Materials Project baselines).

Everything here runs offline: the MP client is injected, so no test needs a key,
a network, or the mp-api package. What is being pinned is mostly refusal
behaviour -- a reference source that raises, hangs, or answers with the wrong
quantity is worse than one that says nothing, because VALIDATE grades and routes
on whatever it returns.

Run from the repo root with:  pixi run pytest tests/unit/test_mp_reference.py
"""
import pytest

from cross_validation.baseline_validator import (
    BaselineDB,
    BaselineRecord,
    ChainedBaselines,
    Prediction,
    canonical_unit,
    compare,
    units_comparable,
)
from cross_validation.mp_reference import (
    MP_PROPERTIES,
    MaterialsProjectBaselines,
    canonical_property,
)

# The real mp-842 summary doc for CaPt2, as returned by the live API on
# 2026-08-05: the C15 Laves phase, and the entry the CaPt2 run should be graded
# against. Kept verbatim so the fakes cannot drift from MP's actual shape --
# note bulk_modulus is a dict of Voigt/Reuss/VRH averages, not a number.
CAPT2 = {
    "material_id": "mp-842",
    "formula_pretty": "CaPt2",
    "symmetry": {"number": 227, "symbol": "Fd-3m"},
    "energy_above_hull": 0.0,
    "theoretical": False,
    "bulk_modulus": {"voigt": 132.905, "reuss": 132.905, "vrh": 132.905},
    "shear_modulus": {"voigt": 70.0, "reuss": 65.0, "vrh": 67.5},
    "band_gap": 0.0,
    "formation_energy_per_atom": -0.62,
    "density": 15.2,
}

# The other entry the same live query returned: a hexagonal polymorph with NO
# elasticity data. Picking it instead would silently produce no comparison.
CAPT2_HEX = {
    "material_id": "mp-1103211",
    "formula_pretty": "CaPt2",
    "symmetry": {"number": 194},
    "energy_above_hull": 0.05739140749999194,
    "theoretical": False,
    "bulk_modulus": None,
    "band_gap": 0.0,
    "density": 13.9,
}


class FakeRester:
    """Stands in for mp-api's SummaryRester: a context manager with .search()."""

    def __init__(self, docs, explode=None):
        self.docs = docs
        self.explode = explode
        self.calls = []
        self.timeout = None
        self.entered = self.exited = 0

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.explode is not None:
            raise self.explode
        return self.docs

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return False


def _source(docs=(CAPT2,), *, explode=None, **kwargs):
    rester = FakeRester(list(docs), explode)
    kwargs.setdefault("formula", "CaPt2")
    kwargs.setdefault("api_key", "test-key")
    timeout = kwargs.get("timeout")

    def factory(_key):
        # The real _rester() passes the timeout to the constructor; mirror that
        # so the assertion is about our code, not about the fake.
        rester.timeout = timeout if timeout is not None else 15
        return rester

    src = MaterialsProjectBaselines(client_factory=factory, **kwargs)
    return src, rester


# ═══════════════════════════════════════════════════════════════════════════
class TestPropertyMapping:
    @pytest.mark.parametrize("name,expected", [
        ("bulk_modulus", "bulk_modulus"),
        ("Bulk_Modulus", "bulk_modulus"),
        ("  k_vrh  ", "bulk_modulus"),
        ("B0", "bulk_modulus"),
        ("bandgap", "band_gap"),
        ("formation_energy", "formation_energy_per_atom"),
    ])
    def test_aliases_resolve(self, name, expected):
        assert canonical_property(name) == expected

    @pytest.mark.parametrize("name", [
        "r2", "n_points", "a_conv_Ang", "V0_Ang3", "smoke", "logS", None, "",
    ])
    def test_unknown_properties_are_refused(self, name):
        """Refusing here is what keeps run diagnostics off the network."""
        assert canonical_property(name) is None

    def test_every_offered_property_states_a_unit(self):
        """The unit is load-bearing -- compare() uses it to block bad pairs."""
        for key, spec in MP_PROPERTIES.items():
            assert spec.unit, f"{key} has no unit"

    def test_no_cell_convention_dependent_properties_are_offered(self):
        """volume/total_magnetization depend on which cell MP stored.

        A primitive-vs-conventional mismatch is a factor-of-N error in the SAME
        unit, so the unit guard cannot catch it. The CaPt2 run reported V0 for a
        6-atom primitive cell; MP may hold the 24-atom conventional one.
        """
        assert "volume" not in MP_PROPERTIES
        assert "total_magnetization" not in MP_PROPERTIES
        assert canonical_property("volume") is None


class TestLookup:
    def test_returns_the_vrh_average_with_its_unit(self):
        src, _ = _source()
        rec = src.lookup("Calcium diplatinide", "bulk_modulus")
        assert rec is not None
        assert rec.literature_value == 132.905     # vrh, not voigt or reuss
        assert rec.unit == "GPa"
        assert rec.molecule == "Calcium diplatinide"
        assert "mp-842" in rec.literature_source

    def test_one_request_serves_every_metric(self):
        """A 6-metric result must not be 6 HTTP calls."""
        src, rester = _source()
        for prop in ("bulk_modulus", "shear_modulus", "band_gap", "density"):
            assert src.lookup("CaPt2", prop) is not None
        assert len(rester.calls) == 1

    def test_an_unknown_property_never_reaches_the_network(self):
        src, rester = _source()
        assert src.lookup("CaPt2", "r2") is None
        assert rester.calls == []
        assert rester.entered == 0

    def test_a_property_mp_lacks_is_a_miss_not_a_zero(self):
        doc = dict(CAPT2, bulk_modulus=None)   # material exists, no elasticity data
        src, _ = _source([doc])
        assert src.lookup("CaPt2", "bulk_modulus") is None
        assert src.lookup("CaPt2", "band_gap") is not None

    def test_the_timeout_is_pushed_onto_the_rester(self):
        """MPRester(**kwargs) does not forward it, so it is set directly."""
        src, rester = _source(timeout=9)
        src.lookup("CaPt2", "bulk_modulus")
        assert rester.timeout == 9

    def test_the_client_is_closed(self):
        src, rester = _source()
        src.lookup("CaPt2", "bulk_modulus")
        assert rester.entered == 1 and rester.exited == 1


class TestItNeverFailsARun:
    """Every failure path returns None. VALIDATE then falls back to the plan."""

    def test_no_api_key_is_not_an_error(self, monkeypatch):
        monkeypatch.delenv("MP_API_KEY", raising=False)
        src = MaterialsProjectBaselines(formula="CaPt2", api_key=None)
        assert src.configured is False
        assert src.lookup("CaPt2", "bulk_modulus") is None
        assert "MP_API_KEY" in src.unavailable_reason

    def test_nothing_to_look_up_by_is_not_an_error(self):
        src = MaterialsProjectBaselines(formula=None, api_key="k")
        assert src.configured is False
        assert src.lookup("whatever", "bulk_modulus") is None

    @pytest.mark.parametrize("boom", [
        ConnectionError("dns"), TimeoutError("slow"), ValueError("schema drift"),
        RuntimeError("401 unauthorized"), KeyError("field"),
    ])
    def test_a_raising_client_yields_none(self, boom):
        src, _ = _source(explode=boom)
        assert src.lookup("CaPt2", "bulk_modulus") is None
        assert src.unavailable_reason  # recorded for the log, not re-raised

    def test_a_failure_is_not_retried_per_metric(self):
        src, rester = _source(explode=ConnectionError("dns"))
        for prop in ("bulk_modulus", "band_gap", "density"):
            assert src.lookup("CaPt2", prop) is None
        assert len(rester.calls) == 1  # one failed attempt, not three

    def test_no_matching_entry_yields_none(self):
        src, _ = _source([])
        assert src.lookup("CaPt2", "bulk_modulus") is None
        assert "no Materials Project entry matched" in src.unavailable_reason

    def test_a_pydantic_style_doc_is_read_by_attribute(self):
        """MP returns models unless asked for dicts; both must work."""
        class Doc:
            def __init__(self, d):
                for k, v in d.items():
                    setattr(self, k, v)
        src, _ = _source([Doc(CAPT2)])
        rec = src.lookup("CaPt2", "bulk_modulus")
        assert rec is not None and rec.literature_value == 132.905

    def test_the_citation_keeps_the_id_a_researcher_can_look_up(self):
        """model_dump() re-encodes MPID(mp-842) as AlphaID "mp-aaaaabgk".

        Both identify the same entry, but only mp-842 can be pasted into
        materialsproject.org, so the model is read by attribute instead.
        """
        class MPID:
            def __str__(self):
                return "mp-842"

            def model_dump(self):        # never the path taken
                return "mp-aaaaabgk"

        class Doc:
            material_id = MPID()
            formula_pretty = "CaPt2"
            symmetry = {"number": 227}
            energy_above_hull = 0.0
            theoretical = False
            bulk_modulus = {"vrh": 132.905}

            def model_dump(self):
                return dict(CAPT2, material_id="mp-aaaaabgk")

        src, _ = _source([Doc()])
        source = src.lookup("CaPt2", "bulk_modulus").literature_source
        assert "mp-842" in source
        assert "aaaaabgk" not in source

    def test_a_pydantic_style_symmetry_object_is_matched(self):
        """symmetry arrives as a SymmetryData model, not a dict, off the wire."""
        class Sym:
            number = 227

        class Doc:
            material_id = "mp-842"
            formula_pretty = "CaPt2"
            symmetry = Sym()
            energy_above_hull = 0.0
            theoretical = False
            bulk_modulus = {"vrh": 132.905}

        src, _ = _source([CAPT2_HEX, Doc()], space_group_number=227)
        assert src.lookup("CaPt2", "bulk_modulus").literature_value == 132.905


class TestPolymorphChoice:
    """A formula query returns every polymorph; the wrong one is a wrong number."""

    def test_the_real_two_entry_capt2_response(self):
        """Exactly what the live API returned for formula="CaPt2".

        mp-1103211 (hexagonal, sg 194) carries NO elasticity data, so choosing it
        would produce no comparison at all and the run would look unvalidated for
        a reason that has nothing to do with the run. The plan resolved sg 227, so
        the C15 entry is the one graded against.
        """
        src, _ = _source([CAPT2_HEX, CAPT2], space_group_number=227)
        rec = src.lookup("Calcium diplatinide", "bulk_modulus")
        assert rec is not None
        assert rec.literature_value == 132.905
        assert "mp-842" in rec.literature_source

    def test_the_hexagonal_entry_alone_is_an_honest_miss(self):
        """Right material, wrong polymorph, no elasticity -> None, not a guess."""
        src, _ = _source([CAPT2_HEX])
        assert src.lookup("CaPt2", "bulk_modulus") is None
        assert src.lookup("CaPt2", "density") is not None   # what it does have

    def test_the_requested_space_group_wins(self):
        other = dict(CAPT2, material_id="mp-999", energy_above_hull=0.0,
                     symmetry={"number": 194}, bulk_modulus={"vrh": 99.0})
        src, _ = _source([other, CAPT2], space_group_number=227)
        rec = src.lookup("CaPt2", "bulk_modulus")
        assert rec.literature_value == 132.905
        assert "mp-842" in rec.literature_source

    def test_otherwise_the_most_stable_entry_wins(self):
        unstable = dict(CAPT2, material_id="mp-999", energy_above_hull=0.4,
                        bulk_modulus={"vrh": 99.0})
        src, _ = _source([unstable, CAPT2])
        assert src.lookup("CaPt2", "bulk_modulus").literature_value == 132.905

    def test_a_missing_hull_distance_loses_to_a_known_one(self):
        nohull = dict(CAPT2, material_id="mp-999", energy_above_hull=None,
                      bulk_modulus={"vrh": 99.0})
        src, _ = _source([nohull, CAPT2])
        assert src.lookup("CaPt2", "bulk_modulus").literature_value == 132.905

    def test_a_computed_entry_is_labelled_as_such(self):
        """Calling a DFT number a measured literature value overstates the check."""
        src, _ = _source([dict(CAPT2, theoretical=True)])
        assert "computed" in src.lookup("CaPt2", "bulk_modulus").literature_source


# ═══════════════════════════════════════════════════════════════════════════
class TestUnitGuard:
    @pytest.mark.parametrize("a,b", [
        ("GPa", "GPa"), ("gpa", "GPa"), ("eV/atom", "ev_per_atom"),
        ("kJ/mol", "kj per mol".replace(" per ", "/")), ("g/cm^3", "g/cm**3"),
        (None, "GPa"), ("GPa", None), (None, None), ("", "GPa"),
    ])
    def test_comparable(self, a, b):
        assert units_comparable(a, b) is True

    @pytest.mark.parametrize("a,b", [
        ("kJ/mol", "eV/atom"), ("eV", "GPa"), ("GPa", "g/cm^3"),
    ])
    def test_not_comparable(self, a, b):
        assert units_comparable(a, b) is False

    def test_unknown_units_stay_permissive(self):
        """No information must not become a blocked comparison."""
        assert units_comparable("furlongs", "furlongs") is True
        assert canonical_unit("  GPa ") == "gpa"

    def test_compare_refuses_a_unit_mismatch(self):
        """The case a live source introduces: eV/atom against kJ/mol.

        Left unmatched rather than compared, because a graded wrong comparison
        gets routed on -- it would reject a correct result or accept a bad one.
        """
        db = BaselineDB([BaselineRecord(
            molecule="CO2", property="formation_energy_per_atom",
            literature_value=-1.5, unit="eV/atom")])
        preds = [Prediction(molecule="CO2", property="formation_energy_per_atom",
                            value=-393.5, unit="kJ/mol")]
        result = compare(preds, db)
        assert result.comparisons == []
        assert "unit mismatch" in result.unmatched[0]

    def test_compare_still_matches_when_units_agree(self):
        db = BaselineDB([BaselineRecord(
            molecule="CaPt2", property="bulk_modulus",
            literature_value=132.905, unit="GPa")])
        preds = [Prediction(molecule="CaPt2", property="bulk_modulus",
                            value=131.07, unit="GPa")]
        result = compare(preds, db)
        assert len(result.comparisons) == 1
        assert result.comparisons[0].literature == 132.905

    def test_a_unitless_prediction_still_matches(self):
        """Normalized metrics routinely carry unit=None; that must keep working."""
        db = BaselineDB([BaselineRecord(
            molecule="CaPt2", property="bulk_modulus",
            literature_value=132.905, unit="GPa")])
        preds = [Prediction(molecule="CaPt2", property="bulk_modulus",
                            value=131.07, unit=None)]
        assert len(compare(preds, db).comparisons) == 1


class TestChainedBaselines:
    def test_the_curated_snapshot_wins(self):
        """A hand-checked record must never be displaced by a queried one."""
        curated = BaselineDB([BaselineRecord(
            molecule="CaPt2", property="bulk_modulus",
            literature_value=180.0, literature_source="handbook")])
        mp, _ = _source()
        chained = ChainedBaselines(curated, mp)
        rec = chained.lookup("CaPt2", "bulk_modulus")
        assert rec.literature_value == 180.0
        assert rec.literature_source == "handbook"

    def test_it_falls_through_to_mp(self):
        mp, _ = _source()
        chained = ChainedBaselines(BaselineDB([]), mp)
        assert chained.lookup("CaPt2", "bulk_modulus").literature_value == 132.905

    def test_all_misses_are_a_miss(self):
        mp, _ = _source([])
        assert ChainedBaselines(BaselineDB([]), mp).lookup("X", "bulk_modulus") is None

    def test_none_sources_are_skipped(self):
        assert ChainedBaselines(BaselineDB([]), None).lookup("X", "y") is None

    def test_the_real_snapshot_still_loads_and_answers(self):
        """Guard against breaking logS lookups while adding a second source."""
        db = BaselineDB.load()
        chained = ChainedBaselines(db, None)
        assert len(db) > 0
        assert chained.lookup("aspirin", "logS") is not None
