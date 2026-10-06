"""Library availability from the env specs, for the ECS worker (P2, #171).

The worker can't see RIS storage, so it can't run the import probe; it reads
the same answer from scripts/ris/envs/*.yml. Checked against the live probe
on 2026-10-06: 22 of 23 entries agreed (OpenMM differs: importable through a
transitive dependency, but declared by no spec -- conservatively "not").
"""
import pytest

import statemachine as SM
from runner import capabilities


@pytest.fixture
def specs(monkeypatch):
    monkeypatch.setattr(SM, "_CLUSTER_ENV_SPECS_CACHE", {
        "default": frozenset({"ase", "numpy", "pymatgen"}),
        "psi4": frozenset({"psi4", "ase"}),
    })
    yield
    SM._CLUSTER_ENV_SPECS_CACHE = None


def _entries(*names_imports):
    return [{"name": n, "import_name": i} for n, i in names_imports]


def test_an_entry_is_installed_only_when_one_spec_declares_its_packages(specs):
    rows = {r["name"]: r for r in capabilities.resolve_from_specs(
        _entries(("Psi4", "psi4"), ("Pymatgen", "pymatgen"), ("OpenMM", "openmm")))}
    assert rows["Psi4"]["installed"] and rows["Psi4"]["env"] == "psi4"
    assert rows["Pymatgen"]["installed"] and "default.yml" in rows["Pymatgen"]["detail"]
    assert not rows["OpenMM"]["installed"]


def test_the_registry_import_path_beats_a_name_guess(specs):
    # EMT is ase.calculators.emt -- ASE's package, not one called "emt".
    row, = capabilities.resolve_from_specs(_entries(("EMT", "ase")))
    assert row["installed"] and row["env"] == "default"


def test_publish_from_specs_never_raises(monkeypatch):
    class BrokenDB:
        def replace_library_availability(self, rows):
            raise RuntimeError("db down")
    monkeypatch.setattr(capabilities, "resolve_from_specs", lambda: [{"name": "x", "installed": True}])
    assert capabilities.publish_from_specs(BrokenDB()) == 0
