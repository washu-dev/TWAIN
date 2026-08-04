"""Resolve pseudopotentials from the library on disk -- never by guessing a name.

Copied verbatim into a RunBundle (like ``inline_tests.py``) whenever the plan's
calculator needs pseudopotentials, i.e. Quantum ESPRESSO or ABINIT. Generated
code imports it instead of spelling filenames:

    from twain_pseudo import espresso_pseudopotentials, espresso_cutoffs, pseudo_dir

    pseudos = espresso_pseudopotentials(atoms)      # {'Si': 'Si.pbe-n-rrkjus_psl.1.0.0.UPF'}
    ecutwfc, ecutrho = espresso_cutoffs(atoms)      # 30.0, 240.0  (Ry, from SSSP)
    profile = EspressoProfile(command='pw.x', pseudo_dir=pseudo_dir())

Why this file exists rather than a prompt instruction: a pseudopotential filename
is unguessable and looks guessable. Silicon's SSSP entry is
``Si.pbe-n-rrkjus_psl.1.0.0.UPF``, but ``Si.pbe-n-kjpaw_psl.1.0.0.UPF`` is just as
plausible a string -- it is the naming scheme oxygen uses -- and it does not
exist. An invented name either crashes hours into a queued job or, worse, names a
real file for a *different* pseudopotential and quietly changes the physics. So
the names come from the library's own manifest, and every one is confirmed
present on disk before it is handed back.

Cutoffs come from the same manifest for the same reason: SSSP publishes a
recommended ``cutoff_wfc``/``cutoff_rho`` per element and PseudoDojo publishes
``hints``. A guessed cutoff is a converged-looking wrong answer.

Paths come from the environment (``ESPRESSO_PSEUDO``, ``ABINIT_PP_PATH``,
``TWAIN_PSEUDO_MANIFEST``), which scripts/ris/provision_envs.sh exports through a
conda activate.d hook -- so nothing here hardcodes a cluster path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = [
    "PseudoLibraryError",
    "abinit_ecut",
    "abinit_pp_paths",
    "espresso_cutoffs",
    "espresso_pseudopotentials",
    "pseudo_dir",
]


class PseudoLibraryError(RuntimeError):
    """The library cannot supply an element -- raised instead of guessing.

    Always fail loudly: a missing pseudopotential must stop the run, not fall
    back to a default that silently computes different physics.
    """


# Environment variables provision_envs.sh writes, in preference order per engine.
_DIR_VARS = {
    "espresso": ("ESPRESSO_PSEUDO", "TWAIN_PSEUDO_DIR"),
    "abinit": ("ABINIT_PP_PATH", "TWAIN_PSEUDO_DIR"),
}
# Manifest filenames to look for when TWAIN_PSEUDO_MANIFEST is unset.
_MANIFEST_GLOBS = ("SSSP_*.json", "standard.djson")


def _symbols(atoms_or_symbols) -> List[str]:
    """Unique element symbols from an ASE ``Atoms`` or any iterable of symbols.

    Order is preserved so generated code produces stable, diffable output.
    """
    if hasattr(atoms_or_symbols, "get_chemical_symbols"):
        raw: Iterable[str] = atoms_or_symbols.get_chemical_symbols()
    elif isinstance(atoms_or_symbols, str):
        raw = [atoms_or_symbols]
    else:
        raw = atoms_or_symbols
    seen: List[str] = []
    for sym in raw:
        sym = str(sym)
        if sym not in seen:
            seen.append(sym)
    return seen


def pseudo_dir(engine: str = "espresso") -> str:
    """Directory holding the pseudopotential files, from the environment."""
    for var in _DIR_VARS.get(engine, ()):
        value = os.environ.get(var)
        if value and Path(value).is_dir():
            return value
    tried = " or ".join(_DIR_VARS.get(engine, ("TWAIN_PSEUDO_DIR",)))
    raise PseudoLibraryError(
        f"no pseudopotential directory for {engine}: set {tried} to the library "
        f"path (on RIS this is exported by the env's activate.d hook -- see "
        f"scripts/ris/provision_envs.sh, and fetch the data with "
        f"scripts/ris/fetch_data.sh)")


def _manifest_path(directory: str) -> Path:
    explicit = os.environ.get("TWAIN_PSEUDO_MANIFEST")
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    for pattern in _MANIFEST_GLOBS:
        # sorted() so a directory holding several versions resolves the same way
        # every run rather than depending on filesystem order.
        found = sorted(Path(directory).glob(pattern))
        if found:
            return found[-1]
    raise PseudoLibraryError(
        f"no pseudopotential manifest in {directory} (looked for "
        f"{', '.join(_MANIFEST_GLOBS)}, and TWAIN_PSEUDO_MANIFEST is unset). "
        f"Without it filenames and cutoffs would have to be guessed, which this "
        f"module exists to prevent.")


def _load(engine: str) -> Tuple[str, Dict[str, dict]]:
    """(directory, {element: entry}) with entries normalized across libraries.

    Each entry has ``filename`` plus, when the library publishes them,
    ``cutoff_wfc``/``cutoff_rho`` (SSSP, Ry) or ``ecut`` (PseudoDojo, Ha).
    """
    directory = pseudo_dir(engine)
    manifest = _manifest_path(directory)
    with open(manifest, encoding="utf-8") as fh:
        raw = json.load(fh)

    entries: Dict[str, dict] = {}
    if "pseudos_metadata" in raw:  # PseudoDojo .djson
        for element, meta in raw["pseudos_metadata"].items():
            hints = meta.get("hints") or {}
            normal = hints.get("normal") or {}
            entries[element] = {"filename": meta["basename"],
                                "ecut": normal.get("ecut")}
    else:  # SSSP .json
        for element, meta in raw.items():
            entries[element] = {"filename": meta["filename"],
                                "cutoff_wfc": meta.get("cutoff_wfc"),
                                "cutoff_rho": meta.get("cutoff_rho")}
    return directory, entries


def _resolve(atoms_or_symbols, engine: str) -> Tuple[str, Dict[str, dict]]:
    """Entries for exactly the requested elements, each verified on disk."""
    directory, entries = _load(engine)
    wanted = _symbols(atoms_or_symbols)

    unknown = [s for s in wanted if s not in entries]
    if unknown:
        raise PseudoLibraryError(
            f"the pseudopotential library has no entry for "
            f"{', '.join(unknown)} (library: {_manifest_path(directory).name}). "
            f"Choose a different engine for this system rather than substituting "
            f"another element's pseudopotential.")

    resolved = {s: entries[s] for s in wanted}
    # The manifest is authoritative about names, not about what was installed --
    # a partial extraction would otherwise surface as an engine crash. (SSSP
    # mixes .UPF and .upf, which is exactly how a fetch once landed 53 of 103.)
    absent = [(s, e["filename"]) for s, e in resolved.items()
              if not (Path(directory) / e["filename"]).is_file()]
    if absent:
        listing = "; ".join(f"{s} -> {fn}" for s, fn in absent)
        raise PseudoLibraryError(
            f"pseudopotential file(s) named by the manifest are missing from "
            f"{directory}: {listing}. Re-run scripts/ris/fetch_data.sh --force.")
    return directory, resolved


def espresso_pseudopotentials(atoms_or_symbols) -> Dict[str, str]:
    """``{element: filename}`` for ASE's ``Espresso(pseudopotentials=...)``."""
    _, resolved = _resolve(atoms_or_symbols, "espresso")
    return {sym: entry["filename"] for sym, entry in resolved.items()}


