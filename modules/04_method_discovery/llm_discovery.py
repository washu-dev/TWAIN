"""LLM-driven, availability-grounded tool discovery (module 04_method_discovery).

The static registry + scorer (registry_loader/scorers) and the calculator
catalog (calculator_registry) are a *seed*: a curated candidate set with factual
metadata. This module makes the actual *choice* by reasoning, not by
hand-assigned fidelity numbers:

  1. Ask the LLM to pick the best library (+ optional python calculator) for the
     task, given the target platform and the candidates' real metadata --
     including practical usability (does a run need external data the package
     doesn't ship? accuracy vs cost?).
  2. **Ground** the pick: deterministically confirm a conda/pip build of the
     chosen calculator exists for the platform before committing. If it doesn't,
     give the LLM the constraint and let it choose again; if it still can't, the
     caller falls back to the deterministic registry path.

No hand-tuned rankings decide the outcome -- the LLM reasons over real
candidates and a real availability check keeps it honest. Everything is
injectable (``agent``, ``available``) so it is fully offline-testable.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class ToolRecommendation:
    libraries: List[str]                 # ordered; [0] is the primary/driver
    calculator: Optional[str] = None
    calculator_import: Optional[str] = None
    calculator_library: Optional[str] = None
    reasoning: str = ""
    warnings: List[str] = field(default_factory=list)


_PROMPT = """You are TWAIN's method-discovery agent for computational chemistry / \
materials science. Choose the best tool(s) to accomplish this task ON THE GIVEN \
PLATFORM. Reason about real fitness -- accuracy vs cost, and practical usability \
(does a run need external data the package does not ship, e.g. pseudopotentials \
or Slater-Koster files?). Prefer a tool that will actually run out of the box on \
the platform; do not pick one with no build for it. Also prefer a method that can \
deliver the requested property by its rigorous definition -- one that supports \
relaxing the geometry to equilibrium, converging its numerical settings, and \
computing the exact quantity requested rather than a cheap proxy.

STRONGLY PREFER A REAL FIRST-PRINCIPLES / ELECTRONIC-STRUCTURE CALCULATION -- one \
that actually solves the physics (DFT, tight-binding, quantum chemistry) -- over an \
ML SURROGATE PREDICTOR (`ml_surrogate: true`), which only regresses a learned model \
and does not compute the property. Choose an ML surrogate ONLY as a last resort when \
no real engine can run on this platform; if you do, say so explicitly in the \
reasoning. If the only real engine for the property has no build here (e.g. a \
plane-wave DFT code on osx-arm64), prefer a real engine that DOES build here (even \
one needing external parameter data) and note the better platform, rather than \
silently substituting a prediction.

TASK
  objective: {objective}
  material:  {material}
  domain:    {domain}
  property:  {property}
  platform:  {platform}

CANDIDATE LIBRARIES (the framework that builds the system / drives a calculator):
{libraries}

CANDIDATE CALCULATORS (compute engines; attach to a driver library). \
`needs_external_data: true` means a real run requires files the package doesn't \
ship. `ml_surrogate: true` means it PREDICTS via a trained model rather than \
computing the physics (dispreferred -- see above). `platforms` lists where a build \
exists (empty = any):
{calculators}

Respond with ONLY a JSON object (no prose, no fences):
{{
  "library": "<primary library name>",
  "supporting_libraries": ["<other libraries used together, if any>"],
  "calculator": "<calculator name, or null if the library computes the property itself>",
  "reasoning": "<one or two sentences: why this is the best fit for this task+platform>"
}}
You may name a well-known tool that isn't listed if it is clearly better and \
installable on the platform, but prefer the candidates. Begin now."""


def _fmt_candidates(items: List[dict], keys: List[str]) -> str:
    lines = []
    for it in items:
        parts = [f"{k}={it.get(k)!r}" for k in keys if it.get(k) not in (None, [], "")]
        lines.append("  - " + ", ".join(parts))
    return "\n".join(lines) if lines else "  (none)"


def build_prompt(*, objective, material, domain, requested_property, platform,
                 libraries: List[dict], calculators: List[dict]) -> str:
    return _PROMPT.format(
        objective=objective or "(unspecified)",
        material=material or "(unspecified)",
        domain=domain or "(unspecified)",
        property=requested_property or "(unspecified)",
        platform=platform or "(unspecified)",
        libraries=_fmt_candidates(libraries, ["name", "capabilities", "description"]),
        calculators=_fmt_candidates(
            calculators,
            ["name", "capabilities", "needs_external_data", "ml_surrogate",
             "platforms", "description"],
        ),
    )


