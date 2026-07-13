"""Calculator discovery for module 04_method_discovery.

A *calculator* is the compute engine that provides the physics (total energy,
electronic structure, ...); a *driver library* (ASE, Pymatgen -- see
``configs/discovery_registry.json``) builds the atomic system and orchestrates
the run. TWAIN picks both: e.g. the band gap of silicon is computed by the
**GPAW** calculator driven through the **ASE** library.

This module answers two questions the planner needs:

  1. *What property is the researcher asking for?* -- :func:`canonical_property`
     maps free-text objectives / acceptance-metric names ("what is the band gap
     of silicon", "band_gap", "electronic band structure") onto a canonical
     property key ("band_gap").
  2. *Which python calculator can compute it?* -- :func:`select_calculator`
     returns the best :class:`CalculatorEntry` whose ``capabilities`` cover that
     property (or ``None`` when the property needs no dedicated calculator, e.g.
     a cheminformatics descriptor).

Everything here is offline and deterministic: it reads the curated JSON catalog
and does simple keyword/capability matching. No network, no heavy imports.
"""
from __future__ import annotations

import json
import platform as _platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _repo_root() -> Path:
    """Locate the repo root by walking up to the directory holding pixi.toml."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pixi.toml").exists():
            return parent
    return here.parents[2]


DEFAULT_CALCULATOR_REGISTRY_PATH = _repo_root() / "configs" / "calculator_registry.json"


def current_platform() -> str:
    """The conda-style platform subdir we're running on (e.g. 'osx-arm64').

    Used to filter out calculators with no build for this platform, so discovery
    never selects a tool that can't actually install/run here.
    """
    machine = (_platform.machine() or "").lower()
    is_arm = machine in {"arm64", "aarch64"}
    if sys.platform == "darwin":
        return "osx-arm64" if is_arm else "osx-64"
    if sys.platform.startswith("linux"):
        return "linux-aarch64" if is_arm else "linux-64"
    if sys.platform.startswith("win"):
        return "win-64"
    return "unknown"


@dataclass
class CalculatorEntry:
    """One python calculator plus the driver library it plugs into."""

    id: str
    name: str
    description: str
    import_name: str
    pip_name: str
    capabilities: List[str]
    driver_library: str = "ASE"
    structure_builder: str = "ase.build"
    version: Optional[str] = None
    input_kinds: List[str] = field(default_factory=list)
    heavy: bool = False
    domains: List[str] = field(default_factory=list)
    trust_tier: int = 1
    license: Optional[str] = None
    # Driver libraries this calculator plugs into (by registry name/id). Empty
    # means "no constraint" (usable with any library). This is what lets the
    # calculator follow whatever library discovery chose, instead of the code
    # forcing a particular pairing.
    compatible_libraries: List[str] = field(default_factory=list)
    # Conda platforms a build actually exists for (e.g. ["linux-64"]). Empty
    # means "any platform" (a pure-python calculator). Selection filters on this
    # so an unrunnable tool is never chosen -- GPAW has no osx-arm64 build, so it
    # must not be picked on a Mac.
    platforms: List[str] = field(default_factory=list)
    # Fidelity for the same property (higher = more accurate); a fallback-ranking
    # hint only -- the LLM discoverer reasons about fitness itself.
    fidelity: int = 1
    # Factual usability signal fed to the LLM: does a real run need external data
    # the package doesn't ship? (GPAW bundles PAW datasets = False; DFTB+ needs
    # Slater-Koster .skf files = True.) Lets discovery prefer self-contained tools.
    needs_external_data: bool = False
    # True for ML surrogate predictors (MatGL/CHGNet) that regress a learned model
    # instead of solving the electronic structure. Discovery prefers a REAL
    # first-principles/electronic-structure calculation over these -- the user
    # wants actual calculations, not predictions (twain-prefer-real-calculations).
    ml_surrogate: bool = False

    def covers(self, property_key: str) -> bool:
        """Whether this calculator can compute ``property_key`` (case-insensitive)."""
        wanted = (property_key or "").lower()
        return wanted in {c.lower() for c in self.capabilities}

    def available_on(self, platform: Optional[str]) -> bool:
        """Whether a build of this calculator exists for ``platform``.

        Empty ``platforms`` = available everywhere (pure python). ``platform``
        None = don't filter (treat as available).

        >>> find_calculator("GPAW").available_on("linux-64")
        True
        >>> find_calculator("GPAW").available_on("osx-arm64")
        False
        """
        if not self.platforms or not platform:
            return True
        return platform.lower() in {p.lower() for p in self.platforms}

    def supports_library(self, library: Optional[str]) -> bool:
        """Whether this calculator can be driven by ``library`` (name or id).

        An empty ``compatible_libraries`` means no constraint (any library).

        >>> find_calculator("GPAW").supports_library("ASE")
        True
        >>> find_calculator("GPAW").supports_library("Pymatgen")
        False
        """
        if not self.compatible_libraries:
            return True
        if not library:
            return False
        return library.strip().lower() in {c.lower() for c in self.compatible_libraries}


# --------------------------------------------------------------------------- #
# Property canonicalization.
#
# Ordered so that the most specific phrases win (band_gap before the generic
# electronic_structure). Each key is a canonical property; the tuple lists the
# free-text needles that map onto it. Matching is substring, case-insensitive.
# --------------------------------------------------------------------------- #
_PROPERTY_ALIASES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("band_gap", (
        "band gap", "bandgap", "band-gap", "band_gap",
        "energy gap", "fundamental gap", "optical gap", "electronic gap",
        "homo-lumo gap", "homo lumo gap",
    )),
    ("band_structure", ("band structure", "band-structure", "band_structure", "e-k dispersion")),
    ("density_of_states", ("density of states", "density-of-states", "dos ", " dos", "pdos")),
    ("total_energy", (
        "total energy", "ground-state energy", "ground state energy",
        "cohesive energy", "formation energy", "atomization energy",
    )),
    ("forces", ("interatomic force", "atomic forces", "force on each atom")),
)
# NOTE: this table is a *seed* of common properties for the deterministic path,
# not the decision-maker. A vague ask that names no specific observable
# ("do a DFT calculation", "electronic properties") intentionally returns None
# here rather than being forced onto any one property -- the LLM discoverer
# (llm_discovery) reasons about the actual task from the free-text objective. Add
# a property by giving it a key + phrases; nothing is hardcoded to a single domain.


def canonical_property(text: str) -> Optional[str]:
    """Map free text (objective + acceptance-metric names) to a canonical property.

    Returns ``None`` when nothing recognizable is mentioned, so the planner can
    tell "needs a calculator" from "no calculator required".

    >>> canonical_property("What is the band gap of silicon?")
    'band_gap'
    >>> canonical_property("compute the electronic band structure of GaAs")
    'band_structure'
    >>> canonical_property("predict the aqueous solubility of aspirin")
    """
    haystack = (text or "").lower()
    for canonical, needles in _PROPERTY_ALIASES:
        if any(needle in haystack for needle in needles):
            return canonical
    return None


def load_calculators(path: Optional[Path] = None) -> List[CalculatorEntry]:
    """Read the calculator catalog from disk (defaults to the shipped registry)."""
    path = Path(path) if path else DEFAULT_CALCULATOR_REGISTRY_PATH
    data = json.loads(path.read_text(encoding="utf-8"))
    _CE_FIELDS = set(CalculatorEntry.__dataclass_fields__)
    return [
        CalculatorEntry(**{k: v for k, v in raw.items() if k in _CE_FIELDS})
        for raw in data.get("calculators", [])
    ]


def find_calculator(
    identifier: Optional[str],
    calculators: Optional[List[CalculatorEntry]] = None,
) -> Optional[CalculatorEntry]:
    """Look up a calculator by name or id (case-insensitive), or None.

    >>> find_calculator("GPAW").heavy
    True
    >>> find_calculator("emt").heavy
    False
    >>> find_calculator("nope") is None
    True
    """
    if not identifier:
        return None
    wanted = identifier.strip().lower()
    entries = calculators if calculators is not None else load_calculators()
    for c in entries:
        if c.id.lower() == wanted or c.name.lower() == wanted:
            return c
    return None


_UNSET = object()


def calculators_for_property(
    requested_property: Optional[str],
    calculators: Optional[List[CalculatorEntry]] = None,
    *,
    domain: Optional[str] = None,
    library: Optional[str] = None,
    platform: Optional[str] = _UNSET,
) -> List[CalculatorEntry]:
    """Calculators that can compute ``requested_property`` here, best first.

    Filtered to those that (a) cover the property, (b) are compatible with
    ``library`` when given, and (c) have a build for ``platform``. ``platform``
    defaults to the current platform (pass ``None`` to skip the platform filter,
    or an explicit subdir like "linux-64" to plan for another machine). Ordering:
    domain match, then higher fidelity, then trust tier, then cheaper engine,
    then id. Returns ``[]`` when nothing qualifies here.

    >>> calculators_for_property("band_gap", platform="linux-64")[0].id   # real DFT preferred
    'gpaw'
    >>> calculators_for_property("band_gap", platform="osx-arm64")[0].id  # real (approx) over ML
    'dftbplus'
    >>> [c.id for c in calculators_for_property("band_gap", library="Pymatgen", platform="linux-64")]
    ['matgl']
    """
    if not requested_property:
        return []
    if platform is _UNSET:
        platform = current_platform()
    entries = calculators if calculators is not None else load_calculators()
    matches = [c for c in entries if c.covers(requested_property)]
    if library is not None:
        matches = [c for c in matches if c.supports_library(library)]
    matches = [c for c in matches if c.available_on(platform)]

    dom = (domain or "").lower()

    def sort_key(c: CalculatorEntry):
        domain_ok = 0 if (dom and dom in {d.lower() for d in c.domains}) else 1
        # Prefer: domain match, then a REAL calculation over an ML surrogate (the
        # user wants actual electronic-structure calculations, not predictions),
        # then higher fidelity, self-contained, higher trust, cheaper, stable id.
        # Fallback ranking only -- the LLM discoverer weighs these itself.
        return (domain_ok, 1 if c.ml_surrogate else 0, -c.fidelity,
                0 if not c.needs_external_data else 1,
                -c.trust_tier, 0 if not c.heavy else 1, c.id)

    return sorted(matches, key=sort_key)


def select_calculator(
    requested_property: Optional[str],
    calculators: Optional[List[CalculatorEntry]] = None,
    *,
    domain: Optional[str] = None,
    library: Optional[str] = None,
    platform: Optional[str] = _UNSET,
) -> Optional[CalculatorEntry]:
    """Return the best calculator that can compute ``requested_property`` here.

    Thin wrapper over :func:`calculators_for_property` returning its top pick, or
    ``None`` when nothing qualifies (no property, none compatible with
    ``library``, or none with a build for ``platform``). ``platform`` defaults to
    the current platform -- so on a Mac, GPAW (no osx-arm64 build) is not chosen;
    a platform-available option (e.g. the self-contained MatGL predictor) is.
    Pass an explicit subdir to plan for another machine, or ``None`` to ignore.

    >>> select_calculator("band_gap", platform="linux-64").id   # real first-principles DFT
    'gpaw'
    >>> select_calculator("band_gap", platform="osx-arm64").id  # real (approx) over ML surrogate
    'dftbplus'
    >>> select_calculator("band_gap", library="ASE", platform="linux-64").id
    'gpaw'
    >>> select_calculator("band_gap", library="Pymatgen", platform="linux-64").id
    'matgl'
    >>> select_calculator(None) is None
    True
    >>> select_calculator("aqueous_solubility", platform="linux-64") is None
    True
    """
    matches = calculators_for_property(
        requested_property, calculators, domain=domain, library=library, platform=platform)
    return matches[0] if matches else None


if __name__ == "__main__":  # pragma: no cover - manual smoke of the module
    import sys

    q = " ".join(sys.argv[1:]) or "what is the band gap of silicon"
    prop = canonical_property(q)
    calc = select_calculator(prop)
    print(f"query    : {q!r}")
    print(f"property : {prop}")
    print(f"calculator: {calc.name + ' via ' + calc.driver_library if calc else None}")
