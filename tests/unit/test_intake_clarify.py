"""Tests for the INTAKE and CLARIFY implementations (Modules 01 & 02).

Covers the two logic modules (``intake.nlu`` / ``intake.clarification``) and the
``StateMachine`` handlers that wire them up. A fake ``agent`` callable stands in
for the LLM so everything is deterministic and offline.

Run from the repo root with:  pixi run pytest tests/unit/test_intake_clarify.py
"""
import json
import sys
from pathlib import Path

import pytest

# statemachine.py imports its siblings by bare name, so its dir must be on path.
CP_DIR = Path(__file__).resolve().parents[2] / "modules" / "_16_agent_mesh_control_plane"
sys.path.insert(0, str(CP_DIR))

from states import State  # noqa: E402
from statemachine import StateMachine  # noqa: E402
from intake.intent_spec import IntentSpec, Domain  # noqa: E402
from intake.nlu import IntakeNLU, strip_code_fences  # noqa: E402
from intake.clarification import ClarificationDialogue  # noqa: E402


# ── fixtures / helpers ───────────────────────────────────────────────────────

def spec_dict(smiles_conf=0.9):
    """A valid IntentSpec payload; lower smiles_conf to make it 'uncertain'."""
    return {
        "objective": "Predict the aqueous solubility of aspirin at 25C",
        "domain": "materials",
        "system_descriptors": {
            "formula": "C9H8O4",
            "molecule": {"name": "aspirin", "SMILES": "CC(=O)Oc1ccccc1C(=O)O"},
        },
        "acceptance_criteria": [
            {"metric_name": "logS", "target_value": -1.7, "tolerance": 0.5}
        ],
        "metadata": {
            "confidence_scores": {"objective": 0.95, "domain": 0.9, "SMILES": smiles_conf},
            "ambiguity": smiles_conf < 0.8,
        },
    }


def spec_json(smiles_conf=0.9):
    return json.dumps(spec_dict(smiles_conf))


class FakeAgent:
    """A callable LLM stand-in: distinct replies for intake vs refine prompts."""

    def __init__(self, intake_reply, refine_reply=None):
        self.intake_reply = intake_reply
        self.refine_reply = intake_reply if refine_reply is None else refine_reply
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if "Refine this IntentSpec" in prompt:
            return self.refine_reply
        return self.intake_reply


# ═══════════════════════════════════════════════════════════════════════════════
# 1. IntakeNLU
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntakeNLU:
    def test_parse_returns_valid_intentspec(self):
        spec = IntakeNLU(FakeAgent(spec_json())).parse("Predict aspirin solubility")
        assert isinstance(spec, IntentSpec)
        assert spec.domain == Domain.MATERIALS
        assert spec.system_descriptors.molecule.name == "aspirin"
        assert spec.metadata.confidence_scores["SMILES"] == 0.9

    def test_parse_strips_code_fences(self):
        fenced = "```json\n" + spec_json() + "\n```"
        spec = IntakeNLU(FakeAgent(fenced)).parse("anything")
        assert spec.objective.startswith("Predict")

    def test_parse_rejects_non_json(self):
        with pytest.raises(ValueError):
            IntakeNLU(FakeAgent("Sorry, I can't help with that.")).parse("x")

    def test_parse_drops_unknown_keys(self):
        payload = spec_dict()
        payload["unexpected_field"] = "ignore me"
        spec = IntakeNLU(FakeAgent(json.dumps(payload))).parse("x")
        assert isinstance(spec, IntentSpec)

    def test_strip_fences_passthrough(self):
        assert strip_code_fences('{"a": 1}') == '{"a": 1}'


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ClarificationDialogue
# ═══════════════════════════════════════════════════════════════════════════════

