"""The structure guard: a crystal run computes the crystal the plan named.

Run 75f06090 asked for diamond silicon (mp-149) and computed a band gap of 0 for
a structure built with Fd-3m origin-2 coordinates (0,0,0): 4 atoms 2.10 A apart,
still Fd-3m, so the space group alone didn't expose it -- the atom count per
primitive cell and the volume per atom did.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ase = pytest.importorskip("ase")
from ase import Atoms
from ase.build import bulk
from ase.spacegroup import crystal

HELPERS = Path(__file__).resolve().parents[2] / "modules" / "06_code_configuration_builder" / "bundle_helpers"
sys.path.insert(0, str(HELPERS))
import twain_structure_guard as G

A = 5.431
SI = {"formula": "Si", "name": "Silicon", "space_group": "Fd-3m", "space_group_number": 227,
      "mp_id": "mp-149", "primitive_sites": 2, "volume_per_atom": 20.1648}


def _run_75f06090():
    return crystal(symbols=["Si"], basis=[(0, 0, 0)], spacegroup=227,
                   cellpar=[A, A, A, 90, 90, 90], setting=2, primitive_cell=True)


class TestCheck:
    def test_the_regression_is_caught(self):
        with pytest.raises(G.StructureMismatch) as info:
            G.check(_run_75f06090(), SI)
        msg = str(info.value)
        assert "4 atoms per primitive cell, but the reference has 2" in msg
        assert "volume per atom is 10.0" in msg and "bulk(" in msg

    @pytest.mark.parametrize("atoms", [
        bulk("Si", "diamond", a=A),
        bulk("Si", "diamond", a=A * 0.99),          # a PBE relaxation moves it a little
        bulk("Si", "diamond", a=A).repeat(2),       # a supercell of the right crystal
        bulk("Si", "diamond", a=A, cubic=True),     # the conventional cell
    ])
    def test_the_right_crystal_passes(self, atoms):
        G.check(atoms, SI)

    @pytest.mark.parametrize("atoms", [
        Atoms("Si", cell=[10, 10, 10], pbc=True),   # a reference atom in a box
        bulk("Cu", "fcc", a=3.6),                   # another species
        Atoms("Si2", positions=[[0, 0, 0], [0, 0, 2.3]], pbc=False),  # not periodic
    ])
    def test_what_is_not_the_target_is_not_judged(self, atoms):
        G.check(atoms, SI)

    def test_a_squeezed_cell_is_caught_without_a_reference(self):
        bare = {"formula": "Si", "space_group_number": 227}
        with pytest.raises(G.StructureMismatch, match="covalent minimum"):
            G.check(bulk("Si", "diamond", a=3.0), bare)

    def test_formulas_reduce(self):
        assert G.reduced_formula({"Ti": 2, "O": 4}) == G.reduced_formula(G.parse_formula("TiO2"))
        assert G.parse_formula("Ca(OH)2") == {}         # out of scope, never misjudged


class TestInTheJob:
    """End to end, as the node runs it: PYTHONPATH -> sitecustomize -> guard."""

    MAIN = (
        "from ase.calculators.emt import EMT\n"
        "from ase.spacegroup import crystal\n"
        "from ase.build import bulk\n"
        "import sys\n"
        "def build():\n"
        "    if sys.argv[1] == 'bad':\n"
        "        return crystal(symbols=['Si'], basis=[(0,0,0)], spacegroup=227,\n"
        "                       cellpar=[5.431]*3+[90]*3, setting=2, primitive_cell=True)\n"
        "    return bulk('Si', 'diamond', a=5.431)\n"
        "atoms = build()\n"
        "atoms.calc = EMT()\n"
        "print('computed')\n"
    )

    def _bundle(self, tmp_path):
        for name in ("twain_structure_guard.py", "sitecustomize.py"):
            shutil.copy(HELPERS / name, tmp_path / name)
        (tmp_path / "twain_expected_structure.json").write_text(json.dumps(SI))
        (tmp_path / "main.py").write_text(self.MAIN)
        return tmp_path

    def _run(self, bundle, which, **env):
        environment = {**os.environ, "PYTHONPATH": str(bundle), **env}
        return subprocess.run([sys.executable, "main.py", which], cwd=bundle, env=environment,
                              capture_output=True, text=True, timeout=120, check=False)

    def test_the_wrong_cell_fails_in_main_py(self, tmp_path):
        done = self._run(self._bundle(tmp_path), "bad")
        assert done.returncode != 0 and "computed" not in done.stdout
        assert "TWAIN_STRUCTURE_MISMATCH" in done.stderr
        assert 'File "' in done.stderr and "main.py" in done.stderr   # repairable traceback

    def test_the_right_cell_runs(self, tmp_path):
        done = self._run(self._bundle(tmp_path), "good")
        assert done.returncode == 0 and "computed" in done.stdout

    def test_it_can_be_switched_off(self, tmp_path):
        done = self._run(self._bundle(tmp_path), "bad", TWAIN_STRUCTURE_GUARD="0")
        assert done.returncode == 0 and "computed" in done.stdout


class TestBuildLooksUpTheReference:
    """BUILD: Materials Project's cell goes into the plan, its facts into the guard."""

    POSCAR = ("Si2\n1.0\n0.0 2.7347 2.7347\n2.7347 0.0 2.7347\n2.7347 2.7347 0.0\nSi\n2\n"
              "direct\n0.875 0.875 0.875 Si\n0.125 0.125 0.125 Si\n")

    def _machine(self):
        import statemachine as SM
        steps = []
        fake = type("M", (), {"_progress": lambda self, *a, **k: steps.append(a)})()
        return SM, fake, steps

    def _plan(self):
        return {"target_system": {"kind": "crystal", "formula": "Si", "crystal": {
            "formula": "Si", "name": "Silicon", "space_group": "Fd-3m",
            "space_group_number": 227, "mp_id": "mp-149"}}}

    def test_with_a_reference(self, monkeypatch):
        SM, fake, steps = self._machine()
        monkeypatch.setattr(SM.mp_reference, "reference_structure", lambda *a, **k: {
            "mp_id": "mp-149", "formula": "Si", "space_group_number": 227, "space_group": "Fd-3m",
            "primitive_sites": 2, "volume_per_atom": 20.16, "poscar": self.POSCAR})
        plan = self._plan()
        expected = SM.StateMachine._crystal_reference(fake, plan, None)
        assert expected["primitive_sites"] == 2 and expected["volume_per_atom"] == 20.16
        cell = plan["target_system"]["structure"]          # what codegen builds verbatim
        assert len(cell["atoms"]) == 2 and len(cell["lattice"]) == 3
        assert "mp-149" in steps[-1][3]                   # shown as a BUILD subtask

    def test_without_a_reference_the_plan_still_guards(self, monkeypatch):
        SM, fake, _ = self._machine()
        monkeypatch.setattr(SM.mp_reference, "reference_structure", lambda *a, **k: None)
        plan = self._plan()
        expected = SM.StateMachine._crystal_reference(fake, plan, None)
        assert expected["space_group_number"] == 227 and "primitive_sites" not in expected
        assert "structure" not in plan["target_system"]

    def test_a_molecule_gets_no_guard(self, monkeypatch):
        SM, fake, _ = self._machine()
        monkeypatch.setattr(SM.mp_reference, "reference_structure",
                            lambda *a, **k: pytest.fail("no lookup for a molecule"))
        plan = {"target_system": {"kind": "molecule", "formula": "C9H8O4"}}
        assert SM.StateMachine._crystal_reference(fake, plan, None) is None


def test_reference_structure_without_a_key_is_none(monkeypatch):
    from cross_validation import mp_reference
    monkeypatch.delenv("MP_API_KEY", raising=False)
    assert mp_reference.reference_structure("Si", mp_id="mp-149") is None
