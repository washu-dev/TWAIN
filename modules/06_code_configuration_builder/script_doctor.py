"""ScriptDoctor: verify, proactively review, and repair a generated main.py.

This is the engine behind the pipeline's REPAIR stage. BUILD (the
:class:`code_gen.codegen_engine.CodegenEngine`) emits a *plausible* ``main.py``
-- it compiles, references the chosen calculator, and has an entrypoint -- but
"compiles" is a low bar: a script can still guess a model identifier that does
not exist, misuse an API, or (after a truncated synthesis reply) define a
function it never finishes. ScriptDoctor turns that plausible script into one
that actually runs.

It works in two phases, both driven by structured :class:`Diagnostic` findings
so the caller can log exactly what was wrong and what changed:

1. **Correctness.** Static checks (does it compile? does it have a runnable
   entrypoint? does it reference the calculator? are any names used-but-never-
   defined -- the fingerprint of truncation?) plus a ``--smoke`` run in the heavy
   -calculator env. Any hard error is handed back to the model with the real
   diagnostics; the fix is re-checked. Repeats up to ``max_rounds``.

2. **Proactive hardening.** Once the script is runnable, one LLM *review* pass
   scans the whole script for latent bugs that a smoke run would not surface
   (wrong argument types, unit/shape mistakes, logic that doesn't compute the
   requested property). Anything it flags as an error is fixed and re-verified,
   so bugs are caught *before* the expensive real run rather than after.

Design notes
------------
* **No network / no LLM needed to be useful.** With ``agent=None`` (the default
  offline path) the doctor still runs every static check and reports findings; it
  simply can't apply LLM fixes. With no sim-env interpreter, the smoke phase
  degrades to ``unverifiable`` instead of blocking. It never raises on an
  environment gap -- repair is best-effort hardening, and EXECUTE remains the
  real gate.
* **Injectable seams for tests.** ``agent`` (prompt -> str), ``verifier``
  ((source, brief) -> :class:`SmokeOutcome`) and ``sim_python`` are all
  injectable so the loop can be exercised fully offline and deterministically.
* **Single acceptance bar.** Repaired candidates are validated with the same
  :func:`code_gen.codegen_engine.extract_valid_source` BUILD uses, so a repair
  can never lower the bar the initial synthesis had to clear.
"""
from __future__ import annotations

import ast
import builtins
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

# Reuse the engine's env plumbing + static validators so BUILD and REPAIR share
# one acceptance bar (see module docstring). The doctor depends on the engine;
# the engine never depends on the doctor -- no import cycle.
try:  # pragma: no cover - import shim (package alias vs. bare name)
    from code_gen.codegen_engine import (
        SIM_ENV, extract_valid_source, has_runnable_entrypoint,
        pixi_env_python, strip_code_fences, _LEFTOVER_PLACEHOLDER,
    )
except ImportError:  # pragma: no cover
    from codegen_engine import (
        SIM_ENV, extract_valid_source, has_runnable_entrypoint,
        pixi_env_python, strip_code_fences, _LEFTOVER_PLACEHOLDER,
    )

# Sentinel so ``sim_python=None`` ("explicitly no interpreter, skip smoke") is
# distinguishable from "not provided, resolve it yourself".
_UNSET = object()

# How long a single --smoke run may take before we treat it as unverifiable.
_SMOKE_TIMEOUT_S = 300


@dataclass(frozen=True)
class SmokeOutcome:
    """Result of running a candidate script with ``--smoke`` in the sim env.

    ``status`` is one of:
      * ``"pass"``          -- the script ran, loaded the calculator, and wrote its
                               output; accept it.
      * ``"repairable"``    -- a real code bug (bad identifier, misused API, a run
                               that produced no output); the ``error`` is fed back
                               to the model to self-correct.
      * ``"unverifiable"``  -- we couldn't verify here (env not built, calculator/
                               library not importable in the env, or a network
                               failure); accept the compiling script as-is.
    """

    status: str
    error: str = ""


