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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "modules" / "06_code_configuration_builder" / "bundle_helpers"))

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

    def test_it_beats_atomization_under_a_per_bond_error(self):
        """The actual claim: a per-bond error hurts atomization far more.

        Model a functional that misses e kJ/mol of binding per bond. CO2 has 4
        bond-equivalents (2 double bonds); the reaction CO + 1/2 O2 -> CO2 leaves
        most of that bonding intact on both sides, so the residual is a fraction
        of what atomization carries.
        """
        e_per_bond = 20.0
        atomization_error = 4 * e_per_bond          # every bond broken
        reaction_error = abs(4 - (2 + 1)) * e_per_bond   # net bonds changed
        assert reaction_error < atomization_error / 3
