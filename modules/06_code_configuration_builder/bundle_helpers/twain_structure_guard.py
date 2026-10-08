"""TWAIN structure guard: the crystal a run computes must be the one the plan named.

Shipped verbatim into a crystal bundle together with ``sitecustomize.py`` and
``twain_expected_structure.json``. Python imports ``sitecustomize`` at start-up
when the bundle is on PYTHONPATH (the job payload puts it there), and that calls
:func:`install`, which wraps ``ase.Atoms.calc``. The first time the script hands
a calculator a candidate for the target -- periodic, the target's formula, not an
isolated atom in a box -- the structure is checked:

* its space group (spglib) must be the planned one, when the plan names one;
* with a Materials Project reference: the same number of atoms per primitive
  cell, and a volume per atom within 30% of it;
* no two atoms may sit closer than 0.65x the sum of their covalent radii.

A mismatch raises inside the script's own ``atoms.calc = ...`` line, so the
traceback points at main.py and the runtime repair loop gets the exact reason.
Run 75f06090 is why: "diamond Si" was built with Fd-3m origin-2 coordinates
(0,0,0) -- the 16c site -- giving 4 atoms 2.10 A apart, a metal, and a 0 eV band
gap reported as a result. The generation prompt warned about precisely this.

Opt out with TWAIN_STRUCTURE_GUARD=0. Never breaks start-up: anything unexpected
here (no ASE, no spglib, no expectation file) disables the check, it doesn't fail.
"""
from __future__ import annotations

import json
import os
import re
import sys
from math import gcd
from pathlib import Path

EXPECTED_FILE = "twain_expected_structure.json"
#: Closest allowed approach, as a fraction of the two covalent radii's sum.
MIN_DISTANCE_FACTOR = 0.65
#: Above this volume per atom a 1-2 atom cell is an isolated reference atom in a box.
ISOLATED_VOLUME_PER_ATOM = 60.0

_expected: dict | None = None
_checked: set = set()


class StructureMismatch(RuntimeError):
    """The structure being computed is not the crystal the plan asked for."""


def reduced_formula(counts: dict) -> str:
    """{'Si': 4} -> 'Si'; {'Ti': 2, 'O': 4} -> 'O2Ti' (sorted, deterministic)."""
    counts = {el: int(n) for el, n in counts.items() if int(n) > 0}
    if not counts:
        return ""
    divisor = 0
    for n in counts.values():
        divisor = gcd(divisor, n)
    return "".join(f"{el}{n // divisor if n // divisor != 1 else ''}"
                   for el, n in sorted(counts.items()))


def parse_formula(formula: str) -> dict:
    """'TiO2' -> {'Ti': 1, 'O': 2}. Brackets/hydrates are out of scope (returns {})."""
    if not formula or any(ch in formula for ch in "()[]·."):
        return {}
    counts: dict = {}
    for el, n in re.findall(r"([A-Z][a-z]?)(\d*)", str(formula).replace(" ", "")):
        counts[el] = counts.get(el, 0) + (int(n) if n else 1)
    return counts


#: Volume per atom may differ from the reference by this fraction (PBE relaxation
#: moves it by a few percent; run 75f06090's wrong cell was off by 50%).
VOLUME_TOLERANCE = 0.30


def _primitive_sites(atoms):
    try:
        import spglib
        cell = (atoms.cell[:], atoms.get_scaled_positions(), atoms.get_atomic_numbers())
        found = spglib.find_primitive(cell, symprec=0.1)
        return len(found[2]) if found else None
    except Exception:  # noqa: BLE001 - spglib absent or undecided: no claim
        return None


def _space_group(atoms):
    try:
        import spglib
    except ImportError:
        return None, None
    cell = (atoms.cell[:], atoms.get_scaled_positions(), atoms.get_atomic_numbers())
    try:
        dataset = spglib.get_symmetry_dataset(cell, symprec=0.1)
    except Exception:  # noqa: BLE001 - spglib can't say; don't guess
        return None, None
    if dataset is None:
        return None, None
    number = getattr(dataset, "number", None)
    symbol = getattr(dataset, "international", None)
    if number is None and isinstance(dataset, dict):
        number, symbol = dataset.get("number"), dataset.get("international")
    return number, symbol


