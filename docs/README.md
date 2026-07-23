# TWAIN Documentation

How this folder is organized, and where to start depending on what you need.
(For *running* TWAIN locally, see the repo-root [`README.md`](../README.md).)

## `project/` — orientation & planning

Start here if you're new to the project.

| Document | What it is |
|---|---|
| [`GETTING_STARTED.md`](project/GETTING_STARTED.md) | Orientation guide: reading paths by role, repo layout, FAQ |
| [`PROJECT_SUMMARY.md`](project/PROJECT_SUMMARY.md) | Executive overview: what TWAIN is, status, deliverables |
| [`REVIEW.md`](project/REVIEW.md) | Architecture review: design decisions, gaps, recommendations |
| [`IMPLEMENTATION_ROADMAP.md`](project/IMPLEMENTATION_ROADMAP.md) | Phased plan, timelines, resource plan |

## `backlog/` — what we're building

| Document | What it is |
|---|---|
| [`BACKLOG_OVERVIEW.md`](backlog/BACKLOG_OVERVIEW.md) | Kanban-style overview of all epics and stories |
| [`DETAILED_BACKLOG.md`](backlog/DETAILED_BACKLOG.md) | Full user stories with acceptance criteria and effort estimates |
| [`initial-backlog.md`](backlog/initial-backlog.md) | The original pre-discovery backlog (historical) |

## `architecture/` — how it's designed

| Document | What it is |
|---|---|
| [`domain-neutral-agentic-pipeline.md`](architecture/domain-neutral-agentic-pipeline.md) | The original architecture design document |
| [`pipeline_flow_diagram.md`](architecture/pipeline_flow_diagram.md) | Dataflow, state machine, and module-interaction diagrams |
| [`DIAGRAMS_INDEX.md`](architecture/DIAGRAMS_INDEX.md) | Index of the `.drawio` diagrams and what each shows |
| [`CONFORMANCE_MIGRATION.md`](architecture/CONFORMANCE_MIGRATION.md) | Mapping of existing code onto the blueprint |
| [`web_ui_plan.md`](architecture/web_ui_plan.md) | Design of the web UI / API / runner stack |
| `*.drawio` | Editable diagram sources (open with draw.io) |

## `decisions/` — why it's designed that way

Architecture decision records; one file per decision
(e.g. [`schema_versioning.md`](decisions/schema_versioning.md)).

## Service-level docs (outside `docs/`)

- [`../runner/README.md`](../runner/README.md) — runner service: local runs, Docker offload, AWS deploy
- [`../api/QUICKSTART.md`](../api/QUICKSTART.md) — FastAPI backend
- [`../app/QUICKSTART.md`](../app/QUICKSTART.md) — Expo web app
