"""End-to-end tests for the band-gap pipeline fix.

Proves that "what is the band gap of silicon" flows through the state machine
correctly: discovery picks the ASE driver, planning selects the GPAW calculator
and carries the silicon material + band_gap property, the code builder emits a
Si-specific ASE+GPAW script (LLM-synthesized when available, else a bespoke
template), and EXECUTE asks the researcher before running the heavy DFT job.

These are the regression tests for the reported bug: "asked about silicon, it
chose iron."

Run from the repo root with:  pixi run pytest tests/unit/test_bandgap_pipeline.py
"""
import doctest
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = REPO_ROOT / "modules" / "16_agent_mesh_control_plane"
TEMPLATES_DIR = REPO_ROOT / "modules" / "06_code_configuration_builder" / "templates"
sys.path.insert(0, str(MODULE_DIR))

from states import State  # noqa: E402
from crash_recovery import DataStorage  # noqa: E402
import statemachine as SM  # noqa: E402


@pytest.fixture(autouse=True)
def _no_docker():
    """These tests simulate platforms via ``current_platform``; pin Docker OFF so
    native-vs-docker planning is deterministic regardless of whether the test host
    has a reachable Docker daemon (CI Linux runners do). Without this, simulating
    osx-arm64 on a Docker-equipped host would upgrade planning to linux-64 and pick
    GPAW instead of the native DFTB+. Docker routing has its own dedicated tests
    (test_docker_execution.py) that opt back in."""
    with patch.object(SM, "docker_available", return_value=False):
        yield


SILICON_INTENT = {
    "objective": "what is the band gap of silicon",
    "domain": "materials",
    "system_descriptors": {"formula": "Si", "molecule": {"name": "silicon", "SMILES": "[Si]"}},
    "acceptance_metrics": [{"metric_name": "band_gap", "target_value": 1.12, "tolerance": 0.2}],
    "metadata": {"ambiguity": False, "confidence_scores": {"objective_confidence": 0.95}},
}

# A periodic solid, described the way the interpreter describes one: kind
# "crystal" plus a space group. Its formula counts 3 atoms while the C15
# primitive cell holds 6 -- the mismatch that made the old sizing ask for 2.
CAPT2_INTENT = {
    "objective": "compute the bulk modulus of CaPt2 (cubic Laves phase, C15)",
    "domain": "materials",
    "system_descriptors": {
        "kind": "crystal",
        "formula": "CaPt2",
        "crystal": {"formula": "CaPt2", "name": "Calcium diplatinide",
                    "phase": "cubic Laves phase (C15)", "crystal_system": "cubic",
                    "space_group": "Fd-3m", "space_group_number": 227},
    },
    "acceptance_metrics": [{"metric_name": "bulk_modulus",
                            "target_value": None, "tolerance": None}],
    "metadata": {"ambiguity": False, "confidence_scores": {"objective_confidence": 0.95}},
}

ASPIRIN_INTENT = {
    "objective": "predict the aqueous solubility of aspirin",
    "domain": "materials",
    "system_descriptors": {"formula": "C9H8O4", "molecule": {"name": "aspirin", "SMILES": "CC(=O)Oc1ccccc1C(=O)O"}},
    "acceptance_metrics": [{"metric_name": "logS", "target_value": -1.7, "tolerance": 0.5}],
    "metadata": {"ambiguity": False, "confidence_scores": {"objective_confidence": 0.95}},
}

# A valid, self-contained ASE+GPAW script the fake gateway can return.
VALID_LLM_SCRIPT = '''```python
import argparse, json, csv
def build():
    from ase.build import bulk
    return bulk("Si", "diamond", a=5.43)
def run(smoke=False):
    from gpaw import GPAW, PW
    atoms = build()
    return {"band_gap": 0.61, "formula": "Si2"}
def main():
    print(json.dumps({**run(), "tool": "ASE", "calculator": "GPAW"}))
if __name__ == "__main__":
    main()
```'''


def _machine(tmp_path, **kw):
    # Stub the sim-env probe so planning stays hermetic/offline (no sim subprocess);
    # by default nothing is "missing" in sim, so the toolset is left as discovery
    # chose it. Tests that exercise sim-pruning pass their own ``sim_available``.
    kw.setdefault("sim_available", lambda names: set())
    with patch.object(DataStorage, "load", return_value=None):
        m = SM.StateMachine(data_path=str(tmp_path / "state.json"), run_id="t", **kw)
    m.artifacts_dir = tmp_path
    return m


def _seed_intent(machine, tmp_path, intent=SILICON_INTENT):
    path = tmp_path / "intent_spec_seed.json"
    path.write_text(json.dumps(intent))
    machine.context.artifacts["intent_spec"] = str(path)


class _FakeAdapter:
    """Records execute() calls and returns a canned result."""

    def __init__(self, result=None):
        self._result = result
        self.calls = []

    def execute(self, bundle, **kwargs):
        self.calls.append((bundle, kwargs))
        return self._result


