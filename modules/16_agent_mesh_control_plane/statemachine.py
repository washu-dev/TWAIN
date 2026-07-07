import json
import uuid
from datetime import datetime, timezone
from enum import Enum, auto
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import twain_paths

from intake.intent_spec import IntentSpec
from result_interpreter.result_package import ResultPackage
from states import State, Context, GuardsBroken, InvalidTransition
from crash_recovery import DataStorage
from AgentInterface import AgentInterface
from intake.intent_spec import IntentSpec
from goal_decomposer.graph_builder import GoalGraph, GraphBuilder
from plan_synthesizer.execution_plan import ExecutionPlan
from plan_synthesizer.plan_synthesizer import PlanSynthesizer
from method_discovery.registry_loader import RegistryLoader
from method_discovery.scorers import DiscoveryQuery, rank_candidates
from method_discovery.ranking_rationale import explain_ranking
from result_interpreter.result_package import ResultPackage
from cross_validation.validation_report import ValidationReport
from provenance_memory.event_log import EventLog
from PromptCompiler import PromptGenerator
from SemanticParsing import Prompter
from budget_tracker import Budget_Tracker
from code_gen.codegen_engine import CodegenEngine

GUARDS: dict[tuple[State, State], "Callable[[Context], bool]"] = {
    (State.INTAKE, State.CLARIFY): lambda c: True,
    (State.CLARIFY, State.DECOMPOSE): lambda c: c.clarified,
    (State.CLARIFY, State.CLARIFY) : lambda c: True,
    (State.DECOMPOSE, State.DISCOVER): lambda c: True,
    (State.DISCOVER, State.PLAN): lambda c: True,
    (State.DECOMPOSE,State.INTAKE): lambda c: True,
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
                 confidence_threshold: float = 0.8, max_clarify_rounds: int = 3,
                 execute_locally: bool = False, execute_install_deps: bool = False,
                 execute_keep_artifacts: bool = True, execute_timeout=None,
                 execution_adapter=None):
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
        # Injected or lazily built on first use. AgentInterface() performs a
        # network OAuth handshake, so building it eagerly here would break every
        # offline construction (tests, resume, and the decompose/discover/plan
        # handlers, which need no agent). See the ``agent`` property below.
        self._agent = agent
        self.ask = ask
        self._request = request
        self.confidence_threshold = confidence_threshold
        self.max_clarify_rounds = max_clarify_rounds
        # Local execution (Story 5.2). Off by default so offline/seeded pipeline
        # runs keep EXECUTE a no-op; the orchestrator (demo/_main) turns it on so
        # real runs actually execute the RunBundle built in BUILD.
        self.execute_locally = execute_locally
        self.execute_install_deps = execute_install_deps
        self.execute_keep_artifacts = execute_keep_artifacts
        self.execute_timeout = execute_timeout
        self._execution_adapter = execution_adapter
        # Rounds of clarification Q&A run so far; bounds the CLARIFY self-loop.
        self._clarify_rounds = 0
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

    @property
    def agent(self):
        """The live NLU agent, constructed on first use.

        Building AgentInterface() performs a network OAuth call, so it is
        deferred until a handler that needs it (intake/clarify) first touches
        ``self.agent``. This keeps the machine importable and constructible
        offline. Pass ``agent=...`` to ``__init__`` to inject a stub/live agent.
        """
        if self._agent is None:
            self._agent = AgentInterface()
        return self._agent

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

    _WELCOME = (
        "Welcome to TWAIN — your autonomous computational-chemistry research "
        "assistant.\nDescribe the problem you'd like to investigate (e.g. "
        "'predict the aqueous solubility of aspirin'): "
    )

    def _ask_user(self, message: str) -> str:
        """Get input from the researcher: ``self.ask`` if injected, else stdin.

        Injecting ``ask`` (a ``message -> answer`` callable) lets the orchestrator
        and tests drive intake/clarify non-interactively; the stdin fallback keeps
        the standalone CLI experience.
        """
        if callable(self.ask):
            return self.ask(message)
        return input(message)

    def _agent_text(self, prompt: str) -> str:
        """Call the agent and return its text, accepting either agent shape.

        ``agent`` may be a plain ``prompt -> str`` callable (what the orchestrator
        and tests inject) or an ``AgentInterface``-style object whose
        ``callAgent`` returns ``{"content": [{"text": ...}]}`` (the live LLM).
        """
        agent = self.agent
        resp = agent.callAgent(prompt) if hasattr(agent, "callAgent") else agent(prompt)
        if isinstance(resp, str):
            return resp
        return resp["content"][0]["text"]

    def _is_confident(self, intent: dict) -> bool:
        """True when every confidence score meets the threshold (and any exist)."""
        scores = (intent.get("metadata") or {}).get("confidence_scores") or {}
        if not scores:
            return False
        return all(value >= self.confidence_threshold for value in scores.values())

    def intake(self) -> State:
        schema = str(twain_paths.SCHEMAS_DIR / "intent_spec.schema.json")
        # A pre-supplied request (orchestrator/UI/test) skips the interactive
        # prompt; otherwise fall back to asking on stdin.
        query = self._request if self._request else self._ask_user(self._WELCOME)
        prompt = self.promptGenerator.jsonSchemaPrompt(schema, query)
        intent = json.loads(self._agent_text(prompt))
        self.context.artifacts["intent_spec"] = self._write_artifact("intent_spec", intent)
        return State.CLARIFY

    def clarify(self) -> State:
        """Raise IntentSpec confidence to the threshold, then mark clarified.

        Fast path: a spec that already clears the bar needs no questions, so
        clarify is a no-op that just sets ``clarified`` and advances. Otherwise it
        runs one agent-generated Q&A round per call, re-checking confidence; the
        orchestrator loops it while it keeps returning CLARIFY. The loop is bounded
        to ``max_clarify_rounds`` rounds -- once exhausted, clarify force-continues
        on the best-effort spec (sets ``clarified``, advances to DECOMPOSE) instead
        of self-looping forever on an intent it can never make confident.
        """
        intent = self._load_artifact("intent_spec")
        if intent is None:
            return State.CLARIFY  # nothing to clarify yet; hold at CLARIFY

        if self._is_confident(intent):
            self.context.clarified = True
            return State.DECOMPOSE

        text = json.dumps(intent)
        questions = self._agent_text(self.promptGenerator.clarificationPrompt(text))
        answer = self._ask_user(
            f"Answer the following questions about your request:\n{questions}\n> "
        )
        text = self._agent_text(self.promptGenerator.modifyJsonSchema(text, answer))
        intent = json.loads(text)
        self.context.artifacts["intent_spec"] = self._write_artifact("intent_spec", intent)
        self._clarify_rounds += 1
        if self._is_confident(intent):
            self.context.clarified = True
            return State.DECOMPOSE

        # Bounded loop: after ``max_clarify_rounds`` rounds we force-continue on
        # the best-effort spec rather than self-looping forever on an intent the
        # agent can't make confident (Story 3.2: "up to N rounds; force-continue").
        if self._clarify_rounds >= self.max_clarify_rounds:
            print(
                f"[clarify] confidence still below {self.confidence_threshold} after "
                f"{self._clarify_rounds} round(s); proceeding with the best-effort IntentSpec."
            )
            self.context.clarified = True
            return State.DECOMPOSE

        self.context.clarified = False
        return State.CLARIFY
    # ---- decompose / discover / plan-synthesis collaborators -------------

    def _load_artifact(self, name: str) -> Optional[dict]:
        """Return a previously written stage artifact's JSON, or None if absent.

        Returning None when the upstream artifact is missing lets these handlers
        be exercised in isolation (e.g. a direct unit call, or a resume that
        skipped intake) by no-opping to the next state instead of raising.
        """
        path = self.context.artifacts.get(name)
        if not path or not Path(path).is_file():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _canonical_goal_graph(self, intent: dict) -> dict:
        """Seed a canonical discover -> execute -> validate DAG from the intent.

        A deterministic decomposition that every property/simulation task shares:
        pick a method, run it, validate the output against the acceptance metrics.
        Swap in an LLM-driven decomposition here later (mirroring intake/clarify)
        without changing the contract -- the result is still a GoalGraph.
        """
        objective = intent.get("objective") or "the requested computation"
        acceptance = [
            f"{m.get('metric_name')} within {m.get('tolerance')} of {m.get('target_value')}"
            for m in intent.get("acceptance_metrics", [])
            if isinstance(m, dict) and m.get("metric_name") is not None
        ]
        goals = [
            {
                "id": "discover_method",
                "category": "discovery",
                "purpose": f"Select a computational method capable of: {objective}",
                "owner_agent": "method_discovery",
            },
            {
                "id": "run_execution",
                "category": "execution",
                "purpose": f"Execute the selected method to address: {objective}",
                "owner_agent": "runtime_orchestrator",
            },
            {
                "id": "validate_results",
                "category": "validation",
                "purpose": "Validate outputs against the acceptance criteria",
                "owner_agent": "cross_validation",
                "acceptance_criteria": acceptance,
            },
        ]
        edges = [
            {"source": "discover_method", "target": "run_execution", "category": "seq"},
            {"source": "run_execution", "target": "validate_results", "category": "seq"},
        ]
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_intent_id": self.run_id,
            "rationale": "Canonical discover -> execute -> validate decomposition.",
        }
        return {"goals": goals, "edges": edges, "metadata": metadata}

    def _discovery_query(self, intent: dict) -> DiscoveryQuery:
        """Derive a discovery query (capability tags + input format) from the intent."""
        objective = (intent.get("objective") or "").lower()
        domain = (intent.get("domain") or "").lower()

        keyword_map = {
            "property_prediction": ("predict", "property", "solub", "toxic", "affinity"),
            "structure_optimization": ("optimi", "geometry", "relax", "structure"),
            "molecular_dynamics": ("dynamics", " md", "trajectory", "simulat"),
            "quantum_chemistry": ("quantum", "electronic", "dft", "ab initio", "orbital"),
        }
        tags = [tag for tag, needles in keyword_map.items()
                if any(n in objective for n in needles)]
        if domain == "materials":
            tags.append("materials")
        if not tags:
            tags.append("property_prediction")
        tags = list(dict.fromkeys(tags))  # de-dupe, keep order

        input_format = None
        molecule = (intent.get("system_descriptors") or {}).get("molecule") or {}
        if molecule.get("SMILES"):
            input_format = "SMILES"
        return DiscoveryQuery(capability_tags=tags, input_format=input_format)

    def _primary_goal_id(self) -> str:
        """Resolve the goal id the plan targets (the execution goal), with fallback."""
        graph = self._load_artifact("goal_graph")
        if graph and graph.get("goals"):
            for goal in graph["goals"]:
                if goal.get("category") == "execution":
                    return goal["id"]
            return graph["goals"][0]["id"]
        return f"goal-{self.run_id}"

    def decompose(self) -> State:
        """Turn the clarified IntentSpec into a validated GoalGraph artifact.

        Builds the goal DAG, structurally validates it (GoalGraph) and confirms
        it is acyclic with referenced edges (GraphBuilder), then persists it as
        the ``goal_graph`` artifact for the discovery stage.
        """
        intent = self._load_artifact("intent_spec")
        if intent is None:
            return State.INTAKE

        graph_dict = self._canonical_goal_graph(intent)
        GraphBuilder.validate(GoalGraph(**graph_dict))  # raises on a bad/cyclic graph
        self.context.artifacts["goal_graph"] = self._write_artifact("goal_graph", graph_dict)
        return State.DISCOVER

    def discover(self) -> State:
        """Rank registry tools for the intent and persist the candidate slate.

        Scores every catalogued method against the derived discovery query and
        writes the top-ranked candidates (with a human-readable rationale) as the
        ``discovery`` artifact for plan synthesis.
        """
        intent = self._load_artifact("intent_spec")
        if intent is None:
            return State.PLAN

        query = self._discovery_query(intent)
        ranked = rank_candidates(RegistryLoader().entries(), query, top_k=3)
        artifact = {
            "query": {
                "capability_tags": query.capability_tags,
                "input_format": query.input_format,
            },
            "candidates": [
                {
                    "rank": c.rank,
                    "id": c.entry.id,
                    "name": c.entry.name,
                    "version": c.entry.version,
                    "composite": round(c.composite, 4),
                    "components": c.components.as_dict(),
                }
                for c in ranked
            ],
            "rationale": explain_ranking(ranked),
        }
        self.context.artifacts["discovery"] = self._write_artifact("discovery", artifact)
        return State.PLAN

    def plan(self) -> State:
        """Synthesize a complete ExecutionPlan from the top-ranked candidate.

        Deterministically re-ranks, takes #1, and composes a plan with cost,
        compute, and risk/safety metadata, written as the ``execution_plan``
        artifact. Researcher approval (the PLAN->BUILD guard's ``plan_approved``)
        remains a separate gate that this handler does not set.
        """
        intent = self._load_artifact("intent_spec")
        if intent is None:
            return State.BUILD

        query = self._discovery_query(intent)
        ranked = rank_candidates(RegistryLoader().entries(), query, top_k=1)
        if not ranked:
            return State.BUILD

        execution_plan = PlanSynthesizer().synthesize(
            candidate=ranked[0],
            goal_id=self._primary_goal_id(),
            acceptance_metrics=intent.get("acceptance_metrics", []),
            requested_capability=query.capability_tags[0],
        )
        self.context.artifacts["execution_plan"] = self._write_artifact(
            "execution_plan", asdict(execution_plan))
        return State.BUILD

    def build(self) -> State:
        """Generate a runnable RunBundle from the ExecutionPlan (Story 5.1).

        Deterministic, template-based codegen (no LLM): the CodegenEngine picks
        a template from the plan's selected tool, substitutes its paths,
        parameters, and acceptance criteria, and writes a self-contained bundle
        -- main.py, config.yaml, requirements.txt, inline_tests.py -- that the
        execution adapter can run without manual edits. The bundle directory and
        the main.py entrypoint are recorded as artifacts for EXECUTE.
        """
        plan = self._load_artifact("execution_plan")
        if plan is None:
            # No plan to build from; no-op to EXECUTE rather than raise, so this
            # handler stays callable in isolation / on a resume that skipped PLAN.
            return State.EXECUTE
        intent = self._load_artifact("intent_spec")
        bundle = CodegenEngine().generate(plan, intent=intent)
        bundle_dir = Path(self.artifacts_dir) / f"run_bundle_{self.run_id}"
        bundle.write(bundle_dir)
        self.context.artifacts["run_bundle"] = str(bundle_dir)
        self.context.artifacts["script"] = str(bundle_dir / bundle.entrypoint)
        return State.EXECUTE
    def execute(self) -> State:
        """Run the generated RunBundle on the local machine (Story 5.2).

        Off by default (``execute_locally=False``) so offline/seeded pipeline
        runs keep EXECUTE a no-op and trust the seeded ``execution_status``. When
        enabled, the LocalExecutionAdapter runs the bundle built in BUILD --
        installing deps into a venv if requested, running the smoke tests first,
        then ``python main.py`` under a resource monitor + timeout. The captured
        logs and metrics are written as the ``execution_result`` artifact, and
        ``execution_status`` is set from the run so the EXECUTE->INTERPRET guard
        reflects what actually happened.
        """
        if not self.execute_locally:
            return State.INTERPRET

        bundle_dir = self.context.artifacts.get("run_bundle")
        if not bundle_dir or not Path(bundle_dir).is_dir():
            # Nothing to execute (e.g. build() no-opped without a plan); leave the
            # guard to whatever seeded the context.
            return State.INTERPRET

        adapter = self._execution_adapter
        if adapter is None:
            from execution_adapter.local_adapter import LocalExecutionAdapter
            # Keep the working dir (with its outputs) under the session artifacts
            # dir so results are discoverable and scoped to this run.
            adapter = LocalExecutionAdapter(workspace_root=str(self.artifacts_dir))

        result = adapter.execute(
            bundle_dir,
            install_deps=self.execute_install_deps,
            keep_artifacts=self.execute_keep_artifacts,
            run_smoke=True,
            timeout=self.execute_timeout,
            run_id=self.run_id,  # names the workdir exec_<session_id> for traceability
        )
        self.context.artifacts["execution_result"] = self._write_artifact(
            "execution_result", result.to_dict()
        )
        self.context.execution_status = bool(result.succeeded)
        return State.INTERPRET
    def interpret(self) -> State:
        return State.VALIDATE
    def validate(self) -> State:
        """Route on the cross-validation verdict recorded in the context.

        ``accepted`` -> ACCEPT, ``rejected`` -> REPLAN, ``needs_review`` -> CORRECT
        -- the three targets the guard table already allows out of VALIDATE. The
        verdict itself is produced upstream (interpret()/cross-validation); this
        handler only routes on it. Defaults to ACCEPT when the verdict is unset so
        a seeded happy-path run still terminates (the VALIDATE->ACCEPT guard then
        confirms the verdict is truly ``accepted`` before committing).
        """
        verdict = self.context.validation_result
        if verdict == "rejected":
            return State.REPLAN
        if verdict == "needs_review":
            return State.CORRECT
        return State.ACCEPT
    def accept(self) -> State:
        return State.TERMINATE
    def correct(self) -> State:
        return State.BUILD
    def replan(self) -> State:
        return State.PLAN