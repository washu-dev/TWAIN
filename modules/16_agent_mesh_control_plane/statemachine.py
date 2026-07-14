import json
import logging
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from enum import Enum, auto
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import twain_paths

logger = logging.getLogger(__name__)

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
from method_discovery.calculator_registry import (
    DEFAULT_DOCKER_IMAGE, DOCKER_LINUX_PLATFORM, calculators_for_property,
    canonical_property, current_platform, docker_available, docker_image_available,
    find_calculator, load_calculators, planning_platform,
)
from method_discovery import llm_discovery
from result_interpreter.result_package import ResultPackage
from cross_validation.validation_report import ValidationReport
from provenance_memory.event_log import EventLog
from PromptCompiler import PromptGenerator
from SemanticParsing import Prompter
from budget_tracker import Budget_Tracker
from code_gen.codegen_engine import SIM_ENV, CodegenEngine, canonical_tool_key, pixi_env_python
from code_gen import dependency_inferencer as _depinf

# Output-token budget for LLM code synthesis. A whole main.py runs well past the
# gateway's small default (1024), so give it generous headroom -- a truncated
# script compiles but has no entrypoint and silently produces nothing.
_CODEGEN_MAX_TOKENS = 8192


def _module_importable(module: str) -> bool:
    """Whether ``module`` can be located by the import system (no full import).

    Uses ``find_spec`` so we don't pay the cost/side effects of importing a heavy
    scientific package just to check it's present. Dotted names (e.g.
    ``openff.toolkit``) resolve their parent; any failure to resolve -- including
    a missing parent package -- is treated as "not importable".
    """
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, AttributeError, ValueError):
        return False

