# TWAIN Schema

This folder defines the **`IntentSpecification`** — the structured format that describes a single TWAIN simulation request. Every request that enters the pipeline is expected to validate against this schema before it is acted on.

## Files

| File | Purpose |
| --- | --- |
| `Schema.json` | The authoritative [JSON Schema](https://json-schema.org/) (draft 2020-12). Use this for validation. |


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
    ├── chemical                  (Pymatgen engine)
    │   ├── category       -> structure | molecule
    │   ├── atoms[]        -> { species, coordinates[3] }
    │   └── lattice        -> 3x3 matrix of numbers
    └── ase                       (ASE engine)
        ├── structure
        │   ├── category          -> structure | molecule
        │   ├── coordinateSystem  -> cartesian | fractional
        │   ├── pbc               -> bool or [bool, bool, bool]
        │   ├── atoms[]           -> { species, coordinates[3] }
        │   └── cell              -> 3x3 matrix of numbers
        └── calculation
            ├── calculator        -> emt | vasp | gpaw | espresso | lammps
            ├── calculationType   -> static | relax | md | neb | eos | vibrations
            ├── relax             -> { optimizer, fmax, maxSteps }
            ├── md                -> { ensemble, timeStep, steps, temperature }
            └── neb               -> { images, fmax }
```

> **Two engines, on purpose.** `query.chemical` targets Pymatgen and `query.ase` targets ASE. They are kept separate (not merged) so the same problem can be run through both engines and benchmarked against each other.

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

### `query.ase`

Describes the same kind of chemistry problem for the **ASE** engine, mirroring the ASE `Atoms` object plus a calculation block. Provided alongside `query.chemical` so both engines can be tested/benchmarked.

**`ase.structure`** (mirrors an ASE `Atoms` object)

| Field | Type | Notes |
| --- | --- | --- |
| `category` | string | `structure` (periodic, has a `cell`) or `molecule` (non-periodic). |
| `coordinateSystem` | string | `cartesian` (ASE `positions`) or `fractional` (ASE `scaled_positions`). Default `cartesian`. |
| `pbc` | boolean or `[bool, bool, bool]` | Periodic boundary conditions; one flag for all axes or per-axis. |
| `atoms` | array (min 1) | Each item is `{ species, coordinates[3] }`. |
| `cell` | array | 3x3 matrix of lattice vectors. Required for `structure` (see conditional rules). |

**`ase.calculation`**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `calculator` | string | yes | Energy/force engine: `emt`, `vasp`, `gpaw`, `espresso`, `lammps`. |
| `calculationType` | string | yes | `static`, `relax`, `md`, `neb`, `eos`, `vibrations`. |
| `relax` | object | no | `optimizer` (BFGS/LBFGS/FIRE/GPMin/MDMin), `fmax`, `maxSteps`. |
| `md` | object | no | `ensemble` (NVE/NVT/NPT), `timeStep` (fs), `steps`, `temperature` (K). |
| `neb` | object | no | `images` (>=3), `fmax`. |

## Conditional rules

The schema uses `allOf` with `if`/`then` blocks for subject-specific validation:

- When `metadata.inputType` is `chemistry`, the request must include `query.chemical` with `category` and `atoms`.
- When `query.chemical.category` is `structure`, `query.chemical.lattice` is required (crystals are periodic; molecules omit it).
- When `query.ase.structure.category` is `structure`, `query.ase.structure.cell` is required (same periodicity reasoning, ASE side).
- `query.ase.calculation` requires both `calculator` and `calculationType` when present.

Additional branches for `physics`, `biophysics`, and `materials` are not yet defined.

> Note: `query.ase` is currently optional even for `chemistry` requests (the required engine block is still `query.chemical`). Once ASE-only runs are supported, the chemistry rule can be relaxed to accept either engine.

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
- [ ] Add sample request files and an automated validation test.
