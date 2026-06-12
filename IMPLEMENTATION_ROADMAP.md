# TWAIN Implementation Roadmap

**Project**: Domain-Neutral, Self-Discovering, Self-Correcting Agentic Pipeline for Chemistry Simulation  
**Vision**: Researchers request chemistry simulations in natural language; system discovers suitable models, generates code, executes, validates, corrects iteratively.  
**Timeline**: 16–18 weeks (1 FTE) or 8–10 weeks (2 FTE)  
**MVP Success Criteria**: End-to-end solubility prediction with automatic discovery, execution, validation, and self-correction.

---

## Project Status: Pre-Implementation Discovery Phase ✓

### What's In Place
- ✅ Architecture design document (`docs/architecture/domain-neutral-agentic-pipeline.md`)
- ✅ Module hierarchy folders (16 modules, `modules/*/`)
- ✅ Initial backlog (`docs/backlog/initial-backlog.md`)
- ✅ Contract-first design philosophy
- ✅ Guiding principles (domain-neutral, human-in-loop, reproducible)

### What's Missing (Critical Path)
- ❌ JSON Schema implementations (Epic 1)
- ❌ Control plane state machine (Epic 2)
- ❌ Researcher approval workflow (Epic 3)
- ❌ Tool discovery + planning (Epics 4)
- ❌ Code generation + execution (Epic 5)
- ❌ Validation + self-correction (Epic 6)
- ❌ Provenance + governance (Epic 7)
- ❌ End-to-end chemistry pilot (Epic 8)

---

## Phased Roadmap

### Phase 1: Foundation (Weeks 1–2)

**Goal**: Define all data contracts (JSON Schemas) so downstream modules can be built in parallel.

**Epic 1 Stories**:
- 1.1: Define IntentSpec schema (researcher request structure)
- 1.2: Define GoalGraph schema (decomposed sub-goals)
- 1.3: Define ExecutionPlan schema (executable pipeline with costs)
- 1.4: Define ResultPackage + ValidationReport schemas (outputs)
- 1.5: Define CorrectionPlan + ProvenanceEvent schemas (feedback + audit)
- 1.6: Build contract validation test suite

**Deliverables**:
- 12 JSON Schema files in `schemas/` folder
- Python dataclass stubs in each module
- Contract validation test suite (100% pass)
- Example instances (3+ per schema)

**Effort**: 2 weeks (critical path blocker)

**Who**: 1 senior engineer + 1 mid-level engineer (pair on schema design)

---

### Phase 2: Control Plane (Weeks 2–3)

**Goal**: Build state machine and event bus so agent modules can communicate reliably.

**Parallel with Phase 1**: Can start state machine design while schemas finalize.

**Epic 2 Stories**:
- 2.1: Implement agent state machine (INTAKE → CLARIFY → … → ACCEPT)
- 2.2: Implement event bus (pub/sub, schema validation, history)
- 2.3: Implement retry policy + exponential backoff
- 2.4: Implement timeout + budget enforcement
- 2.5: Implement session state persistence (resumable runs)

**Deliverables**:
- State machine with guards (unit tests pass)
- Event bus with 100% event routing (integration tests pass)
- Retry/timeout/budget enforcement (policy tests pass)
- Session store (SQLite + JSON backup)

**Effort**: 2 weeks

**Who**: 1 senior engineer (distributed systems experience)

**Blocker**: Phase 1 schemas must be stable

---

### Phase 3: Researcher Approval & Planning (Weeks 3–5)

**Goal**: Build intake, clarification, discovery, and planning modules.

**Epics 3 + 4** (parallel tracks):

**Track A: Researcher UX (Epic 3)** — 1 engineer
- 3.1: Approval gate infrastructure (CLI for now, web UI later)
- 3.2: Clarification dialogue with confidence thresholds
- 3.3: Final acceptance workflow

**Track B: Discovery & Planning (Epic 4)** — 1 engineer
- 4.1: Method discovery registry (curated JSON database)
- 4.2: Candidate scoring rubric with explainability
- 4.3: External registry adapters (PyPI, GitHub)
- 4.4: Plan synthesis with cost/risk estimation

**Deliverables**:
- CLI approval workflow end-to-end
- Curated registry with 10–20 seed tools
- Scoring rubric (relevance, maturity, license, trust)
- ExecutionPlan generated with cost estimates

**Effort**: 2.5 weeks

**Who**: 2 mid-level engineers

**Dependencies**: Phase 1 (schemas), Phase 2 (control plane)

---

### Phase 4: Code Generation & Local Execution (Weeks 5–6)

**Goal**: Generate runnable Python scripts and execute locally.

**Epic 5 Stories**:
- 5.1: Code generation templates for chemistry tools
- 5.2: Local execution adapter (subprocess, dependency installation, resource monitoring)
- 5.3: Session + event loop management (orchestrator)

