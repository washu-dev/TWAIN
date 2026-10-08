"""Triage a failed calculation: who can fix it, and with what action (#186).

The self-heal loop in EXECUTE used to act on exactly one thing -- a Python
traceback inside main.py -- and stop on anything else. Triage reads the job's
evidence and decides who can fix the failure:

* **operator**: a TWAIN/RIS setup problem (twain.sh, the bundle download, an
  output upload, a stale checkout). The researcher's script is not at fault and
  no retry can help: stop at once, with the exact fix -- spending budget on it
  only delays the person who can act.
* **script**: the generated code is wrong (a traceback in main.py, the structure
  guard's mismatch, a smoke-run error message): patch main.py against the
  evidence. Affects only this run.
* **environment**: a package is missing. If pip can install it, add it to this
  run's requirements (the job layers a pip venv on the env: this run only). If
  it is conda-only, the fix is a change to a SHARED environment -- never made
  automatically (that is #187's approval flow): stop and say what's needed.
* **resources**: out of time or memory. Raising them beyond what was approved
  needs the researcher: stop and say so.

Deterministic rules decide everything they can recognise. Only an unrecognised
failure goes to the LLM, which must pick from the same fixed menu (it can't
invent an action). Every diagnosis carries a signature, so the loop can stop
when the same failure comes back.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass

OPERATOR, SCRIPT, ENVIRONMENT, RESOURCES, UNKNOWN = (
    "operator", "script", "environment", "resources", "unknown")
PATCH, ADD_REQUIREMENT, STOP = "patch_script", "add_requirement", "stop"

_TRACEBACK = "Traceback (most recent call last):"
_MISSING = re.compile(r"MISSING DEPENDENCY:\s*([A-Za-z0-9_.\-]+)")
_NO_MODULE = re.compile(r"No module named '([A-Za-z0-9_.\-]+)'")
#: GPAW under MPI prefixes every line ("rank=4 L01: ..."); strip it before
#: reading a traceback, or every failure's "exception" is the same File line.
_MPI_RANK_PREFIX = re.compile(r"^rank=\d+\s+L\d+:\s?", re.MULTILINE)
_OOM = ("out_of_memory", "oom-kill", "oom_kill", "killed process", "memoryerror")


@dataclass
class Evidence:
    status: str
    exit_code: int | None
    exception: str | None
    output_tail: str
    in_main: bool              # a traceback with a main.py frame


@dataclass
class Diagnosis:
    cls: str
    action: str
    reason: str
    detail: str = ""           # what the fixer needs (the failure text, a package)
    signature: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def evidence(result) -> Evidence:
    """The facts triage judges, from an ExecutionResult (or its dict)."""
    get = (lambda k: result.get(k)) if isinstance(result, dict) else (lambda k: getattr(result, k, None))
    status = get("status")
    status = getattr(status, "value", status) or ""
    output = "\n".join(x for x in (get("stdout"), get("stderr")) if isinstance(x, str))
    output = _MPI_RANK_PREFIX.sub("", output)
    exception = None
    in_main = False
    if _TRACEBACK in output:
        block = output.rsplit(_TRACEBACK, 1)[1]
        in_main = "main.py" in block
        tail = [ln for ln in block.splitlines() if ln.strip() and not ln.startswith((" ", "\t"))]
        exception = tail[0].strip()[:500] if tail else None
    code = get("exit_code")
    return Evidence(str(status), int(code) if isinstance(code, int) else None, exception,
                    "\n".join(output.strip().splitlines()[-60:]), in_main)


def signature(ev: Evidence) -> str:
    """A failure's identity, ignoring numbers, paths and addresses."""
    basis = ev.exception or ev.output_tail[-400:] or ev.status
    basis = re.sub(r"(/[\w.\-]+)+", "/<path>", basis.lower())
    basis = re.sub(r"0x[0-9a-f]+|\d+(\.\d+)?", "#", basis)
    return hashlib.sha256(f"{ev.status}|{basis}".encode()).hexdigest()[:16]