# ═══════════════════════════════════════════════════════════════════════════
# Planning: silicon -> ASE + GPAW, material + property carried
# ═══════════════════════════════════════════════════════════════════════════
class TestPlanningSelectsCalculator:
    def test_discovery_ranks_ase_first_for_band_gap(self, tmp_path):
        m = _machine(tmp_path)
        _seed_intent(m, tmp_path)
        m.decompose()
        assert m.discover() == State.PLAN
        discovery = m._load_artifact("discovery")
        assert discovery["candidates"][0]["name"] == "ASE"
        assert "electronic_structure" in discovery["query"]["capability_tags"]

    def test_plan_carries_material_and_property(self, tmp_path):
        # Platform-agnostic: the library is ASE and the material/property are
        # threaded onto the plan, whichever calculator the platform allows.
        m = _machine(tmp_path)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        assert m.plan() == State.BUILD
        plan = m._load_artifact("execution_plan")
        assert plan["selected_method"]["tool_name"] == "ASE"
        assert plan["requested_property"] == "band_gap"
        assert plan["target_system"]["formula"] == "Si"

    def test_calculator_is_platform_appropriate_never_unrunnable(self, tmp_path):
        # On Linux the band-gap calculator is GPAW (first-principles DFT); on a Mac
        # (no GPAW build) it is the best platform-available REAL engine -- DFTB+
        # (approximate DFT) rather than the MatGL ML surrogate -- never GPAW.
        # Discovery finds the runnable tool for the platform, preferring real
        # calculations over predictions; nothing hardcoded.
        from method_discovery.calculator_registry import find_calculator
        for plat, expected in [("linux-64", "GPAW"), ("osx-arm64", "DFTB+")]:
            m = _machine(tmp_path)
            _seed_intent(m, tmp_path)
            m.decompose()
            m.discover()
            with patch.object(SM, "current_platform", return_value=plat):
                m.plan()
            method = m._load_artifact("execution_plan")["selected_method"]
            assert method["calculator"] == expected, f"{plat} -> {method['calculator']}"
            # whatever is picked must actually have a build for the platform
            assert find_calculator(method["calculator"]).available_on(plat)
            if plat == "osx-arm64":
                assert method["calculator"] != "GPAW"  # never picks the unrunnable one

    def test_plan_library_is_whatever_discovery_ranked_first(self, tmp_path):
        # The library must come from discovery's evaluation, never be overridden
        # toward a preferred tool. Whatever discovery ranks #1 is what the plan
        # uses -- this is the guard for "don't hardcode ase+gpaw".
        m = _machine(tmp_path)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        m.plan()
        discovery = m._load_artifact("discovery")
        plan = m._load_artifact("execution_plan")
        assert plan["selected_method"]["tool_name"] == discovery["candidates"][0]["name"]

    def test_calculator_follows_the_chosen_library(self, tmp_path):
        # The attached calculator must be compatible with the library discovery
        # chose (data-driven), not force a particular library.
        from method_discovery.calculator_registry import find_calculator
        m = _machine(tmp_path)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        m.plan()
        plan = m._load_artifact("execution_plan")
        library = plan["selected_method"]["tool_name"]
        calc_name = plan["selected_method"]["calculator"]
        if calc_name is not None:
            assert find_calculator(calc_name).supports_library(library)

    def test_solubility_selects_no_calculator(self, tmp_path):
        # Regression guard: property-prediction queries are unchanged (no calc).
        m = _machine(tmp_path)
        _seed_intent(m, tmp_path, ASPIRIN_INTENT)
        m.decompose()
        m.discover()
        m.plan()
        plan = m._load_artifact("execution_plan")
        assert plan["selected_method"]["calculator"] is None
        assert plan["requested_property"] is None

    def test_multi_library_toolset_when_primary_cannot_drive_calculator(self, tmp_path):
        # If discovery's #1 library can't drive the calculator, discovery keeps
        # that library AND brings in a bridging one -- e.g. Pymatgen + ASE + GPAW.
        # This is the "utilize multiple libraries" / "pick both pymatgen and gpaw"
        # behaviour, still data-driven (compatibility from the registry).
        from method_discovery.registry_loader import RegistryLoader
        from method_discovery.scorers import DiscoveryQuery, rank_candidates

        m = _machine(tmp_path)
        entries = RegistryLoader().entries()
        ranked = rank_candidates(
            entries, DiscoveryQuery(capability_tags=["electronic_structure", "materials"]),
            top_k=None)
        # Force Pymatgen to be the primary, as if discovery had ranked it #1.
        ranked.sort(key=lambda c: 0 if c.entry.name == "Pymatgen" else 1)

        # Use band_structure: every calculator that covers it is ASE-driven, so
        # Pymatgen can't drive it directly -> a bridging library is brought in.
        # (band_gap would instead pair Pymatgen with the Pymatgen-native MatGL.)
        libraries, calc, calc_lib, _blocked = m._select_toolset(
            ranked, "band_structure", "materials", platform="linux-64")
        assert libraries[0] == "Pymatgen"   # discovery's primary is honoured
        assert "ASE" in libraries           # bridging library brought in
        assert calc.name == "GPAW"          # the DFT calculator is attached
        assert calc_lib == "ASE"            # ...driven through ASE, not Pymatgen

    def test_cluster_blocked_engine_is_surfaced_not_silently_substituted(self, tmp_path):
        # Slurm routing, with the best-fit engine (GPAW) vetoed as if no env
        # spec provisioned it: planning must still produce a runnable plan
        # (substitute calculator) AND tell the researcher -- via the
        # ENGINE UNAVAILABLE safety note the approval card keys off -- which
        # engine was passed over and that a GitHub issue can get it
        # provisioned. This is the "let the user decide" contract.
        m = _machine(tmp_path)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        m.execute_slurm = True
        with patch.object(SM, "current_platform", return_value="linux-64"), \
             patch.object(SM, "_cluster_cannot_run",
                          side_effect=lambda lib: lib.lower() == "gpaw"):
            m.plan()
        plan = m._load_artifact("execution_plan")
        assert plan["selected_method"]["calculator"] != "GPAW"  # substitute used
        notes = [n for n in plan["safety_notes"]
                 if n.startswith(SM.ENGINE_UNAVAILABLE_PREFIX)]
        assert notes, plan["safety_notes"]
        assert "GPAW" in notes[0]                       # names the blocked engine
        assert "GitHub issue" in notes[0]               # ...and the way to get it
        assert plan["selected_method"]["calculator"] in notes[0]  # ...and the substitute
        # The generic "not installed" note must not double-report the veto.
        assert not any("not installed" in n and "GPAW" in n
                       for n in plan["safety_notes"])

    def test_cpu_request_scales_with_system_size(self, tmp_path):
        # Suggested Slurm CPUs follow ~1 CPU per atom instead of a flat 8:
        # silicon's formula counts 1 atom, floored to 2 (k-point/domain
        # parallelism needs a partner). Still editable on the approval card.
        m = _machine(tmp_path)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()
        plan = m._load_artifact("execution_plan")
        assert plan["slurm_request"]["cpu_count"] == 2

    def test_cpu_request_scales_with_formula_atoms(self, tmp_path):
        # Aspirin (C9H8O4) counts 21 atoms. The suggestion scales with that but
        # snaps to a width that decomposes cleanly, so 20 rather than 21 -- one
        # core per atom is a heuristic, not a rule (see _suggest_cpu_count).
        m = _machine(tmp_path)
        _seed_intent(m, tmp_path, ASPIRIN_INTENT)
        m.decompose()
        m.discover()
        m.plan()
        plan = m._load_artifact("execution_plan")
        assert plan["slurm_request"]["cpu_count"] == 20
        # ... and the card is told it was a suggestion, and on what basis.
        assert "21-atom" in plan["slurm_rationale"]["cpu_count"]
        assert "suggestion" in plan["slurm_rationale"]["cpu_count"].lower()

    def test_a_periodic_crystal_is_not_sized_from_its_formula(self, tmp_path):
        """The CaPt2 regression, end to end through plan().

        Formula atoms (CaPt2 -> 3) said 2 cores while the C15 cell's 10x10x10
        mesh could spread over 24 ranks; job 2625288 took ~26x longer than the
        same study on 24 (e496cf22). A periodic cell run by a rank-scaling
        calculator now gets the floor, and the card says why.
        """
        m = _machine(tmp_path)
        _seed_intent(m, tmp_path, CAPT2_INTENT)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()
        plan = m._load_artifact("execution_plan")

        parallelism = m._selected_parallelism(plan)
        if parallelism not in ("interpreter", "engine"):
            pytest.skip(f"discovery chose a {parallelism}-parallel calculator here")

        assert plan["slurm_request"]["cpu_count"] == 24
        why = plan["slurm_rationale"]["cpu_count"]
        assert "k-points" in why and "DENSE" in why
        # ...and the quoted cost describes the allocation actually requested,
        # not the default core count synthesize() started from.
        assert plan["compute_estimate"]["cpu_hours"] == pytest.approx(
            24 * plan["slurm_request"]["max_time"], rel=1e-3)

    def test_no_engine_note_when_everything_is_runnable(self, tmp_path):
        # Off-Slurm (or nothing vetoed): the note never appears.
        m = _machine(tmp_path)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()
        plan = m._load_artifact("execution_plan")
        assert not any(n.startswith(SM.ENGINE_UNAVAILABLE_PREFIX)
                       for n in plan["safety_notes"])

    def test_multi_library_requirements_cover_whole_toolset(self):
        from code_gen.codegen_engine import CodegenEngine
        plan = {
            "selected_method": {
                "tool_name": "Pymatgen", "tool_version": 2024.6,
                "libraries": ["Pymatgen", "ASE"], "calculator": "GPAW",
                "calculator_import": "gpaw", "calculator_library": "ASE",
            },
            "compute_estimate": {"cpu_hours": 0.5},
            "slurm_request": {"cpu_count": 8, "gpu_count": 0, "max_time": 0.5, "ram": 16},
            "cost_estimate": {"min_tokens": 100, "min_cost": 0.01},
            "metadata": {"timestamp": "t", "goal_id": "g", "candidate_rank": 1},
            "acceptance_metrics": [{"metric_name": "band_gap", "target_value": 1.1, "tolerance": 0.2}],
            "safety_notes": [],
            "target_system": {"formula": "Si", "molecule": {"name": "silicon", "SMILES": "[Si]"}},
            "requested_property": "band_gap",
        }
        bundle = CodegenEngine().generate(plan)  # no agent -> fallback template
        compile(bundle.main_py, "main.py", "exec")
        reqs = bundle.requirements_txt
        # The conda-only engine is NOT pinned for pip -- listing it turned a
        # recoverable "use the provisioned env" into a hard install failure on
        # RIS ("No matching distribution found for nwchem==7.3.1"). It is named
        # as a comment so the file still records the whole toolset.
        assert "pymatgen==" in reqs and "ase==" in reqs
        assert "gpaw==" not in reqs and "# gpaw: conda-only" in reqs


