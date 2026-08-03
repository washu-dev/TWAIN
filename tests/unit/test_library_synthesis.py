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


def test_library_run_without_property_stays_generic():
    # No requested_property -> nothing to compute -> template path, no synthesis.
    bundle = CodegenEngine().generate(_plan(requested_property=None),
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
