"""Error classification, researcher-facing messaging, and fallbacks (Story 5.3).

When an agent fails, the orchestrator routes the exception through here to turn a
raw traceback into something a researcher can act on:

* **Classify** it into one of ``code | config | resource | timeout | policy |
  llm`` (plus ``unknown``).
* Produce an **actionable message** -- what went wrong and what to do next.
* Suggest a **fallback strategy** (e.g. if discovery fails, ask the researcher to
  name a tool manually) per the error-recovery design
  (``docs/architecture/05_error_recovery.drawio``).

Callers can raise the typed exceptions below to declare intent; anything else is
classified heuristically by type. Classification here is orthogonal to the
transient/permanent split in ``retry_policy`` (which decides *whether to retry*);
this decides *how to explain a failure that has already exhausted retries*.
"""
import _bootstrap  # noqa: F401

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class ErrorCategory(str, Enum):
    CODE = "code"          # bug in an agent/module: Type/Key/Attr/Import/Syntax
    CONFIG = "config"      # bad input / missing file / schema-contract violation
    RESOURCE = "resource"  # OOM, disk, network/connection, capacity
    TIMEOUT = "timeout"    # a stage exceeded its time budget
    POLICY = "policy"      # blocked by governance: license / budget / allow-list
    LLM = "llm"            # model-provider failure: auth, quota, rate limit
    UNKNOWN = "unknown"


# --------------------------------------------------------------------- exceptions
class OrchestratorError(Exception):
    """Base for errors that carry their own category + actionable hint."""

    category: ErrorCategory = ErrorCategory.UNKNOWN

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class CodeError(OrchestratorError):
    category = ErrorCategory.CODE


class ConfigError(OrchestratorError):
    category = ErrorCategory.CONFIG


class ResourceError(OrchestratorError):
    category = ErrorCategory.RESOURCE


class PolicyError(OrchestratorError):
    category = ErrorCategory.POLICY


class LLMError(OrchestratorError):
    category = ErrorCategory.LLM


class AgentTimeout(OrchestratorError, TimeoutError):
    """Raised by the agent runner when a stage exceeds its timeout."""

    category = ErrorCategory.TIMEOUT


@dataclass
class ClassifiedError:
    """A failure rendered into researcher-actionable form."""

    category: ErrorCategory
    message: str          # what went wrong, in plain language
    hint: str             # what the researcher should do next
    fallback: str         # the suggested recovery strategy
    recoverable: bool     # True if resuming (after a fix/wait) can plausibly help
    exception: str        # repr() of the original exception, for the audit log

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "message": self.message,
            "hint": self.hint,
            "fallback": self.fallback,
            "recoverable": self.recoverable,
            "exception": self.exception,
        }


# Categories from which resuming (after the researcher acts, or after a wait) can
# plausibly succeed. CODE/CONFIG/POLICY need a human change first but are still
# resumable once fixed; we mark the transient-ish ones recoverable=True to steer
# the message ("you can resume") vs ("fix this first").
_RECOVERABLE = {ErrorCategory.TIMEOUT, ErrorCategory.RESOURCE, ErrorCategory.LLM}

# Generic, category-level fallback strategies.
_FALLBACKS = {
    ErrorCategory.CODE: "Looks like a defect in the agent/module — fix the code and re-run; this is not auto-retried.",
    ErrorCategory.CONFIG: "Correct the input or configuration (it failed its contract) and resume the session.",
    ErrorCategory.RESOURCE: "Reduce the resource request (memory/CPU) or free capacity, then resume.",
    ErrorCategory.TIMEOUT: "Raise the per-stage timeout or shrink the workload, then resume from this stage.",
    ErrorCategory.POLICY: "Blocked by policy — obtain an override or pick a compliant tool/plan, then resume.",
    ErrorCategory.LLM: "Model call failed — check credentials/quota or wait for rate limits to reset, then resume.",
    ErrorCategory.UNKNOWN: "Inspect the logged exception, address the cause, then resume the session.",
}

