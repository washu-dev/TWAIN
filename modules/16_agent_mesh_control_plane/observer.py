"""The observer: deterministic gates between stages (#185, P1b).

A separate checker that looks at what a stage produced *before* the next stage
starts, independent of the agent that produced it (the producer doesn't grade
its own work). Called synchronously at three transitions, because a gate must
be able to stop the next stage -- an asynchronous subscriber can't:

* **PLAN -> BUILD**: the toolset fits an environment RIS actually has; something
  will verify the result (Materials Project for a crystal, the ESOL table for a
  molecule, or an acceptance target) -- if nothing will, the plan card says so
  before the researcher approves, instead of "not verified" after the run.
* **REPAIR -> EXECUTE**: the bundle can produce what was asked: a fixed
  template's required input file is present; a fixed template emits the
  requested metric; a crystal bundle carries the structure guard. Stopping here
  costs seconds; the same failure on RIS cost a queue wait and an allocation
  (run 44ad9f9e: a descriptor template, chosen for solubility, needing a
  molecules.csv nobody supplied).
* **EXECUTE -> INTERPRET**: the job left outputs to interpret.

Each check is ``pass`` / ``warn`` / ``fail``. A ``fail`` stops the run with the
check's reason (the failure card shows it); a ``warn`` is recorded and shown.
Every verdict is published as a subtask line, so the tracker shows the checks.

**LLM judgement (#188)** adds what fixed rules can't decide. It is a reviewer,
not the author: its own prompt, shown only the researcher's request and the
stage's output, never the conversation that produced them.

* before EXECUTE: "does this script compute the requested property for the
  requested system?" A clear *no* stops the run (nothing has been submitted;
  re-running is cheap).
* before INTERPRET: "are these outputs physically sensible?" A *no* is a
  warning recorded with the result, never a stop: the calculation is done, and
  validation and the researcher make the call.

``TWAIN_OBSERVER_LLM``: ``enforce`` (default), ``warn`` (a script *no* only
warns), or ``0`` (off). An unclear or unreadable answer adds nothing.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class Verdict:
    gate: str
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name, status, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")


# ------------------------------------------------------------------ PLAN -> BUILD
def _esol_molecules(repo_root: Path) -> set:
    try:
        data = json.loads((repo_root / "configs" / "baselines.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    items = data.get("baselines") or data.get("entries") or []
    return {_norm(i.get("molecule")) for i in items if isinstance(i, dict)}


def _reference_source(plan: dict, intent: dict | None, repo_root: Path,
                      periodic: bool) -> str | None:
    """What will check the result, in words -- or None if nothing will."""
    for metric in plan.get("acceptance_metrics") or []:
        if isinstance(metric, dict) and metric.get("target_value") is not None:
            return (f"your acceptance target ({metric.get('metric_name')} = "
                    f"{metric.get('target_value')} ± {metric.get('tolerance')})")
    if periodic and os.environ.get("MP_API_KEY"):
        return "Materials Project's reference value for this crystal"
    sysd = plan.get("target_system") or (intent or {}).get("system_descriptors") or {}
    molecule = (sysd.get("molecule") or {}) if isinstance(sysd, dict) else {}
    name = molecule.get("name") or (sysd.get("name") if isinstance(sysd, dict) else None)
    # A solubility plan has no canonical property: its acceptance metric names it.
    prop = _norm(" ".join([str(plan.get("requested_property") or "")] + [
        str(m.get("metric_name") or "") for m in plan.get("acceptance_metrics") or []
        if isinstance(m, dict)]))
    if name and _norm(name) in _esol_molecules(repo_root) and ("solub" in prop or "logs" in prop):
        return "the ESOL literature value (Delaney 2004) -- if the result is reported as logS"
    return None


def plan_gate(plan: dict, intent: dict | None, *, repo_root: Path, periodic: bool,
              env_candidates: list | None, execute_slurm: bool) -> Verdict:
    v = Verdict("plan")
    if execute_slurm:
        if env_candidates is None:
            v.add("environment", FAIL, "no RIS environment can run this toolset")
        elif env_candidates:
            v.add("environment", PASS, f"runs in the {env_candidates[0]} environment on RIS")
    source = _reference_source(plan, intent, repo_root, periodic)
    if source:
        v.add("reference", PASS, f"the result will be checked against {source}")
    else:
        v.add("reference", WARN,
              "nothing will verify this result: no literature value covers it and the plan "
              "has no acceptance target. Set a target (value and tolerance) on this card, "
              "or the result will be reported as not verified")
    return v


# --------------------------------------------------------------- REPAIR -> EXECUTE
_INPUT_DEFAULT = re.compile(r'''add_argument\(\s*["']--input["'][^)]*default\s*=\s*([A-Z_]+|["'][^"']+["'])''')


def _required_input(main_py: str) -> str | None:
    """The input file a template insists on (its --input default), if any."""
    m = _INPUT_DEFAULT.search(main_py)
    if not m:
        return None
    value = m.group(1)
    if value.startswith(("'", '"')):
        return value.strip("'\"")
    const = re.search(rf'^{value}\s*=\s*["\']([^"\']+)["\']', main_py, re.MULTILINE)
    return const.group(1) if const else None


def bundle_gate(plan: dict, bundle_dir: Path, *, template: str | None,
                periodic: bool) -> Verdict:
    v = Verdict("bundle")
    main = bundle_dir / "main.py"
    text = main.read_text(encoding="utf-8") if main.is_file() else ""
    if not text:
        v.add("script", FAIL, "the bundle has no main.py")
        return v
    synthesized = template in (None, "llm_synthesized")
    needed = _required_input(text)
    if needed and not (bundle_dir / needed).exists() and "refuses to fabricate" in text:
        v.add("input", FAIL,
              f"the script needs an input file ({needed}) that the bundle doesn't contain -- "
              f"it would stop on the cluster without computing anything")
    wanted = [m.get("metric_name") for m in plan.get("acceptance_metrics") or []
              if isinstance(m, dict) and m.get("metric_name")] or [plan.get("requested_property")]
    wanted = [w for w in wanted if w]
    if wanted and not synthesized:
        # The template embeds the plan's own acceptance/config JSON, which names
        # the metric whether or not the template computes it: judge the code only.
        code = "\n".join(ln for ln in text.splitlines()
                         if not re.match(r"\s*_(ACCEPTANCE|CONFIG)_JSON\s*=", ln))
        body = _norm(code)
        tokens = [t for w in wanted for t in _norm(w).split("_") if len(t) > 3]
        if tokens and not any(t in body for t in tokens):
            v.add("metric", FAIL,
                  f"the '{template}' template doesn't produce {', '.join(wanted)}: "
                  f"it computes something else, so the run could only end unverified")
    if periodic:
        if (bundle_dir / "twain_expected_structure.json").is_file():
            v.add("structure", PASS, "the structure guard will check the crystal before computing")
        else:
            v.add("structure", WARN, "no structure guard in this crystal bundle")
    if not v.failed:
        v.add("script", PASS, "the bundle can produce what was asked")
    return v


# --------------------------------------------------------------- EXECUTE -> INTERPRET
def outputs_gate(execution_result: dict | None) -> Verdict:
    v = Verdict("outputs")
    result = execution_result or {}
    if result.get("status") in ("deferred", "skipped") or result.get("succeeded") is None:
        return v                                    # nothing ran: INTERPRET no-ops
    outputs = result.get("artifacts_dir")
    files = [p for p in Path(outputs).rglob("*") if p.is_file()] if outputs and Path(outputs).is_dir() else []
    stdout = (result.get("stdout") or "").strip()
    if not files and not stdout:
        v.add("outputs", FAIL, "the job finished without any output files or printed results")
    else:
        v.add("outputs", PASS, f"{len(files)} output file(s) to interpret")
    return v


# --------------------------------------------------------------- LLM judgement (#188)
YES, NO, UNSURE = "yes", "no", "unsure"
_REVIEWER = (
    "You are an independent reviewer of a computational-chemistry pipeline. You did not "
    "write what you are shown, and you judge only what is in front of you. Reply with JSON "
    'only: {"verdict": "yes" | "no" | "unsure", "reason": one sentence}. Say "no" only '
    "when you are confident; when in doubt, say \"unsure\".\n\n")


def llm_mode() -> str:
    mode = os.environ.get("TWAIN_OBSERVER_LLM", "enforce").strip().lower()
    return "off" if mode in ("0", "off", "false", "no") else ("warn" if mode == "warn" else "enforce")


def _ask(agent, prompt: str) -> tuple[str, str] | None:
    """(verdict, reason) from the reviewer, or None if it gave no usable answer."""
    try:
        raw = agent(_REVIEWER + prompt) or ""
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        verdict = str(data.get("verdict") or "").strip().lower()
        reason = " ".join(str(data.get("reason") or "").split())[:300]
    except Exception:  # noqa: BLE001 - an unreadable review adds nothing
        return None
    return (verdict, reason or verdict) if verdict in (YES, NO, UNSURE) else None


def _task(request: str | None, plan: dict) -> str:
    system = plan.get("target_system") or {}
    return (f"The researcher asked: {(request or '').strip()[:1500] or '(not recorded)'}\n"
            f"Requested property: {plan.get('requested_property') or '(unspecified)'}\n"
            f"System: {json.dumps(system, default=str)[:800] if system else '(unspecified)'}\n")


def llm_script_check(v: Verdict, agent, *, request: str | None, plan: dict,
                     main_py: str) -> None:
    """Add the reviewer's "does this script compute what was asked?" to ``v``."""
    mode = llm_mode()
    if agent is None or mode == "off" or not main_py.strip():
        return
    answer = _ask(agent, _task(request, plan) + (
        "\nDoes the script below compute the requested property for the requested system "
        "(not a different property, not a different or placeholder system)?\n\n"
        f"```python\n{main_py[:14000]}\n```"))
    if answer is None or answer[0] == UNSURE:
        return
    verdict, reason = answer
    if verdict == YES:
        v.add("review", PASS, f"reviewer: the script computes what was asked ({reason})")
    else:
        v.add("review", FAIL if mode == "enforce" else WARN,
              f"the reviewer found the script doesn't compute what was asked -- {reason}")


