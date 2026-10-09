"""Library-only property runs must synthesize REAL code, not the generic stub.

Regression guard for the bug where a run that picked a library which computes the
property itself (e.g. PySCF for a molecular HOMO-LUMO gap) fell back to
``template_generic.py`` -- producing a results CSV with no computed property.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "modules" / "06_code_configuration_builder"))
sys.path.insert(0, str(REPO_ROOT / "modules" / "16_agent_mesh_control_plane"))

from codegen_engine import CodegenEngine, SynthesisFailed, _GENERIC  # noqa: E402

# A minimal, valid PySCF HOMO-LUMO script a stub "agent" returns.
PYSCF_SCRIPT = '''\
import argparse, csv, json


def compute():
    from pyscf import gto, scf
    mol = gto.M(atom="Ti 0 0 0; O 0 0 1.6; O 0 0 -1.6", basis="sto-3g")
    mf = scf.RHF(mol)
    mf.kernel()
    occ = [e for e, o in zip(mf.mo_energy, mf.mo_occ) if o > 0]
    vir = [e for e, o in zip(mf.mo_energy, mf.mo_occ) if o == 0]
    return (min(vir) - max(occ)) * 27.2114


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="results.csv")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    gap = 3.0 if args.smoke else compute()
    print(json.dumps({"band_gap": gap, "tool": "PySCF",
                      "property": "band_gap", "output_file": args.output}))
    with open(args.output, "w", newline="") as f:
        csv.writer(f).writerows([["band_gap"], [gap]])


if __name__ == "__main__":
    main()
'''


def _plan(**over):
    plan = {
        "selected_method": {"tool_name": "PySCF", "libraries": ["PySCF"],
                            "calculator": None, "calculator_import": None,
                            "calculator_library": None},
        "requested_property": "band_gap",
        "acceptance_metrics": [{"metric_name": "band_gap", "target_value": 3.0, "tolerance": 1.0}],
        "target_system": {"molecule": {"name": "Titanium dioxide", "SMILES": "O=[Ti]=O"},
                          "formula": "TiO2"},
        "metadata": {"timestamp": "2026-07-13T00:00:00Z"},
        "safety_notes": [],
    }
    plan.update(over)
    return plan


# ── BUILD ────────────────────────────────────────────────────────────────────

def test_library_only_property_run_synthesizes_real_code():
    bundle = CodegenEngine().generate(_plan(), agent=lambda prompt: PYSCF_SCRIPT)
    assert bundle.template_name == "llm_synthesized"
    assert "generic_run" not in bundle.main_py          # not the stub
    assert "pyscf" in bundle.main_py.lower()            # actually uses the library
    cfg = _parse_yaml(bundle.config_yaml)
    assert cfg["template"] == "llm_synthesized"
    assert cfg["environment"] is None                   # library-only -> default interpreter


def test_library_only_falls_back_to_template_without_agent():
    bundle = CodegenEngine().generate(_plan(), agent=None)
    assert bundle.template_name == _GENERIC.filename    # deterministic, offline


def test_library_only_falls_back_when_synthesis_invalid():
    # Agent reply never references the library -> rejected -> generic fallback.
    bundle = CodegenEngine().generate(_plan(), agent=lambda p: "print('hi')\n")
    assert bundle.template_name == _GENERIC.filename


def test_library_run_with_nothing_to_compute_stays_generic():
    # Neither a requested_property NOR an acceptance metric: nothing names a
    # quantity, so there is genuinely nothing to synthesize and the template
    # stands. (A plan with a metric but no requested_property is a different
    # case -- the metric names the quantity, so it gets real code. Treating the
    # two alike is what scaffolded Slurm job 2571447; see
    # TestNullRequestedPropertyStillGetsRealCode.)
    bundle = CodegenEngine().generate(
        _plan(requested_property=None, acceptance_metrics=[]),
        agent=lambda p: PYSCF_SCRIPT)
    assert bundle.template_name == _GENERIC.filename


def _parse_yaml(text):
    import yaml
    return yaml.safe_load(text)


# ── REPAIR gate ────────────────────────────────────────────────────────────────

class _FakeDoctor:
    def __init__(self):
        self.called = False

    def heal(self, source):
        self.called = True
        return SimpleNamespace(source=source, status="healthy", rounds=0,
                               fixes=[], remaining=[], to_dict=lambda: {"status": "healthy"})


def _machine(tmp_path, **kw):
    import statemachine as SM
    from crash_recovery import DataStorage
    with patch.object(DataStorage, "load", return_value=None):
        m = SM.StateMachine(data_path=str(tmp_path / "s.json"), run_id="t", **kw)
    m.artifacts_dir = tmp_path
    return m


def _seed_bundle(tmp_path, template):
    bundle = tmp_path / "run_bundle_t"
    bundle.mkdir(exist_ok=True)
    (bundle / "main.py").write_text(
        "def main():\n    print('ok')\n\nif __name__ == '__main__':\n    main()\n")
    (bundle / "config.yaml").write_text(f"template: {template}\nenvironment: null\n")
    plan = {"selected_method": {"tool_name": "PySCF", "libraries": ["PySCF"],
                                "calculator": None, "calculator_import": None,
                                "calculator_library": None},
            "requested_property": "band_gap", "acceptance_metrics": []}
    (tmp_path / "execution_plan_t.json").write_text(json.dumps(plan))
    return bundle


def test_repair_heals_library_only_synthesized_bundle(tmp_path):
    from states import State
    bundle = _seed_bundle(tmp_path, "llm_synthesized")
    fake = _FakeDoctor()
    m = _machine(tmp_path, script_doctor=fake)
    m.context.artifacts["run_bundle"] = str(bundle)
    m.context.artifacts["execution_plan"] = str(tmp_path / "execution_plan_t.json")
    assert m.repair() == State.EXECUTE
    assert fake.called, "repair() skipped a library-only LLM-synthesized bundle"


def test_repair_skips_generic_stub_bundle(tmp_path):
    from states import State
    bundle = _seed_bundle(tmp_path, "template_generic.py")
    fake = _FakeDoctor()
    m = _machine(tmp_path, script_doctor=fake)
    m.context.artifacts["run_bundle"] = str(bundle)
    m.context.artifacts["execution_plan"] = str(tmp_path / "execution_plan_t.json")
    assert m.repair() == State.EXECUTE
    assert not fake.called, "repair() should not heal a deterministic/stub bundle"


# ── smoke exercises the compute call (the fix for the uncaught MatGL kwarg bug) ──

def test_generate_threads_smoke_compute_into_prompt():
    """smoke_compute=True must instruct the model to RUN the computation in --smoke."""
    from codegen_engine import CodegenEngine
    seen = {}

    def agent(prompt):
        seen["prompt"] = prompt
        return PYSCF_SCRIPT

    CodegenEngine().generate(_plan(), agent=agent, smoke_compute=True)
    assert "run the real" in seen["prompt"]             # exercise the real compute
    assert "Do NOT stub" in seen["prompt"]

    CodegenEngine().generate(_plan(), agent=agent, smoke_compute=False)
    assert "construct/load the calculator" in seen["prompt"]   # load-only
    assert "run the real" not in seen["prompt"]


def test_build_chooses_smoke_compute_by_calculator_cost(tmp_path):
    """A cheap self-contained calculator computes in smoke; a heavy one only loads."""
    import statemachine as SM
    # Patch the CodegenEngine the state machine actually uses (imported as
    # code_gen.codegen_engine), not the test's direct import of the same file.
    CE = SM.CodegenEngine
    seen = []
    orig = CE.generate

    def spy(self, plan, **kw):
        seen.append(kw.get("smoke_compute"))
        return orig(self, plan, **kw)

    calc_script = ("import matgl\nimport gpaw\n\n"
                   "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n")

    def _run(calc, calc_import):
        plan = {"selected_method": {"tool_name": "ASE", "tool_version": 3.29,
                                    "libraries": ["ASE"], "calculator": calc,
                                    "calculator_import": calc_import, "calculator_library": "ASE"},
                "requested_property": "band_gap", "acceptance_metrics": [],
                "metadata": {"timestamp": "2026-07-13T00:00:00Z"}}
        (tmp_path / "execution_plan_t.json").write_text(json.dumps(plan))
        m = _machine(tmp_path, agent=lambda p: calc_script)
        m.context.artifacts["execution_plan"] = str(tmp_path / "execution_plan_t.json")
        m.build()

    with patch.object(CE, "generate", spy):
        _run("MatGL", "matgl")   # cheap, self-contained -> compute
        _run("GPAW", "gpaw")     # heavy DFT -> load-only
        _run("DFTB+", "ase.calculators.dftb")  # heavy+external-data but smoke_can_compute -> compute
    assert seen == [True, False, True], f"unexpected smoke_compute decisions: {seen}"


# ── crystal polymorph must reach codegen (the anatase-built-as-rutile bug) ─────

def test_material_brief_reads_crystal_descriptor():
    """A solid-state target is described under `crystal`, not `molecule`; its
    polymorph + space group must be extracted, else only the formula survives and
    the model builds the most common polymorph (rutile) instead of the requested."""
    from codegen_engine import CodegenEngine
    plan = {"target_system": {
        "crystal": {"name": "anatase titanium dioxide", "phase": "anatase",
                    "formula": "TiO2", "crystal_system": "tetragonal",
                    "space_group": "I41/amd", "space_group_number": 141},
        "formula": "TiO2"}}
    brief = CodegenEngine._material_brief(plan, None)
    assert brief["name"] == "anatase titanium dioxide"
    assert brief["space_group"] == "I41/amd"
    assert brief["space_group_number"] == 141
    desc = CodegenEngine._material_desc(brief)
    assert "anatase" in desc.lower()
    assert "I41/amd" in desc and "141" in desc
    # the phase is named even when the `name` itself doesn't carry it
    d2 = CodegenEngine._material_desc(
        {"name": "titanium dioxide", "formula": "TiO2", "phase": "anatase"})
    assert d2.lower().startswith("anatase titanium dioxide")


def test_material_desc_molecule_path_unchanged():
    """Regression guard: the molecule descriptor still yields 'name (formula)'."""
    from codegen_engine import CodegenEngine
    plan = {"target_system": {"molecule": {"name": "benzene", "SMILES": "c1ccccc1"},
                              "formula": "C6H6"}}
    brief = CodegenEngine._material_brief(plan, None)
    assert brief["name"] == "benzene" and brief["phase"] is None
    assert CodegenEngine._material_desc(brief) == "benzene (C6H6)"


def test_crystal_polymorph_reaches_codegen_prompt():
    """End-to-end: an anatase plan must put 'anatase' + its space group into the
    codegen prompt, so the model builds anatase rather than defaulting to rutile."""
    from codegen_engine import CodegenEngine
    seen = {}

    def agent(prompt):
        seen["prompt"] = prompt
        return PYSCF_SCRIPT

    plan = _plan(target_system={
        "crystal": {"name": "anatase titanium dioxide", "phase": "anatase",
                    "formula": "TiO2", "crystal_system": "tetragonal",
                    "space_group": "I41/amd", "space_group_number": 141},
        "formula": "TiO2"})
    CodegenEngine().generate(plan, agent=agent)
    assert "anatase" in seen["prompt"].lower()
    assert "I41/amd" in seen["prompt"]


# ── a run that will EXECUTE must not ship the placeholder scaffold ────────────

# Compiles and references the library, but defines a function it never calls:
# the fingerprint of a reply cut off at the token cap. Must name the plan's own
# library (pyscf) or it is rejected earlier, for the wrong reason.
PSI4_SCRIPT = """import psi4