# ═══════════════════════════════════════════════════════════════════════════
# Discovery is LLM-driven and availability-grounded (no hand-tuned decider)
# ═══════════════════════════════════════════════════════════════════════════
class TestLlmDiscovery:
    def _plan_with_agent(self, tmp_path, agent, platform):
        m = _machine(tmp_path, agent=agent)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value=platform):
            m.plan()
        return m._load_artifact("execution_plan")

    def test_llm_choice_is_used_when_grounded(self, tmp_path):
        # The LLM's reasoned pick (ASE+GPAW) is used on linux, where GPAW builds.
        agent = lambda p: '{"library": "ASE", "calculator": "GPAW", "reasoning": "full DFT, ships PAW data"}'
        plan = self._plan_with_agent(tmp_path, agent, "linux-64")
        method = plan["selected_method"]
        assert method["tool_name"] == "ASE" and method["calculator"] == "GPAW"
        assert any("LLM" in n for n in plan["safety_notes"])   # its reasoning is recorded

    def test_unavailable_llm_pick_is_grounded_out_then_falls_back(self, tmp_path):
        # LLM insists on GPAW on a Mac (no build). Grounding rejects it and the
        # deterministic platform-aware path takes over -> DFTB+, never GPAW.
        agent = lambda p: '{"library": "ASE", "calculator": "GPAW", "reasoning": "x"}'
        plan = self._plan_with_agent(tmp_path, agent, "osx-arm64")
        # Falls back to the deterministic platform-aware pick: the best REAL engine
        # available on a Mac (DFTB+), preferred over the MatGL ML surrogate, and
        # never the unrunnable GPAW.
        assert plan["selected_method"]["calculator"] == "DFTB+"
        assert plan["selected_method"]["calculator"] != "GPAW"

    def test_grounding_repair_lets_llm_pick_an_available_calculator(self, tmp_path):
        # First reply picks the unavailable GPAW; the repair prompt (which states
        # the platform constraint) makes the LLM choose DFTB+, which is used.
        def agent(prompt):
            if "NO build for" in prompt:
                return '{"library": "ASE", "calculator": "DFTB+", "reasoning": "runs on mac"}'
            return '{"library": "ASE", "calculator": "GPAW", "reasoning": "best accuracy"}'
        plan = self._plan_with_agent(tmp_path, agent, "osx-arm64")
        assert plan["selected_method"]["calculator"] == "DFTB+"

    def test_no_agent_uses_deterministic_fallback(self, tmp_path):
        # With no agent wired, planning stays offline/deterministic.
        m = _machine(tmp_path)  # no agent
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()
        assert m._load_artifact("execution_plan")["selected_method"]["calculator"] == "GPAW"

    def test_pyscf_is_a_real_self_contained_band_gap_engine_on_mac(self, tmp_path):
        # PySCF does periodic DFT self-contained -- a REAL band-gap engine the LLM
        # can pick on osx-arm64 (no plane-wave DFT build) with NO separate
        # calculator and NO ML. It's a library-only run -> executes in the default
        # env (where PySCF is installed), not sim.
        agent = lambda p: ('{"library": "PySCF", "supporting_libraries": ["Pymatgen"], '
                           '"calculator": null, "reasoning": "real self-contained periodic DFT"}')
        plan = self._plan_with_agent(tmp_path, agent, "osx-arm64")
        method = plan["selected_method"]
        assert "PySCF" in method["libraries"]
        assert method["calculator"] is None      # library computes it itself: no ML, no external calc

    def test_sim_absent_library_is_pruned_from_calculator_toolset(self, tmp_path):
        # The model bundles PySCF (importable in the default/planning env but with
        # NO sim build) alongside an ASE+MatGL band-gap run. Calculator bundles run
        # in sim, so PySCF would fail the bundle's smoke -- it must be pruned.
        agent = lambda p: ('{"library": "PySCF", "supporting_libraries": ["ASE"], '
                           '"calculator": "MatGL", "reasoning": "x"}')
        m = _machine(tmp_path, agent=agent,
                     sim_available=lambda names: {n for n in names if n == "pyscf"})
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="osx-arm64"):
            m.plan()
        plan = m._load_artifact("execution_plan")
        method = plan["selected_method"]
        assert method["calculator"] == "MatGL"
        assert "PySCF" not in method["libraries"], method["libraries"]
        assert method["libraries"], "toolset must not be emptied"
        assert any("PySCF" in n and SM.SIM_ENV in n for n in plan["safety_notes"])


