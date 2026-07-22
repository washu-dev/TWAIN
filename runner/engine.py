"""Adapter over the TWAIN pipeline engine.

Isolates every import of the ``modules/`` engine behind :func:`default_engine`
so the rest of the runner (and its unit tests) can be imported without the heavy
pixi environment. The engine is wired exactly as the orchestrator expects:
``request`` + ``agent`` (the live WashU LLM) + ``ask`` (chat bridge) + ``store``
(:class:`PgStore`) + an event sink.

The guard inputs that the stub INTERPRET/VALIDATE handlers don't set themselves
are seeded so a happy-path run can reach TERMINATE. ``plan_approved`` is
deliberately NOT seeded: it is the approval gate, so it stays False until the
researcher actually approves (the runner calls :meth:`engine.approve_plan` after
a real approval). That makes the guarded ``BUILD->REPAIR`` / ``REPAIR->EXECUTE``
transitions a genuine safety net -- a run cannot build or execute without an
explicit approval, not merely because the runner happens to block for one.
"""
import json
import os
import pathlib
import sys

# Guard inputs for stages whose handlers are still stubs (INTERPRET/VALIDATE) and
# for EXECUTE when execution is disabled (planning-only runs). ``plan_approved``
# is intentionally absent -- see the module docstring; it is set only by a real
# approval so execution truly requires one.
SEED_CONTEXT = {
    "execution_status": True,
    "validation_result": "accepted",
}


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var (1/true/yes/on -> True); ``default`` when unset."""
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    """Read a float env var; ``default`` when unset or unparseable."""
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int env var; ``default`` when unset or unparseable."""
    v = os.environ.get(name)
    if v is None or not v.strip():
        return default
    try:
        return int(v)
    except ValueError:
        return default


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

    def build_orchestrator(
        self, *, session_id, researcher_id, request, ask, sink, store, max_cost=None,
    ):
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
        # TWAIN_EXECUTE_SLURM=1 submits the built bundle to the Slurm cluster
        # (TWAIN_SLURM_CLUSTER names the configs/clusters/ profile; default
        # compute2) instead of running it locally/in Docker. Requires reachable
        # login nodes (VPN + SSH key) or running on a login node itself
        # (TWAIN_SLURM_HOST="").
        slurm = _env_flag("TWAIN_EXECUTE_SLURM")
        execute = auto or slurm or _env_flag("TWAIN_EXECUTE_LOCALLY")
        # Budget caps: a per-run ``max_cost`` (from the user) overrides the
        # deployment default (TWAIN_RUN_MAX_COST); iteration/wall-time rails are
        # deployment-wide. Without this wiring the orchestrator silently fell back
        # to its own $1.00 / 50-iter / 30-min defaults on every run.
        run_max_cost = max_cost if max_cost is not None else _env_float("TWAIN_RUN_MAX_COST", 1.0)
        return self._Orchestrator(
            session_id=session_id,
            researcher_id=researcher_id,
            request=request,
            agent=self._AgentInterface(),
            ask=ask,
            event_bus=sink,
            store=store,
            context=dict(SEED_CONTEXT),
            run_max_cost=run_max_cost,
            run_max_iterations=_env_int("TWAIN_RUN_MAX_ITERATIONS", 50),
            run_wall_time_minutes=_env_int("TWAIN_RUN_WALL_MINUTES", 30),
            provenance=False,  # run_events is the durable trail; skip local JSONL
            execute_locally=execute and not slurm,
            execute_slurm=slurm,
            slurm_cluster=os.environ.get("TWAIN_SLURM_CLUSTER"),
            # Verify + repair generated code (compile/smoke/review) before the real
            # run so API errors are caught; default on whenever we execute.
            verify_codegen=_env_flag("TWAIN_VERIFY_CODEGEN", default=execute),
            auto_approve=auto,
        )

    def rewind(self, orch, target_state: str) -> None:
        """Rewind a resumed orchestrator to ``target_state`` so it re-runs from there.

        ``target_state`` is a pipeline state name (e.g. ``"CLARIFY"``). The guard
        seed is re-applied (see ``SEED_CONTEXT``) so the stubbed happy path still
        flows past the INTERPRET/VALIDATE guards after the rewind, exactly as a
        fresh run does; the human gates (plan approval, heavy-calc confirmation)
        are re-enforced structurally by the runner and the EXECUTE stage.
        """
        try:
            target = self._State[target_state]
        except KeyError as exc:
            raise ValueError(f"unknown rewind target state: {target_state!r}") from exc
        orch.rewind_to(target, reseed=dict(SEED_CONTEXT))

    def approve_plan(self, orch) -> None:
        """Record a real plan approval so the run may proceed past the BUILD gate."""
        orch.approve_plan()

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
