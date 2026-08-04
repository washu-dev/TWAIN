"""sys.path bootstrap for the runtime orchestrator package.

The ``modules/`` tree uses directory names that are not valid Python
identifiers (``07_runtime_orchestrator``, ``14_provenance_memory``) or are
numeric-prefixed (``16_agent_mesh_control_plane``). They therefore cannot
be imported as dotted packages, and the control-plane files import their
siblings by bare name (``from states import State``). The only mechanism that
makes every dependency importable is to place the relevant directories on
``sys.path`` and import by bare name -- exactly what ``tests/unit/
test_state_machine.py`` already does for the state machine.

Importing this module (``import _bootstrap``) is idempotent and wires up:

* the repository root           -> top-level packages (``intake``,
                                   ``plan_synthesizer``, ``provenance_memory``,
                                   ``result_interpreter`` ...)
* the agent-mesh control plane  -> ``states``, ``statemachine``, ``event``,
                                   ``event_bus``, ``retry_policy`` ...
* the provenance-memory module  -> ``store``
* this orchestrator directory   -> ``session``, ``agent_runner``,
                                   ``error_handler``, ``orchestrator``

Some control-plane modules also use *qualified* imports of clean package names
that don't match their numbered directory (``from intake.intent_spec import
IntentSpec`` -> ``modules/01_intake_nlu``). Those names are registered as
namespace-package aliases in ``sys.modules`` -- mirroring ``tests/conftest.py``
-- so the orchestrator runs the same standalone as it does under pytest.

Every orchestrator module imports this first so it works the same whether it is
run as a script (``python orchestrator.py``) or imported from a test that has
put this directory on ``sys.path``.
"""
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent          # modules/07_runtime_orchestrator
_MODULES_DIR = _THIS_DIR.parent                       # modules
_REPO_ROOT = _MODULES_DIR.parent                      # repo root

_PATHS = (
    _REPO_ROOT,
    _MODULES_DIR / "16_agent_mesh_control_plane",
    _MODULES_DIR / "14_provenance_memory",
    _THIS_DIR,
)

# Clean package name -> numbered directory holding its submodules. Kept in sync
# with ``tests/conftest.py`` so qualified imports resolve identically whether the
# orchestrator is driven by pytest or run as a script.
_PACKAGE_ALIASES = {
    "intake": _MODULES_DIR / "01_intake_nlu",
    "goal_decomposer": _MODULES_DIR / "03_goal_decomposer",
    "method_discovery": _MODULES_DIR / "04_method_discovery",
    "plan_synthesizer": _MODULES_DIR / "05_plan_synthesis",
    "execution_adapter": _MODULES_DIR / "08_execution_adapter",
    "result_interpreter": _MODULES_DIR / "10_result_interpreter",
    "cross_validation": _MODULES_DIR / "11_cross_validation",
    "self_correction": _MODULES_DIR / "12_self_correction_reflection",
    "provenance_memory": _MODULES_DIR / "14_provenance_memory",
    "code_gen": _MODULES_DIR / "06_code_configuration_builder",
}


def _register_alias(name: str, path: Path) -> None:
    """Register ``name`` as a namespace package rooted at ``path`` (idempotent)."""
    if name in sys.modules or not path.is_dir():
        return
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    sys.modules[name] = importlib.util.module_from_spec(spec)


def ensure_paths() -> None:
    """Idempotently prepend required dirs to ``sys.path`` and register aliases."""
    for path in _PATHS:
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)
    for name, path in _PACKAGE_ALIASES.items():
        _register_alias(name, path)


# Repo root is exported so modules can locate fixtures/schemas without
# recomputing the relative climb.
REPO_ROOT = _REPO_ROOT

ensure_paths()