# ═══════════════════════════════════════════════════════════════════════════
# Build: the LLM writes the script (no per-property preset template)
# ═══════════════════════════════════════════════════════════════════════════
class TestBuildGeneratesSiliconScript:
    def _build(self, tmp_path, agent):
        # Pin the platform to linux-64 so GPAW is the selected calculator and the
        # sample LLM script (which imports gpaw) matches the calculator check.
        m = _machine(tmp_path, agent=agent)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()
        assert m.build() == State.REPAIR
        main_py = (Path(m.context.artifacts["run_bundle"]) / "main.py").read_text()
        return m, main_py

    def test_llm_writes_the_material_specific_script(self, tmp_path):
        # The general path: the LLM's script (for the discovered library +
        # calculator + material) is used verbatim -- no preset template.
        m, main_py = self._build(tmp_path, agent=lambda prompt: VALID_LLM_SCRIPT)
        compile(main_py, "main.py", "exec")
        assert "gpaw" in main_py.lower()
        assert 'bulk("Si"' in main_py                 # the exact script we synthesized
        reqs = (Path(m.context.artifacts["run_bundle"]) / "requirements.txt").read_text()
        # The conda-only engine is NOT pinned for pip -- listing it turned a
        # recoverable "use the provisioned env" into a hard install failure on
        # RIS ("No matching distribution found for nwchem==7.3.1"). It is named
        # as a comment so the file still records the whole toolset.
        assert "ase==" in reqs
        assert "gpaw==" not in reqs and "# gpaw: conda-only" in reqs

    def test_fallback_is_generic_scaffold_not_a_preset(self, tmp_path):
        # No gateway -> a tool-agnostic scaffold, NOT a band-gap preset template.
        m, main_py = self._build(tmp_path, agent=lambda prompt: "not python at all {")
        compile(main_py, "main.py", "exec")
        assert "Generic tool-runner" in main_py        # the generic scaffold
        assert "bandgap" not in main_py.lower().replace("band gap", "")  # no preset physics
        assert '"species": "Fe"' not in main_py        # certainly no hard-coded iron

    def test_bundle_has_exactly_four_files(self, tmp_path):
        m, _ = self._build(tmp_path, agent=lambda prompt: "junk")
        files = sorted(p.name for p in Path(m.context.artifacts["run_bundle"]).iterdir())
        assert files == ["config.yaml", "inline_tests.py", "main.py", "requirements.txt"]


