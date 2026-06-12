# TWAIN Project Review & Enhancement Plan

**Date**: 2026-06-03  
**Status**: Pre-implementation discovery phase  
**Focus**: Domain-neutral, self-discovering, self-correcting agentic pipeline for chemistry simulation

---

## Executive Summary

The TWAIN project proposes a revolutionary **domain-neutral multi-agent orchestration system** that enables researchers to request chemistry simulations in natural language, automatically discover suitable models and tools, generate executable code, run them, validate results, and iteratively refine until convergence.

**Current State**: Architecture design and module hierarchy are well-defined. Implementation is greenfield.

**Key Strengths**:
- Clear logical flow from natural language → execution → validation → correction
- Domain-neutral core with adapter pattern for domain-specific models
- Human-in-the-loop design keeps researcher in critical approval decisions
- Contract-first approach enables strong typing and composability
- Comprehensive coverage of non-functional requirements (reproducibility, safety, cost control)

**Critical Path to MVP**: 5-8 week effort assuming 1-2 FTE senior engineers.

---

## Section 1: Architecture Validation & Observations

### 1.1 Strengths of Current Design

| Aspect | Observation |
|--------|-------------|
| **Agent Separation** | Clear, non-overlapping responsibilities. Each agent has a single transformation purpose. |
| **Contract Focus** | JSON Schema-based inter-agent communication prevents tight coupling and enables versioning. |
| **Human Gates** | Explicit approval checkpoints before risky actions (planning, major cost changes, final acceptance). |
| **Reproducibility** | Provenance memory + immutable event capture enables full audit trail and rerun from any historical state. |
| **Safety & Governance** | Policy enforcement layered throughout (discovery, planning, execution), not bolted on. |
| **Self-Correction Loop** | Bounded optimization with explicit stopping conditions prevents infinite loops and runaway costs. |
| **Domain Neutrality** | Core agents operate on abstract intent/goals; domain-specific knowledge goes into adapters. |

### 1.2 Design Gaps & Recommendations

#### Gap 1: Researcher Request Intake Format
**Issue**: How does a researcher submit a request? CLI? Web UI? API?  
**Recommendation**: Define an RPC/CLI interface first (for MVP), then add web UI later.

#### Gap 2: Tool Discovery Scope
**Issue**: "Public models" is vague. What counts as trusted? GitHub? PyPI? ArXiv? Computational Chemistry databases (e.g., EMSL, ChemSpider)?  
**Recommendation**: Create a curated registry with explicit trust levels (tier 0: internal validated, tier 1: peer-reviewed, tier 2: popular OSS, tier 3: candidate).

#### Gap 3: Chemistry Domain Adapters
**Issue**: Not yet scoped. Which chemistry simulations first? (e.g., SMILES → property prediction, molecular dynamics, quantum chemistry ground states, reaction pathways)  
**Recommendation**: Start with **molecular property prediction** (e.g., SMILES/SDF → toxicity/solubility). Lowest barrier to entry, easiest to validate with literature benchmarks.

#### Gap 4: Execution Backends
**Issue**: Architecture mentions local/cluster/cloud, but needs MVP scope.  
**Recommendation**: Start with **local Python** (no Docker, no HPC). Add batch/cluster after MVP success.

#### Gap 5: Cost/Budget Tracking
**Issue**: Non-functional requirement listed, but no budget model defined.  
**Recommendation**: Implement simple token/iteration budgets per run. Track LLM calls, compute time, wall-clock time.

#### Gap 6: Convergence Criteria
**Issue**: "acceptance criteria met" is the stopping rule, but how do researchers define these?  
**Recommendation**: Templates: `MSE < 0.05`, `matches literature baseline within 10%`, `no resource anomalies`. Let researchers edit.

#### Gap 7: Error Recovery
**Issue**: What if a tool fails mid-execution? Timeout? Out of memory?  
**Recommendation**: Explicit failure classification (recoverable/fatal) and recovery strategies (retry, relax constraints, fallback tool).

---

## Section 2: Module Dependency Graph & Staging

### 2.1 Critical Path (MVP)

