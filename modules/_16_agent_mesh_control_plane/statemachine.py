import json
import uuid
from enum import Enum, auto
from dataclasses import dataclass, field, asdict
from pathlib import Path

import twain_paths

from pygments.lexer import default

from intake.intent_spec import IntentSpec
from result_interpreter.result_package import ResultPackage
from states import State, Context, GuardsBroken, InvalidTransition
from crash_recovery import DataStorage
from AgentInterface import AgentInterface
from intake.intent_spec import IntentSpec
from goal_decomposer.graph_builder import GoalGraph, GraphBuilder
from plan_synthesizer.execution_plan import ExecutionPlan
from result_interpreter.result_package import ResultPackage
from cross_validation.validation_report import ValidationReport
from provenance_memory.event_log import EventLog
from PromptCompiler import PromptGenerator
from SemanticParsing import Prompter
from budget_tracker import Budget_Tracker

GUARDS: dict[tuple[State, State], "Callable[[Context], bool]"] = {
    (State.INTAKE, State.CLARIFY): lambda c: True,
    (State.CLARIFY, State.DECOMPOSE): lambda c: c.clarified,
    (State.CLARIFY, State.CLARIFY) : lambda c: True,
    (State.DECOMPOSE, State.DISCOVER): lambda c: True,
    (State.DISCOVER, State.PLAN): lambda c: True,
    (State.PLAN, State.BUILD): lambda c: c.plan_approved,
    (State.BUILD, State.EXECUTE): lambda c: c.plan_approved,
    (State.EXECUTE, State.INTERPRET): lambda c: c.execution_status,
    (State.INTERPRET, State.VALIDATE): lambda c: True,
    (State.VALIDATE, State.ACCEPT): lambda c: c.validation_result == "accepted",
    (State.VALIDATE, State.REPLAN): lambda c: c.validation_result == "rejected",
    (State.VALIDATE, State.CORRECT): lambda c: c.validation_result == "needs_review",
    (State.ACCEPT, State.TERMINATE): lambda c: True,
    (State.REPLAN, State.PLAN): lambda c: True,
    (State.CORRECT, State.BUILD): lambda c: c.plan_approved,
}


