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
import json
import os
import subprocess
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


class TestItWorksOutsidePytest:
    """The bug this class exists for lived entirely in pytest-vs-runner difference.

    ``tests/conftest.py`` pre-registers the ``execution_adapter`` and ``code_gen``
    aliases, so every other test here resolved them fine -- while the runner, which
    publishes from ``main()`` before any job imports ``_bootstrap``, could not
    import ``ClusterProfile`` at all and published all 27 entries as "no
    provisioned envs were visible to the runner" with eight provisioned envs on
    disk. Only a bare interpreter reproduces that, so these tests spend a
    subprocess to get one.
    """

    def _bare(self, expression, **extra_env):
        """Evaluate an expression in a fresh interpreter rooted at the repo.

        The cluster env vars are stripped, not just left unset: inheriting a
        TWAIN_ENVS_ROOT from the developer's shell would short-circuit the very
        code path under test.
        """
        env = {k: v for k, v in os.environ.items()
               if k not in ("TWAIN_ENVS_ROOT", "TWAIN_SLURM_CLUSTER")}
        env.update(extra_env)
        done = subprocess.run(
            [sys.executable, "-c", f"import runner.capabilities as c; print({expression})"],
            cwd=str(cap.REPO_ROOT), env=env,
            capture_output=True, text=True, timeout=180, check=False,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def test_the_envs_root_resolves_without_the_pytest_bootstrap(self):
        profile = json.loads(
            (cap.REPO_ROOT / "configs" / "clusters" / "compute2.json").read_text())
        assert self._bare("c._envs_root()") == profile["envs_root"]

    def test_the_import_names_resolve_without_the_pytest_bootstrap(self):
        """The inferencer is reached through the ``code_gen`` alias too, and its
        fallback returns a plausible-looking wrong name rather than nothing."""
        assert self._bare("c._import_name_for('scikit-learn')") == "sklearn"

    def test_an_explicit_envs_root_still_wins(self, tmp_path):
        """Deployments that set the env var must not need a resolvable profile."""
        assert self._bare("c._envs_root()", TWAIN_ENVS_ROOT=str(tmp_path)) == str(tmp_path)


@pytest.mark.parametrize("name,expected", [
    ("Quantum ESPRESSO", "quantumespresso"),
    ("DFTB+", "dftb"),
    ("scikit-learn", "scikitlearn"),
    ("CP2K", "cp2k"),
])
def test_env_key_normalisation(name, expected):
    assert cap._env_key(name) == expected


class TestHomepageTravelsWithTheEntry:
    """A link per library, sourced from the registry rather than the app.

    The URL is static identity, not probed state, so by rights it belongs beside
    the registry entry. It is published through this table anyway for a concrete
    reason: the API image is built from ./api alone, so configs/ is not in it and
    the API cannot read the registries at request time. The runner already reads
    both, so it carries the URL along with the verdict.
    """

    def test_every_registry_entry_offers_a_link(self):
        """A screen that lists 27 things with links on some of them looks broken."""
        missing = [f"{e['kind']}:{e['name']}" for e in cap.registry_entries()
                   if not e.get("homepage")]
        assert missing == [], f"no homepage for {missing}"

    def test_every_url_is_absolute_http(self):
        """A relative or mailto: value would reach Linking.openURL and fail there."""
        for entry in cap.registry_entries():
            url = entry["homepage"]
            assert url.startswith(("http://", "https://")), f"{entry['name']}: {url}"
            assert " " not in url, f"{entry['name']}: {url}"

    def test_a_declared_homepage_wins_over_the_repository(self):
        """Both fields exist on the library registry; the project's own site is the
        better destination for someone learning what a tool is."""
        assert cap._homepage({"homepage": "https://pymatgen.org",
                              "repo_url": "https://github.com/materialsproject/pymatgen"}) \
            == "https://pymatgen.org"

    def test_a_repository_is_used_when_that_is_all_there_is(self):
        assert cap._homepage({"repo_url": "https://github.com/x/y"}) \
            == "https://github.com/x/y"

    def test_a_junk_url_is_dropped_rather_than_published(self):
        """None renders no link. A malformed one renders a link that goes nowhere."""
        for item in ({}, {"homepage": ""}, {"homepage": "   "}, {"homepage": None},
                     {"homepage": "pymatgen.org"}, {"homepage": "javascript:alert(1)"},
                     {"homepage": 42}, {"repo_url": "ftp://example.com"}):
            assert cap._homepage(item) is None, item

    def test_the_url_survives_availability_resolution(self, tmp_path):
        """resolve_availability rebuilds each row, so the field has to be carried."""
        entry = {"kind": "library", "name": "RDKit", "import_name": "json",
                 "homepage": "https://www.rdkit.org"}
        row = cap.resolve_availability([entry], _fake_envs(tmp_path, ["default"]))[0]
        assert row["homepage"] == "https://www.rdkit.org"

    def test_a_registry_without_urls_still_publishes(self):
        """The column is nullable and the app renders no link -- a registry that has
        not been given URLs must not break the list."""
        rows = cap.resolve_availability(
            [{"kind": "library", "name": "Mystery", "import_name": "json"}], {})
        assert rows[0].get("homepage") is None
