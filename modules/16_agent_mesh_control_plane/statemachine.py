import hashlib
import inspect
import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from enum import Enum, auto
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

import twain_paths

logger = logging.getLogger(__name__)

from intake.intent_spec import IntentSpec
from result_interpreter.result_package import ResultPackage
from result_interpreter.extractors.base import (ParsedField, ParsedOutput,
                                                ParserError, get_parser)
from result_interpreter.metric_normalizer import NormalizedResult, normalize
from cross_validation.acceptance_judge import AcceptanceThresholds, cross_validate
from cross_validation.baseline_validator import (
    BaselineDB,
    ChainedBaselines,
    Prediction,
)
from cross_validation import mp_reference, plausibility
from cross_validation.mp_reference import MaterialsProjectBaselines
from self_correction.failure_classifier import RunEvidence
from self_correction.reflection import reflect
from self_correction.rerun_controller import RerunController
from self_correction.strategies import CorrectionContext, build_plan
from states import State, Context, GuardsBroken, InvalidTransition
from crash_recovery import DataStorage
from AgentInterface import AgentInterface
from goal_decomposer.graph_builder import GoalGraph, GraphBuilder
from plan_synthesizer.execution_plan import ExecutionPlan
from plan_synthesizer.plan_synthesizer import PlanSynthesizer
from method_discovery.registry_loader import RegistryLoader
from method_discovery.scorers import DiscoveryQuery, rank_candidates
from method_discovery.ranking_rationale import explain_ranking
from method_discovery.calculator_registry import (
    DEFAULT_DOCKER_IMAGE, DOCKER_LINUX_PLATFORM, calculators_for_property,
    canonical_property, current_platform, docker_available, docker_image_available,
    find_calculator, load_calculators, planning_platform,
)
from method_discovery import llm_discovery
from method_discovery import library_requests as _libreq
from PromptCompiler import PromptGenerator
from code_gen.codegen_engine import (SIM_ENV, CodegenEngine, _first_metric_name,
                                     canonical_tool_key, wants_thermo_cycle,
                                     mp_lookup_requested, pixi_env_python)
from code_gen import dependency_inferencer as _depinf

# Output-token budget for LLM code synthesis. A whole main.py runs well past the
# gateway's small default (1024), so give it generous headroom -- a truncated
# script compiles but has no entrypoint and silently produces nothing.
# A whole main.py has to fit: geometry + method + thermochemistry + CSV output
# + argparse + a smoke path, on top of the header the prompt mandates. At 8192
# a Psi4 thermochemistry script was cut off mid-body -- it still compiled, so
# only the entrypoint check caught it, and BUILD then shipped the placeholder
# scaffold (job 2569967).
_CODEGEN_MAX_TOKENS = 16384


def _module_importable(module: str) -> bool:
    """Whether ``module`` can be located by the import system (no full import).

    Uses ``find_spec`` so we don't pay the cost/side effects of importing a heavy
    scientific package just to check it's present. Dotted names (e.g.
    ``openff.toolkit``) resolve their parent; any failure to resolve -- including
    a missing parent package -- is treated as "not importable".
    """
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, AttributeError, ValueError):
        return False


_CLUSTER_ENV_SPECS_CACHE: Optional[dict] = None

# What RIS ACTUALLY has, from the latest inventory job (#185) -- set by the
# runner before each slice via use_cluster_inventory(). When set, it replaces
# the spec files as the source of truth for planning: a spec says what an env
# should hold, the inventory what it does (run e825d5ed planned for an env that
# was never provisioned at the root the job used).
_CLUSTER_INVENTORY: Optional[dict] = None
_CLUSTER_INVENTORY_TAKEN: Optional[str] = None


def use_cluster_inventory(envs: Optional[dict], taken_at: Optional[str] = None) -> None:
    """Plan from an observed inventory ``{env: iterable of package names}``; None = specs.

    An env the inventory could not read (it reports an error) keeps its spec's
    contents rather than counting as empty -- an unreadable listing is not
    evidence that nothing is installed.
    """
    global _CLUSTER_INVENTORY, _CLUSTER_INVENTORY_TAKEN
    if not envs:
        _CLUSTER_INVENTORY, _CLUSTER_INVENTORY_TAKEN = None, None
        return
    specs = _spec_env_packages()
    _CLUSTER_INVENTORY = {
        str(name).lower(): (frozenset(str(p).lower() for p in pkgs) if pkgs is not None
                            else specs.get(str(name).lower(), frozenset()))
        for name, pkgs in envs.items()}
    _CLUSTER_INVENTORY_TAKEN = taken_at


def cluster_env_source() -> str:
    """Where planning's view of the cluster envs comes from, for notes and logs."""
    if _CLUSTER_INVENTORY is not None:
        return f"RIS inventory{f' of {_CLUSTER_INVENTORY_TAKEN}' if _CLUSTER_INVENTORY_TAKEN else ''}"
    return "env specs (scripts/ris/envs/*.yml)"


def _cluster_env_specs() -> dict:
    """``{env name: frozenset(package names)}`` from the cluster env specs.

    ``scripts/ris/envs/<name>.yml`` provisions ``<envs_root>/<name>``; its
    ``dependencies:`` list is the declarative truth for what that ONE env holds.
    Parsed line-wise (``- pkg=ver``) to avoid a YAML dependency; an unreadable
    or absent specs dir yields ``{}`` ("nothing conda-only is provisioned").
    Kept per env -- not as one union -- because a job runs in a single env:
    "some spec lists it" is not "the env this job uses has it" (#169).
    """
    if _CLUSTER_INVENTORY is not None:
        return _CLUSTER_INVENTORY
    return _spec_env_packages()


def _spec_env_packages() -> dict:
    """The spec files' view (cached): ``{env: frozenset(package names)}``."""
    global _CLUSTER_ENV_SPECS_CACHE
    if _CLUSTER_ENV_SPECS_CACHE is not None:
        return _CLUSTER_ENV_SPECS_CACHE
    specs = {}
    specs_dir = twain_paths.REPO_ROOT / "scripts" / "ris" / "envs"
    try:
        for spec in sorted(specs_dir.glob("*.yml")):
            pkgs = set()
            in_deps = False
            for raw in spec.read_text(encoding="utf-8").splitlines():
                line = raw.split("#", 1)[0].rstrip()
                if not line.strip():
                    continue
                if line.strip() == "dependencies:":
                    in_deps = True
                elif in_deps and line.lstrip().startswith("- "):
                    name = re.split(r"[=<>!\s]", line.strip()[2:].strip(),
                                    maxsplit=1)[0]
                    if name:
                        pkgs.add(name.lower())
                elif not line.startswith(" "):
                    in_deps = False
            specs[spec.stem.lower()] = frozenset(pkgs)
    except OSError:
        specs = {}
    _CLUSTER_ENV_SPECS_CACHE = specs
    return _CLUSTER_ENV_SPECS_CACHE


def _cluster_env_packages() -> frozenset:
    """Every package declared by any cluster env spec (the union)."""
    return frozenset().union(*_cluster_env_specs().values())




def _cluster_env_names() -> frozenset:
    """Names of the pre-provisioned cluster envs (the spec filenames' stems)."""
    return frozenset(_cluster_env_specs())


# Cache of PyPI availability verdicts keyed by (package, version): a plan-time
# network probe must not repeat per candidate per run. Values are True / False /
# None (= could not determine; treated as available, the preflight and the job
# remain the authority).
_PYPI_VERDICTS: dict = {}


def _pip_installable(dep) -> Optional[bool]:
    """Whether PyPI can serve ``dep`` (cached; ``None`` = unknown/offline)."""
    key = (dep.package.lower(), dep.version)
    if key not in _PYPI_VERDICTS:
        _PYPI_VERDICTS[key] = _depinf.is_available_on_pypi(dep.package, dep.version)
    return _PYPI_VERDICTS[key]


def _config_error(message: str, hint: str) -> Exception:
    """A typed ``ConfigError`` when the orchestrator is importable, else a plain one.

    Mirrors ``_execution_error``'s fallback: the state machine is exercised
    standalone in unit tests, where ``error_handler`` is not on the path, and a
    plan-time refusal must still carry its reason there.
    """
    try:
        from error_handler import ConfigError
        return ConfigError(message, hint=hint)
    except Exception:  # noqa: BLE001 - standalone use: plain error with the text
        return RuntimeError(f"{message} -- {hint}")


def _pip_gettable(dep) -> bool:
    """Whether a job's pip fallback can supply ``dep`` on a compute node."""
    package = dep.package.lower()
    if package in _depinf.CONDA_ONLY_PACKAGES:
        return False
    # On PyPI, yet not gettable inside a job's wall clock (a multi-GB torch
    # download onto a compute node) -- see CLUSTER_UNRUNNABLE_PACKAGES.
    if package in _depinf.CLUSTER_UNRUNNABLE_PACKAGES:
        return False
    # Offline / ambiguous PyPI answers fail open: the job's own smoke test
    # still vets the environment before the real calculation.
    return _pip_installable(dep) is not False


def _plan_dependencies(libraries) -> list:
    """The distinct dependencies (``_depinf`` objects) behind ``libraries``."""
    deps, seen = [], set()
    for entry in libraries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        for dep in _depinf.import_names(entry.strip()):
            if dep.package.lower() not in seen:
                seen.add(dep.package.lower())
                deps.append(dep)
    return deps


def cluster_env_candidates(libraries) -> Optional[list]:
    """The cluster envs that can run ``libraries`` together, best first, or None.

    A Slurm job runs in ONE pre-provisioned env and layers a pip venv on it for
    whatever that env lacks (slurm_execution_adapter's ``_env_payload``). So an
    env qualifies iff every package the libraries need is either declared by
    THAT env's spec or gettable with pip. Planning vetoes on this rule, and the
    job tries exactly these envs in this order, layering on the first -- the two
    can no longer disagree (#169). The old rule checked each library against
    the union of all specs and picked envs by name: OpenMM + OpenFF passed
    planning because nwchem.yml lists OpenFF, then the job tried only
    ``default`` (no package is named "nwchem") and died in pip (runs 21ffdacd,
    786bd6b1).

    Ranking: fewest packages left to pip, then an env named after one of the
    packages (it exists to provide that engine), with ``default`` last at equal
    coverage. ``None`` means no env works; ``[]`` only when there are no specs
    at all (nothing to choose from -- the job uses its plain-pip path).
    """
    specs = _cluster_env_specs()
    if not specs:
        return []
    deps = _plan_dependencies(libraries)
    wanted = {d.package.lower() for d in deps}
    blockers = {d.package.lower() for d in deps if not _pip_gettable(d)}
    viable = []
    for name, provided in specs.items():
        missing = [d for d in deps if d.package.lower() not in provided]
        if not all(_pip_gettable(d) for d in missing):
            continue
        # Only envs with a reason to be tried: each candidate costs the job a
        # smoke-test probe, and an unrelated engine env (cp2k for a pymatgen
        # run) offers nothing default doesn't.
        if name != "default" and name not in wanted and not (provided & blockers):
            continue
        viable.append((len(missing), name not in wanted, name == "default", name))
    if not viable:
        return None
    return [entry[-1] for entry in sorted(viable)]


def _selected_toolset(plan: dict) -> list:
    """The calculator, libraries and tool a plan selected (names, unfiltered)."""
    method = (plan or {}).get("selected_method") or {}
    return ([method.get("calculator")] + list(method.get("libraries") or [])
            + [method.get("tool_name")])


def _cluster_env_gap(libraries) -> str:
    """Why no env fits ``libraries``, in one line (for the refusal message)."""
    deps = _plan_dependencies(libraries)
    blockers = sorted(d.package for d in deps if not _pip_gettable(d))
    specs = _cluster_env_specs()
    homes = {b: sorted(n for n, pk in specs.items() if b.lower() in pk) for b in blockers}
    parts = [f"{b} ({'only in ' + ', '.join(homes[b]) if homes[b] else 'in no env spec'}"
             f"; not pip-installable)" for b in blockers]
    names = ", ".join(n for n in libraries if isinstance(n, str) and n.strip())
    return (f"{names} need {'; '.join(parts) or 'packages no single env provides'} "
            f"-- no single env provides all of them")


def _cluster_cannot_run(library: str) -> bool:
    """Whether no cluster env (plus pip) can provide ``library``'s packages.

    Per library, for ranking candidates early; the whole toolset is checked
    together with :func:`cluster_env_candidates` once PLAN has picked it.
    """
    return cluster_env_candidates([library]) is None


# Formula tokens for counting atoms: an element symbol + optional multiplier.
_FORMULA_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*)")


def _atom_count(system_descriptors) -> Optional[int]:
    """Best-effort atom count of the run's target system, or None.

    Prefers a resolved structure's explicit atom list (the exact simulation
    cell); falls back to counting element multiplicities in the formula
    (CaPt2 -> 3, C9H8O4 -> 21). Drives the suggested Slurm CPU request
    (~1 CPU per atom), so a rough answer is fine and None just keeps the
    generic default.
    """
    if not isinstance(system_descriptors, dict):
        return None
    structure = system_descriptors.get("structure")
    if isinstance(structure, dict):
        atoms = structure.get("atoms")
        if isinstance(atoms, list) and atoms:
            return len(atoms)
    formula = system_descriptors.get("formula")
    if not isinstance(formula, str):
        crystal = system_descriptors.get("crystal")
        formula = crystal.get("formula") if isinstance(crystal, dict) else None
    if isinstance(formula, str) and formula.strip():
        total = sum(int(count) if count else 1
                    for symbol, count in _FORMULA_TOKEN.findall(formula) if symbol)
        return total or None
    return None


# Core counts that divide a domain decomposition or k-point grid without an
# awkward remainder. The suggestion snaps down to one of these.
_PARALLEL_WIDTHS = (2, 4, 6, 8, 12, 16, 20, 24, 32, 40, 48, 56, 64)


def _cores_per_atom() -> float:
    """Cores to suggest per atom. ``TWAIN_CORES_PER_ATOM``, default 1.0.

    Deliberately tunable: one core per atom is a serviceable default for
    plane-wave DFT, not a law, and the right ratio depends on the method and the
    machine. A non-positive or unparseable value falls back to the default.
    """
    try:
        ratio = float(os.environ.get("TWAIN_CORES_PER_ATOM", "1.0"))
    except (TypeError, ValueError):
        return 1.0
    return ratio if ratio > 0 else 1.0


def _periodic_min_cores() -> int:
    """Cores a periodic run should get regardless of how few atoms it has.

    ``TWAIN_PERIODIC_MIN_CORES``, default 24. Measured rather than guessed: the
    CaPt2 C15 study spread its 10x10x10 mesh over 12 k-point groups x 2 domains =
    24 ranks, and the identical study on 2 ranks ran about 26x slower (job
    2625288 against e496cf22). A non-positive or unparseable value falls back.
    """
    try:
        value = int(float(os.environ.get("TWAIN_PERIODIC_MIN_CORES", "24")))
    except (TypeError, ValueError):
        return 24
    return value if value >= 2 else 24


def _is_periodic(system_descriptors) -> bool:
    """Whether the target is a periodic solid rather than an isolated molecule."""
    if not isinstance(system_descriptors, dict):
        return False
    if str(system_descriptors.get("kind") or "").strip().lower() == "crystal":
        return True
    crystal = system_descriptors.get("crystal")
    return isinstance(crystal, dict) and bool(crystal)


def _suggest_cpu_count(atoms: int, max_cpus: int, *,
                       periodic: bool = False,
                       scales_with_ranks: bool = False) -> int:
    """A parallel-friendly core count for an ``atoms``-atom system.

    Scales with system size, snaps DOWN to a width that decomposes cleanly (so
    21 atoms asks for 20 rather than 21), and clamps to [2, ``max_cpus``].
    Snapping down rather than up keeps a queue request from exceeding what the
    calculation can actually use.

    Atom count alone is the wrong proxy for a periodic cell, and not merely
    imprecise -- it points the wrong way. The dominant parallel dimension of a
    plane-wave run is its k-points, and the mesh a cell needs scales INVERSELY
    with the cell's size: a small primitive cell wants a dense mesh and so has
    the MOST parallelism to spend. CaPt2 is the worst case of that, because the
    atom count comes from the formula (CaPt2 -> 3, while the C15 primitive cell
    holds 6), so 1 core/atom asked for 3, snapped down to 2, and the run took
    ~26x longer than the same study on 24 cores.

    So a periodic system whose calculator actually gains from extra ranks gets a
    floor instead of an atoms-derived trickle. ``scales_with_ranks`` comes from
    the registry's ``parallelism`` field: for a "threads" calculator more ranks
    do nothing, and a wider request would only idle allocated cores.
    """
    ceiling = max(2, int(max_cpus))
    raw = max(2, int(round(atoms * _cores_per_atom())))
    if periodic and scales_with_ranks:
        raw = max(raw, _periodic_min_cores())
    if raw >= ceiling:
        return ceiling
    friendly = [w for w in _PARALLEL_WIDTHS if w <= raw and w <= ceiling]
    return friendly[-1] if friendly else 2


def _cluster_node_limits() -> dict:
    """Per-node resource ceilings of the configured Slurm cluster.

    Read from the cluster profile (configs/clusters/<name>.json); empty when
    the profile is missing or carries no limits. Used to cap the suggested
    CPU request -- the approval card shows the same numbers to the researcher.
    """
    try:
        from execution_adapter.cluster_profile import ClusterProfile
        profile = ClusterProfile.load(os.environ.get("TWAIN_SLURM_CLUSTER", "compute2"))
    except Exception:
        return {}
    return {k: v for k, v in {
        "cpu_count": profile.max_cpus_per_node,
        "gpu_count": profile.max_gpus_per_node,
        "ram": profile.max_ram_gb,
    }.items() if v is not None}


# Intake filter: one tiny LLM call classifying the RAW request before any
# intent extraction. The intent schema force-fits `domain` into its enum
# (materials/quantum), so an off-topic ask ("explain bitcoin") gets
# misclassified rather than flagged -- it then dies much later, deep in the
# pipeline, with a confusing "no engine could run this" error after burning
# clarification rounds and planning calls. Asking the model directly, before
# extraction, is the general check; any parse/agent failure fails OPEN so a
# broken filter can never block real science.
INTAKE_FILTER_PROMPT = """\
You are the intake filter for TWAIN, an agent that plans and runs computational \
chemistry and materials-science simulations (properties of molecules, crystals, \
and materials via DFT, tight binding, ML surrogates, or database lookups).

Classify the researcher's request below. Reply with EXACTLY one line and nothing else:
SIMULATION -- if it plausibly asks to compute, simulate, estimate, or look up a \
chemistry or materials property or system
OFF_TOPIC: <subject> -- otherwise, where <subject> names what the request is \
actually about in 1-3 words

Request: {query}
"""


def _decline_message(category: str) -> str:
    """The user-facing decline for an off-topic request (posted as the run's end)."""
    return (
        f"This doesn't look like a computational chemistry or materials-science "
        f"request -- it reads as a question about {category}. TWAIN plans and "
        f"runs simulations (properties of molecules, crystals, and materials), "
        f"so nothing was planned or executed. If you did mean a simulation, "
        f"start a new run describing the system (a material, molecule, or "
        f"formula) and the property you want computed."
    )


# Stable lead-in for the "your best-fit engine can't run here" safety note.
# The approval card keys off this exact prefix to offer a one-tap "request it
# via GitHub issue" action, so change it in both places or not at all
# (app/src/screens/ChatScreen.tsx).
ENGINE_UNAVAILABLE_PREFIX = "ENGINE UNAVAILABLE ON THIS DEPLOYMENT: "


# MPI jobs interleave per-rank output as 'rank=N LNN: <line>' (GPAW's rank
# logger); stripped so a crash traceback parses like a plain one.
_MPI_RANK_PREFIX = re.compile(r"^rank=\d+\s+L\d+:\s?", re.MULTILINE)


def _runtime_traceback(result) -> Optional[str]:
    """Extract the generated script's own crash traceback from a failed run.

    This is the trigger for EXECUTE's general self-heal loop: whatever novel
    mistake the synthesized code makes, it surfaces as a Python traceback in
    the run's output -- no per-incident pattern needed. Returns the last
    traceback block (capped), or None when repair can't help: dependency
    errors / timeouts / setup failures have their own handling (hence only
    ``failed`` status qualifies), and a crash whose frames never touch
    ``main.py`` happened outside the code we can rewrite.
    """
    status = getattr(result, "status", None)
    if (getattr(status, "value", None) or str(status)) != "failed":
        return None
    blob = ((getattr(result, "stdout", "") or "")
            + "\n" + (getattr(result, "stderr", "") or ""))
    blob = _MPI_RANK_PREFIX.sub("", blob)
    marker = "Traceback (most recent call last):"
    if marker not in blob:
        return None
    block = marker + blob.rsplit(marker, 1)[1]
    block = "\n".join(block.splitlines()[:60])
    if "main.py" not in block:
        return None
    return block


_INTENT_MAP_CACHE: Optional[dict] = None


# What a question is FOR, so the driver can label it and each gate can find its
# own. Mirrored by runner.bridges (which maps these to message kinds) and by the
# app, which keys its buttons off them.
ASK_CLARIFY = "clarification"
ASK_HEAVY_CONFIRM = "heavy_confirm"
ASK_VALIDATION_GATE = "validation_gate"