def _closest_pair(atoms):
    """(distance, i, j, limit) for the pair furthest inside its covalent limit, or None."""
    from ase.data import covalent_radii
    n = len(atoms)
    if n < 2 or n > 400:
        return None
    dist = atoms.get_all_distances(mic=True)
    radii = [covalent_radii[z] for z in atoms.get_atomic_numbers()]
    worst = None
    for i in range(n):
        for j in range(i + 1, n):
            limit = MIN_DISTANCE_FACTOR * (radii[i] + radii[j])
            if dist[i][j] < limit and (worst is None or dist[i][j] / limit < worst[0] / worst[3]):
                worst = (float(dist[i][j]), i, j, limit)
    return worst


def check(atoms, expected: dict) -> None:
    """Raise StructureMismatch if ``atoms`` is a candidate for the target but wrong."""
    target = parse_formula(expected.get("formula") or "")
    if not target or not all(atoms.pbc):
        return
    symbols = atoms.get_chemical_symbols()
    counts = {el: symbols.count(el) for el in set(symbols)}
    if reduced_formula(counts) != reduced_formula(target):
        return                      # another species (a reference, a molecule): not ours to judge
    if len(atoms) <= 2 and atoms.get_volume() / len(atoms) > ISOLATED_VOLUME_PER_ATOM:
        return                      # an isolated atom in a vacuum box
    problems = []
    want = expected.get("space_group_number")
    number, symbol = _space_group(atoms)
    if want and number is not None and int(number) != int(want):
        problems.append(f"its space group is {symbol} (No. {number}), but the plan asked for "
                        f"{expected.get('space_group') or ''} (No. {want})".replace("  ", " "))
    want_sites = expected.get("primitive_sites")
    sites = _primitive_sites(atoms) if want_sites else None
    if want_sites and sites is not None and int(sites) != int(want_sites):
        problems.append(f"it has {sites} atoms per primitive cell, but the reference has "
                        f"{want_sites}")
    want_volume = expected.get("volume_per_atom")
    if want_volume:
        volume = atoms.get_volume() / len(atoms)
        if abs(volume / float(want_volume) - 1.0) > VOLUME_TOLERANCE:
            problems.append(f"its volume per atom is {volume:.1f} A^3, against "
                            f"{float(want_volume):.1f} A^3 in the reference")
    worst = _closest_pair(atoms)
    if worst is not None:
        d, i, j, limit = worst
        problems.append(f"atoms {i} and {j} ({symbols[i]}-{symbols[j]}) are {d:.2f} A apart, "
                        f"closer than the {limit:.2f} A covalent minimum")
    if problems:
        name = expected.get("name") or expected.get("formula")
        ref = f" (Materials Project {expected['mp_id']})" if expected.get("mp_id") else ""
        raise StructureMismatch(
            f"TWAIN_STRUCTURE_MISMATCH: the structure given to the calculator is not "
            f"{name}{ref}: " + "; ".join(problems) + f". It has {len(atoms)} atoms in its cell. "
            f"Rebuild it -- for an element or simple binary use ase.build.bulk(...) with the "
            f"right prototype (e.g. bulk('Si', 'diamond', a=5.431)) instead of hand-written "
            f"Wyckoff coordinates.")


def _fingerprint(atoms):
    return (tuple(atoms.get_atomic_numbers()), tuple(round(x, 3) for x in atoms.cell.lengths()),
            tuple(round(x, 2) for x in atoms.cell.angles()))


def install(bundle_dir: str | os.PathLike | None = None) -> bool:
    """Wrap ``ase.Atoms.calc`` so attaching a calculator checks the structure."""
    global _expected
    if os.environ.get("TWAIN_STRUCTURE_GUARD", "1") == "0":
        return False
    path = Path(bundle_dir or Path(__file__).resolve().parent) / EXPECTED_FILE
    try:
        _expected = json.loads(path.read_text(encoding="utf-8"))
        from ase.atoms import Atoms
    except Exception:  # noqa: BLE001 - no expectation or no ASE: nothing to guard
        return False
    prop = Atoms.__dict__.get("calc")
    if not isinstance(prop, property) or getattr(prop.fset, "_twain_guarded", False):
        return False

    def guarded_set(self, calc):
        if calc is not None and _expected:
            key = _fingerprint(self)
            if key not in _checked:
                _checked.add(key)
                check(self, _expected)
        prop.fset(self, calc)

    guarded_set._twain_guarded = True
    Atoms.calc = property(prop.fget, guarded_set, prop.fdel, prop.__doc__)
    print("[twain-guard] checking the target structure "
          f"({_expected.get('formula')}, space group {_expected.get('space_group_number')})",
          file=sys.stderr)
    return True
