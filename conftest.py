"""Repo-root conftest.

Empty by design. Its presence makes pytest treat the repo root as the
rootdir and inserts it into sys.path, so top-level packages
(intake/, goal_decomposer/, plan_synthesizer/) are importable from tests
regardless of how pytest is invoked (e.g. `pixi run pytest tests/`).
"""