@dataclass(frozen=True)
class Diagnostic:
    """One problem found in a script.

    ``source`` records which check produced it (``compile``, ``entrypoint``,
    ``calculator``, ``undefined-name``, ``placeholder``, ``smoke``, ``review``);
    ``severity`` is ``error`` (blocks a correct run) or ``warning`` (worth fixing
    but not fatal). ``line`` is 1-based when known.
    """

    source: str
    severity: str
    message: str
    line: Optional[int] = None

    def render(self) -> str:
        where = f" (line {self.line})" if self.line else ""
        return f"[{self.severity}:{self.source}]{where} {self.message}"


@dataclass
class HealReport:
    """Outcome of :meth:`ScriptDoctor.heal`.

    ``status`` is one of:
      * ``"healthy"``      -- passed every check unchanged.
      * ``"repaired"``     -- one or more fixes were applied and the result passes.
      * ``"unverifiable"`` -- statically clean, but the smoke run couldn't execute
                              here (no sim env / calculator absent).
      * ``"unrepairable"`` -- still has blocking errors (no agent, agent gave up,
                              or the round budget was exhausted).

    ``source`` is the best script the doctor produced (possibly unchanged).
    ``fixes`` is a human-readable log of what each repair round addressed, and
    ``remaining`` lists diagnostics still present at the end.
    """

    source: str
    status: str
    rounds: int = 0
    fixes: List[str] = field(default_factory=list)
    remaining: List[Diagnostic] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.status == "repaired"

    @property
    def healthy(self) -> bool:
        """Whether the script is safe to hand to EXECUTE (no known blockers)."""
        return self.status in ("healthy", "repaired", "unverifiable")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "rounds": self.rounds,
            "fixes": list(self.fixes),
            "remaining": [d.render() for d in self.remaining],
        }


# --------------------------------------------------------------------------- #
# Prompts. Both are domain-agnostic -- they describe *how* to review/repair a
# reliable script, never *what* property or material to expect.
# --------------------------------------------------------------------------- #
_REVIEW_PROMPT = """You are reviewing a Python script (main.py) that computes \
{property} of {material_desc} using the {library} library with the {calculator} \
calculator. Statically review it for LATENT bugs that would make it crash or \
produce a wrong result at run time -- for example: calling a library API with the \
wrong argument types or arity, using a model/dataset/parameter identifier that may \
not exist, unit or array-shape mistakes, an uninitialized or undefined variable, a \
resource blow-up (dense k-grid or huge cell in the main path), or logic that does \
not actually compute {property}.

Do NOT report style, formatting, naming, or import-placement preferences. Only \
report real, actionable bugs.

Return ONLY a JSON array (no prose, no markdown fences). Each element must be an \
object: {{"severity": "error" or "warning", "line": <1-based int or null>, \
"message": "<what is wrong and how to fix it>"}}. Return [] if you find nothing.

--- main.py ---
{source}
--- end main.py ---"""


_REPAIR_PROMPT = """A Python script (main.py) that computes {property} of \
{material_desc} using {library} + the {calculator} calculator has problems. Return \
the COMPLETE corrected script (Python source only -- no markdown fences, no prose). \
Keep everything that already works and fix the root cause of every problem listed.

--- problems found ---
{problems}
--- end problems ---

--- current main.py ---
{source}
--- end main.py ---

How to fix:
- If a model/dataset/parameter identifier was not found (a 401/404, "Repository Not \
Found", or "No valid model" error), you used a name that does not exist. Discover \
the valid identifier at runtime from the library (list its available/pretrained \
models and pick the one matching the task) instead of guessing.
- If an argument had the wrong type or an API was misused (TypeError, \
AttributeError), match the library's documented signature and types.
- If a variable is undefined or the script looks truncated, complete the logic.
- Do NOT hardcode physical constants, lattice parameters, or the expected answer; \
build the system from the library's reference data and actually compute the value.
- Keep the same libraries, calculator, material, property, argparse flags \
(--output and --smoke), and the JSON-to-stdout + CSV-to-output contract.
- End the file with an `if __name__ == "__main__":` block that runs it. Output the \
COMPLETE script in one reply -- do not stop partway or omit the entrypoint.

Return the full corrected script now."""


