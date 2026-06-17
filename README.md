# TWAIN

Domain-neutral, self-discovering, self-correcting agentic pipeline for research simulation workflows.

## What Was Added

- Architecture and design document:
  - `docs/architecture/domain-neutral-agentic-pipeline.md`
- Initial implementation backlog:
  - `docs/backlog/initial-backlog.md`
- Independent module hierarchy:
  - `modules/` and subfolders
  - `modules/README.md`

## Top-Level Structure

- `docs/architecture/` - target architecture and technical design.
- `docs/backlog/` - implementation backlog and planning artifacts.
- `docs/decisions/` - architecture decision records.
- `modules/` - independent pipeline modules.
- `interfaces/` - inter-module contracts and APIs.
- `schemas/` - JSON schema contracts.
- `configs/` - environment and policy configuration.
- `tests/` - contract, unit, integration, and replay tests.
- `experiments/` - exploratory experiments and benchmark runs.
- `scripts/` - local automation scripts.

## Immediate Next Steps

1. Create schema files for core contracts in `schemas/`.
2. Define module interface stubs in `interfaces/`.
3. Implement control-plane state machine in `modules/16_agent_mesh_control_plane`.
4. Implement MVP flow: Intake -> Clarification -> Plan Synthesis -> Local Execution -> Validation.
