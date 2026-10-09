"""Bring a quantity to its property's canonical form, and say how (#237).

A result, a researcher's target and a literature value describe one quantity
in whatever form each author chose. Aspirin's solubility arrives as log S, as
mol/L, as "4,600 mg/L", as "1 g in 300 mL", as "0.3 %". Comparing them means
first putting them on one scale, the way a person would, and showing the
working.

This module is the arithmetic, and only the arithmetic: deterministic, with
every factor in the tables below. Deciding WHICH quantity a piece of text
means (an LLM's job, when the text is ambiguous) happens elsewhere, and its
answer is fed through here, so a misreading surfaces as a unit that does not
convert rather than a silently wrong number.

    >>> h = to_canonical("aqueous_solubility", 4600, "mg/L", formula="C9H8O4")
    >>> round(h.value, 2), h.unit
    (-1.59, 'log10(mol/L)')
    >>> h.chain
    ['4600 mg/L = 4.6 g/L', '4.6 g/L / 180.16 g/mol = 0.025533 mol/L', 'log10(0.025533) = -1.593']
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

#: The canonical form each property family is compared in.
CANONICAL = {
    "aqueous_solubility": "log10(mol/L)",
    "formation_enthalpy": "kJ/mol",
    "band_gap": "eV",
}


class NotConvertible(ValueError):
    """The value can't be put in canonical form (unknown unit, missing data)."""


@dataclass
class Harmonized:
    value: float
    unit: str
    chain: list = field(default_factory=list)   # the working, one step per line


# --------------------------------------------------------------- units
def normalize_unit(unit) -> str:
    """A unit string in one spelling: "mg·L⁻¹" -> "mg/l", "mol dm-3" -> "mol/l"."""
    text = str(unit or "").strip().lower()
    text = text.replace("µ", "u").replace("μ", "u").replace("·", " ").replace("−", "-")
    text = text.replace("³", "3").replace("⁻¹", "-1").replace("⁻³", "-3")
    text = re.sub(r"\s*per\s*", "/", text)
    text = re.sub(r"\bdm\^?-?3\b", "l", text.replace("dm^-3", "l-1"))
    text = re.sub(r"\s*\(\s*", "(", re.sub(r"\s*\)\s*", ")", text))
    # "mol l-1", "mol l^-1" -> "mol/l"
    text = re.sub(r"\s*([a-z]+)\^?-1\b", r"/\1", text)
    text = re.sub(r"\s+", "", text)
    return text.replace("//", "/")


# Mass concentration -> g/L.
_MASS_CONC = {
    "g/l": 1.0, "mg/l": 1e-3, "ug/l": 1e-6, "ppm": 1e-3, "ppb": 1e-6,
    "g/ml": 1e3, "mg/ml": 1.0, "ug/ml": 1e-3, "g/dl": 10.0, "mg/dl": 1e-2,
    "g/100ml": 10.0, "mg/100ml": 1e-2, "%": 10.0, "%w/v": 10.0, "%(w/v)": 10.0,
    "g/kg": 1.0, "mg/kg": 1e-3,   # dilute aqueous solutions: 1 kg of water ~ 1 L
}
# Molar concentration -> mol/L.
_MOLAR = {"mol/l": 1.0, "m": 1.0, "mmol/l": 1e-3, "mm": 1e-3, "umol/l": 1e-6, "um": 1e-6,
          "nmol/l": 1e-9, "nm": 1e-9, "mol/kg": 1.0}
# Already log10(mol/L).
_LOG_MOLAR = {"log10(mol/l)", "log(mol/l)", "logs", "log10mol/l", "logmol/l", "log10(m)",
              "logunits", "log10units", "loglog10(mol/l)", "logsunits", "log10"}
# Molar energy -> kJ/mol (eV, hartree and Ry are per formula unit / molecule).
_EV_KJ_MOL = 96.48533212
_ENERGY = {"kj/mol": 1.0, "kcal/mol": 4.184, "j/mol": 1e-3, "ev": _EV_KJ_MOL,
           "mev": _EV_KJ_MOL / 1e3, "hartree": 2625.499639, "ha": 2625.499639,
           "eh": 2625.499639, "ry": 1312.749820, "cm-1": 0.01196266, "/cm": 0.01196266}
# Band-gap energy -> eV.
_GAP = {"ev": 1.0, "mev": 1e-3, "hartree": 27.211386, "ha": 27.211386, "ry": 13.605693,
        "kj/mol": 1 / _EV_KJ_MOL, "kcal/mol": 4.184 / _EV_KJ_MOL, "j": 6.241509074e18}