def _ask_accepts_purpose(ask) -> bool:
    """Whether ``ask`` takes a ``purpose`` keyword (or **kwargs).

    Probed rather than tried-and-caught: calling and catching TypeError would
    re-invoke an ask that had already asked the researcher.
    """
    try:
        params = inspect.signature(ask).parameters
    except (TypeError, ValueError):
        return False
    if "purpose" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _json_safe(value):
    """Replace non-finite floats with None so an artifact is valid JSON.

    ``json.dump`` writes bare ``NaN``/``Infinity`` by default, which strict
    readers reject: Starlette serializes API responses with ``allow_nan=False``,
    so a single NaN anywhere in an artifact turns the whole report endpoint into
    a 500 rather than degrading one field. Null is the honest JSON spelling of
    "this number does not exist".
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


# Verdict ranking: a run is only as good as its worst check.
_SEVERITY = {"accepted": 0, "needs_review": 1, "rejected": 2}


def _as_float(cell):
    """``cell`` as a finite float, or None when it is not a number."""
    try:
        value = float(str(cell).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _stamp_sample_count(normalized, parsed, primary_name: str):
    """Record how many samples the primary metric was aggregated from.

    One number that is the mean of many rows is a different claim from one
    measured once, and validation has to tell them apart before comparing
    against a single reference (see _ungradable_aggregate).
    """
    field = parsed.get(primary_name)
    if field is not None:
        normalized.metadata["primary_samples"] = len(field.values)
    return normalized


def _finite_fields(parsed):
    """``parsed`` with non-finite samples dropped, or None if nothing survives.

    A NaN is the absence of a measurement, not a measurement -- but it arrives
    mixed in with real ones (one diverged row in a 200-row prediction CSV, a
    diagnostic column that is NaN beside a perfectly good primary metric).
    Discarding the whole field, or the whole output, would throw away every
    valid sample alongside it, so only the non-finite points are dropped and a
    field is removed only when it has no finite sample left at all.
    """
    kept = []
    for fld in parsed.fields:
        values = [v for v in fld.values if math.isfinite(v)]
        if not values:
            continue
        kept.append(ParsedField(name=fld.name, values=values,
                                unit=fld.unit, source=fld.source))
    if not kept:
        return None
    return ParsedOutput(fields=kept, metadata=dict(parsed.metadata))


def _intent_map() -> dict:
    """Load (once) the data-driven discovery intent map from ``configs/``.

    Keeps the objective->capability-tag vocabulary and input-format strings out
    of Python so the discovery domain can grow without code edits (mirrors how
    method-discovery sources its registries from ``configs/*.json``).
    """
    global _INTENT_MAP_CACHE
    if _INTENT_MAP_CACHE is None:
        path = twain_paths.CONFIGS_DIR / "discovery_intent_map.json"
        _INTENT_MAP_CACHE = json.loads(path.read_text(encoding="utf-8"))
    return _INTENT_MAP_CACHE


# The linear "spine" of the pipeline, in order. These are the states a run can
# be rewound to (the loop-only states REPAIR/CORRECT/REPLAN are never rewind
# targets -- a rewind lands on the stage a researcher recognizes, and the machine
# re-derives the loop states from there). ``rewind_to`` uses this order to decide
# which stages count as "downstream" of the target.
REWINDABLE_STATES: list[State] = [
    State.INTAKE, State.CLARIFY, State.DECOMPOSE, State.DISCOVER, State.PLAN,
    State.BUILD, State.EXECUTE, State.INTERPRET, State.VALIDATE, State.ACCEPT,
]

# What each stage *produces*, so a rewind can discard exactly that stage's (and
# every later stage's) output and let the re-run regenerate it from the surviving
# upstream artifacts. ``artifacts`` are keys in ``Context.artifacts``; ``flags``
# are the guard fields on ``Context`` the stage sets. REPAIR's ``repair_report``
# is folded into BUILD (it is regenerated whenever the bundle is), and EXECUTE
# owns ``execution_result`` + the ``execution_status`` guard it sets from the run.
# CORRECT's ``correction_plan`` folds into VALIDATE the same way REPAIR's does
# into BUILD: it is re-derived from whatever the next validation concludes.
_STAGE_OUTPUTS: dict[State, dict[str, list[str]]] = {
    State.INTAKE:    {"artifacts": ["intent_spec"], "flags": []},
    State.CLARIFY:   {"artifacts": [], "flags": ["clarified"]},
    State.DECOMPOSE: {"artifacts": ["goal_graph", "goal_graph_error"], "flags": []},
    State.DISCOVER:  {"artifacts": ["discovery"], "flags": []},
    State.PLAN:      {"artifacts": ["execution_plan"], "flags": []},
    State.BUILD:     {"artifacts": ["run_bundle", "script", "repair_report",
                                    "codegen_report"],
                      "flags": ["plan_approved", "approved_plan"]},
    State.EXECUTE:   {"artifacts": ["execution_result"],
                      "flags": ["execution_status", "heavy_confirmed"]},
    State.INTERPRET: {"artifacts": ["normalized_result"], "flags": []},
    State.VALIDATE:  {"artifacts": ["validation_report", "correction_plan"],
                      "flags": ["validation_result"]},
    State.ACCEPT:    {"artifacts": [], "flags": []},
}


GUARDS: dict[tuple[State, State], "Callable[[Context], bool]"] = {
    (State.INTAKE, State.CLARIFY): lambda c: True,
    # Off-topic decline: intake refuses a request that isn't a chemistry /
    # materials simulation ask (e.g. "explain bitcoin") and ends the run
    # immediately -- nothing is clarified, planned, or executed.
    (State.INTAKE, State.TERMINATE): lambda c: True,
    (State.CLARIFY, State.DECOMPOSE): lambda c: c.clarified,
    (State.CLARIFY, State.CLARIFY) : lambda c: True,
    (State.DECOMPOSE, State.DISCOVER): lambda c: True,
    (State.DISCOVER, State.PLAN): lambda c: True,
    (State.DECOMPOSE,State.INTAKE): lambda c: True,
    # PLAN->BUILD is unguarded on purpose: crossing it only *reaches* BUILD, the
    # state a run parks in with the plan generated and awaiting the researcher's
    # approval -- no bundle is built and nothing is executed until BUILD's handler
    # runs on the way OUT. The real approval gate is the next edge (BUILD->REPAIR):
    # a run cannot build/execute until ``plan_approved`` is set by an explicit
    # approval (see StateMachine.approve_plan / Orchestrator.approve_plan). Keeping
    # this edge open lets the driver pause at BUILD to ask; the guarded edges below
    # then enforce the decision.
    (State.PLAN, State.BUILD): lambda c: True,
    (State.BUILD, State.REPAIR): lambda c: c.plan_approved,
    (State.REPAIR, State.EXECUTE): lambda c: c.plan_approved,
    (State.EXECUTE, State.INTERPRET): lambda c: c.execution_status,
    (State.INTERPRET, State.VALIDATE): lambda c: True,
    (State.VALIDATE, State.ACCEPT): lambda c: c.validation_result == "accepted",
    (State.VALIDATE, State.REPLAN): lambda c: c.validation_result == "rejected",
    (State.VALIDATE, State.CORRECT): lambda c: c.validation_result == "needs_review",
    (State.ACCEPT, State.TERMINATE): lambda c: True,
    (State.REPLAN, State.PLAN): lambda c: True,
    (State.CORRECT, State.BUILD): lambda c: c.plan_approved,
}


class StateMachine:
    def __init__(self, data_path: str = None, *, agent=None, request=None,
                 ask=None, artifacts_dir=None, run_id: str = None,
                 confidence_threshold: float = 0.8, max_clarify_rounds: int = 3,
                 execute_locally: bool = False, execute_install_deps: bool = False,
                 execute_keep_artifacts: bool = True, execute_timeout=None,
                 execution_adapter=None, verify_codegen: bool = False,
                 script_doctor=None, library_available=None, sim_available=None,
                 library_request_tracker=None, auto_approve=False,
                 execute_slurm: bool = False, slurm_cluster: str = None,
                 should_abort=None, job_event_wait=None, issue_job_ticket=None,
                 cluster_jobs=None):
        # Collaborators are injected and optional, so the machine is usable
        # offline and under test. ``agent`` is either a callable prompt->text or
        # an AgentInterface-like object (.call_agent). It is NOT constructed
        # eagerly here: AgentInterface() performs a network OAuth call, which
        # would break every construction (tests, resume, demo). Pass
        # agent=AgentInterface() at the call site to enable live NLU.
        # All runtime output goes under the repo-anchored logs/ tree (see
        # twain_paths). Defaults resolve here so the machine writes to the same
        # place no matter the working directory; callers may still override.
        twain_paths.ensure_dirs()
        if data_path is None:
            data_path = str(twain_paths.SESSIONS_DIR / "statemachine.sm.json")
        # Ties artifacts back to the driving run: the orchestrator passes its
        # session id here, so files are named ``<name>_<session_id>.json``.
        # Standalone callers get a fresh uuid so artifacts stay unique per run.
        self.run_id = run_id or uuid.uuid4().hex
        # Injected or lazily built on first use. AgentInterface() performs a
        # network OAuth handshake, so building it eagerly here would break every
        # offline construction (tests, resume, and the decompose/discover/plan
        # handlers, which need no agent). See the ``agent`` property below.
        self._agent = agent
        self.ask = ask
        self._request = request
        self.confidence_threshold = confidence_threshold
        self.max_clarify_rounds = max_clarify_rounds
        # Local execution (Story 5.2). Off by default so offline/seeded pipeline
        # runs keep EXECUTE a no-op; the orchestrator (demo/_main) turns it on so
        # real runs actually execute the RunBundle built in BUILD.
        self.execute_locally = execute_locally
        self.execute_install_deps = execute_install_deps
        self.execute_keep_artifacts = execute_keep_artifacts
        self.execute_timeout = execute_timeout
        self._execution_adapter = execution_adapter
        # HPC execution (Story 5.4): when on, EXECUTE submits the RunBundle to
        # the Slurm cluster named by ``slurm_cluster`` (configs/clusters/<name>.json,
        # default compute2) instead of running it locally/in Docker. Implies the
        # run happens even though execute_locally may be off.
        self.execute_slurm = execute_slurm
        self.slurm_cluster = slurm_cluster or "compute2"
        # Terminate seam: a zero-arg callable that returns True once the
        # researcher asked to stop the run. The Slurm adapter polls it between
        # squeue checks so a Terminate press scancels the cluster job instead
        # of letting it burn its whole wall time.
        self.should_abort = should_abort
        # RIS webhook seam: ``(job_id, seconds)`` sleep the Slurm adapter uses
        # between polls, returning early when ris-api reports on that job.
        self.job_event_wait = job_event_wait
        # S3 staging seam (TWAIN_STAGING=s3, #170): ``(run_id, attempt, prefix,
        # ttl) -> token`` issuing the ticket a Slurm job trades for presigned URLs.
        self.issue_job_ticket = issue_job_ticket
        # Detached EXECUTE (P2, #171): the store of Slurm jobs a run is paused
        # on, plus ``suspend_for(reason)`` -- set by the orchestrator, raising its
        # pause signal -- so EXECUTE submits and pauses instead of waiting.
        self.cluster_jobs = cluster_jobs
        self.suspend_for = None
        # Activity seam: ``(event_type, payload)`` publisher the orchestrator
        # sets so stages can report what they're doing (``stage.progress``,
        # ``job.log``) instead of leaving the UI on "Working…". None => silent.
        self.publish_progress = None
        # When on, the REPAIR stage may call the LLM to repair the synthesized
        # calculator script and proactively scan it for latent bugs. Off by
        # default so offline/seeded/test runs make no network calls there; the
        # REPAIR stage still runs its free static checks (see repair()).
        self.verify_codegen = verify_codegen
        # Injectable ScriptDoctor for the REPAIR stage (tests supply one with a
        # stub agent + verifier so repair runs fully offline). None -> repair()
        # builds one from the run's plan/agent.
        self._script_doctor = script_doctor
        # How discovery checks whether a library is actually installed. Discovery
        # must only ever commit to a preset library that can be imported where the
        # run happens (the default interpreter), so it never plans around a tool
        # that isn't there. Defaults to a real in-process import probe; injectable
        # (name -> True/False/None) so planning tests stay hermetic and offline.
        self._library_available = library_available
        # How discovery checks the *sim* env (where CALCULATOR bundles run). A
        # calculator-run toolset must be importable in sim, not just the default
        # env -- e.g. PySCF has no sim build, so it must not ride along in a
        # calculator toolset. ``list[import_name] -> set(missing)``; None -> a real
        # (batched, one-subprocess) sim probe. Injectable so planning tests stay
        # hermetic (no sim subprocess).
        self._sim_available = sim_available
        # Where "we wanted a library that isn't installed" goes. TWAIN still only
        # ever plans with a preset, importable library; the ask is recorded in a
        # deduplicated ledger, filed as a 'LibraryAddition' GitHub issue, and
        # reported to the researcher on the plan. Built lazily on first use (so no
        # env/git/network work happens for a run that needs nothing) and
        # injectable so tests exercise the path offline.
        self._library_request_tracker = library_request_tracker
        # Unattended mode: run to completion without pausing for human confirmation
        # at the heavy-calculation gate (the plan-approval gate is enforced by the
        # driver, e.g. the runner). Set by the orchestrator/runner for automatic runs.
        self.auto_approve = auto_approve
        # Rounds of clarification Q&A run so far; bounds the CLARIFY self-loop.
        self._clarify_rounds = 0
        # Bounds the VALIDATE -> CORRECT/REPLAN self-correction loop (Story 6.3):
        # iteration cap + convergence check, with every stop carrying a reason.
        self._rerun = RerunController()
        self.context = Context()
        self.current_state = State.INTAKE
        self.storage = DataStorage(data_path)
        self.recovery_data = self.storage.load()
        self.prompt_generator = PromptGenerator()

        if not self.recovery_data:
            self.context = Context()
            self.current_state = State.INTAKE
        else:
            self.current_state, self.context = self.recovery_data
        # Where intake/clarify write artifacts (intent_spec.json); defaults next
        # to the recovery file so a run started anywhere persists predictably.
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else twain_paths.ARTIFACTS_DIR

    @property
    def agent(self):
        """The live NLU agent, constructed on first use.

        Building AgentInterface() performs a network OAuth call, so it is
        deferred until a handler that needs it (intake/clarify) first touches
        ``self.agent``. This keeps the machine importable and constructible
        offline. Pass ``agent=...`` to ``__init__`` to inject a stub/live agent.
        """
        if self._agent is None:
            self._agent = AgentInterface()
        return self._agent

    def run(self):
        handler = getattr(self, self.current_state.name.lower())
        next_state = handler()

        key = (self.current_state, next_state)
        if(key not in GUARDS):
            raise InvalidTransition("Invalid transition, inter node travel must be explicitly defined in GUARDS")
        func = GUARDS[key]
        permission = func(self.context)

        if(not permission):
            raise GuardsBroken("Transition not allowed, incomplete context")
        else:
            self.new_state(next_state)





    def new_state(self, next_state: State):
        self.current_state = next_state
        self.storage.commit(self.current_state,self.context)

    def rewind_to(self, target: State) -> None:
        """Rewind the machine to an earlier pipeline stage so it can be re-run.

        "Rerun from CLARIFY" means: go back to CLARIFY and re-do it and everything
        after it, keeping the work of the stages *before* it as input. So this
        discards exactly the artifacts and guard flags that ``target`` and every
        later stage produced (per :data:`_STAGE_OUTPUTS`), leaving the upstream
        artifacts intact, resets the clarify-round counter, and points the machine
        at ``target``. The next :meth:`run` re-enters ``target`` and re-derives
        everything downstream.

        The reset guard flags fall back to their :class:`Context` defaults; a
        driver that seeds guards for a stubbed happy path (e.g. the runner's
        ``execution_status``/``validation_result`` seed) should re-apply that seed
        after rewinding -- see ``Orchestrator.rewind_to``. ``target`` must be one
        of :data:`REWINDABLE_STATES`.
        """
        if target not in REWINDABLE_STATES:
            raise InvalidTransition(
                f"cannot rewind to {getattr(target, 'name', target)}; "
                f"valid targets: {[s.name for s in REWINDABLE_STATES]}"
            )
        cutoff = REWINDABLE_STATES.index(target)
        defaults = Context()  # fresh guard-flag defaults to reset downstream flags to
        for state in REWINDABLE_STATES[cutoff:]:
            outputs = _STAGE_OUTPUTS.get(state, {})
            for key in outputs.get("artifacts", []):
                self.context.artifacts.pop(key, None)
            for flag in outputs.get("flags", []):
                setattr(self.context, flag, getattr(defaults, flag))
        # A rewind restarts the CLARIFY loop from scratch.
        self._clarify_rounds = 0
        # Likewise the correction loop: a rerun that inherited the finished run's
        # iteration count would hit the cap immediately and refuse to correct.
        self._rerun = RerunController(self._rerun.policy)
        self.current_state = target
        self.storage.commit(self.current_state, self.context)

    def approve_plan(self, approved: bool = True) -> None:
        """Record the researcher's plan-approval decision (the BUILD/EXECUTE gate).

        Sets ``plan_approved`` and persists it, so the guarded ``BUILD->REPAIR`` /
        ``REPAIR->EXECUTE`` transitions may proceed. Until this is called (or the
        context is seeded), ``plan_approved`` is False and those guards hold the
        run at the approval gate -- nothing is built or executed. This is the
        engine-level enforcement point for "no execution without an approved plan".
        """
        self.context.plan_approved = approved
        # Remember WHAT was approved, so a later re-plan can tell whether this
        # decision still covers it (see _plan_fingerprint).
        self.context.approved_plan = self._plan_fingerprint() if approved else None
        self.storage.commit(self.current_state, self.context)

    def _plan_fingerprint(self) -> Optional[str]:
        """A digest of the parts of the plan an approval is actually about.

        The card shows the method and the resources, so those are what the
        decision covers; rationales, cost estimates and timestamps can change
        without invalidating it.
        """
        plan = self._load_artifact("execution_plan")
        if not plan:
            return None
        method = plan.get("selected_method") or {}
        material = {
            "tool_name": method.get("tool_name"),
            "calculator": method.get("calculator"),
            "calculator_import": method.get("calculator_import"),
            "libraries": sorted(str(l) for l in method.get("libraries") or []),
            "requested_property": plan.get("requested_property"),
            "slurm_request": plan.get("slurm_request"),
        }
        blob = json.dumps(material, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ---- intake / clarify collaborators ----------------------------------


    def _write_artifact(self, name: str, data: dict) -> str:
        # Artifacts are namespaced by ``run_id`` (the driving session id), so
        # each run's files are uniquely named and traceable back to its session.
        # The returned path is what callers store in ``context.artifacts``.
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifacts_dir / f"{name}_{self.run_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_json_safe(data), f, indent=2, default=str)
        return str(path)

    _WELCOME = (
        "Welcome to TWAIN — your autonomous computational-chemistry research "
        "assistant.\nDescribe the problem you'd like to investigate (e.g. "
        "'predict the aqueous solubility of aspirin'): "
    )

    def _ask_user(self, message: str, purpose: str = ASK_CLARIFY) -> str:
        """Get input from the researcher: ``self.ask`` if injected, else stdin.

        Injecting ``ask`` (a ``message -> answer`` callable) lets the orchestrator
        and tests drive intake/clarify non-interactively; the stdin fallback keeps
        the standalone CLI experience.

        ``purpose`` names the gate asking (see the ``ASK_*`` constants). The web
        driver records it as the message kind so each gate can recognise its own
        question instead of assuming the most recent one must be its own -- an
        assumption that silently swallowed answers once a question could be left
        unanswered. Passed only to an ``ask`` that accepts it, so single-argument
        callables (the CLI, older test doubles) keep working.
        """
        if callable(self.ask):
            if _ask_accepts_purpose(self.ask):
                return self.ask(message, purpose=purpose)
            return self.ask(message)
        return input(message)

    def _agent_text(self, prompt: str, **call_kwargs) -> str:
        """Call the agent and return its text, accepting either agent shape.

        ``agent`` may be a plain ``prompt -> str`` callable (what the orchestrator
        and tests inject) or an ``AgentInterface``-style object whose
        ``call_agent`` returns ``{"content": [{"text": ...}]}`` (the live LLM).
        ``call_kwargs`` (e.g. ``max_tokens``) are forwarded only to the
        ``call_agent`` form; a plain callable is invoked with just the prompt.
        """
        agent = self.agent
        if hasattr(agent, "call_agent"):
            resp = agent.call_agent(prompt, **call_kwargs)
        else:
            resp = agent(prompt)
        if isinstance(resp, str):
            return resp
        return resp["content"][0]["text"]

    @staticmethod
    def _extract_json_object(text: str) -> str:
        """Return the JSON object embedded in an LLM response.

        Models often wrap JSON in prose or ```json fences despite instructions.
        Strip fences, then take the substring from the first ``{`` to the last
        ``}`` so ``json.loads`` sees a clean object.
        """
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            return fenced.group(1)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start:end + 1]
        return text

    def _agent_json(self, prompt: str, *, max_tokens: int = 4096,
                    retries: int = 1) -> dict:
        """Call the agent and parse its JSON reply, retrying on a bad payload.

        Intent specs routinely exceed the agent's default 1024-token response
        cap, which truncates the JSON mid-string (JSONDecodeError: unterminated
        string) -- so JSON calls get an explicit larger budget, fence/prose
        stripping, and one clean retry before the error propagates.
        """
        last_error = None
        for _ in range(retries + 1):
            text = self._agent_text(prompt, max_tokens=max_tokens)
            try:
                return json.loads(self._extract_json_object(text))
            except json.JSONDecodeError as exc:
                last_error = exc
        raise last_error

    @staticmethod
    def _system_kind(intent: dict) -> str:
        """The target system's representation: 'crystal', 'surface', or 'molecule'.

        Reads the IntentSpec's explicit ``kind`` discriminator when present, else
        infers it from which sub-object the spec carries (periodic solids under
        ``crystal``, discrete molecules under ``molecule``). Defaults to
        'molecule' so a spec with neither behaves as it did before.
        """
        sysd = intent.get("system_descriptors") or {}
        kind = str(sysd.get("kind") or "").lower()
        if kind in ("molecule", "crystal", "surface"):
            return kind
        if isinstance(sysd.get("crystal"), dict) and sysd.get("crystal"):
            return "crystal"
        return "molecule"

    def _relevant_scores(self, intent: dict) -> dict:
        """Confidence scores that apply to the chosen system representation.

        Scores that don't apply are dropped -- a crystal is never gated on a
        (meaningless) ``SMILES_confidence`` and a molecule isn't gated on
        ``phase``/``structure`` confidence -- so both the confidence gate and the
        clarification questions ignore them. Without this, a solid-state request
        loops in CLARIFY forever asking for a SMILES it can never sensibly provide.
        """
        scores = (intent.get("metadata") or {}).get("confidence_scores") or {}
        if self._system_kind(intent) in ("crystal", "surface"):
            irrelevant = {"smiles_confidence", "name_confidence"}
        else:
            irrelevant = {"phase_confidence", "structure_confidence"}
        return {k: v for k, v in scores.items() if k.lower() not in irrelevant}

    def _is_confident(self, intent: dict) -> bool:
        """True when every *relevant* confidence score meets the threshold."""
        relevant = self._relevant_scores(intent)
        if not relevant:
            return False
        return all(value >= self.confidence_threshold for value in relevant.values())

    @staticmethod
    def _score_field_name(score_key: str) -> str:
        """Human-readable field a confidence score refers to.

        ``SMILES_confidence`` -> ``SMILES``, ``phase_confidence`` -> ``phase``. The
        trailing ``_confidence`` (schema convention) is stripped; anything else is
        returned unchanged.
        """
        if score_key.lower().endswith("_confidence"):
            return score_key[: -len("_confidence")]
        return score_key

    def _uncertain_fields(self, intent: dict) -> list:
        """Relevant fields below the confidence threshold, most-uncertain first.

        Exactly what CLARIFY should ask about: targeting only genuine gaps keeps
        the questions few and stops clarify re-interrogating fields intake already
        resolved. Returns the human-readable field names (see ``_score_field_name``).
        """
        relevant = self._relevant_scores(intent)
        low = sorted(
            (k for k, v in relevant.items() if v < self.confidence_threshold),
            key=lambda k: relevant[k],
        )
        return [self._score_field_name(k) for k in low]

    def intake(self) -> State:
        schema = str(twain_paths.SCHEMAS_DIR / "intent_spec.schema.json")
        # A pre-supplied request (orchestrator/UI/test) skips the interactive
        # prompt; otherwise fall back to asking on stdin.
        query = self._request if self._request else self._ask_user(self._WELCOME)
        # Decline off-topic asks HERE, before intent extraction: one cheap
        # classification of the raw request beats a late, confusing engine
        # failure after clarification and planning were already paid for.
        category = self._off_topic_category(query)
        if category:
            message = _decline_message(category)
            self.context.artifacts["declined"] = self._write_artifact(
                "declined", {"category": category, "message": message})
            logger.info("[intake] declined off-topic request (%s)", category)
            return State.TERMINATE
        prompt = self.prompt_generator.json_schema_prompt(schema, query)
        intent = self._agent_json(prompt)
        self.context.artifacts["intent_spec"] = self._write_artifact("intent_spec", intent)
        return State.CLARIFY

    def _off_topic_category(self, query) -> Optional[str]:
        """What an off-topic request is actually about, or None when in-domain.

        One short LLM call on the raw request (see ``INTAKE_FILTER_PROMPT``).
        Fails OPEN -- no agent, an errored call, or an unparseable verdict all
        mean "proceed": the filter exists to save the researcher from a slow
        confusing failure, never to block real work.
        """
        if not str(query or "").strip():
            return None
        try:
            verdict = str(self._agent_text(
                INTAKE_FILTER_PROMPT.format(query=query))).strip()
        except Exception:  # noqa: BLE001 -- fail open, whatever broke
            return None
        if not verdict.upper().startswith("OFF_TOPIC"):
            return None
        category = verdict.split(":", 1)[1].strip() if ":" in verdict else ""
        return category or "something other than a simulation"

    def clarify(self) -> State:
        """Raise IntentSpec confidence to the threshold, then mark clarified.

        Fast path: a spec that already clears the bar needs no questions, so
        clarify is a no-op that just sets ``clarified`` and advances. Otherwise it
        runs one agent-generated Q&A round per call, re-checking confidence; the
        orchestrator loops it while it keeps returning CLARIFY. The loop is bounded
        to ``max_clarify_rounds`` rounds -- once exhausted, clarify force-continues
        on the best-effort spec (sets ``clarified``, advances to DECOMPOSE) instead
        of self-looping forever on an intent it can never make confident.
        """
        intent = self._load_artifact("intent_spec")
        if intent is None:
            return State.CLARIFY  # nothing to clarify yet; hold at CLARIFY

        if self._is_confident(intent):
            self.context.clarified = True
            return State.DECOMPOSE

        text = json.dumps(intent)
        # Target only the fields intake left genuinely uncertain, so the model asks
        # about real gaps (and stays terse) instead of re-interrogating the request.
        uncertain = self._uncertain_fields(intent)
        questions = self._agent_text(
            self.prompt_generator.clarification_prompt(text, uncertain_fields=uncertain)
        ).strip()

        # If the model finds nothing worth asking (or replies "No questions."), don't
        # pester the researcher with an empty prompt -- proceed on the best-effort
        # spec. The bounded loop below still caps genuine Q&A rounds.
        if not questions or questions.lower().rstrip(".!") == "no questions":
            logger.info("[clarify] no clarifying questions needed; proceeding.")
            self.context.clarified = True
            return State.DECOMPOSE

        answer = self._ask_user(
            f"I need a little more detail before continuing:\n{questions}",
            ASK_CLARIFY,
        )
        intent = self._agent_json(self.prompt_generator.modify_json_schema(text, answer))
        self.context.artifacts["intent_spec"] = self._write_artifact("intent_spec", intent)
        self._clarify_rounds += 1
        if self._is_confident(intent):
            self.context.clarified = True
            return State.DECOMPOSE

        # Bounded loop: after ``max_clarify_rounds`` rounds we force-continue on
        # the best-effort spec rather than self-looping forever on an intent the
        # agent can't make confident (Story 3.2: "up to N rounds; force-continue").
        if self._clarify_rounds >= self.max_clarify_rounds:
            logger.info(
                "[clarify] confidence still below %s after %s round(s); "
                "proceeding with the best-effort IntentSpec.",
                self.confidence_threshold, self._clarify_rounds,
            )
            self.context.clarified = True
            return State.DECOMPOSE

        self.context.clarified = False
        return State.CLARIFY
    # ---- decompose / discover / plan-synthesis collaborators -------------

    def _load_artifact(self, name: str) -> Optional[dict]:
        """Return a previously written stage artifact's JSON, or None if absent.

        Returning None when the upstream artifact is missing lets these handlers
        be exercised in isolation (e.g. a direct unit call, or a resume that
        skipped intake) by no-opping to the next state instead of raising.
        """
        path = self.context.artifacts.get(name)
        if not path or not Path(path).is_file():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _build_goal_graph(self, intent: dict) -> dict:
        """Decompose the IntentSpec into a GoalGraph DAG.

        Prefers an LLM-driven decomposition (mirroring intake/clarify) so the DAG
        is tailored to the actual request rather than a fixed template. When no
        agent is configured -- or the agent's output is missing, malformed, or not
        an acyclic graph -- it falls back to the deterministic canonical
        decomposition so the stage always yields a valid GoalGraph (and stays
        runnable fully offline). The returned dict is structurally re-validated by
        the caller before it is persisted.
        """
        if self._agent is None:
            return self._canonical_goal_graph(intent)
        try:
            graph = self._decompose_with_agent(intent)
            GraphBuilder.validate(GoalGraph(**graph))  # schema + acyclicity gate
            return graph
        except Exception as exc:
            # Do NOT silently degrade: an agent is present, so a fallback means
            # the LLM decomposition was unusable. Record why (and the raw
            # response) so the run is debuggable, then fall back deterministically.
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "agent goal-graph decomposition failed; using canonical fallback (%s)",
                reason,
            )
            self._dump_failed_decomposition(reason)
            return self._canonical_goal_graph(
                intent, fallback_reason=f"agent decomposition failed -> {reason}"
            )

    def _decompose_with_agent(self, intent: dict) -> dict:
        """Ask the agent to decompose ``intent`` into a GoalGraph dict.

        Builds a schema-anchored prompt from goal_graph.schema.json and parses the
        agent's JSON response, backfilling the required metadata fields the schema
        demands so a model that omits them still yields a valid graph. A generous
        ``max_tokens`` is requested because a full goal DAG is far larger than the
        default budget -- too small a budget truncates the JSON mid-object.
        """
        schema = str(twain_paths.SCHEMAS_DIR / "goal_graph.schema.json")
        prompt = self.prompt_generator.goal_graph_prompt(
            schema, json.dumps(intent), self.run_id
        )
        self._last_decomposition_raw = self._agent_text(prompt, max_tokens=4096)
        graph = json.loads(self._extract_json_object(self._last_decomposition_raw))
        metadata = graph.setdefault("metadata", {})
        metadata.setdefault("source_intent_id", self.run_id)
        metadata.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        return graph

    def _dump_failed_decomposition(self, reason: str) -> None:
        """Persist the raw agent response that failed to parse/validate.

        Written next to the run's other artifacts so a canonical fallback can be
        traced to the exact LLM output that caused it.
        """
        try:
            self.context.artifacts["goal_graph_error"] = self._write_artifact(
                "goal_graph_error",
                {
                    "reason": reason,
                    "raw_response": getattr(self, "_last_decomposition_raw", None),
                },
            )
        except Exception:
            pass  # diagnostics are best-effort; never fail the run for them

    def _canonical_goal_graph(self, intent: dict, fallback_reason: str = "") -> dict:
        """Deterministic discover -> execute -> validate DAG from the intent.

        The offline fallback for :meth:`_build_goal_graph`: a decomposition that
        every property/simulation task shares -- pick a method, run it, validate
        the output against the acceptance metrics. Used when no agent is available
        or the agent's decomposition cannot be validated. ``fallback_reason``, when
        supplied, is recorded in the graph's rationale so a canonical graph emitted
        despite a live agent is traceable to the failure that caused it.
        """
        objective = intent.get("objective") or "the requested computation"
        # A metric can carry no target (the researcher gave no number). Rendering
        # that as "within None of None" would state a criterion the graph cannot
        # judge, so say what is actually going to happen to it instead.
        acceptance = [
            (f"{m.get('metric_name')} within {m.get('tolerance')} "
             f"of {m.get('target_value')}")
            if m.get("target_value") is not None and m.get("tolerance") is not None
            else f"{m.get('metric_name')} reported for review (no target specified)"
            for m in intent.get("acceptance_metrics", [])
            if isinstance(m, dict) and m.get("metric_name") is not None
        ]
        goals = [
            {
                "id": "discover_method",
                "category": "discovery",
                "purpose": f"Select a computational method capable of: {objective}",
                "owner_agent": "method_discovery",
            },
            {
                "id": "run_execution",
                "category": "execution",
                "purpose": f"Execute the selected method to address: {objective}",
                "owner_agent": "runtime_orchestrator",
            },
            {
                "id": "validate_results",
                "category": "validation",
                "purpose": "Validate outputs against the acceptance criteria",
                "owner_agent": "cross_validation",
                "acceptance_criteria": acceptance,
            },
        ]
        edges = [
            {"source": "discover_method", "target": "run_execution", "category": "seq"},
            {"source": "run_execution", "target": "validate_results", "category": "seq"},
        ]
        rationale = "Canonical discover -> execute -> validate decomposition."
        if fallback_reason:
            rationale += f" ({fallback_reason})"
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_intent_id": self.run_id,
            "rationale": rationale,
        }
        return {"goals": goals, "edges": edges, "metadata": metadata}

    def _library_importable(self, name: str) -> Optional[bool]:
        """Whether the registry library ``name`` is installed in the run interpreter.

        Discovery/planning and plain library runs all happen in the default env,
        so a library that can't be imported here can't actually run. Maps the
        registry name to its top-level import module via ``dependency_inferencer``
        (``scikit-learn`` -> ``sklearn``, ``openff-toolkit`` -> ``openff.toolkit``,
        ...) and probes it. Returns True/False, or None when it genuinely can't
        tell -- callers treat None as "trust the ranking" rather than dropping a
        candidate on a probe glitch. Injectable via ``library_available``.

        Under Slurm execution the local probe is not enough: a conda-only
        library can be importable here (pixi installs it from conda-forge) yet
        unrunnable on the cluster, where jobs only get pre-provisioned envs +
        a pip fallback. :func:`_cluster_cannot_run` grounds against the
        declarative env specs (+ PyPI) and vetoes such candidates outright.
        """
        if self.execute_slurm and _cluster_cannot_run(name):
            return False
        if self._library_available is not None:
            return self._library_available(name)
        deps = _depinf.import_names(name)
        if not deps:
            return None
        return _module_importable(deps[0].import_name)

    def _library_tracker(self, intent: Optional[dict] = None):
        """This run's :class:`LibraryRequestTracker` (built once, on first need).

        Construction is deferred because a run that wants nothing unavailable
        should do no env/git/network work at all. Injectable via
        ``library_request_tracker`` so tests exercise the whole path offline.
        """
        if self._library_request_tracker is None:
            objective = intent.get("objective") if isinstance(intent, dict) else None
            self._library_request_tracker = _libreq.LibraryRequestTracker(
                run_id=self.run_id, objective=objective)
        return self._library_request_tracker

    def _request_library(self, library, *, source, reason="", alternative=None,
                         intent=None):
        """Record an ask for an uninstalled ``library`` (ledger + GitHub issue).

        The counterpart to the preset-library guarantee: the plan still uses only
        installed tools, but the wish is no longer silently discarded. Swallows
        every failure -- an install request must never be able to break planning.
        """
        if not library:
            return None
        try:
            return self._library_tracker(intent).record(
                library, source=source, reason=reason, alternative=alternative)
        except Exception as exc:  # noqa: BLE001 - recording is strictly best-effort
            logger.warning("Could not record library request '%s': %s", library, exc)
            return None

    @staticmethod
    def _requested_libraries(intent: dict) -> list:
        """Libraries the researcher named explicitly, from the IntentSpec."""
        raw = (intent or {}).get("requested_libraries") or []
        if isinstance(raw, str):  # a model that emitted a bare string
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x or "").strip()]

    def _installed_candidates(self, ranked):
        """Keep only ranked candidates whose library is installed here.

        This is the guarantee behind "a preset list of libraries that are all
        installed": discovery never commits to a library the run can't import.
        Candidates whose availability is unknown (probe -> None) are kept. Ranks
        are renumbered 1..n over the survivors so the top pick is always rank 1.
        Returns ``(kept, dropped_names, preempted_names)``, where *preempted* is
        the subset of dropped candidates that out-scored every survivor -- i.e.
        the tools discovery would have chosen had they been installed, which is
        what makes them worth an install request rather than just a note. If the
        filter would drop *everything* (e.g. probed in a bare environment) the
        original ranking is returned unchanged so planning is never stranded.
        """
        kept, dropped, preempted = [], [], []
        for c in ranked:
            # Probe by id: ids are the clean canonical keys (``openbabel``),
            # whereas display names ("Open Babel") don't always map to an import.
            if self._library_importable(c.entry.id) is False:
                dropped.append(c.entry.name)
                if not kept:  # nothing installed has out-ranked it yet
                    preempted.append(c.entry.name)
            else:
                kept.append(c)
        if not kept:
            return list(ranked), [], []
        for i, c in enumerate(kept, start=1):
            c.rank = i
        return kept, dropped, preempted

    def _installed_libraries(self, ranked, names):
        """Split library *names* into ``(installed, missing)``.

        The name-level counterpart to :meth:`_installed_candidates`, for lists that
        aren't scored candidates: the libraries an LLM named, or the ones the
        researcher asked for. Each name is resolved to a known candidate first so
        the probe sees the clean registry id; an unlisted name is probed as given.
        Order is preserved, and "unknown" (probe -> None) counts as installed, in
        line with the rest of the grounding.
        """
        installed, missing = [], []
        for name in names or []:
            cand = self._candidate_by_name(ranked, name)
            key = cand.entry.id if cand else name
            (missing if self._library_importable(key) is False else installed).append(name)
        return installed, missing

    @staticmethod
    def _prefer_requested(ranked, requested):
        """Move explicitly-requested (and installed) candidates to the front.

        The researcher's own choice outranks the score: when they name a preset
        library, discovery plans *with* it instead of merely acknowledging it.
        Requested names that aren't in the ranking are ignored here -- those are
        handled as install requests. Ranks are renumbered so the top pick is 1;
        the sort is stable, so score order is preserved within each group.
        """
        if not requested:
            return ranked
        wanted = [w.lower() for w in requested]

        def _priority(c):
            for i, w in enumerate(wanted):
                if c.entry.name.lower() == w or c.entry.id.lower() == w:
                    return i
            return len(wanted)

        promoted = sorted(ranked, key=_priority)
        for i, c in enumerate(promoted, start=1):
            c.rank = i
        return promoted

    def _sim_missing(self, import_names) -> set:
        """Which of ``import_names`` are NOT importable in the sim env.

        Calculator bundles run in the sim env, so a toolset library absent there
        (e.g. PySCF has no py3.11 sim build) would fail the bundle's smoke even
        though it's installed in the default env. Probes the sim interpreter once
        (batched, one subprocess); returns an empty set when it can't probe (no
        sim env built) so we never over-prune on an environment we can't see.
        Injectable via ``sim_available`` for hermetic tests.
        """
        names = [n for n in dict.fromkeys(import_names) if n]
        if not names:
            return set()
        if self._sim_available is not None:
            return set(self._sim_available(names))
        sim_py = pixi_env_python(SIM_ENV)
        if not sim_py:
            return set()
        code = (
            "import importlib.util, sys, json\n"
            "out = []\n"
            "for m in json.loads(sys.argv[1]):\n"
            "    try:\n"
            "        if importlib.util.find_spec(m) is None: out.append(m)\n"
            "    except Exception:\n"
            "        out.append(m)\n"
            "print(json.dumps(out))"
        )
        try:
            proc = subprocess.run([sim_py, "-c", code, json.dumps(names)],
                                  capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                return set(json.loads((proc.stdout or "").strip() or "[]"))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return set()

    def _runnable_toolset(self, libraries, driver):
        """Drop toolset libraries not importable in the sim (calculator-run) env.

        Returns ``(kept, dropped)``. Always keeps ``driver`` (the library the
        calculator is driven through) and never returns an empty toolset. This is
        the env-aware complement to :meth:`_installed_candidates` (which grounds
        against the default env where planning + plain runs happen): a calculator
        run executes in sim, so its whole toolset must import there -- e.g. a
        PySCF that the model bundled alongside an ASE+MatGL run is dropped because
        it has no sim build, while sim-present libraries (ASE, Pymatgen) stay.
        """
        imports = {lib: _depinf.import_names(lib)[0].import_name for lib in libraries}
        missing = self._sim_missing(list(imports.values()))
        if not missing:
            return libraries, []
        kept, dropped = [], []
        for lib in libraries:
            if imports[lib] in missing and lib.lower() != (driver or "").lower():
                dropped.append(lib)
            else:
                kept.append(lib)
        return (kept or libraries), dropped

    def _discovery_query(self, intent: dict) -> DiscoveryQuery:
        """Derive a discovery query (capability tags + input format) from the intent."""
        objective = (intent.get("objective") or "").lower()
        domain = (intent.get("domain") or "").lower()

        # Capability-tag vocabulary + input formats are data-driven (see
        # configs/discovery_intent_map.json). Electronic-structure asks (band gap,
        # DOS, ...) are listed first there so they lead the tag order.
        cfg = _intent_map()
        tags = [tag for tag, needles in cfg["capability_keywords"].items()
                if any(n in objective for n in needles)]
        domain_tag = cfg.get("domain_tags", {}).get(domain)
        if domain_tag:
            tags.append(domain_tag)
        if not tags:
            tags.append(cfg["fallback_tag"])
        tags = list(dict.fromkeys(tags))  # de-dupe, keep order

        # Map the system representation onto the driver-library input format the
        # registry scores against: periodic solids are consumed as CIF (the format
        # ASE/Pymatgen/quacc declare), discrete molecules as SMILES.
        input_format = None
        fmt_by_kind = cfg.get("input_formats_by_kind", {})
        sysd = intent.get("system_descriptors") or {}
        kind = self._system_kind(intent)
        if kind in ("crystal", "surface"):
            input_format = fmt_by_kind.get(kind)
        elif (sysd.get("molecule") or {}).get("SMILES"):
            input_format = fmt_by_kind.get("molecule")
        return DiscoveryQuery(capability_tags=tags, input_format=input_format)

    def _requested_property(self, intent: dict) -> Optional[str]:
        """Canonical property the researcher wants (or None if none is recognized).

        Blends the free-text objective with the acceptance-metric names so a spec
        that says "band gap" in either place resolves to ``band_gap``. This is
        what tells the planner whether the run needs a dedicated calculator (a DFT
        engine for band structure) versus a plain library run.
        """
        parts = [str(intent.get("objective") or "")]
        for metric in intent.get("acceptance_metrics", []) or []:
            if isinstance(metric, dict) and metric.get("metric_name"):
                parts.append(str(metric["metric_name"]))
        return canonical_property(" ".join(parts))

    def _primary_goal_id(self) -> str:
        """Resolve the goal id the plan targets (the execution goal), with fallback."""
        goal = self._primary_goal()
        if goal is not None:
            return goal["id"]
        return f"goal-{self.run_id}"

    def _primary_goal(self) -> Optional[dict]:
        """The goal the plan targets: the execution goal, else the first goal."""
        graph = self._load_artifact("goal_graph")
        if not graph or not graph.get("goals"):
            return None
        goals = graph["goals"]
        return next((g for g in goals if g.get("category") == "execution"), goals[0])

    @staticmethod
    def _describe_system(sd: dict) -> str:
        """Human label for the target material, e.g. 'Ag (Silver), fcc crystal'."""
        if not sd:
            return "the target system"
        crystal = sd.get("crystal") or {}
        formula = sd.get("formula") or crystal.get("formula")
        name = crystal.get("name") or sd.get("name")
        label = formula or name or "the target system"
        if name and formula and name.lower() != formula.lower():
            label = f"{formula} ({name})"
        qualifiers = " ".join(x for x in [crystal.get("phase"), sd.get("kind")] if x)
        if qualifiers and label != "the target system":
            return f"{label}, {qualifiers}"
        return label

    def _compose_plan_summary(
        self, intent: dict, requested_property: Optional[str],
        libraries: list, calc_entry, recommendation,
    ) -> str:
        """Plain-language description of what this run will do (for the approval gate)."""
        prop = requested_property
        if not prop:
            for metric in intent.get("acceptance_metrics", []) or []:
                if isinstance(metric, dict) and metric.get("metric_name"):
                    prop = metric["metric_name"]
                    break
        prop = prop or "the requested property"

        system = self._describe_system(intent.get("system_descriptors") or {})
        toolset = " + ".join(libraries) if libraries else "the selected tools"
        calc = f" with the {calc_entry.name} calculator" if calc_entry is not None else ""

        parts = [f"Compute {prop} for {system} using {toolset}{calc}."]
        goal = self._primary_goal()
        purpose = (goal or {}).get("purpose")
        if purpose:
            parts.append(f"Goal: {purpose}")
        reasoning = getattr(recommendation, "reasoning", None) if recommendation is not None else None
        if reasoning:
            parts.append(f"Approach: {reasoning}")
        return " ".join(parts)

    def decompose(self) -> State:
        """Turn the clarified IntentSpec into a validated GoalGraph artifact.

        Decomposes the intent into a goal DAG (agent-driven when an agent is
        configured, deterministic otherwise), structurally validates it
        (GoalGraph) and confirms it is acyclic with referenced edges
        (GraphBuilder), then persists it as the ``goal_graph`` artifact for the
        discovery stage.
        """
        intent = self._load_artifact("intent_spec")
        if intent is None:
            return State.INTAKE

        graph_dict = self._build_goal_graph(intent)
        GraphBuilder.validate(GoalGraph(**graph_dict))  # raises on a bad/cyclic graph
        self.context.artifacts["goal_graph"] = self._write_artifact("goal_graph", graph_dict)
        return State.DISCOVER

    def discover(self) -> State:
        """Rank registry tools for the intent and persist the candidate slate.

        Scores every catalogued method against the derived discovery query and
        writes the top-ranked candidates (with a human-readable rationale) as the
        ``discovery`` artifact for plan synthesis. Candidates that out-ranked
        everything installed are recorded as ``LibraryAddition`` install requests
        and reported on the slate, so the filtering is visible rather than silent.
        """
        intent = self._load_artifact("intent_spec")
        if intent is None:
            return State.PLAN

        query = self._discovery_query(intent)
        ranked = rank_candidates(RegistryLoader().entries(), query, top_k=None)
        # Only surface candidates that are actually installed here, then take the
        # top 3 -- the slate the researcher sees is one they can really run.
        ranked, dropped, preempted = self._installed_candidates(ranked)
        ranked = ranked[:3]
        # A dropped candidate that out-scored every installed one is a tool
        # discovery genuinely wanted: ask for it to be installed.
        requests = [
            self._request_library(
                name, source=_libreq.SOURCE_RANKING, intent=intent,
                reason=(f"top-ranked for capability tags "
                        f"{', '.join(query.capability_tags) or '(none)'}"),
                alternative=(ranked[0].entry.name if ranked else None))
            for name in preempted
        ]
        artifact = {
            "query": {
                "capability_tags": query.capability_tags,
                "input_format": query.input_format,
            },
            # What the ranking wanted but this environment can't run, so the slate
            # is never mistaken for the whole field.
            "unavailable_candidates": dropped,
            "library_requests": [r.as_dict() for r in requests if r],
            "candidates": [
                {
                    "rank": c.rank,
                    "id": c.entry.id,
                    "name": c.entry.name,
                    "version": c.entry.version,
                    "composite": round(c.composite, 4),
                    "components": c.components.as_dict(),
                }
                for c in ranked
            ],
            "rationale": explain_ranking(ranked),
        }
        self.context.artifacts["discovery"] = self._write_artifact("discovery", artifact)
        return State.PLAN

    def plan(self) -> State:
        """Synthesize a complete ExecutionPlan by choosing a toolset for the task.

        The choice is made by **LLM discovery** when an agent is available: the
        model reasons over the candidate tools (the registry + calculator catalog
        as a seed, with factual metadata like ``needs_external_data``), the target
        platform, and practical fitness, and picks a library (+ optional python
        calculator). A deterministic availability check grounds the pick so an
        unrunnable tool (e.g. GPAW on a Mac) is never committed. When no agent is
        wired, or the LLM's pick can't be grounded, it falls back to the
        deterministic, platform-aware registry ranking (:meth:`_select_toolset`).
        The plan carries the full toolset + target material + property; researcher
        approval (the PLAN->BUILD guard) is a separate gate this handler doesn't set.

        Whatever gets filtered out for not being installed -- a tool the LLM named,
        one that out-ranked every installed candidate, or one the researcher asked
        for by name -- is recorded as a ``LibraryAddition`` install request and
        reported in ``safety_notes``. The plan itself still uses installed
        libraries only; the request is how the wish reaches whoever maintains the
        environment.
        """
        intent = self._load_artifact("intent_spec")
        if intent is None:
            return State.BUILD

        query = self._discovery_query(intent)
        entries = RegistryLoader().entries()
        ranked = rank_candidates(entries, query, top_k=None)  # full ranking
        if not ranked:
            return State.BUILD
        # Engines this deployment's cluster can't run, noted BEFORE the veto
        # filters them out: the researcher deserves to hear "your best-fit
        # engine exists but isn't provisioned here" on the approval card (and
        # can then ask the team to provision it via a GitHub issue), rather
        # than a silent substitution. Only candidates that OUTRANK the best
        # runnable one are noted -- anything ranked below it would have lost
        # anyway, and naming it is just noise.
        cluster_blocked = []
        if self.execute_slurm:
            for c in ranked:
                if not _cluster_cannot_run(c.entry.id):
                    break
                cluster_blocked.append(c.entry.name)
        # Ground the toolset in what's installed: drop any candidate library that
        # isn't importable in the run interpreter, so both the deterministic pick
        # (ranked[0]) and the LLM's candidate slate are guaranteed runnable here.
        ranked, dropped_uninstalled, preempted_uninstalled = self._installed_candidates(ranked)
        # Cluster-vetoed names get their own dedicated note below; keep them out
        # of the generic "not installed" note so the reason stays truthful. They
        # are kept out of the install requests for the same reason: the library is
        # not missing from the preset set, it just has no provisioned cluster env,
        # so a 'LibraryAddition' issue asking to install it would be wrong.
        dropped_uninstalled = [n for n in dropped_uninstalled
                               if n not in cluster_blocked]
        preempted_uninstalled = [n for n in preempted_uninstalled
                                 if n not in cluster_blocked]

        # Software the researcher named explicitly. An installed one is honoured --
        # promoted to the top of the ranking and offered to the LLM as a preference
        # -- so a direct ask actually decides the toolset. One that isn't installed
        # can't be used (the preset rule still wins for this run), so it becomes an
        # install request below.
        honoured_requests, missing_requests = self._installed_libraries(
            ranked, self._requested_libraries(intent))
        ranked = self._prefer_requested(ranked, honoured_requests)

        requested_property = self._requested_property(intent)
        domain = (intent.get("domain") or "").lower() or None
        # Plan against the richest platform we can actually reach: with a Docker
        # daemon up, that's linux-64 (the runner image), so a higher-fidelity
        # Linux-only engine (e.g. GPAW) is selectable on a Mac and its run is
        # routed into the container at EXECUTE. Without Docker it's the host, so
        # the best *native* engine is chosen. host_platform drives the routing note.
        host_platform = current_platform()
        platform = planning_platform(host_platform, docker=docker_available())

        # Libraries TWAIN wanted but can't import. Collected here and recorded once
        # the toolset is final, so every request can name the installed library that
        # was used instead (and so one library asked for twice is one request).
        pending_requests = [
            (name, _libreq.SOURCE_USER, "named explicitly by the researcher")
            for name in missing_requests
        ] + [
            (name, _libreq.SOURCE_RANKING,
             f"out-ranked every installed candidate for capability tags "
             f"{', '.join(query.capability_tags) or '(none)'}")
            for name in preempted_uninstalled
        ]

        recommendation = self._llm_recommend(intent, ranked, requested_property, domain,
                                             platform, requested_libraries=honoured_requests)
        if recommendation is not None:
            # Ground every library the model named -- not just the primary, since an
            # uninstalled supporting library would fail the bundle just as surely.
            installed_named, missing_named = self._installed_libraries(
                ranked, recommendation.libraries)
            why = "selected by discovery for this task" + (
                f": {recommendation.reasoning}" if recommendation.reasoning else "")
            pending_requests += [(n, _libreq.SOURCE_LLM, why) for n in missing_named]
            driver = (recommendation.calculator_library or "").lower()
            # Resolve the model's primary library to a known candidate so the
            # cluster veto is reported under its clean registry name; an
            # unlisted/invented name falls through as-is.
            picked = self._candidate_by_name(ranked, recommendation.libraries[0])
            primary_key = picked.entry.id if picked else recommendation.libraries[0]
            rec_calc = find_calculator(recommendation.calculator)
            if not installed_named or installed_named[0].lower() != recommendation.libraries[0].lower():
                # The model's *primary* isn't installed here -- don't plan around
                # something the run can't import; fall back to the deterministic,
                # installed pick. When the block was the CLUSTER veto (not a local
                # install gap), that's the model's best-fit engine being passed
                # over: note it for the researcher.
                if self.execute_slurm and _cluster_cannot_run(primary_key):
                    cluster_blocked.append(
                        picked.entry.name if picked else recommendation.libraries[0])
                recommendation = None
            elif (rec_calc is not None and self.execute_slurm
                  and _cluster_cannot_run(rec_calc.id)):
                # The model attached a calculator the cluster can't run (conda-
                # only, not in any provisioned env spec); fall back to the
                # deterministic pick, which filters those out.
                cluster_blocked.append(rec_calc.name)
                recommendation = None
            elif driver and driver in {n.lower() for n in missing_named}:
                # The calculator can't be driven without its driver library, so the
                # whole recommendation is unrunnable, not just trimmable.
                recommendation = None
            else:
                # Primary (and driver) are fine; carry on without the missing extras.
                recommendation.libraries = installed_named
        if recommendation is not None:
            libraries = recommendation.libraries
            primary = self._candidate_by_name(ranked, libraries[0]) or ranked[0]
            calc_entry = find_calculator(recommendation.calculator)
            calculator_library = recommendation.calculator_library
            selection_note = ("Discovery (LLM) chose " + " + ".join(libraries)
                              + (f" with {recommendation.calculator}" if recommendation.calculator else "")
                              + (f": {recommendation.reasoning}" if recommendation.reasoning else ""))
        else:
            libraries, calc_entry, calculator_library, blocked_calcs = (
                self._select_toolset(
                    ranked, requested_property, domain, platform=platform))
            cluster_blocked.extend(blocked_calcs)
            primary = ranked[0]
            selection_note = None

        # A calculator run executes in the sim env, so its toolset must import
        # THERE, not just in the default env the candidate grounding checked. Drop
        # any library with no sim build (e.g. a PySCF the model bundled alongside
        # an ASE+MatGL run) so it can't fail the bundle's smoke; keep the driver
        # and re-point the primary if it was the one dropped.
        dropped_unrunnable = []
        if calc_entry is not None:
            driver = calculator_library or calc_entry.driver_library
            libraries, dropped_unrunnable = self._runnable_toolset(libraries, driver)
            if dropped_unrunnable and primary.entry.name in dropped_unrunnable:
                primary = self._candidate_by_name(ranked, libraries[0]) or primary
        else:
            # A plan that promises a CALCULATED property but attaches no engine
            # cannot produce it, and the gap does not stay quiet: codegen fills it
            # by inventing a calculator, which then is not in requirements.txt and
            # is absent at run time. A NaCl2 heat-of-formation planned as Pymatgen
            # with calculator=null reached EXECUTE and died on "No module named
            # matgl" after trying MACE, CHGNet and M3GNet (Slurm job 2633871).
            #
            # Refused here rather than repaired later: BUILD's static check can stop
            # the script computing, but it cannot conjure the engine this plan
            # needed, so the honest outcome is to say the plan is not runnable
            # before anything is approved, built or queued. Which capabilities count
            # is config, not code (configs/discovery_intent_map.json), so adding a
            # calculator-free way to answer one is a data change.
            #
            # "No calculator" is only fatal when the toolset is DRIVERS. Some
            # libraries are engines in their own right -- PySCF and Psi4 compute an
            # electronic structure themselves and need nothing attached -- while ASE
            # and Pymatgen are the things calculators plug INTO, which is exactly
            # what the registry's driver_library field records. Capability tags
            # cannot tell them apart: ASE also claims electronic_structure, and
            # believing it would refuse a legitimate self-contained PySCF band gap.
            needs_engine = sorted(
                set(query.capability_tags)
                & set(_intent_map().get("capabilities_requiring_a_calculator") or []))
            # The tag vocabulary does not cover every calculated property: an
            # objective naming a heat of formation, or a bulk modulus, matches no
            # keyword and falls back to property_prediction, which is deliberately
            # NOT engine-requiring (a descriptor or a lookup lives there). So also
            # ask the detector codegen itself uses -- it fired for the NaCl2 run,
            # which is why that bundle carried twain_thermo.py.
            if wants_thermo_cycle(requested_property, _first_metric_name(
                    {"acceptance_metrics": intent.get("acceptance_metrics") or []}),
                    intent.get("objective")):
                needs_engine = needs_engine or ["a multi-species energy cycle"]
            # RETRIEVING a value needs no engine, and TWAIN can now do that (the
            # Materials Project route). Refusing a lookup because the toolset has no
            # calculator would be refusing the correct plan.
            if mp_lookup_requested(intent.get("objective")):
                needs_engine = []
            drivers = {str(c.driver_library).strip().lower()
                       for c in load_calculators() if c.driver_library}
            self_contained = [lib for lib in libraries
                              if str(lib).strip().lower() not in drivers]
            if needs_engine and not self_contained:
                raise _config_error(
                    f"the plan needs a calculator it does not have: "
                    f"'{requested_property or query.capability_tags[0]}' is answered by "
                    f"an atomistic calculation ({', '.join(needs_engine)}), but "
                    f"discovery attached no engine and the toolset is libraries only "
                    f"({', '.join(libraries) or 'none'}).",
                    hint=("No provisioned engine matched this request. Either name a "
                          "calculator the cluster has (see 'What TWAIN Can Run' on the "
                          "home screen), ask for a quantity the libraries can answer "
                          "directly, or retrieve the value from a database instead of "
                          "computing it."))

        execution_plan = PlanSynthesizer().synthesize(
            candidate=primary,
            goal_id=self._primary_goal_id(),
            acceptance_metrics=intent.get("acceptance_metrics", []),
            requested_capability=query.capability_tags[0],
        )
        execution_plan.selected_method.libraries = libraries
        if selection_note:
            execution_plan.safety_notes.append(selection_note)
        # Candidates filtered for not being installed. Any of them that TWAIN
        # actually wanted gets its own, fuller note below (with its install
        # request), so it is left out here rather than mentioned twice.
        asked_for = {_libreq.canonical_name(n) for n, _s, _r in pending_requests}
        merely_skipped = [n for n in dropped_uninstalled
                          if _libreq.canonical_name(n) not in asked_for]
        if merely_skipped:
            execution_plan.safety_notes.append(
                "Discovery skipped candidate(s) not installed in the run "
                "environment: " + ", ".join(merely_skipped))
        if cluster_blocked:
            blocked = list(dict.fromkeys(cluster_blocked))
            picked_desc = calc_entry.name if calc_entry is not None else libraries[0]
            execution_plan.safety_notes.append(
                f"{ENGINE_UNAVAILABLE_PREFIX}{', '.join(blocked)} would fit this "
                f"request but cannot run on this deployment's cluster -- the "
                f"package(s) are not in any provisioned environment and pip "
                f"cannot install them there. Proceeding with {picked_desc} "
                f"instead. If you need {blocked[0]}, submit a GitHub issue "
                f"asking the team to provision it (an env spec in "
                f"scripts/ris/envs/ plus one provision_envs.sh run); otherwise "
                f"approving this plan runs {picked_desc}.")
        if dropped_unrunnable:
            execution_plan.safety_notes.append(
                f"Dropped from the toolset (no build in the '{SIM_ENV}' environment "
                f"where the calculator runs): {', '.join(dropped_unrunnable)}")

        # The toolset is settled, so every library TWAIN wanted but couldn't use can
        # now be recorded against the installed library that replaced it. Each ask
        # is ledgered, filed as a 'LibraryAddition' GitHub issue (deduplicated, so a
        # recurring wish is one issue), and reported to the researcher right here on
        # the plan -- the run itself stays on the preset libraries.
        library_requests, seen_requests = [], set()
        for name, source, reason in pending_requests:
            key = _libreq.canonical_name(name)
            if key in seen_requests:
                continue
            seen_requests.add(key)
            req = self._request_library(name, source=source, reason=reason,
                                        alternative=libraries[0], intent=intent)
            if req is not None:
                library_requests.append(req)
        if library_requests:
            execution_plan.library_requests = [r.as_dict() for r in library_requests]
            execution_plan.safety_notes.extend(r.note() for r in library_requests)

        if calc_entry is not None:
            execution_plan.selected_method.calculator = calc_entry.name
            execution_plan.selected_method.calculator_import = calc_entry.import_name
            execution_plan.selected_method.calculator_library = calculator_library or calc_entry.driver_library
            note = (f"Toolset {' + '.join(libraries)} with calculator {calc_entry.name} "
                    f"(driven via {execution_plan.selected_method.calculator_library}) "
                    f"for property '{requested_property}'")
            if calc_entry.heavy:
                note += " (heavy run -- confirm before executing)"
                # The generic 10-minute wall default gets a real DFT run killed
                # at the short partition's limit; give heavy calculators room
                # (still editable on the approval card). max_time is hours.
                from plan_synthesizer.plan_synthesizer import HEAVY_WALL_MINUTES
                heavy_hours = HEAVY_WALL_MINUTES / 60.0
                if execution_plan.slurm_request.max_time < heavy_hours:
                    execution_plan.slurm_request.max_time = heavy_hours
                    rationale = dict(execution_plan.slurm_rationale or {})
                    rationale["max_time"] = (
                        f"TWAIN's suggestion: {heavy_hours:g} h, raised from the "
                        f"generic default because {calc_entry.name} is a heavy "
                        f"calculation that the short-queue limit would kill. "
                        f"Lower it if you know this run is quick."
                    )
                    execution_plan.slurm_rationale = rationale
            if calc_entry.needs_external_data:
                note += " (needs external parameter data to run)"
            execution_plan.safety_notes.append(note)
            if calc_entry.needs_docker(host_platform):
                # Non-native engine (e.g. GPAW on a Mac): the run is offloaded to
                # the linux-64 runner container. Say so explicitly -- the choice
                # is never silent (the substitution problem the user hit before).
                execution_plan.safety_notes.append(
                    f"{calc_entry.name} has no {host_platform} build; it will run in the "
                    f"linux-64 Docker container (image '{DEFAULT_DOCKER_IMAGE}', linux/amd64 "
                    f"emulation -- slower than native). Build it first if needed: "
                    f"`docker build --platform linux/amd64 -f runner/Dockerfile "
                    f"-t {DEFAULT_DOCKER_IMAGE} .` (see runner/README.md).")
            if calc_entry.ml_surrogate:
                # The researcher wants real calculations, not predictions: make it
                # unmistakable when the only available engine is an ML surrogate.
                execution_plan.safety_notes.append(
                    f"NOTE: {calc_entry.name} is an ML surrogate -- it PREDICTS "
                    f"'{requested_property}' from a trained model, not a "
                    f"first-principles calculation. For a real result, run on a "
                    f"platform with a genuine engine (e.g. linux-64 uses GPAW DFT).")
        elif requested_property:
            # No calculator was attached. Say why: either none is catalogued for
            # the property, or one exists but has no build for this platform
            # (e.g. GPAW on a Mac) -- so discovery didn't pick an unrunnable tool.
            anywhere = calculators_for_property(requested_property, domain=domain, platform=None)
            if anywhere:
                execution_plan.safety_notes.append(
                    f"A calculator for '{requested_property}' exists ({anywhere[0].name}) but has "
                    f"no build for this platform ({host_platform}); using {libraries[0]} alone -- run on a "
                    f"supported platform (e.g. linux-64) to use it.")

        # Accuracy-first: if Docker is down we planned natively, which may be a
        # lower-fidelity engine than a Linux-only one we could reach via Docker.
        # Surface that so the downgrade is explicit and actionable, never silent.
        if requested_property and not docker_available():
            best = calculators_for_property(requested_property, domain=domain,
                                            platform=DOCKER_LINUX_PLATFORM)
            chosen_id = calc_entry.id if calc_entry is not None else None
            if best and best[0].id != chosen_id and best[0].needs_docker(host_platform):
                execution_plan.safety_notes.append(
                    f"Higher fidelity available: {best[0].name} (Linux-only) would compute "
                    f"'{requested_property}' at higher fidelity than the native choice"
                    + (f" ({calc_entry.name})" if calc_entry is not None else "")
                    + f". Install & start Docker, build the '{DEFAULT_DOCKER_IMAGE}' image "
                    f"(runner/README.md), and TWAIN will run it in the linux-64 container.")
        execution_plan.target_system = intent.get("system_descriptors") or None
        execution_plan.requested_property = requested_property
        # Suggest a CPU count from the system size, and say so on the card. This
        # is a starting point, not a rule: cores-per-atom is a rough proxy for
        # how much parallelism a calculation can use, so the ratio is tunable
        # (TWAIN_CORES_PER_ATOM) and the result is rounded to a width that
        # divides a domain decomposition sensibly rather than landing on an
        # awkward count like 21. Floored at 2 (k-point/domain parallelism needs a
        # partner), capped at the node's cores, and editable on the approval card.
        atoms = _atom_count(intent.get("system_descriptors"))
        if atoms:
            max_cpus = _cluster_node_limits().get("cpu_count") or 64
            periodic = _is_periodic(intent.get("system_descriptors"))
            # Registry-declared: a "threads" calculator gains nothing from extra
            # ranks, so widening its request would only idle allocated cores.
            parallelism = getattr(calc_entry, "parallelism", "threads") or "threads"
            scales_with_ranks = parallelism in ("interpreter", "engine")
            cores = _suggest_cpu_count(atoms, max_cpus, periodic=periodic,
                                       scales_with_ranks=scales_with_ranks)
            execution_plan.slurm_request.cpu_count = cores
            if periodic and scales_with_ranks and cores > atoms:
                why = (
                    f"TWAIN's suggestion: {cores} cores. This is a periodic cell run "
                    f"by {parallelism}-parallel {getattr(calc_entry, 'name', 'the calculator')}, "
                    f"so the cores go to k-points rather than to atoms -- and a small "
                    f"cell needs a DENSE k-mesh, which is why {atoms} formula atoms do "
                    f"not mean a small job. Capped at the node's {max_cpus}. Change it "
                    f"freely; fewer cores mostly just makes it slower."
                )
            else:
                why = (
                    f"TWAIN's suggestion: {cores} cores for a {atoms}-atom system "
                    f"(about {_cores_per_atom():g} per atom, rounded to a "
                    f"parallel-friendly width, capped at the node's {max_cpus}). "
                    f"A rough proxy for available parallelism -- change it freely."
                )
            execution_plan.slurm_rationale = {"cpu_count": why}
            # compute_estimate was derived inside synthesize() from the DEFAULT core
            # count, and cpu_count/max_time have both been rewritten since. Left
            # alone it reports the cost of an allocation nobody asked for -- the
            # budget and the approval card would quote different jobs.
            execution_plan.compute_estimate.cpu_hours = round(
                cores * float(execution_plan.slurm_request.max_time), 4)
        # A Materials Project retrieval is credential-gated: without MP_API_KEY
        # in the runner's environment the generated lookup script cannot run.
        # Say so ON THE APPROVAL CARD, before any build or queue time is spent.
        if (mp_lookup_requested(intent.get("objective"))
                and not os.environ.get("MP_API_KEY")):
            execution_plan.safety_notes.append(
                "The objective asks to RETRIEVE data from the Materials Project, "
                "but MP_API_KEY is not set in the runner's environment -- the "
                "lookup will fail until it is added to the deployment's .env "
                "(free key: https://materialsproject.org/api).")
        # Does the requested compound exist? A heat of formation was planned for
        # NaCl2 -- sodium is monovalent, so the compound does not exist and no
        # calculation of it means anything (session 96926868). Materials Project is
        # the authority TWAIN already talks to, and zero entries there is a strong
        # hint: measured, NaCl2 returns nothing while CaPt2, FeAl and Ni3Al all
        # return something.
        #
        # A NOTE, never a veto. MP is not exhaustive and holds hypothetical phases
        # (NaCl3 has entries), so absence is evidence, not proof -- and the
        # researcher may be studying something genuinely new. Crystals only: MP is
        # not the right authority for a molecule. The obvious alternative, pymatgen
        # oxidation-state guessing, is unusable -- it calls CaPt2, FeAl and Ni3Al
        # implausible too, so it would have blocked the CaPt2 run that later
        # validated against MP to 1.4%.
        sysd = intent.get("system_descriptors")
        formula = (CodegenEngine._material_brief(
            {"target_system": sysd}, intent) or {}).get("formula")
        if formula and _is_periodic(sysd):
            if mp_reference.formula_is_known(formula) is False:
                execution_plan.safety_notes.append(
                    f"The Materials Project has no entry for {formula}. That is not "
                    f"proof it cannot exist -- MP is not exhaustive, and a genuinely "
                    f"new composition would look the same -- but it is worth checking "
                    f"the formula before spending an allocation, because a "
                    f"calculation on a composition that cannot form returns a number "
                    f"with nothing behind it.")
        execution_plan.summary = self._compose_plan_summary(
            intent, requested_property, libraries, calc_entry, recommendation)

        if self.execute_slurm:
            # The whole toolset must fit ONE cluster env (+ pip): each library can
            # pass the per-library veto alone yet no env holds them together.
            # Refuse here, before the researcher approves an allocation for a job
            # that can only die in pip (#169).
            toolset = _selected_toolset(asdict(execution_plan))
            if cluster_env_candidates(toolset) is None:
                raise _config_error(
                    "no RIS cluster environment can run this plan: "
                    + _cluster_env_gap(toolset),
                    "Provision or extend a shared env so one env provides these "
                    "packages together (scripts/ris/envs/*.yml + "
                    "scripts/ris/provision_envs.sh), then rerun.")
        self.context.artifacts["execution_plan"] = self._write_artifact(
            "execution_plan", asdict(execution_plan))
        self._revoke_approval_if_plan_changed()
        return State.BUILD

    def _revoke_approval_if_plan_changed(self) -> None:
        """Withdraw a standing approval when this plan is not the approved one.

        A re-plan can land on a different method, and running that on the old
        decision spends the researcher's compute on a plan they never saw. But it
        usually lands on the SAME method, and re-asking then was doubling the
        approval card on every rejected run -- so only a real change re-opens it.
        """
        if not self.context.plan_approved:
            return
        current = self._plan_fingerprint()
        if current is None:
            return
        if self.context.approved_plan is None:
            # The approval was seeded rather than recorded (the runner seeds one
            # for unattended runs, and checkpoints written before this field
            # existed carry none), so there is nothing to compare against.
            # Adopt this plan as the approved one: revoking a decision we cannot
            # show has been invalidated would strand the run at the gate.
            self.context.approved_plan = current
            return
        if current == self.context.approved_plan:
            return
        self.context.plan_approved = False
        self.context.approved_plan = None
        logger.info("[plan] the re-planned method or resources differ from what "
                    "was approved; the new plan needs approval before it runs.")

    @staticmethod
    def _candidate_by_name(ranked, name):
        """The ScoredCandidate for a library name (or None)."""
        want = (name or "").lower()
        for c in ranked:
            if c.entry.name.lower() == want or c.entry.id.lower() == want:
                return c
        return None

    def _llm_recommend(self, intent, ranked, requested_property, domain, platform,
                       requested_libraries=None):
        """Ask the LLM to pick a toolset, grounded by platform availability.

        Returns a ToolRecommendation, or None (no agent wired, or the pick
        couldn't be grounded) so ``plan()`` uses the deterministic path. Uses the
        raw injected agent -- never triggers a network build of AgentInterface
        just to plan, so offline planning tests stay offline.
        """
        if self._agent is None:
            return None
        library_candidates = [
            {"name": c.entry.name, "capabilities": list(c.entry.capability_tags),
             "description": c.entry.description}
            for c in ranked
        ]
        calc_candidates = [
            {"name": e.name, "id": e.id, "import_name": e.import_name,
             "pip_name": e.pip_name, "driver_library": e.driver_library,
             "capabilities": list(e.capabilities), "platforms": list(e.platforms),
             "needs_external_data": e.needs_external_data,
             "ml_surrogate": e.ml_surrogate, "description": e.description}
            for e in load_calculators()
        ]
        sysd = intent.get("system_descriptors") or {}
        molecule = sysd.get("molecule") or {} if isinstance(sysd, dict) else {}
        material = (sysd.get("formula") if isinstance(sysd, dict) else None) or molecule.get("name") or ""
        try:
            return llm_discovery.recommend_toolset(
                objective=intent.get("objective", ""), material=material, domain=domain,
                requested_property=requested_property, platform=platform,
                libraries=library_candidates, calculators=calc_candidates,
                requested_libraries=requested_libraries,
                agent=self._agent_text,
            )
        except Exception:  # noqa: BLE001 - any failure -> deterministic fallback
            return None

    def _select_toolset(self, ranked, requested_property, domain, platform=None):
        """Assemble a compatible toolset from the discovery ranking.

        Returns ``(libraries, calculator_entry_or_None,
        calculator_library_or_None, cluster_blocked_calculator_names)``.
        ``libraries[0]`` is the primary (discovery's #1). If the property needs a
        calculator, the best covering one that is *available on this platform* is
        attached: preferring a calculator compatible with the primary, else
        bringing in a bridging library the calculator *is* compatible with
        (preferring one discovery also ranked), so multiple libraries can be used
        together. Fully data-driven -- no tool is forced, and a calculator with no
        build for the current platform (e.g. GPAW on a Mac) is never chosen.
        ``platform`` defaults to the current platform.

        Under Slurm execution, a calculator whose packages pip can't install
        and no cluster env spec provides (e.g. QE/Abinit binaries) would die
        in the job's install step -- those are never attached. The ones that
        OUTRANKED the best runnable calculator (i.e. were passed over) are
        returned by name so the caller can tell the researcher on the
        approval card instead of substituting silently.
        """
        primary = ranked[0].entry
        libraries = [primary.name]
        if not requested_property:
            return libraries, None, None, []
        if platform is None:
            platform = current_platform()
        covering = calculators_for_property(requested_property, domain=domain, platform=platform)
        blocked = []
        if self.execute_slurm:
            # Report only calculators that outrank the best runnable one (they
            # are the "passed over" engines); anything below would lose anyway.
            for c in covering:
                if not _cluster_cannot_run(c.id):
                    break
                blocked.append(c.name)
            covering = [c for c in covering if not _cluster_cannot_run(c.id)]
        if not covering:
            return libraries, None, None, blocked

        # 1) a covering calculator compatible with the primary library
        for calc in covering:
            if calc.supports_library(primary.name):
                return libraries, calc, primary.name, blocked

        # 2) none compatible with the primary -> pair the best covering calculator
        #    with a library it supports (preferring one discovery ranked), and use
        #    both libraries together.
        calc = covering[0]
        bridge = self._pick_compatible_library(ranked, calc)
        if bridge and bridge.lower() != primary.name.lower():
            libraries.append(bridge)
        return libraries, calc, bridge, blocked

    @staticmethod
    def _pick_compatible_library(ranked, calc):
        """Name of a library ``calc`` can be driven through, preferring a ranked one."""
        compat = {c.lower() for c in (calc.compatible_libraries or [])}
        if not compat and calc.driver_library:
            compat = {calc.driver_library.lower()}
        for cand in ranked:
            if cand.entry.name.lower() in compat or cand.entry.id.lower() in compat:
                return cand.entry.name
        if calc.compatible_libraries:
            return calc.compatible_libraries[0]
        return calc.driver_library

    def build(self) -> State:
        """Generate a runnable RunBundle from the ExecutionPlan (Story 5.1).

        The CodegenEngine emits a self-contained bundle -- main.py, config.yaml,
        requirements.txt, inline_tests.py -- that the execution adapter can run
        without manual edits. For a plain library run this is deterministic,
        template-based codegen; when the plan selected a calculator (e.g. GPAW
        for a DFT band gap) the engine LLM-synthesizes a script tailored to the
        target material, falling back to a generic scaffold if synthesis is
        unavailable. Either way the material from the IntentSpec reaches the
        generated code, so the script computes the requested system rather than a
        hard-coded sample. BUILD only *generates* -- verifying and repairing the
        script is the next stage (REPAIR); the bundle directory and the main.py
        entrypoint are recorded as artifacts for it and for EXECUTE.
        """
        plan = self._load_artifact("execution_plan")
        if plan is None:
            # No plan to build from; no-op through REPAIR (which also no-ops with
            # no bundle) rather than raise, so this handler stays callable in
            # isolation / on a resume that skipped PLAN.
            return State.REPAIR
        intent = self._load_artifact("intent_spec")
        # Pass the intent + an agent callable so codegen can (a) build a script
        # for the actual material in the intent, and (b) LLM-synthesize a tailored
        # script when a calculator is selected (band gap -> ASE+GPAW). The agent is
        # only invoked on the calculator path; a plain library run stays offline
        # and deterministic, and any synthesis failure falls back to a template.
        # Code synthesis emits a whole main.py, so it needs a far larger output
        # budget than the gateway's small default -- otherwise the script is
        # truncated mid-statement (compiles, but has no runnable entrypoint, so it
        # produces no results). Verifying/repairing the script is the REPAIR
        # stage's job, so BUILD only generates.
        # Decide whether the generated --smoke check should run the property
        # computation for real (so a wrong API call/keyword is caught in REPAIR) or
        # just load the tool. A cheap, self-contained calculator (ML predictor /
        # semiempirical, no external data) can compute in smoke; a heavy or
        # external-data one (full DFT, needs pseudopotentials/SK files) cannot. A
        # library-only run (the library computes the property itself, e.g. PySCF) is
        # self-contained, so it computes in smoke too. A calculator may override the
        # heuristic via `smoke_can_compute` when it's heavy/external-data for a full
        # run yet can still do a cheap single-point smoke because TWAIN provisions
        # its data -- e.g. DFTB+ (semiempirical; .skf files fetched into DFTB_PREFIX).
        method = plan.get("selected_method") or {}
        calc_name = method.get("calculator")
        # The engine's own binary, when it is an external program driven through
        # ASE. None for a calculator that IS a python package (GPAW), where the
        # import check already proves the environment can run it.
        calc_executable = None
        # Which pseudopotential library the engine needs, when it ships none
        # (Quantum ESPRESSO, ABINIT). Set means the bundle carries twain_pseudo.py
        # and codegen resolves filenames + cutoffs through it.
        pseudo_library = None
        if calc_name:
            ce = find_calculator(calc_name)
            if ce is None:
                smoke_compute = False
            else:
                calc_executable = ce.executable
                pseudo_library = ce.pseudo_library
                if ce.smoke_can_compute is not None:
                    smoke_compute = ce.smoke_can_compute
                else:
                    smoke_compute = not ce.heavy and not ce.needs_external_data
        else:
            smoke_compute = True
        engine = CodegenEngine()
        tool = (method.get("calculator") or method.get("tool_name")
                or "the selected tool") if isinstance(method, dict) else "the selected tool"
        self._progress("BUILD", "codegen", "active",
                       f"Writing the simulation script for {tool}")
        try:
            bundle = engine.generate(
                plan, intent=intent,
                agent=lambda p: self._agent_text(p, max_tokens=_CODEGEN_MAX_TOKENS),
                smoke_compute=smoke_compute,
                # So the smoke gate rejects an env that has the ASE bindings but
                # not the engine binary, instead of the run dying mid-calculation
                # with "command not found" after a queue wait.
                calculator_executable=calc_executable,
                # So a pseudopotential filename is looked up in the installed
                # library rather than written from memory -- an invented .UPF name
                # is either a crash after a queue wait or, worse, a real file for
                # different physics.
                pseudo_library=pseudo_library,
                # Registry-declared: an engine that parallelises itself needs the
                # script to hand it TWAIN_ENGINE_LAUNCH, and makes 6N+1
                # finite-difference engine invocations worth warning about.
                parallelism=(ce.parallelism if calc_name and ce else "threads"),
                # A run that is going to EXECUTE must not fall back to the
                # placeholder scaffold: it loads the tool, writes a stub and
                # exits 0, so the job, the scheduler and TWAIN all report success
                # having computed nothing. Planning-only runs keep the fallback --
                # there the bundle is a deliverable to read, not to run.
                require_synthesis=(self.execute_locally or self.execute_slurm))
        except Exception:
            self._progress("BUILD", "codegen", "failed",
                           "Could not write a runnable script for this plan")
            raise
        finally:
            # Recorded either way: on success it says which attempt produced the
            # script, and on failure why each attempt was rejected -- the thing
            # that was missing when a scaffolded run reached the cluster.
            if engine.last_synthesis is not None:
                self.context.artifacts["codegen_report"] = self._write_artifact(
                    "codegen_report", engine.last_synthesis)
        synthesis = engine.last_synthesis or {}
        tries = synthesis.get("attempt")
        self._progress("BUILD", "codegen", "done",
                       "Script written" + (f" (attempt {tries})" if tries and tries > 1 else ""))
        bundle_dir = Path(self.artifacts_dir) / f"run_bundle_{self.run_id}"
        bundle.write(bundle_dir)
        self._progress("BUILD", "bundle", "done",
                       f"Run bundle assembled ({len(list(bundle_dir.iterdir()))} files)")
        self.context.artifacts["run_bundle"] = str(bundle_dir)
        self.context.artifacts["script"] = str(bundle_dir / bundle.entrypoint)
        return State.REPAIR

    def repair(self) -> State:
        """Verify and, if needed, repair the generated script before EXECUTE.

        A dedicated stage (its own state) that turns the *plausible* ``main.py``
        BUILD emitted into one that actually runs. It runs the
        :class:`code_gen.script_doctor.ScriptDoctor`, which (1) statically checks
        the script (compiles? has an entrypoint? references the calculator? any
        undefined names?), (2) smoke-runs it in the heavy-calculator env, feeding
        real errors back to the model to self-correct, and (3) proactively reviews
        a runnable script for latent bugs and fixes them before the expensive run.

        Non-blocking by construction: it heals what it can, writes back the
        improved ``main.py``, records a ``repair_report`` artifact, and always
        advances to EXECUTE (which stays the real gate). Only calculator-driven,
        LLM-synthesized bundles are healed -- deterministic template bundles are
        already valid, and a plain run with no bundle just passes through. The LLM
        repair/review calls happen only when ``verify_codegen`` is on and an agent
        is available; otherwise the doctor still runs its free static checks and
        logs any findings.
        """
        bundle_dir = self.context.artifacts.get("run_bundle")
        main_path = Path(bundle_dir) / "main.py" if bundle_dir else None
        if not main_path or not main_path.is_file():
            return State.EXECUTE  # nothing built (e.g. build no-opped); pass through

        plan = self._load_artifact("execution_plan") or {}
        method = plan.get("selected_method") or {}
        calc_import = method.get("calculator_import")
        # Heal only LLM-synthesized scripts: a calculator-driven run (calculator_import
        # set) or a library-only run that synthesized real code (config marks it
        # 'llm_synthesized'). A deterministic-template or generic-stub bundle is
        # already valid, so it passes straight through.
        is_synthesized = self._bundle_config(bundle_dir).get("template") == "llm_synthesized"
        if not calc_import and not is_synthesized:
            return State.EXECUTE

        doctor = self._make_script_doctor(plan, method, calc_import)
        original = main_path.read_text(encoding="utf-8")
        report = doctor.heal(original)
        if report.source and report.source != original:
            main_path.write_text(report.source, encoding="utf-8")
        self.context.artifacts["repair_report"] = self._write_artifact(
            "repair_report", report.to_dict())
        self._log_repair(report)
        return State.EXECUTE

    def _bundle_helper_files(self) -> dict:
        """Helper modules sitting beside main.py in the current run bundle.

        Handed to the ScriptDoctor so its smoke sandbox runs the bundle the
        cluster will run, not main.py in isolation.
        """
        bundle = Path(self.artifacts_dir) / f"run_bundle_{self.run_id}"
        files = {}
        try:
            for path in sorted(bundle.glob("twain_*.py")):
                files[path.name] = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        return files

    def _make_script_doctor(self, plan: dict, method: dict, calc_import):
        """The ScriptDoctor both REPAIR and EXECUTE's self-heal loop use.

        Smoke-runs in the interpreter the bundle will actually run in: the sim
        env for a calculator run, else the default interpreter -- a
        library-only run (e.g. PySCF) resolves there, not in sim. The LLM
        repair channel is attached only when ``verify_codegen`` is on.
        """
        from code_gen.script_doctor import ScriptDoctor
        smoke_python = pixi_env_python(SIM_ENV) if calc_import else sys.executable
        return self._script_doctor or ScriptDoctor(
            agent=(lambda p: self._agent_text(p, max_tokens=_CODEGEN_MAX_TOKENS))
            if self.verify_codegen else None,
            brief=self._repair_brief(plan, method),
            bundle_files=self._bundle_helper_files(),
            sim_python=smoke_python,
            # The REPAIR checklist: smoke test, fix rounds, review (#162).
            on_step=lambda step, status, label: self._progress("REPAIR", step, status, label),
        )

    def _bundle_config(self, bundle_dir) -> dict:
        """Read the built bundle's config.yaml (empty dict if absent/unreadable).

        Used by REPAIR to tell an LLM-synthesized script (``template:
        llm_synthesized``) from a deterministic template that needs no healing.
        """
        if not bundle_dir:
            return {}
        try:
            import yaml
            text = (Path(bundle_dir) / "config.yaml").read_text(encoding="utf-8")
            return yaml.safe_load(text) or {}
        except Exception:  # noqa: BLE001 - missing/invalid config -> no metadata
            return {}

    def _repair_brief(self, plan: dict, method: dict) -> dict:
        """Context the ScriptDoctor needs: tool/calculator imports, property, material."""
        intent = self._load_artifact("intent_spec")
        libraries = method.get("libraries") or [method.get("tool_name")]
        driver = method.get("calculator_library") or (libraries[0] if libraries else "ASE")
        driver_deps = _depinf.import_names(driver)
        driver_import = driver_deps[0].import_name if driver_deps else canonical_tool_key(driver)
        material = CodegenEngine._material_brief(plan, intent)
        return {
            "library": driver,
            "library_import": driver_import,
            "calculator": method.get("calculator"),
            "calculator_import": method.get("calculator_import"),
            # Same fallback codegen uses (_generate_with_calculator): a planner
            # that leaves requested_property null still names the property in its
            # acceptance metric. Without the fallback the doctor received the
            # literal "the requested property", which no property-class check can
            # recognise -- so the uncorrelated-thermochemistry guard could never
            # fire on the very runs it was written for.
            "property": (plan.get("requested_property")
                         or _first_metric_name(plan)
                         or "the requested property"),
            "material_desc": CodegenEngine._material_desc(material),
            # The bare formula as well as the prose description: a check that has
            # to reason about which ELEMENTS a run touches cannot parse them back
            # out of "Calcium diplatinide (CaPt2), cubic Laves phase" reliably.
            "formula": material.get("formula"),
            # Registry-declared placement, so the doctor can tell whether the
            # script has to hand its ranks to the engine.
            "parallelism": self._selected_parallelism(plan),
            "acceptance": plan.get("acceptance_metrics") or [],
            "output_file": "results.csv",
            # The researcher's own words: lets checks that enforce fast defaults
            # (e.g. primitive cell) stand down when the researcher explicitly
            # asked for the expensive variant (conventional cell, supercell, ...).
            "objective": (intent or {}).get("objective") or plan.get("objective") or "",
        }

    def _log_repair(self, report) -> None:
        """Log a one-line summary of the repair outcome for the researcher."""
        if report.status == "healthy":
            logger.info("[repair] generated script passed all checks; no changes needed.")
        elif report.status == "repaired":
            logger.info("[repair] healed the script in %s round(s): %s",
                        report.rounds, "; ".join(report.fixes))
        elif report.status == "unverifiable":
            logger.info("[repair] static checks passed; could not smoke-run here "
                        "(no '%s' env) -- delivering as-is.", SIM_ENV)
        else:  # unrepairable
            remaining = "; ".join(d.render() for d in report.remaining[:3])
            logger.info("[repair] could not fully repair the script "
                        "(%s round(s)); EXECUTE will surface any failure. "
                        "Remaining: %s", report.rounds, remaining)

    def execute(self) -> State:
        """Run the generated RunBundle on the local machine (Story 5.2).

        Off by default (``execute_locally=False``) so offline/seeded pipeline
        runs keep EXECUTE a no-op and trust the seeded ``execution_status``. When
        enabled, the LocalExecutionAdapter runs the bundle built in BUILD --
        installing deps into a venv if requested, running the smoke tests first,
        then ``python main.py`` under a resource monitor + timeout. The captured
        logs and metrics are written as the ``execution_result`` artifact, and
        ``execution_status`` is set from the run so the EXECUTE->INTERPRET guard
        reflects what actually happened.
        """
        if not (self.execute_locally or self.execute_slurm):
            return State.INTERPRET

        bundle_dir = self.context.artifacts.get("run_bundle")
        if not bundle_dir or not Path(bundle_dir).is_dir():
            # Nothing to execute (e.g. build() no-opped without a plan); leave the
            # guard to whatever seeded the context.
            return State.INTERPRET

        # Heavy calculations (a DFT band-gap run via GPAW can take many minutes
        # and pull in large deps) are gated on the researcher's go-ahead: the
        # generated script is the deliverable either way, so we ask before
        # burning the compute. Plain/cheap runs skip the prompt entirely.
        if not self._confirm_heavy_execution():
            return self._skip_execution(
                bundle_dir, status="deferred",
                note="Researcher chose not to run the heavy calculation now.")

        # A calculator bundle must run in the heavy-calculator ('sim') env, not
        # the default interpreter -- running it with the default `python` fails
        # with ModuleNotFoundError. Plain library runs stay on this interpreter.
        run_python = self._bundle_python()
        calc = self._selected_calculator()

        adapter = self._execution_adapter
        docker_route = False
        slurm_route = False
        if adapter is None and self.execute_slurm:
            # HPC route (Story 5.4): stage the bundle to the cluster, submit via
            # sbatch with the plan's resource request, poll to completion, and
            # fetch outputs back. Takes precedence over local/Docker -- the
            # researcher explicitly opted into the cluster.
            adapter = self._build_slurm_adapter()
            if adapter is None:
                return self._skip_execution(
                    bundle_dir, status="skipped_missing_dependency",
                    note=f"Not run on the cluster: no usable profile for "
                         f"'{self.slurm_cluster}' (configs/clusters/). ",
                    how_to=self._how_to_run(bundle_dir))
            slurm_route = True
        if adapter is None:
            # Route non-native engines (no build for this host, e.g. GPAW on a
            # Mac) into the linux-64 runner container; everything else runs in the
            # local sim/default interpreter. Either way, a run that can't happen
            # here delivers the script with guidance instead of crashing.
            if calc is not None and calc.needs_docker(current_platform()):
                if not docker_available():
                    return self._skip_execution(
                        bundle_dir, status="skipped_missing_dependency",
                        note=f"Not run here: {calc.name} has no {current_platform()} build and "
                             f"needs the linux-64 Docker runner, but no Docker daemon is reachable.",
                        how_to=self._how_to_run(bundle_dir))
                if not docker_image_available(DEFAULT_DOCKER_IMAGE):
                    return self._skip_execution(
                        bundle_dir, status="skipped_missing_dependency",
                        note=f"Not run here: {calc.name} runs in Docker, but the "
                             f"'{DEFAULT_DOCKER_IMAGE}' image isn't built yet.",
                        how_to=self._how_to_run(bundle_dir))
                from execution_adapter.docker_adapter import DockerExecutionAdapter
                adapter = DockerExecutionAdapter(workspace_root=str(self.artifacts_dir))
                docker_route = True
            else:
                # Real local execution. Before spending time, make sure the run can
                # actually happen here: the heavy env must be built, and the selected
                # calculator importable in it. Otherwise deliver the script with clear
                # guidance (a graceful outcome, like a deferral) instead of crashing.
                if calc is not None and run_python is None:
                    return self._skip_execution(
                        bundle_dir, status="skipped_missing_dependency",
                        note=f"Not run here: the '{SIM_ENV}' environment isn't built on this machine.",
                        how_to=self._how_to_run(bundle_dir))
                if not self.execute_install_deps:
                    missing = self._missing_run_imports(run_python)
                    if missing:
                        return self._skip_execution(
                            bundle_dir, status="skipped_missing_dependency",
                            note=f"Not run here: '{missing[0]}' is not installed in the run environment.",
                            how_to=self._how_to_run(bundle_dir))
                from execution_adapter.local_adapter import LocalExecutionAdapter
                # Keep the working dir (with its outputs) under the session artifacts
                # dir so results are discoverable and scoped to this run.
                adapter = LocalExecutionAdapter(workspace_root=str(self.artifacts_dir))

        run_kwargs = dict(
            # The sim env / Docker image already ship the whole stack, so never
            # pip-install into a venv there; that only applies to default-interpreter
            # runs -- and to Slurm jobs, whose compute nodes have no TWAIN env at all
            # (the job builds a venv from the bundle's requirements.txt).
            install_deps=slurm_route or (self.execute_install_deps
                                         and run_python is None and not docker_route),
            keep_artifacts=self.execute_keep_artifacts,
            run_smoke=True,
            timeout=self.execute_timeout,
            # Docker/Slurm fix the interpreter via the image/job; native uses
            # run_python (None => the adapter's default interpreter).
            python_executable=None if (docker_route or slurm_route) else run_python,
            run_id=self.run_id,  # names the workdir exec_<session_id> for traceability
        )
        # Self-heal loop: a run that crashes with a Python traceback inside the
        # generated script gets repaired against that traceback and re-executed
        # (bounded). The static checks catch the failure modes we've already
        # seen; this catches the ones we haven't -- the run's own error is the
        # ground truth, whatever the mistake was.
        attempts = 1 + self._runtime_repair_budget()
        attempt = 0
        while True:
            attempt += 1
            result = adapter.execute(bundle_dir, **run_kwargs)
            # A detached run (P2) pauses between attempts and re-enters here on
            # every resume, so a local counter would restart at 1 and the repair
            # budget would never run out: trust the attempt the job was.
            attempt = int((getattr(result, "install_log", None) or {}).get("attempt") or attempt)
            if result.succeeded or attempt >= attempts:
                break
            failure = _runtime_traceback(result)
            if failure is None:
                break
            self._progress("EXECUTE", "heal", "active",
                           f"The run crashed — repairing the script against its error "
                           f"(attempt {attempt} of {attempts - 1})")
            if not self._heal_runtime_failure(bundle_dir, failure, attempt):
                self._progress("EXECUTE", "heal", "failed",
                               "Could not repair the crash automatically")
                break
            self._progress("EXECUTE", "heal", "done",
                           f"Repaired — running it again (attempt {attempt + 1} of {attempts})",
                           attempt=attempt + 1)
        self.context.artifacts["execution_result"] = self._write_artifact(
            "execution_result", result.to_dict()
        )
        self.context.execution_status = bool(result.succeeded)
        if not result.succeeded:
            # Surface the *real* reason (missing deps, script error, resource
            # limits) with next steps, rather than letting the EXECUTE->INTERPRET
            # guard fail downstream as an opaque "incomplete context".
            raise self._execution_error(result, bundle_dir)
        return State.INTERPRET

    def _publish_progress(self, event_type: str, payload: dict) -> None:
        """Hand an activity event to the orchestrator's publisher (best effort)."""
        if self.publish_progress is None:
            return
        try:
            self.publish_progress(event_type, payload)
        except Exception:  # noqa: BLE001 - reporting must never break a stage
            pass

    def _progress(self, stage: str, step: str, status: str, label: str, **detail) -> None:
        """Report one checklist step of ``stage`` (see job_activity.py's shape)."""
        self._publish_progress("stage.progress", {
            "stage": stage, "step": step, "status": status,
            "label": label, "detail": detail,
        })

    def _runtime_repair_budget(self) -> int:
        """How many repair-and-re-execute rounds a failed run may consume.

        Each round costs a full execution (on Slurm: staging + a queue wait),
        so the default is small; ``TWAIN_RUNTIME_REPAIR_ATTEMPTS=0`` disables
        the loop entirely.
        """
        try:
            return max(0, int(os.getenv("TWAIN_RUNTIME_REPAIR_ATTEMPTS", "2")))
        except ValueError:
            return 2

    def _heal_runtime_failure(self, bundle_dir, failure: str, attempt: int) -> bool:
        """Repair ``main.py`` against the real run's traceback; True if rewritten.

        Only LLM-synthesized bundles are eligible (deterministic templates
        don't invent API calls) and only when the LLM repair channel is on
        (``verify_codegen``). The ScriptDoctor re-verifies the fix (static
        checks + smoke where possible), so a failed repair leaves the bundle
        untouched and the caller stops retrying.
        """
        plan = self._load_artifact("execution_plan") or {}
        method = plan.get("selected_method") or {}
        calc_import = method.get("calculator_import")
        is_synthesized = self._bundle_config(bundle_dir).get("template") == "llm_synthesized"
        if not calc_import and not is_synthesized:
            return False
        main_path = Path(bundle_dir) / "main.py"
        if not main_path.is_file():
            return False
        doctor = self._make_script_doctor(plan, method, calc_import)
        if doctor.agent is None:
            return False
        fixed = doctor.repair_runtime(
            main_path.read_text(encoding="utf-8"), failure)
        if not fixed:
            logger.info("[execute] the run crashed in the generated script, and "
                        "automatic repair could not produce a better one; "
                        "surfacing the failure.")
            return False
        main_path.write_text(fixed, encoding="utf-8")
        logger.info("[execute] the run crashed in the generated script; repaired "
                    "it against the runtime traceback and re-executing "
                    "(repair round %d).", attempt)
        return True

    def _selected_calculator(self):
        """The CalculatorEntry the plan selected (or None for a plain run)."""
        plan = self._load_artifact("execution_plan")
        if not plan:
            return None
        name = (plan.get("selected_method") or {}).get("calculator")
        return find_calculator(name)

    def _selected_parallelism(self, plan: dict) -> str:
        """Where the plan calculator's parallelism lives (registry-declared).

        Defaults to "threads" for a library-only plan or an unknown calculator:
        the conservative choice, since wrongly launching a serial driver under
        mpirun corrupts its working directory.
        """
        name = (plan.get("selected_method") or {}).get("calculator")
        entry = find_calculator(name) if name else None
        return getattr(entry, "parallelism", "threads") or "threads"

    def _build_slurm_adapter(self):
        """A SlurmExecutionAdapter wired from the cluster profile + the plan.

        Resources come from the plan's ``slurm_request`` (synthesized during
        PLAN); connection details from the profile, overridable via
        ``TWAIN_SLURM_HOST`` (empty string => run sbatch locally, i.e. the
        process is already on a login node) and ``TWAIN_SLURM_USER``. Returns
        None when the profile can't be loaded, so execute() can skip gracefully
        with guidance instead of crashing.

        Job control defaults to the RIS API (``RIS_API_TOKEN``/
        ``RIS_API_BASE_URL``); set ``TWAIN_SLURM_BACKEND=ssh`` to fall back to
        the legacy sbatch/squeue/sacct/scancel-over-SSH path -- see
        SlurmExecutionAdapter's docstring. Staging (rsync) is unaffected by
        this flag either way.
        """
        from execution_adapter.cluster_profile import ClusterProfile
        from execution_adapter.slurm_execution_adapter import SlurmExecutionAdapter
        from plan_synthesizer.execution_plan import SlurmRequest
        from plan_synthesizer.plan_synthesizer import MIN_RAM_GB, MIN_WALL_MINUTES
        try:
            profile = ClusterProfile.load(self.slurm_cluster)
        except (OSError, ValueError, TypeError) as exc:
            print(f"[execute] cluster profile '{self.slurm_cluster}' unusable: {exc}")
            return None
        request = None
        plan = self._load_artifact("execution_plan") or {}
        raw = plan.get("slurm_request")
        if isinstance(raw, dict):
            try:
                # Plan contract: ram is GB, max_time is hours (plan_synthesizer /
                # schema examples). The Slurm adapter expects MB + minutes.
                ram_gb = max(MIN_RAM_GB, int(raw.get("ram") or MIN_RAM_GB))
                max_hours = float(raw.get("max_time") or (MIN_WALL_MINUTES / 60.0))
                request = SlurmRequest(
                    cpu_count=int(raw.get("cpu_count") or 8),
                    gpu_count=int(raw.get("gpu_count") or 0),
                    max_time=max(MIN_WALL_MINUTES, max_hours * 60.0),
                    ram=ram_gb * 1024,
                )
            except (TypeError, ValueError):
                request = None  # malformed plan request -> adapter default
        # Pre-provisioned cluster envs to try, in order, before falling back to a
        # venv + pip: conda-only packages (GPAW needs libxc, Psi4 and NWChem have
        # no PyPI distribution at all) cannot be pip-built on a compute node, so
        # the env that ships them has to be a candidate or the run is doomed.
        #
        # Derived from the WHOLE toolset and via each library's conda PACKAGE name,
        # which is how the specs are keyed. Both parts are load-bearing:
        #
        #   * Whole toolset, not just calculator + tool_name. A library-only plan
        #     still needs its conda-only member: quacc+ASE+Psi4 has calculator=None
        #     and tool_name="quacc", so Psi4 sat in `libraries` and the only env
        #     with psi4 was never probed. The job fell through to pip, which
        #     correctly refuses a conda-only package, and died at the smoke gate
        #     with "MISSING DEPENDENCY: psi4" (Slurm job 2580169).
        #   * Package name, not the display name lowercased. "NWChem" -> "nwchem"
        #     happens to work, but "Quantum ESPRESSO" -> "quantum espresso" and
        #     "DFTB+" -> "dftb+" name no env at all, so those two engines could
        #     never have been selected however well provisioned they were.
        env_pythons = []
        if profile.envs_root:
            # Exactly the envs that can run this toolset (an env's own spec plus
            # pip for the rest), best first: the job tries them in this order and
            # layers its pip venv on the first -- the same rule PLAN vetoed on, so
            # an approved plan can't land in an env that lacks what it needs (#169).
            names = cluster_env_candidates(_selected_toolset(plan))
            if names is None:
                names = ["default"]  # PLAN refuses these; keep a pre-#169 plan runnable-ish
            env_pythons = [f"{profile.envs_root}/{n}/bin/python" for n in names]

        host = os.environ.get("TWAIN_SLURM_HOST")  # None => profile login node
        return SlurmExecutionAdapter(
            profile,
            request=request,
            host=host,
            user=os.environ.get("TWAIN_SLURM_USER"),
            workspace_root=str(self.artifacts_dir),
            env_pythons=env_pythons,
            # Where this engine's parallelism lives, so the payload spends the
            # allocated cores in the right place -- see the adapter's _env_payload.
            parallelism=self._selected_parallelism(plan),
            # Poll for as long as the job may legitimately run (its wall time)
            # plus queue headroom -- otherwise a 4-hour DFT run outlives the
            # adapter's default 2-hour wait and EXECUTE reports a bogus timeout.
            max_wait=self.slurm_wait_budget(),
            # Terminate button: checked between polls; scancels the job.
            should_abort=self.should_abort,
            # Webhook wake-up: poll as soon as ris-api reports on the job.
            job_event_wait=self.job_event_wait,
            # Live EXECUTE checklist + job log for the UI.
            on_progress=self._publish_progress,
            issue_job_ticket=self.issue_job_ticket,
            # Pause on the cluster job instead of waiting (needs both seams).
            cluster_jobs=self.cluster_jobs if self.suspend_for else None,
            suspend=self.suspend_for if self.cluster_jobs is not None else None,
        )

    # Extra polling headroom on top of the job's wall time: covers time spent
    # pending in the Slurm queue plus staging/accounting latency.
    SLURM_QUEUE_MARGIN_SECONDS = 30 * 60

    def slurm_wait_budget(self) -> float:
        """Seconds EXECUTE should wait on a Slurm job: wall time + queue margin.

        Read from the plan's ``slurm_request`` (max_time is hours). Also used by
        the orchestrator to stretch the EXECUTE stage timeout so the stage
        doesn't abort while the adapter is still legitimately polling.
        """
        from execution_adapter.slurm_execution_adapter import DEFAULT_MAX_WAIT
        from plan_synthesizer.plan_synthesizer import MIN_WALL_MINUTES
        plan = self._load_artifact("execution_plan") or {}
        raw = plan.get("slurm_request") or {}
        try:
            hours = float(raw.get("max_time") or (MIN_WALL_MINUTES / 60.0))
        except (TypeError, ValueError):
            hours = MIN_WALL_MINUTES / 60.0
        return max(DEFAULT_MAX_WAIT,
                   hours * 3600.0 + self.SLURM_QUEUE_MARGIN_SECONDS)

    def _confirm_heavy_execution(self) -> bool:
        """Ask the researcher before running a heavy calculation; True to proceed.

        Only heavy calculators (DFT engines like GPAW) prompt -- everything else
        proceeds silently. The prompt goes through ``_ask_user`` so it works both
        on the CLI (stdin) and through the web UI's injected ``ask`` seam. Any
        answer other than an explicit yes defers the run.
        """
        calculator = self._selected_calculator()
        if calculator is None or not calculator.heavy:
            return True
        # Already agreed to for this engine in this run. A correction or re-plan
        # loop comes back through EXECUTE, and re-asking there is noise: the
        # researcher consented to spending compute on this calculation, and the
        # answer they gave has not changed. Keyed on the engine, so a re-plan
        # that lands on a DIFFERENT heavy calculator still asks.
        if self.context.heavy_confirmed == calculator.name:
            logger.info("[execute] the heavy %s run was already confirmed for "
                        "this run; not asking again.", calculator.name)
            return True
        # Unattended mode: the researcher opted into automatic runs, so proceed
        # without asking (approving the plan already authorized this execution).
        if self.auto_approve:
            logger.info("[execute] Unattended mode: proceeding with the heavy "
                        "%s calculation without prompting.", calculator.name)
            return True
        # If we can't actually prompt (headless run with no injected ``ask`` and
        # no interactive stdin), default to deferring rather than hanging on
        # ``input()`` or crashing on EOF. Safe by construction: the script is
        # already built; not running it is the conservative choice.
        if not self._can_prompt():
            logger.info("[execute] Heavy calculation requires confirmation, but no interactive "
                        "input is available; deferring. Inject an 'ask' callable or run "
                        "interactively to execute it.")
            return False
        if calculator.needs_docker(current_platform()):
            where = (f"in the linux-64 Docker container (no {current_platform()} build; "
                     f"runs under linux/amd64 emulation, so slower than native)")
        else:
            where = f"and needs {calculator.name} installed"
        answer = self._ask_user(
            f"The plan builds a {calculator.name} calculation, a heavy DFT run that "
            f"can take several minutes {where}. The "
            f"generated script is ready either way.\nRun it now? [y/N]: ",
            ASK_HEAVY_CONFIRM,
        )
        confirmed = str(answer).strip().lower() in {"y", "yes", "run", "now", "1", "true"}
        if confirmed:
            self.context.heavy_confirmed = calculator.name
        return confirmed

    def _can_prompt(self) -> bool:
        """Whether we can actually ask the researcher a question right now."""
        if callable(self.ask):
            return True
        try:
            return bool(sys.stdin) and sys.stdin.isatty()
        except Exception:  # noqa: BLE001 - no usable stdin -> can't prompt
            return False

    def _skip_execution(self, bundle_dir: str, *, status: str, note: str,
                        how_to: Optional[str] = None) -> State:
        """Record a not-executed-but-complete outcome and advance cleanly.

        Used when the researcher defers a heavy run, or when the run's heavy
        dependency isn't installed here. The bundle stays on disk as the
        deliverable; provenance records why it wasn't run, and the stage is marked
        complete so the pipeline winds down rather than stalling on the
        EXECUTE->INTERPRET guard (which would otherwise raise an opaque error).
        """
        result = {
            "status": status,
            "succeeded": False,
            "note": note,
            "how_to_run": how_to,
            "bundle_dir": str(bundle_dir),
            "script": self.context.artifacts.get("script"),
        }
        self.context.artifacts["execution_result"] = self._write_artifact(
            "execution_result", result
        )
        message = f"[execute] {note}"
        if how_to:
            message += f"\n[execute] To run it: {how_to}"
        else:
            message += (f"\n[execute] The runnable bundle is at {bundle_dir} -- "
                        f"run it later with:  python {Path(bundle_dir) / 'main.py'}")
        logger.info(message)
        # A built-but-not-executed run is a complete, valid outcome; advance.
        self.context.execution_status = True
        return State.INTERPRET

    def _bundle_python(self) -> Optional[str]:
        """Interpreter the built bundle must run under, or None for the default.

        Calculator bundles need the heavy-calculator ('sim') env; a plain library
        run uses the default interpreter (None -> the adapter's own default).
        Returns None too when a calculator is selected but the sim env isn't built
        here -- the caller turns that into a graceful, guided skip.
        """
        if self._selected_calculator() is None:
            return None
        return pixi_env_python(SIM_ENV)

    def _how_to_run(self, bundle_dir) -> str:
        """Actionable 'run it yourself' guidance appropriate to the bundle's env."""
        main = Path(bundle_dir) / "main.py"
        calc = self._selected_calculator()
        if calc is not None and calc.needs_docker(current_platform()):
            # Non-native engine: guide the linux-64 Docker path (build the image,
            # then TWAIN reruns it in the container -- or run the bundle by hand).
            return (
                f"{calc.name} has no {current_platform()} build, so it runs in the linux-64 "
                f"Docker runner. One-time: install/start Docker (see runner/README.md) and build "
                f"the image: `docker build --platform linux/amd64 -f runner/Dockerfile "
                f"-t {DEFAULT_DOCKER_IMAGE} .`. Then re-run TWAIN (it will execute in the container), "
                f"or run the bundle directly: `docker run --rm --platform linux/amd64 "
                f"-v {Path(bundle_dir)}:/work -w /app {DEFAULT_DOCKER_IMAGE} "
                f"pixi run -e {SIM_ENV} python /work/main.py`.")
        if calc is not None:
            return (f"The bundle runs in TWAIN's '{SIM_ENV}' environment. Run it with: "
                    f"`pixi run -e {SIM_ENV} python {main}` (build the env first with "
                    f"`pixi install` if needed).")
        reqs = Path(bundle_dir) / "requirements.txt"
        return f"Install deps (`pip install -r {reqs}`), then run: `python {main}`."

    def _missing_run_imports(self, run_python: Optional[str] = None) -> list:
        """Heavy imports the run needs that aren't available in its interpreter.

        Lets EXECUTE detect e.g. a missing GPAW before a wasted run and report it
        as actionable guidance instead of a cryptic guard failure. Checks the
        interpreter the bundle will actually run under: the sim env for a
        calculator bundle (``run_python``), else this interpreter.
        """
        import importlib.util
        plan = self._load_artifact("execution_plan") or {}
        calc_import = (plan.get("selected_method") or {}).get("calculator_import")
        if not calc_import:
            return []
        if run_python and run_python != sys.executable:
            # Ask the target interpreter itself whether the calculator imports.
            try:
                proc = subprocess.run(
                    [run_python, "-c",
                     "import importlib.util,sys; "
                     f"sys.exit(0 if importlib.util.find_spec({calc_import!r}) else 1)"],
                    capture_output=True, timeout=30,
                )
                return [] if proc.returncode == 0 else [calc_import]
            except (OSError, subprocess.SubprocessError):
                return [calc_import]
        try:
            available = importlib.util.find_spec(calc_import) is not None
        except Exception:  # noqa: BLE001 - unresolvable spec => treat as missing
            available = False
        return [] if available else [calc_import]

    def _execution_error(self, result, bundle_dir: str) -> Exception:
        """Build a clear, actionable error for a failed execution.

        Prefers the orchestrator's typed ``ConfigError`` (carries a category +
        hint the researcher-facing notifier renders); falls back to a plain
        ``RuntimeError`` with the same text when that module isn't importable
        (e.g. the state machine exercised standalone in a unit test).
        """
        status = getattr(result, "status", None)
        status_name = getattr(status, "value", None) or str(status)
        detail = (getattr(result, "message", "") or "").strip()
        output = (getattr(result, "stdout", "") or "") + "\n" + (getattr(result, "stderr", "") or "")
        highlights = [ln.strip() for ln in output.splitlines()
                      if "MISSING DEPENDENCY" in ln or "Error" in ln or "error" in ln]
        reason = detail or (highlights[0] if highlights else f"execution failed ({status_name})")
        message = f"the generated run did not succeed ({status_name}): {reason}"
        hint = (f"Inspect the script and dependencies at {bundle_dir} (main.py, "
                f"requirements.txt). {self._how_to_run(bundle_dir)}")
        try:
            from error_handler import ConfigError
            return ConfigError(message, hint=hint)
        except Exception:  # noqa: BLE001 - standalone use: plain error with the text
            return RuntimeError(f"{message} -- {hint}")

    def _no_result_error(self, result: dict, hints: list) -> Exception:
        """Build the error for a run that exited cleanly but delivered nothing.

        Exit code 0 is not the contract -- the printed result is. A script
        whose output holds no finite value for the requested metric (every
        field NaN, the metric missing, or nothing parseable at all) computed
        nothing the researcher asked for, so failing with the output in hand
        beats reporting a hollow success.
        """
        metric = hints[0]
        tail = "\n".join((result.get("stdout") or "").strip().splitlines()[-12:])
        message = (f"the run finished, but its output contains no finite value "
                   f"for '{metric}' -- every parseable result was missing, NaN, "
                   f"or non-numeric, so there is nothing to validate or deliver")
        hint = ("This usually means the simulation diverged or its analysis "
                "failed silently. Last output lines:\n" + tail)
        try:
            from error_handler import ConfigError
            return ConfigError(message, hint=hint)
        except Exception:  # noqa: BLE001 - standalone use: plain error with the text
            return RuntimeError(f"{message} -- {hint}")

    # ---- interpret / validate / correct (Epic 6) ---------------------------

    # Metric names that map onto a differently-named baseline property. Baseline
    # lookup is already case-insensitive, so a metric literally named "logS"
    # matches without help; only different spellings need an entry here.
    _BASELINE_PROPERTY_ALIASES = {
        "solubility": "logS",
        "aqueous_solubility": "logS",
        "aqueous solubility": "logS",
        "log_s": "logS",
        # The plan names the metric after the property AND its unit, which is
        # what the generated scripts print; the baseline DB keys on the property
        # alone, so this run missed aspirin's own -1.72 literature value.
        "aqueous_solubility_logs": "logS",
        "aqueous_solubility_log_mol_per_l": "logS",
        "solubility_logs": "logS",
        "logs": "logS",
    }

    def interpret(self) -> State:
        """Extract normalized metrics from the executed run's output (Story 6.1).

        Reads the ``execution_result`` artifact and pulls numeric metrics out of
        the run's stdout / output files with the pluggable parsers (json, csv,
        log), then normalizes them into one primary + secondary metric view with
        per-metric uncertainty. The result is persisted as the
        ``normalized_result`` artifact for cross-validation. Runs where nothing
        was actually executed (seeded pipelines, deferred/skipped heavy runs)
        no-op through to VALIDATE, which then routes on whatever verdict the
        context was seeded with.
        """
        # INTERPRET owns ``normalized_result``, so it must clear the previous
        # pass's before deciding: on a correction loop whose rerun was skipped
        # or deferred, leaving it in place lets VALIDATE re-grade the earlier
        # run's numbers and report them as this run's result.
        self.context.artifacts.pop("normalized_result", None)
        result = self._load_artifact("execution_result")
        if not result or not result.get("succeeded"):
            return State.VALIDATE
        normalized = self._normalize_run_output(result)
        if normalized is None:
            hints = self._metric_hints()
            if hints:
                # The researcher asked for a specific quantity and the run --
                # exit code notwithstanding -- never produced a finite value
                # for it. Delivering that as a clean success hands them NaN;
                # failing with the output in hand is the honest outcome.
                raise self._no_result_error(result, hints)
            logger.info("[interpret] no numeric metrics could be extracted from "
                        "the run output; delivering without validation.")
            return State.VALIDATE
        payload = normalized.to_dict()
        # A benchmark table's per-row identities, so VALIDATE can compare each
        # system against its OWN literature value instead of grading the mean.
        entities = self._per_entity_rows(result)
        if entities:
            payload["entities"] = entities
            logger.info("[interpret] %d systems in the result table",
                        len(entities))
        self.context.artifacts["normalized_result"] = self._write_artifact(
            "normalized_result", payload)
        metric = normalized.primary_metric
        unit = f" {metric.unit}" if metric.unit else ""
        logger.info("[interpret] %s = %.6g%s +/- %.2g (%s)", metric.name,
                    metric.value, unit, metric.uncertainty,
                    metric.uncertainty_method)
        return State.VALIDATE

    def _metric_hints(self) -> list:
        """Names the researcher's ask suggests for the primary metric, in order
        of specificity: the plan's canonical property, then the acceptance-metric
        names from the plan and the intent."""
        hints = []
        plan = self._load_artifact("execution_plan") or {}
        if plan.get("requested_property"):
            hints.append(str(plan["requested_property"]))
        intent = self._load_artifact("intent_spec") or {}
        for source in (plan, intent):
            for metric in source.get("acceptance_metrics") or []:
                if isinstance(metric, dict) and metric.get("metric_name"):
                    hints.append(str(metric["metric_name"]))
        return hints

    @staticmethod
    def _pick_metric(names, hints):
        """Match a parsed/normalized field to a requested name, tolerantly:
        exact (case-insensitive) first, then substring either way (so the hint
        "logS_MAE" still finds a field named "logS")."""
        lower = {str(n).lower(): n for n in names}
        for hint in hints:
            match = lower.get(str(hint).lower())
            if match is not None:
                return match
        for hint in hints:
            h = str(hint).lower()
            for name in names:
                n = str(name).lower()
                if h in n or n in h:
                    return name
        return None

    @staticmethod
    def _stdout_json(stdout: str) -> Optional[str]:
        """The last JSON object printed to stdout, or None.

        Generated scripts are told to print their metric summary as a single
        JSON line at the end, so single lines are scanned first; a script that
        pretty-prints (``json.dumps(..., indent=2)``) spans lines, so the tail
        starting at the last line-initial ``{`` is tried as a block too."""
        text = stdout or ""
        # A top-level array is not a metric summary, and its elements sit at
        # line-initial '{' when it is pretty-printed -- without this, one of
        # them would be picked up as though it were the summary.
        if text.lstrip().startswith("["):
            return None
        # Every offset a JSON object could start at: index 0 (a summary that IS
        # the whole of stdout, which the generic template prints) plus every
        # line-initial '{'.
        offsets = set()
        if text.lstrip().startswith("{"):
            offsets.add(text.index("{"))
        found = text.find("\n{")
        while found != -1:
            offsets.add(found + 1)
            found = text.find("\n{", found + 1)
        # Latest first: the summary is printed last, and raw_decode stops at the
        # end of the object, so trailing text on the same line is harmless.
        # Trying earlier candidates in turn means a later line that only looks
        # like JSON -- a Python dict repr, a truncated object -- cannot bury a
        # valid summary printed above it.
        for offset in sorted(offsets, reverse=True):
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return json.dumps(obj)
        return None

    def _output_candidates(self, result: dict):
        """(parser_name, content) candidates from the run, most structured first:
        the JSON summary on stdout, then CSV output files from the run's workdir,
        then raw stdout via the key=value log parser."""
        candidates = []
        stdout = result.get("stdout") or ""
        blob = self._stdout_json(stdout)
        if blob:
            candidates.append(("json", blob))
        out_dir = result.get("artifacts_dir")
        if out_dir and Path(out_dir).is_dir():
            # Prefer files the templates name for results (results.csv,
            # predictions.csv) over incidental CSVs.
            csvs = sorted(
                Path(out_dir).glob("*.csv"),
                key=lambda p: (("result" not in p.name and "predict" not in p.name), p.name),
            )
            for path in csvs[:3]:
                try:
                    candidates.append(("csv", path.read_text(encoding="utf-8")))
                except OSError:
                    continue
        if stdout:
            candidates.append(("log", stdout))
        return candidates

    def _normalize_run_output(self, result: dict) -> Optional[NormalizedResult]:
        """Parse + normalize the run's output, preferring a candidate that
        actually contains the requested metric. None when nothing matching
        could be extracted.

        The whatever-parsed-first fallback only applies when the researcher
        named NO metric: with hints in hand and no output matching them, the
        run plainly never produced the requested quantity, and normalizing a
        bookkeeping column instead (``n_criteria`` from the generic scaffold
        was the concrete case) hands validate() a fake primary metric to grade.
        Delivering without validation -- and saying so -- is the honest result.

        Non-finite fields never count as a match: a run that prints
        ``"aqueous_solubility_logS": NaN`` has NOT produced the requested
        quantity, whatever the field is called.
        """
        hints = self._metric_hints()
        first_fallback = None  # parsed output when the researcher named no metric
        for parser_name, content in self._output_candidates(result):
            try:
                parsed = get_parser(parser_name).parse(content)
            except (ParserError, ValueError):
                continue
            finite = _finite_fields(parsed)
            if finite is None:
                continue
            primary = self._pick_metric(finite.field_names(), hints)
            if primary is not None:
                return _stamp_sample_count(normalize(finite, primary=primary),
                                           finite, primary)
            if first_fallback is None:
                first_fallback = finite
        if hints or first_fallback is None:
            return None
        return _stamp_sample_count(normalize(first_fallback), first_fallback,
                                   first_fallback.fields[0].name)

    # Header names that identify WHICH system a row is about, most specific
    # first. A benchmark table pairs one of these with the metric column.
    _ENTITY_COLUMNS = ("molecule", "compound", "name", "system", "material",
                       "formula", "smiles", "id")

    def _per_entity_rows(self, result: dict) -> list:
        """One (entity, metric, value) row per system in a benchmark table.

        Averaging the metric column and comparing that mean against ONE
        molecule's reference can land inside the tolerance and falsely ACCEPT
        (see _ungradable_aggregate). Identifiers live in a text column that the
        numeric parser drops, so the table is re-read here. Empty for a
        single-system run.
        """
        import csv as _csv
        rows = []
        for parser_name, content in self._output_candidates(result):
            if parser_name != "csv":
                continue
            try:
                reader = _csv.DictReader(io.StringIO(content))
                records = list(reader)
            except (_csv.Error, ValueError):
                continue
            if len(records) < 2 or not reader.fieldnames:
                continue
            headers = {str(h).strip().lower(): h for h in reader.fieldnames if h}
            entity_col = next((headers[c] for c in self._ENTITY_COLUMNS
                               if c in headers), None)
            if entity_col is None:
                continue
            numeric = [h for h in reader.fieldnames
                       if h and h != entity_col
                       and all(_as_float(r.get(h)) is not None for r in records)]
            if not numeric:
                continue
            metric_col = self._pick_metric(numeric, self._metric_hints()) or numeric[0]
            for record in records:
                entity = str(record.get(entity_col) or "").strip()
                value = _as_float(record.get(metric_col))
                if entity and value is not None and math.isfinite(value):
                    rows.append({"entity": entity, "metric": metric_col,
                                 "value": value})
            if rows:
                return rows
        return rows

    def validate(self) -> State:
        """Cross-validate the interpreted result and route on the verdict (6.2).

        With a ``normalized_result`` artifact present, every extracted metric
        becomes a prediction for the run's molecule and is compared against the
        literature baseline DB (configs/baselines.json); the acceptance judge
        grades the agreement into accepted / needs_review / rejected and the
        ``validation_report`` artifact records the full comparison. When no
        baseline covers this molecule, the plan's own acceptance criteria
        (target +/- tolerance) are the reference instead; with no reference at
        all the result is delivered as-is, with the report saying so.

        Verdicts that ask for another pass (needs_review -> CORRECT, rejected ->
        REPLAN) are gated by the bounded rerun controller (Story 6.3): once the
        iteration cap is hit or the loop stops improving, the result is
        delivered anyway -- flagged for the researcher in the report -- rather
        than looping forever.

        Runs with nothing interpreted (seeded / skipped / deferred) keep the
        previous behavior: route on whatever verdict the context carries,
        defaulting to ACCEPT so a planning-only run still terminates.
        """
        # CORRECT's plan belongs to the pass that produced it (see _STAGE_OUTPUTS):
        # left in place, a run that ends accepted still ships a diagnosis of what
        # supposedly went wrong.
        self.context.artifacts.pop("correction_plan", None)
        normalized = self._load_artifact("normalized_result")
        if normalized is not None:
            self.context.validation_result = self._cross_validate(normalized)
        elif self.context.artifacts.get("validation_report"):
            self.context.validation_result = self._stop_unproductive_loop()
        verdict = self.context.validation_result
        if verdict == "rejected":
            # Whether the re-planned run needs a fresh approval is decided by
            # plan() once the new plan exists and can be compared with the
            # approved one -- withdrawing it here re-asks even when the replan
            # lands on exactly the same method, which is pure noise.
            return State.REPLAN
        if verdict == "needs_review":
            return State.CORRECT
        return State.ACCEPT

    def _stop_unproductive_loop(self) -> str:
        """End a correction loop whose rerun produced nothing to grade.

        The bounded gate only runs when there IS a result, so a pass that comes
        back empty would route on the previous verdict -- back into
        CORRECT/REPLAN, forever, without advancing the counter meant to stop it.
        Nothing new was measured, so deliver the last graded result flagged.
        Only reachable once a report exists, so a seeded verdict is never
        overridden.
        """
        report = self._load_artifact("validation_report")
        if report is None:
            # The path is in the context but the file is gone or unreadable.
            # Writing a rerun-only stub here would REPLACE the real report with
            # one carrying no comparison and no rationale, so leave it alone.
            logger.info("[validate] the previous validation report is unreadable; "
                        "delivering on the verdict already on record (%s).",
                        self.context.validation_result)
            return "accepted"
        verdict = report.get("acceptance_status") or self.context.validation_result
        report["rerun"] = {
            "decision": "stop",
            "stop_reason": "no_new_result",
            "reason": ("The corrected run produced no result to grade, so "
                       "another identical round cannot improve on it."),
            "final_verdict": verdict,
            "disposition": "delivered_for_researcher_review",
        }
        self.context.artifacts["validation_report"] = self._write_artifact(
            "validation_report", report)
        logger.info("[validate] the corrected run produced nothing to grade; "
                    "delivering the previous result flagged for review "
                    "(verdict on record: %s).", verdict)
        return "accepted"

    def _accept_or_loop(self, normalized: dict, artifact: dict) -> bool:
        """Ask whether to rerun (True) or accept this result as it stands (False).

        A failing verdict has three very different causes and the grader cannot
        tell them apart: the generated code is wrong (the aspirin solubility run
        computed a partition constant and called it a solubility), or the method
        is systematically offset from the reference and the run is as good as
        that method gets (PBE puts silicon's gap near 0.6 eV against an
        experimental 1.17 -- a textbook DFT underestimate, not a failure), or the
        reference is not comparable at all. Only the first is worth another
        calculation. A researcher can see which it is in seconds, so they are
        asked before the compute is spent, and the answer is recorded.

        Unattended runs keep looping automatically. When there is no way to ask,
        the result is accepted rather than rerun: spending an unattended DFT
        calculation on an unreviewed guess is the worse default.
        """
        if self.auto_approve:
            return True
        if not self._can_prompt():
            logger.info("[validate] no interactive input available; accepting the "
                        "flagged result rather than rerunning unattended.")
            return False
        answer = self._ask_user(
            self._accept_or_loop_prompt(normalized, artifact), ASK_VALIDATION_GATE)
        text = str(answer).strip().lower()
        # Only an explicit ask for another pass spends the compute. "yes" is NOT
        # one: at a question offering two named choices it most likely means "yes,
        # accept", so treating it as a rerun would do the opposite of what was
        # meant. Anything unrecognized accepts, matching the headless default.
        if text in {"rerun", "re-run", "loop", "retry", "again", "r", "improve"}:
            return True
        logger.info("[validate] the researcher accepted the flagged result.")
        return False

    def _accept_or_loop_prompt(self, normalized: dict, artifact: dict) -> str:
        """The question posed at the accept-or-rerun gate.

        States what was computed, what it was compared against and where that
        reference came from -- the researcher cannot judge a verdict without
        knowing whether the target is an experimental number, a value the plan
        proposed, or nothing comparable at all.
        """
        metric = normalized.get("primary_metric") or {}
        unit = f" {metric['unit']}" if metric.get("unit") else ""
        value = metric.get("value")
        shown = f"{value:.6g}{unit}" if isinstance(value, (int, float)) else "n/a"
        method = ((self._load_artifact("execution_plan") or {})
                  .get("selected_method") or {})
        engine = method.get("calculator") or method.get("tool_name") or "the plan's method"
        comparisons = (artifact.get("cross_validation") or {}).get("comparisons") or []
        if comparisons:
            reference = (f"literature values from the baseline database "
                         f"({len(comparisons)} compared)")
        elif artifact.get("gap_basis") == "tolerance_multiples":
            reference = ("the target this run's own plan proposed, which was not "
                         "taken from a measurement")
        else:
            reference = "no comparable reference"
        return (
            f"Validation says {artifact.get('acceptance_status')}: "
            f"{artifact.get('rationale')}\n"
            f"  computed: {metric.get('name', 'result')} = {shown} (via {engine})\n"
            f"  compared against: {reference}\n"
            f"Bear in mind a method can be right and still miss a reference it was "
            f"never meant to reproduce (a DFT functional against an experimental "
            f"value, say). Rerunning costs another full calculation.\n"
            f"Accept this result, or rerun to try improving it? [accept/rerun]: "
        )

    def _run_molecule(self) -> Optional[str]:
        """The molecule/material this run is about (baseline DB lookup key)."""
        plan = self._load_artifact("execution_plan") or {}
        intent = self._load_artifact("intent_spec") or {}
        material = CodegenEngine._material_brief(plan, intent)
        return material.get("name") or material.get("formula")

    def _normalized_metrics(self, normalized: dict) -> list:
        """All metric dicts (primary first) from a normalized_result artifact."""
        metrics = []
        if isinstance(normalized.get("primary_metric"), dict):
            metrics.append(normalized["primary_metric"])
        metrics.extend(m for m in normalized.get("secondary_metrics") or []
                       if isinstance(m, dict))
        # Non-finite secondaries (a NaN diagnostic next to a finite primary)
        # would poison the baseline comparison, so they never become predictions.
        return [m for m in metrics
                if m.get("name") and isinstance(m.get("value"), (int, float))
                and not isinstance(m.get("value"), bool)
                and math.isfinite(m["value"])]

    def _implausible_metrics(self, normalized: dict) -> list:
        """Metrics that cannot be values of the quantity they claim to be.

        Reads the RAW primary/secondary metrics rather than
        ``_normalized_metrics``, which drops non-finite values: a NaN primary
        should be reported as unusable, not silently leave nothing to grade.
        Never raises -- a broken range table must not fail a finished run.
        """
        if not plausibility.enabled():
            return []
        metrics = []
        if isinstance(normalized.get("primary_metric"), dict):
            metrics.append(normalized["primary_metric"])
        metrics.extend(m for m in normalized.get("secondary_metrics") or []
                       if isinstance(m, dict))
        try:
            return plausibility.check_metrics(metrics)
        except Exception as exc:  # noqa: BLE001 - a backstop must not become a hazard
            logger.info("[validate] plausibility check skipped: %s", exc)
            return []

    def _baseline_db(self):
        """The curated snapshot, backed by live Materials Project values.

        The snapshot is consulted first so a hand-checked record always wins. MP
        is only attached for a material MP could plausibly hold: an explicit
        mp-id, or a formula that the plan resolved as a crystal. A molecular run
        is excluded on purpose -- MP has a solid CO2 entry, and quietly grading a
        gas-phase enthalpy against it would be worse than not comparing at all.
        """
        db = BaselineDB.load()
        material = CodegenEngine._material_brief(
            self._load_artifact("execution_plan") or {},
            self._load_artifact("intent_spec") or {})
        is_crystal = any(material.get(k) for k in
                         ("crystal_system", "space_group", "space_group_number", "phase"))
        if not material.get("mp_id") and not (material.get("formula") and is_crystal):
            return db
        space_group = material.get("space_group_number")
        mp = MaterialsProjectBaselines(
            formula=material.get("formula"),
            mp_id=material.get("mp_id"),
            space_group_number=(int(space_group)
                                if isinstance(space_group, (int, float)) else None),
        )
        if not mp.configured:
            # No key on this host: keep the report's wording honest rather than
            # attaching a source that can only ever answer None.
            logger.info("[validate] Materials Project reference unavailable: "
                        "MP_API_KEY is not set")
            return db
        return ChainedBaselines(db, mp)

    def _baseline_property(self, name) -> str:
        """A metric name as the baseline DB spells the property."""
        text = str(name)
        return self._BASELINE_PROPERTY_ALIASES.get(text.strip().lower(), text)

    def _predictions(self, normalized: dict, molecule: str) -> list:
        """Adapt the interpreted result into baseline-DB predictions.

        A benchmark table becomes one prediction per system, each matched to its
        own literature value -- which is also what lets the validator compute
        RMSE and a correlation, both undefined for a single point.
        """
        entities = normalized.get("entities")
        if isinstance(entities, list) and entities:
            return [
                Prediction(molecule=str(row["entity"]),
                           property=self._baseline_property(row.get("metric")),
                           value=float(row["value"]))
                for row in entities
                if isinstance(row, dict) and row.get("entity")
                and isinstance(row.get("value"), (int, float))
            ]
        predictions = []
        for m in self._normalized_metrics(normalized):
            predictions.append(Prediction(
                molecule=molecule, property=self._baseline_property(m["name"]),
                value=float(m["value"]), unit=m.get("unit"),
                uncertainty=m.get("uncertainty")))
        return predictions

    def _cross_validate(self, normalized: dict) -> str:
        """Grade the normalized result and return the verdict to route on.

        Writes the ``validation_report`` artifact (schema shape + the full
        comparison detail and, when the rerun gate fires, the loop decision).
        The returned verdict may differ from the report's ``acceptance_status``
        in exactly one case: the rerun controller stopped the correction loop,
        so the flagged result is delivered (routed as accepted) with the true
        verdict and stop reason preserved in the report.
        """
        molecule = self._run_molecule()
        predictions = self._predictions(normalized, molecule) if molecule else []
        thresholds = self._acceptance_thresholds()
        result, verdict, report = cross_validate(
            predictions,
            db=self._baseline_db(),
            thresholds=thresholds,
            report_id=f"val-{self.run_id}",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        artifact = asdict(report)
        artifact["rationale"] = verdict.rationale
        artifact["cross_validation"] = result.to_dict()
        artifact["thresholds"] = asdict(thresholds)
        status = verdict.status
        gap = result.mean_relative_error
        # How ``gap`` should be read downstream: a fraction of the literature
        # value, or a multiple of the tolerance the researcher set. correct()
        # needs the distinction to turn it into a calibrated-range signal.
        basis = "relative_error"
        ungraded = self._ungradable_aggregate(normalized, result)
        if ungraded is not None:
            status, artifact["rationale"], gap = ungraded, self._AGGREGATE_NOTE, None
            artifact["acceptance_status"] = status
        elif not result.comparisons:
            # No literature baseline covers this molecule/property: judge
            # against the plan's own acceptance criteria instead of parking
            # every novel system in needs_review.
            status, rationale, gap = self._acceptance_fallback(normalized)
            artifact["acceptance_status"] = status
            artifact["rationale"] = rationale
            basis = "tolerance_multiples"
        else:
            # A matching baseline must not silently retire the researcher's own
            # acceptance criteria: both are checked and the stricter wins, so a
            # result that agrees with the literature but misses the tolerance
            # they asked for is not reported as a clean pass.
            own_status, own_rationale, _ = self._acceptance_fallback(normalized)
            if _SEVERITY.get(own_status, 0) > _SEVERITY.get(status, 0):
                status = own_status
                artifact["acceptance_status"] = status
                artifact["rationale"] = (
                    f"{artifact['rationale']} Held to the run's own acceptance "
                    f"criteria, which are stricter: {own_rationale}")
        # Last, and able to override any of the three branches above: a value that
        # is not physically possible. Every check so far is comparative, so a novel
        # system with a null acceptance target reaches this point graded "accepted"
        # on the honest grounds that nothing could grade it -- which is how a CO2
        # heat of formation of -27452 kJ/mol was delivered as a result. A bound
        # needs no reference value, so it is the only check that covers that case.
        implausible = self._implausible_metrics(normalized)
        if implausible:
            status = "rejected"
            artifact["acceptance_status"] = status
            artifact["plausibility"] = [asdict(f) for f in implausible]
            artifact["rationale"] = (
                "Rejected on physical grounds: "
                + " ".join(f.message() for f in implausible)
                + f" (Previous verdict, on agreement alone: {artifact['rationale']})")
            # No relative error to report: the value is not near a reference, it
            # is off the scale. Leaving gap None earns exactly one correction pass
            # (see _gate_rerun), which is what a regenerated script needs.
            gap = None
        artifact["gap"] = gap
        artifact["gap_basis"] = basis
        logger.info("[validate] %s -- %s", status, artifact["rationale"])
        # Ask the researcher before spending another calculation. Deliberately
        # BEFORE _gate_rerun: everything above only reads, so if this suspends
        # for the answer the re-entry re-grades identically and no metric has
        # been recorded twice (which would read as 0% improvement -> converged).
        if status != "accepted" and not self._accept_or_loop(normalized, artifact):
            artifact["rerun"] = {
                "decision": "stop",
                "stop_reason": "researcher_accepted",
                "reason": "The researcher accepted this result as it stands.",
                "iteration": self._rerun.iteration,
                "final_verdict": status,
                "disposition": "accepted_by_researcher",
            }
            self.context.artifacts["validation_report"] = self._write_artifact(
                "validation_report", artifact)
            return "accepted"
        self._restore_rerun_budget()
        final = self._gate_rerun(status, gap, artifact)
        self.context.artifacts["validation_report"] = self._write_artifact(
            "validation_report", artifact)
        return final

    _AGGREGATE_NOTE = (
        "This run produced results for several systems, but the output does not "
        "say which row belongs to which system, so the values were averaged into "
        "one number. A mean cannot be checked against a single system's "
        "literature value -- agreement here would not mean the run is right. "
        "Add an identifying column (molecule, formula or SMILES) to the results "
        "table and re-run to have each system validated against its own "
        "reference."
    )

    def _ungradable_aggregate(self, normalized: dict, result) -> Optional[str]:
        """``"needs_review"`` when the result is a mean nothing can check, else None.

        Errors in opposite directions cancel, so an averaged benchmark can score
        ~0% against one molecule's reference while every prediction is badly
        wrong. Identified rows are graded individually instead; unidentified ones
        cannot honestly be graded at all.
        """
        if normalized.get("entities"):
            return None
        samples = (normalized.get("metadata") or {}).get("primary_samples")
        if not isinstance(samples, int) or samples < 2:
            return None
        if not result.comparisons:
            return None
        return "needs_review"

    def _acceptance_thresholds(self) -> AcceptanceThresholds:
        """The agreement thresholds to grade against.

        Story 6.2's 15%/30% by default, overridable per deployment via
        ``TWAIN_ACCEPT_BELOW`` / ``TWAIN_REVIEW_BELOW`` (fractions) and per run
        via ``acceptance_thresholds`` on the plan. Malformed values fall back to
        the defaults rather than failing the run.
        """
        plan = (self._load_artifact("execution_plan") or {})
        configured = plan.get("acceptance_thresholds")
        configured = configured if isinstance(configured, dict) else {}
        defaults = AcceptanceThresholds()
        values = {}
        for field_name, env in (("accept_below", "TWAIN_ACCEPT_BELOW"),
                                ("review_below", "TWAIN_REVIEW_BELOW")):
            raw = configured.get(field_name, os.environ.get(env))
            try:
                values[field_name] = float(raw)
            except (TypeError, ValueError):
                values[field_name] = getattr(defaults, field_name)
        try:
            return AcceptanceThresholds(**values)
        except ValueError:  # e.g. accept_below > review_below
            logger.info("[validate] ignoring incoherent acceptance thresholds "
                        "%s; using the defaults.", values)
            return defaults

    def _acceptance_fallback(self, normalized: dict):
        """Judge against the plan's acceptance criteria (target +/- tolerance).

        Returns ``(status, rationale, gap)`` where gap is the worst relative
        miss (drives the rerun controller's convergence check). Within tolerance
        -> accepted; within twice the tolerance -> needs_review; beyond that ->
        rejected. With no criteria matching an extracted metric there is nothing
        to judge, so the result is delivered as-is (accepted) and the rationale
        says exactly that.
        """
        plan = self._load_artifact("execution_plan") or {}
        criteria = [c for c in plan.get("acceptance_metrics") or []
                    if isinstance(c, dict) and c.get("metric_name") is not None]
        metrics = self._normalized_metrics(normalized)
        by_name = {str(m["name"]): m for m in metrics}

        checked, worst_status, worst_gap = [], "accepted", None
        for criterion in criteria:
            match = self._pick_metric(list(by_name), [criterion["metric_name"]])
            if match is None:
                continue
            try:
                value = float(by_name[match]["value"])
                target = float(criterion.get("target_value", 0.0))
                tolerance = abs(float(criterion.get("tolerance", 0.0)))
            except (TypeError, ValueError):
                continue  # malformed criterion -> nothing to judge against
            miss = abs(value - target)
            if miss <= tolerance:
                status = "accepted"
            elif tolerance and miss <= 2 * tolerance:
                status = "needs_review"
            else:
                status = "rejected"
            checked.append(
                f"{criterion['metric_name']}: {value:.4g} vs target {target:.4g} "
                f"+/- {tolerance:.4g} -> {status}")
            if _SEVERITY[status] > _SEVERITY[worst_status]:
                worst_status = status
            rel_miss = miss / tolerance if tolerance else miss
            worst_gap = rel_miss if worst_gap is None else max(worst_gap, rel_miss)

        if not checked:
            return ("accepted",
                    "No literature baseline or matching acceptance criterion "
                    "covers this result; delivered without external validation.",
                    None)
        return (worst_status,
                "Judged against the plan's acceptance criteria (no literature "
                "baseline): " + "; ".join(checked),
                worst_gap)

    def _restore_rerun_budget(self) -> None:
        """Rehydrate the correction budget from the last validation report.

        ``_rerun`` is in-memory but a run spans job slices, so each pass would
        otherwise start at zero and the iteration cap could never be reached --
        the loop would run until the orchestrator's backstop failed the run,
        the abort the cap exists to replace. Only restores when this process has
        counted nothing yet, so a multi-pass run keeps its live counter.
        """
        if self._rerun.iteration or self._rerun.metric_history:
            return
        previous = (self._load_artifact("validation_report") or {}).get("rerun")
        if not isinstance(previous, dict):
            return
        iteration = previous.get("iteration")
        if isinstance(iteration, int) and iteration > 0:
            self._rerun.iteration = min(iteration, self._rerun.policy.max_iterations)
        history = previous.get("metric_history")
        if isinstance(history, list):
            self._rerun.metric_history = [
                float(v) for v in history
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v)
            ]

    def _gate_rerun(self, status: str, gap: Optional[float], artifact: dict) -> str:
        """Bound the correction loop (Story 6.3) and return the routing verdict.

        Accepted results pass straight through. For needs_review/rejected, the
        rerun controller decides whether another correction pass is worthwhile
        (iteration cap, convergence, cost-benefit). When it says stop, the
        flagged result is delivered -- routed as accepted so the run terminates
        -- with the true verdict, stop reason and disposition recorded in the
        validation report.
        """
        if status == "accepted":
            return status
        if gap is not None:
            self._rerun.record_metric(gap)
        # There is no per-iteration cost model yet, so the cost-benefit arm of
        # the controller is inert (cost 0) and the loop is bounded by the
        # iteration cap and the convergence check. A gap we could not measure
        # still earns one pass rather than being scored as "no benefit".
        decision = self._rerun.decide(
            expected_benefit=gap if gap is not None else 1.0,
            estimated_cost=0.0)
        if decision.should_rerun:
            self._rerun.begin_iteration()
            artifact["rerun"] = {
                "decision": "rerun",
                "iteration": self._rerun.iteration,
                # Carried so the next slice -- a different process, with a fresh
                # controller -- can restore the budget instead of starting over.
                "metric_history": list(self._rerun.metric_history),
                "reason": decision.reason,
            }
            return status
        artifact["rerun"] = {
            "decision": "stop",
            "stop_reason": decision.stop_reason,
            "iteration": self._rerun.iteration,
            "metric_history": list(self._rerun.metric_history),
            "reason": decision.reason,
            "final_verdict": status,
            "disposition": "delivered_for_researcher_review",
        }
        logger.info("[validate] %s", decision.reason)
        logger.info("[validate] Delivering the result flagged for review "
                    "(verdict on record: %s).", status)
        return "accepted"

    def accept(self) -> State:
        return State.TERMINATE

    def correct(self) -> State:
        """Diagnose the marginal validation and record a CorrectionPlan (6.3).

        Builds run evidence from the validation report, classifies the failure
        mode, and writes the proposed corrections as the ``correction_plan``
        artifact -- the auditable record of what the system thinks went wrong
        and what it would change. Proposals are recorded, not yet auto-applied
        to the execution plan; the rerun controller (see validate()) bounds how
        many times this loop can come back around. Always returns BUILD, the
        only transition the guard table allows out of CORRECT.
        """
        report = self._load_artifact("validation_report") or {}
        cross = report.get("cross_validation") or {}
        gap = report.get("gap")
        if gap is None:
            gap = cross.get("mean_relative_error")
        context = CorrectionContext(
            validation_report_id=str((report.get("metadata") or {}).get("ID") or ""),
            iteration_count=self._rerun.iteration,
            gap=gap,
            next_candidate=self._next_discovery_candidate(),
            iteration_allowance=max(
                1, self._rerun.policy.max_iterations - self._rerun.iteration),
            plan_id=f"corr-{self.run_id}",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        # No scorer in failure_classifier reads relative_error, so the mode is
        # UNKNOWN and the generic plan below is the honest outcome. Deriving a
        # mode from the gap would mean dividing by the run's numerical-precision
        # uncertainty, making the diagnosis track how tightly the script
        # converged rather than the science. The real signals (rejected input,
        # diverged loss, OOD score) belong to the stages that can observe them.
        reflection = reflect(RunEvidence(relative_error=gap), context)
        plan = reflection.correction_plan
        if plan is None:
            # UNKNOWN failure mode -- no strategy owns it. Record an honest
            # generic plan: try the next-ranked discovery tool when one exists,
            # otherwise rerun unchanged to confirm reproducibility, and escalate
            # if that doesn't move the needle.
            if context.next_candidate:
                correction = {
                    "modification_type": "switch_model",
                    "target": "model",
                    "new_value": context.next_candidate,
                    "rationale": "No specific failure signal fired; the next-ranked "
                                 "discovery candidate is the best untried lever.",
                }
            else:
                correction = {
                    "modification_type": "relax_constraints",
                    "target": "param",
                    "new_value": {"action": "rerun_unchanged"},
                    "rationale": "No specific failure signal fired and no alternative "
                                 "tool is available; rerun to confirm reproducibility "
                                 "before escalating.",
                }
            plan = build_plan(
                reflection.diagnosis.explanation
                or "No diagnostic signal fired; failure mode undetermined.",
                [correction],
                "Escalate to the researcher with the validation report.",
                context,
                primary_metric_delta=0.0,
                confidence=reflection.diagnosis.confidence,
            )
        plan["diagnosis_detail"] = reflection.diagnosis.to_dict()
        self.context.artifacts["correction_plan"] = self._write_artifact(
            "correction_plan", plan)
        proposals = ", ".join(
            str(c.get("modification_type"))
            for c in plan.get("proposed_corrections", []))
        logger.info("[correct] diagnosis: %s (confidence %s); proposed: %s. "
                    "Rerunning the build (iteration %s/%s).",
                    reflection.diagnosis.mode.value,
                    reflection.diagnosis.confidence, proposals,
                    self._rerun.iteration, self._rerun.policy.max_iterations)
        return State.BUILD

    def _next_discovery_candidate(self) -> Optional[str]:
        """The highest-ranked discovery candidate that isn't the tool just used."""
        discovery = self._load_artifact("discovery") or {}
        plan = self._load_artifact("execution_plan") or {}
        current = str((plan.get("selected_method") or {}).get("tool_name") or "").lower()
        for candidate in discovery.get("candidates") or []:
            name = candidate.get("name") or candidate.get("id")
            if name and str(name).lower() != current:
                return str(name)
        return None

    def replan(self) -> State:
        return State.PLAN