# ═══════════════════════════════════════════════════════════════════════════
# Repair: the REPAIR stage heals a broken script before EXECUTE
# ═══════════════════════════════════════════════════════════════════════════
class TestRepairStageHealsTheScript:
    """BUILD emits a script that *compiles* but can still fail at run time (a
    guessed model id, a misused API). The REPAIR stage smoke-runs it, feeds the
    real error back to the model, and rewrites the bundle's main.py -- so the bug
    is caught before EXECUTE instead of surfacing there (the reported failure)."""

    # Statically valid (compiles, references gpaw, has an entrypoint) so BUILD
    # accepts it -- but it guesses a model id, so it fails at run time.
    BUILT = ('import argparse, json\n'
             'def build():\n'
             '    from ase.build import bulk\n'
             '    return bulk("Si", "diamond")\n'
             'def main():\n'
             '    from gpaw import GPAW\n'
             '    model = "guessed-model-id-that-does-not-exist"\n'
             '    print(json.dumps({"band_gap": None, "note": model}))\n'
             'if __name__ == "__main__":\n'
             '    main()\n')
    # The corrected script the model returns on repair.
    FIXED = ('import argparse, json\n'
             'def build():\n'
             '    from ase.build import bulk\n'
             '    return bulk("Si", "diamond")\n'
             'def main():\n'
             '    from gpaw import GPAW, PW\n'
             '    print(json.dumps({"band_gap": 0.6, "tool": "ASE", "calculator": "GPAW"}))\n'
             'if __name__ == "__main__":\n'
             '    main()\n')

    def _built_bundle(self, tmp_path, script_doctor):
        m = _machine(tmp_path, agent=lambda prompt: self.BUILT, script_doctor=script_doctor)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()
        assert m.build() == State.REPAIR
        # BUILD accepted the (statically valid) script verbatim.
        assert "guessed-model-id" in (Path(m.context.artifacts["run_bundle"]) / "main.py").read_text()
        return m

    def test_repair_rewrites_a_runtime_broken_script(self, tmp_path):
        from code_gen.script_doctor import ScriptDoctor, SmokeOutcome
        outcomes = iter([SmokeOutcome("repairable", "RepositoryNotFoundError: 401 for guessed-model-id"),
                         SmokeOutcome("pass")])
        doctor = ScriptDoctor(agent=lambda p: self.FIXED, brief={"calculator_import": "gpaw"},
                              verifier=lambda s, b: next(outcomes), review=False)
        m = self._built_bundle(tmp_path, script_doctor=doctor)

        assert m.repair() == State.EXECUTE
        healed = (Path(m.context.artifacts["run_bundle"]) / "main.py").read_text()
        assert "guessed-model-id" not in healed                # the bad id is gone
        assert 'GPAW, PW' in healed                            # replaced with the fix
        report = json.loads(Path(m.context.artifacts["repair_report"]).read_text())
        assert report["status"] == "repaired"

    def test_repair_reports_when_it_cannot_fix(self, tmp_path):
        # A runtime failure with no agent to fix it: the doctor records the problem
        # and advances (EXECUTE stays the gate) rather than silently shipping it.
        from code_gen.script_doctor import ScriptDoctor, SmokeOutcome
        doctor = ScriptDoctor(agent=None, brief={"calculator_import": "gpaw"},
                              verifier=lambda s, b: SmokeOutcome("repairable", "TypeError in calc"))
        m = self._built_bundle(tmp_path, script_doctor=doctor)

        assert m.repair() == State.EXECUTE
        report = json.loads(Path(m.context.artifacts["repair_report"]).read_text())
        assert report["status"] == "unrepairable"
        assert any("smoke" in r for r in report["remaining"])