```
Phase 1: Schemas & Control Plane (Week 1)
  ├─ 01_intake_nlu (stub)
  ├─ Schemas: IntentSpec, ExecutionPlan, ValidationReport
  ├─ 16_agent_mesh_control_plane (state machine core)
  └─ 13_human_in_the_loop (approval UI/CLI)

Phase 2: Discovery & Planning (Week 2–3)
  ├─ 02_clarification_dialogue (Q&A loop)
  ├─ 03_goal_decomposer (simple heuristics)
  ├─ 04_method_discovery (local registry lookup + PyPI search)
  ├─ 05_plan_synthesis (pick best candidate, build DAG)
  └─ Schemas: GoalGraph, CandidateMethodSet

Phase 3: Execution (Week 3–4)
  ├─ 06_code_configuration_builder (Python script gen)
  ├─ 08_execution_adapter (local subprocess)
  ├─ 07_runtime_orchestrator (session + event loop)
  └─ 09_observability_monitor (log capture + anomaly signals)

Phase 4: Validation & Correction (Week 4–5)
  ├─ 10_result_interpreter (output parsing)
  ├─ 11_cross_validation (vs literature baseline)
  ├─ 12_self_correction_reflection (decision logic)
  └─ Schemas: ResultPackage, CorrectionPlan

Phase 5: Provenance & Governance (Week 5–6)
  ├─ 14_provenance_memory (SQLite + JSON event log)
  ├─ 15_policy_safety_governance (basic allow-list checks)
  └─ Schema: ProvenanceEvent

Phase 6: Chemistry Pilot + Integration (Week 6–8)
  ├─ End-to-end flow: NL request → execution → validation → approval
  ├─ First chemistry benchmark (molecular property prediction)
  ├─ Researcher feedback loop & refinement
  └─ Documentation & reproducibility validation
```

---

## Section 3: Key Design Decisions Requiring Clarification

| Decision | Options | Recommendation | Rationale |
|----------|---------|-----------------|-----------|
| **Orchestrator Framework** | Custom graph runner vs Prefect/Airflow/Temporal | Custom simple state machine for MVP | Tight control over researcher gates + visibility into correction loops |
| **LLM Provider** | Claude, GPT-4, open-source (Llama), hybrid | Claude with fallback to open-source | Fast iteration + strong reasoning for multi-step planning |
| **Provenance Storage** | SQLite, PostgreSQL, event sourcing (Kafka) | SQLite + JSON file archive (local) | Simple, reproducible, zero infrastructure for MVP |
| **First Chemistry Model** | SMILES→property, molecular dynamics, quantum, reaction pathways | SMILES→toxicity/solubility (transfer learning) | Abundant public data, fast iteration, easy validation |
| **Discovery Sources** | GitHub, PyPI, Conda, specialized DBs, arXiv | PyPI (polyfill library) + curated internal registry | Balanced between breadth and trust; avoids model-zoo fragmentation |
| **Cost Control Model** | Token budget, compute time, iteration cap | Token + iteration cap (e.g., 5 replan cycles max) | Prevents infinite loops while allowing some recovery |

---

## Section 4: Expanded Module Descriptions (Pre-Implementation)

### 4.1 **01_Intake_NLU**
- **Purpose**: Parse researcher request into structured IntentSpec
- **Input**: Natural language description of research goal
- **Output**: IntentSpec (objective, domain hints, material/system descriptors, constraints)
- **MVP Behavior**: Simple regex + LLM prompt to extract objective + domain + constraints
- **Example**: "I want to predict the solubility of aspirin in water at 25°C" → IntentSpec with objective, chemical domain, compound name, temperature constraint
- **File Structure**:
  - `intake_engine.py` - Core NLU logic
  - `prompt_templates/` - System prompts for LLM extraction
  - `schemas.py` - IntentSpec dataclass

### 4.2 **02_Clarification_Dialogue**
- **Purpose**: Ask targeted Q&A until IntentSpec quality threshold is met
- **Input**: IntentSpec with confidence scores per field
- **Output**: RefinedIntentSpec (all fields above confidence threshold)
- **MVP Behavior**: Multi-turn dialogue asking for missing/ambiguous details
- **Quality Thresholds**: Every field must have >80% confidence; domain hints must resolve to a catalog
- **File Structure**:
  - `clarification_engine.py` - Q&A state machine
  - `quality_validator.py` - Confidence scoring
  - `dialogue_templates/` - Human-readable Q&A

### 4.3 **03_Goal_Decomposer**
- **Purpose**: Break researcher request into executable sub-goals
- **Input**: RefinedIntentSpec
- **Output**: GoalGraph (DAG of sub-goals with dependencies)
- **MVP Behavior**: Simple heuristics (model selection → data prep → execution → validation)
- **File Structure**:
  - `decomposer_engine.py` - Heuristic goal breakdown
  - `goal_templates/` - Goal templates by domain
  - `graph_builder.py` - DAG construction

