"""The bundle's thermochemical-cycle helper: no term dropped, no coefficient guessed.

A CO2 heat-of-formation run reported -357.6 kJ/mol against a -393.5 target. Its
electronic atomization was fine; it omitted the three reference ATOMS' own
H(298) - E_elec. An atom has no vibrations and no rotations, so it is easy to give
it no correction, but it still carries 5/2 kT = 6.197 kJ/mol -- 18.6 for C + 2 O,
which is the entire implementation half of that run's error (run e4b683bd). Its
source even carried a confident justification for the omission.

These tests pin the algebra against the experimental CO2 cycle, so the term cannot
quietly go missing again.
"""

import doctest
import sys
from pathlib import Path

import pytest

from conftest import FakeAtoms  # noqa: E402  (conftest owns sys.path)

import twain_thermo as tt  # noqa: E402

# The run's own numbers (eV where noted), so the fix is checked against reality.
E_CO2, E_C, E_O = -1030.0, -37.0, -75.0          # placeholder electronic energies
CORR_CO2_EV = 0.41319727981408505                 # CO2 H(298)-E_elec, from the run
DHF_ATOMS = {"C": 716.68, "O": 249.18}            # NIST-JANAF, 298.15 K


class TestMonatomicCorrection:
    def test_it_is_five_halves_RT(self):
        kj = tt.monatomic_enthalpy_correction() * tt.EV_TO_KJ_PER_MOL
        assert kj == pytest.approx(6.197, abs=1e-3)

    def test_it_is_never_zero_at_room_temperature(self):
        """The whole point: an atom's correction is not zero."""
        assert tt.monatomic_enthalpy_correction(298.15) > 0

    def test_it_vanishes_at_absolute_zero(self):
        assert tt.monatomic_enthalpy_correction(0.0) == 0.0

    def test_it_scales_linearly(self):
        assert (tt.monatomic_enthalpy_correction(600.0)
                == pytest.approx(2 * tt.monatomic_enthalpy_correction(300.0)))

    def test_a_negative_temperature_raises(self):
        with pytest.raises(tt.ThermoError):
            tt.monatomic_enthalpy_correction(-1.0)


class TestStoichiometry:
    def test_counted_from_symbols(self):
        assert tt.stoichiometry(["C", "O", "O"]) == {"C": 1, "O": 2}

    def test_counted_from_an_atoms_like_object(self):
        class FakeAtoms:
            def get_chemical_symbols(self):
                return ["O", "C", "O"]
        assert tt.stoichiometry(FakeAtoms()) == {"O": 2, "C": 1}

    def test_an_empty_structure_raises(self):
        with pytest.raises(tt.ThermoError):
            tt.stoichiometry([])


class TestTheCycleReproducesExperiment:
    """The acid test: assemble the run's cycle and land on the right answer."""

    def test_the_atoms_correction_is_worth_exactly_18_6_kJ(self):
        symbols = ["C", "O", "O"]
        energies = {"C": E_C, "O": E_O}
        with_atoms = tt.atomization_enthalpy(
            symbols, E_CO2, energies, molecule_correction=CORR_CO2_EV)
        # Zeroing the atomic corrections is precisely the bug that was shipped.
        without = tt.atomization_enthalpy(
            symbols, E_CO2, energies, molecule_correction=CORR_CO2_EV,
            atom_corrections={"C": 0.0, "O": 0.0})
        delta = (with_atoms - without) * tt.EV_TO_KJ_PER_MOL
        assert delta == pytest.approx(18.59, abs=0.01)

    def test_experimental_energies_give_the_experimental_formation_enthalpy(self):
        """Feed the cycle the experimental atomization; dHf must come back -393.5.

        This checks the algebra and the sign convention end to end, independent
        of any calculator.
        """
        exp_atomization_kj = 1608.55
        d_at_ev = exp_atomization_kj / tt.EV_TO_KJ_PER_MOL
        dHf = tt.formation_enthalpy(["C", "O", "O"], d_at_ev, DHF_ATOMS)
        assert dHf == pytest.approx(-393.51, abs=0.05)

    def test_the_runs_reported_value_is_reproduced_by_the_bug(self):
        """And the corrected cycle moves it to -376.2, as computed by hand."""
        elec_kj = 1612.494742923312
        corr_kj = CORR_CO2_EV * tt.EV_TO_KJ_PER_MOL
        buggy = (sum(n * DHF_ATOMS[e] for e, n in {"C": 1, "O": 2}.items())
                 - (elec_kj - corr_kj))
        assert buggy == pytest.approx(-357.59, abs=0.02)
        fixed_at = (elec_kj + 3 * 6.1971 - corr_kj) / tt.EV_TO_KJ_PER_MOL
        fixed = tt.formation_enthalpy(["C", "O", "O"], fixed_at, DHF_ATOMS)
        assert fixed == pytest.approx(-376.18, abs=0.05)

    def test_stoichiometry_is_not_hand_multiplied(self):
        """Doubling the oxygen count must change the result by one O's worth."""
        one_o = tt.atomization_enthalpy(["C", "O"], E_CO2, {"C": E_C, "O": E_O})
        two_o = tt.atomization_enthalpy(["C", "O", "O"], E_CO2, {"C": E_C, "O": E_O})
        corr = tt.monatomic_enthalpy_correction()
        assert two_o - one_o == pytest.approx(E_O + corr)


