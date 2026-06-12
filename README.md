# TWAIN

TWAIN is an AI-driven platform for running scientific simulations (chemistry, physics, biophysics, and materials) on high-performance computing clusters. A user describes what they want in natural language from a cross-device client; an LLM intelligence layer turns that intent into a structured, validated specification; the platform then schedules the simulation on a compute cluster, logs its execution, captures the reasoning/provenance behind it, and post-processes the results for visualization.

## Architecture

The system is organized into layers (see [`docs/`](docs/) for the full diagram):

| Layer | Responsibility | Stack |
| --- | --- | --- |
| 1 — Client | Cross-device UI and shared state | SwiftUI · CloudKit |
| 2 — API Gateway & State Sync | Request routing and state synchronization | Python / FastAPI |
| 3 — Core AI / Intelligence | Turns natural-language intent into a validated spec | Python + LLM |
| 4 — Data & Specification | The Intent Specification schema and data stores | JSON/YAML Schema · SQLite |
| 6 — Orchestration & Provenance | Schedules jobs and captures provenance | Python · Slurm · PBS |
| 7 — Post-Processing & Visualization | Turns raw outputs into results/plots | Python · matplotlib · plotly |
| 8 — Infrastructure | Dev environment and tooling | — |

The full, authoritative diagram lives in [`docs/TWAIN_Architecture.drawio`](docs/TWAIN_Architecture.drawio) (with a PDF export for quick viewing).

## Repository structure

| Path | Description |
| --- | --- |
| [`Schema/`](Schema/) | The `IntentSpecification` JSON Schema that defines a valid simulation request. See [`Schema/README.md`](Schema/README.md). |
| `Job Monitoring/` | Execution Log and Provenance Store modules (SQLite-backed job tracking). |
| [`docs/`](docs/) | Architecture diagram, PDF export, and reference papers. |

## The Intent Specification

Every request entering the pipeline is validated against the `IntentSpecification` schema in [`Schema/Schema.json`](Schema/Schema.json). It has two parts:

- **`userConstraints`** — *how* the job runs (submitter, type, timestamp, and SLURM resources).
- **`query`** — *what* to simulate (e.g. a chemistry subject described for the Pymatgen and/or ASE engines).

See [`Schema/README.md`](Schema/README.md) for the full field reference and validation instructions.

## Development

Work is split across feature branches (`Schema`, `ExecutionLog`, `SpeechRecognition`, `Benchmark`), each branching from and merging back into `master`. Package management uses [pixi](https://pixi.sh/).

## Status

Active development, currently centered on Layer 4 (Data & Specification) and job monitoring.

- **In review:** general Intent Specification schema fields, Execution Log, Provenance Store.
- **In progress:** Benchmark & Test Library, Speech Recognition.
- **Up next:** additional simulation-engine schemas (e.g. GROMACS), Workflow Registry, and workflow-manager research.
- **Backlog:** query subjects for physics / biophysics / materials and pixi-based packaging.

Later layers (orchestration, visualization) are planned per the architecture roadmap.