class ScriptDoctor:
    """Verify and repair a synthesized ``main.py`` (the REPAIR stage's engine).

    ``agent`` is a ``prompt -> str`` callable (the live gateway, already wrapped
    with a generous output budget) or ``None`` for a static-only pass. ``brief``
    carries the run's context (calculator/library import names, property,
    material description, acceptance metrics, output filename) used both to check
    invariants and to give the model context in prompts. ``verifier`` and
    ``sim_python`` are injectable so the smoke phase runs offline in tests.
    """

    def __init__(self, *, agent: Optional[Callable[[str], str]] = None,
                 brief: Optional[dict] = None,
                 sim_python: Union[str, None, object] = _UNSET,
                 verifier: Optional[Callable[[str, dict], SmokeOutcome]] = None,
                 max_rounds: int = 3, review: bool = True):
        self.agent = agent
        self.brief = dict(brief or {})
        self._sim_python = sim_python
        self._verifier = verifier
        self.max_rounds = max(1, max_rounds)
        self.review_enabled = review

    # -- public API ----------------------------------------------------------
    def heal(self, source: str) -> HealReport:
        """Diagnose, repair, and proactively harden ``source``; return a report.

        Never raises on an environment gap: with no agent it reports findings it
        can't fix; with no sim env it skips the smoke run. The returned
        ``HealReport.source`` is always usable (the best script produced).
        """
        if not isinstance(source, str) or not source.strip():
            return HealReport(source or "", "unrepairable", 0, [],
                              [Diagnostic("input", "error", "empty script")])

        fixes: List[str] = []
        rounds = 0
        last_smoke: Optional[SmokeOutcome] = None

        # -- phase 1: correctness (static + smoke), repair-driven --------------
        while rounds < self.max_rounds:
            static = self.static_diagnostics(source)
            errors = [d for d in static if d.severity == "error"]
            last_smoke = None
            if not errors:
                last_smoke = self.smoke(source)
                if last_smoke.status == "repairable":
                    errors = [Diagnostic("smoke", "error", last_smoke.error)]
            if not errors:
                break  # runnable: compiles, has an entrypoint, and smoke is clean

            if self.agent is None:
                # Static/dynamic problems found but nothing can fix them here.
                return HealReport(source, "unrepairable", rounds, fixes, static or errors)

            warnings = [d for d in static if d.severity == "warning"]
            fixed = self._repair(source, errors + warnings)
            rounds += 1
            if not fixed or fixed == source:
                return HealReport(source, "unrepairable", rounds, fixes, static or errors)
            fixes.append(f"round {rounds}: " + _summarize(errors))
            source = fixed
        else:
            # Budget exhausted with errors still present.
            return HealReport(source, "unrepairable", rounds, fixes,
                              self.static_diagnostics(source))

        # -- phase 2: proactive hardening (one LLM review of a runnable script) -
        if self.agent is not None and self.review_enabled:
            findings = self.review(source)
            actionable = [d for d in findings if d.severity == "error"]
            if actionable:
                fixed = self._repair(source, findings)
                # A hardening fix must not regress the script -- neither the hard
                # static checks NOR the runtime. Static-only acceptance let a review
                # that introduced a bad-but-syntactically-valid API call (e.g.
                # `matgl.get_available_models()`) slip through to EXECUTE. So we
                # re-verify: accept the review only if it still passes static AND
                # smokes clean (repairing a runtime break the review introduced);
                # otherwise keep the runnable pre-review script.
                if fixed and fixed != source and not _has_errors(self.static_diagnostics(fixed)):
                    verified = self._smoke_repair(fixed)
                    if verified is not None:
                        source = verified
                        rounds += 1
                        fixes.append(f"proactive review: addressed {len(actionable)} latent issue(s)")

        return HealReport(source, _final_status(fixes, last_smoke), rounds, fixes,
                          self.static_diagnostics(source))

    def _smoke_repair(self, source: str) -> Optional[str]:
        """Smoke ``source`` and repair a runtime break, bounded by ``max_rounds``.

        Assumes ``source`` already passes the static checks. Returns a version that
        smokes clean (``pass``/``unverifiable``), or ``None`` when a ``repairable``
        runtime break can't be fixed here -- so the caller can keep the previous,
        known-good script instead of shipping a regression. Used to re-verify the
        proactive review's output (BUILD's initial script is verified by phase 1).
        """
        for _ in range(self.max_rounds):
            outcome = self.smoke(source)
            if outcome.status != "repairable":
                return source  # clean (or can't-verify-here) -> good enough
            if self.agent is None:
                return None
            fixed = self._repair(source, [Diagnostic("smoke", "error", outcome.error)])
            if not fixed or fixed == source or _has_errors(self.static_diagnostics(fixed)):
                return None
            source = fixed
        return None

    # -- diagnosis -----------------------------------------------------------
    def static_diagnostics(self, source: str) -> List[Diagnostic]:
        """All problems detectable without running the script.

        Ordered cheapest-first. A syntax error short-circuits the rest (the AST
        the other checks need can't be built).
        """
        try:
            compile(source, "main.py", "exec")
        except SyntaxError as exc:
            return [Diagnostic("compile", "error",
                               f"does not compile: {exc.msg}", exc.lineno)]

        diags: List[Diagnostic] = []
        if not has_runnable_entrypoint(source):
            diags.append(Diagnostic(
                "entrypoint", "error",
                "no runnable entrypoint: the module defines functions but never "
                "calls them (add `if __name__ == \"__main__\":`). Often a sign the "
                "script was truncated."))
        calc = self.brief.get("calculator_import")
        if calc and calc.lower() not in source.lower():
            diags.append(Diagnostic(
                "calculator", "error",
                f"never references the required calculator import '{calc}'."))
        for name, line in _undefined_names(source):
            diags.append(Diagnostic(
                "undefined-name", "error",
                f"name '{name}' is used but never defined, imported, or assigned "
                f"(likely a typo or a truncated script).", line))
        for token in sorted(set(_LEFTOVER_PLACEHOLDER.findall(source))):
            diags.append(Diagnostic(
                "placeholder", "warning",
                f"unfilled template placeholder {token} left in the script."))
        return diags

    def smoke(self, source: str) -> SmokeOutcome:
        """Run ``source`` with ``--smoke`` in the sim env and classify the result.

        Best-effort and side-effect-free: the script runs in a throwaway temp dir.
        Any inability to actually verify (no interpreter, spawn/timeout failure) is
        reported as ``unverifiable`` so the caller accepts the compiling candidate.
        A zero exit that produced no output file is a real bug (``repairable``) --
        it is how a truncated, entrypoint-less script fails silently.
        """
        if self._verifier is not None:
            return self._verifier(source, self.brief)  # injected verifier (tests)
        python = self._resolve_sim_python()
        if not python:
            return SmokeOutcome("unverifiable", "no sim-env interpreter to verify with")
        output_file = self.brief.get("output_file") or "results.csv"
        try:
            with tempfile.TemporaryDirectory(prefix="twain_repair_") as tmp:
                (Path(tmp) / "main.py").write_text(source, encoding="utf-8")
                proc = subprocess.run(
                    [python, "main.py", "--smoke"],
                    cwd=tmp, capture_output=True, text=True, timeout=_SMOKE_TIMEOUT_S,
                )
                produced = (Path(tmp) / output_file).is_file()
        except (OSError, subprocess.SubprocessError):
            return SmokeOutcome("unverifiable", "could not run the smoke verification")
        if proc.returncode == 0:
            if produced:
                return SmokeOutcome("pass")
            return SmokeOutcome(
                "repairable",
                f"the script exited 0 but wrote no output file ('{output_file}'). "
                f"In --smoke mode it must build a small system, load the calculator, "
                f"and write the metrics row. Ensure the entrypoint actually runs.")
        return self._classify_smoke((proc.stderr or "") + (proc.stdout or ""))

    def review(self, source: str) -> List[Diagnostic]:
        """Ask the model to scan a runnable script for latent bugs.

        Returns ``[]`` when there is no agent, the reply doesn't parse, or nothing
        was found -- the proactive pass is advisory and must never wedge the loop.
        """
        if self.agent is None:
            return []
        prompt = _REVIEW_PROMPT.format(source=source, **self._prompt_context())
        try:
            raw = self.agent(prompt)
        except Exception:  # noqa: BLE001 - any agent failure -> no findings
            return []
        return _parse_findings(raw)

    # -- repair --------------------------------------------------------------
    def _repair(self, source: str, diagnostics: List[Diagnostic]) -> Optional[str]:
        """One repair call: hand the model the script + findings, validate the fix.

        Returns a compiling, entrypoint-bearing, calculator-referencing script, or
        ``None`` when the agent is absent/fails or the reply doesn't pass the bar.
        """
        if self.agent is None:
            return None
        problems = "\n".join(f"{i}. {d.render()}" for i, d in enumerate(diagnostics, 1))
        prompt = _REPAIR_PROMPT.format(
            source=source, problems=problems, **self._prompt_context())
        try:
            raw = self.agent(prompt)
        except Exception:  # noqa: BLE001 - agent failure -> no fix this round
            return None
        return extract_valid_source(raw, self.brief.get("calculator_import"))

    # -- internals -----------------------------------------------------------
    def _prompt_context(self) -> Dict[str, str]:
        """The material/tool context both prompts interpolate (with safe defaults)."""
        b = self.brief
        return {
            "property": b.get("property") or "the requested property",
            "material_desc": b.get("material_desc") or "the requested material",
            "library": b.get("library") or b.get("library_import") or "the library",
            "calculator": b.get("calculator") or b.get("calculator_import") or "the calculator",
        }

    def _resolve_sim_python(self) -> Optional[str]:
        """The heavy-calculator interpreter to verify in, or None if unavailable."""
        if self._sim_python is not _UNSET:
            return self._sim_python  # injected (a path, or None to force skip)
        return pixi_env_python(SIM_ENV)

    def _classify_smoke(self, output: str) -> SmokeOutcome:
        """A non-zero smoke run -> repairable code bug vs. can't-verify-here.

        Missing top-level toolset packages and network failures mean the env (not
        the code) is the blocker -> ``unverifiable``. A missing *submodule* of an
        installed package, or any other runtime error (bad model id, wrong argument
        type, ...) is a real bug in the generated code -> ``repairable``.
        """
        tail = "\n".join(output.strip().splitlines()[-25:])
        low = output.lower()
        network_markers = (
            "connectionerror", "max retries", "temporary failure in name resolution",
            "getaddrinfo", "failed to establish a new connection", "read timed out",
            "connection refused", "network is unreachable", "nameresolutionerror",
        )
        if any(m in low for m in network_markers):
            return SmokeOutcome("unverifiable", tail)
        # Top-level packages the run legitimately needs; if one of *these* is
        # missing, the env isn't provisioned here (not a code bug).
        toolset = {
            str(self.brief.get("calculator_import") or "").split(".")[0].lower(),
            str(self.brief.get("library_import") or "").split(".")[0].lower(),
        }
        toolset.discard("")
        for match in re.finditer(r"No module named ['\"]([\w.]+)['\"]", output):
            name = match.group(1)
            if "." not in name and name.lower() in toolset:
                return SmokeOutcome("unverifiable", tail)  # e.g. matgl absent here
        return SmokeOutcome("repairable", tail)


