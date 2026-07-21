"""Adapter over the TWAIN pipeline engine.

Isolates every import of the ``modules/`` engine behind :func:`default_engine`
so the rest of the runner (and its unit tests) can be imported without the heavy
pixi environment. The engine is wired exactly as the orchestrator expects:
``request`` + ``agent`` (the live WashU LLM) + ``ask`` (chat bridge) + ``store``
(:class:`PgStore`) + an event sink.

The guard inputs that the stub INTERPRET/VALIDATE/EXECUTE handlers don't set
themselves are seeded so a happy-path run can reach TERMINATE; ``plan_approved``
is seeded too, but the runner still pauses at BUILD and only continues on real
user approval — so nothing is built or executed without it.
"""
import json
import os
import pathlib
import sys

from runner.suspend import SuspendRun

SEED_CONTEXT = {
    "plan_approved": True,
    "execution_status": True,
    "validation_result": "accepted",
}


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var (1/true/yes/on -> True); ``default`` when unset."""
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _load():
    """Wire sys.path and import the engine (mirrors the orchestrator bootstrap)."""
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root / "modules" / "07_runtime_orchestrator"))
    # _bootstrap must import first: it wires the numbered module dirs onto
    # sys.path so the bare sibling imports below resolve. Do not reorder.
    import _bootstrap  # noqa: F401, I001

    from AgentInterface import AgentInterface  # noqa: I001
    from orchestrator import Orchestrator
    from states import State

    return Orchestrator, State, AgentInterface


class _RealEngine:
    """Builds and reads real ``Orchestrator`` instances."""

    def __init__(self):
        self._Orchestrator, self._State, self._AgentInterface = _load()
        self.STATE_BUILD = self._State.BUILD

    def build_orchestrator(self, *, session_id, researcher_id, request, ask, sink, store):
        # Real execution is env-gated so the SAME image works everywhere: set
        # TWAIN_EXECUTE_LOCALLY=1 (local `docker run -e ...` or the ECS task
        # definition) to actually run the generated calculation at EXECUTE -- e.g.
        # a real GPAW DFT band gap on the linux-64 runner, where the sim env ships
        # GPAW. Off by default keeps the pipeline planning-only (safe where running
        # LLM-generated code isn't wanted). When on, EXECUTE sets execution_status
        # from the real run, overriding the seed; the heavy-calc gate asks for
        # confirmation through the same chat `ask` bridge used at CLARIFY.
        # TWAIN_AUTO_RUN=1 = fully unattended: execute for real AND skip the human
        # confirmation gates (the heavy-calc prompt here; the plan-approval gate in
        # runner._drive_run). TWAIN_EXECUTE_LOCALLY=1 executes but still asks.
        auto = _env_flag("TWAIN_AUTO_RUN")
        execute = auto or _env_flag("TWAIN_EXECUTE_LOCALLY")
        return self._Orchestrator(
            session_id=session_id,
            researcher_id=researcher_id,
            request=request,
            agent=self._AgentInterface(),
            ask=ask,
            event_bus=sink,
            store=store,
            context=dict(SEED_CONTEXT),
            provenance=False,  # run_events is the durable trail; skip local JSONL
            execute_locally=execute,
            # Verify + repair generated code (compile/smoke/review) before the real
            # run so API errors are caught; default on whenever we execute.
            verify_codegen=_env_flag("TWAIN_VERIFY_CODEGEN", default=execute),
            auto_approve=auto,
            # Let the pipeline pause (rather than block or error) when the ask
            # bridge needs the user: DbAsk raises SuspendRun, orchestrator.run()
            # catches it, checkpoints PAUSED, and returns so the runner releases
            # the process. A ``resume`` job continues the run when the user replies.
            suspend_exc=SuspendRun,
        )

    def current_state_name(self, orch) -> str:
        """The pipeline state the orchestrator is parked in (e.g. 'CLARIFY', 'BUILD')."""
        return orch.sm.current_state.name

    def read_execution_plan(self, orch) -> dict | None:
        path = orch.sm.context.artifacts.get("execution_plan")
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return None

    def final_summary(self, orch) -> str:
        state = getattr(getattr(orch, "sm", None), "current_state", None)
        name = state.name if state is not None else "unknown"
        return f"Run complete (final state: {name}). Open the report to see the results."


def default_engine() -> _RealEngine:
    """Construct the real engine adapter (imports the pipeline)."""
    return _RealEngine()