class TestClarificationDialogue:
    def test_is_sufficient_threshold(self):
        d = ClarificationDialogue(threshold=0.8)
        assert d.is_sufficient(IntentSpec(**spec_dict(0.9))) is True
        assert d.is_sufficient(IntentSpec(**spec_dict(0.2))) is False

    def test_uncertain_fields(self):
        d = ClarificationDialogue(threshold=0.8)
        assert d.uncertain_fields(IntentSpec(**spec_dict(0.2))) == ["SMILES"]

    def test_clarify_refines_until_sufficient(self):
        agent = FakeAgent(intake_reply=spec_json(0.2), refine_reply=spec_json(0.9))
        d = ClarificationDialogue(agent, threshold=0.8, max_rounds=3)
        spec = IntentSpec(**spec_dict(0.2))
        answers = []
        refined = d.clarify(spec, ask=lambda q: (answers.append(q), "it is aspirin")[1])
        assert d.is_sufficient(refined) is True
        assert answers  # a question was actually asked

    def test_clarify_is_noop_without_channel(self):
        spec = IntentSpec(**spec_dict(0.2))
        # No ask channel -> nothing to clarify through; spec returned unchanged.
        refined = ClarificationDialogue(FakeAgent(spec_json(0.9))).clarify(spec, ask=None)
        assert refined is spec


# ═══════════════════════════════════════════════════════════════════════════════
# 3. StateMachine.intake()
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntakeHandler:
    def test_produces_intent_spec_artifact(self, tmp_path):
        sm = StateMachine(agent=FakeAgent(spec_json()),
                          request="Predict aspirin solubility",
                          artifacts_dir=str(tmp_path))
        assert sm.intake() == State.CLARIFY
        path = sm.context.artifacts["intent_spec"]
        assert Path(path).is_file()
        # Round-trips back into a valid IntentSpec.
        spec = IntentSpec(**json.loads(Path(path).read_text()))
        assert spec.objective.startswith("Predict")

    def test_inert_without_agent(self, tmp_path):
        sm = StateMachine(request="Predict aspirin solubility", artifacts_dir=str(tmp_path))
        assert sm.intake() == State.CLARIFY
        assert "intent_spec" not in sm.context.artifacts

    def test_inert_without_request(self, tmp_path):
        sm = StateMachine(agent=FakeAgent(spec_json()), artifacts_dir=str(tmp_path))
        assert sm.intake() == State.CLARIFY
        assert "intent_spec" not in sm.context.artifacts

    def test_reads_request_from_context_artifacts(self, tmp_path):
        sm = StateMachine(agent=FakeAgent(spec_json()), artifacts_dir=str(tmp_path))
        sm.context.artifacts["request"] = "Predict aspirin solubility"
        sm.intake()
        assert "intent_spec" in sm.context.artifacts


# ═══════════════════════════════════════════════════════════════════════════════
# 4. StateMachine.clarify()
# ═══════════════════════════════════════════════════════════════════════════════

class TestClarifyHandler:
    def test_sets_clarified_when_confident(self, tmp_path):
        sm = StateMachine(agent=FakeAgent(spec_json(0.9)),
                          request="Predict aspirin solubility",
                          artifacts_dir=str(tmp_path))
        sm.intake()
        assert sm.context.clarified is False
        assert sm.clarify() == State.DECOMPOSE
        assert sm.context.clarified is True

    def test_refines_low_confidence_then_clarifies(self, tmp_path):
        sm = StateMachine(agent=FakeAgent(intake_reply=spec_json(0.2),
                                          refine_reply=spec_json(0.9)),
                          request="Predict aspirin solubility",
                          ask=lambda q: "it is aspirin",
                          artifacts_dir=str(tmp_path))
        sm.intake()
        assert sm.clarify() == State.DECOMPOSE
        assert sm.context.clarified is True

    def test_low_confidence_without_channel_stays_blocked(self, tmp_path):
        # Low confidence + no ask channel -> cannot clear the bar -> guard stays
        # closed (clarified remains False), so the orchestrator/HITL takes over.
        sm = StateMachine(agent=FakeAgent(spec_json(0.2)),
                          request="Predict aspirin solubility",
                          artifacts_dir=str(tmp_path))
        sm.intake()
        sm.clarify()
        assert sm.context.clarified is False

    def test_inert_without_spec_leaves_flag(self, tmp_path):
        sm = StateMachine(artifacts_dir=str(tmp_path))
        assert sm.context.clarified is False
        assert sm.clarify() == State.DECOMPOSE
        assert sm.context.clarified is False

    def test_does_not_unset_existing_clarified(self, tmp_path):
        # Regression: a pre-seeded clarified flag (e.g. orchestrator happy path)
        # must survive a clarify() call that has no spec to assess.
        sm = StateMachine(artifacts_dir=str(tmp_path))
        sm.context.clarified = True
        sm.clarify()
        assert sm.context.clarified is True
