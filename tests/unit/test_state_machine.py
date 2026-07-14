"""Unit tests for the agent-mesh control-plane state machine.

Covers: state enum completeness, guard coverage, valid transitions,
invalid transitions, guard rejection, crash recovery round-trip,
liveness (no deadlocks), and known bugs in the current implementation.

Run from the repo root with:  pixi run pytest tests/unit/test_state_machine.py
"""
import copy
import json
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_DIR = Path(__file__).resolve().parents[2] / "modules" / "16_agent_mesh_control_plane"
sys.path.insert(0, str(MODULE_DIR))

from states import State, Context, InvalidTransition, GuardsBroken
from crash_recovery import DataStorage

# statemachine imports cleanly via the conftest package aliases + the MODULE_DIR
# path insert above (this is exactly how tests/unit/test_orchestrator.py imports
# it). Do NOT wrap this import in ``patch.dict(sys.modules, ...)``: on exit
# patch.dict purges every module imported *inside* the block -- including stdlib
# modules like ``urllib.error`` first imported transitively here -- which corrupts
# global import state for later test files (e.g. duplicate exception classes make
# ``except HTTPError`` miss in test_codegen). See the audit for the mechanism.
import statemachine as SM

GUARDS = SM.GUARDS
StateMachine = SM.StateMachine


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_machine(tmp_path, **ctx_overrides) -> StateMachine:
    """Build a StateMachine whose recovery file lives in tmp_path."""
    data_path = str(tmp_path / "state.json")
    with patch.object(DataStorage, "load", return_value=None):
        m = StateMachine(data_path=data_path)
    m.context = Context(**ctx_overrides)
    return m


# A schema-shaped IntentSpec the fake agent emits; high confidence by default so
# clarify() marks the run clarified and advances to DECOMPOSE.
VALID_INTENT = {
    "objective": "Predict the aqueous solubility of aspirin",
    "domain": "materials",
    "system_descriptors": {
        "formula": "C9H8O4",
        "molecule": {"name": "aspirin", "SMILES": "CC(=O)Oc1ccccc1C(=O)O"},
    },
    "acceptance_metrics": [
        {"metric_name": "logS", "target_value": -1.7, "tolerance": 0.5}
    ],
    "metadata": {
        "ambiguity": False,
        "confidence_scores": {
            "objective_confidence": 0.95,
            "domain_confidence": 0.95,
            "name_confidence": 0.95,
            "SMILES_confidence": 0.95,
            "formula_confidence": 0.95,
        },
    },
}


class FakeAgent:
    """Deterministic, offline stand-in for AgentInterface.

    Returns clarifying questions for a clarification prompt and the canned
    IntentSpec JSON for any generate/rewrite prompt, matching the
    ``resp["content"][0]["text"]`` shape the handlers expect.
    """

    def __init__(self, low_confidence: bool = False):
        intent = copy.deepcopy(VALID_INTENT)
        if low_confidence:
            intent["metadata"]["confidence_scores"]["SMILES_confidence"] = 0.4
        self._intent_json = json.dumps(intent)

    def call_agent(self, prompt, **kwargs):
        if "questions" in str(prompt).lower():
            return {"content": [{"text": "1. Which solvent and temperature?"}]}
        return {"content": [{"text": self._intent_json}]}


def _offline_machine(tmp_path, *, agent=None, **ctx_overrides) -> StateMachine:
    """A StateMachine wired with a fake agent + tmp artifacts dir, so the
    interactive intake/clarify handlers run fully offline (still needs
    ``patch('builtins.input', ...)`` around any call that reads input)."""
    data_path = str(tmp_path / "state.json")
    with patch.object(DataStorage, "load", return_value=None):
        m = StateMachine(data_path=data_path, run_id="t", agent=agent or FakeAgent())
    m.artifacts_dir = tmp_path
    if ctx_overrides:
        m.context = Context(**ctx_overrides)
    return m


HAPPY_CONTEXT = dict(
    clarified=True,
    plan_approved=True,
    execution_status=True,
    validation_result="accepted",
)