class TestItFailsLoudly:
    def test_a_missing_reference_energy_raises(self):
        with pytest.raises(tt.ThermoError, match="no reference energy for O"):
            tt.atomization_enthalpy(["C", "O"], E_CO2, {"C": E_C})

    def test_a_missing_atomic_formation_enthalpy_raises(self):
        with pytest.raises(tt.ThermoError, match="formation enthalpy for O"):
            tt.formation_enthalpy(["C", "O"], 16.0, {"C": 716.68})

    def test_an_override_can_add_but_the_default_is_never_zero(self):
        """atom_corrections exists to refine a term, not to delete it."""
        base = tt.atomization_enthalpy(["C"], E_CO2, {"C": E_C})
        assert base != tt.atomization_enthalpy(
            ["C"], E_CO2, {"C": E_C}, atom_corrections={"C": 0.0})


class TestDoctests:
    def test_module_doctests_pass(self):
        assert doctest.testmod(tt, verbose=False).failed == 0


class TestItRunsStandaloneInABundle:
    def test_it_imports_with_no_third_party_packages(self, tmp_path):
        import json
        import subprocess
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "twain_thermo.py").write_text(
            Path(tt.__file__).read_text(encoding="utf-8"), encoding="utf-8")
        (bundle / "use_it.py").write_text(
            "import json\n"
            "from twain_thermo import atomization_enthalpy, formation_enthalpy\n"
            "d = atomization_enthalpy(['C','O','O'], -1030.0,\n"
            "                         {'C': -37.0, 'O': -75.0},\n"
            "                         molecule_correction=0.413)\n"
            "print(json.dumps({'dHf': formation_enthalpy(['C','O','O'], d,\n"
            "                  {'C': 716.68, 'O': 249.18})}))\n")
        proc = subprocess.run([sys.executable, "use_it.py"], cwd=bundle,
                              capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr
        assert "dHf" in json.loads(proc.stdout)


class TestErrorCancellingReactionRoute:
    """CO + 1/2 O2 -> CO2 instead of pulling CO2 apart into atoms.

    Atomization makes the answer carry the functional's error on every bond it
    breaks. Measured on this exact molecule: B3LYP is out by ~17 kJ/mol on the
    1608 kJ/mol atomization, and bare Hartree-Fock -- which a later run of the
    same conversation selected via Psi4's method='scf' -- by ~616, reporting
    dHf = +245.7. A reaction that conserves bond count and type lets those errors
    cancel between the two sides.
    """

    # Experimental, kJ/mol, 298.15 K. Keyed the way twain_thermo spells formulas.
    DHF = {"C1O1": -110.53, "O2": 0.0}
    TARGET = -393.51

    def _ev(self, kj):
        return kj / tt.EV_TO_KJ_PER_MOL

    def test_the_cycle_reproduces_the_experimental_value(self):
        """Feed it the experimental reaction enthalpy; dHf must come back right."""
        d_rxn_kj = self.TARGET - self.DHF["C1O1"]        # -282.98
        # One species per side, energies chosen to realize that reaction enthalpy.
        co = tt.species(["C", "O"], 0.0, correction=0.0)
        o2 = tt.species(["O", "O"], 0.0, correction=0.0, coefficient=0.5)
        co2 = tt.species(["C", "O", "O"], self._ev(d_rxn_kj), correction=0.0)
        got = tt.formation_enthalpy_via_reaction(
            ["C", "O", "O"], [co, o2], [co2], self.DHF)
        assert got == pytest.approx(self.TARGET, abs=0.05)

    def test_a_fractional_coefficient_is_honoured(self):
        """1/2 O2 is the whole point; an integer-only API would misstate it."""
        half = tt.species(["O", "O"], -10.0, correction=0.0, coefficient=0.5)
        whole = tt.species(["O", "O"], -10.0, correction=0.0, coefficient=1.0)
        co = tt.species(["C"], 0.0)
        assert (tt.reaction_enthalpy([co, half], [tt.species(["C", "O"], 0.0, correction=0.0)])
                != tt.reaction_enthalpy([co, whole],
                                        [tt.species(["C", "O", "O"], 0.0, correction=0.0)]))

    def test_an_unbalanced_reaction_raises(self):
        """An unbalanced reaction returns a meaningless number, so refuse it."""
        with pytest.raises(tt.ThermoError, match="does not balance"):
            tt.reaction_enthalpy(
                [tt.species(["C", "O"], 0.0, correction=0.0)],
                [tt.species(["C", "O", "O"], 0.0, correction=0.0)])

    def test_a_polyatomic_without_a_correction_raises(self):
        """Dropping a molecule's ZPE is the same class of bug as dropping 5/2 kT."""
        with pytest.raises(tt.ThermoError, match="polyatomic but has no"):
            tt.reaction_enthalpy(
                [tt.species(["C", "O"], 0.0)],                    # no correction
                [tt.species(["C", "O"], 0.0, correction=0.0)])

    def test_a_lone_atom_needs_no_correction(self):
        """An atom's 5/2 kT is supplied, so the caller cannot forget it."""
        left = tt.species(["O"], 0.0)
        right = tt.species(["O"], 0.0, correction=tt.monatomic_enthalpy_correction())
        assert tt.reaction_enthalpy([left], [right]) == pytest.approx(0.0, abs=1e-12)

    def test_the_target_must_be_a_product(self):
        with pytest.raises(tt.ThermoError, match="not among the products"):
            tt.formation_enthalpy_via_reaction(
                ["C", "O", "O"],
                [tt.species(["C", "O", "O"], 0.0, correction=0.0)],
                [tt.species(["C", "O"], 0.0, correction=0.0),
                 tt.species(["O"], 0.0)],
                {"C1O1": -110.53, "O1": 249.18})

    def test_a_missing_reference_enthalpy_raises(self):
        with pytest.raises(tt.ThermoError, match="no standard formation enthalpy"):
            tt.formation_enthalpy_via_reaction(
                ["C", "O", "O"],
                [tt.species(["C", "O"], 0.0, correction=0.0),
                 tt.species(["O", "O"], 0.0, correction=0.0, coefficient=0.5)],
                [tt.species(["C", "O", "O"], 0.0, correction=0.0)],
                {"O2": 0.0})   # CO missing


class TestAFormulaStringIsAFormula:
    """A string names a SPECIES, not one element and not a list of symbols.

    Every existing test here passes FakeAtoms or a symbol list, so nothing
    exercised the string path -- which is how this reached the cluster. A CO2
    formation run written the obvious way died at
    ``no standard formation enthalpy for CO1, O21`` (Slurm job 2629667): the
    string branch took "CO" to be the element CO and "O2" to be the element O2,
    then appended the count.
    """

    @pytest.mark.parametrize("text,expected", [
        ("CO2", {"C": 1, "O": 2}),
        ("O2", {"O": 2}),
        ("CO", {"C": 1, "O": 1}),
        ("C", {"C": 1}),
        ("H2O", {"H": 2, "O": 1}),
        ("CH4", {"C": 1, "H": 4}),
        ("NaCl", {"Na": 1, "Cl": 1}),        # two-letter symbol
        ("C6H12O6", {"C": 6, "H": 12, "O": 6}),
        ("  CO2  ", {"C": 1, "O": 2}),       # whitespace tolerated
        ("OC", {"O": 1, "C": 1}),            # order irrelevant
    ])
    def test_parse_formula(self, text, expected):
        assert tt.parse_formula(text) == expected

    @pytest.mark.parametrize("junk", [
        "", "   ", "co2", "CO2(g)", "x", "-1", 42, None,
        "2CO", "3H2O",              # a coefficient belongs in species(coefficient=)
        "CO2(", "CO2)", "()", "Mg(OH",  # unbalanced / empty groups
        "SO4^2-", "Fe3+", "Ca2+",   # charges: dropping one conflates Fe2+ with Fe3+
        "CO2·", "·H2O",             # dangling separator
    ])
    def test_unparseable_raises_rather_than_guessing(self, junk):
        """A silently wrong formula yields a plausible number, which is the whole
        failure mode this module exists to prevent."""
        with pytest.raises(tt.ThermoError):
            tt.parse_formula(junk)

    def test_stoichiometry_reads_a_string_as_a_formula(self):
        assert tt.stoichiometry("CO2") == {"C": 1, "O": 2}
        assert tt.stoichiometry("O2") == {"O": 2}
        # ...and still handles the shapes it always did.
        assert tt.stoichiometry(["C", "O", "O"]) == {"C": 1, "O": 2}
        assert tt.stoichiometry(FakeAtoms(["C", "O", "O"])) == {"C": 1, "O": 2}

    def test_a_polyatomic_string_is_not_mistaken_for_one_atom(self):
        """The silent half of the bug.

        {"CO2": 1} sums to ONE atom, so a polyatomic looked monatomic: the guard
        demanding H(T)-E_elec could not fire, and a missing correction would have
        taken 5/2 kT and quietly dropped the zero-point energy.
        """
        entry = tt.species("CO2", -1030.0)          # correction deliberately omitted
        assert sum(entry["counts"].values()) == 3
        with pytest.raises(tt.ThermoError, match="polyatomic"):
            tt.reaction_enthalpy([entry], [tt.species("CO2", -1030.0, correction=0.0)])

    def test_canonical_formula_is_readable_and_spelling_agnostic(self):
        for spelling in ("CO", "OC", "C1O1", ["C", "O"]):
            assert tt.canonical_formula(spelling) == "CO"
        assert tt.canonical_formula("CO2") == "CO2"
        assert tt.canonical_formula(["O", "O"]) == "O2"


class TestReferenceKeysMatchBySpeciesNotSpelling:
    """The reported crash: a reference table keyed the way a chemist writes it.

    The contract used to demand one internal spelling ("C1O1" for CO), which no
    caller could be expected to produce -- and the generated script wrote
    {"CO": -110.53, "O2": 0.0}, as anyone would.
    """

    def _cycle(self, table):
        return tt.formation_enthalpy_via_reaction(
            "CO2",
            [tt.species("CO", -1.0, correction=0.0, coefficient=1.0),
             tt.species("O2", -2.0, correction=0.0, coefficient=0.5)],
            [tt.species("CO2", -5.0, correction=0.0, coefficient=1.0)],
            table,
        )

    def test_the_generated_scripts_table_works(self):
        assert self._cycle({"CO": -110.53, "O2": 0.0}) == pytest.approx(-399.99, abs=0.01)

    @pytest.mark.parametrize("table", [
        {"CO": -110.53, "O2": 0.0},
        {"C1O1": -110.53, "O2": 0.0},
        {"OC": -110.53, "O2": 0.0},
        {"CO": -110.53, "OO": 0.0},
    ])
    def test_every_spelling_gives_the_same_answer(self, table):
        assert self._cycle(table) == pytest.approx(self._cycle({"CO": -110.53, "O2": 0.0}))

    def test_a_genuinely_missing_species_still_raises(self):
        """Normalising must not turn a real omission into a silent zero."""
        with pytest.raises(tt.ThermoError, match="no standard formation enthalpy"):
            self._cycle({"O2": 0.0})

    def test_the_error_names_what_was_supplied(self):
        """"missing CO" while the caller believes they supplied it is the confusing
        case; listing both sides makes a spelling problem self-evident."""
        with pytest.raises(tt.ThermoError, match="Supplied: O2"):
            self._cycle({"O2": 0.0})

    def test_the_error_uses_readable_formulas(self):
        """Not "CO1"/"O21", and not "C1O1" either."""
        with pytest.raises(tt.ThermoError) as excinfo:
            self._cycle({})
        message = str(excinfo.value)
        assert "CO" in message and "O2" in message
        assert "CO1" not in message and "O21" not in message and "C1O1" not in message


class TestEveryReferenceLookupMatchesBySpecies:
    """The normalisation is module-wide, not only in the function that broke.

    The old codegen note told models to key references "by formula with elements
    sorted", which yields {"C1": 716.68, "O1": 249.18} for an ATOM table exactly
    as readily as {"C1O1": ...} for a reaction one -- so fixing only the reaction
    path would have left the same trap in the other two.
    """

    def test_atomization_accepts_count_suffixed_atom_keys(self):
        plain = tt.atomization_enthalpy(
            FakeAtoms(["C", "O", "O"]), E_CO2, {"C": E_C, "O": E_O},
            molecule_correction=CORR_CO2_EV)
        suffixed = tt.atomization_enthalpy(
            FakeAtoms(["C", "O", "O"]), E_CO2, {"C1": E_C, "O1": E_O},
            molecule_correction=CORR_CO2_EV)
        assert suffixed == pytest.approx(plain)

    def test_formation_accepts_count_suffixed_atom_keys(self):
        plain = tt.formation_enthalpy(FakeAtoms(["C", "O", "O"]), 16.0, DHF_ATOMS)
        suffixed = tt.formation_enthalpy(
            FakeAtoms(["C", "O", "O"]), 16.0, {"C1": 716.68, "O1": 249.18})
        assert suffixed == pytest.approx(plain)

    def test_atom_corrections_are_normalised_too(self):
        """Overriding an element must not depend on how the key was spelled."""
        override = tt.atomization_enthalpy(
            FakeAtoms(["C", "O", "O"]), E_CO2, {"C": E_C, "O": E_O},
            molecule_correction=CORR_CO2_EV, atom_corrections={"C1": 0.0, "O1": 0.0})
        expected = tt.atomization_enthalpy(
            FakeAtoms(["C", "O", "O"]), E_CO2, {"C": E_C, "O": E_O},
            molecule_correction=CORR_CO2_EV, atom_corrections={"C": 0.0, "O": 0.0})
        assert override == pytest.approx(expected)

    def test_a_real_omission_still_raises_everywhere(self):
        """Normalising must not turn a missing reference into a silent zero."""
        with pytest.raises(tt.ThermoError, match="no reference energy"):
            tt.atomization_enthalpy(FakeAtoms(["C", "O", "O"]), E_CO2, {"C": E_C})
        with pytest.raises(tt.ThermoError, match="no standard atomic formation"):
            tt.formation_enthalpy(FakeAtoms(["C", "O", "O"]), 16.0, {"C": 716.68})


class TestTheNotationChemistsActuallyWrite:
    """Nested groups and hydrates, cross-checked against ASE's own parser.

    This file is copied verbatim into every RunBundle, including bundles that ship
    neither ASE nor pymatgen, so the parser has to be standard-library only. That
    is a constraint on HOW it is written, not on what it supports -- the authority
    lives here in the tests, where ASE is available.
    """

    CORPUS = ["CO2", "O2", "C", "H2O", "CH4", "NaCl", "C6H12O6", "Mg(OH)2",
              "Ca3(PO4)2", "Al2(SO4)3", "(NH4)2SO4", "Fe(NO3)3", "Ba(OH)2",
              "CaPt2", "SiO2", "C2H5OH"]

    @pytest.mark.parametrize("text", CORPUS)
    def test_agrees_with_ase(self, text):
        Formula = pytest.importorskip("ase.formula").Formula
        assert tt.parse_formula(text) == dict(Formula(text).count())

    def test_square_brackets_ase_rejects(self):
        """A coordination complex: ASE's parser raises on these, ours does not."""
        assert tt.parse_formula("K4[Fe(CN)6]3") == {
            "K": 4, "Fe": 3, "C": 18, "N": 18}

    @pytest.mark.parametrize("text,expected", [
        ("CuSO4·5H2O", {"Cu": 1, "S": 1, "O": 9, "H": 10}),
        ("CuSO4.5H2O", {"Cu": 1, "S": 1, "O": 9, "H": 10}),
        ("Na2CO3*10H2O", {"Na": 2, "C": 1, "O": 13, "H": 20}),
        ("MgSO4·7H2O", {"Mg": 1, "S": 1, "O": 11, "H": 14}),
    ])
    def test_hydrates_in_every_separator_they_are_written_with(self, text, expected):
        assert tt.parse_formula(text) == expected

    def test_a_hydrate_count_is_not_a_stoichiometric_coefficient(self):
        """"5H2O" after a separator is five waters; "2CO" as a whole formula is a
        reaction coefficient, which belongs in species(coefficient=...). Folding
        the latter into the formula would double count it."""
        assert tt.parse_formula("CuSO4·5H2O")["H"] == 10
        with pytest.raises(tt.ThermoError):
            tt.parse_formula("2CO")

    def test_a_grouped_species_flows_through_a_real_cycle(self):
        """End to end: the parser is only useful if the cycle accepts it."""
        entry = tt.species("Mg(OH)2", -100.0, correction=0.0)
        assert entry["counts"] == {"Mg": 1, "O": 2, "H": 2}
        assert entry["formula"] == "H2MgO2"          # canonical, elements sorted
        assert tt.canonical_formula("Mg(OH)2") == tt.canonical_formula("MgO2H2")


class TestUnitsAreStatedNotAssumed:
    """The units failure, pinned with run 1cd39ffd's real Psi4 numbers.

    That script converted Hartree -> eV -> kJ/mol and then called species(), so
    formation_enthalpy_via_reaction applied EV_TO_KJ_PER_MOL a second time to the
    reaction term while the kJ/mol reference enthalpies were untouched. CO2's
    standard heat of formation came back as -27452 kJ/mol instead of -393.8.
    An earlier run of the same prompt on the same engine passed eV and got
    -397.3, so a docstring saying "eV" -- which is what existed -- is not a fix.
    """

    # Psi4 B3LYP/6-311+G(2d,p) total energies in HARTREE, from the run's log.
    E_HA = {"CO2": -188.6506214983544, "CO": -113.3519988959608,
            "O2": -150.3746548968684}
    # H(T)-E_elec in eV, as ase.thermochemistry.IdealGasThermo returns them.
    CORR_EV = {"CO2": 0.41399, "CO": 0.22283, "O2": 0.19692}
    REFS = {"CO": -110.53, "O2": 0.0}               # kJ/mol, NIST
    EXPERIMENT = -393.5

    def _cycle(self, energies, corrections, **unit_kwargs):
        reactants = [
            tt.species(["C", "O"], energies["CO"], correction=corrections["CO"],
                       coefficient=1.0, **unit_kwargs),
            tt.species(["O", "O"], energies["O2"], correction=corrections["O2"],
                       coefficient=0.5, **unit_kwargs),
        ]
        products = [
            tt.species(["C", "O", "O"], energies["CO2"],
                       correction=corrections["CO2"], coefficient=1.0,
                       **unit_kwargs),
        ]
        return tt.formation_enthalpy_via_reaction(
            ["C", "O", "O"], reactants, products, self.REFS)

    def test_hartree_in_gives_the_experimental_answer(self):
        """What the failing script should have written: name the units, convert
        nothing. Energy in Hartree, ASE's correction in eV."""
        got = self._cycle(self.E_HA, self.CORR_EV,
                          unit="Hartree", correction_unit="eV")
        assert got == pytest.approx(self.EXPERIMENT, abs=1.0)

    def test_ev_in_is_unchanged_by_the_new_argument(self):
        """Every existing bundle passes eV and no unit=; that must keep working."""
        ev = {k: v * tt.ENERGY_UNITS["hartree"] for k, v in self.E_HA.items()}
        assert self._cycle(ev, self.CORR_EV) == pytest.approx(self.EXPERIMENT, abs=1.0)

    def test_a_declared_kj_mol_energy_is_no_longer_double_converted(self):
        """The script's own numbers -- pre-converted to kJ/mol -- now give the
        right answer BECAUSE they say so, instead of -27452."""
        factor = tt.ENERGY_UNITS["hartree"] * tt.EV_TO_KJ_PER_MOL
        kj = {k: v * factor for k, v in self.E_HA.items()}
        corr_kj = {k: v * tt.EV_TO_KJ_PER_MOL for k, v in self.CORR_EV.items()}
        assert self._cycle(kj, corr_kj, unit="kJ/mol") == pytest.approx(
            self.EXPERIMENT, abs=1.0)

    def test_the_undeclared_double_conversion_still_reproduces_the_bug(self):
        """Not a defect to fix here: with no unit stated, kJ/mol IS read as eV.
        Pinned so the size of the error is on record -- ~70x, not slightly off --
        which is why the plausibility backstop in module 11 exists as well."""
        factor = tt.ENERGY_UNITS["hartree"] * tt.EV_TO_KJ_PER_MOL
        kj = {k: v * factor for k, v in self.E_HA.items()}
        corr_kj = {k: v * tt.EV_TO_KJ_PER_MOL for k, v in self.CORR_EV.items()}
        got = self._cycle(kj, corr_kj)
        assert got == pytest.approx(-27452, abs=200)
        assert abs(got / self.EXPERIMENT) > 50

    @pytest.mark.parametrize("spelling", [
        "eV", "ev", "Hartree", "hartree", "Ha", "a.u.", "AU", "Eh", "E_h",
        "Rydberg", "Ry", "ryd", "kJ/mol", "kj/mol", "kJ mol", "kJ_mol",
        "kcal/mol", "kcal mol",
    ])
    def test_the_spellings_a_calculator_actually_prints_are_accepted(self, spelling):
        assert tt.energy_in_ev(1.0, spelling) > 0

    @pytest.mark.parametrize("factor,unit", [
        (27.211386245988, "Hartree"),
        (13.605693122994, "Rydberg"),
        (1.0 / 96.48533212, "kJ/mol"),
        (4.184 / 96.48533212, "kcal/mol"),
    ])
    def test_the_conversion_factors_are_right(self, factor, unit):
        assert tt.energy_in_ev(1.0, unit) == pytest.approx(factor, rel=1e-9)

    def test_rydberg_is_half_a_hartree(self):
        """The slip most likely to pass unnoticed: a factor of two reads as a
        physical disagreement, not a bug."""
        assert tt.energy_in_ev(2.0, "Ry") == pytest.approx(
            tt.energy_in_ev(1.0, "Ha"), rel=1e-12)

    def test_an_unknown_unit_raises_instead_of_guessing(self):
        with pytest.raises(tt.ThermoError) as excinfo:
            tt.species("CO2", -188.65, correction=0.0, unit="hartrees per mole")
        assert "unknown energy unit" in str(excinfo.value)
        assert "kJ/mol" in str(excinfo.value)      # names what IS accepted

    def test_energy_and_correction_can_have_different_units(self):
        """The real pairing: Psi4 returns Hartree, IdealGasThermo returns eV."""
        mixed = tt.species("CO2", -188.65, correction=0.414,
                           unit="Hartree", correction_unit="eV")
        assert mixed["correction"] == pytest.approx(0.414, rel=1e-12)
        assert mixed["energy"] == pytest.approx(-5133.428, abs=0.01)

    def test_the_correction_follows_the_energy_unit_by_default(self):
        both_kj = tt.species("CO2", -100.0, correction=-10.0, unit="kJ/mol")
        assert both_kj["correction"] == pytest.approx(
            -10.0 / tt.EV_TO_KJ_PER_MOL, rel=1e-12)

    def test_a_missing_correction_is_still_allowed_to_be_none(self):
        """None means "an atom, supply 5/2 kT" and must not become 0.0 eV."""
        assert tt.species("C", -37.0, unit="Hartree")["correction"] is None

    def test_atomization_takes_units_too(self):
        ha = tt.atomization_enthalpy(
            "CO2", self.E_HA["CO2"], {"C": -37.8, "O": -75.0},
            molecule_correction=0.414, unit="Hartree", correction_unit="eV")
        ev = tt.atomization_enthalpy(
            "CO2", self.E_HA["CO2"] * tt.ENERGY_UNITS["hartree"],
            {"C": -37.8 * tt.ENERGY_UNITS["hartree"],
             "O": -75.0 * tt.ENERGY_UNITS["hartree"]},
            molecule_correction=0.414)
        assert ha == pytest.approx(ev, rel=1e-9)

    def test_formation_enthalpy_takes_units_too(self):
        in_ev = tt.formation_enthalpy("CO2", 16.66, DHF_ATOMS)
        in_kj = tt.formation_enthalpy("CO2", 16.66 * tt.EV_TO_KJ_PER_MOL,
                                      DHF_ATOMS, unit="kJ/mol")
        assert in_ev == pytest.approx(in_kj, rel=1e-9)
