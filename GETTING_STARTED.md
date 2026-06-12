# TWAIN: Getting Started Guide

**Welcome to TWAIN** — a domain-neutral, self-discovering, self-correcting agentic pipeline for chemistry simulation.

This guide helps you navigate the project and get up to speed quickly.

---

## For the Impatient: 2-Minute Overview

**What is TWAIN?**  
A system where researchers can say "Predict the solubility of aspirin" and the system automatically:
1. Understands the request (NLU)
2. Discovers suitable tools (discovery)
3. Generates code (code gen)
4. Executes it (orchestration)
5. Validates results (cross-validation)
6. Fixes problems automatically (self-correction)
7. Keeps you in control every step (human approval gates)

**Why does this matter?**  
Science workflows are repetitive, error-prone, and hard to reproduce. TWAIN automates the tedious parts while keeping researchers in control of critical decisions.

**Timeline to MVP?**  
8–10 weeks with 2 engineers.

**Status?**  
Architecture designed ✓, backlog detailed ✓, ready for implementation.

---

## Document Roadmap

### Executive Level (5-min read)
- **Start here**: [`PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md)
  - What is TWAIN?
  - Architecture overview (16 modules)
  - Timeline + effort estimate
  - Success criteria

### Design & Architecture (30-min read)
- **Architecture review**: [`REVIEW.md`](REVIEW.md)
  - Validates current design
  - Identifies 7 critical gaps + recommendations
  - Risk mitigation
  - Module descriptions

- **Detailed diagrams**: [`docs/architecture/pipeline_flow_diagram.md`](docs/architecture/pipeline_flow_diagram.md)
  - Complete dataflow diagram
  - Module interaction matrix
  - State machine
  - Contract sequence diagrams
  - Error recovery paths

- **Original architecture**: [`docs/architecture/domain-neutral-agentic-pipeline.md`](docs/architecture/domain-neutral-agentic-pipeline.md)
  - High-level design principles
  - Data contracts overview
  - Non-functional requirements

### Implementation (1–2 hour read)
- **Detailed backlog**: [`docs/backlog/DETAILED_BACKLOG.md`](docs/backlog/DETAILED_BACKLOG.md)
  - 40+ user stories (Epic 1–8)
  - Acceptance criteria per story
  - Effort estimates (S/M/L/XL)
  - Dependencies and blocking relationships
  - Summary table with effort distribution

- **Implementation roadmap**: [`IMPLEMENTATION_ROADMAP.md`](IMPLEMENTATION_ROADMAP.md)
  - Phased plan (7 phases, 16–18 weeks with 1 FTE)
  - Resource recommendations
  - Success metrics
  - Immediate next steps (kickoff checklist)
  - Post-MVP expansion roadmap

### Module Reference
- **Module hierarchy**: [`modules/README.md`](modules/README.md)
  - List of 16 modules
  - Brief description of each

---

## How to Use This Project

### I'm a Project Manager
1. Read: `PROJECT_SUMMARY.md` (2 min)
2. Read: `IMPLEMENTATION_ROADMAP.md` sections "Resource Plan" + "Phased Roadmap" (10 min)
3. Use: `DETAILED_BACKLOG.md` to create Asana/Linear project board
4. Action: Schedule design review to confirm architectural decisions (30 min)

**Output**: Sprint 1 plan with Phase 1 stories assigned

---

### I'm a Tech Lead / Architect
1. Read: `PROJECT_SUMMARY.md` (2 min)
2. Read: `REVIEW.md` sections "Architecture Validation" + "Key Design Decisions" (15 min)
3. Review: `docs/architecture/pipeline_flow_diagram.md` (20 min)
4. Deep dive: `DETAILED_BACKLOG.md` Epic 1 + Epic 2 stories (30 min)
5. Action: Schedule design review; confirm or propose alternative architectural decisions

**Output**: Design review meeting with tech team

---

### I'm a Back-End Engineer (Start Here)
1. Read: `PROJECT_SUMMARY.md` (2 min)
2. Review: `docs/architecture/pipeline_flow_diagram.md` (15 min) — understand module interactions
3. Read: `REVIEW.md` section "Module Dependency Graph & Staging" (5 min) — understand critical path
4. Assign to you: Story from `DETAILED_BACKLOG.md` (Epic 1 or 2)
5. Deep dive: Acceptance criteria for your assigned story

**Output**: Ready to start Sprint 1 story (Phase 1: Schemas)

---

### I'm a Researcher (Using the System)
1. Read: `PROJECT_SUMMARY.md` section "What is TWAIN?" (2 min)
2. Skim: `IMPLEMENTATION_ROADMAP.md` section "Chemistry Pilot & Integration" (10 min) — this is how you'll interact with it
3. Wait: Phases 1–7 will be built by engineers
4. You'll be involved: Phase 8 (demo + feedback)

**Output**: Understand what TWAIN will do; be ready for pilot testing in ~10 weeks

---

## Project Structure (Where Things Are)

```
TWAIN/
│
├── 📄 PROJECT_SUMMARY.md ..................... Executive overview (START HERE)
├── 📄 GETTING_STARTED.md ..................... This guide
├── 📄 REVIEW.md ............................. Architecture review + gaps
├── 📄 IMPLEMENTATION_ROADMAP.md .............. Phased plan + timelines
│
├── docs/
│   ├── architecture/
│   │   ├── domain-neutral-agentic-pipeline.md (original design)
│   │   ├── pipeline_flow_diagram.md ........... (NEW) visual diagrams
│   │   └── (will add: API_REFERENCE.md, DEVELOPMENT.md)
│   │
│   ├── backlog/
│   │   ├── initial-backlog.md ................ (seed backlog)
│   │   ├── DETAILED_BACKLOG.md ............... (NEW) 40+ stories
│   │   └── (will add: sprint-plans/, completed/)
│   │
│   └── decisions/
│       └── (to be populated with architecture decision records)
│
├── modules/ ................................ 16 independent modules
│   ├── 01_intake_nlu/
│   ├── 02_clarification_dialogue/
│   ├── 03_goal_decomposer/
│   ├── 04_method_discovery/
│   ├── 05_plan_synthesis/
│   ├── 06_code_configuration_builder/
│   ├── 07_runtime_orchestrator/
│   ├── 08_execution_adapter/
│   ├── 09_observability_monitor/
│   ├── 10_result_interpreter/
│   ├── 11_cross_validation/
│   ├── 12_self_correction_reflection/
│   ├── 13_human_in_the_loop/
│   ├── 14_provenance_memory/
│   ├── 15_policy_safety_governance/
│   ├── 16_agent_mesh_control_plane/
│   └── README.md ............................ Module overview
│
├── interfaces/ ............................. Inter-module contracts
│   └── (to be populated with typed stubs)
│
├── schemas/ ................................ JSON Schemas (data contracts)
│   ├── intent_spec.schema.json .............. (to be created)
│   ├── execution_plan.schema.json ........... (to be created)
│   ├── examples/ ............................ (to be created)
│   └── (11 more schemas needed)
│
├── configs/ ................................ Configuration files
│   ├── discovery_registry.json .............. (to be created)
│   ├── policies.yaml ........................ (to be created)
│   ├── execution_backends.yaml .............. (to be created)
│   └── chemistry_domains.yaml ............... (to be created)
│
├── tests/ .................................. Test suite
│   ├── unit/ ............................... (to be created)
│   ├── contract/ ........................... (to be created)
│   ├── integration/ ........................ (to be created)
│   └── chemistry_pilot/ .................... (to be created)
│
├── experiments/ ............................ Exploratory work
│   ├── notebook_exploration/ ............... (to be created)
│   ├── baseline_benchmarks/ ................ (to be created)
│   └── tool_integration_tests/ ............. (to be created)
│
├── scripts/ ................................ Automation helpers
│   └── (to be created: setup, test runners, deployment)
│
├── README.md ............................... Project overview (current)
├── TWAIN_Architecture.drawio ............... Original architecture diagram
├── Robert Wexler_1908.pdf .................. Historical context
└── .git/ ................................... Git repository

```

---

## Key Decisions (Before You Start)

**Before Day 1 of implementation, confirm these with your team:**

1. **Orchestrator Framework?**
   - Recommended: Custom state machine (tight control over researcher gates)
   - Alternatives: Prefect, Airflow, Temporal

2. **LLM Provider?**
   - Recommended: Claude (Anthropic) + fallback to open-source
   - Alternatives: GPT-4, Llama, Mixtral

3. **Provenance Storage?**
   - Recommended: SQLite for MVP (simple, zero infra)
   - Alternative: PostgreSQL (for multi-tenant, post-MVP)

4. **First Chemistry Domain?**
   - Recommended: Molecular solubility prediction (abundant data, fast iteration)
   - Alternatives: Molecular dynamics, quantum chemistry, reaction pathways

5. **Tool Discovery Sources?**
   - Recommended: PyPI + curated internal registry
   - Alternatives: GitHub, ArXiv, specialized chemistry databases

6. **Cost Control Model?**
   - Recommended: Token budget + iteration cap (e.g., max 5 corrections)
   - Alternatives: Compute time budget, per-month ceiling

**Action**: Schedule 1-hour design review with tech team → document decisions in `docs/decisions/01_architecture_decisions.md`

---

## Immediate Next Steps (Week 1)

### Day 1: Team Kickoff
- [ ] Read: `PROJECT_SUMMARY.md` + `REVIEW.md`
- [ ] Schedule: 1-hour design review (confirm decisions above)
- [ ] Assign: Phase 1 lead (senior engineer)

### Days 2–3: Epic 1 Design
- [ ] Finalize: IntentSpec schema (12 fields + validation rules)
- [ ] Finalize: ExecutionPlan schema (resource request + costs)
- [ ] Create: 3+ realistic examples per schema
- [ ] Agree: Python dataclass structure (Pydantic)

### Days 4–5: Project Setup
- [ ] Create: Asana or Linear project board
- [ ] Setup: GitHub Actions for CI/CD
- [ ] Create: `tests/contract/` test skeleton
- [ ] Create: `docs/decisions/01_architecture_decisions.md` (record decisions)

### Weeks 1–2: Phase 1 Implementation
- [ ] Write: 12 JSON Schema files (6 core + 6 supporting)
- [ ] Write: Python dataclass stubs + validation
- [ ] Write: Contract test suite (pytest)
- [ ] Target: 100% test pass rate by end of Week 2

---

## Communication & Collaboration

### Standup (Daily, 15 min)
- Status: What did you do yesterday? What's blockers?
- Blocker resolution: Parking lot for >2 min conversations
- Upcoming: What's next?

### Design Review (Weekly, 1 hour)
- Architecture decisions (if any open)
- Epic completion reviews (once implementation starts)
- Risk/blocker escalation

### Office Hours (Optional, 1 hour/week)
- Drop-in for quick questions
- Pair programming sessions
- Knowledge transfer

---

## Code Repository Layout (What to Ignore Initially)

**Don't worry about these until Phase 2+:**
- `.git/` — git repository (already initialized)
- `TWAIN_Architecture.drawio` — review + refactor after Phase 1 (use Lucidchart or draw.io)
- `Robert Wexler_1908.pdf` — historical context (nice to read, not required)

**Focus on these:**
- `docs/` — all documentation
- `modules/` — where your code will live
- `schemas/` — JSON Schemas (Phase 1 focus)
- `GETTING_STARTED.md` — this file

---

## Troubleshooting / FAQ

### Q: I'm confused about the architecture. Where do I start?
**A**: Read `PROJECT_SUMMARY.md` (2 min). If you still need clarity, review `pipeline_flow_diagram.md` (15 min) and/or ask in standup.

### Q: What's the critical path? What blocks other work?
**A**: Phase 1 (schemas) blocks everything else. See `IMPLEMENTATION_ROADMAP.md` section "Phased Roadmap" or the dependency diagram in `pipeline_flow_diagram.md`.

### Q: How much detail do I need before starting to code?
**A**: Finish Epic 1 (schemas) with 100% test coverage. Once schemas are locked, other modules can be built in parallel against those contracts.

### Q: What if we change a schema after Phase 2 starts?
**A**: Breaking changes require versioning. See `DETAILED_BACKLOG.md` story 1.6 (contract validation + versioning policy).

### Q: How do we handle module interdependencies during development?
**A**: Use mocks. Each module has a stub interface in `interfaces/`. Implement the real module, but mock others until they're ready.

### Q: When do we start testing?
**A**: Write tests alongside code (TDD). Phase 1 includes contract tests. Phase 2+ includes unit, integration, and replay tests.

---

## Learning Resources (Optional)

### System Design
- *System Design Interview* by Alex Xu (chapters on scaling services, event buses)
- *Designing Data-Intensive Applications* by Martin Kleppmann (chapters on consensus, stream processing)

### Multi-Agent Systems
- OpenAI Swarm documentation (agent communication patterns)
- Anthropic documentation (Claude API, function calling)

### Chemistry (Domain Knowledge)
- *Computational Chemistry* by David Young (free online)
- ChEMBL database (public chemistry data)
- RDKit documentation (molecular representations)

### Python Best Practices
- *Clean Code* by Robert C. Martin
- *Fluent Python* by Luciano Ramalho

---

## Success Checkpoints (Phase by Phase)

### Phase 1 Complete (Week 2)
- [ ] All 12 schemas defined (JSON Schema + Python dataclass)
- [ ] 3+ examples per schema
- [ ] Contract test suite passes (100%)
- [ ] Code review completed by tech lead

### Phase 2 Complete (Week 4)
- [ ] State machine implemented + tested
- [ ] Event bus implemented + tested
- [ ] Retry policy + budget tracking implemented
- [ ] Session persistence working (save/load/resume)

### Phase 3–4 Complete (Week 7)
- [ ] Intake + Clarification agents working (with mocked downstream)
- [ ] Discovery + planning agents working (with mocked downstream)
- [ ] Approval gates functional

### Phase 5 Complete (Week 9)
- [ ] Code generation producing valid Python
- [ ] Local execution adapter working
- [ ] Orchestrator coordinating stages

### Phase 6 Complete (Week 11)
- [ ] Result interpretation + validation working
- [ ] Self-correction loop making decisions

### Phase 7 Complete (Week 13)
- [ ] Provenance log capturing all events
- [ ] Policy enforcement blocking unsafe actions

### Phase 8 Complete (Week 15–18)
- [ ] E2E test: solubility prediction end-to-end
- [ ] Demo readiness + researcher feedback

---

## What's Measured / How We Track Progress

### Code Metrics
- **Test Coverage**: >80% of all modules (pytest)
- **Schema Compliance**: 100% of inter-module messages validate against schema
- **Build Success**: CI passes on all PRs (no broken builds merged)

### Operational Metrics (Post-MVP)
- **E2E Latency**: <5 min per iteration
- **Reproducibility**: 100% of historical runs replay identically
- **Success Rate**: % of researcher requests that complete successfully
- **Cost Tracking**: Actual vs. estimated cost (should be within 20%)

### Chemistry Validation
- **Solubility Prediction MSE**: <0.5 log units vs. literature (target)
- **Auto-Correction Effectiveness**: % of failed runs that auto-correct successfully
- **Researcher Approval Rate**: % of plans researcher approves (target: >80%)

---

## Questions? Next Steps?

1. **Read** `PROJECT_SUMMARY.md` (2 min)
2. **Schedule** design review with tech lead (confirm architectural decisions)
3. **Create** Asana/Linear project (track Epic 1–8 progress)
4. **Assign** Phase 1 lead (senior engineer to start schemas)
5. **Go** build! 🚀

---

**Document Version**: 1.0  
**Created**: 2026-06-03  
**Last Updated**: 2026-06-03  
**Status**: Ready for implementation
