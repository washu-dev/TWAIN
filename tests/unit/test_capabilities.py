"""Unit tests for runner.capabilities (the app's "what TWAIN can run" list).

Two findings from probing the real cluster are pinned here, because both made the
list assert capability the cluster did not have:

  * ``ase.calculators.espresso`` imports wherever ASE is installed, so an import
    probe reported Quantum ESPRESSO, ABINIT, CP2K and NWChem as available on a
    cluster with none of those engines. Engines are proved by their BINARY.
  * crediting the first env alphabetically said the ``abinit`` env provides RDKit,
    ASE, Pymatgen and NWChem, because every env inherits the common stack.

Run from the repo root with:  pixi run pytest tests/unit/test_capabilities.py
"""
import sys

import pytest

from runner import capabilities as cap


def _fake_envs(tmp_path, names, *, binaries=None):
    """Env dirs with a working interpreter, plus any fake engine binaries."""
    binaries = binaries or {}
    envs = {}
    for name in names:
        bindir = tmp_path / name / "bin"
        bindir.mkdir(parents=True)
        python = bindir / "python"
        python.symlink_to(sys.executable)
        for exe in binaries.get(name, []):
            (bindir / exe).write_text("#!/bin/sh\n")
        envs[name] = str(python)
    return envs


class TestRegistryEntries:
    def test_every_entry_resolves_an_import_name(self):
        """name.lower() is wrong for the entries that matter, so the inferencer
        is reused rather than a second mapping that can drift."""
        entries = cap.registry_entries()
        assert len(entries) > 20
        assert [e for e in entries if not e.get("import_name")] == []
        by_name = {e["name"]: e for e in entries}
        assert by_name["scikit-learn"]["import_name"] == "sklearn"
        assert by_name["Open Babel"]["import_name"] == "openbabel"
        assert by_name["OpenFF Toolkit"]["import_name"] == "openff.toolkit"

    def test_both_registries_are_represented(self):
        kinds = {e["kind"] for e in cap.registry_entries()}
        assert kinds == {"library", "calculator"}

    def test_separate_program_engines_declare_an_executable(self):
        """Without this the engines fall back to an import that proves nothing."""
        calcs = {e["name"]: e for e in cap.registry_entries()
                 if e["kind"] == "calculator"}
        for engine in ("Quantum ESPRESSO", "ABINIT", "CP2K", "NWChem", "DFTB+"):
            assert calcs[engine].get("executable"), f"{engine} has no executable"
        # GPAW really is a Python package -- it must keep the import check.
        assert not calcs["GPAW"].get("executable")


class TestEnginesAreProvedByTheirBinary:
    ENTRY = {"kind": "calculator", "name": "Quantum ESPRESSO",
             "import_name": "ase.calculators.espresso", "executable": "pw.x"}

    def test_an_importable_wrapper_is_not_enough(self, tmp_path):
        """The false positive: ASE present, engine absent.

        ase.calculators.espresso imports in any env with ASE, so this used to
        report Quantum ESPRESSO as installed on a cluster without it.
        """
        envs = _fake_envs(tmp_path, ["default"])          # no pw.x anywhere
        row = cap.resolve_availability([self.ENTRY], envs)[0]
        assert row["installed"] is False
        assert "pw.x" in row["detail"]

    def test_the_binary_makes_it_installed(self, tmp_path):
        envs = _fake_envs(tmp_path, ["default", "qe"], binaries={"qe": ["pw.x"]})
        row = cap.resolve_availability([self.ENTRY], envs)[0]
        assert row["installed"] is True
        assert row["env"] == "qe"
        assert "pw.x" in row["detail"]

    def test_a_python_calculator_still_uses_the_import(self, tmp_path):
        entry = {"kind": "calculator", "name": "JSONCalc",
                 "import_name": "json", "executable": None}
        envs = _fake_envs(tmp_path, ["default"])
        row = cap.resolve_availability([entry], envs)[0]
        assert row["installed"] is True
        assert "importable" in row["detail"]


class TestEnvAttribution:
    def test_the_env_named_after_the_tool_wins(self, tmp_path):
        """Alphabetical order credited 'abinit' with providing everything."""
        entry = {"kind": "library", "name": "psi4", "import_name": "json"}
        envs = _fake_envs(tmp_path, ["abinit", "default", "psi4"])
        assert cap.resolve_availability([entry], envs)[0]["env"] == "psi4"

    def test_otherwise_the_shared_stack_wins(self, tmp_path):
        """Common libraries live in default; an engine env merely inherits them."""
        entry = {"kind": "library", "name": "RDKit", "import_name": "json"}
        envs = _fake_envs(tmp_path, ["abinit", "cp2k", "default"])
        assert cap.resolve_availability([entry], envs)[0]["env"] == "default"

    def test_attribution_is_stable_without_default(self, tmp_path):
        entry = {"kind": "library", "name": "Whatever", "import_name": "json"}
        envs = _fake_envs(tmp_path, ["cp2k", "abinit"])
        assert cap.resolve_availability([entry], envs)[0]["env"] == "abinit"


class TestItNeverAssertsWhatItCannotSee:
    def test_no_envs_means_nothing_is_claimed(self):
        entries = [{"kind": "library", "name": "RDKit", "import_name": "json"}]
        row = cap.resolve_availability(entries, {})[0]
        assert row["installed"] is False
        assert "no provisioned envs" in row["detail"]

    def test_an_entry_without_an_import_name_is_explained(self, tmp_path):
        entries = [{"kind": "library", "name": "Mystery", "import_name": None}]
        row = cap.resolve_availability(entries, _fake_envs(tmp_path, ["default"]))[0]
        assert row["installed"] is False
        assert "no import name" in row["detail"]

    def test_a_broken_interpreter_is_not_a_crash(self, tmp_path):
        (tmp_path / "bad" / "bin").mkdir(parents=True)
        (tmp_path / "bad" / "bin" / "python").write_text("not an interpreter\n")
        envs = {"bad": str(tmp_path / "bad" / "bin" / "python")}
        rows = cap.resolve_availability(
            [{"kind": "library", "name": "RDKit", "import_name": "json"}], envs)
        assert rows[0]["installed"] is False

    def test_env_interpreters_ignores_dirs_without_a_python(self, tmp_path):
        (tmp_path / "empty").mkdir()
        (tmp_path / "real" / "bin").mkdir(parents=True)
        (tmp_path / "real" / "bin" / "python").symlink_to(sys.executable)
        found = cap.env_interpreters(tmp_path)
        assert set(found) == {"real"}

    def test_publish_swallows_a_database_failure(self):
        """A stale list beats a runner that dies at start-up."""
        class Db:
            def replace_library_availability(self, rows):
                raise RuntimeError("db down")
        assert cap.publish(Db(), timeout=5) == 0

    def test_publish_writes_what_it_resolved(self, monkeypatch):
        written = {}

        class Db:
            def replace_library_availability(self, rows):
                written["rows"] = list(rows)

        monkeypatch.setattr(cap, "resolve_availability",
                            lambda **kw: [{"kind": "library", "name": "X",
                                           "installed": True}])
        assert cap.publish(Db()) == 1
        assert written["rows"][0]["name"] == "X"


@pytest.mark.parametrize("name,expected", [
    ("Quantum ESPRESSO", "quantumespresso"),
    ("DFTB+", "dftb"),
    ("scikit-learn", "scikitlearn"),
    ("CP2K", "cp2k"),
])
def test_env_key_normalisation(name, expected):
    assert cap._env_key(name) == expected
