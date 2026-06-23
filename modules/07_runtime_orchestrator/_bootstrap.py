"""sys.path bootstrap for the runtime orchestrator package.

The ``modules/`` tree uses directory names that are not valid Python
identifiers (``07_runtime_orchestrator``, ``14_provenance_memory``) or are
underscore-prefixed (``_16_agent_mesh_control_plane``). They therefore cannot
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

Every orchestrator module imports this first so it works the same whether it is
run as a script (``python orchestrator.py``) or imported from a test that has
put this directory on ``sys.path``.
"""
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent          # modules/07_runtime_orchestrator
_MODULES_DIR = _THIS_DIR.parent                       # modules
_REPO_ROOT = _MODULES_DIR.parent                      # repo root

_PATHS = (
    _REPO_ROOT,
    _MODULES_DIR / "_16_agent_mesh_control_plane",
    _MODULES_DIR / "14_provenance_memory",
    _THIS_DIR,
)


def ensure_paths() -> None:
    """Idempotently prepend every required directory to ``sys.path``."""
    for path in _PATHS:
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)


# Repo root is exported so modules can locate fixtures/schemas without
# recomputing the relative climb.
REPO_ROOT = _REPO_ROOT

ensure_paths()
