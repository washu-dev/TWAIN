# Benchmark & Test Library

File-based benchmark suite for TWAIN (Layer 4). It runs sample
`IntentSpecification` documents through the simulation engines and compares
the results against precomputed reference outputs, so we can:

1. **Regression-test** — detect when schema or engine-glue changes alter results.
2. **Compare engines** — run the same problem through ASE and Pymatgen (M2).
3. **Exercise the pipeline end-to-end** — every input is validated against
   `Schema/Schema.json` before it runs.

Everything runs locally using ASE's built-in EMT calculator — no cluster,
no licenses, no sensitive data.

## Layout

| Path | Purpose |
| --- | --- |
| `structures/` | Sample IntentSpecification documents (the benchmark inputs). |
| `references/` | Precomputed reference outputs, committed to git. |
| `results/` | Scratch output of ad-hoc runs (gitignored). |
| `run_benchmark.py` | The runner: validate spec → run engine → report/compare. |

## Usage

```bash
# one-off run (writes to results/)
pixi run -e benchmark python Benchmark/run_benchmark.py Benchmark/structures/h2o_molecule.json

# choose the engine: ase (default), pymatgen, or both
pixi run -e benchmark python Benchmark/run_benchmark.py Benchmark/structures/h2o_molecule.json --engine both

# store the result(s) as the canonical reference
pixi run -e benchmark python Benchmark/run_benchmark.py Benchmark/structures/h2o_molecule.json --engine both --write-reference

# regression check against stored references (non-zero exit on drift)
pixi run -e benchmark python Benchmark/run_benchmark.py Benchmark/structures/h2o_molecule.json --engine both --check
```

With `--engine both` the runner also compares the engines against each other
(energy and final positions must agree within tolerance).

### Fragments from the semantic parser (Intelligence Layer)

The Layer 3 semantic parser emits per-engine *fragments* (e.g. `data.json`
with a top-level `pymatgen` key) rather than full IntentSpecifications.
The runner accepts these directly, validating against the per-engine schema
and attaching synthetic benchmark metadata:

```bash
pixi run -e benchmark python Benchmark/run_benchmark.py Benchmark/structures/graphite_fragment.json --fragment pymatgen --check
```

This makes the benchmark an end-to-end test of parser output: anything the
LLM produces can be validated, executed, and regression-checked here.

### How the two engines work

- **ase** — builds `ase.Atoms` directly from the spec's `query.ase` block.
- **pymatgen** — builds a pymatgen `Molecule`/`Structure` from `query.chemical`
  (the `experiment.method` is mapped locally: `MPRelaxSet` → relax,
  `MPStaticSet` → static), converts via `AseAtomsAdaptor`.

Both execute with ASE's local EMT calculator, so agreement between them
verifies that the two schema blocks describe the same physical system. The
real VASP path behind the MP input sets comes with cluster integration.

Comparisons use tolerances (energy `1e-6` eV, positions `1e-4` Å) because
floating-point results vary slightly across platforms.

## Status / roadmap

- [x] M1 — ASE engine path (EMT static/relax), reference store + check
- [x] M2 — Pymatgen engine path + cross-engine agreement check
- [x] M3 — Accept semantic-parser fragments (`--fragment pymatgen`)
- [ ] M4 — ExecutionLog-compatible output; ASE fragment schema; third sample structure + results write-up

Known vocabulary drift: `Schema.json` uses category `structure` while
`PymatgenSchema.json` fragments use `lattice` for the same concept. The
runner accepts both for now; tracked as a schema issue.
