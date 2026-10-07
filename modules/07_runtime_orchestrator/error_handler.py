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

import os
import re
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


def _http_status(exc: BaseException) -> Optional[int]:
    """Return the HTTP status code of a requests-style error, if it carries one.

    ``requests.exceptions.HTTPError`` (raised by ``response.raise_for_status()``)
    exposes the originating response on ``.response``; duck-typed here so we don't
    import requests just to classify.
    """
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def classify(exc: BaseException, state: Optional[str] = None) -> ClassifiedError:
    """Classify ``exc`` (optionally raised in pipeline ``state``)."""
    # 1) Explicitly typed orchestrator errors win — they declared their category.
    if isinstance(exc, OrchestratorError):
        category = exc.category
        hint = exc.hint or _FALLBACKS[category]
    else:
        # 2) HTTP failures from the model API carry a status code; classify by it
        #    so auth/quota issues don't get mistaken for a RESOURCE problem (an
        #    HTTPError is an OSError subclass and would otherwise fall through).
        status = _http_status(exc)
        if status in (401, 403):
            category = ErrorCategory.LLM
            hint = (
                f"Authorization failed (HTTP {status}). If you're calling the WUSTL AI API, "
                "connect to the campus network/VPN and verify API_KEY / CLIENT_ID / "
                "CLIENT_SECRET in .env, then resume."
            )
        elif status == 429:
            category = ErrorCategory.LLM
            hint = ("Rate limited (HTTP 429) by the model provider — wait for the limit "
                    "to reset, then resume.")
        elif status is not None and status >= 500:
            # Upstream model gateway fault (e.g. aiapi.wustl.edu 500). Must not
            # fall through to the OSError -> RESOURCE rule — HTTPError subclasses
            # OSError and would otherwise suggest reducing memory/CPU.
            category = ErrorCategory.LLM
            hint = (
                f"The model provider returned HTTP {status} (temporary server error). "
                "Wait a minute and resume; if it keeps failing, check "
                "https://aiapi.wustl.edu status or try again on campus VPN."
            )
        elif status is not None:
            category = ErrorCategory.LLM
            hint = (
                f"The model provider returned HTTP {status}. Check the request / "
                "API credentials, then resume."
            )
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
    """Render a classified error as a readable notification block.

    Includes the log file holding this run when the launcher named one in
    ``TWAIN_RUN_LOG``. On the cluster more than one process drives runs -- the
    always-on runner plus on-demand workers from scale_runners.sh -- so "check the
    runner log" is ambiguous and was actively misleading: a scaled worker's whole
    run went to its own file while readers were pointed at runner-ris.log, which
    never mentioned that run. Omitted entirely when unset (local CLI runs, where
    the block is printed to the terminal anyway).
    """
    run_log = os.environ.get("TWAIN_RUN_LOG", "").strip()
    lines = [
        "── TWAIN run error ─────────────────────────────────────────",
        f"  session : {session_id}" if session_id else None,
        f"  stage   : {state}" if state else None,
        f"  type    : {error.category.value}",
        f"  what    : {error.message}",
        f"  do next : {error.hint}",
        f"  fallback: {error.fallback}",
        f"  log     : {run_log}" if run_log else None,
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


# ── what the researcher sees when a run stops (#169) ─────────────────────────────

#: Plain-language headline per EXECUTE outcome (ExecutionStatus values).
_EXECUTION_HEADLINES = {
    "dependency_error": "The cluster has no environment that can run this plan",
    "setup_failed": "The job could not get set up on the cluster",
    "timeout": "The job ran out of time",
    "failed": "The calculation crashed on the cluster",
}

#: Stage names as a researcher reads them.
STAGE_LABELS = {
    "INTAKE": "Understanding the request", "CLARIFY": "Clarifying the request",
    "DECOMPOSE": "Breaking down the goal", "DISCOVER": "Choosing methods",
    "PLAN": "Planning", "BUILD": "Writing the simulation", "REPAIR": "Checking the script",
    "EXECUTE": "Running on the cluster", "INTERPRET": "Reading the results",
    "VALIDATE": "Validating the result", "ACCEPT": "Accepting the result",
}

#: Detail kept for the failure card (the tail: errors print last).
FAILURE_DETAIL_CHARS = 4000
#: The tail of a failed Slurm job's stderr carried on the failure card.
JOB_STDERR_LINES = 40
JOB_STDERR_CHARS = 4000

#: What to do about a setup failure, by a marker in its message. The job's own
#: stderr (on the card) says exactly what broke; these say whose move it is.
_SETUP_NEXT_STEPS = (
    ("RIS checkout is older",
     ("Update TWAIN's checkout on RIS -- the job's stderr below has the exact "
      "`git pull` command -- then re-run from EXECUTE.")),
    ("RIS configuration",
     ("Fix twain.sh on RIS as the job's stderr below describes (it must be "
      "readable by the job's account and export CODE_DIR), then re-run from EXECUTE.")),
    ("could not download its run bundle",
     ("The compute node could not fetch this run's files from TWAIN's storage. "
      "That is a TWAIN-side access problem, not your script: use Report to flag "
      "it, and re-run from EXECUTE once it is fixed.")),
)
_SETUP_NEXT_STEP_DEFAULT = ("This is a cluster setup problem, not your script -- "
                            "the job's stderr below says what failed.")

_PREFIX = re.compile(r"^Stage \w+ failed\s*[—-]\s*(?:\w+Error|\w+Exception|\w+):\s*")
_RUN_PREFIX = re.compile(r"^the generated run did not succeed \((\w+)\):\s*")


def describe_failure(classified: "ClassifiedError", state: str,
                     execution_result: Optional[dict] = None) -> dict:
    """A run's failure as the chat shows it: where, why, the detail, what next.

    ``classified.message`` is complete but written for logs ("Stage EXECUTE
    failed — ConfigError: the generated run did not succeed (dependency_error):
    no runnable environment ... ERROR: Could not find a version ..."). This keeps
    all of it as ``detail`` and leads with a one-line ``headline`` -- for an
    EXECUTE failure, from the job's outcome class -- so the submitter is never
    left with "see the run log" and no log to see.
    """
    text = (classified.message or "").strip()
    body = _PREFIX.sub("", text)
    outcome = None
    match = _RUN_PREFIX.match(body)
    if match:
        outcome, body = match.group(1), body[match.end():]
    if isinstance(execution_result, dict) and execution_result.get("status"):
        outcome = outcome or str(execution_result["status"])
    first = body.splitlines()[0] if body else ""
    # The cause before its elaboration: "no runnable environment ... on compute2"
    # rather than the full ": every pre-provisioned env failed ..." sentence.
    cause = re.split(r":\s(?=[a-z])", first, maxsplit=1)[0].strip().rstrip(".")
    headline = _EXECUTION_HEADLINES.get(outcome or "") or (cause[:1].upper() + cause[1:]) \
        or "The run stopped unexpectedly"
    install_log = (execution_result or {}).get("install_log") or {}
    detail = body if len(body) <= FAILURE_DETAIL_CHARS else "…" + body[-FAILURE_DETAIL_CHARS:]
    next_step = classified.hint or classified.fallback
    if outcome == "setup_failed":
        # The generic EXECUTE hint ("inspect the script at <container path>")
        # is wrong here: the script never ran.
        next_step = next((step for marker, step in _SETUP_NEXT_STEPS if marker in body),
                         _SETUP_NEXT_STEP_DEFAULT)
    return {
        "stage": state,
        "stage_label": STAGE_LABELS.get(state, state.title()),
        "headline": headline,
        "cause": cause if cause and cause.lower() != headline.lower() else None,
        "detail": detail,
        "next_step": next_step,
        "category": classified.category.value,
        "recoverable": classified.recoverable,
        "outcome": outcome,
        "job_id": install_log.get("job_id"),
        "job_stderr": _stderr_tail((execution_result or {}).get("stderr")),
    }


def _stderr_tail(stderr) -> Optional[str]:
    """The last lines of a job's stderr -- where the wrapper and tracebacks say why."""
    text = (stderr or "").strip() if isinstance(stderr, str) else ""
    if not text:
        return None
    tail = "\n".join(text.splitlines()[-JOB_STDERR_LINES:])
    return tail if len(tail) <= JOB_STDERR_CHARS else "…" + tail[-JOB_STDERR_CHARS:]


def failure_message(failure: dict) -> str:
    """The one chat/email line for a stopped run."""
    # The cause and full detail live on the failure card; one line here.
    parts = [f"The run stopped while {failure['stage_label'].lower()} "
             f"({failure['stage']}): {failure['headline']}."]
    if failure.get("next_step"):
        parts.append(f"Next step: {failure['next_step']}")
    return " ".join(parts)
