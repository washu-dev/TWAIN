"""Publish what TWAIN can actually run, for the app's library list.

The registries declare what TWAIN *knows about*; whether a library is *installed*
is a property of the cluster envs under ``twain-envs/``. Only the runner can see
those -- the API is a separate deployable with no access to that filesystem -- so
the runner probes and publishes, and the API serves the table.

That split is also what makes the list self-maintaining: provisioning a new env
and restarting the runner (which ``auto_update.sh`` already does on every deploy)
re-probes everything, so installing something updates the homepage without anyone
editing a list.

Probing is one subprocess per env, not per library: each env's interpreter is
handed every import name at once and answers with ``find_spec`` verdicts. Twenty
seven registry entries across six envs is six processes, and ``find_spec`` avoids
importing heavy scientific packages just to prove they exist.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_REGISTRY = REPO_ROOT / "configs" / "discovery_registry.json"
CALCULATOR_REGISTRY = REPO_ROOT / "configs" / "calculator_registry.json"

# Asked to report on every import at once; stdin carries the list so no name has
# to survive shell quoting.
_PROBE = (
    "import importlib.util, json, sys\n"
    "out = {}\n"
    "for module in json.load(sys.stdin):\n"
    "    try:\n"
    "        out[module] = importlib.util.find_spec(module) is not None\n"
    "    except Exception:\n"
    "        out[module] = False\n"
    "json.dump(out, sys.stdout)\n"
)


def _ensure_module_paths() -> None:
    """Register the ``modules/NN_*`` package aliases this module imports through.

    ``code_gen`` and ``execution_adapter`` are aliases for directories whose real
    names are not importable (``06_code_configuration_builder``,
    ``08_execution_adapter``); ``_bootstrap`` registers them. Doing that here,
    rather than assuming someone already has, is the whole point: publishing runs
    from ``main()`` BEFORE the first job builds an orchestrator, so on the cluster
    ``_bootstrap`` had not been imported yet, ``ClusterProfile`` was unreachable,
    and the runner published all 27 entries as "no provisioned envs were visible"
    while eight fully provisioned envs sat on disk. Under pytest the aliases are
    already in place (``tests/conftest.py``), which is exactly why no unit test
    saw it -- see ``test_the_runners_own_import_path_resolves_the_envs_root``.

    Idempotent: the sys.path guard and the import cache make repeat calls free.
    """
    orchestrator_dir = str(REPO_ROOT / "modules" / "07_runtime_orchestrator")
    if orchestrator_dir not in sys.path:
        sys.path.insert(0, orchestrator_dir)
    try:
        import _bootstrap  # noqa: F401 - registers the aliases as an import side effect
    except ImportError as exc:  # pragma: no cover - a broken checkout, not a config
        logger.warning("[capabilities] module path bootstrap failed: %s", exc)


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[capabilities] could not read %s: %s", path.name, exc)
        return {}


def _import_name_for(display_name: str) -> str | None:
    """The module a library is imported as, via the dependency inferencer.

    The calculator registry states ``import_name`` outright; the discovery
    registry does not, and guessing ``name.lower()`` is wrong for exactly the
    entries that matter ("Open Babel", "OpenFF Toolkit", "scikit-learn"). The
    inferencer already owns that mapping for codegen, so reuse it rather than
    keeping a second table that can disagree with the first.
    """
    _ensure_module_paths()
    try:
        from code_gen import dependency_inferencer as depinf
    except ImportError as exc:  # pragma: no cover - only when module paths aren't set up
        # Logged, not silent: the fallback returns a plausible-looking name for
        # every entry ("Open Babel" -> "open babel"), so a bootstrap failure used
        # to surface only as a list where nothing was installed.
        logger.warning("[capabilities] dependency inferencer unavailable (%s); "
                       "guessing the import name for %r", exc, display_name)
        return display_name.strip().lower() or None
    try:
        deps = depinf.import_names(display_name)
    except Exception:  # noqa: BLE001 - an unknown library is not an error here
        return None
    return deps[0].import_name if deps else None


def registry_entries() -> list[dict]:
    """Every library and calculator TWAIN knows about, with its import name."""
    entries: list[dict] = []
    for item in _load(DISCOVERY_REGISTRY).get("entries") or []:
        name = item.get("name")
        if not name:
            continue
        entries.append({
            "kind": "library",
            "name": name,
            "import_name": _import_name_for(name),
            "version": item.get("version"),
            "description": item.get("description"),
        })
    for item in _load(CALCULATOR_REGISTRY).get("calculators") or []:
        name = item.get("name")
        if not name:
            continue
        entries.append({
            "kind": "calculator",
            "name": name,
            "import_name": item.get("import_name") or _import_name_for(name),
            # The binary, for engines that are separate programs. This is the ONLY
            # honest test for them: ase.calculators.espresso imports anywhere ASE
            # is installed, so an import probe reported Quantum ESPRESSO, ABINIT,
            # CP2K and NWChem as available on a cluster that had none of them --
            # a false claim of capability, which is the one thing this list must
            # never make. Calculators that really are Python (GPAW, MatGL, EMT)
            # declare no executable and keep the import check.
            "executable": item.get("executable"),
            "version": item.get("version"),
            "description": item.get("description"),
        })
    return entries


def _envs_root() -> Path | None:
    root = os.environ.get("TWAIN_ENVS_ROOT")
    if root:
        return Path(root)
    _ensure_module_paths()
    try:
        from execution_adapter.cluster_profile import ClusterProfile
        profile = ClusterProfile.load(os.environ.get("TWAIN_SLURM_CLUSTER", "compute2"))
    except Exception as exc:  # noqa: BLE001 - no profile is a normal local case
        logger.info("[capabilities] no cluster envs root (%s)", exc)
        return None
    return Path(profile.envs_root) if getattr(profile, "envs_root", None) else None


def env_interpreters(envs_root: Path | None = None) -> dict[str, str]:
    """``{env name: python path}`` for the envs that actually exist on disk.

    Read from the filesystem rather than from the env SPECS: a spec that has
    never been provisioned would otherwise be reported as capability the cluster
    does not have, which is the opposite of what this table is for.
    """
    root = envs_root if envs_root is not None else _envs_root()
    if root is None:
        return {}
    try:
        candidates = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return {}
    found = {}
    for env in candidates:
        python = env / "bin" / "python"
        if python.exists():
            found[env.name] = str(python)
    return found


def _probe(python: str, modules: list[str], timeout: float) -> dict[str, bool]:
    """find_spec verdicts from one interpreter, or empty when it can't be asked."""
    if not modules:
        return {}
    try:
        # noqa: S603 -- nothing here is untrusted. The argv is a fixed constant
        # (_PROBE) plus an interpreter path derived from deployment config
        # (TWAIN_ENVS_ROOT / the cluster profile's envs_root), never from a request
        # or a registry field. The module names are the only variable input and they
        # travel on STDIN precisely so they never reach argv, and shell=False.
        done = subprocess.run(  # noqa: S603
            [python, "-c", _PROBE],
            input=json.dumps(modules),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if done.returncode != 0:
            logger.info("[capabilities] probe via %s exited %s", python, done.returncode)
            return {}
        parsed = json.loads(done.stdout or "{}")
        return {k: bool(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.info("[capabilities] probe via %s failed: %s", python, exc)
        return {}


def _env_key(name: str) -> str:
    """A library/calculator name reduced to how an env is named after it.

    "Quantum ESPRESSO" -> "quantumespresso", "DFTB+" -> "dftb". Used only to
    prefer the env that exists FOR this tool when attributing a hit, so the list
    does not report that the abinit env provides NWChem.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _search_order(envs: dict[str, str], name: str) -> list[str]:
    """Envs to credit for a hit, most specific first.

    Alphabetical order was actively misleading: every env inherits the common
    stack, so "abinit" (first alphabetically) was credited with providing RDKit,
    ASE, Pymatgen and NWChem. Prefer the env named after the tool, then 'default'
    where the shared stack actually lives, then the rest for stability.
    """
    key = _env_key(name)
    named = [e for e in envs if _env_key(e) and (_env_key(e) in key or key in _env_key(e))]
    rest = [e for e in sorted(envs) if e not in named and e != "default"]
    default = ["default"] if "default" in envs else []
    return named + default + rest


def resolve_availability(entries: list[dict] | None = None,
                         envs: dict[str, str] | None = None,
                         *, envs_root: Path | None = None,
                         timeout: float = 90.0) -> list[dict]:
    """Decide installed/not for each entry, naming the env that provides it."""
    entries = registry_entries() if entries is None else entries
    envs = env_interpreters(envs_root) if envs is None else envs
    modules = sorted({e["import_name"] for e in entries if e.get("import_name")})

    verdicts: dict[str, dict[str, bool]] = {}
    for env_name in sorted(envs):
        verdicts[env_name] = _probe(envs[env_name], modules, timeout)

    rows = []
    for entry in entries:
        executable = entry.get("executable")
        module = entry.get("import_name")
        env = None
        detail = ""
        if executable:
            # A separate program: the binary in the env's bin/ is the only proof.
            for env_name in _search_order(envs, entry["name"]):
                interpreter = envs.get(env_name)
                if interpreter and (Path(interpreter).parent / executable).exists():
                    env = env_name
                    detail = f"'{executable}' present in the {env_name} env"
                    break
            if env is None:
                detail = (f"'{executable}' was not found in any provisioned env"
                          if envs else
                          "no provisioned envs were visible to the runner")
        elif module:
            for env_name in _search_order(envs, entry["name"]):
                if verdicts.get(env_name, {}).get(module):
                    env = env_name
                    detail = f"importable as '{module}' in the {env_name} env"
                    break
            if env is None:
                detail = (f"'{module}' did not import in any provisioned env"
                          if envs else
                          "no provisioned envs were visible to the runner")
        else:
            detail = "no import name is known for this entry"
        rows.append({**entry, "installed": env is not None, "env": env, "detail": detail})
    return rows


def publish(db, *, timeout: float = 90.0) -> int:
    """Probe and record availability. Never raises -- this is not worth a run."""
    try:
        rows = resolve_availability(timeout=timeout)
        if not rows:
            logger.info("[capabilities] nothing to publish (registries unreadable?)")
            return 0
        db.replace_library_availability(rows)
        installed = sum(1 for r in rows if r["installed"])
        logger.info("[capabilities] published %s entries, %s installed",
                    len(rows), installed)
        return len(rows)
    except Exception as exc:  # noqa: BLE001 - a stale list beats a dead runner
        logger.warning("[capabilities] publish failed: %s", exc)
        return 0
