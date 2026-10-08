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
LLM judgement ("does this script compute the property for this system?") is
P4 (#188) and plugs in here as further checks.
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
    prop = _norm(plan.get("requested_property"))
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
