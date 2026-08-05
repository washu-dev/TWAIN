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

import re
from collections import Counter
from typing import Dict, Iterable, Mapping, Optional

__all__ = [
    "EV_TO_KJ_PER_MOL",
    "ThermoError",
    "atomization_enthalpy",
    "canonical_formula",
    "formation_enthalpy",
    "formation_enthalpy_via_reaction",
    "monatomic_enthalpy_correction",
    "parse_formula",
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


_FORMULA_TOKEN = re.compile(
    r"(?P<open>[(\[{])"
    r"|(?P<close>[)\]}])(?P<group_count>\d*)"
    r"|(?P<element>[A-Z][a-z]?)(?P<count>\d*)"
)
# Hydrate / adduct separators, as they are actually written: CuSO4·5H2O,
# CuSO4.5H2O, Na2CO3*10H2O.
_SEPARATORS = "·•⋅∙*."
# Charge markers. Rejected rather than stripped -- see parse_formula.
_CHARGE = re.compile(r"[+\-^]|\d+[+\-]")


def _parse_groups(text: str, original: str) -> Dict[str, int]:
    """One formula segment, honouring nested ``()``/``[]``/``{}`` multipliers.

    A stack, because the notation nests: ``K4[Fe(CN)6]3`` closes an inner group
    inside an outer one, and each close multiplies everything it collected.
    """
    stack: list = [{}]
    position = 0
    for match in _FORMULA_TOKEN.finditer(text):
        if match.start() != position:
            raise ThermoError(_unreadable(original))
        position = match.end()
        if match.group("open"):
            stack.append({})
        elif match.group("close"):
            if len(stack) == 1:
                raise ThermoError(f"unbalanced brackets in {original!r}")
            group = stack.pop()
            if not group:
                raise ThermoError(f"empty group in {original!r}")
            digits = match.group("group_count")
            multiplier = int(digits) if digits else 1
            if multiplier < 1:
                raise ThermoError(f"group multiplier must be >= 1 in {original!r}")
            for element, n in group.items():
                stack[-1][element] = stack[-1].get(element, 0) + n * multiplier
        else:
            element = match.group("element")
            digits = match.group("count")
            n = int(digits) if digits else 1
            if n < 1:
                raise ThermoError(f"element count must be >= 1 in {original!r}")
            stack[-1][element] = stack[-1].get(element, 0) + n
    if position != len(text):
        raise ThermoError(_unreadable(original))
    if len(stack) != 1:
        raise ThermoError(f"unbalanced brackets in {original!r}")
    return stack[0]


def _unreadable(original: str) -> str:
    return (f"could not read {original!r} as a chemical formula (expected e.g. "
            f"'CO2', 'O2', 'Mg(OH)2', 'CuSO4·5H2O')")


def parse_formula(text: str) -> Dict[str, int]:
    """``{element: count}`` from a formula string: ``"CO2" -> {"C": 1, "O": 2}``.

    A formula is neither an iterable of symbols nor a single symbol, and treating
    it as either corrupts it. Iterating ``"CO2"`` yields ``"C"``, ``"O"``, ``"2"``;
    taking the whole string as one symbol -- which this module used to do -- makes
    ``"CO"`` the *element* CO and ``"O2"`` the *element* O2. That produced the
    lookup keys ``"CO1"`` and ``"O21"``, so a reference table written the obvious
    way (``{"CO": -110.53, "O2": 0.0}``) could never match and a CO2 formation run
    died at ``no standard formation enthalpy for CO1, O21`` (Slurm job 2629667).

    It also hid a worse failure: ``{"CO2": 1}`` sums to ONE atom, so a polyatomic
    molecule looked monatomic and the guard demanding its ``H(T) - E_elec``
    could not fire -- a missing correction would have silently taken 5/2 kT and
    dropped the zero-point energy.

    Handles the notation chemists actually write: nested groups
    (``Ca3(PO4)2``, ``K4[Fe(CN)6]3``) and hydrates/adducts with any of the usual
    separators (``CuSO4·5H2O``, ``CuSO4.5H2O``, ``Na2CO3*10H2O``). Written against
    the standard library on purpose -- this file is copied verbatim into every
    RunBundle, including ones that ship neither ASE nor pymatgen, so it cannot
    borrow their parsers. The tests cross-check it against ASE's, which is where
    that authority belongs.

    Two deliberate refusals, because both would otherwise produce a confident
    wrong number:

    * a CHARGE (``SO4^2-``, ``Fe3+``) is rejected rather than ignored -- silently
      dropping it makes Fe2+ and Fe3+ the same species;
    * anything else it cannot read raises, since a misparsed formula yields a
      plausible enthalpy.

    >>> parse_formula("CO2") == {"C": 1, "O": 2}
    True
    >>> parse_formula("Mg(OH)2") == {"Mg": 1, "O": 2, "H": 2}
    True
    >>> parse_formula("Ca3(PO4)2") == {"Ca": 3, "P": 2, "O": 8}
    True
    >>> parse_formula("CuSO4·5H2O") == {"Cu": 1, "S": 1, "O": 9, "H": 10}
    True
    """
    if not isinstance(text, str):
        raise ThermoError(f"expected a formula string, got {type(text).__name__}")
    stripped = text.strip()
    if not stripped:
        raise ThermoError("empty formula")
    if _CHARGE.search(stripped):
        raise ThermoError(
            f"{text!r} carries a charge; this cycle works from gas-phase "
            f"electronic energies, and dropping the charge would make e.g. Fe2+ "
            f"and Fe3+ the same species. Supply a neutral formula.")

    segments = [s for s in _split_segments(stripped)]
    if any(not s for s in segments):
        raise ThermoError(_unreadable(text))

    counts: Dict[str, int] = {}
    for index, segment in enumerate(segments):
        # A leading integer is a HYDRATE count, and only after a separator:
        # "CuSO4·5H2O" has five waters. On the first segment it would instead be a
        # stoichiometric coefficient -- "2CO" -- which belongs in species(
        # coefficient=...), not in the formula. Folding it in here would double
        # count it against a coefficient the caller also passed, so it is refused.
        leading = re.match(r"^(\d+)(.+)$", segment) if index else None
        multiplier, body = (int(leading.group(1)), leading.group(2)) if leading \
            else (1, segment)
        if multiplier < 1:
            raise ThermoError(f"hydrate multiplier must be >= 1 in {text!r}")
        for element, n in _parse_groups(body, text).items():
            counts[element] = counts.get(element, 0) + n * multiplier
    if not counts:
        raise ThermoError(_unreadable(text))
    return counts


def _split_segments(text: str) -> list:
    """Split a hydrate/adduct on its separator, keeping the pieces in order."""
    out, current = [], []
    for char in text:
        if char in _SEPARATORS:
            out.append("".join(current))
            current = []
        else:
            current.append(char)
    out.append("".join(current))
    return out


def stoichiometry(atoms_or_symbols) -> Dict[str, int]:
    """``{element: count}`` for an ASE ``Atoms``, a formula string, or symbols.

    >>> stoichiometry(["C", "O", "O"]) == {"C": 1, "O": 2}
    True
    >>> stoichiometry("CO2") == {"C": 1, "O": 2}
    True
    """
    if hasattr(atoms_or_symbols, "get_chemical_symbols"):
        symbols: Iterable[str] = atoms_or_symbols.get_chemical_symbols()
    elif isinstance(atoms_or_symbols, str):
        # A string is a FORMULA, parsed as one. Callers write species("CO2", ...)
        # and reference tables keyed "CO"/"O2"; both have to mean what they say.
        return parse_formula(atoms_or_symbols)
    else:
        symbols = atoms_or_symbols
    counts = Counter(str(s) for s in symbols)
    if not counts:
        raise ThermoError("no atoms: cannot build a thermochemical cycle")
    return dict(counts)


def _by_species(mapping: Mapping[str, float]) -> Dict[str, float]:
    """Re-key a caller's reference table by species rather than by spelling.

    Applied to EVERY reference lookup in this module, not just the reaction one.
    The same trap exists for the element-keyed tables: codegen used to instruct
    models to key references "by formula with elements sorted", which yields
    ``{"C1": 716.68, "O1": 249.18}`` for an atom table just as readily as
    ``{"C1O1": ...}`` for a reaction one -- and a raw dict lookup rejects both.

    An unparseable key is kept verbatim rather than dropped, so it simply fails to
    match and the caller is told what is missing alongside what was supplied.
    """
    out: Dict[str, float] = {}
    for key, value in mapping.items():
        try:
            out[canonical_formula(key)] = float(value)
        except (ThermoError, TypeError, ValueError):
            out[str(key)] = value
    return out


def canonical_formula(atoms_or_symbols) -> str:
    """One spelling for a species, whatever it was written as.

    ``"CO"``, ``"OC"``, ``"C1O1"`` and ``["C", "O"]`` all become ``"CO"``. Used on
    BOTH sides of every reference lookup, so a table keyed the way a chemist
    writes it matches a species built from an ASE object.
    """
    return _formula(stoichiometry(atoms_or_symbols))


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
    atom_energies = _by_species(atom_energies)
    missing = sorted(el for el in counts if el not in atom_energies)
    if missing:
        raise ThermoError(
            f"no reference energy for {', '.join(missing)}; the cycle needs one "
            f"free-atom energy per element in the molecule "
            f"({', '.join(sorted(counts))})")

    default_correction = monatomic_enthalpy_correction(temperature)
    total_atoms = 0.0
    for element, n in counts.items():
        correction = _by_species(atom_corrections or {}).get(
            element, default_correction)
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
    atom_formation_enthalpies_kj = _by_species(atom_formation_enthalpies_kj)
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
    """Canonical spelling, counts of 1 omitted: ``{"C":1,"O":2}`` -> ``"CO2"``.

    Readable on purpose. It is an internal identity key, but it also reaches the
    researcher through every error message here, and "no standard formation
    enthalpy for C1O1, O2" sends someone hunting for a species they never wrote.
    """
    return "".join(f"{el}{counts[el] if counts[el] != 1 else ''}"
                   for el in sorted(counts))


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
    reaction needs a known standard formation enthalpy -- 0.0 for an element in
    its standard state. Those are reference data the caller supplies; this module
    owns the algebra and the signs, not the thermochemical tables.

    Keys are matched by SPECIES, not by spelling: ``{"CO": -110.53, "O2": 0.0}``
    is normalised the same way the reaction's own species are, so a table written
    the way a chemist writes it matches, and so does ``"OC"`` or ``"C1O1"``.
    Requiring one internal spelling was a trap nobody could satisfy from the
    outside.

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

    # Normalise the caller's table onto the same keys the species carry, so the
    # match is on the species and not on how either side spelled it. An
    # unparseable key is kept verbatim rather than dropped: it simply will not
    # match, and the error below then names what is actually missing.
    references = _by_species(reference_formation_enthalpies_kj)

    others = reactants + [p for p in products if p["formula"] != target]
    missing = sorted({o["formula"] for o in others if o["formula"] not in references})
    if missing:
        raise ThermoError(
            f"no standard formation enthalpy for {', '.join(missing)}; supply one "
            f"per reference species (0.0 for an element in its standard state) "
            f"rather than omitting it. Supplied: "
            f"{', '.join(sorted(references)) or '(none)'}")

    d_rxn_kj = reaction_enthalpy(reactants, products,
                                 temperature=temperature) * EV_TO_KJ_PER_MOL
    known = sum(r["coefficient"] * references[r["formula"]] for r in reactants)
    known -= sum(p["coefficient"] * references[p["formula"]]
                 for p in products if p["formula"] != target)
    return (d_rxn_kj + known) / coefficient
