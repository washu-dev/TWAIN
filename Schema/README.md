# TWAIN Schema

This folder defines the **`IntentSpecification`** — the structured format that describes a single TWAIN simulation request. Every request that enters the pipeline is expected to validate against this schema before it is acted on.

## Files

| File | Purpose |
| --- | --- |
| `Schema.json` | The authoritative [JSON Schema](https://json-schema.org/) (draft 2020-12). Use this for validation. |
| `Schema.yaml` | A human-readable draft / example of a request. Useful as a reference while the per-subject fields are being designed. Not used for validation. |

## What is the IntentSpecification?

It is a JSON object with two top-level parts:

- **`userConstraints`** (required) — *how* the job should run (who, what kind, when, and compute resources).
- **`query`** — *what* to simulate (the scientific subject and its parameters).

```
IntentSpecification
├── userConstraints        (required)
│   ├── metadata           (required)
│   │   ├── user
│   │   ├── submissionId   (required)
│   │   ├── inputType      (required)  -> chemistry | physics | biophysics | materials
│   │   └── timeStamp      (required)  -> date-time
│   └── slurmInfo          (optional)
│       ├── cpuCount       -> integer >= 1   (default 8)
│       ├── gpuCount       -> integer >= 0   (default 0)
│       ├── ramAmount      -> integer, MB    (default 4000)
│       └── maxTime        -> integer, hours (default 8)
└── query
    └── chemical
        ├── category       -> structure | molecule
        ├── atoms[]        -> { species, coordinates[3] }
        └── lattice        -> 3x3 matrix of numbers
```

## Fields

### `userConstraints.metadata` (required)

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `user` | string | no | Who submitted the request. |
| `submissionId` | string | yes | Unique id identifying the request. |
| `inputType` | string | yes | One of `chemistry`, `physics`, `biophysics`, `materials`. |
| `timeStamp` | string (date-time) | yes | When the request was submitted. |

### `userConstraints.slurmInfo` (optional)

Compute resources for the SLURM scheduler. All fields are optional; defaults apply when omitted.

| Field | Type | Constraints | Default |
| --- | --- | --- | --- |
| `cpuCount` | integer | `>= 1` | 8 |
| `gpuCount` | integer | `>= 0` | 0 |
| `ramAmount` | integer | megabytes only | 4000 |
| `maxTime` | integer | `>= 1`, hours | 8 |

### `query.chemical`

Describes a chemistry subject (used when `inputType` is `chemistry`).

| Field | Type | Notes |
| --- | --- | --- |
| `category` | string | `structure` or `molecule`. |
| `atoms` | array (min 1) | Each item is an object with `species` (element symbol) and `coordinates` (exactly 3 numbers). |
| `lattice` | array | A 3x3 matrix of numbers describing structure repetition. |

## Conditional rules

The schema uses `allOf` with `if`/`then` blocks for subject-specific validation:

- When `metadata.inputType` is `chemistry`, the request must include `query.chemical` with `category` and `atoms`.

Additional branches for `physics`, `biophysics`, and `materials` are not yet defined.

## Validating a request

Any JSON Schema 2020-12 validator works. Example with [`ajv`](https://ajv.js.org/):

```bash
npx ajv-cli validate -s Schema.json -d your-request.json --spec=draft2020
```

Or in Python with [`jsonschema`](https://python-jsonschema.readthedocs.io/):

```python
import json
from jsonschema import Draft202012Validator

schema = json.load(open("Schema.json"))
request = json.load(open("your-request.json"))
Draft202012Validator(schema).validate(request)
```

## Status / TODO

- [ ] Define `query` subjects for `physics`, `biophysics`, and `materials`.
- [ ] Keep `Schema.yaml` in sync with `Schema.json` (or generate one from the other).
- [ ] Add sample request files and an automated validation test.