# ═══════════════════════════════════════════════════════════════════════════
# Execute: ask before running the heavy DFT job
# ═══════════════════════════════════════════════════════════════════════════
class TestHeavyCalcGate:
    def _machine_ready_to_execute(self, tmp_path, answer):
        # Pin to linux-64 so a *heavy* calculator (GPAW) is selected -- the gate
        # only applies to heavy calculators.
        m = _machine(tmp_path, agent=lambda prompt: "junk")
        m.ask = lambda message: answer
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()
        m.build()
        m.execute_locally = True
        return m

    def test_declining_defers_and_does_not_run(self, tmp_path):
        m = self._machine_ready_to_execute(tmp_path, answer="n")
        fake = _FakeAdapter()  # would append a call if used
        m._execution_adapter = fake
        assert m.execute() == State.INTERPRET
        assert fake.calls == []  # never ran the heavy job
        result = json.loads(Path(m.context.artifacts["execution_result"]).read_text())
        assert result["status"] == "deferred"
        # the run can still wind down cleanly
        assert m.context.execution_status is True

    def test_ui_button_payloads_parse_correctly(self, tmp_path):
        # The web UI's heavy-gate buttons send exactly "yes" / "no" (ChatScreen
        # handleYesNo). Pin those payloads to the parser so a wording change in
        # _confirm_heavy_execution can't silently break the buttons.
        m = self._machine_ready_to_execute(tmp_path, answer="yes")
        assert m._confirm_heavy_execution() is True
        m = self._machine_ready_to_execute(tmp_path, answer="no")
        assert m._confirm_heavy_execution() is False

    def test_heavy_prompt_carries_the_yn_marker(self, tmp_path):
        # The UI shows Yes/No buttons only when the pending clarification
        # contains "[y/N]" -- the marker the confirmation prompt must keep.
        asked = []
        m = self._machine_ready_to_execute(tmp_path, answer="n")
        m.ask = lambda message: asked.append(message) or "n"
        assert m._confirm_heavy_execution() is False
        assert asked and "[y/N]" in asked[0]

    def test_auto_approve_proceeds_without_prompting(self, tmp_path):
        # Unattended mode: a heavy calculator runs without any confirmation, even
        # with no `ask` bridge and no tty (which would otherwise defer).
        m = _machine(tmp_path, agent=lambda prompt: "junk", auto_approve=True)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()  # linux-64 -> heavy GPAW
        assert m._selected_calculator().heavy          # precondition: the gate applies
        assert m._confirm_heavy_execution() is True     # auto-approved, no prompt

    def test_without_auto_approve_or_prompt_it_defers(self, tmp_path):
        # Contrast: no auto-approve, no way to ask -> the heavy run defers (safe).
        m = _machine(tmp_path, agent=lambda prompt: "junk")
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()
        assert m._confirm_heavy_execution() is False

    def test_missing_calculator_is_a_graceful_skip_not_a_crash(self, tmp_path):
        # User confirms, but the heavy 'sim' env isn't built here and we're not
        # installing deps: deliver the script with run guidance instead of a
        # cryptic guard failure (the reported "GuardsBroken" experience).
        m = self._machine_ready_to_execute(tmp_path, answer="yes")
        # _execution_adapter is None -> the real-adapter path, which pre-checks that
        # the bundle's run environment is available before spending time.
        with patch.object(SM, "pixi_env_python", return_value=None):
            assert m.execute() == State.INTERPRET
        result = json.loads(Path(m.context.artifacts["execution_result"]).read_text())
        assert result["status"] == "skipped_missing_dependency"
        assert result["how_to_run"]                    # actionable guidance present
        assert "sim" in result["how_to_run"]           # points at the right (sim) env
        assert m.context.execution_status is True       # winds down cleanly, no crash

    def test_confirming_runs_the_calculation(self, tmp_path):
        from execution_adapter.execution_result import ExecutionResult, ExecutionStatus

        m = self._machine_ready_to_execute(tmp_path, answer="yes")
        m._execution_adapter = _FakeAdapter(
            ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0, stdout="{}", peak_memory_mb=10.0)
        )
        assert m.execute() == State.INTERPRET
        assert len(m._execution_adapter.calls) == 1  # the heavy job ran
        assert m.context.execution_status is True

    def test_defers_safely_when_no_interactive_input(self, tmp_path):
        # execute_locally with a heavy calc, but no ask injected and no tty: must
        # defer (not hang on input() or crash on EOF) -- review finding #9.
        m = _machine(tmp_path, agent=lambda prompt: "junk")  # self.ask stays None
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()  # linux-64 -> heavy GPAW -> the gate applies
        m.build()
        m.execute_locally = True
        fake = _FakeAdapter()  # would record a call if execution ran
        m._execution_adapter = fake
        with patch.object(SM.sys, "stdin") as stdin_mock:
            stdin_mock.isatty.return_value = False  # headless
            assert m.execute() == State.INTERPRET
        assert fake.calls == []
        result = json.loads(Path(m.context.artifacts["execution_result"]).read_text())
        assert result["status"] == "deferred"

    def test_non_heavy_run_is_not_gated(self, tmp_path):
        # A plain library run (no calculator) must never prompt: wire an ask that
        # fails the test if it is ever called, then confirm EXECUTE completes.
        from execution_adapter.execution_result import ExecutionResult, ExecutionStatus

        def _boom(message):
            raise AssertionError("non-heavy run must not prompt the researcher")

        m = _machine(tmp_path, agent=lambda prompt: "junk")
        m.ask = _boom
        _seed_intent(m, tmp_path, ASPIRIN_INTENT)
        m.decompose()
        m.discover()
        m.plan()
        m.build()
        m.execute_locally = True
        m._execution_adapter = _FakeAdapter(
            ExecutionResult(status=ExecutionStatus.SUCCESS, exit_code=0, stdout="{}", peak_memory_mb=1.0)
        )
        assert m.execute() == State.INTERPRET       # completes; _boom never fired
        assert len(m._execution_adapter.calls) == 1  # adapter ran without a prompt


