"""Pytest configuration for the test suite.

Inserts the repo root onto sys.path so tests can import the top-level
packages (intake/, goal_decomposer/, plan_synthesizer/) regardless of how
pytest is invoked (e.g. `pixi run pytest tests/`). Living under tests/ rather
than at the repo root means pytest does not add the repo root automatically,
so we add it here explicitly.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
