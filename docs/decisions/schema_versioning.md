# ADR: Schema Versioning & Migration Policy

**Status**: Accepted
**Date**: 2026-06-16
**Story**: 1.6 (Contract Validation Test Suite)
**Applies to**: every contract in `schemas/*.schema.json`

---

## Context

The pipeline is contract-first: modules communicate through the JSON Schemas in
`schemas/`. Once modules depend on a contract, an uncoordinated change can
silently break downstream consumers. We need a written rule for *how* contracts
may change and *how* old documents are migrated to new versions.

## Decision

### 1. Versioning scheme

Each schema carries a semantic version `MAJOR.MINOR.PATCH`, recorded in the
schema's `$id` and mirrored by a `version` field on the document `metadata`:

```
"$id": "GoalGraphSchema/1.2.0"
```

- **PATCH** — editorial only (descriptions, examples, wording). No structural effect.
- **MINOR** — backward-compatible additions (new *optional* field, new enum value
  appended, loosened constraint). Existing valid documents stay valid.
- **MAJOR** — backward-incompatible change (remove/rename a field, make an
  optional field required, tighten a type or constraint, remove an enum value).

`$id` values must be **unique** across all schemas; the contract suite enforces
this (`test_schema_ids_are_unique`).

### 2. What counts as breaking

Breaking (MAJOR): removing a field, renaming a field, changing a field's type,
making an optional field required, removing/renaming an enum value, adding a new
`required` entry.

Non-breaking (MINOR/PATCH): adding an optional field, appending an enum value,
relaxing a constraint, editing descriptions/examples.

### 3. Deprecation strategy

A field/value is never removed abruptly. To retire one:

1. **Mark deprecated** — add `"deprecated": true` and a note in its `description`
   pointing to the replacement. Bump **MINOR**. The field keeps working.
2. **Grace period** — keep the deprecated field for at least one MINOR release so
   consumers can migrate. Producers should populate both old and new fields
   during this window.
3. **Remove** — delete the field in the next **MAJOR** release and ship a
   migration script (below).

### 4. Migration scripts

Breaking (MAJOR) changes ship with a migration that converts a document from the
previous major version to the new one:

```
scripts/migrations/<schema>_<from>_to_<to>.py
```

Each migration exposes:

```python
def migrate(document: dict) -> dict:
    """Transform a document valid under <from> into one valid under <to>."""
```

Conventions:
- Migrations are **pure** (no I/O) and **idempotent** where possible.
- Each migration has a unit test: an old-version example in →
  new-version document that validates against the new schema.
- Migrations are chained for multi-step upgrades (1.x -> 2.x -> 3.x).

### 5. Enforcement (this story's test suite)

`tests/contract/test_schema_consistency.py` runs on every commit and checks:
- every schema passes JSON Schema 2020-12 meta-validation,
- `$id`s are unique,
- every schema has at least one example and every example validates,
- cross-schema `$ref`s resolve with no cycles.

`tests/integration/test_e2e_happy_path.py` validates that the full
request → plan → execution → result → validation flow satisfies every contract
together.

## Consequences

- Contract drift (like the earlier `ExecutionPlanSchema`/`IntentSchema` `$id`
  collision) is caught automatically rather than in downstream modules.
- Breaking changes are explicit, announced via deprecation, and accompanied by a
  runnable migration, keeping historical provenance documents replayable.
- Slight overhead per breaking change (write + test a migration), accepted as the
  cost of stable inter-module contracts.
