"""Generic agent invocation for the orchestrator (Story 5.3).

``run_agent(agent, input_spec) -> output_spec`` is the single choke point through
which every pipeline stage calls its agent module. It adds the three
cross-cutting concerns the acceptance criteria require:

* **Timeout** per agent (e.g. EXECUTE = 20 min, CLARIFY = 5 min). The agent runs
  in a worker thread and we wait at most ``timeout`` seconds; overruns raise
  :class:`error_handler.AgentTimeout`.
* **Validation** -- the returned ``output_spec`` is checked against an expected
  schema (a JSON Schema dict or a predicate). A contract violation is permanent.
* **Retry** -- *transient* failures (network, 5xx, rate limit) are retried with
  exponential backoff + jitter; *permanent* failures fail fast. The
  transient/permanent split reuses ``retry_policy.classify_error`` (Story 2.3).
  This module implements its own retry loop (rather than reusing
  ``RetryPolicy.execute``) so retries interleave with the per-agent timeout
  thread below; ``RetryPolicy``/``ResilientCaller`` are used at the coarser
  run-step level by the orchestrator.

An "agent" is anything callable as ``agent(input_spec)`` or an object exposing
``.run`` / ``.invoke``. This keeps real agent modules and test doubles
interchangeable.
"""
import _bootstrap  # noqa: F401

import threading
import time as _time
from random import Random
from typing import Any, Callable, Dict, Optional, Union

from error_handler import AgentTimeout, ConfigError
from retry_policy import classify_error, ErrorType

# Per-stage timeouts in seconds (acceptance criteria give CLARIFY=5m, EXECUTE=20m).
DEFAULT_TIMEOUTS: Dict[str, int] = {
    "INTAKE": 5 * 60,
    "CLARIFY": 5 * 60,
    "DECOMPOSE": 5 * 60,
    "DISCOVER": 10 * 60,
    "PLAN": 10 * 60,
    "BUILD": 10 * 60,
    # REPAIR may run several --smoke verifications (up to 5 min each) plus a few
    # LLM repair/review calls, so it gets a generous stage budget.
    "REPAIR": 20 * 60,
    "EXECUTE": 2 * 60 * 60,
    "INTERPRET": 10 * 60,
    "VALIDATE": 10 * 60,
    "CORRECT": 10 * 60,
    "REPLAN": 10 * 60,
    "ACCEPT": 5 * 60,
}
DEFAULT_TIMEOUT = 10 * 60

# A validator is either a JSON Schema (dict) or a predicate over the output.
Validator = Union[Dict, Callable[[Any], bool]]


def timeout_for(state_name: str) -> int:
    """Return the configured timeout (seconds) for a pipeline state."""
    return DEFAULT_TIMEOUTS.get(state_name, DEFAULT_TIMEOUT)


def _as_callable(agent) -> Callable[[Dict], Any]:
    """Normalize an agent to ``f(input_spec) -> output_spec``."""
    if callable(agent):
        return agent
    for attr in ("run", "invoke", "__call__"):
        fn = getattr(agent, attr, None)
        if callable(fn):
            return fn
    raise ConfigError(
        f"agent {agent!r} is not invocable",
        hint="An agent must be callable or expose a .run(input_spec) / .invoke(input_spec) method.",
    )


def _validate_output(output: Any, validator: Optional[Validator], state_name: str) -> None:
    """Raise if ``output`` fails ``validator``. Contract failures are permanent."""
    if validator is None:
        return
    if isinstance(validator, dict):
        # Lazy import: jsonschema is only needed when a schema is supplied.
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import ValidationError
        try:
            Draft202012Validator(validator).validate(output)
        except ValidationError as exc:  # surface a clean, actionable message
            raise ConfigError(
                f"output of stage {state_name} did not match its schema: {exc.message}",
                hint="The agent returned an artifact that violates its output contract.",
            ) from exc
        return
    # Predicate form.
    if not validator(output):
        raise ConfigError(
            f"output of stage {state_name} failed validation",
            hint="The agent's output predicate returned False.",
        )


def _run_with_timeout(fn: Callable[[Dict], Any], input_spec: Dict,
                      timeout: Optional[float], state_name: str) -> Any:
    """Invoke ``fn(input_spec)`` under a wall-clock ``timeout`` (None = no limit).

    The work runs in a *daemon* worker thread that we ``join`` for at most
    ``timeout`` seconds. Python cannot forcibly kill a thread, so a genuinely hung
    stage keeps running in the background -- but as a daemon it never blocks
    interpreter exit, and :class:`AgentTimeout` is surfaced *promptly* once the
    join elapses. (A ``ThreadPoolExecutor`` context manager would instead block on
    ``shutdown(wait=True)`` at ``__exit__`` until the worker finished, so the
    timeout only fired after the work completed -- defeating the point.)
    Heavyweight stages (EXECUTE) still delegate real process termination to the
    execution adapter's subprocess timeout.
    """
    if timeout is None:
        return fn(input_spec)

    box: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["result"] = fn(input_spec)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller thread
            box["error"] = exc

    worker = threading.Thread(
        target=_worker, name=f"agent-{state_name or 'stage'}", daemon=True
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AgentTimeout(
            f"stage {state_name} exceeded its {timeout:.0f}s timeout",
            hint="Increase the stage timeout or reduce the workload, then resume.",
        )
    if "error" in box:
        raise box["error"]
    return box.get("result")


def run_agent(
    agent,
    input_spec: Dict,
    *,
    state_name: str = "",
    timeout: Optional[float] = None,
    validator: Optional[Validator] = None,
    max_retries: int = 3,
    base_delay: float = 1.0,
    jitter: float = 0.1,
    sleep: Callable[[float], None] = _time.sleep,
    rng: Optional[Random] = None,
) -> Any:
    """Invoke ``agent`` on ``input_spec`` with timeout, validation, and retries.

    Returns the validated ``output_spec``. Raises the original exception once
    retries are exhausted or immediately for a permanent error; raises
    :class:`AgentTimeout` if the agent overruns ``timeout``; raises
    :class:`ConfigError` if the output fails ``validator``.
    """
    fn = _as_callable(agent)
    rng = rng or Random(0)  # seeded: jitter is reproducible for deterministic tests
    attempt = 0
    while True:
        try:
            output = _run_with_timeout(fn, input_spec, timeout, state_name)
            _validate_output(output, validator, state_name)
            return output
        except AgentTimeout:
            # Timeouts are not retried here -- a 20-minute overrun should not be
            # silently attempted again; the orchestrator decides what to do.
            raise
        except ConfigError:
            raise  # output-contract violations are permanent by construction
        except Exception as exc:
            if classify_error(exc) == ErrorType.PERMANENT:
                raise  # fail fast: no point retrying a bad request / auth error
            attempt += 1
            if attempt > max_retries:
                raise  # transient, but we've exhausted the budget
            delay = base_delay * (2 ** (attempt - 1))
            delay *= 1 + rng.uniform(-jitter, jitter)
            sleep(max(0.0, delay))