**Deliverables**:
- Python code template library (`modules/06_code_configuration_builder/templates/`)
- RunBundle generation (main.py, requirements.txt, config.yaml)
- Local execution with log capture and resource monitoring
- Orchestrator that coordinates all stages

**Effort**: 2 weeks

**Who**: 1 senior engineer + 1 mid-level engineer

**Dependencies**: Phases 1, 2, 3

---

### Phase 5: Validation & Self-Correction (Weeks 6–7)

**Goal**: Parse results, validate against baselines, propose improvements.

**Epic 6 Stories**:
- 6.1: Result parsing + metric extraction (CSV, JSON, logs)
- 6.2: Cross-validation against literature baselines
- 6.3: Self-correction reflection (diagnose failures, propose fixes)

**Deliverables**:
- Result interpreter with pluggable output parsers
- Baseline database (literature references)
- Failure classification and correction strategies
- Self-correction loop with bounded iterations

**Effort**: 2 weeks

**Who**: 1 senior engineer + 1 mid-level engineer

**Dependencies**: Phases 1–4

---

### Phase 6: Provenance & Governance (Weeks 7–8)

**Goal**: Record immutable audit trail and enforce policy constraints.

**Epic 7 Stories**:
- 7.1: Provenance event log (append-only, replay-enabled)
- 7.2: Policy enforcement (license, budget, tool allow-list)
- 7.3: Trust scoring for tools and data sources

**Deliverables**:
- Event log schema + storage (SQLite + JSON export)
- Policy enforcement hooks (pre-discovery, pre-planning, pre-execution)
- License checker + budget tracker
- Trust scoring with explainability

**Effort**: 2 weeks

**Who**: 1 senior engineer (governance + security expertise)

**Dependencies**: Phases 1–5

---

### Phase 7: Chemistry Pilot & Integration (Weeks 8–10)

**Goal**: Validate entire pipeline with real chemistry benchmark.

**Epic 8 Stories**:
- 8.1: Define solubility prediction task + baselines
- 8.2: Implement RDKit-based solubility prediction tool
- 8.3: End-to-end integration test (mock researcher)
- 8.4: Demo + feedback synthesis

**Deliverables**:
- Solubility benchmark task (20 molecules, literature baselines)
- Pre-trained ML model for property prediction
- E2E test: NL request → execution → validation → approval (all mocked)
- Demo readiness + feedback document

**Effort**: 2 weeks (plus feedback iteration)

**Who**: 1 mid-level engineer + 1 domain expert (chemistry)

**Dependencies**: All prior phases

---

## Detailed Backlog

See: **`docs/backlog/DETAILED_BACKLOG.md`** for:
- All 40+ user stories with acceptance criteria
- Effort estimates (S/M/L/XL)
- Dependencies and blocking relationships
- Success metrics

### Quick Reference: Story Effort Distribution

| Epic | # Stories | Effort | Parallel? |
|------|-----------|--------|-----------|
| 1. Schemas | 6 | 2w | No (critical path) |
| 2. Control Plane | 5 | 2w | Partial (after schema finalization) |
| 3. Researcher UX | 3 | 2w | Yes (with Epic 4, after Epic 1) |
| 4. Discovery | 4 | 2.5w | Yes (with Epic 3, after Epic 1) |
| 5. Code Gen | 3 | 2w | After Epics 3–4 |
| 6. Validation | 3 | 2w | After Epic 5 |
| 7. Governance | 3 | 2w | After Epics 5–6 |
| 8. Pilot | 4 | 2w | After all other epics |
| **TOTAL** | **40** | **16–18w** | — |

---

## Resource Plan

### Recommended Staffing (MVP Phase)

**Option 1: 1 FTE (18 weeks)**
- 1 senior full-stack engineer
- Phases complete sequentially

**Option 2: 2 FTE (9–10 weeks)** [Recommended]
- 1 senior engineer (architecture, control plane, core agents)
- 1 mid-level engineer (schemas, code gen, testing, chemistry integration)
- Phases 1–2 sequential, Phases 3–5 parallel

**Option 3: 3 FTE (6–7 weeks)** [Aggressive]
- 1 senior engineer (architecture, control plane)
- 2 mid-level engineers (discovery/planning, code gen/execution, validation/governance in parallel)
- Needs strong async communication to avoid conflicts

### Skills Required
- **Back-end**: Python, async/await, FastAPI or equivalent
- **Systems**: State machines, event-driven architecture, distributed systems
- **Chemistry**: Domain knowledge (optional for MVP, critical for Phase 8)
- **DevOps**: Docker, environment management, reproducibility
- **AI/ML**: LLM prompting, agent design patterns

---

## Key Architectural Decisions (To Finalize)