EXPECTED_STATES = {
    "INTAKE", "CLARIFY", "DECOMPOSE", "DISCOVER", "PLAN", "BUILD", "REPAIR",
    "EXECUTE", "INTERPRET", "VALIDATE", "ACCEPT", "CORRECT", "REPLAN",
    "TERMINATE",
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. State enum
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateEnum:
    def test_all_required_states_present(self):
        actual = {s.name for s in State}
        assert EXPECTED_STATES <= actual

    def test_no_extra_states(self):
        actual = {s.name for s in State}
        assert actual == EXPECTED_STATES


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Guard table completeness
# ═══════════════════════════════════════════════════════════════════════════════

class TestGuardTable:
    EXPECTED_TRANSITIONS = {
        (State.INTAKE, State.CLARIFY),
        (State.CLARIFY, State.DECOMPOSE),
        (State.CLARIFY, State.CLARIFY),  # clarification Q&A self-loop
        (State.DECOMPOSE, State.INTAKE),  # no intent yet -> go back to intake
        (State.DECOMPOSE, State.DISCOVER),
        (State.DISCOVER, State.PLAN),
        (State.PLAN, State.BUILD),
        (State.BUILD, State.REPAIR),
        (State.REPAIR, State.EXECUTE),
        (State.EXECUTE, State.INTERPRET),
        (State.INTERPRET, State.VALIDATE),
        (State.VALIDATE, State.ACCEPT),
        (State.VALIDATE, State.REPLAN),
        (State.VALIDATE, State.CORRECT),
        (State.ACCEPT, State.TERMINATE),
        (State.REPLAN, State.PLAN),
        (State.CORRECT, State.BUILD),
    }

    def test_all_expected_transitions_have_guards(self):
        missing = self.EXPECTED_TRANSITIONS - set(GUARDS.keys())
        assert missing == set(), f"Missing guard entries: {missing}"

    def test_no_unexpected_transitions(self):
        extra = set(GUARDS.keys()) - self.EXPECTED_TRANSITIONS
        assert extra == set(), f"Unexpected guard entries: {extra}"

    def test_correct_to_build_transition_exists(self):
        assert (State.CORRECT, State.BUILD) in GUARDS


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Valid transitions (happy path)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHappyPath:
    def test_full_pipeline_accept(self, tmp_path):
        # run() advances one transition at a time (the orchestrator loops it), so
        # we drive it to a fixed point here. A fake agent + canned input let the
        # interactive intake/clarify stages run offline.
        m = _offline_machine(tmp_path, **HAPPY_CONTEXT)
        with patch.object(m.storage, "commit"), \
                patch("builtins.input", return_value="predict the solubility of aspirin"):
            for _ in range(len(State) + 5):
                if m.current_state == State.TERMINATE:
                    break
                m.run()
        assert m.current_state == State.TERMINATE

    def test_handler_returns_match_guards(self, tmp_path):
        handlers_next = {
            "intake": State.CLARIFY,
            "clarify": State.DECOMPOSE,
            "decompose": State.DISCOVER,
            "discover": State.PLAN,
            "plan": State.BUILD,
            "build": State.REPAIR,
            "repair": State.EXECUTE,
            "execute": State.INTERPRET,
            "interpret": State.VALIDATE,
            "validate": State.ACCEPT,
            "accept": State.TERMINATE,
            "correct": State.BUILD,
            "replan": State.PLAN,
        }
        # Called in order: intake() writes the intent_spec that clarify()/
        # decompose() then consume.
        m = _offline_machine(tmp_path)
        with patch("builtins.input", return_value="predict the solubility of aspirin"):
            for name, expected_next in handlers_next.items():
                handler = getattr(m, name)
                assert handler() == expected_next, \
                    f"{name}() should return {expected_next}"


# ═══════════════════════════════════════════════════════════════════════════════
# 3b. BUILD stage produces a runnable RunBundle (Story 5.1)
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildProducesRunBundle:
    """build() must turn the execution_plan into a materialized, self-contained
    RunBundle the execution adapter can run without manual edits."""

    PLAN = {
        "selected_method": {"tool_name": "Pymatgen", "tool_version": 2024.1},
        "compute_estimate": {"cpu_hours": 1.0},
        "slurm_request": {"cpu_count": 8, "gpu_count": 1, "max_time": 24.0, "ram": 16},
        "cost_estimate": {"min_tokens": 100, "min_cost": 1.0},
        "metadata": {"timestamp": "2026-06-15T12:00:00Z", "goal_id": "g1", "candidate_rank": 1},
        "acceptance_metrics": [{"metric_name": "density", "target_value": 7.8, "tolerance": 0.5}],
        "safety_notes": ["Verify SLURM partition limits"],
    }

    def test_build_writes_bundle_and_records_artifacts(self, tmp_path):
        m = _offline_machine(tmp_path)
        m.context.artifacts["execution_plan"] = m._write_artifact("execution_plan", self.PLAN)

        next_state = m.build()

        assert next_state == State.REPAIR
        bundle_dir = Path(m.context.artifacts["run_bundle"])
        assert bundle_dir.is_dir()
        for filename in ("main.py", "config.yaml", "requirements.txt", "inline_tests.py"):
            assert (bundle_dir / filename).is_file(), f"bundle missing {filename}"
        assert m.context.artifacts["script"].endswith("main.py")
        # the generated entrypoint must be syntactically valid Python
        compile((bundle_dir / "main.py").read_text(encoding="utf-8"), "main.py", "exec")

    def test_build_without_plan_is_a_noop_to_repair(self, tmp_path):
        # No execution_plan artifact -> build() must not raise; it advances anyway
        # (through REPAIR, which also no-ops with no bundle).
        m = _offline_machine(tmp_path)
        assert m.build() == State.REPAIR
        assert "run_bundle" not in m.context.artifacts

    def test_repair_without_bundle_is_a_noop_to_execute(self, tmp_path):
        # No run_bundle artifact -> repair() passes straight through to EXECUTE.
        m = _offline_machine(tmp_path)
        assert m.repair() == State.EXECUTE
        assert "repair_report" not in m.context.artifacts


# ═══════════════════════════════════════════════════════════════════════════════
# 3c. EXECUTE stage runs the RunBundle via the adapter (Story 5.2)
# ═══════════════════════════════════════════════════════════════════════════════

class _FakeAdapter:
    """Records execute() calls and returns a canned result."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def execute(self, bundle, **kwargs):
        self.calls.append((bundle, kwargs))
        return self._result


class TestExecuteRunsBundle:
    PLAN = {
        "selected_method": {"tool_name": "Pymatgen", "tool_version": 2024.1},
        "compute_estimate": {"cpu_hours": 1.0},
        "slurm_request": {"cpu_count": 8, "gpu_count": 1, "max_time": 24.0, "ram": 16},
        "cost_estimate": {"min_tokens": 100, "min_cost": 1.0},
        "metadata": {"timestamp": "2026-06-15T12:00:00Z", "goal_id": "g1", "candidate_rank": 1},
        "acceptance_metrics": [{"metric_name": "density", "target_value": 7.8, "tolerance": 0.5}],
        "safety_notes": ["Verify SLURM partition limits"],
        # A real structure the caller resolved upstream (TWAIN never fabricates one),
        # so the material-aware Pymatgen bundle has something legitimate to analyse.
        "target_system": {"structure": {
            "lattice": [[4.0, 0.0, 0.0], [0.0, 4.0, 0.0], [0.0, 0.0, 4.0]],
            "atoms": [
                {"species": "Na", "coordinates": [0.0, 0.0, 0.0]},
                {"species": "Cl", "coordinates": [0.5, 0.5, 0.5]},
            ],
            "coordinateSystem": "fractional",
        }},
    }

    def _machine_with_bundle(self, tmp_path):
        m = _offline_machine(tmp_path)
        bundle_dir = Path(m.artifacts_dir) / f"run_bundle_{m.run_id}"
        bundle_dir.mkdir(parents=True)
        (bundle_dir / "main.py").write_text("print('{}')\n", encoding="utf-8")
        m.context.artifacts["run_bundle"] = str(bundle_dir)
        return m, bundle_dir

    def test_disabled_is_a_noop(self, tmp_path):
        m, _ = self._machine_with_bundle(tmp_path)
        fake = _FakeAdapter(None)  # would explode if used
        m._execution_adapter = fake
        m.execute_locally = False
        assert m.execute() == State.INTERPRET
        assert fake.calls == []                         # adapter never invoked
        assert "execution_result" not in m.context.artifacts

    def test_runs_bundle_and_records_result(self, tmp_path):
        from execution_adapter.execution_result import ExecutionResult, ExecutionStatus

        m, bundle_dir = self._machine_with_bundle(tmp_path)
        result = ExecutionResult(
            status=ExecutionStatus.SUCCESS, exit_code=0, stdout="{}", peak_memory_mb=12.5
        )
        fake = _FakeAdapter(result)
        m._execution_adapter = fake
        m.execute_locally = True

        next_state = m.execute()

        assert next_state == State.INTERPRET
        assert m.context.execution_status is True
        # the adapter was handed the bundle dir + our run options, incl. the
        # session id so the workdir is named exec_<run_id>
        assert fake.calls[0][0] == str(bundle_dir)
        assert fake.calls[0][1]["run_smoke"] is True
        assert fake.calls[0][1]["run_id"] == m.run_id
        # an execution_result artifact was written and is JSON-loadable
        result_path = m.context.artifacts["execution_result"]
        data = json.loads(Path(result_path).read_text(encoding="utf-8"))
        assert data["status"] == "success" and data["peak_memory_mb"] == 12.5

    def test_failed_run_raises_actionable_error(self, tmp_path):
        from execution_adapter.execution_result import ExecutionResult, ExecutionStatus

        m, _ = self._machine_with_bundle(tmp_path)
        m._execution_adapter = _FakeAdapter(
            ExecutionResult(status=ExecutionStatus.DEPENDENCY_ERROR, exit_code=1)
        )
        m.execute_locally = True
        # A failed real execution surfaces an actionable error (with the real
        # reason + next steps) instead of letting the EXECUTE->INTERPRET guard
        # fail downstream as an opaque "incomplete context". execution_status is
        # still recorded False before raising, for provenance/resume.
        with pytest.raises(Exception):
            m.execute()
        assert m.context.execution_status is False

    def test_no_bundle_is_a_noop(self, tmp_path):
        m = _offline_machine(tmp_path)
        m.execute_locally = True
        m._execution_adapter = _FakeAdapter(None)
        assert m.execute() == State.INTERPRET  # no run_bundle artifact -> no-op
        assert "execution_result" not in m.context.artifacts

    def test_real_pymatgen_bundle_executes_end_to_end(self, tmp_path):
        pytest.importorskip("pymatgen")
        m = _offline_machine(tmp_path)
        m.context.artifacts["execution_plan"] = m._write_artifact("execution_plan", self.PLAN)
        m.build()  # writes a real RunBundle for Pymatgen
        m.execute_locally = True

        assert m.execute() == State.INTERPRET
        assert m.context.execution_status is True
        data = json.loads(Path(m.context.artifacts["execution_result"]).read_text(encoding="utf-8"))
        assert data["status"] == "success"
        assert data["peak_memory_mb"] is not None  # metrics captured
        # the working dir is named from the session id (not a random string)
        assert data["artifacts_dir"].endswith(f"exec_{m.run_id}")
        assert (Path(data["artifacts_dir"]) / "results.csv").is_file()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Guard rejection (invalid context)
# ═══════════════════════════════════════════════════════════════════════════════

class TestGuardRejection:
    def test_clarify_loops_when_not_confident(self, tmp_path):
        # Low-confidence intent: clarify() must stay in the CLARIFY Q&A loop
        # (return CLARIFY, leave clarified False) rather than advance to DECOMPOSE.
        m = _offline_machine(tmp_path, agent=FakeAgent(low_confidence=True))
        with patch("builtins.input", return_value="answer"):
            m.intake()                      # seed a low-confidence intent_spec
            next_state = m.clarify()
        assert next_state == State.CLARIFY
        assert m.context.clarified is False

    def test_clarify_force_continues_after_max_rounds(self, tmp_path):
        # Persistently low-confidence intent: clarify() must not self-loop forever.
        # It is bounded to max_clarify_rounds rounds, then force-continues to
        # DECOMPOSE on the best-effort spec (max_clarify_rounds is now honoured).
        m = _offline_machine(tmp_path, agent=FakeAgent(low_confidence=True))
        m.max_clarify_rounds = 2
        with patch("builtins.input", return_value="answer"):
            m.intake()
            assert m.clarify() == State.CLARIFY      # round 1: still below threshold
            assert m.context.clarified is False
            assert m.clarify() == State.DECOMPOSE    # round 2 hits the bound
        assert m.context.clarified is True

    def test_plan_to_build_blocked_without_plan_approved(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=False,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.PLAN
        with patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()

    def test_build_to_repair_blocked_without_plan_approved(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=False,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.BUILD
        with patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()

    def test_repair_to_execute_blocked_without_plan_approved(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=False,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.REPAIR
        with patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()

    def test_execute_to_interpret_blocked_without_execution_status(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=False, validation_result="accepted")
        m.current_state = State.EXECUTE
        with patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()

    def test_validate_to_accept_blocked_when_rejected(self, tmp_path):
        # The guard must block VALIDATE->ACCEPT when the verdict is "rejected".
        # validate() now correctly routes rejected->REPLAN, so force the (invalid)
        # ACCEPT target to exercise the guard-rejection path directly.
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="rejected")
        m.current_state = State.VALIDATE
        with patch.object(m, "validate", return_value=State.ACCEPT), \
                patch.object(m.storage, "commit"):
            with pytest.raises(GuardsBroken):
                m.run()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Invalid transitions (no guard entry)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInvalidTransitions:
    @pytest.mark.parametrize("src,dst", [
        (State.INTAKE, State.EXECUTE),
        (State.PLAN, State.VALIDATE),
        (State.BUILD, State.ACCEPT),
        (State.CLARIFY, State.TERMINATE),
    ])
    def test_undefined_transition_raises(self, src, dst, tmp_path):
        m = _make_machine(tmp_path, **HAPPY_CONTEXT)
        m.current_state = src
        with patch.object(m, src.name.lower(), return_value=dst):
            with patch.object(m.storage, "commit"):
                with pytest.raises(InvalidTransition):
                    m.run()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Liveness – every non-terminal state has at least one outgoing transition
# ═══════════════════════════════════════════════════════════════════════════════

class TestLiveness:
    def test_every_nonterminal_state_has_outgoing_guard(self):
        terminal = {State.TERMINATE}
        sources = {src for (src, _) in GUARDS.keys()}
        nonterminal_states = {s for s in State if s not in terminal}
        missing = nonterminal_states - sources
        assert missing == set(), \
            f"States with no outgoing transition (potential deadlock): {missing}"

    def test_every_handler_returns_a_state(self, tmp_path):
        m = _offline_machine(tmp_path)
        with patch("builtins.input", return_value="predict the solubility of aspirin"):
            for s in State:
                if s == State.TERMINATE:
                    continue
                handler = getattr(m, s.name.lower(), None)
                assert handler is not None, f"No handler for {s.name}"
                result = handler()
                assert isinstance(result, State), \
                    f"{s.name} handler returned {result!r}, not a State"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Crash recovery
# ═══════════════════════════════════════════════════════════════════════════════

class TestCrashRecovery:
    def test_commit_creates_file(self, tmp_path):
        path = str(tmp_path / "state.json")
        ds = DataStorage(path)
        ds.commit(State.PLAN, Context(clarified=True, plan_approved=True))
        assert Path(path).exists()

    def test_commit_produces_valid_json(self, tmp_path):
        path = str(tmp_path / "state.json")
        ds = DataStorage(path)
        ds.commit(State.PLAN, Context(clarified=True, plan_approved=True))
        with open(path) as f:
            data = json.load(f)
        assert isinstance(data, dict)

    def test_commit_can_serialize_context_with_artifacts(self, tmp_path):
        path = str(tmp_path / "state.json")
        ds = DataStorage(path)
        ctx = Context(
            clarified=True,
            plan_approved=True,
            execution_status=True,
            validation_result="accepted",
            artifacts={"intent_spec": "/some/path.json"},
        )
        ds.commit(State.PLAN, ctx)

    def test_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "state.json")
        ds = DataStorage(path)
        ctx = Context(clarified=True, plan_approved=True)
        ds.commit(State.PLAN, ctx)
        state, loaded_ctx = ds.load()
        assert state == State.PLAN
        assert loaded_ctx.clarified is True
        assert loaded_ctx.plan_approved is True

    def test_load_returns_none_when_no_file(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        ds = DataStorage(path)
        assert ds.load() is None

    def test_load_with_manually_written_file(self, tmp_path):
        path = str(tmp_path / "state.json")
        ctx = Context(clarified=True, plan_approved=True)
        with open(path, "w") as f:
            json.dump({"current_state": State.PLAN.name, "context": asdict(ctx)}, f)
        ds = DataStorage(path)
        state, loaded_ctx = ds.load()
        assert state == State.PLAN
        assert loaded_ctx.clarified is True
        assert loaded_ctx.plan_approved is True

    def test_machine_resumes_from_saved_state(self, tmp_path):
        path = str(tmp_path / "state.json")
        ctx = Context(clarified=True, plan_approved=True,
                      execution_status=True, validation_result="accepted")
        with open(path, "w") as f:
            json.dump({"current_state": State.VALIDATE.name, "context": asdict(ctx)}, f)
        m = StateMachine(data_path=path)
        assert m.current_state == State.VALIDATE
        assert m.context.plan_approved is True


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Branching paths from VALIDATE
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidateBranching:
    """validate() routes on context.validation_result to the three targets the
    guard table allows out of VALIDATE (accepted->ACCEPT, rejected->REPLAN,
    needs_review->CORRECT)."""

    def test_validate_to_accept_on_accepted(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="accepted")
        m.current_state = State.VALIDATE
        assert m.validate() == State.ACCEPT

    def test_validate_to_replan_on_rejected(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="rejected")
        m.current_state = State.VALIDATE
        assert m.validate() == State.REPLAN

    def test_validate_needs_review_goes_to_correct(self, tmp_path):
        m = _make_machine(tmp_path, clarified=True, plan_approved=True,
                          execution_status=True, validation_result="needs_review")
        m.current_state = State.VALIDATE
        assert m.validate() == State.CORRECT


# A crystal (periodic-solid) IntentSpec: no molecule/SMILES, described by
# formula + polymorph. This is the "bulk modulus of TiO2" shape.
CRYSTAL_INTENT = {
    "objective": "Compute the bulk modulus of rutile TiO2",
    "domain": "materials",
    "system_descriptors": {
        "kind": "crystal",
        "formula": "TiO2",
        "crystal": {"formula": "TiO2", "name": "titanium dioxide", "phase": "rutile"},
    },
    "acceptance_metrics": [
        {"metric_name": "bulk_modulus", "target_value": 210, "tolerance": 30}
    ],
    "metadata": {
        "ambiguity": False,
        "confidence_scores": {
            "objective_confidence": 0.95,
            "domain_confidence": 0.95,
            "formula_confidence": 0.95,
            "phase_confidence": 0.9,
        },
    },
}


class TestSystemRepresentation:
    """The intent ontology distinguishes discrete molecules (SMILES) from
    periodic solids (formula + polymorph), so a crystal request is never gated
    on -- or asked to clarify -- a SMILES it cannot have."""

    def test_kind_crystal_from_explicit_discriminator(self, tmp_path):
        m = _make_machine(tmp_path)
        assert m._system_kind(CRYSTAL_INTENT) == "crystal"

    def test_kind_crystal_inferred_without_discriminator(self, tmp_path):
        m = _make_machine(tmp_path)
        intent = copy.deepcopy(CRYSTAL_INTENT)
        del intent["system_descriptors"]["kind"]
        assert m._system_kind(intent) == "crystal"

    def test_kind_molecule_default(self, tmp_path):
        m = _make_machine(tmp_path)
        assert m._system_kind(VALID_INTENT) == "molecule"

    def test_crystal_confident_without_smiles_score(self, tmp_path):
        """The core fix: a crystal spec clears the confidence gate on its own
        (formula/phase) scores -- it does not need a SMILES_confidence."""
        m = _make_machine(tmp_path)
        assert m._is_confident(CRYSTAL_INTENT) is True

    def test_crystal_not_gated_on_stray_low_smiles_score(self, tmp_path):
        """A stray low SMILES_confidence on a crystal is irrelevant and must not
        block -- this is what previously forced endless SMILES clarification."""
        m = _make_machine(tmp_path)
        intent = copy.deepcopy(CRYSTAL_INTENT)
        intent["metadata"]["confidence_scores"]["SMILES_confidence"] = 0.1
        assert m._is_confident(intent) is True

    def test_molecule_still_gated_on_smiles_score(self, tmp_path):
        """Molecules must still gate on SMILES_confidence (regression guard)."""
        m = _make_machine(tmp_path)
        intent = copy.deepcopy(VALID_INTENT)
        intent["metadata"]["confidence_scores"]["SMILES_confidence"] = 0.1
        assert m._is_confident(intent) is False

    def test_discovery_input_format_cif_for_crystal(self, tmp_path):
        m = _make_machine(tmp_path)
        assert m._discovery_query(CRYSTAL_INTENT).input_format == "CIF"

    def test_discovery_input_format_smiles_for_molecule(self, tmp_path):
        m = _make_machine(tmp_path)
        assert m._discovery_query(VALID_INTENT).input_format == "SMILES"
