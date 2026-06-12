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

# store the result as the canonical reference
pixi run -e benchmark python Benchmark/run_benchmark.py Benchmark/structures/h2o_molecule.json --write-reference

# regression check against the stored reference (non-zero exit on drift)
pixi run -e benchmark python Benchmark/run_benchmark.py Benchmark/structures/h2o_molecule.json --check
```

Comparisons use tolerances (energy `1e-6` eV, positions `1e-4` Å) because
floating-point results vary slightly across platforms.

## Status / roadmap

- [x] M1 — ASE engine path (EMT static/relax), reference store + check
- [ ] M2 — Pymatgen engine path for the same specs
- [ ] M3 — Engine-vs-engine comparison report; ExecutionLog-compatible output
- [ ] M4 — Remaining sample structures + results write-up