def espresso_cutoffs(atoms_or_symbols) -> Tuple[float, float]:
    """``(ecutwfc, ecutrho)`` in Ry: the max of SSSP's per-element recommendations.

    The maximum, not the mean: a cutoff that converges the hardest element in the
    cell converges the rest, while anything lower silently under-converges it.
    """
    _, resolved = _resolve(atoms_or_symbols, "espresso")
    wfc = [e["cutoff_wfc"] for e in resolved.values() if e.get("cutoff_wfc")]
    rho = [e["cutoff_rho"] for e in resolved.values() if e.get("cutoff_rho")]
    if not wfc or not rho:
        raise PseudoLibraryError(
            "the manifest publishes no cutoffs for these elements; do not guess "
            "-- run a convergence test and state the cutoff explicitly.")
    return max(wfc), max(rho)


def abinit_pp_paths(atoms_or_symbols=None) -> List[str]:
    """Search path list for ``AbinitProfile(pp_paths=...)``."""
    return [pseudo_dir("abinit")]


def abinit_ecut(atoms_or_symbols) -> Optional[float]:
    """PseudoDojo's recommended ``ecut`` in Hartree (max over elements)."""
    _, resolved = _resolve(atoms_or_symbols, "abinit")
    hints = [e["ecut"] for e in resolved.values() if e.get("ecut")]
    if not hints:
        raise PseudoLibraryError(
            "the manifest publishes no ecut hints for these elements; do not "
            "guess -- run a convergence test and state the cutoff explicitly.")
    return max(hints)
