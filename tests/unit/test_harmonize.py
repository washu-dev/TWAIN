"""Harmonizing quantities to one scale (#237), on aspirin's PubChem records."""
from __future__ import annotations

import pytest
from cross_validation import harmonize as H

ASPIRIN = "C9H8O4"


def test_aspirin_molar_mass():
    assert round(H.molar_mass(ASPIRIN), 2) == 180.16
    assert H.molar_mass("C9H8O4X") is None          # unknown element: unknown, not guessed
    assert H.molar_mass("aspirin") is None


# PubChem's aspirin solubility records (CID 2244), as a person would read them.
@pytest.mark.parametrize("value, unit, expected", [
    (4600, "mg/L", -1.59),                           # HSDB: "In water, 4,600 mg/L at 25 °C"
    (H.grams_in_volume(1, 300), "g/L", -1.73),       # HSDB: "1 g sol in 300 mL water at 25 °C"
    (0.3, "%", -1.78),                               # NIOSH: "(77 °F): 0.3%"
    (0.0101886, "mol/L", -1.99),                     # the run's own mol/L
    (10.1886, "mmol/L", -1.99),
    (-1.72, "log10(mol/L)", -1.72),                  # Delaney / the researcher's "log S"
    (-1.72, "logS", -1.72),
    (1.83556, "g/L", -1.99),
    (1835.56, "mg/L", -1.99),
])
def test_aspirin_solubility_on_one_scale(value, unit, expected):
    h = H.to_canonical("aqueous_solubility", value, unit, formula=ASPIRIN)
    assert (round(h.value, 2), h.unit) == (expected, "log10(mol/L)")
    assert h.chain


def test_the_working_is_shown():
    h = H.to_canonical("aqueous_solubility", 4600, "mg/L", formula=ASPIRIN)
    assert h.chain == ["4600 mg/L = 4.6 g/L", "4.6 g/L / 180.16 g/mol = 0.025533 mol/L",
                       "log10(0.025533) = -1.593"]


@pytest.mark.parametrize("unit", ["mg·L⁻¹", "mg L-1", "mg per L", " MG/L "])
def test_spellings_of_one_unit(unit):
    assert round(H.to_canonical("aqueous_solubility", 4600, unit, formula=ASPIRIN).value, 2) == -1.59


@pytest.mark.parametrize("call, why", [
    (lambda: H.to_canonical("aqueous_solubility", 4600, "mg/L"), "molar mass"),
    (lambda: H.to_canonical("aqueous_solubility", 4600, "furlongs", formula=ASPIRIN), "unknown"),
    (lambda: H.to_canonical("aqueous_solubility", 0, "mol/L"), "positive"),
    (lambda: H.to_canonical("aqueous_solubility", float("nan"), "mol/L"), "finite"),
    (lambda: H.to_canonical("viscosity", 1, "cP"), "canonical"),
])
def test_what_cannot_be_converted_says_why(call, why):
    with pytest.raises(H.NotConvertible, match=why):
        call()


@pytest.mark.parametrize("family, value, unit, expected", [
    ("formation_enthalpy", -94.05, "kcal/mol", -393.5),     # CO2
    ("formation_enthalpy", -393.5, "kJ/mol", -393.5),
    ("formation_enthalpy", -393509, "J/mol", -393.5),
    ("band_gap", 1170, "meV", 1.17),                        # Si
    ("band_gap", 1.17, "eV", 1.17),
])
def test_energies(family, value, unit, expected):
    assert round(H.to_canonical(family, value, unit).value, 1) == round(expected, 1)