def llm_method_check(agent, *, request: str | None, plan: dict,
                     guidance: str | None = None) -> tuple[str, str] | None:
    """The reviewer's "can this method compute what was asked?" at PLAN (#222).

    Returns ``(verdict, reason)`` -- ``yes`` / ``no`` -- or None when there is no
    reviewer, it is off, or the answer was unclear. Judged before the approval
    card, so a method that cannot deliver the property is replaced before the
    researcher spends an approval (and a cluster job) on it.
    """
    if agent is None or llm_mode() == "off":
        return None
    method = plan.get("selected_method") or {}
    tools = " + ".join(method.get("libraries") or []) or method.get("tool_name") or "?"
    answer = _ask(agent, _task(request, plan) + (
        f"\nProposed method: {tools}"
        + (f" with the calculator {method['calculator']}" if method.get("calculator") else "")
        + f".\nPlan summary: {str(plan.get('summary') or '')[:2500]}\n"
        + (f"\nEstablished routes for this property:\n{guidance}\n" if guidance else "")
        + "\nCan this method, as planned, compute the requested property for this system "
          "(the quantity itself, not a related one it cannot be converted from)?"))
    if answer is None or answer[0] == UNSURE:
        return None
    return answer


def _output_digest(execution_result: dict, limit: int = 6000) -> str:
    """The job's small text outputs and the end of its stdout, for the reviewer."""
    parts = []
    outputs = execution_result.get("artifacts_dir")
    if outputs and Path(outputs).is_dir():
        for path in sorted(Path(outputs).rglob("*")):
            if (path.is_file() and path.suffix.lower() in (".json", ".csv", ".txt", ".yaml")
                    and path.stat().st_size <= 200_000):
                text = path.read_text(encoding="utf-8", errors="replace")
                parts.append(f"--- {path.name}\n{text[:2500]}")
    stdout = (execution_result.get("stdout") or "").strip()
    if stdout:
        parts.append(f"--- end of stdout\n{stdout[-2500:]}")
    return "\n".join(parts)[:limit]


def llm_outputs_check(v: Verdict, agent, *, request: str | None, plan: dict,
                      execution_result: dict | None) -> None:
    """Add the reviewer's "are these outputs physically sensible?" to ``v`` (warn only)."""
    result = execution_result or {}
    if agent is None or llm_mode() == "off" or not result.get("succeeded"):
        return
    digest = _output_digest(result)
    if not digest:
        return
    answer = _ask(agent, _task(request, plan) + (
        "\nThe calculation finished. Are its outputs physically sensible for this system and "
        "property (plausible magnitude, sign and units; not an obvious placeholder, zero, or "
        f"NaN)?\n\n{digest}"))
    if answer is None or answer[0] == UNSURE:
        return
    verdict, reason = answer
    looks = "look physically sensible" if verdict == YES else "look physically doubtful"
    v.add("review", PASS if verdict == YES else WARN, f"reviewer: the outputs {looks} ({reason})")