| Decision | Current Options | Recommendation | Rationale |
|----------|-----------------|-----------------|-----------|
| **Orchestrator** | Custom state machine vs Prefect/Airflow/Temporal | Custom (in-house) | Control over researcher gates + visibility into correction loops |
| **LLM Provider** | Claude (Anthropic) vs GPT-4 vs open-source | Claude with fallback to OSS | Fast iteration + strong reasoning; fallback for cost |
| **Provenance Store** | SQLite vs PostgreSQL vs event sourcing | SQLite (local MVP) | Simple, zero infrastructure, reproducible; migrate to PostgreSQL for multi-tenant |
| **First Chemistry** | SMILES→property vs MD vs quantum | Solubility prediction | Abundant data, fast iteration, easy validation |
| **External Sources** | GitHub vs PyPI vs specialized registries | PyPI + curated catalog | Balanced breadth/trust; avoids model-zoo fragmentation |
| **Cost Model** | Token budget vs compute time vs iteration cap | Token + iteration cap | Prevents infinite loops while allowing recovery |

**Action**: Schedule 1-hour design review with team to confirm these or propose alternatives.

---

## Success Metrics (End of MVP)

### Functional
- [ ] Researcher submits NL request (e.g., "Predict aspirin solubility")
- [ ] System asks clarification Q&A (if needed)
- [ ] System discovers, plans, synthesizes code, executes
- [ ] Results validated against literature baseline
- [ ] Self-correction proposes + executes improvements (if needed)
- [ ] Researcher approves final result
- [ ] Full provenance record enables deterministic replay

### Non-Functional
- [ ] **E2E Latency**: <5 min per iteration (LLM + execution + validation)
- [ ] **Reproducibility**: 100% of historical runs replay identically
- [ ] **Explainability**: Each decision shows ranked alternatives + rationale
- [ ] **Safety**: 0 unsafe tools discovered; 100% policy compliance enforced
- [ ] **Cost Tracking**: Real-time budget tracking; no overages without override

### Chemistry Validation
- [ ] Solubility predictions within 15% MSE of literature baselines
- [ ] Auto-correction improves baseline results by >5%
- [ ] Artifacts shareable with collaborators (code + config + provenance)

---

## Risk Mitigation

| Risk | Prob | Impact | Mitigation |
|------|------|--------|-----------|
| LLM reasoning fails (bad plans) | Medium | High | Human approval gate; option to override or re-plan |
| Tool discovery toxicity | Medium | High | Curated registry + policy allow-list; researcher final approval |
| Infinite correction loop | Low | Medium | Iteration cap (e.g., 5 cycles); convergence metric |
| Provenance data loss | Low | High | Append-only log + SQLite; daily backup; recovery tests |
| Researcher overwhelm | Medium | Low | Progressive disclosure: max 3 candidates; confidence-driven Q&A |
| Cost overrun | Medium | Medium | Per-run + monthly budget caps; researcher approval for +10% |

---

## Post-MVP Roadmap (Phases 8+)

### Phase 8: Production Hardening (4–6 weeks)
- Containerization (Docker)
- Multi-tenant authentication (OAuth)
- Cloud deployment (GCP Cloud Run)
- Cost monitoring dashboard
- SLO definition + alerting

### Phase 9: Chemistry Domain Expansion (6–8 weeks)
- Molecular dynamics simulations (ASE, GROMACS)
- Reaction pathway prediction
- Quantum chemistry ground state energy
- Materials property prediction

### Phase 10: Advanced Agentic Features (8–12 weeks)
- Multi-tool composition (sequential pipelines)
- Batch prediction at scale
- Auto-correction without researcher approval (configurable)
- Literature integration (automatic citation)

### Phase 11: Open Ecosystem (12–16 weeks)
- Publish core modules as pip/conda packages
- Domain adapter SDK for external contributors
- Example notebooks and tutorials
- Community governance model

---

## Getting Started: Immediate Next Steps

### This Week (Developer Kickoff)

1. **Team Meeting** (1 hour)
   - Review architecture (`REVIEW.md`, `docs/architecture/`)
   - Confirm Epic 1 schema design (finalize contract structure)
   - Assign Phase 1 lead (senior engineer)

2. **Setup** (2–4 hours)
   - Clone repo: `git clone https://github.com/washu-dev/TWAIN.git`
   - Create project space: Asana or Linear board
   - Setup CI/CD pipeline (GitHub Actions):
     - Run schema validation on every commit
     - Run unit tests (once available)
     - Generate docs automatically

3. **Epic 1 Kickoff** (2 days)
   - Finalize IntentSpec schema (12 required fields + validation rules)
   - Finalize ExecutionPlan schema (resource request + cost structure)
   - Create placeholder Python dataclass stubs
   - Define 3 realistic examples per schema

### Next Week (Phase 1 in Progress)

