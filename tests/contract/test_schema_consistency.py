"""Story 1.6: Contract validation test suite.

Verifies that all inter-module JSON Schema contracts are mutually consistent so
a schema change can't silently break downstream modules. Checks:

  * Every schema passes JSON Schema 2020-12 meta-validation.
  * `$id` values are unique across schemas (no collisions).
  * Every schema ships at least one example, and every example validates.
  * Cross-schema references ($ref) resolve and contain no cycles.

Run from the repo root with:  pixi run pytest tests/contract/test_schema_consistency.py
"""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "schemas"
EXAMPLE_DIR = SCHEMA_DIR / "examples"

SCHEMA_PATHS = sorted(SCHEMA_DIR.glob("*.schema.json"))
EXAMPLE_PATHS = sorted(EXAMPLE_DIR.glob("*.json"))


def _load(path):
    with open(path, "r") as f:
        return json.load(f)


def _schema_stem(path: Path) -> str:
    """`foo.schema.json` -> `foo`."""
    return path.name[: -len(".schema.json")]


def _examples_for(stem: str):
    """Examples whose filename starts with the schema stem (e.g. goal_graph ->
    goal_graph_molecular_solubility.json)."""
    return [p for p in EXAMPLE_PATHS if p.stem.startswith(stem)]


def test_schema_directory_is_not_empty():
    assert SCHEMA_PATHS, "no *.schema.json files found under schemas/"


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=lambda p: p.name)
def test_schema_passes_meta_validation(schema_path):
    Draft202012Validator.check_schema(_load(schema_path))


def test_schema_ids_are_unique():
    ids = {}
    for path in SCHEMA_PATHS:
        schema = _load(path)
        sid = schema.get("$id")
        assert sid, f"{path.name} is missing a $id"
        assert sid not in ids, (
            f"$id collision: {path.name} and {ids[sid]} both declare $id={sid!r}"
        )
        ids[sid] = path.name


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=lambda p: p.name)
def test_every_schema_has_an_example(schema_path):
    stem = _schema_stem(schema_path)
    assert _examples_for(stem), f"no example file found for {schema_path.name}"


@pytest.mark.parametrize("example_path", EXAMPLE_PATHS, ids=lambda p: p.name)
def test_example_validates_against_its_schema(example_path):
    # Match an example to the longest schema stem it starts with (avoids a
    # short stem accidentally claiming another schema's example).
    candidates = [p for p in SCHEMA_PATHS if example_path.stem.startswith(_schema_stem(p))]
    assert candidates, f"no schema matches example {example_path.name}"
    schema_path = max(candidates, key=lambda p: len(_schema_stem(p)))
    Draft202012Validator(_load(schema_path)).validate(_load(example_path))


def test_cross_schema_refs_resolve_without_cycles():
    """Collect every $ref across schemas; ensure file-based refs resolve and
    the reference graph is acyclic. (Today the schemas are standalone, so this
    is a guard against future $ref imports introducing dangling/circular deps.)"""

    def collect_refs(node):
        refs = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    refs.append(value)
                else:
                    refs.extend(collect_refs(value))
        elif isinstance(node, list):
            for item in node:
                refs.extend(collect_refs(item))
        return refs

    graph = {}
    for path in SCHEMA_PATHS:
        file_refs = []
        for ref in collect_refs(_load(path)):
            # Internal pointers (e.g. "#/$defs/foo") are self-contained; only
            # external file references can dangle or form cross-file cycles.
            if not ref.startswith("#"):
                target = ref.split("#", 1)[0]
                resolved = (path.parent / target).resolve()
                assert resolved.exists(), f"{path.name} references missing schema {target!r}"
                file_refs.append(resolved)
        graph[path.resolve()] = file_refs

    visiting, visited = set(), set()

    def has_cycle(node):
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for neighbor in graph.get(node, []):
            if has_cycle(neighbor):
                return True
        visiting.discard(node)
        visited.add(node)
        return False

    for node in graph:
        assert not has_cycle(node), f"circular schema reference involving {node.name}"
