"""Dependency inference for generated RunBundles (Story 5.1).

Given the *tool* a plan selected (e.g. ``"pymatgen"``, ``"ASE"``, ``"deepchem"``)
this module answers three questions the codegen engine needs before it can emit a
runnable bundle:

    1. *Which* PyPI packages does the tool need?  (the tool itself + the runtime
       support packages every generated ``main.py`` relies on -- numpy, pandas,
       pyyaml).
    2. *Which version* of each?  We pin exact versions so a bundle is
       reproducible: the same ``requirements.txt`` resolves to the same wheels
       months later.
    3. *Does the package actually exist on PyPI?*  :func:`is_available_on_pypi`
       performs a best-effort lookup so a typo'd or private tool name is caught
       before the execution adapter wastes a ``pip install`` on it.

Design notes
------------
* The tool -> packages table (:data:`TOOL_REGISTRY`) is curated. Tool names are
  matched case-insensitively and by a couple of common aliases, so both the
  registry id (``"ase"``) and the display name (``"ASE"``) resolve.
* An *unknown* tool is not fatal: :func:`infer` still returns the shared runtime
  packages plus a best-effort ``pip``-installable line for the tool name itself,
  and marks it ``pinned=False`` so callers know the version is unverified.
* The PyPI check is network-only and fully injectable (``fetch`` callable), so
  the whole module is import-safe and unit-testable offline. Network failures
  yield ``None`` ("unknown"), never an exception.

The import name of a package is tracked separately from its PyPI distribution
name (they differ often: ``scikit-learn`` imports as ``sklearn``,
``PyYAML`` imports as ``yaml``, ``openff-toolkit`` imports as ``openff.toolkit``)
because the smoke tests need the *import* name to verify the tool loads.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from http.client import InvalidURL
from typing import Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"


@dataclass(frozen=True)
class Dependency:
    """One resolved Python dependency for a generated bundle.

    ``package`` is the PyPI distribution name (what ``pip install`` wants),
    ``import_name`` is the top-level module you ``import`` (they frequently
    differ), and ``version`` is the pinned exact version or ``None`` when the
    version could not be pinned (unknown tool).
    """

    package: str
    version: Optional[str]
    import_name: str
    pinned: bool = True

    def requirement_line(self) -> str:
        """Render the ``requirements.txt`` line for this dependency.

        >>> Dependency("pymatgen", "2024.6.10", "pymatgen").requirement_line()
        'pymatgen==2024.6.10'
        >>> Dependency("mytool", None, "mytool", pinned=False).requirement_line()
        'mytool'
        """
        if self.version:
            return f"{self.package}=={self.version}"
        return self.package


# --------------------------------------------------------------------------- #
# Pinned runtime support packages shared by every generated main.py.
# (Kept as exact pins so bundles are reproducible; refresh deliberately.)
# --------------------------------------------------------------------------- #
_NUMPY = Dependency("numpy", "1.26.4", "numpy")
_PANDAS = Dependency("pandas", "2.2.2", "pandas")
_PYYAML = Dependency("PyYAML", "6.0.2", "yaml")

# Runtime packages the generated scripts always use (CSV/JSON I/O helpers lean on
# pandas/numpy; main.py reads config.yaml via PyYAML when present).
COMMON_RUNTIME: List[Dependency] = [_NUMPY, _PANDAS, _PYYAML]


@dataclass
class ToolDependencies:
    """The tool package plus any extra packages that specific tool pulls in."""

    tool: Dependency
    extras: List[Dependency] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Curated tool -> package(s) table. Keys are lowercased tool ids/names; aliases
# below map alternative spellings onto a canonical key. Versions are pinned to
# real releases available as of this module's authoring for reproducibility.
# --------------------------------------------------------------------------- #
TOOL_REGISTRY: Dict[str, ToolDependencies] = {
    "pymatgen": ToolDependencies(Dependency("pymatgen", "2024.6.10", "pymatgen")),
    "ase": ToolDependencies(Dependency("ase", "3.23.0", "ase")),
    "rdkit": ToolDependencies(Dependency("rdkit", "2024.3.5", "rdkit")),
    "deepchem": ToolDependencies(
        Dependency("deepchem", "2.8.0", "deepchem"),
        extras=[Dependency("scikit-learn", "1.5.1", "sklearn")],
    ),
    "chemprop": ToolDependencies(Dependency("chemprop", "1.7.1", "chemprop")),
    "scikit-learn": ToolDependencies(Dependency("scikit-learn", "1.5.1", "sklearn")),
    "mordred": ToolDependencies(
        Dependency("mordredcommunity", "2.0.6", "mordred"),
        extras=[Dependency("rdkit", "2024.3.5", "rdkit")],
    ),
    "openmm": ToolDependencies(Dependency("openmm", "8.1.1", "openmm")),
    "openff-toolkit": ToolDependencies(Dependency("openff-toolkit", "0.16.2", "openff.toolkit")),
    "pyscf": ToolDependencies(Dependency("pyscf", "2.6.2", "pyscf")),
    "psi4": ToolDependencies(Dependency("psi4", "1.9.1", "psi4")),
    "xtb": ToolDependencies(Dependency("xtb-python", "22.1", "xtb")),
    # DFT calculators attached to a driver library (usually ASE). GPAW pulls in
    # ASE, which the generated ASE+GPAW scripts also import directly.
    "gpaw": ToolDependencies(
        Dependency("gpaw", "24.6.0", "gpaw"),
        extras=[Dependency("ase", "3.23.0", "ase")],
    ),
    # DFTB+ (density-functional tight binding); driven via ASE. Conda-only in
    # practice (needs the dftb+ binary + Slater-Koster params). The package has
    # no python module, so the *import* probe targets ASE's wrapper while the
    # requirement line still installs the `dftbplus` package.
    "dftbplus": ToolDependencies(
        Dependency("dftbplus", "24.1", "ase.calculators.dftb"),
        extras=[Dependency("ase", "3.23.0", "ase")],
    ),
    # Binary DFT/QC codes driven via ASE wrappers (no importable python module of
    # their own -> the import probe targets the ASE wrapper).
    "qe": ToolDependencies(
        Dependency("qe", "7.5", "ase.calculators.espresso"),
        extras=[Dependency("ase", "3.23.0", "ase")],
    ),
    "abinit": ToolDependencies(
        Dependency("abinit", "10.0.3", "ase.calculators.abinit"),
        extras=[Dependency("ase", "3.23.0", "ase")],
    ),
    "cp2k": ToolDependencies(
        Dependency("cp2k", "2026.1", "ase.calculators.cp2k"),
        extras=[Dependency("ase", "3.23.0", "ase")],
    ),
    "nwchem": ToolDependencies(
        Dependency("nwchem", "7.3.1", "ase.calculators.nwchem"),
        extras=[Dependency("ase", "3.23.0", "ase")],
    ),
    # Real python packages (import the module directly).
    "tblite": ToolDependencies(
        Dependency("tblite", "0.6.0", "tblite"),
        extras=[Dependency("ase", "3.23.0", "ase")],
    ),
    "matgl": ToolDependencies(
        Dependency("matgl", "4.0.3", "matgl"),
        extras=[Dependency("pymatgen", "2024.6.10", "pymatgen")],
    ),
    "chgnet": ToolDependencies(Dependency("chgnet", "0.4.2", "chgnet")),
    "mdanalysis": ToolDependencies(Dependency("MDAnalysis", "2.10.0", "MDAnalysis")),
    "mdtraj": ToolDependencies(Dependency("mdtraj", "1.11.1", "mdtraj")),
}

# Packages pip can never install on the execution host, KNOWN ahead of time.
# This set is NOT the primary defense (planning also asks PyPI directly, which
# catches any absent package without curation -- see statemachine's
# ``_cluster_cannot_run``); it exists for the cases PyPI's index can't
# reveal: a source distribution exists but can't build on a compute node
# (gpaw needs a compiled MPI stack; xtb-python ships no wheels), plus it keeps
# the known conda-forge/binary-only codes (psi4, qe, abinit, cp2k, nwchem,
# dftbplus) blockable offline. A plan needing one of these is only runnable
# where a pre-provisioned env (scripts/ris/envs/*.yml) already provides it.
CONDA_ONLY_PACKAGES = frozenset({
    "psi4", "xtb-python", "gpaw", "dftbplus", "qe", "abinit", "cp2k", "nwchem",
    # PyPI holds only a yanked 0.18.0 placeholder, so pip finds "versions: none"
    # (runs 21ffdacd, 786bd6b1); conda-forge is the real distribution.
    "openff-toolkit",
})

# Packages that ARE on PyPI but cannot realistically be installed at job start.
# The cluster check treats "pip can serve it" as "the job's pip fallback can get
# it", which holds for a pure-Python package and not for a machine-learned
# potential: matgl and chgnet pull PyTorch, which is multiple GB of wheels
# downloaded onto a compute node inside the run's own wall-clock budget, with no
# provisioned env to fall back on (no scripts/ris/envs/*.yml declares either).
#
# The cost of getting this wrong is a plan that looks runnable and is not: a NaCl2
# heat-of-formation run reached EXECUTE and died on "No module named matgl" after
# trying MACE, CHGNet and M3GNet in turn (Slurm job 2633871). Provision an env for
# one of these and it can come straight off this list -- the veto reads the specs.
CLUSTER_UNRUNNABLE_PACKAGES = frozenset({
    "matgl", "chgnet", "mace-torch", "torch", "dgl",
})


# Alternative spellings -> canonical registry key.
_ALIASES: Dict[str, str] = {
    "scikit learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "openff": "openff-toolkit",
    "openff toolkit": "openff-toolkit",
    "openff_toolkit": "openff-toolkit",
    "mordred-legacy": "mordred",
    "rdkit-pypi": "rdkit",
    "xtb-python": "xtb",
    "dftb+": "dftbplus",
    "dftb": "dftbplus",
    "quantum espresso": "qe",
    "quantum-espresso": "qe",
    "espresso": "qe",
    "mdanalysis": "mdanalysis",
    "open babel": "openbabel",
    "openbabel": "openbabel",
}


def canonical_tool_key(tool_name: str) -> str:
    """Normalize a tool name to its :data:`TOOL_REGISTRY` key.

    Matching is case-insensitive and alias-aware.

    >>> canonical_tool_key("Pymatgen")
    'pymatgen'
    >>> canonical_tool_key("scikit learn")
    'scikit-learn'
    >>> canonical_tool_key("SomeUnknownTool")
    'someunknowntool'
    """
    key = (tool_name or "").strip().lower()
    return _ALIASES.get(key, key)


#: How a plan joins several tools into one name ("OpenMM+OpenFF Toolkit+RDKit").
_TOOL_SEPARATORS = re.compile(r"\s*(?:\+|,|&|/|;|\band\b|\bwith\b)\s*", re.IGNORECASE)


def tool_keys(tool_name: str) -> List[str]:
    """The registry keys a tool name stands for -- several when a plan combined them.

    A known name (an alias like ``dftb+`` included) is one tool. Anything else is
    split on ``+ , & / ; and with``, so a combined name doesn't reach PyPI as one
    package name with spaces in it -- an ``InvalidURL`` (#188). An unknown part
    keeps its words joined by hyphens, PyPI-style.

    >>> tool_keys("OpenMM+OpenFF Toolkit+RDKit")
    ['openmm', 'openff-toolkit', 'rdkit']
    >>> tool_keys("DFTB+")
    ['dftbplus']
    >>> tool_keys("Some New Tool")
    ['some-new-tool']
    >>> tool_keys("openff.toolkit")     # an import name, as a failed job reports it
    ['openff-toolkit']
    """
    key = canonical_tool_key(tool_name)
    key = _BY_IMPORT_NAME.get(key, key)
    if key in TOOL_REGISTRY or key in _ALIASES.values():
        return [key]
    keys: List[str] = []
    for part in _TOOL_SEPARATORS.split(key) if key else []:
        part = canonical_tool_key(part)
        if part and part not in TOOL_REGISTRY:
            part = re.sub(r"\s+", "-", part)
        if part and part not in keys:
            keys.append(part)
    return keys


#: A registry tool by its import name ("openff.toolkit" -> "openff-toolkit").
_BY_IMPORT_NAME: Dict[str, str] = {
    str(entry.tool.import_name).lower(): key for key, entry in TOOL_REGISTRY.items()
    if entry.tool.import_name and str(entry.tool.import_name).lower() != key}


def _unknown(key: str) -> Dependency:
    safe = key or "unknown-tool"
    return Dependency(safe, None, safe.replace("-", "_"), pinned=False)


def infer(tool_name: str) -> List[Dependency]:
    """Infer the full pinned dependency list for a tool.

    Returns the tool package (+ any tool-specific extras) followed by the shared
    runtime packages, de-duplicated by PyPI name (first pin wins).

    An unknown tool still yields a usable list: the runtime packages plus an
    *unpinned* best-effort line for the tool name itself.

    >>> [d.requirement_line() for d in infer("pymatgen")]
    ['pymatgen==2024.6.10', 'numpy==1.26.4', 'pandas==2.2.2', 'PyYAML==6.0.2']
    >>> infer("TotallyMadeUpTool")[0].pinned
    False
    """
    # An unknown tool: assume its name is itself pip-installable/importable, but
    # don't pretend we know a good version to pin.
    resolved: List[Dependency] = import_names(tool_name)
    resolved.extend(COMMON_RUNTIME)

    seen: set[str] = set()
    unique: List[Dependency] = []
    for dep in resolved:
        if dep.package.lower() in seen:
            continue
        seen.add(dep.package.lower())
        unique.append(dep)
    return unique


def import_names(tool_name: str) -> List[Dependency]:
    """Return only the *tool* dependencies (excluding shared runtime packages).

    Used by the smoke-test generator, which import-checks the scientific tool
    (and its heavy extras) but not numpy/pandas/pyyaml -- those are covered by
    the common runtime and rarely the cause of a "missing tool" failure.

    >>> [d.import_name for d in import_names("deepchem")]
    ['deepchem', 'sklearn']
    """
    deps: List[Dependency] = []
    for key in tool_keys(tool_name) or [""]:
        entry = TOOL_REGISTRY.get(key)
        deps.extend([entry.tool, *entry.extras] if entry is not None else [_unknown(key)])
    return deps


def requirements_txt(tool_name: str, *, header: Optional[str] = None) -> str:
    """Render a complete ``requirements.txt`` body for a tool.

    >>> print(requirements_txt("ase"))  # doctest: +NORMALIZE_WHITESPACE
    # Auto-generated by TWAIN code_configuration_builder -- pinned for reproducibility.
    ase==3.23.0
    numpy==1.26.4
    pandas==2.2.2
    PyYAML==6.0.2
    """
    lines = [header or "# Auto-generated by TWAIN code_configuration_builder -- pinned for reproducibility."]
    lines.extend(dep.requirement_line() for dep in infer(tool_name))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# PyPI availability check (best-effort, network-only, injectable).
# --------------------------------------------------------------------------- #
def _default_fetch(url: str) -> dict:
    """Fetch and parse a PyPI JSON payload using only the standard library."""
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "TWAIN-codegen/0.1"})
    with urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted URL template)
        return json.loads(resp.read().decode("utf-8"))


def is_available_on_pypi(
    package: str,
    version: Optional[str] = None,
    *,
    fetch: Optional[Callable[[str], dict]] = None,
) -> Optional[bool]:
    """Return whether ``package`` (optionally at ``version``) exists on PyPI.

    ``True`` = found (and, if a version was given, that version is a real
    release); ``False`` = the package (or that specific version) does not exist;
    ``None`` = could not determine (network/parse error) -- callers should treat
    ``None`` as "unverified", not "missing".

    The ``fetch`` callable is injectable so this is fully testable offline:

    >>> catalog = {"pymatgen": {"releases": {"2024.6.10": []}}}
    >>> def fake(url):
    ...     name = url.split("/pypi/")[1].split("/")[0]
    ...     if name in catalog:
    ...         return catalog[name]
    ...     raise HTTPError(url, 404, "Not Found", {}, None)
    >>> is_available_on_pypi("pymatgen", fetch=fake)
    True
    >>> is_available_on_pypi("pymatgen", "2024.6.10", fetch=fake)
    True
    >>> is_available_on_pypi("pymatgen", "9.9.9", fetch=fake)
    False
    >>> is_available_on_pypi("no-such-pkg-xyz", fetch=fake)
    False
    """
    fetch = fetch or _default_fetch
    try:
        payload = fetch(PYPI_JSON_URL.format(package=package))
    except HTTPError as exc:
        # 404 is a definitive "not found"; other HTTP errors are inconclusive.
        return False if exc.code == 404 else None
    except (URLError, InvalidURL, ValueError, json.JSONDecodeError, KeyError):
        return None

    if version is None:
        return True
    releases = payload.get("releases") or {}
    return version in releases


if __name__ == "__main__":  # pragma: no cover - manual smoke of the module
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "pymatgen"
    print(requirements_txt(name))
    print("import checks:", [d.import_name for d in import_names(name)])