4. **Schema Implementation**
   - Write JSON Schema files (6 files, `schemas/*.schema.json`)
   - Write Python dataclass + validation (Pydantic)
   - Build contract test suite (pytest)

5. **Design Review** (1 hour)
   - Confirm state machine for Phase 2
   - Confirm event bus interface
   - Confirm module communication contract

6. **Documentation**
   - Publish API stubs for all 16 modules (type hints)
   - Create developer quickstart guide
   - Setup Sphinx or MkDocs for auto-generated docs

### Following Weeks (Phases 2+)

7. **Parallel Workstreams**
   - Phase 1 completion (schemas + tests)
   - Phase 2 design + skeleton (control plane)
   - Phase 3 mockups (approval UI)

---

## File Structure: What's There, What's Missing

### ✅ Already In Place
```
TWAIN/
├── docs/
│   ├── architecture/
│   │   └── domain-neutral-agentic-pipeline.md (architecture doc)
│   ├── backlog/
│   │   ├── initial-backlog.md (seed backlog)
│   │   └── DETAILED_BACKLOG.md (expanded stories) ← NEW
│   └── decisions/ (decision records, to be populated)
├── modules/
│   ├── 01_intake_nlu/
│   ├── 02_clarification_dialogue/
│   ├── ... (16 module folders, currently empty)
│   └── README.md (module overview)
├── interfaces/ (contract stubs, to be populated)
├── schemas/ (JSON Schemas, to be populated)
├── configs/ (YAML policies, discovery registry, to be populated)
├── tests/ (unit, integration, contract tests, to be populated)
├── experiments/ (baselines, notebooks, to be populated)
├── scripts/ (automation helpers, to be populated)
├── README.md (project overview)
├── REVIEW.md (this project review) ← NEW
├── IMPLEMENTATION_ROADMAP.md (this roadmap) ← NEW
└── TWAIN_Architecture.drawio (drawio diagram, to be reviewed/updated)
```

### ❌ Critical Missing (Needed for MVP)

**Phase 1 (Schemas)**:
- `schemas/*.schema.json` (IntentSpec, GoalGraph, ExecutionPlan, etc.)
- `schemas/examples/*.json` (realistic instances)
- Pydantic models in each module folder

**Phase 2 (Control Plane)**:
- `modules/16_agent_mesh_control_plane/state_machine.py`
- `modules/16_agent_mesh_control_plane/event_bus.py`
- `modules/16_agent_mesh_control_plane/retry_policy.py`

**And so on** (see DETAILED_BACKLOG.md for complete list)

---

## How to Use This Roadmap

1. **Stakeholders**: Review "Project Status" and "Phased Roadmap" sections for timeline + resource plan
2. **Tech Leads**: Review "Key Architectural Decisions" section; schedule design review if needed
3. **Engineers**: Read DETAILED_BACKLOG.md for specific user stories, acceptance criteria, effort estimates
4. **Project Manager**: Use "Resource Plan" + "Success Metrics" to define sprints and track progress
5. **Researchers**: Review "Vision" and "Chemistry Pilot" section for how you'll interact with the system

---

## Conclusion

TWAIN is a **well-architected, domain-neutral agentic platform** ready for implementation. The foundation is strong:

- ✅ Clear logical flow from NL → execution → validation → correction
- ✅ Domain-neutral core + adapter pattern for chemistry-specific models
- ✅ Human-in-the-loop ensures researcher remains in control
- ✅ Contract-first design prevents coupling and enables parallel development
- ✅ Comprehensive coverage of non-functional requirements (reproducibility, safety, cost control)

**Next step**: Staff Phase 1 (schemas) and finalize architectural decisions in a team design review. Estimated time to MVP: **8–10 weeks with 2 FTE**.

---

## Document Cross-References

- **Architecture**: `docs/architecture/domain-neutral-agentic-pipeline.md`
- **Detailed Backlog**: `docs/backlog/DETAILED_BACKLOG.md`
- **Pipeline Diagrams**: `docs/architecture/pipeline_flow_diagram.md`
- **This Project Review**: `REVIEW.md`
- **Module Hierarchy**: `modules/README.md`
- **Existing Drawio Diagram**: `TWAIN_Architecture.drawio` (review + update recommended)

---

## Questions to Resolve Before Starting

1. **Researcher Interaction**: CLI-first or web UI from day 1?
2. **LLM Provider**: Claude (Anthropic) API or GPT-4 or open-source Llama?
3. **Chemistry Focus**: Molecular property prediction first, or different domain?
4. **Deployment Target**: Local + GCP, or AWS, or both?
5. **Team Composition**: 1 FTE, 2 FTE, or 3 FTE?
6. **Timeline**: Hard deadline (e.g., "shipped by Q3"), or best-effort?

**Schedule a 30-min design review with stakeholders to confirm before Day 1 of implementation.**