GUARDS: dict[tuple[State, State], "Callable[[Context], bool]"] = {
    (State.INTAKE, State.CLARIFY): lambda c: True,
    (State.CLARIFY, State.DECOMPOSE): lambda c: c.clarified,
    (State.CLARIFY, State.CLARIFY) : lambda c: True,
    (State.DECOMPOSE, State.DISCOVER): lambda c: True,
    (State.DISCOVER, State.PLAN): lambda c: True,
    (State.DECOMPOSE,State.INTAKE): lambda c: True,
    (State.PLAN, State.BUILD): lambda c: c.plan_approved,
    (State.BUILD, State.REPAIR): lambda c: c.plan_approved,
    (State.REPAIR, State.EXECUTE): lambda c: c.plan_approved,
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
                 execution_adapter=None, verify_codegen: bool = False,
                 script_doctor=None, library_available=None, sim_available=None,
                 auto_approve=False):
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
        # When on, the REPAIR stage may call the LLM to repair the synthesized
        # calculator script and proactively scan it for latent bugs. Off by
        # default so offline/seeded/test runs make no network calls there; the
        # REPAIR stage still runs its free static checks (see repair()).
        self.verify_codegen = verify_codegen
        # Injectable ScriptDoctor for the REPAIR stage (tests supply one with a
        # stub agent + verifier so repair runs fully offline). None -> repair()
        # builds one from the run's plan/agent.
        self._script_doctor = script_doctor
        # How discovery checks whether a library is actually installed. Discovery
        # must only ever commit to a preset library that can be imported where the
        # run happens (the default interpreter), so it never plans around a tool
        # that isn't there. Defaults to a real in-process import probe; injectable
        # (name -> True/False/None) so planning tests stay hermetic and offline.
        self._library_available = library_available
        # How discovery checks the *sim* env (where CALCULATOR bundles run). A
        # calculator-run toolset must be importable in sim, not just the default
        # env -- e.g. PySCF has no sim build, so it must not ride along in a
        # calculator toolset. ``list[import_name] -> set(missing)``; None -> a real
        # (batched, one-subprocess) sim probe. Injectable so planning tests stay
        # hermetic (no sim subprocess).
        self._sim_available = sim_available
        # Unattended mode: run to completion without pausing for human confirmation
        # at the heavy-calculation gate (the plan-approval gate is enforced by the
        # driver, e.g. the runner). Set by the orchestrator/runner for automatic runs.
        self.auto_approve = auto_approve
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

    def _agent_text(self, prompt: str, **call_kwargs) -> str:
        """Call the agent and return its text, accepting either agent shape.

        ``agent`` may be a plain ``prompt -> str`` callable (what the orchestrator
        and tests inject) or an ``AgentInterface``-style object whose
        ``callAgent`` returns ``{"content": [{"text": ...}]}`` (the live LLM).
        ``call_kwargs`` (e.g. ``max_tokens``) are forwarded only to the
        ``callAgent`` form; a plain callable is invoked with just the prompt.
        """
        agent = self.agent
        if hasattr(agent, "callAgent"):
            resp = agent.callAgent(prompt, **call_kwargs)
        else:
            resp = agent(prompt)
        if isinstance(resp, str):
            return resp
        return resp["content"][0]["text"]

    @staticmethod
    def _extract_json_object(text: str) -> str:
        """Return the JSON object embedded in an LLM response.

        Models often wrap JSON in prose or ```json fences despite instructions.
        Strip fences, then take the substring from the first ``{`` to the last
        ``}`` so ``json.loads`` sees a clean object.
        """
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            return fenced.group(1)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start:end + 1]
        return text

    @staticmethod
    def _extract_json_object(text: str) -> str:
        """Return the JSON object embedded in an LLM response.

        Models often wrap JSON in prose or ```json fences despite instructions.
        Strip fences, then take the substring from the first ``{`` to the last
        ``}`` so ``json.loads`` sees a clean object.
        """
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            return fenced.group(1)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start:end + 1]
        return text

    @staticmethod
    def _system_kind(intent: dict) -> str:
        """The target system's representation: 'crystal', 'surface', or 'molecule'.

        Reads the IntentSpec's explicit ``kind`` discriminator when present, else
        infers it from which sub-object the spec carries (periodic solids under
        ``crystal``, discrete molecules under ``molecule``). Defaults to
        'molecule' so a spec with neither behaves as it did before.
        """
        sysd = intent.get("system_descriptors") or {}
        kind = str(sysd.get("kind") or "").lower()
        if kind in ("molecule", "crystal", "surface"):
            return kind
        if isinstance(sysd.get("crystal"), dict) and sysd.get("crystal"):
            return "crystal"
        return "molecule"

    def _is_confident(self, intent: dict) -> bool:
        """True when every *relevant* confidence score meets the threshold.

        Scores that don't apply to the chosen system representation are ignored,
        so a crystal is never gated on a (meaningless) ``SMILES_confidence`` and a
        molecule isn't gated on ``phase``/``structure`` confidence. Without this,
        a solid-state request loops in CLARIFY forever asking for a SMILES it can
        never sensibly provide.
        """
        scores = (intent.get("metadata") or {}).get("confidence_scores") or {}
        if not scores:
            return False
        if self._system_kind(intent) in ("crystal", "surface"):
            irrelevant = {"smiles_confidence", "name_confidence"}
        else:
            irrelevant = {"phase_confidence", "structure_confidence"}
        relevant = {k: v for k, v in scores.items() if k.lower() not in irrelevant}
        if not relevant:
            return False
        return all(value >= self.confidence_threshold for value in relevant.values())

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

    def _build_goal_graph(self, intent: dict) -> dict:
        """Decompose the IntentSpec into a GoalGraph DAG.

        Prefers an LLM-driven decomposition (mirroring intake/clarify) so the DAG
        is tailored to the actual request rather than a fixed template. When no
        agent is configured -- or the agent's output is missing, malformed, or not
        an acyclic graph -- it falls back to the deterministic canonical
        decomposition so the stage always yields a valid GoalGraph (and stays
        runnable fully offline). The returned dict is structurally re-validated by
        the caller before it is persisted.
        """
        if self._agent is None:
            return self._canonical_goal_graph(intent)
        try:
            graph = self._decompose_with_agent(intent)
            GraphBuilder.validate(GoalGraph(**graph))  # schema + acyclicity gate
            return graph
        except Exception as exc:
            # Do NOT silently degrade: an agent is present, so a fallback means
            # the LLM decomposition was unusable. Record why (and the raw
            # response) so the run is debuggable, then fall back deterministically.
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "agent goal-graph decomposition failed; using canonical fallback (%s)",
                reason,
            )
            self._dump_failed_decomposition(reason)
            return self._canonical_goal_graph(
                intent, fallback_reason=f"agent decomposition failed -> {reason}"
            )

    def _decompose_with_agent(self, intent: dict) -> dict:
        """Ask the agent to decompose ``intent`` into a GoalGraph dict.

        Builds a schema-anchored prompt from goal_graph.schema.json and parses the
        agent's JSON response, backfilling the required metadata fields the schema
        demands so a model that omits them still yields a valid graph. A generous
        ``max_tokens`` is requested because a full goal DAG is far larger than the
        default budget -- too small a budget truncates the JSON mid-object.
        """
        schema = str(twain_paths.SCHEMAS_DIR / "goal_graph.schema.json")
        prompt = self.promptGenerator.goalGraphPrompt(
            schema, json.dumps(intent), self.run_id
        )
        self._last_decomposition_raw = self._agent_text(prompt, max_tokens=4096)
        graph = json.loads(self._extract_json_object(self._last_decomposition_raw))
        metadata = graph.setdefault("metadata", {})
        metadata.setdefault("source_intent_id", self.run_id)
        metadata.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        return graph

    def _dump_failed_decomposition(self, reason: str) -> None:
        """Persist the raw agent response that failed to parse/validate.

        Written next to the run's other artifacts so a canonical fallback can be
        traced to the exact LLM output that caused it.
        """
        try:
            self.context.artifacts["goal_graph_error"] = self._write_artifact(
                "goal_graph_error",
                {
                    "reason": reason,
                    "raw_response": getattr(self, "_last_decomposition_raw", None),
                },
            )
        except Exception:
            pass  # diagnostics are best-effort; never fail the run for them

    def _canonical_goal_graph(self, intent: dict, fallback_reason: str = "") -> dict:
        """Deterministic discover -> execute -> validate DAG from the intent.

        The offline fallback for :meth:`_build_goal_graph`: a decomposition that
        every property/simulation task shares -- pick a method, run it, validate
        the output against the acceptance metrics. Used when no agent is available
        or the agent's decomposition cannot be validated. ``fallback_reason``, when
        supplied, is recorded in the graph's rationale so a canonical graph emitted
        despite a live agent is traceable to the failure that caused it.
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
        rationale = "Canonical discover -> execute -> validate decomposition."
        if fallback_reason:
            rationale += f" ({fallback_reason})"
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_intent_id": self.run_id,
            "rationale": rationale,
        }
        return {"goals": goals, "edges": edges, "metadata": metadata}

    def _library_importable(self, name: str) -> Optional[bool]:
        """Whether the registry library ``name`` is installed in the run interpreter.

        Discovery/planning and plain library runs all happen in the default env,
        so a library that can't be imported here can't actually run. Maps the
        registry name to its top-level import module via ``dependency_inferencer``
        (``scikit-learn`` -> ``sklearn``, ``openff-toolkit`` -> ``openff.toolkit``,
        ...) and probes it. Returns True/False, or None when it genuinely can't
        tell -- callers treat None as "trust the ranking" rather than dropping a
        candidate on a probe glitch. Injectable via ``library_available``.
        """
        if self._library_available is not None:
            return self._library_available(name)
        deps = _depinf.import_names(name)
        if not deps:
            return None
        return _module_importable(deps[0].import_name)

    def _installed_candidates(self, ranked):
        """Keep only ranked candidates whose library is installed here.

        This is the guarantee behind "a preset list of libraries that are all
        installed": discovery never commits to a library the run can't import.
        Candidates whose availability is unknown (probe -> None) are kept. Ranks
        are renumbered 1..n over the survivors so the top pick is always rank 1.
        Returns ``(kept, dropped_names)``; if the filter would drop *everything*
        (e.g. probed in a bare environment) the original ranking is returned
        unchanged so planning is never stranded.
        """
        kept, dropped = [], []
        for c in ranked:
            # Probe by id: ids are the clean canonical keys (``openbabel``),
            # whereas display names ("Open Babel") don't always map to an import.
            if self._library_importable(c.entry.id) is False:
                dropped.append(c.entry.name)
            else:
                kept.append(c)
        if not kept:
            return list(ranked), []
        for i, c in enumerate(kept, start=1):
            c.rank = i
        return kept, dropped

    def _sim_missing(self, import_names) -> set:
        """Which of ``import_names`` are NOT importable in the sim env.

        Calculator bundles run in the sim env, so a toolset library absent there
        (e.g. PySCF has no py3.11 sim build) would fail the bundle's smoke even
        though it's installed in the default env. Probes the sim interpreter once
        (batched, one subprocess); returns an empty set when it can't probe (no
        sim env built) so we never over-prune on an environment we can't see.
        Injectable via ``sim_available`` for hermetic tests.
        """
        names = [n for n in dict.fromkeys(import_names) if n]
        if not names:
            return set()
        if self._sim_available is not None:
            return set(self._sim_available(names))
        sim_py = pixi_env_python(SIM_ENV)
        if not sim_py:
            return set()
        code = (
            "import importlib.util, sys, json\n"
            "out = []\n"
            "for m in json.loads(sys.argv[1]):\n"
            "    try:\n"
            "        if importlib.util.find_spec(m) is None: out.append(m)\n"
            "    except Exception:\n"
            "        out.append(m)\n"
            "print(json.dumps(out))"
        )
        try:
            proc = subprocess.run([sim_py, "-c", code, json.dumps(names)],
                                  capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                return set(json.loads((proc.stdout or "").strip() or "[]"))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return set()

    def _runnable_toolset(self, libraries, driver):
        """Drop toolset libraries not importable in the sim (calculator-run) env.

        Returns ``(kept, dropped)``. Always keeps ``driver`` (the library the
        calculator is driven through) and never returns an empty toolset. This is
        the env-aware complement to :meth:`_installed_candidates` (which grounds
        against the default env where planning + plain runs happen): a calculator
        run executes in sim, so its whole toolset must import there -- e.g. a
        PySCF that the model bundled alongside an ASE+MatGL run is dropped because
        it has no sim build, while sim-present libraries (ASE, Pymatgen) stay.
        """
        imports = {lib: _depinf.import_names(lib)[0].import_name for lib in libraries}
        missing = self._sim_missing(list(imports.values()))
        if not missing:
            return libraries, []
        kept, dropped = [], []
        for lib in libraries:
            if imports[lib] in missing and lib.lower() != (driver or "").lower():
                dropped.append(lib)
            else:
                kept.append(lib)
        return (kept or libraries), dropped

    def _discovery_query(self, intent: dict) -> DiscoveryQuery:
        """Derive a discovery query (capability tags + input format) from the intent."""
        objective = (intent.get("objective") or "").lower()
        domain = (intent.get("domain") or "").lower()

        keyword_map = {
            # Electronic-structure asks (band gap, band structure, DOS, ...) need a
            # DFT calculator driven by an atomistic library -- listed first so it
            # leads the tag order for these queries.
            "electronic_structure": (
                "band gap", "bandgap", "band-gap", "band_gap", "band structure",
                "electronic", "dft", "ab initio", "density of states", " dos",
                "fermi", "conduct", "dielectric", "work function",
            ),
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

        # Map the system representation onto the driver-library input format the
        # registry scores against: periodic solids are consumed as CIF (the format
        # ASE/Pymatgen/quacc declare), discrete molecules as SMILES.
        input_format = None
        sysd = intent.get("system_descriptors") or {}
        if self._system_kind(intent) in ("crystal", "surface"):
            input_format = "CIF"
        elif (sysd.get("molecule") or {}).get("SMILES"):
            input_format = "SMILES"
        return DiscoveryQuery(capability_tags=tags, input_format=input_format)

    def _requested_property(self, intent: dict) -> Optional[str]:
        """Canonical property the researcher wants (or None if none is recognized).

        Blends the free-text objective with the acceptance-metric names so a spec
        that says "band gap" in either place resolves to ``band_gap``. This is
        what tells the planner whether the run needs a dedicated calculator (a DFT
        engine for band structure) versus a plain library run.
        """
        parts = [str(intent.get("objective") or "")]
        for metric in intent.get("acceptance_metrics", []) or []:
            if isinstance(metric, dict) and metric.get("metric_name"):
                parts.append(str(metric["metric_name"]))
        return canonical_property(" ".join(parts))

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

        Decomposes the intent into a goal DAG (agent-driven when an agent is
        configured, deterministic otherwise), structurally validates it
        (GoalGraph) and confirms it is acyclic with referenced edges
        (GraphBuilder), then persists it as the ``goal_graph`` artifact for the
        discovery stage.
        """
        intent = self._load_artifact("intent_spec")
        if intent is None:
            return State.INTAKE

        graph_dict = self._build_goal_graph(intent)
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
        ranked = rank_candidates(RegistryLoader().entries(), query, top_k=None)
        # Only surface candidates that are actually installed here, then take the
        # top 3 -- the slate the researcher sees is one they can really run.
        ranked, _dropped = self._installed_candidates(ranked)
        ranked = ranked[:3]
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
        """Synthesize a complete ExecutionPlan by choosing a toolset for the task.

        The choice is made by **LLM discovery** when an agent is available: the
        model reasons over the candidate tools (the registry + calculator catalog
        as a seed, with factual metadata like ``needs_external_data``), the target
        platform, and practical fitness, and picks a library (+ optional python
        calculator). A deterministic availability check grounds the pick so an
        unrunnable tool (e.g. GPAW on a Mac) is never committed. When no agent is
        wired, or the LLM's pick can't be grounded, it falls back to the
        deterministic, platform-aware registry ranking (:meth:`_select_toolset`).
        The plan carries the full toolset + target material + property; researcher
        approval (the PLAN->BUILD guard) is a separate gate this handler doesn't set.
        """
        intent = self._load_artifact("intent_spec")
        if intent is None:
            return State.BUILD

        query = self._discovery_query(intent)
        entries = RegistryLoader().entries()
        ranked = rank_candidates(entries, query, top_k=None)  # full ranking
        if not ranked:
            return State.BUILD
        # Ground the toolset in what's installed: drop any candidate library that
        # isn't importable in the run interpreter, so both the deterministic pick
        # (ranked[0]) and the LLM's candidate slate are guaranteed runnable here.
        ranked, dropped_uninstalled = self._installed_candidates(ranked)

        requested_property = self._requested_property(intent)
        domain = (intent.get("domain") or "").lower() or None
        # Plan against the richest platform we can actually reach: with a Docker
        # daemon up, that's linux-64 (the runner image), so a higher-fidelity
        # Linux-only engine (e.g. GPAW) is selectable on a Mac and its run is
        # routed into the container at EXECUTE. Without Docker it's the host, so
        # the best *native* engine is chosen. host_platform drives the routing note.
        host_platform = current_platform()
        platform = planning_platform(host_platform, docker=docker_available())

        recommendation = self._llm_recommend(intent, ranked, requested_property, domain, platform)
        if recommendation is not None:
            # Resolve the model's primary library to a known candidate so we probe
            # by its clean id; an unlisted/invented name falls through as-is.
            picked = self._candidate_by_name(ranked, recommendation.libraries[0])
            primary_key = picked.entry.id if picked else recommendation.libraries[0]
            if self._library_importable(primary_key) is False:
                # The model named a library that isn't installed here -- don't
                # plan around something the run can't import; fall back to the
                # deterministic, installed pick.
                recommendation = None
        if recommendation is not None:
            libraries = recommendation.libraries
            primary = self._candidate_by_name(ranked, libraries[0]) or ranked[0]
            calc_entry = find_calculator(recommendation.calculator)
            calculator_library = recommendation.calculator_library
            selection_note = ("Discovery (LLM) chose " + " + ".join(libraries)
                              + (f" with {recommendation.calculator}" if recommendation.calculator else "")
                              + (f": {recommendation.reasoning}" if recommendation.reasoning else ""))
        else:
            libraries, calc_entry, calculator_library = self._select_toolset(
                ranked, requested_property, domain, platform=platform)
            primary = ranked[0]
            selection_note = None

        # A calculator run executes in the sim env, so its toolset must import
        # THERE, not just in the default env the candidate grounding checked. Drop
        # any library with no sim build (e.g. a PySCF the model bundled alongside
        # an ASE+MatGL run) so it can't fail the bundle's smoke; keep the driver
        # and re-point the primary if it was the one dropped.
        dropped_unrunnable = []
        if calc_entry is not None:
            driver = calculator_library or calc_entry.driver_library
            libraries, dropped_unrunnable = self._runnable_toolset(libraries, driver)
            if dropped_unrunnable and primary.entry.name in dropped_unrunnable:
                primary = self._candidate_by_name(ranked, libraries[0]) or primary

        execution_plan = PlanSynthesizer().synthesize(
            candidate=primary,
            goal_id=self._primary_goal_id(),
            acceptance_metrics=intent.get("acceptance_metrics", []),
            requested_capability=query.capability_tags[0],
        )
        execution_plan.selected_method.libraries = libraries
        if selection_note:
            execution_plan.safety_notes.append(selection_note)
        if dropped_uninstalled:
            execution_plan.safety_notes.append(
                "Discovery skipped candidate(s) not installed in the run "
                "environment: " + ", ".join(dropped_uninstalled))
        if dropped_unrunnable:
            execution_plan.safety_notes.append(
                f"Dropped from the toolset (no build in the '{SIM_ENV}' environment "
                f"where the calculator runs): {', '.join(dropped_unrunnable)}")

        if calc_entry is not None:
            execution_plan.selected_method.calculator = calc_entry.name
            execution_plan.selected_method.calculator_import = calc_entry.import_name
            execution_plan.selected_method.calculator_library = calculator_library or calc_entry.driver_library
            note = (f"Toolset {' + '.join(libraries)} with calculator {calc_entry.name} "
                    f"(driven via {execution_plan.selected_method.calculator_library}) "
                    f"for property '{requested_property}'")
            if calc_entry.heavy:
                note += " (heavy run -- confirm before executing)"
            if calc_entry.needs_external_data:
                note += " (needs external parameter data to run)"
            execution_plan.safety_notes.append(note)
            if calc_entry.needs_docker(host_platform):
                # Non-native engine (e.g. GPAW on a Mac): the run is offloaded to
                # the linux-64 runner container. Say so explicitly -- the choice
                # is never silent (the substitution problem the user hit before).
                execution_plan.safety_notes.append(
                    f"{calc_entry.name} has no {host_platform} build; it will run in the "
                    f"linux-64 Docker container (image '{DEFAULT_DOCKER_IMAGE}', linux/amd64 "
                    f"emulation -- slower than native). Build it first if needed: "
                    f"`docker build --platform linux/amd64 -f runner/Dockerfile "
                    f"-t {DEFAULT_DOCKER_IMAGE} .` (see runner/README.md).")
            if calc_entry.ml_surrogate:
                # The researcher wants real calculations, not predictions: make it
                # unmistakable when the only available engine is an ML surrogate.
                execution_plan.safety_notes.append(
                    f"NOTE: {calc_entry.name} is an ML surrogate -- it PREDICTS "
                    f"'{requested_property}' from a trained model, not a "
                    f"first-principles calculation. For a real result, run on a "
                    f"platform with a genuine engine (e.g. linux-64 uses GPAW DFT).")
        elif requested_property:
            # No calculator was attached. Say why: either none is catalogued for
            # the property, or one exists but has no build for this platform
            # (e.g. GPAW on a Mac) -- so discovery didn't pick an unrunnable tool.
            anywhere = calculators_for_property(requested_property, domain=domain, platform=None)
            if anywhere:
                execution_plan.safety_notes.append(
                    f"A calculator for '{requested_property}' exists ({anywhere[0].name}) but has "
                    f"no build for this platform ({host_platform}); using {libraries[0]} alone -- run on a "
                    f"supported platform (e.g. linux-64) to use it.")

        # Accuracy-first: if Docker is down we planned natively, which may be a
        # lower-fidelity engine than a Linux-only one we could reach via Docker.
        # Surface that so the downgrade is explicit and actionable, never silent.
        if requested_property and not docker_available():
            best = calculators_for_property(requested_property, domain=domain,
                                            platform=DOCKER_LINUX_PLATFORM)
            chosen_id = calc_entry.id if calc_entry is not None else None
            if best and best[0].id != chosen_id and best[0].needs_docker(host_platform):
                execution_plan.safety_notes.append(
                    f"Higher fidelity available: {best[0].name} (Linux-only) would compute "
                    f"'{requested_property}' at higher fidelity than the native choice"
                    + (f" ({calc_entry.name})" if calc_entry is not None else "")
                    + f". Install & start Docker, build the '{DEFAULT_DOCKER_IMAGE}' image "
                    f"(runner/README.md), and TWAIN will run it in the linux-64 container.")
        execution_plan.target_system = intent.get("system_descriptors") or None
        execution_plan.requested_property = requested_property

        self.context.artifacts["execution_plan"] = self._write_artifact(
            "execution_plan", asdict(execution_plan))
        return State.BUILD

    @staticmethod
    def _candidate_by_name(ranked, name):
        """The ScoredCandidate for a library name (or None)."""
        want = (name or "").lower()
        for c in ranked:
            if c.entry.name.lower() == want or c.entry.id.lower() == want:
                return c
        return None

    def _llm_recommend(self, intent, ranked, requested_property, domain, platform):
        """Ask the LLM to pick a toolset, grounded by platform availability.

        Returns a ToolRecommendation, or None (no agent wired, or the pick
        couldn't be grounded) so ``plan()`` uses the deterministic path. Uses the
        raw injected agent -- never triggers a network build of AgentInterface
        just to plan, so offline planning tests stay offline.
        """
        if self._agent is None:
            return None
        library_candidates = [
            {"name": c.entry.name, "capabilities": list(c.entry.capability_tags),
             "description": c.entry.description}
            for c in ranked
        ]
        calc_candidates = [
            {"name": e.name, "id": e.id, "import_name": e.import_name,
             "pip_name": e.pip_name, "driver_library": e.driver_library,
             "capabilities": list(e.capabilities), "platforms": list(e.platforms),
             "needs_external_data": e.needs_external_data,
             "ml_surrogate": e.ml_surrogate, "description": e.description}
            for e in load_calculators()
        ]
        sysd = intent.get("system_descriptors") or {}
        molecule = sysd.get("molecule") or {} if isinstance(sysd, dict) else {}
        material = (sysd.get("formula") if isinstance(sysd, dict) else None) or molecule.get("name") or ""
        try:
            return llm_discovery.recommend_toolset(
                objective=intent.get("objective", ""), material=material, domain=domain,
                requested_property=requested_property, platform=platform,
                libraries=library_candidates, calculators=calc_candidates,
                agent=self._agent_text,
            )
        except Exception:  # noqa: BLE001 - any failure -> deterministic fallback
            return None

    def _select_toolset(self, ranked, requested_property, domain, platform=None):
        """Assemble a compatible toolset from the discovery ranking.

        Returns ``(libraries, calculator_entry_or_None, calculator_library_or_None)``.
        ``libraries[0]`` is the primary (discovery's #1). If the property needs a
        calculator, the best covering one that is *available on this platform* is
        attached: preferring a calculator compatible with the primary, else
        bringing in a bridging library the calculator *is* compatible with
        (preferring one discovery also ranked), so multiple libraries can be used
        together. Fully data-driven -- no tool is forced, and a calculator with no
        build for the current platform (e.g. GPAW on a Mac) is never chosen.
        ``platform`` defaults to the current platform.
        """
        primary = ranked[0].entry
        libraries = [primary.name]
        if not requested_property:
            return libraries, None, None
        if platform is None:
            platform = current_platform()
        covering = calculators_for_property(requested_property, domain=domain, platform=platform)
        if not covering:
            return libraries, None, None

        # 1) a covering calculator compatible with the primary library
        for calc in covering:
            if calc.supports_library(primary.name):
                return libraries, calc, primary.name

        # 2) none compatible with the primary -> pair the best covering calculator
        #    with a library it supports (preferring one discovery ranked), and use
        #    both libraries together.
        calc = covering[0]
        bridge = self._pick_compatible_library(ranked, calc)
        if bridge and bridge.lower() != primary.name.lower():
            libraries.append(bridge)
        return libraries, calc, bridge

    @staticmethod
    def _pick_compatible_library(ranked, calc):
        """Name of a library ``calc`` can be driven through, preferring a ranked one."""
        compat = {c.lower() for c in (calc.compatible_libraries or [])}
        if not compat and calc.driver_library:
            compat = {calc.driver_library.lower()}
        for cand in ranked:
            if cand.entry.name.lower() in compat or cand.entry.id.lower() in compat:
                return cand.entry.name
        if calc.compatible_libraries:
            return calc.compatible_libraries[0]
        return calc.driver_library

    def build(self) -> State:
        """Generate a runnable RunBundle from the ExecutionPlan (Story 5.1).

        The CodegenEngine emits a self-contained bundle -- main.py, config.yaml,
        requirements.txt, inline_tests.py -- that the execution adapter can run
        without manual edits. For a plain library run this is deterministic,
        template-based codegen; when the plan selected a calculator (e.g. GPAW
        for a DFT band gap) the engine LLM-synthesizes a script tailored to the
        target material, falling back to a generic scaffold if synthesis is
        unavailable. Either way the material from the IntentSpec reaches the
        generated code, so the script computes the requested system rather than a
        hard-coded sample. BUILD only *generates* -- verifying and repairing the
        script is the next stage (REPAIR); the bundle directory and the main.py
        entrypoint are recorded as artifacts for it and for EXECUTE.
        """
        plan = self._load_artifact("execution_plan")
        if plan is None:
            # No plan to build from; no-op through REPAIR (which also no-ops with
            # no bundle) rather than raise, so this handler stays callable in
            # isolation / on a resume that skipped PLAN.
            return State.REPAIR
        intent = self._load_artifact("intent_spec")
        # Pass the intent + an agent callable so codegen can (a) build a script
        # for the actual material in the intent, and (b) LLM-synthesize a tailored
        # script when a calculator is selected (band gap -> ASE+GPAW). The agent is
        # only invoked on the calculator path; a plain library run stays offline
        # and deterministic, and any synthesis failure falls back to a template.
        # Code synthesis emits a whole main.py, so it needs a far larger output
        # budget than the gateway's small default -- otherwise the script is
        # truncated mid-statement (compiles, but has no runnable entrypoint, so it
        # produces no results). Verifying/repairing the script is the REPAIR
        # stage's job, so BUILD only generates.
        # Decide whether the generated --smoke check should run the property
        # computation for real (so a wrong API call/keyword is caught in REPAIR) or
        # just load the tool. A cheap, self-contained calculator (ML predictor /
        # semiempirical, no external data) can compute in smoke; a heavy or
        # external-data one (full DFT, needs pseudopotentials/SK files) cannot. A
        # library-only run (the library computes the property itself, e.g. PySCF) is
        # self-contained, so it computes in smoke too. A calculator may override the
        # heuristic via `smoke_can_compute` when it's heavy/external-data for a full
        # run yet can still do a cheap single-point smoke because TWAIN provisions
        # its data -- e.g. DFTB+ (semiempirical; .skf files fetched into DFTB_PREFIX).
        method = plan.get("selected_method") or {}
        calc_name = method.get("calculator")
        if calc_name:
            ce = find_calculator(calc_name)
            if ce is None:
                smoke_compute = False
            elif ce.smoke_can_compute is not None:
                smoke_compute = ce.smoke_can_compute
            else:
                smoke_compute = not ce.heavy and not ce.needs_external_data
        else:
            smoke_compute = True
        bundle = CodegenEngine().generate(
            plan, intent=intent,
            agent=lambda p: self._agent_text(p, max_tokens=_CODEGEN_MAX_TOKENS),
            smoke_compute=smoke_compute)
        bundle_dir = Path(self.artifacts_dir) / f"run_bundle_{self.run_id}"
        bundle.write(bundle_dir)
        self.context.artifacts["run_bundle"] = str(bundle_dir)
        self.context.artifacts["script"] = str(bundle_dir / bundle.entrypoint)
        return State.REPAIR

    def repair(self) -> State:
        """Verify and, if needed, repair the generated script before EXECUTE.

        A dedicated stage (its own state) that turns the *plausible* ``main.py``
        BUILD emitted into one that actually runs. It runs the
        :class:`code_gen.script_doctor.ScriptDoctor`, which (1) statically checks
        the script (compiles? has an entrypoint? references the calculator? any
        undefined names?), (2) smoke-runs it in the heavy-calculator env, feeding
        real errors back to the model to self-correct, and (3) proactively reviews
        a runnable script for latent bugs and fixes them before the expensive run.

        Non-blocking by construction: it heals what it can, writes back the
        improved ``main.py``, records a ``repair_report`` artifact, and always
        advances to EXECUTE (which stays the real gate). Only calculator-driven,
        LLM-synthesized bundles are healed -- deterministic template bundles are
        already valid, and a plain run with no bundle just passes through. The LLM
        repair/review calls happen only when ``verify_codegen`` is on and an agent
        is available; otherwise the doctor still runs its free static checks and
        logs any findings.
        """
        bundle_dir = self.context.artifacts.get("run_bundle")
        main_path = Path(bundle_dir) / "main.py" if bundle_dir else None
        if not main_path or not main_path.is_file():
            return State.EXECUTE  # nothing built (e.g. build no-opped); pass through

        plan = self._load_artifact("execution_plan") or {}
        method = plan.get("selected_method") or {}
        calc_import = method.get("calculator_import")
        # Heal only LLM-synthesized scripts: a calculator-driven run (calculator_import
        # set) or a library-only run that synthesized real code (config marks it
        # 'llm_synthesized'). A deterministic-template or generic-stub bundle is
        # already valid, so it passes straight through.
        is_synthesized = self._bundle_config(bundle_dir).get("template") == "llm_synthesized"
        if not calc_import and not is_synthesized:
            return State.EXECUTE

        from code_gen.script_doctor import ScriptDoctor
        # Smoke-run in the interpreter the bundle will actually run in: the sim env
        # for a calculator run, else the default interpreter -- a library-only run
        # (e.g. PySCF) resolves there, not in sim.
        smoke_python = pixi_env_python(SIM_ENV) if calc_import else sys.executable
        doctor = self._script_doctor or ScriptDoctor(
            agent=(lambda p: self._agent_text(p, max_tokens=_CODEGEN_MAX_TOKENS))
            if self.verify_codegen else None,
            brief=self._repair_brief(plan, method),
            sim_python=smoke_python,
        )
        original = main_path.read_text(encoding="utf-8")
        report = doctor.heal(original)
        if report.source and report.source != original:
            main_path.write_text(report.source, encoding="utf-8")
        self.context.artifacts["repair_report"] = self._write_artifact(
            "repair_report", report.to_dict())
        self._log_repair(report)
        return State.EXECUTE

    def _bundle_config(self, bundle_dir) -> dict:
        """Read the built bundle's config.yaml (empty dict if absent/unreadable).

        Used by REPAIR to tell an LLM-synthesized script (``template:
        llm_synthesized``) from a deterministic template that needs no healing.
        """
        if not bundle_dir:
            return {}
        try:
            import yaml
            text = (Path(bundle_dir) / "config.yaml").read_text(encoding="utf-8")
            return yaml.safe_load(text) or {}
        except Exception:  # noqa: BLE001 - missing/invalid config -> no metadata
            return {}

    def _repair_brief(self, plan: dict, method: dict) -> dict:
        """Context the ScriptDoctor needs: tool/calculator imports, property, material."""
        intent = self._load_artifact("intent_spec")
        libraries = method.get("libraries") or [method.get("tool_name")]
        driver = method.get("calculator_library") or (libraries[0] if libraries else "ASE")
        driver_deps = _depinf.import_names(driver)
        driver_import = driver_deps[0].import_name if driver_deps else canonical_tool_key(driver)
        material = CodegenEngine._material_brief(plan, intent)
        return {
            "library": driver,
            "library_import": driver_import,
            "calculator": method.get("calculator"),
            "calculator_import": method.get("calculator_import"),
            "property": plan.get("requested_property") or "the requested property",
            "material_desc": CodegenEngine._material_desc(material),
            "acceptance": plan.get("acceptance_metrics") or [],
            "output_file": "results.csv",
        }

    def _log_repair(self, report) -> None:
        """Print a one-line summary of the repair outcome for the researcher."""
        if report.status == "healthy":
            print("[repair] generated script passed all checks; no changes needed.")
        elif report.status == "repaired":
            print(f"[repair] healed the script in {report.rounds} round(s): "
                  + "; ".join(report.fixes))
        elif report.status == "unverifiable":
            print("[repair] static checks passed; could not smoke-run here "
                  f"(no '{SIM_ENV}' env) -- delivering as-is.")
        else:  # unrepairable
            remaining = "; ".join(d.render() for d in report.remaining[:3])
            print(f"[repair] could not fully repair the script "
                  f"({report.rounds} round(s)); EXECUTE will surface any failure. "
                  f"Remaining: {remaining}")

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

        # Heavy calculations (a DFT band-gap run via GPAW can take many minutes
        # and pull in large deps) are gated on the researcher's go-ahead: the
        # generated script is the deliverable either way, so we ask before
        # burning the compute. Plain/cheap runs skip the prompt entirely.
        if not self._confirm_heavy_execution():
            return self._skip_execution(
                bundle_dir, status="deferred",
                note="Researcher chose not to run the heavy calculation now.")

        # A calculator bundle must run in the heavy-calculator ('sim') env, not
        # the default interpreter -- running it with the default `python` fails
        # with ModuleNotFoundError. Plain library runs stay on this interpreter.
        run_python = self._bundle_python()
        calc = self._selected_calculator()

        adapter = self._execution_adapter
        docker_route = False
        if adapter is None:
            # Route non-native engines (no build for this host, e.g. GPAW on a
            # Mac) into the linux-64 runner container; everything else runs in the
            # local sim/default interpreter. Either way, a run that can't happen
            # here delivers the script with guidance instead of crashing.
            if calc is not None and calc.needs_docker(current_platform()):
                if not docker_available():
                    return self._skip_execution(
                        bundle_dir, status="skipped_missing_dependency",
                        note=f"Not run here: {calc.name} has no {current_platform()} build and "
                             f"needs the linux-64 Docker runner, but no Docker daemon is reachable.",
                        how_to=self._how_to_run(bundle_dir))
                if not docker_image_available(DEFAULT_DOCKER_IMAGE):
                    return self._skip_execution(
                        bundle_dir, status="skipped_missing_dependency",
                        note=f"Not run here: {calc.name} runs in Docker, but the "
                             f"'{DEFAULT_DOCKER_IMAGE}' image isn't built yet.",
                        how_to=self._how_to_run(bundle_dir))
                from execution_adapter.docker_adapter import DockerExecutionAdapter
                adapter = DockerExecutionAdapter(workspace_root=str(self.artifacts_dir))
                docker_route = True
            else:
                # Real local execution. Before spending time, make sure the run can
                # actually happen here: the heavy env must be built, and the selected
                # calculator importable in it. Otherwise deliver the script with clear
                # guidance (a graceful outcome, like a deferral) instead of crashing.
                if calc is not None and run_python is None:
                    return self._skip_execution(
                        bundle_dir, status="skipped_missing_dependency",
                        note=f"Not run here: the '{SIM_ENV}' environment isn't built on this machine.",
                        how_to=self._how_to_run(bundle_dir))
                if not self.execute_install_deps:
                    missing = self._missing_run_imports(run_python)
                    if missing:
                        return self._skip_execution(
                            bundle_dir, status="skipped_missing_dependency",
                            note=f"Not run here: '{missing[0]}' is not installed in the run environment.",
                            how_to=self._how_to_run(bundle_dir))
                from execution_adapter.local_adapter import LocalExecutionAdapter
                # Keep the working dir (with its outputs) under the session artifacts
                # dir so results are discoverable and scoped to this run.
                adapter = LocalExecutionAdapter(workspace_root=str(self.artifacts_dir))

        result = adapter.execute(
            bundle_dir,
            # The sim env / Docker image already ship the whole stack, so never
            # pip-install into a venv there; that only applies to default-interpreter runs.
            install_deps=self.execute_install_deps and run_python is None and not docker_route,
            keep_artifacts=self.execute_keep_artifacts,
            run_smoke=True,
            timeout=self.execute_timeout,
            # Docker fixes the interpreter via the image; native uses run_python
            # (None => the adapter's default interpreter).
            python_executable=None if docker_route else run_python,
            run_id=self.run_id,  # names the workdir exec_<session_id> for traceability
        )
        self.context.artifacts["execution_result"] = self._write_artifact(
            "execution_result", result.to_dict()
        )
        self.context.execution_status = bool(result.succeeded)
        if not result.succeeded:
            # Surface the *real* reason (missing deps, script error, resource
            # limits) with next steps, rather than letting the EXECUTE->INTERPRET
            # guard fail downstream as an opaque "incomplete context".
            raise self._execution_error(result, bundle_dir)
        return State.INTERPRET

    def _selected_calculator(self):
        """The CalculatorEntry the plan selected (or None for a plain run)."""
        plan = self._load_artifact("execution_plan")
        if not plan:
            return None
        name = (plan.get("selected_method") or {}).get("calculator")
        return find_calculator(name)

    def _confirm_heavy_execution(self) -> bool:
        """Ask the researcher before running a heavy calculation; True to proceed.

        Only heavy calculators (DFT engines like GPAW) prompt -- everything else
        proceeds silently. The prompt goes through ``_ask_user`` so it works both
        on the CLI (stdin) and through the web UI's injected ``ask`` seam. Any
        answer other than an explicit yes defers the run.
        """
        calculator = self._selected_calculator()
        if calculator is None or not calculator.heavy:
            return True
        # Unattended mode: the researcher opted into automatic runs, so proceed
        # without asking (approving the plan already authorized this execution).
        if self.auto_approve:
            print("[execute] Unattended mode: proceeding with the heavy "
                  f"{calculator.name} calculation without prompting.")
            return True
        # If we can't actually prompt (headless run with no injected ``ask`` and
        # no interactive stdin), default to deferring rather than hanging on
        # ``input()`` or crashing on EOF. Safe by construction: the script is
        # already built; not running it is the conservative choice.
        if not self._can_prompt():
            print("[execute] Heavy calculation requires confirmation, but no interactive "
                  "input is available; deferring. Inject an 'ask' callable or run "
                  "interactively to execute it.")
            return False
        if calculator.needs_docker(current_platform()):
            where = (f"in the linux-64 Docker container (no {current_platform()} build; "
                     f"runs under linux/amd64 emulation, so slower than native)")
        else:
            where = f"and needs {calculator.name} installed"
        answer = self._ask_user(
            f"The plan builds a {calculator.name} calculation, a heavy DFT run that "
            f"can take several minutes {where}. The "
            f"generated script is ready either way.\nRun it now? [y/N]: "
        )
        return str(answer).strip().lower() in {"y", "yes", "run", "now", "1", "true"}

    def _can_prompt(self) -> bool:
        """Whether we can actually ask the researcher a question right now."""
        if callable(self.ask):
            return True
        try:
            return bool(sys.stdin) and sys.stdin.isatty()
        except Exception:  # noqa: BLE001 - no usable stdin -> can't prompt
            return False

    def _skip_execution(self, bundle_dir: str, *, status: str, note: str,
                        how_to: Optional[str] = None) -> State:
        """Record a not-executed-but-complete outcome and advance cleanly.

        Used when the researcher defers a heavy run, or when the run's heavy
        dependency isn't installed here. The bundle stays on disk as the
        deliverable; provenance records why it wasn't run, and the stage is marked
        complete so the pipeline winds down rather than stalling on the
        EXECUTE->INTERPRET guard (which would otherwise raise an opaque error).
        """
        result = {
            "status": status,
            "succeeded": False,
            "note": note,
            "how_to_run": how_to,
            "bundle_dir": str(bundle_dir),
            "script": self.context.artifacts.get("script"),
        }
        self.context.artifacts["execution_result"] = self._write_artifact(
            "execution_result", result
        )
        message = f"[execute] {note}"
        if how_to:
            message += f"\n[execute] To run it: {how_to}"
        else:
            message += (f"\n[execute] The runnable bundle is at {bundle_dir} -- "
                        f"run it later with:  python {Path(bundle_dir) / 'main.py'}")
        print(message)
        # A built-but-not-executed run is a complete, valid outcome; advance.
        self.context.execution_status = True
        return State.INTERPRET

    def _bundle_python(self) -> Optional[str]:
        """Interpreter the built bundle must run under, or None for the default.

        Calculator bundles need the heavy-calculator ('sim') env; a plain library
        run uses the default interpreter (None -> the adapter's own default).
        Returns None too when a calculator is selected but the sim env isn't built
        here -- the caller turns that into a graceful, guided skip.
        """
        if self._selected_calculator() is None:
            return None
        return pixi_env_python(SIM_ENV)

    def _how_to_run(self, bundle_dir) -> str:
        """Actionable 'run it yourself' guidance appropriate to the bundle's env."""
        main = Path(bundle_dir) / "main.py"
        calc = self._selected_calculator()
        if calc is not None and calc.needs_docker(current_platform()):
            # Non-native engine: guide the linux-64 Docker path (build the image,
            # then TWAIN reruns it in the container -- or run the bundle by hand).
            return (
                f"{calc.name} has no {current_platform()} build, so it runs in the linux-64 "
                f"Docker runner. One-time: install/start Docker (see runner/README.md) and build "
                f"the image: `docker build --platform linux/amd64 -f runner/Dockerfile "
                f"-t {DEFAULT_DOCKER_IMAGE} .`. Then re-run TWAIN (it will execute in the container), "
                f"or run the bundle directly: `docker run --rm --platform linux/amd64 "
                f"-v {Path(bundle_dir)}:/work -w /app {DEFAULT_DOCKER_IMAGE} "
                f"pixi run -e {SIM_ENV} python /work/main.py`.")
        if calc is not None:
            return (f"The bundle runs in TWAIN's '{SIM_ENV}' environment. Run it with: "
                    f"`pixi run -e {SIM_ENV} python {main}` (build the env first with "
                    f"`pixi install` if needed).")
        reqs = Path(bundle_dir) / "requirements.txt"
        return f"Install deps (`pip install -r {reqs}`), then run: `python {main}`."

    def _missing_run_imports(self, run_python: Optional[str] = None) -> list:
        """Heavy imports the run needs that aren't available in its interpreter.

        Lets EXECUTE detect e.g. a missing GPAW before a wasted run and report it
        as actionable guidance instead of a cryptic guard failure. Checks the
        interpreter the bundle will actually run under: the sim env for a
        calculator bundle (``run_python``), else this interpreter.
        """
        import importlib.util
        plan = self._load_artifact("execution_plan") or {}
        calc_import = (plan.get("selected_method") or {}).get("calculator_import")
        if not calc_import:
            return []
        if run_python and run_python != sys.executable:
            # Ask the target interpreter itself whether the calculator imports.
            try:
                proc = subprocess.run(
                    [run_python, "-c",
                     "import importlib.util,sys; "
                     f"sys.exit(0 if importlib.util.find_spec({calc_import!r}) else 1)"],
                    capture_output=True, timeout=30,
                )
                return [] if proc.returncode == 0 else [calc_import]
            except (OSError, subprocess.SubprocessError):
                return [calc_import]
        try:
            available = importlib.util.find_spec(calc_import) is not None
        except Exception:  # noqa: BLE001 - unresolvable spec => treat as missing
            available = False
        return [] if available else [calc_import]

    def _execution_error(self, result, bundle_dir: str) -> Exception:
        """Build a clear, actionable error for a failed execution.

        Prefers the orchestrator's typed ``ConfigError`` (carries a category +
        hint the researcher-facing notifier renders); falls back to a plain
        ``RuntimeError`` with the same text when that module isn't importable
        (e.g. the state machine exercised standalone in a unit test).
        """
        status = getattr(result, "status", None)
        status_name = getattr(status, "value", None) or str(status)
        detail = (getattr(result, "message", "") or "").strip()
        output = (getattr(result, "stdout", "") or "") + "\n" + (getattr(result, "stderr", "") or "")
        highlights = [ln.strip() for ln in output.splitlines()
                      if "MISSING DEPENDENCY" in ln or "Error" in ln or "error" in ln]
        reason = detail or (highlights[0] if highlights else f"execution failed ({status_name})")
        message = f"the generated run did not succeed ({status_name}): {reason}"
        hint = (f"Inspect the script and dependencies at {bundle_dir} (main.py, "
                f"requirements.txt). {self._how_to_run(bundle_dir)}")
        try:
            from error_handler import ConfigError
            return ConfigError(message, hint=hint)
        except Exception:  # noqa: BLE001 - standalone use: plain error with the text
            return RuntimeError(f"{message} -- {hint}")

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