def parse_recommendation(text: str) -> Optional[dict]:
    """Extract the JSON object from the agent reply (tolerating fences/prose).

    >>> parse_recommendation('{"library": "ASE", "calculator": "GPAW"}')["library"]
    'ASE'
    >>> parse_recommendation('```json\\n{"library": "ASE"}\\n```')["library"]
    'ASE'
    >>> parse_recommendation("no json here") is None
    True
    """
    if not isinstance(text, str):
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _default_conda_check(package: str, platform: str) -> Optional[bool]:
    """Whether a conda-forge build of ``package`` exists for ``platform``.

    Best-effort via ``pixi search``; returns None when it can't tell (no pixi,
    no network, timeout) so the caller trusts the LLM rather than blocking.
    """
    try:
        proc = subprocess.run(
            ["pixi", "search", package, "--platform", platform],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    if "No candidates" in out or "could not be found" in out.lower():
        return False
    if re.search(r"\b\d+\.\d+", out):  # version lines => builds exist
        return True
    return None


def _resolve_calculator(name: Optional[str], calculators: List[dict]) -> Optional[dict]:
    if not name:
        return None
    want = name.strip().lower()
    for c in calculators:
        if c.get("name", "").lower() == want or c.get("id", "").lower() == want:
            return c
    return None


def recommend_toolset(
    *,
    objective: str,
    material: str,
    domain: Optional[str],
    requested_property: Optional[str],
    platform: str,
    libraries: List[dict],
    calculators: List[dict],
    agent: Callable[[str], str],
    available: Optional[Callable[[str, str], Optional[bool]]] = None,
    max_repair: int = 1,
) -> Optional[ToolRecommendation]:
    """LLM picks a toolset; a platform availability check grounds it.

    Returns a :class:`ToolRecommendation`, or ``None`` when the reply is unusable
    or the chosen calculator can't be grounded on the platform after repair (the
    caller then uses the deterministic registry path). ``agent`` is a
    ``prompt -> str`` callable; ``available(package, platform)`` returns
    True/False/None (None = unknown -> trusted).
    """
    available = available or _default_conda_check
    prompt = build_prompt(
        objective=objective, material=material, domain=domain,
        requested_property=requested_property, platform=platform,
        libraries=libraries, calculators=calculators,
    )
    warnings: List[str] = []
    for attempt in range(max_repair + 1):
        try:
            reply = agent(prompt)
        except Exception:  # noqa: BLE001 - any agent failure -> caller falls back
            return None
        rec = parse_recommendation(reply)
        if not rec or not rec.get("library"):
            return None

        calc_name = rec.get("calculator")
        calc = _resolve_calculator(calc_name, calculators)
        # Ground the calculator against real platform availability. Known
        # candidates carry a verified ``platforms`` list (cached from a conda
        # search); novel picks fall through to the live ``available`` check.
        if calc_name and calc is not None:
            plats = calc.get("platforms")
            if plats is not None:
                ok = (not plats) or (platform.lower() in {p.lower() for p in plats})
            else:
                pkg = calc.get("pip_name") or calc.get("id") or calc_name
                ok = available(pkg, platform)
            if ok is False:
                warnings.append(f"{calc_name} has no build for {platform}")
                if attempt < max_repair:
                    prompt = (prompt + f"\n\nNOTE: {calc_name} has NO build for "
                              f"{platform}. Choose a calculator that does, or null.")
                    continue
                return None  # couldn't ground -> let the caller fall back

        libs = [rec["library"]] + [s for s in (rec.get("supporting_libraries") or [])
                                   if s and s.lower() != rec["library"].lower()]
        calc_import = calc.get("import_name") if calc else None
        calc_library = (calc.get("driver_library") if calc else None)
        # Make sure the calculator's driver library is in the toolset.
        if calc_library and calc_library.lower() not in {x.lower() for x in libs}:
            libs.append(calc_library)
        return ToolRecommendation(
            libraries=libs,
            calculator=(calc.get("name") if calc else None),
            calculator_import=calc_import,
            calculator_library=calc_library,
            reasoning=str(rec.get("reasoning", "")),
            warnings=warnings,
        )
    return None
