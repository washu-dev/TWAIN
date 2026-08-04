"""Assemble a thermochemical cycle without silently dropping a term.

Copied verbatim into a RunBundle (like ``inline_tests.py`` and
``twain_pseudo.py``) when the plan asks for a formation, atomization, reaction or
bond-dissociation enthalpy. Generated code calls these instead of hand-rolling
the algebra::

    from twain_thermo import atomization_enthalpy, formation_enthalpy

    d_atomization = atomization_enthalpy(
        co2, e_co2, {"C": e_c, "O": e_o}, molecule_correction=corr_co2)
    dHf = formation_enthalpy(co2, d_atomization, {"C": 716.68, "O": 249.18})

Why this exists rather than a prompt instruction: the algebra is short, looks
obvious, and has two failure modes that produce a *plausible* number.

1. Every species contributes its own ``H(T) - E_elec``, monatomic references
   included. An atom has no vibrations and no rotations, so it is easy to assume
   it has no correction at all -- but it still carries 3/2 kT of translation plus
   kT of PV, i.e. 5/2 kT = 6.197 kJ/mol at 298.15 K. A CO2 heat-of-formation run
   dropped it for all three reference atoms and reported -357.6 kJ/mol against a
   -393.5 target; the missing 18.6 kJ/mol was exactly 3 x 5/2 RT (run e4b683bd).
   Its source even carried a confident justification for the omission, claiming
   the tabulated atomic formation enthalpies absorb it. They do not: those are
   the atoms' own formation enthalpies, and the cycle they feed needs a true
   enthalpy difference at T.

2. Stoichiometry. ``E(C) + 2 E(O)`` has to match the molecule, and a hand-written
   coefficient is one edit away from wrong. Here it is counted from the structure.

Sign convention throughout: an atomization enthalpy is positive (energy required
to pull a stable molecule apart), and a formation enthalpy is negative for a
molecule more stable than its constituent elements.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, Mapping, Optional

__all__ = [
    "EV_TO_KJ_PER_MOL",
    "ThermoError",
    "atomization_enthalpy",
    "formation_enthalpy",
    "formation_enthalpy_via_reaction",
    "monatomic_enthalpy_correction",
    "reaction_enthalpy",
    "species",
    "stoichiometry",
]

# CODATA: 1 eV = 96.48533212 kJ/mol.
EV_TO_KJ_PER_MOL = 96.48533212
# Boltzmann constant in eV/K, so corrections stay in the calculators' own unit.
_KB_EV_PER_K = 8.617333262e-5


class ThermoError(ValueError):
    """A cycle that cannot be assembled -- raised instead of guessing.

    Always fail loudly: a missing reference energy or a mismatched stoichiometry
    must stop the run, not quietly contribute zero.
    """


def stoichiometry(atoms_or_symbols) -> Dict[str, int]:
    """``{element: count}`` for an ASE ``Atoms`` or any iterable of symbols.

    >>> stoichiometry(["C", "O", "O"]) == {"C": 1, "O": 2}
    True
    """
    if hasattr(atoms_or_symbols, "get_chemical_symbols"):
        symbols: Iterable[str] = atoms_or_symbols.get_chemical_symbols()
    elif isinstance(atoms_or_symbols, str):
        symbols = [atoms_or_symbols]
    else:
        symbols = atoms_or_symbols
    counts = Counter(str(s) for s in symbols)
    if not counts:
        raise ThermoError("no atoms: cannot build a thermochemical cycle")
    return dict(counts)


def monatomic_enthalpy_correction(temperature: float = 298.15) -> float:
    """``H(T) - E_elec`` for a free atom, in eV.

    5/2 kT: 3/2 kT of translation plus kT of PV. No vibrational or rotational
    terms exist for a single atom, which is the reason this is so often taken to
    be zero.

    >>> round(monatomic_enthalpy_correction() * EV_TO_KJ_PER_MOL, 3)
    6.197
    """
    if temperature < 0:
        raise ThermoError(f"temperature must be >= 0 K, got {temperature}")
    return 2.5 * _KB_EV_PER_K * temperature


def atomization_enthalpy(
    atoms_or_symbols,
    molecule_energy: float,
    atom_energies: Mapping[str, float],
    *,
    molecule_correction: float = 0.0,
    temperature: float = 298.15,
    atom_corrections: Optional[Mapping[str, float]] = None,
) -> float:
    """``sum H(atoms) - H(molecule)`` at ``temperature``, in eV (positive).

    ``molecule_energy`` and ``atom_energies`` are electronic energies in eV, as a
    calculator returns them. ``molecule_correction`` is the molecule's
    ``H(T) - E_elec`` (its ZPE plus thermal terms, e.g. from
    ``ase.thermochemistry.IdealGasThermo.get_enthalpy(T) - E_elec``).

    Each reference atom's ``H(T) - E_elec`` is added automatically -- pass
    ``atom_corrections`` only to override an element (e.g. to include an
    electronic-degeneracy term), never to zero one out.
    """
    counts = stoichiometry(atoms_or_symbols)
    missing = sorted(el for el in counts if el not in atom_energies)
    if missing:
        raise ThermoError(
            f"no reference energy for {', '.join(missing)}; the cycle needs one "
            f"free-atom energy per element in the molecule "
            f"({', '.join(sorted(counts))})")

    default_correction = monatomic_enthalpy_correction(temperature)
    total_atoms = 0.0
    for element, n in counts.items():
        correction = (atom_corrections or {}).get(element, default_correction)
        total_atoms += n * (atom_energies[element] + correction)
    return total_atoms - (molecule_energy + molecule_correction)


def formation_enthalpy(
    atoms_or_symbols,
    atomization_enthalpy_ev: float,
    atom_formation_enthalpies_kj: Mapping[str, float],
) -> float:
    """Standard formation enthalpy of the molecule, in kJ/mol.

    ``dHf(molecule) = sum n_i dHf(atom_i, g) - atomization_enthalpy``.

    ``atom_formation_enthalpies_kj`` are the standard formation enthalpies of the
    GASEOUS atoms at the same temperature, in kJ/mol, and are reference data the
    caller supplies (NIST-JANAF / CODATA -- e.g. C(g) 716.68, O(g) 249.18 at
    298.15 K). They are deliberately not tabulated here: this module owns the
    algebra, not the thermochemical data.
    """
    counts = stoichiometry(atoms_or_symbols)
    missing = sorted(el for el in counts
                     if el not in atom_formation_enthalpies_kj)
    if missing:
        raise ThermoError(
            f"no standard atomic formation enthalpy for {', '.join(missing)}; "
            f"supply one per element from NIST-JANAF/CODATA rather than "
            f"omitting it")
    total = sum(n * atom_formation_enthalpies_kj[el] for el, n in counts.items())
    return total - atomization_enthalpy_ev * EV_TO_KJ_PER_MOL


# --------------------------------------------------------------------------- #
# Error-cancelling reaction route
# --------------------------------------------------------------------------- #
# Preferred over atomization for a formation enthalpy. Atomization breaks every
# bond, so the functional's per-bond error accumulates into the answer: B3LYP on
# CO2's 1608 kJ/mol atomization is out by ~17 kJ/mol, and bare Hartree-Fock by
# ~616. A reaction that conserves bond count and type (CO + 1/2 O2 -> CO2) lets
# those errors cancel between the two sides, which is why it reaches useful
# accuracy at the same cost.
def species(symbols, energy: float, *, correction: Optional[float] = None,
            coefficient: float = 1.0) -> dict:
    """One participant in a reaction.

    ``energy`` is the electronic energy in eV. ``correction`` is that species'
    ``H(T) - E_elec`` in eV; leave it None for a single ATOM (5/2 kT is supplied)
    but pass it for anything polyatomic -- omitting it there silently drops the
    zero-point energy, which is tens of kJ/mol.
    """
    counts = stoichiometry(symbols)
    return {"counts": counts, "formula": _formula(counts), "energy": float(energy),
            "correction": correction, "coefficient": float(coefficient)}


def _formula(counts: Mapping[str, int]) -> str:
    return "".join(f"{el}{counts[el]}" for el in sorted(counts))


def _enthalpy(entry: Mapping, temperature: float) -> float:
    """A species' ``H(T)`` in eV, insisting the correction was considered."""
    correction = entry["correction"]
    natoms = sum(entry["counts"].values())
    if correction is None:
        if natoms > 1:
            raise ThermoError(
                f"{entry['formula']} is polyatomic but has no H(T)-E_elec: pass "
                f"correction= (e.g. IdealGasThermo.get_enthalpy(T) - E_elec), or "
                f"correction=0.0 deliberately for a bare electronic difference. "
                f"Leaving it out drops the zero-point energy.")
        correction = monatomic_enthalpy_correction(temperature)
    return entry["energy"] + correction


