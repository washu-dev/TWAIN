# TWAIN Project Summary

**Date**: 2026-06-03  
**Status**: Pre-implementation discovery phase complete ✓  
**Next Phase**: Begin Epic 1 (Schemas) — ready for implementation

---

## What is TWAIN?

**TWAIN** is a domain-neutral, self-discovering, self-correcting agentic pipeline that enables researchers to:

1. **Request in natural language**: "Predict the water solubility of aspirin at 25°C"
2. **Clarify ambiguities**: System asks targeted Q&A until confident
3. **Discover suitable models**: Automatically search registries (PyPI, GitHub, curated databases)
4. **Synthesize execution plans**: Build optimized workflows with cost/resource estimates
5. **Generate executable code**: Create scripts tailored to the selected tools
6. **Execute and monitor**: Run on local machine (extensible to HPC/cloud)
7. **Validate results**: Compare against literature baselines
8. **Auto-correct on failure**: Diagnose root causes and propose targeted improvements
9. **Keep researcher in control**: Approval gates at critical decision points

**Output**: Reproducible science artifacts (code, config, results) + immutable audit trail.

---

## Architecture Overview

### 16 Independent Modules

| # | Module | Purpose |
|---|--------|---------|
| 01 | Intake NLU | Parse researcher request → structured intent |
| 02 | Clarification Dialogue | Ask Q&A until confident |
| 03 | Goal Decomposer | Break request into executable sub-goals |
| 04 | Method Discovery | Find + rank candidate tools/models |
| 05 | Plan Synthesis | Select best tool + estimate costs |
| 06 | Code & Config Builder | Generate runnable scripts |
| 07 | Runtime Orchestrator | Coordinate execution lifecycle |
| 08 | Execution Adapter | Backend abstraction (local/cluster/cloud) |
| 09 | Observability Monitor | Capture logs, metrics, anomalies |
| 10 | Result Interpreter | Parse tool outputs → structured results |
| 11 | Cross Validation | Compare results vs. baselines |
| 12 | Self Correction | Diagnose failures + propose fixes |
| 13 | Human In The Loop | Researcher approval gates |
| 14 | Provenance Memory | Immutable audit trail + replay |
| 15 | Policy & Governance | License checks, budget enforcement |
| 16 | Agent Mesh Control Plane | State machine, retries, timeouts |

### Key Design Principles

1. **Domain-Neutral Core**: Logic in core modules; chemistry-specific code in adapters
2. **Contract-First**: All inter-module communication via JSON Schemas (no tight coupling)
3. **Human-In-The-Loop**: Researcher approves plans, reviews results, makes final decisions
4. **Reproducible**: Every run is fully captured; can replay from historical provenance
5. **Self-Correcting**: Bounded iteration loop: diagnose → replan → rerun → validate
6. **Safe**: Policy enforcement (licenses, budgets, tool allow-lists) throughout pipeline

---

## Deliverables from This Discovery Phase

### 1. **Project Review** (`REVIEW.md`)
- Validates current architecture (strengths + gaps)
- Identifies 7 critical design gaps + recommendations
- Provides module dependency graph
- Includes risk mitigation strategies

### 2. **Detailed Implementation Backlog** (`docs/backlog/DETAILED_BACKLOG.md`)
- **40+ user stories** broken down by epic
- Acceptance criteria for each story
- Effort estimates (S/M/L/XL scale)
- Dependencies and blocking relationships
- Test plan for each story

### 3. **Architecture & Dataflow Diagrams** (`docs/architecture/pipeline_flow_diagram.md`)
- Complete system dataflow (16 modules, interaction paths)
- Module dependency matrix
- State machine (major transitions)
- Data contract sequence diagrams
- Error recovery paths
- Runtime execution timeline example
- Resource utilization profile

### 4. **Implementation Roadmap** (`IMPLEMENTATION_ROADMAP.md`)
- **7 phased implementation plan** (16–18 weeks with 1 FTE)
- Resource recommendations (1 FTE, 2 FTE, or 3 FTE)
- Parallel workstreams (can parallelize Phases 3–5 if 2 FTE)
- Key architectural decisions (to confirm before starting)
- Success metrics (functional, non-functional, chemistry validation)
- Post-MVP expansion roadmap (Phases 8–11)
- Immediate next steps (kickoff checklist)