# --------------------------------------------------------------------------- #
# Module-level helpers (pure; unit-testable in isolation).
# --------------------------------------------------------------------------- #
def _summarize(diagnostics: List[Diagnostic]) -> str:
    """A short, human-readable summary of a batch of diagnostics."""
    if not diagnostics:
        return "no issues"
    kinds = sorted({d.source for d in diagnostics})
    head = diagnostics[0].message.split(".")[0]
    return f"fixed {len(diagnostics)} issue(s) [{', '.join(kinds)}]: {head}"


def _has_errors(diagnostics: List[Diagnostic]) -> bool:
    return any(d.severity == "error" for d in diagnostics)


def _final_status(fixes: List[str], last_smoke: Optional[SmokeOutcome]) -> str:
    """Terminal status for a script that cleared phase 1 (no blocking errors)."""
    if last_smoke is not None and last_smoke.status == "unverifiable" and not fixes:
        return "unverifiable"
    return "repaired" if fixes else "healthy"


def _parse_findings(raw) -> List[Diagnostic]:
    """Parse an LLM review reply (a JSON array) into review Diagnostics.

    Tolerant of markdown fences and surrounding prose; returns ``[]`` on anything
    it can't confidently read as a list of findings.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    text = strip_code_fences(raw).strip()
    data = _loads_array(text)
    if not isinstance(data, list):
        return []
    out: List[Diagnostic] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        severity = "error" if str(item.get("severity")).lower() == "error" else "warning"
        line = item.get("line")
        line = line if isinstance(line, int) else None
        out.append(Diagnostic("review", severity, message, line))
    return out


def _loads_array(text: str):
    """``json.loads`` the first top-level JSON array in ``text``, or ``None``."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            return None
    return None


