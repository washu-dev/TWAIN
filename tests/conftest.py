"""Pytest bootstrap that makes the `modules/NN_*` layout importable.

The pipeline code lives in numerically-prefixed directories (e.g.
`modules/04_method_discovery`) whose names are not valid Python identifiers, so
they cannot be imported directly. To keep existing qualified imports working
(`from method_discovery.registry_loader import ...`, `from plan_synthesizer...`)
without rewriting every module and test, this conftest:

  * puts the repo root on sys.path (for any remaining top-level imports),
  * puts module dirs whose files use *bare* sibling imports (the control plane,
    e.g. `from states import State`) on sys.path, and
  * registers clean package *aliases* in sys.modules that point at the numbered
    directories, so `import method_discovery.X` resolves to
    `modules/04_method_discovery/X`.

Aliases are pre-registered in sys.modules, so they take precedence over any
stale top-level package of the same name that may still exist on disk during a
migration.
"""
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULES = REPO_ROOT / "modules"

# Clean package name -> directory holding its submodules. Feel free to add more packages here.
_PACKAGE_ALIASES = {
    "intake": MODULES / "01_intake_nlu",
    "goal_decomposer": MODULES / "03_goal_decomposer",
    "method_discovery": MODULES / "04_method_discovery",
    "plan_synthesizer": MODULES / "05_plan_synthesis",
    "execution_adapter": MODULES / "08_execution_adapter",
    "result_interpreter": MODULES / "10_result_interpreter",
    "cross_validation": MODULES / "11_cross_validation",
    "self_correction": MODULES / "12_self_correction_reflection",
    "provenance_memory": MODULES / "14_provenance_memory",
    "code_gen": MODULES / "06_code_configuration_builder",
    "execution_adapter": MODULES / "08_execution_adapter",
}

# Dirs whose modules import siblings by bare name (e.g. `from states import State`). Feel free to add more directories here.
_BARE_IMPORT_DIRS = [
    MODULES / "16_agent_mesh_control_plane",
]


def _register_alias(name: str, path: Path) -> None:
    """Register `name` as a namespace package rooted at `path`."""
    if name in sys.modules or not path.is_dir():
        return
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(path)]
    sys.modules[name] = importlib.util.module_from_spec(spec)


def _bootstrap() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    for directory in _BARE_IMPORT_DIRS:
        if directory.is_dir() and str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
    for name, path in _PACKAGE_ALIASES.items():
        _register_alias(name, path)


_bootstrap()