# --------------------------------------------------------------- molar mass
# IUPAC standard atomic weights (abridged conventional values), enough for the
# molecules TWAIN plans on. An element missing here makes the MW unknown, never
# guessed.
_ATOMIC_WEIGHT = {
    "H": 1.008, "He": 4.0026, "Li": 6.94, "B": 10.81, "C": 12.011, "N": 14.007,
    "O": 15.999, "F": 18.998, "Na": 22.990, "Mg": 24.305, "Al": 26.982, "Si": 28.085,
    "P": 30.974, "S": 32.06, "Cl": 35.45, "K": 39.098, "Ca": 40.078, "Ti": 47.867,
    "Fe": 55.845, "Co": 58.933, "Ni": 58.693, "Cu": 63.546, "Zn": 65.38, "Br": 79.904,
    "Ag": 107.87, "Sn": 118.71, "I": 126.90, "Pt": 195.08, "Au": 196.97, "Hg": 200.59,
    "Pb": 207.2, "Se": 78.971, "As": 74.922, "Ga": 69.723, "Ge": 72.630, "Mn": 54.938,
    "Cr": 51.996, "V": 50.942, "Ba": 137.33, "Sr": 87.62, "Rb": 85.468, "Cs": 132.91,
}
_FORMULA_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*)")


def molar_mass(formula: str | None) -> float | None:
    """g/mol from a simple formula ("C9H8O4" -> 180.16), or None if unknown."""
    if not formula or not re.fullmatch(r"(?:[A-Z][a-z]?\d*)+", str(formula).strip()):
        return None
    total = 0.0
    for symbol, count in _FORMULA_TOKEN.findall(str(formula).strip()):
        weight = _ATOMIC_WEIGHT.get(symbol)
        if weight is None:
            return None
        total += weight * (int(count) if count else 1)
    return total


def _g(x: float) -> str:
    return f"{x:.5g}"


# --------------------------------------------------------------- conversion
def to_canonical(family: str, value: float, unit, *, formula: str | None = None,
                 mw: float | None = None) -> Harmonized:
    """``value`` in ``unit`` as the family's canonical form, with the working.

    Raises :class:`NotConvertible` when the unit is unknown for the family, or
    a mass-based solubility has no molar mass to divide by.
    """
    if family not in CANONICAL:
        raise NotConvertible(f"no canonical form for {family!r}")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise NotConvertible(f"{value!r} is not a finite number")
    target = CANONICAL[family]
    key = normalize_unit(unit)
    shown = str(unit).strip() if unit else ""

    if family == "aqueous_solubility":
        if key in _LOG_MOLAR:
            return Harmonized(float(value), target, [f"{_g(value)} is already log10(mol/L)"])
        chain = []
        if key in _MOLAR:
            molar = value * _MOLAR[key]
            if _MOLAR[key] != 1.0:
                chain.append(f"{_g(value)} {shown} = {_g(molar)} mol/L")
        elif key in _MASS_CONC:
            mw = mw or molar_mass(formula)
            if not mw:
                raise NotConvertible(f"{shown} needs the molar mass, and none is known")
            grams = value * _MASS_CONC[key]
            if _MASS_CONC[key] != 1.0:
                chain.append(f"{_g(value)} {shown} = {_g(grams)} g/L")
            molar = grams / mw
            chain.append(f"{_g(grams)} g/L / {mw:.2f} g/mol = {_g(molar)} mol/L")
        else:
            raise NotConvertible(f"unknown solubility unit {shown!r}")
        if molar <= 0:
            raise NotConvertible("a solubility must be positive to take its log")
        logs = math.log10(molar)
        chain.append(f"log10({_g(molar)}) = {logs:.3f}")
        return Harmonized(logs, target, chain)

    table = _ENERGY if family == "formation_enthalpy" else _GAP
    if key not in table:
        raise NotConvertible(f"unknown {family} unit {shown!r}")
    converted = value * table[key]
    chain = ([f"{_g(value)} is already {target}"] if table[key] == 1.0
             else [f"{_g(value)} {shown} x {table[key]:.6g} = {_g(converted)} {target}"])
    return Harmonized(converted, target, chain)


def grams_in_volume(grams: float, millilitres: float) -> float:
    """g/L for "1 g dissolves in 300 mL" (3.33 g/L)."""
    if millilitres <= 0:
        raise NotConvertible("the volume must be positive")
    return grams * 1000.0 / millilitres