def _missing_module(ev: Evidence) -> str | None:
    for pattern in (_MISSING, _NO_MODULE):
        m = pattern.search(ev.output_tail)
        if m:
            return m.group(1)
    return None


def diagnose(ev: Evidence, *, pip_gettable: Callable[[str], bool],
             agent: Callable[[str], str] | None = None) -> Diagnosis:
    sig = signature(ev)

    def d(cls, action, reason, detail=""):
        return Diagnosis(cls, action, reason, detail, sig)

    if ev.status == "setup_failed" or ev.exit_code in (3, 4, 6, 7):
        return d(OPERATOR, STOP, "a TWAIN/RIS setup problem, not the script -- the job's "
                 "stderr names the fix; retrying can't help")
    if "TWAIN_STRUCTURE_MISMATCH" in ev.output_tail:
        line = next((ln for ln in ev.output_tail.splitlines() if "TWAIN_STRUCTURE_MISMATCH" in ln), "")
        return d(SCRIPT, PATCH, "the script built the wrong crystal structure",
                 line or ev.output_tail[-2000:])
    module = _missing_module(ev)
    if module or ev.status == "dependency_error":
        if module and pip_gettable(module):
            return d(ENVIRONMENT, ADD_REQUIREMENT,
                     f"'{module}' is missing; pip can install it for this run", module)
        return d(ENVIRONMENT, STOP,
                 f"'{module or 'a required package'}' is missing and pip can't install it on "
                 f"the cluster: a shared environment needs it (an approved change)", module or "")
    if ev.status == "timeout" or ev.exit_code == 124:
        return d(RESOURCES, STOP, "the job ran out of its approved wall time -- raise it on "
                 "the approval card and re-run (needs your approval)")
    if any(marker in ev.output_tail.lower() for marker in _OOM):
        return d(RESOURCES, STOP, "the job ran out of memory -- raise RAM on the approval card "
                 "and re-run (needs your approval)")
    if ev.in_main:
        return d(SCRIPT, PATCH, f"the script crashed: {ev.exception or 'see the traceback'}",
                 ev.output_tail[-3000:])
    if ev.exception or ev.output_tail.strip():
        if agent is not None:
            return _llm_diagnosis(ev, agent, sig)
        return d(SCRIPT, PATCH, f"the run failed: {ev.exception or ev.output_tail.splitlines()[-1]}",
                 ev.output_tail[-3000:])
    return d(UNKNOWN, STOP, "the job failed without saying why (no output to diagnose)")


_MENU = {"patch_script": (SCRIPT, PATCH), "stop_operator": (OPERATOR, STOP),
         "stop_shared_environment": (ENVIRONMENT, STOP), "stop_resources": (RESOURCES, STOP)}


def _llm_diagnosis(ev: Evidence, agent, sig: str) -> Diagnosis:
    prompt = (
        "A scientific calculation failed on an HPC cluster. Classify the failure and choose "
        "ONE action. Reply with JSON only: {\"action\": one of "
        f"{sorted(_MENU)}, \"reason\": one sentence}}.\n"
        "- patch_script: the generated Python script is at fault and editing it can fix it.\n"
        "- stop_operator: a platform/cluster setup problem the script cannot fix.\n"
        "- stop_shared_environment: a package or program the environment lacks.\n"
        "- stop_resources: time or memory ran out.\n\n"
        f"Status: {ev.status}; exit code: {ev.exit_code}\nLast output:\n{ev.output_tail[-3000:]}")
    try:
        raw = agent(prompt) or ""
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        cls, action = _MENU[data["action"]]
        reason = str(data.get("reason") or "").strip()[:300] or data["action"]
    except Exception:  # noqa: BLE001 - an unreadable answer stops rather than guesses
        return Diagnosis(UNKNOWN, STOP, "the failure couldn't be classified automatically",
                         "", sig)
    return Diagnosis(cls, action, reason, ev.output_tail[-3000:] if action == PATCH else "", sig)