def main():
    psi4.set_memory('500 MB')
    mol = psi4.geometry('O=C=O')
    print(psi4.energy('scf/cc-pvdz'))

if __name__ == '__main__':
    main()
"""


TRUNCATED_REPLY = (
    "from pyscf import gto, scf\n\n"
    "def compute_gap(mol):\n"
    "    mf = scf.RHF(mol).run()\n"
    "    return mf.mo_energy\n"
)


class TestRequireSynthesis:
    """Slurm job 2569967 "completed successfully" in 5 seconds having computed
    nothing: synthesis failed, BUILD silently substituted the generic scaffold,
    and the scaffold loads the tool, writes a stub and exits 0 -- so the payload,
    the scheduler and TWAIN all reported success.
    """

    def test_planning_only_still_falls_back(self):
        """The bundle is a deliverable to read there, so the scaffold is fine."""
        bundle = CodegenEngine().generate(
            _plan(), agent=lambda p: TRUNCATED_REPLY, require_synthesis=False)
        assert bundle.template_name == _GENERIC.filename

    def test_a_run_that_will_execute_refuses_the_scaffold(self):
        with pytest.raises(SynthesisFailed) as exc:
            CodegenEngine().generate(
                _plan(), agent=lambda p: TRUNCATED_REPLY, require_synthesis=True)
        assert "computed nothing" in str(exc.value)

    def test_the_failure_names_the_truncation(self):
        """no_entrypoint is the fingerprint of hitting the token cap, and saying
        so is the difference between a fixable report and a guess."""
        with pytest.raises(SynthesisFailed) as exc:
            CodegenEngine().generate(
                _plan(), agent=lambda p: TRUNCATED_REPLY, require_synthesis=True)
        assert "no_entrypoint" in str(exc.value)
        assert "truncated" in str(exc.value)

    def test_a_gateway_error_is_named_not_swallowed(self):
        def broken(_prompt):
            raise TimeoutError("gateway did not respond")

        with pytest.raises(SynthesisFailed) as exc:
            CodegenEngine().generate(_plan(), agent=broken, require_synthesis=True)
        assert "agent_error: TimeoutError" in str(exc.value)
        assert "gateway did not respond" in str(exc.value)

    def test_it_retries_before_giving_up(self):
        """One draw is flaky; the second often works. REPAIR already does this."""
        calls = []

        def flaky(prompt):
            calls.append(prompt)
            return TRUNCATED_REPLY if len(calls) == 1 else PYSCF_SCRIPT

        bundle = CodegenEngine().generate(
            _plan(), agent=flaky, require_synthesis=True)
        assert len(calls) == 2
        assert bundle.template_name == "llm_synthesized"

    def test_every_attempt_is_recorded_on_failure(self):
        engine = CodegenEngine()
        with pytest.raises(SynthesisFailed):
            engine.generate(_plan(), agent=lambda p: TRUNCATED_REPLY,
                            require_synthesis=True)
        report = engine.last_synthesis
        assert report["ok"] is False
        assert len(report["attempts"]) == CodegenEngine.SYNTHESIS_ATTEMPTS
        assert all("no_entrypoint" in a["reason"] for a in report["attempts"])
        assert all(a["reply_chars"] > 0 for a in report["attempts"])

    def test_a_success_records_which_attempt_worked(self):
        engine = CodegenEngine()
        engine.generate(_plan(), agent=lambda p: PYSCF_SCRIPT, require_synthesis=True)
        assert engine.last_synthesis == {
            "ok": True, "attempts": [], "attempt": 1,
            "reply_chars": len(PYSCF_SCRIPT),
        }


class TestBuildRefusesAScaffoldedExecution:
    """BUILD is where the decision has consequences: a scaffolded bundle that
    will be submitted to Slurm burns the allocation and reports success.

    The state machine reaches codegen through the ``code_gen`` package alias, so
    it raises THAT module's SynthesisFailed -- a different class object from the
    bare ``codegen_engine`` import above, for the same file. Catching the wrong
    one silently misses the exception, so these tests use the aliased class.
    """

    @staticmethod
    def _error_class():
        from code_gen.codegen_engine import SynthesisFailed as Aliased
        return Aliased

    def _machine(self, tmp_path, reply, **flags):
        import statemachine as SM
        from crash_recovery import DataStorage
        with patch.object(DataStorage, "load", return_value=None):
            m = SM.StateMachine(data_path=str(tmp_path / "s.json"), run_id="build",
                                agent=lambda p: reply, **flags)
        m.artifacts_dir = tmp_path
        plan = _plan()
        path = tmp_path / "execution_plan.json"
        path.write_text(json.dumps(plan))
        m.context.artifacts["execution_plan"] = str(path)
        return m

    def test_a_slurm_run_fails_build_instead_of_shipping_a_stub(self, tmp_path):
        m = self._machine(tmp_path, TRUNCATED_REPLY, execute_slurm=True)
        with pytest.raises(self._error_class()):
            m.build()
        assert "run_bundle" not in m.context.artifacts   # nothing to submit

    def test_a_local_execution_run_also_refuses(self, tmp_path):
        m = self._machine(tmp_path, TRUNCATED_REPLY, execute_locally=True)
        with pytest.raises(self._error_class()):
            m.build()

    def test_a_planning_only_run_still_gets_its_bundle(self, tmp_path):
        """Nothing will execute, so the scaffold remains a useful deliverable."""
        m = self._machine(tmp_path, TRUNCATED_REPLY)
        m.build()
        assert Path(m.context.artifacts["run_bundle"]).is_dir()

    def test_the_reasons_are_written_to_an_artifact(self, tmp_path):
        """The gap that left a scaffolded RIS run unexplained."""
        m = self._machine(tmp_path, TRUNCATED_REPLY, execute_slurm=True)
        with pytest.raises(self._error_class()):
            m.build()
        report = json.loads(Path(m.context.artifacts["codegen_report"]).read_text())
        assert report["ok"] is False
        assert all("no_entrypoint" in a["reason"] for a in report["attempts"])

    def test_a_successful_build_records_the_report_too(self, tmp_path):
        m = self._machine(tmp_path, PYSCF_SCRIPT, execute_slurm=True)
        m.build()
        report = json.loads(Path(m.context.artifacts["codegen_report"]).read_text())
        assert report["ok"] is True and report["attempt"] == 1


class TestNullRequestedPropertyStillGetsRealCode:
    """Regression for Slurm job 2571447, the run that scaffolded a *second* time
    after the first guard went in.

    Its plan named an acceptance metric but carried requested_property=None, and
    the library-only branch gated synthesis on requested_property alone. So no
    synthesis was attempted at all, _generate_standard rendered the placeholder,
    and the guard -- which lived inside the synthesis path -- was never reached.
    """

    def _psi4_plan(self):
        return {
            "selected_method": {"tool_name": "Psi4", "libraries": ["Psi4"],
                                "calculator": None, "calculator_import": None,
                                "calculator_library": "Psi4"},
            "requested_property": None,          # the null that caused this
            "metadata": {"timestamp": "t", "goal_id": "g1", "candidate_rank": 1},
            "acceptance_metrics": [{
                "metric_name": "standard_heat_of_formation_kJ_per_mol",
                "target_value": -393.5, "tolerance": 5.0}],
            "compute_estimate": {"cpu_hours": 1.0},
            "cost_estimate": {"min_cost": 0.1},
            "slurm_request": {"cpu_count": 2, "gpu_count": 0, "max_time": 1.0, "ram": 8},
            "safety_notes": [],
            "target_system": {"molecule": {"name": "carbon dioxide", "SMILES": "O=C=O"}},
        }

    def test_synthesis_is_attempted_from_the_metric_name(self):
        """The metric names what the run is for, so it is enough to ask for code."""
        prompts = []
        CodegenEngine().generate(
            self._psi4_plan(), agent=lambda p: (prompts.append(p), PSI4_SCRIPT)[1])
        assert prompts, "no synthesis was attempted"
        assert "standard_heat_of_formation_kJ_per_mol" in prompts[0]

    def test_a_synthesized_script_is_used(self):
        bundle = CodegenEngine().generate(
            self._psi4_plan(), agent=lambda p: PSI4_SCRIPT)
        assert bundle.template_name == "llm_synthesized"

    def test_an_executing_run_refuses_the_scaffold_on_this_path_too(self):
        """The guard has to sit where the scaffold is CHOSEN, not only on the
        synthesis route -- this plan reaches it by a different road."""
        with pytest.raises(SynthesisFailed):
            CodegenEngine().generate(
                self._psi4_plan(), agent=lambda p: "print('nope')\n",
                require_synthesis=True)

    def test_it_refuses_even_with_no_agent_at_all(self):
        with pytest.raises(SynthesisFailed):
            CodegenEngine().generate(self._psi4_plan(), require_synthesis=True)

    def test_planning_only_still_gets_the_scaffold(self):
        bundle = CodegenEngine().generate(self._psi4_plan())
        assert bundle.template_name == _GENERIC.filename


# ── #224: a template is used only when it computes what was asked ────────────────

ESOL_SCRIPT = '''\
import argparse, csv, json


def compute():
    from rdkit import Chem
    from rdkit.Chem import Crippen, Descriptors, Lipinski
    mol = Chem.MolFromSmiles("CC(=O)Oc1ccccc1C(=O)O")
    clogp = Crippen.MolLogP(mol)
    mw = Descriptors.MolWt(mol)
    rb = Lipinski.NumRotatableBonds(mol)
    ap = sum(a.GetIsAromatic() for a in mol.GetAtoms()) / mol.GetNumHeavyAtoms()
    return 0.16 - 0.63 * clogp - 0.0062 * mw + 0.066 * rb - 0.74 * ap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="results.csv")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    logs = compute()
    print(json.dumps({"aqueous_solubility_at_25C": logs, "tool": "RDKit",
                      "output_file": args.output}))
    with open(args.output, "w", newline="") as f:
        csv.writer(f).writerows([["aqueous_solubility_at_25C"], [logs]])


if __name__ == "__main__":
    main()
'''

ASPIRIN = dict(
    selected_method={"tool_name": "RDKit", "libraries": ["RDKit"], "calculator": None,
                     "calculator_import": None, "calculator_library": None},
    requested_property=None,
    acceptance_metrics=[{"metric_name": "aqueous_solubility_at_25C", "target_value": None,
                         "tolerance": None}],
    target_system={"kind": "molecule", "formula": "C9H8O4",
                   "molecule": {"name": "aspirin", "SMILES": "CC(=O)Oc1ccccc1C(=O)O"}})


def test_aspirin_solubility_with_rdkit_is_synthesized_not_the_descriptor_template():
    bundle = CodegenEngine().generate(_plan(**ASPIRIN), agent=lambda prompt: ESOL_SCRIPT)
    assert bundle.template_name == "llm_synthesized"
    assert "molecules.csv" not in bundle.main_py


def test_offline_it_still_renders_the_template():
    bundle = CodegenEngine().generate(_plan(**ASPIRIN), agent=None)
    assert bundle.template_name == "template_property_prediction.py"


@pytest.mark.parametrize("wanted, fits", [
    ("logP", True), ("tpsa", True), ("molecular_weight", True),
    ("aqueous_solubility_at_25C", False), ("band_gap", False), (None, False)])
def test_what_the_descriptor_template_computes(wanted, fits):
    from codegen_engine import _RDKIT_PROPERTY
    assert _RDKIT_PROPERTY.computes(wanted) is fits


def test_a_template_needing_a_molecule_list_is_synthesized_even_for_logp():
    # It computes logP, but from a molecules.csv the bundle never carries.
    bundle = CodegenEngine().generate(
        _plan(**{**ASPIRIN, "acceptance_metrics": [{"metric_name": "logP"}]}),
        agent=lambda prompt: ESOL_SCRIPT.replace("aqueous_solubility_at_25C", "logP"))
    assert bundle.template_name == "llm_synthesized"


def test_a_structure_template_that_computes_the_property_is_kept():
    from codegen_engine import _PYMATGEN
    assert _PYMATGEN.computes("density") and not _PYMATGEN.needs_input_data
