"""The bundle's pseudopotential resolver: names come from disk, never from memory.

Quantum ESPRESSO and ABINIT ship no pseudopotentials, and their filenames are
unguessable while looking guessable -- silicon's SSSP file is
``Si.pbe-n-rrkjus_psl.1.0.0.UPF`` but ``Si.pbe-n-kjpaw_psl.1.0.0.UPF`` (oxygen's
scheme) is just as plausible and does not exist. These tests pin the two things
that make the difference: a name is only returned when the file is really there,
and anything the library cannot supply raises instead of falling back.

The fixtures mirror the real manifests byte-for-byte in shape (verified against
SSSP 1.3.0 and ONCVPSP-PBE-PDv0.4 on the cluster), so a change in either
library's format shows up here rather than in a queued job.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "modules" / "06_code_configuration_builder" / "bundle_helpers"))

import twain_pseudo as tp  # noqa: E402


# Real entries, copied from SSSP_1.3.0_PBE_efficiency.json on RIS.
SSSP = {
    "Si": {"filename": "Si.pbe-n-rrkjus_psl.1.0.0.UPF", "cutoff_wfc": 30.0,
           "cutoff_rho": 240.0, "md5": "0b0bb1205258b0d07b9f9672cf965d36",
           "pseudopotential": "100US"},
    "O": {"filename": "O.pbe-n-kjpaw_psl.0.1.UPF", "cutoff_wfc": 50.0,
          "cutoff_rho": 400.0, "md5": "0234752ac141de4415c5fc33072bef88",
          "pseudopotential": "031PAW"},
    "Fe": {"filename": "Fe.pbe-spn-kjpaw_psl.0.2.1.UPF", "cutoff_wfc": 90.0,
           "cutoff_rho": 1080.0, "md5": "e86618425769142926afa95317d90200",
           "pseudopotential": "031PAW"},
}
# Real entry shape, copied from ONCVPSP-PBE-PDv0.4/standard.djson.
DOJO = {
    "dojo_info": {"pp_type": "NC"},
    "pseudos_metadata": {
        "Si": {"basename": "Si.psp8", "Z_val": 4.0, "l_max": 2, "md5": "45c39",
               "hints": {"low": {"ecut": 14.0}, "normal": {"ecut": 18.0},
                         "high": {"ecut": 24.0}}},
        "O": {"basename": "O.psp8", "Z_val": 6.0, "l_max": 2, "md5": "abc12",
              "hints": {"low": {"ecut": 36.0}, "normal": {"ecut": 42.0},
                        "high": {"ecut": 48.0}}},
    },
}


@pytest.fixture
def sssp(tmp_path, monkeypatch):
    """A populated SSSP-style library, wired up the way the activate.d hook does."""
    d = tmp_path / "sssp"
    d.mkdir()
    (d / "SSSP_1.3.0_PBE_efficiency.json").write_text(json.dumps(SSSP))
    for meta in SSSP.values():
        (d / meta["filename"]).write_text("<upf/>")
    monkeypatch.setenv("ESPRESSO_PSEUDO", str(d))
    monkeypatch.delenv("TWAIN_PSEUDO_MANIFEST", raising=False)
    return d


@pytest.fixture
def dojo(tmp_path, monkeypatch):
    d = tmp_path / "pseudodojo"
    d.mkdir()
    (d / "standard.djson").write_text(json.dumps(DOJO))
    for meta in DOJO["pseudos_metadata"].values():
        (d / meta["basename"]).write_text("psp8")
    monkeypatch.setenv("ABINIT_PP_PATH", str(d))
    monkeypatch.delenv("TWAIN_PSEUDO_MANIFEST", raising=False)
    return d


class FakeAtoms:
    """Stands in for ase.Atoms, which the bundle has but the test env need not."""

    def __init__(self, symbols):
        self._symbols = symbols

    def get_chemical_symbols(self):
        return self._symbols


class TestEspresso:
    def test_filenames_come_from_the_manifest(self, sssp):
        got = tp.espresso_pseudopotentials(FakeAtoms(["Si", "O", "O"]))
        assert got == {"Si": "Si.pbe-n-rrkjus_psl.1.0.0.UPF",
                       "O": "O.pbe-n-kjpaw_psl.0.1.UPF"}

    def test_the_plausible_wrong_name_is_not_what_we_return(self, sssp):
        """Guard the specific confusion: Si uses rrkjus, O uses kjpaw."""
        got = tp.espresso_pseudopotentials(["Si"])
        assert got["Si"] != "Si.pbe-n-kjpaw_psl.1.0.0.UPF"

    def test_cutoffs_are_the_max_over_elements(self, sssp):
        # O needs 50/400 and Si 30/240; a cell with both must use O's.
        assert tp.espresso_cutoffs(FakeAtoms(["Si", "O"])) == (50.0, 400.0)

    def test_repeated_symbols_collapse_and_keep_order(self, sssp):
        assert list(tp.espresso_pseudopotentials(["O", "Si", "O"])) == ["O", "Si"]

    def test_a_bare_symbol_string_works(self, sssp):
        assert "Si" in tp.espresso_pseudopotentials("Si")

    def test_a_manifest_name_with_no_file_on_disk_raises(self, sssp):
        """The manifest is authoritative about names, not about what got installed.

        This is the case-sensitive-glob bug that landed 53 of 103 SSSP elements:
        the manifest listed Ag, the file was never copied, and nothing complained.
        """
        (sssp / "Si.pbe-n-rrkjus_psl.1.0.0.UPF").unlink()
        with pytest.raises(tp.PseudoLibraryError, match="missing from"):
            tp.espresso_pseudopotentials(["Si"])

    def test_an_element_outside_the_library_raises(self, sssp):
        with pytest.raises(tp.PseudoLibraryError, match="no entry for"):
            tp.espresso_pseudopotentials(["Xx"])

    def test_no_library_configured_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ESPRESSO_PSEUDO", raising=False)
        monkeypatch.delenv("TWAIN_PSEUDO_DIR", raising=False)
        with pytest.raises(tp.PseudoLibraryError, match="ESPRESSO_PSEUDO"):
            tp.espresso_pseudopotentials(["Si"])

    def test_a_directory_with_no_manifest_raises(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("ESPRESSO_PSEUDO", str(empty))
        monkeypatch.delenv("TWAIN_PSEUDO_MANIFEST", raising=False)
        with pytest.raises(tp.PseudoLibraryError, match="no pseudopotential manifest"):
            tp.espresso_pseudopotentials(["Si"])

    def test_an_explicit_manifest_env_var_wins(self, sssp, tmp_path, monkeypatch):
        other = tmp_path / "pinned.json"
        other.write_text(json.dumps({"Si": {"filename": "Si.pbe-n-rrkjus_psl.1.0.0.UPF",
                                            "cutoff_wfc": 11.0, "cutoff_rho": 88.0}}))
        monkeypatch.setenv("TWAIN_PSEUDO_MANIFEST", str(other))
        assert tp.espresso_cutoffs(["Si"]) == (11.0, 88.0)


class TestAbinit:
    def test_pp_paths_is_the_library_directory(self, dojo):
        assert tp.abinit_pp_paths(["Si"]) == [str(dojo)]

    def test_absolute_psp8_paths_are_returned(self, dojo):
        got = tp.abinit_pseudopotentials(FakeAtoms(["Si", "O"]))
        assert got == [str(dojo / "Si.psp8"), str(dojo / "O.psp8")]

    def test_ecut_is_the_max_normal_hint(self, dojo):
        # PseudoDojo hints: Si normal 18.0, O normal 42.0.
        assert tp.abinit_ecut(FakeAtoms(["Si", "O"])) == 42.0

    def test_the_djson_wrapper_key_is_unwrapped(self, dojo):
        """standard.djson nests entries under pseudos_metadata, SSSP does not."""
        assert tp.abinit_pseudopotentials(["O"])[0].endswith("O.psp8")

    def test_a_missing_psp8_raises(self, dojo):
        (dojo / "Si.psp8").unlink()
        with pytest.raises(tp.PseudoLibraryError, match="missing from"):
            tp.abinit_pseudopotentials(["Si"])


class TestItRunsStandaloneInABundle:
    """The file is copied into a bundle and imported by main.py, not installed."""

    def test_it_imports_with_no_third_party_packages(self, sssp, tmp_path):
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        (bundle / "twain_pseudo.py").write_text(
            Path(tp.__file__).read_text(encoding="utf-8"), encoding="utf-8")
        script = ("import json\n"
                  "from twain_pseudo import espresso_pseudopotentials, espresso_cutoffs\n"
                  "print(json.dumps({'p': espresso_pseudopotentials(['Si', 'O']),\n"
                  "                  'c': espresso_cutoffs(['Si', 'O'])}))\n")
        (bundle / "use_it.py").write_text(script)
        proc = subprocess.run([sys.executable, "use_it.py"], cwd=bundle,
                              capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["p"]["Si"] == "Si.pbe-n-rrkjus_psl.1.0.0.UPF"
        assert out["c"] == [50.0, 400.0]