### 4.4 **04_Method_Discovery**
- **Purpose**: Discover suitable public tools/models/workflows
- **Input**: GoalGraph, RefinedIntentSpec
- **Output**: CandidateMethodSet (top-k ranked methods with evidence)
- **MVP Behavior**: Query internal registry + PyPI for relevant packages; score by relevance, maturity, license
- **Scoring Rubric**: relevance (0–1), maturity (stars/citations), license (1=permissive, 0=restrictive), reproducibility evidence
- **File Structure**:
  - `discovery_engine.py` - Registry search and ranking
  - `registry/` - Curated methods database (JSON)
  - `scorers/` - Scoring rubric implementations
  - `sources/` - Adapters for PyPI, GitHub, curated registries

### 4.5 **05_Plan_Synthesis**
- **Purpose**: Select toolchain and compose executable workflow
- **Input**: CandidateMethodSet, RefinedIntentSpec
- **Output**: ExecutionPlan (selected pipeline, compute estimate, resource request, acceptance criteria)
- **MVP Behavior**: Pick top candidate, define data flow DAG, estimate runtime/cost, set quality thresholds
- **File Structure**:
  - `plan_synthesizer.py` - Plan composition
  - `estimators/` - Cost and runtime estimation
  - `validators/` - Plan feasibility checks

### 4.6 **06_Code_Configuration_Builder**
- **Purpose**: Generate runnable scripts, configs, and container specs
- **Input**: ExecutionPlan
- **Output**: RunBundle (Python scripts, config files, environment specs, inline tests)
- **MVP Behavior**: Generate Python script with data loading, model execution, result serialization
- **File Structure**:
  - `codegen_engine.py` - Script generation
  - `templates/` - Code templates by model family
  - `validators/` - Static analysis (imports, syntax)

### 4.7 **07_Runtime_Orchestrator**
- **Purpose**: Coordinate execution lifecycle and module interactions
- **Input**: RunBundle, execution directives
- **Output**: RunSession (execution record with timeline and artifacts)
- **MVP Behavior**: Sequential execution of: setup → run → capture → cleanup
- **File Structure**:
  - `orchestrator.py` - Main session controller
  - `session.py` - RunSession dataclass
  - `lifecycle.py` - State machine for run phases

### 4.8 **08_Execution_Adapter**
- **Purpose**: Backend-agnostic execution abstraction
- **Input**: RunBundle, backend type (local/cluster/cloud)
- **Output**: ExecutionEvents (status updates, logs, exit codes)
- **MVP Behavior**: Local subprocess execution with STDOUT/STDERR capture
- **File Structure**:
  - `adapter_base.py` - Abstract adapter interface
  - `local_adapter.py` - Subprocess-based execution
  - `event_emitter.py` - Event stream

### 4.9 **09_Observability_Monitor**
- **Purpose**: Capture logs, metrics, traces, and failure signals
- **Input**: ExecutionEvents (live stream)
- **Output**: RunDiagnostics (aggregated signals + anomaly flags)
- **MVP Behavior**: Parse logs for warnings/errors, track memory/CPU, flag timeout/OOM
- **File Structure**:
  - `monitor.py` - Log aggregator
  - `detectors/` - Anomaly detectors (timeout, OOM, convergence failure)
  - `diagnostics.py` - RunDiagnostics dataclass

### 4.10 **10_Result_Interpreter**
- **Purpose**: Parse execution outputs into structured findings
- **Input**: Execution artifacts (output files, logs)
- **Output**: ResultPackage (normalized metrics, researcher-facing summary, confidence bounds)
- **MVP Behavior**: Extract numerical results from output, compute KPIs, add uncertainty metadata
- **File Structure**:
  - `interpreter_engine.py` - Output parsing
  - `extractors/` - Domain-specific output parsers
  - `formatters/` - Human-readable summary generation

### 4.11 **11_Cross_Validation**
- **Purpose**: Compare outcomes against baselines and prior runs
- **Input**: ResultPackage, ExecutionPlan, provenance history
- **Output**: ValidationReport (agreement metrics, gap analysis, improvement signal)
- **MVP Behavior**: Compare primary metric vs literature baseline or prior internal run
- **File Structure**:
  - `validator.py` - Comparison logic
  - `baselines/` - Reference results (literature, benchmarks)
  - `metrics.py` - Agreement measures (MSE, relative error, etc.)