### 5. **Folder Hierarchy** (`modules/*/`)
- 16 module folders ready for implementation
- Clear separation of concerns
- `interfaces/`, `schemas/`, `configs/`, `tests/`, `experiments/` supporting directories

---

## Timeline & Effort Estimate

| Phase | Weeks | Effort | Parallel? | Key Deliverables |
|-------|-------|--------|-----------|------------------|
| 1. Schemas | 2 | 2w | No (critical path) | 12 JSON Schemas, Python stubs, contract tests |
| 2. Control Plane | 2 | 2w | After Phase 1 | State machine, event bus, retry policy, session store |
| 3. Researcher UX | 2.5 | 2w | With Phase 4 (after Phases 1–2) | Approval gates, clarification dialogue, final review |
| 4. Discovery & Planning | 2.5 | 2.5w | With Phase 3 (after Phases 1–2) | Registry, scoring, plan synthesis with cost estimates |
| 5. Code Gen & Execution | 2 | 2w | After Phases 3–4 | Code templates, local executor, orchestrator |
| 6. Validation & Correction | 2 | 2w | After Phase 5 | Result parsing, baselines, self-correction loop |
| 7. Provenance & Governance | 2 | 2w | After Phases 5–6 | Event log, policy enforcement, trust scoring |
| 8. Chemistry Pilot | 2 | 2w | After all prior phases | Solubility benchmark, end-to-end integration test |
| **Total** | **16–18** | **16–18w (1 FTE)** | See notes | **MVP shipped** |

**With 2 FTE in parallel**: 8–10 weeks (Phase 1 sequential, Phases 3–7 overlapping)

---

## Success Criteria (End of MVP)

### Functional
- [ ] Researcher submits NL request ("Predict aspirin solubility")
- [ ] System clarifies ambiguities (if needed)
- [ ] System discovers suitable tools, synthesizes plan, generates code, executes
- [ ] Results validated against literature baseline
- [ ] Self-correction detects failures, proposes improvements, reruns (if needed)
- [ ] Researcher approves final result
- [ ] Full provenance record enables deterministic replay

### Non-Functional
- [ ] **E2E Latency**: <5 min per iteration
- [ ] **Reproducibility**: 100% of historical runs replay identically
- [ ] **Explainability**: Every decision shows ranked alternatives + rationale
- [ ] **Safety**: 0 unsafe tools discovered; 100% policy compliance
- [ ] **Cost Control**: Real-time budget tracking; no overages without override

### Chemistry Validation
- [ ] Solubility predictions within 15% MSE of literature baselines
- [ ] Auto-correction improves baseline results (e.g., from MSE 0.8 → 0.5)
- [ ] Artifacts shareable with collaborators (code + config + provenance)

---

## Key Architectural Decisions (To Finalize)

**Before Day 1 of implementation**, confirm:

| Decision | Recommended | Alternative(s) | Rationale |
|----------|-------------|-----------------|-----------|
| **Orchestrator Framework** | Custom state machine | Prefect, Airflow, Temporal | Control over researcher gates + visibility into correction loops |
| **LLM Provider** | Claude (Anthropic) | GPT-4, open-source Llama | Strong reasoning for planning; fallback to OSS for cost |
| **Provenance Storage** | SQLite (MVP) | PostgreSQL (multi-tenant) | Simple, zero infra; migrate post-MVP |
| **First Chemistry Domain** | Molecular property prediction (solubility) | MD, quantum, reactions | Abundant data, fast iteration, easy validation |
| **External Tool Sources** | PyPI + curated registry | GitHub, ArXiv, specialized DBs | Balanced breadth/trust; avoids fragmentation |
| **Cost Control Model** | Token budget + iteration cap | Compute time budget, monthly ceiling | Prevents infinite loops; aligned with LLM costs |

**Action**: Schedule 1-hour design review with core team to confirm or propose alternatives.

---

## What Was Already There (Repo State)

- ✅ `TWAIN_Architecture.drawio` — rich but product-focused diagram (review + refactor)
- ✅ `Robert Wexler_1908.pdf` — historical context document
- ✅ Git repository initialized and linked to GitHub

## What We Added This Week

- ✅ **Architecture validation** (`REVIEW.md`) — identified gaps + recommendations
- ✅ **Detailed backlog** (`docs/backlog/DETAILED_BACKLOG.md`) — 40+ actionable stories
- ✅ **Pipeline diagrams** (`docs/architecture/pipeline_flow_diagram.md`) — visual architecture
- ✅ **Implementation roadmap** (`IMPLEMENTATION_ROADMAP.md`) — phased plan + timelines
- ✅ **This summary** (`PROJECT_SUMMARY.md`) — executive overview