def _element_totals(entries: Iterable[Mapping]) -> Dict[str, float]:
    totals: Dict[str, float] = {}
    for entry in entries:
        for element, n in entry["counts"].items():
            totals[element] = totals.get(element, 0.0) + n * entry["coefficient"]
    return totals


def reaction_enthalpy(reactants, products, *, temperature: float = 298.15) -> float:
    """``sum v H(products) - sum v H(reactants)`` at ``temperature``, in eV.

    Both sides are sequences from :func:`species`. The reaction must balance --
    an unbalanced one yields a number with no meaning, so it raises instead.
    """
    reactants, products = list(reactants), list(products)
    if not reactants or not products:
        raise ThermoError("a reaction needs at least one species on each side")
    left, right = _element_totals(reactants), _element_totals(products)
    if {k: round(v, 6) for k, v in left.items()} != {k: round(v, 6) for k, v in right.items()}:
        raise ThermoError(
            f"reaction does not balance: reactants {left} vs products {right}. "
            f"Fix the coefficients rather than the arithmetic.")
    out = sum(e["coefficient"] * _enthalpy(e, temperature) for e in products)
    return out - sum(e["coefficient"] * _enthalpy(e, temperature) for e in reactants)


def formation_enthalpy_via_reaction(
    target_symbols,
    reactants,
    products,
    reference_formation_enthalpies_kj: Mapping[str, float],
    *,
    temperature: float = 298.15,
) -> float:
    """``dHf(target)`` in kJ/mol by Hess's law from a balanced reaction.

    The target must appear among ``products``. Every OTHER species in the
    reaction needs a known standard formation enthalpy, keyed by formula as
    :func:`_formula` spells it (``"C1O1"`` for CO, ``"O2"`` for O2) -- 0.0 for an
    element in its standard state. Those are reference data the caller supplies;
    this module owns the algebra and the signs, not the thermochemical tables.

    ``dHf(target) = [dH_rxn + sum v dHf(reactants) - sum v dHf(other products)] / v_target``
    """
    reactants, products = list(reactants), list(products)
    target = _formula(stoichiometry(target_symbols))
    matches = [p for p in products if p["formula"] == target]
    if not matches:
        raise ThermoError(
            f"target {target} is not among the products "
            f"({', '.join(p['formula'] for p in products)}); Hess's law here "
            f"solves for a product's formation enthalpy")
    coefficient = sum(m["coefficient"] for m in matches)

    others = reactants + [p for p in products if p["formula"] != target]
    missing = sorted({o["formula"] for o in others
                      if o["formula"] not in reference_formation_enthalpies_kj})
    if missing:
        raise ThermoError(
            f"no standard formation enthalpy for {', '.join(missing)}; supply one "
            f"per reference species (0.0 for an element in its standard state) "
            f"rather than omitting it")

    d_rxn_kj = reaction_enthalpy(reactants, products,
                                 temperature=temperature) * EV_TO_KJ_PER_MOL
    known = sum(r["coefficient"] * reference_formation_enthalpies_kj[r["formula"]]
                for r in reactants)
    known -= sum(p["coefficient"] * reference_formation_enthalpies_kj[p["formula"]]
                 for p in products if p["formula"] != target)
    return (d_rxn_kj + known) / coefficient