### 4.12 **12_Self_Correction_Reflection**
- **Purpose**: Diagnose failures and propose targeted modifications
- **Input**: ValidationReport, ExecutionPlan, RunDiagnostics
- **Output**: CorrectionPlan (proposed deltas, expected gain, rerun budget)
- **MVP Behavior**: IF validation fails, propose one of: relax constraints, change solver hyperparameters, try next-best model, request more data
- **File Structure**:
  - `reflection_engine.py` - Diagnosis and proposal logic
  - `strategies/` - Correction strategies by failure mode
  - `optimizer.py` - Bounded rerun loop controller

### 4.13 **13_Human_In_The_Loop**
- **Purpose**: Manage researcher approvals and overrides
- **Input**: Plans, corrections, results (awaiting approval)
- **Output**: ApprovalDecision (approved, rejected with feedback, edited request)
- **MVP Behavior**: CLI or simple web UI showing plan + cost estimate, waiting for yes/no/edit
- **File Structure**:
  - `approval_handler.py` - Approval gate logic
  - `ui/` - CLI/web interface stubs
  - `notification.py` - Alert researcher of pending decision

### 4.14 **14_Provenance_Memory**
- **Purpose**: Immutable record of all decisions and execution state
- **Input**: All module outputs (events, decisions, code, configs)
- **Output**: ProvenanceRecord (versioned, queryable audit trail)
- **MVP Behavior**: SQLite DB + JSON event log; enable rerun from any historical state
- **File Structure**:
  - `store.py` - SQLite backend
  - `event_log.py` - Immutable append-only event stream
  - `replay.py` - Load and rerun from historical record

### 4.15 **15_Policy_Safety_Governance**
- **Purpose**: Enforce policy constraints, licensing, and risk thresholds
- **Input**: Discovery results, ExecutionPlan, discovered tools
- **Output**: PolicyDecision (approved, blocked, requires_review)
- **MVP Behavior**: Check tool licenses (allow permissive only), check compute budget vs policy ceiling, block unsafe patterns
- **File Structure**:
  - `policy_engine.py` - Policy checker
  - `policies/` - Configurable policy rules (YAML)
  - `license_db.py` - License metadata

### 4.16 **16_Agent_Mesh_Control_Plane**
- **Purpose**: Route, coordinate, and resilience-manage agent interactions
- **Input**: All agent requests/responses
- **Output**: ControlPlaneEvents (routing decisions, retry strategies, circuit breaker signals)
- **MVP Behavior**: State machine for module transitions, retry logic with exponential backoff, timeout enforcement
- **File Structure**:
  - `control_plane.py` - State machine core
  - `state_machine.py` - Transition guards and actions
  - `retry_policy.py` - Backoff and circuit breaker logic
  - `event_bus.py` - Inter-module messaging

---

## Section 5: Supporting Infrastructure

### 5.1 Schemas Directory
Key JSON Schema files to define first:

- `intent_spec.schema.json` - Researcher request structure
- `goal_graph.schema.json` - Decomposed goals and dependencies
- `candidate_method_set.schema.json` - Ranked discovery results
- `execution_plan.schema.json` - Executable plan + resource request + acceptance criteria
- `run_bundle.schema.json` - Generated code + config + metadata
- `execution_event.schema.json` - Runtime status updates
- `run_diagnostics.schema.json` - Execution anomalies and signals
- `result_package.schema.json` - Normalized output metrics
- `validation_report.schema.json` - Comparison vs baselines
- `correction_plan.schema.json` - Proposed modifications and reruns
- `provenance_event.schema.json` - Immutable audit trail entry
- `approval_decision.schema.json` - Researcher approval state

### 5.2 Interfaces Directory
Typed Python/TypeScript stubs for inter-module contracts:

- `agent_interface.py` - Base agent class with input/output spec
- `discovery_interface.py` - Method discovery contract
- `execution_interface.py` - Backend adapter interface
- `validation_interface.py` - Baseline and comparison interface
- `approval_interface.py` - Researcher decision interface

### 5.3 Configs Directory
Configuration files:

- `policy_config.yaml` - License and compute budget policy
- `discovery_sources.yaml` - Trusted registries and model sources
- `execution_backends.yaml` - Available execution targets (local/cluster/cloud)
- `chemistry_domains.yaml` - Supported chemistry domains and example models
- `loglevel_config.yaml` - Logging configuration

### 5.4 Tests Directory
Comprehensive test suite (contract, unit, integration, replay):