def _undefined_names(source: str) -> List[Tuple[str, int]]:
    """Names used (Load context) but never defined anywhere in the module.

    A deliberately *conservative*, scope-flattening scan: a name counts as defined
    if it is imported, assigned, a parameter, a function/class name, a
    global/nonlocal, or an exception alias *anywhere* in the module. That
    under-reports (a name defined only in another function is treated as defined),
    which keeps false positives near zero while still catching the case that
    matters -- a truncated script referring to a name it never introduces (e.g.
    ``band_gap = comp``). Bails out entirely on ``from x import *`` (which can
    supply arbitrary names, so certainty is impossible).

    >>> _undefined_names("x = 1\\nprint(x)")
    []
    >>> _undefined_names("print(comp)")
    [('comp', 1)]
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            return []

    defined = set(dir(builtins)) | {
        "__name__", "__file__", "__doc__", "__builtins__",
        "__spec__", "__loader__", "__package__", "__annotations__",
    }
    used: Dict[str, int] = {}

    class _Scan(ast.NodeVisitor):
        def visit_Import(self, node):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])

        def visit_ImportFrom(self, node):
            for a in node.names:
                defined.add(a.asname or a.name)

        def visit_FunctionDef(self, node):
            defined.add(node.name)
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            defined.add(node.name)
            self.generic_visit(node)

        def visit_arg(self, node):
            defined.add(node.arg)

        def visit_Global(self, node):
            defined.update(node.names)

        visit_Nonlocal = visit_Global

        def visit_ExceptHandler(self, node):
            if node.name:
                defined.add(node.name)
            self.generic_visit(node)

        def visit_Name(self, node):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                defined.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                used.setdefault(node.id, node.lineno)

    _Scan().visit(tree)
    return [(name, line) for name, line in used.items() if name not in defined]