# ═══════════════════════════════════════════════════════════════════════════
# No per-property preset templates exist -- codegen is general
# ═══════════════════════════════════════════════════════════════════════════
class TestNoPresetTemplates:
    def test_band_gap_preset_template_files_are_gone(self):
        for name in ("template_bandgap_gpaw.py", "template_bandgap_dftbplus.py"):
            assert not (TEMPLATES_DIR / name).exists(), f"preset template {name} still present"

    def test_engine_has_no_electronic_property_routing(self):
        from code_gen import codegen_engine as eng
        assert not hasattr(eng, "_ELECTRONIC_PROPERTIES")
        assert not hasattr(eng, "_CALC_TEMPLATE_SPECS")

    def test_llm_chosen_calculator_script_is_used(self, tmp_path):
        # A prompt-aware agent: discovery picks DFTB+ (available on the Mac),
        # codegen returns a matching DFTB+ script -> used verbatim (no preset).
        dftb_script = (
            "import json\n"
            "def run():\n"
            "    from ase.build import bulk\n"
            "    from ase.calculators.dftb import Dftb\n"
            "    return {'band_gap': 1.1}\n"
            "print(json.dumps(run()))\n"
        )
        def agent(prompt):
            if "method-discovery agent" in prompt:
                return '{"library": "ASE", "calculator": "DFTB+", "reasoning": "runs on this Mac"}'
            return dftb_script
        m = _machine(tmp_path, agent=agent)
        _seed_intent(m, tmp_path)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="osx-arm64"):
            m.plan()
        assert m._load_artifact("execution_plan")["selected_method"]["calculator"] == "DFTB+"
        m.build()
        main_py = (Path(m.context.artifacts["run_bundle"]) / "main.py").read_text()
        compile(main_py, "main.py", "exec")
        assert "ase.calculators.dftb" in main_py       # the LLM's DFTB+ script
        reqs = (Path(m.context.artifacts["run_bundle"]) / "requirements.txt").read_text()
        # The conda-only engine is NOT pinned for pip -- listing it turned a
        # recoverable "use the provisioned env" into a hard install failure on
        # RIS ("No matching distribution found for nwchem==7.3.1"). It is named
        # as a comment so the file still records the whole toolset.
        assert "ase==" in reqs
        assert "dftbplus==" not in reqs and "# dftbplus: conda-only" in reqs