- `unit/` - Individual module logic tests
- `contract/` - Inter-module schema and interface tests
- `integration/` - End-to-end flow tests (mock execution)
- `replay/` - Historical provenance replay tests
- `chemistry_pilot/` - Domain-specific validation tests

### 5.5 Experiments Directory
Sandboxes and exploratory work:

- `notebook_exploration/` - Jupyter notebooks for prototyping discovery, planning, validation
- `baseline_benchmarks/` - Reference implementations and literature baselines
- `tool_integration_tests/` - Ad-hoc tests for new tools/models

---

## Section 6: Risk Mitigation

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|-----------|
| **LLM reasoning failure** (bad plan synthesis) | Medium | High | Explicit human approval gate before execution; option to override or request re-plan |
| **Tool discovery toxicity** (unsafe/invalid tools discovered) | Medium | High | Curated registry + policy allow-list; tie discovery to trust tier; researcher final approval |
| **Execution divergence** (run on cluster != local) | Low | Medium | Reproducibility tests; containerization (Phase 2); test harness captures env fingerprint |
| **Infinite correction loop** (never converges) | Low | Medium | Explicit iteration cap (e.g., 5 cycles); convergence metric; researcher can force stop |
| **Provenance data loss** | Low | High | Append-only event log + SQLite; daily backup; recovery test suite |
| **Researcher overwhelm** (too many options/questions) | Medium | Low | Progressive disclosure: 3-5 top candidates max; confidence-driven clarification (only ask when needed) |
| **Cost overrun** | Medium | Medium | Per-run and per-project budget guardrails; researcher approval for cost increases >10% |

---

## Section 7: Success Criteria (End of Phase 6)

### Functional
- [ ] Researcher can submit natural language request (NL)
- [ ] System clarifies ambiguities and converges to executable plan
- [ ] Plan is synthesized, code generated, and executed locally
- [ ] Results are validated against a literature baseline
- [ ] If validation fails, system proposes and executes auto-correction
- [ ] Researcher can approve/reject at each stage
- [ ] Full provenance record enables deterministic replay

### Non-Functional
- [ ] E2E latency (request → final result): <5 minutes per iteration (LLM + execution + validation)
- [ ] Reproducibility: 100% of historical runs replay identically
- [ ] Explainability: Each decision shows >2 alternatives + ranking rationale
- [ ] Safety: 0 unsafe tools discovered; 100% policy compliance enforced
- [ ] Cost: Stays within per-run budget; researcher sees real-time cost tracking

### Validation (Chemistry Pilot)
- [ ] Predict solubility of 5 benchmark molecules; match literature baseline within 15% MSE
- [ ] Detect correction opportunity; auto-rerun improves baseline; researcher approves final result
- [ ] Produce reproducible artifact set: code, config, provenance, results (shareable with collaborators)

---

## Section 8: Deployment & Operations (Post-MVP)

### Phase 7: Production Readiness
- Containerize backend (Docker)
- Add authentication/multi-tenant support (OAuth)
- Deploy on GCP CloudRun or similar
- Add cost monitoring dashboard
- Establish SLO targets (latency, success rate)

### Phase 8: Expand Chemistry Domains
- Add molecular dynamics simulation support
- Add reaction pathway prediction
- Add quantum chemistry (ground state energy, etc.)
- Add materials property prediction

### Phase 9: Community & Sustainability
- Document API and domain adapter patterns
- Publish pip/conda packages for core modules
- Establish external contributor guidelines
- Build example notebooks and tutorials

---

## Next Immediate Actions

1. **Define JSON Schemas** (Epic 1) → `schemas/` folder
2. **Implement Control Plane State Machine** (Epic 2) → `modules/16_agent_mesh_control_plane/control_plane.py`
3. **Implement Intake + Clarification MVP** (partial Epics 3 & 4) → `modules/01_intake_nlu/` and `modules/02_clarification_dialogue/`
4. **Design First Chemistry Benchmark** (Epic 8) → `experiments/baseline_benchmarks/molecular_solubility/`
5. **Standup: Discuss architecture decisions** (section 3) → document conclusions

---

## References

- Current architecture doc: `docs/architecture/domain-neutral-agentic-pipeline.md`
- Initial backlog: `docs/backlog/initial-backlog.md`
- Module hierarchy: `modules/README.md`
- Existing drawio diagram: `TWAIN_Architecture.drawio` (review and update with Phase 1–6 flow)