## What's Next (Week 1 of Implementation)

1. **Team Kickoff** (1 hour)
   - Review architecture documents
   - Confirm Phase 1 (schemas) design
   - Assign Phase 1 lead

2. **Epic 1 Design Finalization** (2–3 days)
   - Finalize IntentSpec + ExecutionPlan schemas
   - Create 3+ examples per schema
   - Agree on Python dataclass structure

3. **Project Setup** (1–2 days)
   - Setup CI/CD pipeline (GitHub Actions)
   - Create Asana/Linear project board
   - Define sprint cycles (1-week sprints recommended)

4. **Schema Implementation** (Weeks 1–2)
   - Write 12 JSON Schema files
   - Write Python dataclass stubs + validation
   - Build contract test suite
   - Achieve 100% test coverage

---

## How to Engage

### For Project Managers
- Use `IMPLEMENTATION_ROADMAP.md` to define sprints
- Use `DETAILED_BACKLOG.md` for story estimation
- Track Epic completion (8 epics total)

### For Engineers
- Start with `DETAILED_BACKLOG.md` for user stories
- Reference `REVIEW.md` for architectural context
- Use `pipeline_flow_diagram.md` for module interaction patterns

### For Researchers
- See "What is TWAIN?" section above for vision
- Review "Chemistry Pilot" section in `IMPLEMENTATION_ROADMAP.md` for how you'll use it
- You'll be involved in final approval + feedback (Phase 8)

### For Stakeholders
- Read `PROJECT_SUMMARY.md` (this document) + `REVIEW.md` for status
- Review `IMPLEMENTATION_ROADMAP.md` for timeline + resource plan
- Schedule design review to confirm architectural decisions

---

## Risk Summary

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|-----------|
| LLM reasoning failure (bad plans) | Medium | High | Human approval gate; override option |
| Tool discovery toxicity (unsafe tools) | Medium | High | Curated registry + allow-list; researcher approval |
| Infinite correction loop | Low | Medium | Iteration cap (5 cycles); convergence metric |
| Provenance data loss | Low | High | Append-only log + backup; recovery tests |
| Researcher overwhelm | Medium | Low | Progressive disclosure; confidence-driven Q&A |
| Cost overrun | Medium | Medium | Budget caps; researcher approval for overages |

---

## Questions Before Starting?

1. **Researcher interaction**: CLI-first or web UI from day 1?
2. **LLM provider**: Claude, GPT-4, or open-source?
3. **Chemistry domain**: Molecular properties first, or different?
4. **Staffing**: 1 FTE, 2 FTE, or 3 FTE?
5. **Timeline**: Hard deadline, or best-effort?
6. **Deployment**: Local + GCP, or AWS, or both?

**Answer in design review (before Day 1 of implementation).**

---

## Document Map

| Document | Purpose | Audience |
|----------|---------|----------|
| `REVIEW.md` | Detailed architecture review, gaps, recommendations | Tech leads, architects |
| `docs/backlog/DETAILED_BACKLOG.md` | 40+ user stories, effort estimates, acceptance criteria | Engineers, project managers |
| `docs/architecture/pipeline_flow_diagram.md` | Visual diagrams (dataflow, state machine, interaction matrix) | All engineers |
| `IMPLEMENTATION_ROADMAP.md` | Phased plan (7 phases), resource plan, immediate next steps | Project managers, stakeholders |
| `PROJECT_SUMMARY.md` | This document — executive overview, status, what's next | All stakeholders |
| `modules/README.md` | Module hierarchy overview | All engineers |
| `TWAIN_Architecture.drawio` | Original architecture diagram (review + refactor recommended) | Architects |

---

## Conclusion

TWAIN is a **well-designed, domain-neutral agentic platform** ready for implementation. The research and planning phase is complete. All architectural decisions are documented, risks are mitigated, and the backlog is detailed and ready for execution.

**Estimated time to MVP**: 8–10 weeks with 2 FTE  
**Next action**: Confirm architectural decisions in design review; staff Phase 1 (schemas)

---

**Created**: 2026-06-03  
**Status**: Ready for implementation  
**Version**: 1.0