class TestPlanRefusesToPromiseWhatItCannotCompute:
    """End to end through plan(): the NaCl2 shape, and the cases that must pass."""

    NACL2_INTENT = {
        "objective": "Compute the standard heat of formation of NaCl2",
        "domain": "materials",
        "system_descriptors": {"kind": "crystal", "formula": "NaCl2",
                               "crystal": {"formula": "NaCl2",
                                           "name": "Sodium dichloride"}},
        "acceptance_metrics": [{"metric_name": "standard_heat_of_formation",
                                "target_value": None, "tolerance": None}],
        "metadata": {"ambiguity": False,
                     "confidence_scores": {"objective_confidence": 0.95}},
    }

    def _agent(self, library, calculator=None):
        reply = json.dumps({"library": library, "supporting_libraries": [],
                            "calculator": calculator, "reasoning": "best fit"})
        return lambda prompt: reply

    def test_a_driver_only_plan_for_a_calculated_property_is_refused(self, tmp_path):
        """Pymatgen is a DRIVER: calculators plug into it, it computes nothing."""
        m = _machine(tmp_path, agent=self._agent("Pymatgen"))
        _seed_intent(m, tmp_path, self.NACL2_INTENT)
        m.decompose()
        m.discover()
        with pytest.raises(Exception, match="needs a calculator it does not have"):
            with patch.object(SM, "current_platform", return_value="linux-64"):
                m.plan()

    def test_a_self_contained_engine_is_allowed(self, tmp_path):
        """PySCF computes an electronic structure itself -- nothing to attach.

        The false positive that a tags-only rule would produce, since ASE claims
        electronic_structure too.
        """
        m = _machine(tmp_path, agent=self._agent("PySCF"))
        _seed_intent(m, tmp_path, self.NACL2_INTENT)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()          # must not raise
        assert m._load_artifact("execution_plan") is not None

    def test_a_retrieval_is_not_refused(self, tmp_path):
        """RETRIEVING a value needs no engine, and TWAIN can now do that. Refusing
        a Materials Project lookup for having no calculator would refuse the
        correct plan."""
        intent = {**self.NACL2_INTENT,
                  "objective": "Look up the standard heat of formation of NaCl2 "
                               "from the Materials Project database"}
        m = _machine(tmp_path, agent=self._agent("Pymatgen"))
        _seed_intent(m, tmp_path, intent)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()          # must not raise
        assert m._load_artifact("execution_plan") is not None

    def test_a_driver_with_an_engine_attached_is_allowed(self, tmp_path):
        m = _machine(tmp_path, agent=self._agent("ASE", calculator="EMT"))
        _seed_intent(m, tmp_path, self.NACL2_INTENT)
        m.decompose()
        m.discover()
        with patch.object(SM, "current_platform", return_value="linux-64"):
            m.plan()
        plan = m._load_artifact("execution_plan")
        assert plan["selected_method"]["calculator"] == "EMT"


class TestUnrunnableMlipsAreNotPlannedForTheCluster:
    """matgl/chgnet are on PyPI but pull multi-GB torch at job start.

    The cluster veto read "pip can serve it" as "the job can get it", so a plan
    could name a calculator no provisioned env has and nothing could install in
    time -- which is how MACE/CHGNet/M3GNet came to be reached for at all.
    """

    def test_the_veto_now_blocks_them(self):
        assert SM._cluster_cannot_run("matgl") is True
        assert SM._cluster_cannot_run("chgnet") is True

    def test_provisioned_engines_are_still_runnable(self):
        """The veto must not start blocking the engines that ARE provisioned."""
        for engine in ("gpaw", "nwchem", "espresso", "abinit", "cp2k", "dftbplus"):
            assert SM._cluster_cannot_run(engine) is False, engine

    def test_it_reads_the_env_specs_so_provisioning_undoes_it(self):
        """Provision an env for one and it comes off the list by itself."""
        provided = SM._cluster_env_packages()
        assert "matgl" not in provided and "chgnet" not in provided
        assert {"gpaw", "nwchem", "psi4"} <= provided