class StateMachine:
    def __init__(self, data_path: str = None, *, agent=None, request=None,
                 ask=None, artifacts_dir=None, run_id: str = None,
                 confidence_threshold: float = 0.8, max_clarify_rounds: int = 3):
        # Collaborators are injected and optional, so the machine is usable
        # offline and under test. ``agent`` is either a callable prompt->text or
        # an AgentInterface-like object (.callAgent). It is NOT constructed
        # eagerly here: AgentInterface() performs a network OAuth call, which
        # would break every construction (tests, resume, demo). Pass
        # agent=AgentInterface() at the call site to enable live NLU.
        # All runtime output goes under the repo-anchored logs/ tree (see
        # twain_paths). Defaults resolve here so the machine writes to the same
        # place no matter the working directory; callers may still override.
        twain_paths.ensure_dirs()
        if data_path is None:
            data_path = str(twain_paths.SESSIONS_DIR / "statemachine.sm.json")
        # Ties artifacts back to the driving run: the orchestrator passes its
        # session id here, so files are named ``<name>_<session_id>.json``.
        # Standalone callers get a fresh uuid so artifacts stay unique per run.
        self.run_id = run_id or uuid.uuid4().hex
        self.agent = AgentInterface()
        self.ask = ask
        self._request = request
        self.confidence_threshold = confidence_threshold
        self.max_clarify_rounds = max_clarify_rounds
        self.approvedPlan = False
        self.context = Context()
        self.current_state = State.INTAKE
        self.storage = DataStorage(data_path)
        self.recoveryData = self.storage.load()
        self.promptGenerator = PromptGenerator()

        if not self.recoveryData:
            self.context = Context()
            self.current_state = State.INTAKE
        else:
            self.current_state, self.context = self.recoveryData
        # Where intake/clarify write artifacts (intent_spec.json); defaults next
        # to the recovery file so a run started anywhere persists predictably.
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else twain_paths.ARTIFACTS_DIR

    def run(self):
        handler = getattr(self, self.current_state.name.lower())
        next_state = handler()

        key = (self.current_state, next_state)
        if(key not in GUARDS):
            raise InvalidTransition("Invalid transition, inter node travel must be explicitly defined in GUARDS")
        func = GUARDS[key]
        permission = func(self.context)

        if(not permission):
            raise GuardsBroken("Transition not allowed, incomplete context")
        else:
            self.newState(next_state)





    def newState(self, next_state: State):
        self.current_state = next_state
        self.storage.commit(self.current_state,self.context)

    # ---- intake / clarify collaborators ----------------------------------


    def _write_artifact(self, name: str, data: dict) -> str:
        # Artifacts are namespaced by ``run_id`` (the driving session id), so
        # each run's files are uniquely named and traceable back to its session.
        # The returned path is what callers store in ``context.artifacts``.
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / f"{name}_{self.run_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        return str(path)

    def intake(self) -> State:
        INTAKE_SCHEMA = str(twain_paths.SCHEMAS_DIR / "intent_spec.schema.json")
        query = input("What is your prompt:")
        prompt = self.promptGenerator.jsonSchemaPrompt(INTAKE_SCHEMA, query)
        resp = self.agent.callAgent(prompt)
        print(resp)
        self.context.artifacts["intent_spec"] = self._write_artifact(
            "intent_spec", json.loads(resp["content"][0]["text"]))
        return State.CLARIFY

    def clarify(self) -> State:
        """Raise IntentSpec confidence to the threshold, then mark clarified.

        Only sets ``context.clarified`` when the intent actually meets the bar,
        so an unmet threshold (or a missing spec) correctly leaves the
        CLARIFY->DECOMPOSE guard blocking rather than waving the run through.
        """
        path = self.context.artifacts.get("intent_spec")
        with(open(path, "r")) as f:
            text = f.read();
        questions_from_agent = self.agent.callAgent(self.promptGenerator.clarificationPrompt(text))
        print("\n \n \n")
        user_response = input(f'Answer the following questions about your prompt \n {questions_from_agent["content"][0]["text"]}')
        clarified_json = self.agent.callAgent(self.promptGenerator.modifyJsonSchema(text, user_response))
        self.context.artifacts["intent_spec"] = self._write_artifact(
            "intent_spec", json.loads(clarified_json["content"][0]["text"]))

        # if ref and Path(ref).is_file():
        #     #from intake.clarification import ClarificationDialogue
        #     with open(ref, encoding="utf-8") as f:
        #         spec = IntentSpec(**json.load(f))
        #     #dialogue = ClarificationDialogue(
        #         self._llm if self.agent is not None else None,
        #         self.confidence_threshold, self.max_clarify_rounds)
        #     #if not dialogue.is_sufficient(spec):
        #         spec = dialogue.clarify(spec, self.ask)
        #         self.context.artifacts["intent_spec"] = self._write_artifact(
        #             "intent_spec", asdict(spec))
        #     if dialogue.is_sufficient(spec):
        #         self.context.clarified = True
        print("TEST")
        with(open(path, "r")) as f:
            conf_scores = json.loads(f.read())["metadata"]["confidence_scores"]
        self.context.clarified = True
        for key, value in conf_scores.items():
            if value < self.confidence_threshold:
                self.context.clarified = False
        if(self.context.clarified): return State.DECOMPOSE
        return State.CLARIFY
    def decompose(self) -> State:
        return State.DISCOVER
    def discover(self) -> State:
        return State.PLAN
    def plan(self) -> State:
        return State.BUILD
    def build(self) -> State:
        return State.EXECUTE
    def execute(self) -> State:
        return State.INTERPRET
    def interpret(self) -> State:
        return State.VALIDATE
    def validate(self) -> State:
        return State.ACCEPT
    def accept(self) -> State:
        return State.TERMINATE
    def correct(self) -> State:
        return State.BUILD
    def replan(self) -> State:
        return State.PLAN