# Stage-specific fallbacks take precedence (keyed by the State name the failure
# happened in). DISCOVER is the canonical example from the acceptance criteria.
_STAGE_FALLBACKS = {
    "DISCOVER": "Automated tool discovery failed — specify a tool manually (set the plan's selected_method) and re-run.",
    "BUILD": "Code/config generation failed — provide a known-good RunBundle or simplify the plan, then resume.",
    "EXECUTE": "Execution failed — check the generated script/dependencies and resource limits, then resume.",
}


def _name_hits(exc: BaseException, *needles: str) -> bool:
    """True if the exception's class or module name contains any needle."""
    cls = type(exc)
    haystack = f"{getattr(cls, '__module__', '')}.{cls.__name__}".lower()
    return any(n in haystack for n in needles)


def classify(exc: BaseException, state: Optional[str] = None) -> ClassifiedError:
    """Classify ``exc`` (optionally raised in pipeline ``state``)."""
    # 1) Explicitly typed orchestrator errors win — they declared their category.
    if isinstance(exc, OrchestratorError):
        category = exc.category
        hint = exc.hint or _FALLBACKS[category]
    else:
        category = _heuristic_category(exc)
        hint = _FALLBACKS[category]

    fallback = _STAGE_FALLBACKS.get(state or "", _FALLBACKS[category])
    message = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
    if state:
        message = f"Stage {state} failed — {message}"

    return ClassifiedError(
        category=category,
        message=message,
        hint=hint,
        fallback=fallback,
        recoverable=category in _RECOVERABLE,
        exception=repr(exc),
    )


def _heuristic_category(exc: BaseException) -> ErrorCategory:
    # Order matters: most specific signals first.
    if isinstance(exc, TimeoutError) or _name_hits(exc, "timeout"):
        return ErrorCategory.TIMEOUT
    if _name_hits(exc, "budget", "policy", "license", "governance", "overbudget"):
        return ErrorCategory.POLICY
    if _name_hits(exc, "ratelimit", "anthropic", "openai", "completion", "llm"):
        return ErrorCategory.LLM
    # Config problems first: a missing file is a FileNotFoundError, which is an
    # OSError subclass, so this must precede the broad OSError -> resource rule.
    # jsonschema raises ValidationError; an unmet guard means an upstream stage
    # left the run in a state its successor's guard rejects (a contract issue).
    if isinstance(exc, FileNotFoundError) or _name_hits(exc, "validation", "schema", "guard"):
        return ErrorCategory.CONFIG
    if isinstance(exc, MemoryError) or isinstance(exc, (ConnectionError, OSError)):
        return ErrorCategory.RESOURCE
    if _name_hits(exc, "memory", "resource", "disk", "capacity", "connection"):
        return ErrorCategory.RESOURCE
    if isinstance(exc, (TypeError, KeyError, AttributeError, NameError,
                        IndexError, ImportError, SyntaxError)):
        return ErrorCategory.CODE
    return ErrorCategory.UNKNOWN


def format_for_researcher(error: ClassifiedError, session_id: str = "", state: str = "") -> str:
    """Render a classified error as a readable notification block."""
    lines = [
        "── TWAIN run error ─────────────────────────────────────────",
        f"  session : {session_id}" if session_id else None,
        f"  stage   : {state}" if state else None,
        f"  type    : {error.category.value}",
        f"  what    : {error.message}",
        f"  do next : {error.hint}",
        f"  fallback: {error.fallback}",
        f"  resumable: {'yes' if error.recoverable else 'needs a fix first'}",
        "────────────────────────────────────────────────────────────",
    ]
    return "\n".join(line for line in lines if line is not None)


def default_notifier(message: str) -> None:
    """Default researcher notification channel: stdout (interactive CLI)."""
    print(message)


def notify_researcher(
    error: ClassifiedError,
    session_id: str = "",
    state: str = "",
    notifier: Callable[[str], None] = default_notifier,
) -> str:
    """Send an actionable error notification; returns the message that was sent."""
    message = format_for_researcher(error, session_id=session_id, state=state)
    notifier(message)
    return message
