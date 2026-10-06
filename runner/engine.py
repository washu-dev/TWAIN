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

from runner.suspend import SuspendRun

# Guard inputs for stages whose handlers are still stubs (INTERPRET/VALIDATE) and
# for EXECUTE when execution is disabled (planning-only runs). ``plan_approved``
# is intentionally absent -- see the module docstring; it is set only by a real
# approval so execution truly requires one.
SEED_CONTEXT = {
    "execution_status": True,
    "validation_result": "accepted",
}


def _as_number(value):
    """A float, or None for anything that is not one.

    None is a legal acceptance target (the schema allows null), so an empty field
    must round-trip as "no bar" rather than collapsing to 0.0 -- which would be a
    bar, and a very strict one.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


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
        self, *, session_id, researcher_id, request, ask, sink, store,
        cancel=None, max_cost=None, job_event_wait=None,
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
        # Slurm submission (instead of running locally/in Docker) is a deployment
        # decision, not a per-run choice: TWAIN_EXECUTE_SLURM=1 routes EXECUTE to
        # the RIS cluster fleet-wide (the UI no longer offers a local option).
        # TWAIN_SLURM_CLUSTER names the configs/clusters/ profile (default
        # compute2). Requires reachable login nodes (VPN + SSH key) or running
        # on a login node itself (TWAIN_SLURM_HOST="").
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
            # Let the pipeline pause (rather than block or error) when the ask
            # bridge needs the user: DbAsk raises SuspendRun, orchestrator.run()
            # catches it, checkpoints PAUSED, and returns so the runner releases
            # the process. A ``resume`` job continues the run when the user replies.
            suspend_exc=SuspendRun,
            # Terminate button: True once the user asked to stop. The orchestrator
            # checks it between stages (raising RunCancelled) and the Slurm poll
            # loop checks it between squeue polls (scancelling the job).
            cancel_check=cancel,
            # RIS webhooks: (job_id, seconds) sleep between Slurm polls that
            # returns early when ris-api reports on that job.
            job_event_wait=job_event_wait,
        )

    def current_state_name(self, orch) -> str:
        """The pipeline state the orchestrator is parked in (e.g. 'CLARIFY', 'BUILD')."""
        return orch.sm.current_state.name

    def compute_target_of(self, orch) -> str:
        """``'slurm'`` or ``'local'`` for the orchestrator the runner built."""
        return "slurm" if getattr(orch.sm, "execute_slurm", False) else "local"

    def slurm_cluster_of(self, orch) -> str | None:
        return getattr(orch.sm, "slurm_cluster", None)

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

    def plan_is_approved(self, orch) -> bool:
        """Whether this run's plan already carries the researcher's approval.

        BUILD is re-entered for reasons that are not the approval gate -- a
        correction pass comes back through it with the same approved plan -- so
        the driver needs to tell "waiting on a human" from "already decided".
        """
        return bool(getattr(orch.sm.context, "plan_approved", False))

    def replan_with_feedback(self, orch, feedback: str) -> None:
        """Fold the researcher's rejection feedback into the run and rewind it.

        The feedback is appended to the intent's ``objective`` — the free-text
        field every downstream stage reads (discovery's property/lookup cues,
        the plan brief, and the LLM codegen prompt) — and the run rewinds to
        DISCOVER so tools, plan, and code are all regenerated with it in view.
        The updated intent artifact rides the normal checkpoint machinery
        (capture/rehydrate), so the revision survives process handoffs.
        """
        path = orch.sm.context.artifacts.get("intent_spec")
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                intent = json.load(f)
            objective = str(intent.get("objective") or "").rstrip()
            intent["objective"] = (
                f"{objective}\n\nREVISION REQUESTED — the researcher rejected "
                f"the previous plan with this feedback, which takes precedence "
                f"where it conflicts with the above: {feedback.strip()}"
            )
            with open(path, "w", encoding="utf-8") as f:
                json.dump(intent, f, indent=2)
        self.rewind(orch, "DISCOVER")

    def read_execution_plan(self, orch) -> dict | None:
        path = orch.sm.context.artifacts.get("execution_plan")
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return None

    def apply_slurm_overrides(self, orch, overrides: dict) -> None:
        """Patch ``slurm_request`` on the on-disk execution plan (plan-unit fields).

        ``overrides`` uses the plan contract: ``ram`` in GB, ``max_time`` in hours.
        Values are clamped to the same floors the synthesizer applies.
        """
        from plan_synthesizer.plan_synthesizer import MIN_RAM_GB, MIN_WALL_MINUTES

        path = orch.sm.context.artifacts.get("execution_plan")
        if not path or not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
        current = dict(plan.get("slurm_request") or {})
        if "cpu_count" in overrides:
            current["cpu_count"] = max(1, int(overrides["cpu_count"]))
        if "gpu_count" in overrides:
            current["gpu_count"] = max(0, int(overrides["gpu_count"]))
        if "ram" in overrides:
            current["ram"] = max(MIN_RAM_GB, int(overrides["ram"]))
        if "max_time" in overrides:
            # Plan stores hours; floor is MIN_WALL_MINUTES expressed in hours.
            hours = float(overrides["max_time"])
            current["max_time"] = max(MIN_WALL_MINUTES / 60.0, hours)
        plan["slurm_request"] = current
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2)

    def apply_acceptance_overrides(self, orch, metrics) -> None:
        """Patch ``acceptance_metrics`` on the on-disk plan with the researcher's.

        This is the bar the result is judged against at VALIDATE, and it is the one
        thing on the approval card only the researcher can supply: TWAIN often has
        no defensible target and writes ``target_value: null``, which leaves nothing
        to check the answer against. A silver band gap ran with a null target and
        was graded only against a literature baseline (run 913c1ee9).

        Matched by metric_name so a partial list edits one metric and leaves the
        rest; an unknown name is appended, because asking for a bar TWAIN did not
        propose is a legitimate request. Null target/tolerance are preserved --
        clearing a bar is as meaningful as setting one, and the plan schema allows
        both (schemas/execution_plan.schema.json).
        """
        if not isinstance(metrics, list) or not metrics:
            return
        path = orch.sm.context.artifacts.get("execution_plan")
        if not path or not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
        current = list(plan.get("acceptance_metrics") or [])
        by_name = {str(m.get("metric_name")): i for i, m in enumerate(current)
                   if isinstance(m, dict)}
        for edit in metrics:
            if not isinstance(edit, dict) or not edit.get("metric_name"):
                continue
            name = str(edit["metric_name"])
            patch = {"metric_name": name,
                     "target_value": _as_number(edit.get("target_value")),
                     "tolerance": _as_number(edit.get("tolerance"))}
            if name in by_name:
                current[by_name[name]] = {**current[by_name[name]], **patch}
            else:
                current.append(patch)
        plan["acceptance_metrics"] = current
        with open(path, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2)

    def _read_artifact(self, orch, name: str) -> dict | None:
        """A stage artifact's JSON, or None when it is absent or unreadable."""
        path = orch.sm.context.artifacts.get(name)
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else None
            except (OSError, ValueError):
                return None
        return None

    def failure_message(self, orch) -> str:
        """Where the run stopped and why, for the chat and the notification."""
        failure = getattr(orch, "last_failure", None)
        if not failure:
            return "The run stopped unexpectedly. Open the run to see what happened."
        # Importable on its own -- not only after _load() has wired sys.path,
        # which a fresh process (CI running one test) hasn't done yet.
        orchestrator_dir = str(pathlib.Path(__file__).resolve().parent.parent
                               / "modules" / "07_runtime_orchestrator")
        if orchestrator_dir not in sys.path:
            sys.path.insert(0, orchestrator_dir)
        from error_handler import failure_message
        return failure_message(failure)

    def decline_reason(self, orch) -> str | None:
        """The off-topic decline message when intake refused the request, or None.

        Intake writes a ``declined`` artifact (with the user-facing message) and
        ends the run without planning or executing anything; the runner posts
        this instead of the generic "run complete" summary and marks the
        conversation rejected.
        """
        return (self._read_artifact(orch, "declined") or {}).get("message")

    def final_summary(self, orch) -> str:
        """The completion message posted to the chat.

        A run that interpreted and validated a result says what it found and
        how the check went — the researcher shouldn't have to dig through the
        report artifacts to learn whether validation happened. Runs with
        nothing interpreted (planning-only, deferred) keep the plain line.
        """
        state = getattr(getattr(orch, "sm", None), "current_state", None)
        name = state.name if state is not None else "unknown"
        lines = [f"Run complete (final state: {name})."]
        normalized = self._read_artifact(orch, "normalized_result")
        metric = (normalized or {}).get("primary_metric") or {}
        if isinstance(metric.get("value"), (int, float)):
            unit = f" {metric['unit']}" if metric.get("unit") else ""
            unc = metric.get("uncertainty")
            spread = f" ± {unc:.2g}" if isinstance(unc, (int, float)) and unc else ""
            lines.append(f"Result: {metric.get('name')} = "
                         f"{metric['value']:.6g}{spread}{unit}")
        report = self._read_artifact(orch, "validation_report")
        if report:
            status = report.get("acceptance_status") or "unknown"
            rationale = str(report.get("rationale") or "").strip()
            loop = report.get("rerun") or {}
            flagged = ""
            if isinstance(loop, dict) and loop.get("decision") == "stop":
                reason = loop.get("stop_reason") or loop.get("reason") or "budget exhausted"
                flagged = (f" (correction loop stopped: {reason}; "
                           "delivered for your review)")
            lines.append(f"Validation: {status}{flagged}"
                         + (f" — {rationale}" if rationale else ""))
        elif normalized:
            lines.append("Validation: not performed — no reference was "
                         "available to check this result against.")
        lines.append("Open the report to see the full results.")
        return "\n".join(lines)


def default_engine() -> _RealEngine:
    """Construct the real engine adapter (imports the pipeline)."""
    return _RealEngine()
