# TWAIN Detailed Implementation Backlog

**Version**: 1.0  
**Last Updated**: 2026-06-03  
**Status**: Pre-implementation planning  

---

## Overview

This backlog breaks down the TWAIN MVP into actionable user stories, organized by epic and priority tier. Each story includes acceptance criteria, effort estimates, and dependencies.

**Estimation Scale**: S (1–2 days), M (3–5 days), L (1–2 weeks), XL (2–3 weeks)

---

## Epic 1: Contract-First Foundation

**Goal**: Define all data contracts (JSON Schemas) and validation infrastructure. Enables downstream modules to be built in parallel with confidence.

**Effort**: 2 weeks (M + L)  
**Priority**: Critical Path

---

### Story 1.1: Define IntentSpec Schema

**As a** system architect  
**I want to** define the canonical structure for parsed researcher requests  
**So that** upstream NLU and downstream modules have a contract to rely on

**Acceptance Criteria**:
- [ ] JSON Schema file: `schemas/intent_spec.schema.json` defined with:
  - `objective` (string, required): Research goal in normalized form
  - `domain` (enum: "molecular_chemistry", "materials", "quantum", "reactions", etc.)
  - `system_descriptors` (object): Chemical system description (e.g., `compound_name`, `smiles`, `formula`)
  - `constraints` (array of objects): Temperature, pressure, solvent, etc.
  - `acceptance_metrics` (array of objects): Quality thresholds (e.g., `metric_name`, `target_value`, `tolerance`)
  - `metadata` (object): Confidence scores per field (0–1), disambiguity flags
- [ ] Python dataclass: `intake/intent_spec.py` with validation
- [ ] Unit tests: `tests/unit/test_intent_spec.py` (JSON roundtrip, validation rules)
- [ ] Example valid instance: `schemas/examples/intent_spec_example.json`

**Definition of Done**:
- Schema validates against 3+ diverse researcher request types
- Python model can serialize/deserialize without loss
- Tests pass

**Effort**: M (3–5 days)  
**Owner**: TBD  
**Depends on**: None

---

### Story 1.2: Define GoalGraph Schema

**As a** goal decomposer  
**I want to** emit a structured DAG of sub-goals with dependencies  
**So that** downstream planners can reason about execution order

**Acceptance Criteria**:
- [ ] JSON Schema: `schemas/goal_graph.schema.json` with:
  - `goals` (array): ID, type, description, owner agent, acceptance criteria
  - `edges` (array): Source goal ID, target goal ID, edge type (seq, parallel, conditional)
  - `metadata` (object): Created timestamp, source intent ID, rationale
- [ ] Python graph builder: `goal_decomposer/graph_builder.py` with:
  - Topological sort validation
  - Cycle detection
  - Degree (in/out) verification
- [ ] Unit tests: `tests/unit/test_goal_graph.py` (valid/invalid DAGs, traversal)
- [ ] Example: `schemas/examples/goal_graph_molecular_solubility.json`

**Definition of Done**:
- 100% of valid GoalGraphs pass schema validation
- Topological sort produces consistent orderings
- Tests pass

**Effort**: M (3–5 days)  
**Owner**: TBD  
**Depends on**: Story 1.1

---

### Story 1.3: Define ExecutionPlan Schema

**As a** plan synthesizer  
**I want to** encode a complete, executable pipeline with resource and risk metadata  
**So that** code generator and orchestrator can unambiguously build and run the plan

**Acceptance Criteria**:
- [ ] JSON Schema: `schemas/execution_plan.schema.json` with:
  - `selected_method` (object): Tool/model name, version, source repo/package
  - `data_pipeline` (object): Input/output stages, data types, transformations
  - `compute_estimate` (object): Estimated CPU hours, memory GB, wall-clock time min
  - `cost_estimate` (object): LLM tokens, compute cost (USD), risk score
  - `acceptance_criteria` (array): Same as intent, but resolved to specific metrics (e.g., "MSE < 0.05")
  - `resource_request` (object): CPU cores, memory, disk, timeout
  - `safety_notes` (array): Policy flags, license, reproducibility concerns
  - `metadata` (object): Created timestamp, goal graph ID, candidate rank
- [ ] Python dataclass: `plan_synthesizer/execution_plan.py`
- [ ] Validator: `plan_synthesizer/plan_validator.py` (feasibility checks: resource availability, cost <= budget)
- [ ] Tests: `tests/unit/test_execution_plan.py`
- [ ] Example: `schemas/examples/execution_plan_solubility.json`

**Definition of Done**:
- ExecutionPlans are fully specified (no ambiguity about what code gen receives)
- Cost estimates can be compared against policy budget
- Validator catches infeasible plans

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 1.1, 1.2

---

### Story 1.4: Define ResultPackage and ValidationReport Schemas

**As a** result interpreter and validator  
**I want to** encode execution outputs and cross-validation findings in structured form  
**So that** self-correction reflection can reason about success/failure programmatically

**Acceptance Criteria**:
- [ ] JSON Schema: `schemas/result_package.schema.json` with:
  - `primary_metric` (object): Name, value, uncertainty (std dev / confidence interval)
  - `secondary_metrics` (array): Additional KPIs
  - `raw_outputs` (object): Paths to output files, logs, checkpoints
  - `execution_context` (object): Actual CPU/memory/time used, exit code
  - `quality_flags` (array): Warnings (convergence issues, missing data, etc.)
  - `metadata` (object): Execution plan ID, tool version, timestamp
- [ ] JSON Schema: `schemas/validation_report.schema.json` with:
  - `baseline_comparison` (object): Literature/prior run metric, agreement (%), gap analysis
  - `acceptance_status` (enum: "accepted", "rejected", "needs_review")
  - `diagnoses` (array): Detected failure modes and confidence
  - `improvement_opportunity` (object): Quality gain if corrected, estimated effort
  - `metadata` (object): Result package ID, baseline ID, confidence

- [ ] Python dataclasses: `result_interpreter/result_package.py`, `cross_validation/validation_report.py`
- [ ] Tests: Roundtrip schema validation, metric extraction

**Definition of Done**:
- Results from real tool executions can be normalized into ResultPackage
- ValidationReport can be generated from ResultPackage + baseline
- No data loss in roundtrip

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 1.1

---

### Story 1.5: Define CorrectionPlan and ProvenanceEvent Schemas

**As a** self-correction reflection and provenance modules  
**I want to** encode proposed modifications and immutable audit trail  
**So that** reruns are traceable and correction strategies can be scored objectively

**Acceptance Criteria**:
- [ ] JSON Schema: `schemas/correction_plan.schema.json` with:
  - `diagnosis` (string): Root cause of validation failure
  - `proposed_corrections` (array of objects): Modification type, target (input/model/param/solver), new value, rationale
  - `expected_gain` (object): Improvement forecast (primary metric delta), confidence
  - `rerun_budget` (object): Token budget, iteration allowance, cost ceiling
  - `fallback_strategy` (string): If corrections don't help, what's next?
  - `metadata` (object): Validation report ID, iteration count
- [ ] JSON Schema: `schemas/provenance_event.schema.json` with:
  - `event_type` (enum: "request", "plan", "execute", "validate", "correct", "approve")
  - `timestamp`, `agent_id`, `input_hash`, `output_hash`, `decision_rationale`
  - Enables: "What was the context for this decision?" + deterministic replay
- [ ] Append-only event log implementation: `provenance_memory/event_log.py`
- [ ] Tests: Replay from event log produces identical artifacts

**Definition of Done**:
- 100% of pipeline decisions are provenance-logged
- Replay from event log reproduces any historical run
- Correction proposals are scored and ranked

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 1.1–1.4

---

### Story 1.6: Contract Validation Test Suite

**As a** developer  
**I want to** automatically validate that all inter-module contracts (schemas) are mutually consistent  
**So that** contract changes don't break downstream modules

**Acceptance Criteria**:
- [ ] Test suite: `tests/contract/test_schema_consistency.py` verifies:
  - All schemas pass JSON Schema meta-validation
  - Cross-schema references (e.g., ExecutionPlan references GoalGraph ID) are valid
  - Example files validate against their schemas
  - No circular dependencies in schema imports
- [ ] Schema versioning policy: `docs/decisions/schema_versioning.md`
  - Deprecation strategy for breaking changes
  - Migration scripts for old → new contract versions
- [ ] Integration test: E2E happy path (request → plan → execution → result → validation) validates all contracts

**Definition of Done**:
- All contract tests pass on every commit
- Versioning policy is documented and followed
- Example files validate

**Effort**: M (3–4 days)  
**Owner**: TBD  
**Depends on**: Stories 1.1–1.5

---

## Epic 2: Control Plane & Orchestration

**Goal**: Implement the core state machine and event bus that coordinates agent interactions, retries, timeouts, and budgets.

**Effort**: 2 weeks (L)  
**Priority**: Critical Path (blocks everything else)

---

### Story 2.1: Implement Agent State Machine

**As a** orchestrator  
**I want to** define state transitions and guards for the module pipeline  
**So that** the system never enters an invalid state and recovery is automatic

**Acceptance Criteria**:
- [ ] State machine: `modules/16_agent_mesh_control_plane/state_machine.py` defines:
  - States: INTAKE → CLARIFY → DECOMPOSE → DISCOVER → PLAN → BUILD → EXECUTE → INTERPRET → VALIDATE → {ACCEPT, CORRECT, REPLAN}
  - Transitions with guards (e.g., "Can only execute if plan is approved")
  - Each state is idempotent (safe to retry without re-executing side effects)
  - Rollback capability (revert to PLAN state for re-synthesis)
- [ ] Guard conditions prevent invalid transitions:
  - Can't EXECUTE without APPROVED plan
  - Can't VALIDATE without successful EXECUTION
  - Can't CORRECT without REJECTED validation
- [ ] Python enum and transition logic: `state_machine.py` with unit tests
- [ ] State recovery: If control plane crashes, resume from last committed state

**Definition of Done**:
- State machine passes liveness tests (no deadlocks)
- Recovery from crash-and-restart is deterministic
- Tests cover all valid and invalid transitions

**Effort**: L (1–2 weeks)  
**Owner**: TBD  
**Depends on**: Epic 1 (schemas)

---

### Story 2.2: Implement Event Bus & Inter-Agent Messaging

**As a** module developer  
**I want to** send and receive structured messages without tight coupling  
**So that** modules can be tested and deployed independently

**Acceptance Criteria**:
- [ ] Event bus: `modules/16_agent_mesh_control_plane/event_bus.py` provides:
  - Pub/sub interface: `publish(event_type, payload)`, `subscribe(event_type, handler)`
  - Event routing: Payload validated against schema before delivery
  - Event history: Last 1000 events in memory + append-only log
  - Priority levels: CRITICAL (immediate delivery) vs DEFAULT (batched)
- [ ] Message format: All inter-agent messages are events with:
  - `event_type`, `timestamp`, `source_agent_id`, `payload`, `trace_id` (for debugging)
- [ ] Handler interface: Agents register handlers for incoming events
- [ ] Tests:
  - Pub/sub basic functionality
  - Payload schema validation
  - Handler execution order (deterministic)
  - Event replay from log

**Definition of Done**:
- All agent communication goes through event bus
- No direct function calls between agents
- Event logs can be replayed to reproduce state

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 2.1

---

### Story 2.3: Implement Retry Policy & Exponential Backoff

**As a** orchestrator  
**I want to** automatically recover from transient failures (network timeout, API rate limit)  
**So that** user doesn't see spurious failures

**Acceptance Criteria**:
- [ ] Retry policy: `modules/16_agent_mesh_control_plane/retry_policy.py`
  - Max retries: 3 by default, configurable
  - Backoff: exponential (1s, 2s, 4s, 8s, …) with jitter (±10%)
  - Jitter prevents thundering herd (concurrent retries)
  - Classify errors: TRANSIENT (retry) vs PERMANENT (fail fast)
  - Examples: timeout, 429 (rate limit), 500 (server error) = TRANSIENT; 400 (bad request), 403 (unauthorized) = PERMANENT
- [ ] Circuit breaker:
  - Track consecutive failures per external service
  - If >5 failures in 60s, open circuit (fail-fast for 5 min)
  - Log and alert on circuit-open events
- [ ] Tests:
  - Simulate transient failures; verify retry happens
  - Verify exponential backoff + jitter
  - Verify circuit opens and closes correctly

**Definition of Done**:
- Transient failures don't propagate to researcher
- Permanent failures fail fast with clear error
- Circuit breaker prevents cascading failures

**Effort**: M (3–4 days)  
**Owner**: TBD  
**Depends on**: Story 2.2

---

### Story 2.4: Implement Timeout & Budget Enforcement

**As a** control plane  
**I want to** enforce hard limits on execution time and LLM token usage  
**So that** no single run can exceed cost or time budget

**Acceptance Criteria**:
- [ ] Budget tracker: `modules/16_agent_mesh_control_plane/budget_tracker.py`
  - Per-run budget: max LLM tokens (e.g., 10k), max iterations (e.g., 5), max wall-clock time (e.g., 30 min)
  - Global budget: per-project, per-month token/cost ceiling (if multi-project)
  - Real-time tracking: `budget_used()`, `budget_remaining()`, `budget_exceeded()`
  - Policy enforcement: Reject new request if global budget exceeded; reject new iteration if run budget exceeded
- [ ] Timeout: `timeout_manager.py`
  - Per-stage timeout: EXECUTE stage has 20 min timeout; CLARIFY has 5 min
  - Graceful shutdown: Send SIGTERM, wait 10s, then SIGKILL
  - Record actual execution time for future estimation
- [ ] Tests:
  - Budget enforcement blocks overages
  - Timeout kills long-running processes
  - Budget tracking is accurate

**Definition of Done**:
- No run can exceed budget without researcher override
- Timeouts are enforced and logged
- Cost tracking matches actual LLM/compute costs

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 2.1, 2.2

---

### Story 2.5: Implement Session State & Persistence

**As a** orchestrator  
**I want to** persist run state (session) so interrupted runs can resume  
**So that** network glitches or service restarts don't lose progress

**Acceptance Criteria**:
- [ ] Session model: `modules/07_runtime_orchestrator/session.py`
  - `RunSession` dataclass: ID, researcher ID, intent, plan, execution state, results, provenance log
  - Checkpoint intervals: Save to disk every 30s or after each stage transition
  - Resumable: Can reload session and continue from last checkpoint
- [ ] Persistence: `modules/14_provenance_memory/store.py`
  - Store: SQLite (local) or PostgreSQL (cloud)
  - Each session: one row in `sessions` table + event log
  - Query: `get_session(session_id)`, `list_sessions(researcher_id)`, `resume_session(session_id)`
- [ ] Tests:
  - Session serialize/deserialize roundtrip
  - Restart from checkpoint produces identical state
  - Concurrent session isolation

**Definition of Done**:
- Session state survives process restart
- Resume is deterministic
- Multiple concurrent sessions don't interfere

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Stories 2.1, 2.2, 2.4

---

## Epic 3: Researcher-In-The-Loop UX

**Goal**: Build approval gates and clarification loops that keep researcher in control.

**Effort**: 2 weeks (L)  
**Priority**: High (gates all execution)

---

### Story 3.1: Implement Approval Gate Infrastructure

**As a** researcher  
**I want to** see what the system plans to do before it runs  
**So that** I can catch mistakes or override the plan

**Acceptance Criteria**:
- [ ] Approval checkpoint: `modules/13_human_in_the_loop/approval_handler.py`
  - Workflow: Plan synthesized → state = AWAITING_APPROVAL → block execution until researcher responds
  - Display: Show researcher the plan in human-readable form (formatted ExecutionPlan)
  - Actions: Approve, reject, edit, request re-plan
- [ ] CLI interface: `modules/13_human_in_the_loop/cli_ui.py`
  - Command: `twain approve <session_id>` → show plan, ask yes/no
  - Command: `twain reject <session_id> --reason "..."` → record feedback, trigger re-plan
  - Command: `twain edit <session_id> --plan <json>` → modify plan before approval
- [ ] Notification: Alert researcher when approval is needed
  - Via stdout (if interactive), or
  - Via email/webhook (if async)
- [ ] Tests:
  - Approval workflow transitions correctly
  - Rejection triggers re-planning
  - Edit is validated against ExecutionPlan schema

**Definition of Done**:
- Researcher can see and approve/reject/edit every plan before execution
- Rejection feedback is captured and surfaces to planning agent

**Effort**: L (1–2 weeks)  
**Owner**: TBD  
**Depends on**: Stories 2.1, Epic 1 (schemas)

---

### Story 3.2: Implement Clarification Loop with Confidence Threshold

**As a** clarification agent  
**I want to** ask the researcher follow-up questions until my understanding is confident enough  
**So that** I don't synthesize plans based on ambiguous intent

**Acceptance Criteria**:
- [ ] Confidence model: `modules/02_clarification_dialogue/quality_validator.py`
  - Score each field of IntentSpec: 0 (unknown) to 1 (certain)
  - Minimum quality threshold: 0.8 for all fields; domain hint must resolve to a catalog entry
  - If any field < 0.8, add to clarification queue
- [ ] Dialogue engine: `modules/02_clarification_dialogue/clarification_engine.py`
  - Read unconfident fields from IntentSpec
  - Generate 1–3 targeted clarification questions (human-readable)
  - Collect researcher answers
  - Re-score IntentSpec with new information
  - Repeat until threshold met (up to 3 rounds; force-continue if researcher impatient)
- [ ] UI: `modules/02_clarification_dialogue/cli_dialogue.py`
  - Prompt: "I'm not sure what <detail> means. Can you clarify?"
  - Accept free-text response, update score
- [ ] Tests:
  - Confidence scores are calibrated (high confidence = correct 80%+ of time)
  - Dialogue terminates in ≤3 rounds

**Definition of Done**:
- System doesn't plan until IntentSpec confidence >= threshold
- Researcher can skip clarification if they trust initial parsing
- Dialogue is natural and concise (<5 questions max)

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 1.1

---

### Story 3.3: Implement Final Acceptance & Override Workflow

**As a** researcher  
**I want to** review final results and decide whether to accept, request changes, or override  
**So that** I'm not locked into a failed run

**Acceptance Criteria**:
- [ ] Final review checkpoint: `modules/13_human_in_the_loop/final_review_handler.py`
  - Triggered after validation (whether accepted or rejected)
  - Display: ResultPackage + ValidationReport in human-readable form
  - Actions: Accept (publish), Request changes (trigger re-plan or correction), Override (use results despite rejection)
- [ ] Override workflow: If researcher overrides validation rejection:
  - Record decision + rationale in provenance
  - Flag result with "override" marker
  - Continue to publication (don't block)
- [ ] UI: `modules/13_human_in_the_loop/final_review_cli.py`
  - Show metrics, baseline comparison, quality flags
  - Prompt: "Is this result acceptable?"
  - If no: "What should we change?" → triggers correction loop
- [ ] Tests:
  - Accept publishes artifacts
  - Override publishes with flag
  - Request-changes triggers new replan

**Definition of Done**:
- Researcher has final say on all results
- Overrides are recorded for audit

**Effort**: M (3–4 days)  
**Owner**: TBD  
**Depends on**: Story 3.1

---

## Epic 4: Discovery & Planning

**Goal**: Implement tool discovery and plan synthesis with explainable ranking.

**Effort**: 2.5 weeks (L)  
**Priority**: Critical Path

---

### Story 4.1: Implement Method Discovery Registry

**As a** discovery engine  
**I want to** have a curated database of chemistry tools and models  
**So that** I can quickly find candidates for a given goal

**Acceptance Criteria**:
- [ ] Registry schema: `configs/discovery_registry.schema.json`
  - Entry: ID, name, version, description, author, repo URL, paper/doi
  - Capability tags: "property_prediction", "molecular_dynamics", "quantum_chemistry", etc.
  - Inputs: data format (SMILES, SDF, molecular graph, etc.)
  - Outputs: predicted properties, confidence/uncertainty method
  - License: permissive/restrictive
  - Maturity: "alpha", "beta", "stable", "deprecated"
  - Provenance: Trust tier (0=internal validated, 1=peer-reviewed, 2=popular OSS, 3=candidate)
- [ ] Registry file: `configs/discovery_registry.json` with 10–20 seed entries (examples):
  - RDKit (molecular representations)
  - DeepChem (ML property prediction)
  - ASE (molecular dynamics)
  - PySCF (quantum chemistry)
  - OpenFF (force fields)
- [ ] Loader: `modules/04_method_discovery/registry_loader.py`
  - Load registry from JSON file
  - In-memory cache with invalidation (reload on file change)
- [ ] Tests:
  - Registry schema validates
  - Loader produces consistent results

**Definition of Done**:
- Registry is queryable by goal + chemistry domain
- 10+ seed entries with realistic metadata

**Effort**: M (3–4 days)  
**Owner**: TBD  
**Depends on**: Story 1.1

---

### Story 4.2: Implement Candidate Scoring Rubric

**As a** discovery engine  
**I want to** rank candidates by relevance, maturity, and risk  
**So that** researcher sees top-3 most promising tools

**Acceptance Criteria**:
- [ ] Scoring function: `modules/04_method_discovery/scorers.py`
  - Relevance: Keyword match (goal tags vs tool capability tags), 0–1
  - Maturity: Stars + citations + age, 0–1 (alpha=0.3, beta=0.6, stable=0.9, deprecated=0)
  - License: permissive=1, copyleft=0.5, restrictive=0
  - Trust tier: Tier 0=1, Tier 1=0.95, Tier 2=0.8, Tier 3=0.5
  - Reproducibility evidence: Has paper, has tests, has examples, 0–0.3 bonus
  - Composite score: weighted sum (relevance=0.4, maturity=0.2, license=0.15, trust=0.15, reproducibility=0.1)
  - Rank by composite score, show top-3
- [ ] Explainability: `modules/04_method_discovery/ranking_rationale.py`
  - Each ranked candidate shows component scores (e.g., "Relevance 0.8 | Maturity 0.7 | License 1.0")
  - Rationale: Why top candidate beat #2 (e.g., "3.5% higher relevance")
- [ ] Tests:
  - Score function is monotonic (more stars = higher score)
  - Composite score is bounded [0, 1]
  - Top-k ranking is stable (same candidates, same order)

**Definition of Done**:
- Top-3 candidates are explainably ranked
- Researcher can see why tool A beats tool B

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 4.1

---

### Story 4.3: Implement External Registry Adapters (PyPI, GitHub)

**As a** discovery engine  
**I want to** augment local registry with external sources (PyPI, GitHub, etc.)  
**So that** I can discover tools not in the seed registry

**Acceptance Criteria**:
- [ ] PyPI adapter: `modules/04_method_discovery/sources/pypi_adapter.py`
  - Query PyPI for packages matching goal tags (e.g., "deepchem", "quantum chemistry")
  - Extract: package name, latest version, description, license, GitHub URL, stars (via GitHub API)
  - Normalize to registry entry format
  - Cache results (1 day TTL)
- [ ] GitHub adapter: `modules/04_method_discovery/sources/github_adapter.py`
  - Query GitHub for repos matching goal tags
  - Extract: repo name, description, stars, topics, license, README
  - Normalize to registry entry
  - Cache (1 day TTL)
- [ ] Merge logic: `modules/04_method_discovery/registry_merger.py`
  - Union of local + external results
  - Deduplication (same tool from multiple sources)
  - External results default to trust tier 2 (popular OSS) unless vetted
  - Rate limit: Max 3 external queries per discovery session (to avoid excessive API calls)
- [ ] Tests:
  - PyPI query returns valid packages
  - GitHub query returns valid repos
  - Deduplication removes exact duplicates
  - Rate limiting enforced

**Definition of Done**:
- Discovery can find popular tools beyond seed registry
- External results are clearly marked (trust tier)
- API rate limits respected

**Effort**: L (1–2 weeks)  
**Owner**: TBD  
**Depends on**: Stories 4.1, 4.2

---

### Story 4.4: Implement Plan Synthesis with Risk & Cost Estimation

**As a** plan synthesizer  
**I want to** compose an executable workflow from the top-ranked candidate and estimated budget  
**So that** researcher knows what execution will cost and how long it will take

**Acceptance Criteria**:
- [ ] Plan composer: `modules/05_plan_synthesis/plan_synthesizer.py`
  - Input: Top-1 candidate (from discovery), RefinedIntentSpec, GoalGraph
  - Output: ExecutionPlan with:
    - Selected tool + version
    - Data pipeline (input format, preprocessing, output extraction)
    - Acceptance criteria (resolved to specific metrics and thresholds)
    - Resource request (estimated CPU, memory, timeout)
    - Cost estimate (LLM tokens for this run, compute time → $$)
    - Risk notes (any reproducibility concerns, uncommon chemistry domain, etc.)
- [ ] Cost estimation: `modules/05_plan_synthesis/cost_estimator.py`
  - LLM tokens: Count tokens in plan synthesis prompts + assume clarification (3 Q&A rounds avg) ≈ 5–10k tokens
  - Compute time: Look up tool metadata (if available), else fallback to 10 min
  - Cloud cost (if applicable): Estimated GCP Compute Engine cost for instance type
  - Display to researcher: "This run will cost ~1 token, take ~10 min, and cost ~$0.50"
- [ ] Risk assessment: `modules/05_plan_synthesis/risk_assessor.py`
  - Flags: Tool untested for this chemistry domain (e.g., using quantum solver for property prediction)
  - Flags: Outdated tool (>2 years without updates)
  - Flags: Low evidence of reproducibility (no paper, no tests)
  - Risk score: 0–1, aggregated
- [ ] Validation: `modules/05_plan_synthesis/plan_validator.py`
  - Check: Selected tool can ingest expected input format
  - Check: Expected output metrics match acceptance criteria
  - Check: Estimated cost <= researcher's per-run budget
  - Check: Estimated time <= wall-clock limit
  - Reject invalid plans with clear error message
- [ ] Tests:
  - Cost estimation is within 2x of actual (calibrate after first few runs)
  - Risk assessment detects known failure modes
  - Validation prevents infeasible plans

**Definition of Done**:
- ExecutionPlan is complete and feasible
- Researcher sees cost and time estimate
- Risk notes surface concerns

**Effort**: L (1–2 weeks)  
**Owner**: TBD  
**Depends on**: Stories 1.3, 4.2, 4.3

---

## Epic 5: Code Generation & Execution

**Goal**: Generate runnable code from plans and execute on local infrastructure.

**Effort**: 2 weeks (L)  
**Priority**: Critical Path (enables first end-to-end run)

---

### Story 5.1: Implement Code Generation for Chemistry Tools

**As a** code generator  
**I want to** emit a runnable Python script that loads data, runs the selected tool, and saves results  
**So that** orchestrator can execute it without manual intervention

**Acceptance Criteria**:
- [ ] Code template library: `modules/06_code_configuration_builder/templates/`
  - `template_property_prediction.py` — load SMILES, predict property, save CSV
  - `template_molecular_dynamics.py` — set up system, run MD, extract trajectories
  - `template_rdkit_manipulation.py` — molecular graph operations
  - Each template has:
    - Docstring explaining inputs/outputs
    - Inline comments for clarity
    - Placeholder vars for substitution (e.g., `{MODEL_NAME}`, `{INPUT_FILE}`)
    - Unit tests embedded as doctests
- [ ] Codegen engine: `modules/06_code_configuration_builder/codegen_engine.py`
  - Input: ExecutionPlan
  - Select template based on selected_method type
  - Substitute placeholders (tool name, input/output paths, hyperparameters, acceptance criteria)
  - Output: `RunBundle` with:
    - `main.py` (generated executable script)
    - `config.yaml` (parameters, paths, resource request)
    - `requirements.txt` (Python dependencies)
    - `inline_tests.py` (smoke tests)
- [ ] Dependency inference: `modules/06_code_configuration_builder/dependency_inferencer.py`
  - Given tool name, infer required packages (e.g., "deepchem" → `pip install deepchem`)
  - Pin versions for reproducibility
  - Check availability (package exists on PyPI)
- [ ] Inline tests: `modules/06_code_configuration_builder/smoke_test_generator.py`
  - Generate basic sanity checks (e.g., "import tool succeeds", "expected output file created")
  - Run before actual execution (catch dependency/syntax errors early)
- [ ] Tests:
  - Generated scripts are syntactically valid Python
  - Generated scripts run without errors on sample data
  - Inline tests detect missing dependencies

**Definition of Done**:
- RunBundle can be executed without manual edits
- Smoke tests pass before real execution

**Effort**: L (1–2 weeks)  
**Owner**: TBD  
**Depends on**: Story 1.3

---

### Story 5.2: Implement Local Execution Adapter

**As a** orchestrator  
**I want to** execute a RunBundle on the local machine (Linux/macOS/Windows)  
**So that** MVP doesn't require HPC infrastructure

**Acceptance Criteria**:
- [ ] Adapter: `modules/08_execution_adapter/local_adapter.py`
  - Input: RunBundle
  - Setup: Create temp directory, copy scripts, install dependencies (pip install -r requirements.txt)
  - Execute: Run `python main.py` as subprocess
  - Capture: STDOUT, STDERR, exit code, execution time, peak memory (via `psutil`)
  - Cleanup: Remove temp files (optional --keep-artifacts flag for debugging)
  - Output: ExecutionResult with logs, artifacts path, exit code
- [ ] Dependency installation: `modules/08_execution_adapter/dependency_installer.py`
  - Install from requirements.txt in temporary virtual environment
  - Timeout: 5 min (fail if pip takes too long)
  - Retry on network error (exponential backoff)
- [ ] Resource monitoring: `modules/08_execution_adapter/resource_monitor.py`
  - Poll process every 5s: CPU%, memory MB, open files
  - Record peak values
  - Flag anomalies: "Memory usage exceeded 1GB" → potential OOM risk
  - Graceful shutdown: Send SIGTERM, wait 10s, SIGKILL if needed
- [ ] Tests:
  - Execute simple Python script, capture output
  - Handle timeout gracefully
  - Handle missing dependency (clear error)
  - Resource monitoring accuracy

**Definition of Done**:
- RunBundles execute successfully on local machine
- Logs and metrics captured for observability
- Timeouts respected

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 5.1

---

### Story 5.3: Implement Session & Event Loop Management

**As a** orchestrator  
**I want to** coordinate the lifecycle of a complete run (intake → execution → validation)  
**So that** all stages happen in the correct order and no intermediate state is lost

**Acceptance Criteria**:
- [ ] RunSession coordinator: `modules/07_runtime_orchestrator/orchestrator.py`
  - Implement state machine from Story 2.1
  - Each state transition:
    - Publish event to event bus (Story 2.2)
    - Persist session to store (Story 2.5)
    - Invoke corresponding agent module
  - Handle errors: If agent fails, log error, transition to ERROR state, notify researcher
  - Checkpoints: Save session every stage, enable resume from last good state
- [ ] Agent invocation: `modules/07_runtime_orchestrator/agent_runner.py`
  - Generic runner: `run_agent(agent_module, input_spec) → output_spec`
  - Timeout per agent (e.g., EXECUTE = 20 min, CLARIFY = 5 min)
  - Validation: Output matches expected schema
  - Retry: Transient errors (network) get automatic retry; permanent errors fail-fast
- [ ] Error handling: `modules/07_runtime_orchestrator/error_handler.py`
  - Classify errors: code/config/resource/timeout/policy/llm
  - Actionable error messages to researcher
  - Fallback strategies (e.g., if discovery fails, ask researcher to manually specify tool)
- [ ] Tests:
  - Complete happy-path flow: intake → execute → result (with mocked agents)
  - Resume from checkpoint is deterministic
  - Error handling prevents infinite loops

**Definition of Done**:
- End-to-end flow is coordinated and resilient
- Sessions are resumable
- Errors are clear and actionable

**Effort**: L (1–2 weeks)  
**Owner**: TBD  
**Depends on**: Stories 2.1, 2.2, 2.5

---

## Epic 6: Validation & Self-Correction

**Goal**: Interpret results, validate against baselines, and propose iterative improvements.

**Effort**: 2 weeks (L)  
**Priority**: High (enables learning loop)

---

### Story 6.1: Implement Result Parsing & Metric Extraction

**As a** result interpreter  
**I want to** extract numerical results from tool outputs (files, logs, stdout)  
**So that** I can compare against baselines programmatically

**Acceptance Criteria**:
- [ ] Output parsers: `modules/10_result_interpreter/extractors/`
  - CSV/TSV parser (most common): read file, extract column by name, return values + metadata
  - JSON parser: extract nested field, flatten if needed
  - Log parser: grep for pattern, extract numeric value
  - Tool-specific parsers: e.g., RDKit output → dict of properties
  - Pluggable: add parser for new tool without changing core logic
- [ ] Metric normalization: `modules/10_result_interpreter/metric_normalizer.py`
  - Tool outputs vary: some produce CSV, some JSON, some print to stdout
  - Normalize to ResultPackage: primary_metric, secondary_metrics, metadata
  - Include units and uncertainty (if available)
- [ ] Confidence assignment: `modules/10_result_interpreter/confidence_estimator.py`
  - If tool reports uncertainty (std dev, 95% CI), use that
  - Else, estimate from convergence behavior (e.g., trend in loss over epochs)
  - Fallback: assume 10% uncertainty
- [ ] Tests:
  - Parse diverse output formats correctly
  - Normalized metrics are consistent across tools
  - Uncertainty estimates are reasonable

**Definition of Done**:
- ResultPackage correctly represents tool output
- Metrics are comparable across tools

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 1.4

---

### Story 6.2: Implement Cross-Validation Against Baselines

**As a** validator  
**I want to** compare predicted values against known literature results  
**So that** I can quantify agreement and detect systematic errors

**Acceptance Criteria**:
- [ ] Baseline database: `configs/baselines.json`
  - Format: {molecule, property, literature_value, literature_source, doi}
  - Seed data: 10–20 benchmark molecules with measured solubility/toxicity (e.g., from TPSA dataset, FDA compounds)
  - Versioned and immutable (snapshots)
- [ ] Comparison logic: `modules/11_cross_validation/baseline_validator.py`
  - For each prediction in ResultPackage, look up literature baseline
  - Compute agreement metrics:
    - Absolute error: |predicted - literature|
    - Relative error: |predicted - literature| / |literature| (if literature != 0)
    - Pearson correlation (if >1 molecule)
    - RMSE (if >1 molecule)
  - Generate ValidationReport with gap analysis
- [ ] Acceptance verdict: `modules/11_cross_validation/acceptance_judge.py`
  - Rule: If relative error < 15% for all molecules, ACCEPT
  - Else if relative error < 30%, NEEDS_REVIEW (marginal)
  - Else REJECT (poor agreement)
  - Configurable thresholds (researcher can adjust)
- [ ] Tests:
  - Compare against known reference values, verify metrics
  - Acceptance verdict matches manual inspection

**Definition of Done**:
- ValidationReport quantifies agreement vs baselines
- Acceptance verdict is objective and explainable

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Stories 1.4, 6.1

---

### Story 6.3: Implement Self-Correction Reflection

**As a** self-correction module  
**I want to** diagnose why validation failed and propose specific improvements  
**So that** the system can auto-correct without researcher intervention

**Acceptance Criteria**:
- [ ] Failure diagnosis: `modules/12_self_correction_reflection/failure_classifier.py`
  - Classify failure mode: input quality (bad SMILES), model mismatch (wrong model for task), hyperparameter (learning rate too high), data distribution (test set out-of-distribution)
  - For each mode, diagnostic signal (e.g., if model mismatch: activation magnitudes are too high; if hyperparameter: loss plateaus early)
  - Confidence: 0–1 (higher = more certain about diagnosis)
- [ ] Correction strategies: `modules/12_self_correction_reflection/strategies/`
  - If input quality: validate SMILES, sanitize, retry
  - If model mismatch: try next-best candidate (try #2 from discovery), rebuild plan, rerun
  - If hyperparameter: grid search (try 3 lr values), report best
  - If data distribution: ask researcher for more training data, or relax acceptance criteria
  - Each strategy produces a CorrectionPlan with expected improvement forecast
- [ ] Bounded iterations: `modules/12_self_correction_reflection/rerun_controller.py`
  - Max iterations: 5 by default (configurable, budget-constrained by Story 2.4)
  - Convergence check: If improvement < 5% vs last iteration, stop and ask researcher
  - Cost-benefit: Only rerun if expected improvement > estimated cost (LLM + compute)
- [ ] Tests:
  - Failure classification matches manual diagnosis (>80% accuracy on synthetic cases)
  - Correction strategies improve metrics or surface actionable feedback
  - Bounded iterations prevent infinite loops

**Definition of Done**:
- Self-correction proposes actionable improvements
- System doesn't rerun indefinitely
- Researcher sees improvement forecast before rerun approval

**Effort**: L (1–2 weeks)  
**Owner**: TBD  
**Depends on**: Stories 6.1, 6.2

---

## Epic 7: Provenance, Safety, & Governance

**Goal**: Record immutable audit trail and enforce policy constraints.

**Effort**: 2 weeks (L)  
**Priority**: High (compliance + reproducibility)

---

### Story 7.1: Implement Provenance Event Log

**As a** audit system  
**I want to** record every decision, prompt, code, and result in immutable form  
**So that** any historical run can be replayed deterministically

**Acceptance Criteria**:
- [ ] Event model: `modules/14_provenance_memory/event_log.py`
  - Schema: ProvenanceEvent (Story 1.5)
  - Immutable append-only log (SQLite + JSON backup)
  - Per-session event stream: all events for one researcher request
  - Index: by session_id, timestamp, event_type for querying
- [ ] Event capture: Hook into all major transitions
  - REQUEST_RECEIVED: Timestamp, researcher ID, NL request text
  - INTENT_PARSED: IntentSpec snapshot
  - PLAN_SYNTHESIZED: ExecutionPlan snapshot
  - EXECUTION_STARTED, EXECUTION_COMPLETED: Timing, exit code, artifacts path
  - VALIDATION_COMPLETED: ValidationReport snapshot
  - DECISION_MADE: Researcher approval/rejection + rationale
- [ ] Replay engine: `modules/14_provenance_memory/replay.py`
  - Load session from event log
  - Replay all deterministic steps (intent parsing, planning, validation)
  - Compare replay outputs vs original outputs (checksums)
  - Flag differences (indicates non-determinism or data drift)
- [ ] Storage: `modules/14_provenance_memory/store.py`
  - Local: SQLite at `~/.twain/sessions.db`
  - Remote (optional): Export events to cloud storage (GCS, S3) for archival
  - Backup: Daily export of event logs to JSON files (shareable with collaborators)
- [ ] Tests:
  - Events roundtrip (serialize/deserialize) without loss
  - Replay from events produces identical artifacts (deterministic)
  - Query by session_id returns correct events

**Definition of Done**:
- Full audit trail for every run
- Replay is deterministic
- Provenance survives data corruption

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 1.5

---

### Story 7.2: Implement Policy Enforcement

**As a** safety officer  
**I want to** prevent discovery/execution of unsafe or unapproved tools  
**So that** the system complies with institutional policy

**Acceptance Criteria**:
- [ ] Policy engine: `modules/15_policy_safety_governance/policy_engine.py`
  - Policy rules (YAML config): `configs/policies.yaml`
  - Rules:
    - License check: Only permissive licenses (MIT, Apache 2.0, BSD) — block GPL/AGPL
    - Tool allow-list: Curated list of approved tools; discovery candidates not on list require researcher approval
    - Compute budget: Max cost per run (e.g., $10), per researcher per month (e.g., $500)
    - Tool version constraints: Minimum version stability (e.g., no alpha/beta)
  - Enforcement points: After discovery (filter candidates), after planning (check cost), before execution (verify tool is approved)
  - Actions: APPROVED, NEEDS_REVIEW (flag for researcher), BLOCKED (fail with reason)
- [ ] License checker: `modules/15_policy_safety_governance/license_checker.py`
  - Query SPDX license database
  - Classify license: permissive, copyleft, proprietary, unknown
  - Block proprietary/unknown unless researcher explicitly approves
- [ ] Cost tracker: `modules/15_policy_safety_governance/cost_tracker.py`
  - Track cumulative cost per researcher per month
  - Reject request if would exceed budget
  - Notify researcher: "You've used $X this month; this run costs $Y (X+Y would exceed $Z limit)"
- [ ] Audit log: All policy decisions logged to provenance
- [ ] Tests:
  - Blocked tools don't reach execution
  - Costs are tracked accurately
  - Policy config is validated on startup

**Definition of Done**:
- Policy is enforced pre-execution
- Researcher sees policy flags and can override (with audit trail)

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Story 7.1

---

### Story 7.3: Implement Trust Scoring & Source Validation

**As a** discovery module  
**I want to** assign trust scores to discovered tools and data sources  
**So that** researcher knows how much to rely on a result

**Acceptance Criteria**:
- [ ] Trust model: `modules/15_policy_safety_governance/trust_scorer.py`
  - Factors:
    - Source authority (peer-reviewed paper > popular GitHub > unknown repo)
    - Tool maturity (stable > beta > alpha)
    - Community adoption (stars, downloads, citations)
    - Governance (actively maintained, responsive to issues)
  - Score: 0–1 (0.8–1.0 = trusted, 0.5–0.8 = caution, <0.5 = high risk)
  - Composable: Trust score is added to every discovered candidate and execution artifact
- [ ] Validation rules: `modules/15_policy_safety_governance/validation_rules.py`
  - For discovered tools: Attach trust score to CandidateMethodSet
  - For inputs (SMILES, SDF): Validate format and sanitize (remove potentially dangerous characters)
  - For outputs: Check for anomalies (impossibly low/high values, corrupted file format)
- [ ] Researcher transparency: ValidationReport includes trust score of tool used
  - "This result was computed with RDKit (trust=0.95 / stable, widely cited) …"
  - Allows researcher to contextualize confidence in result
- [ ] Tests:
  - Trust scores correlate with actual tool quality (>0.7 Spearman correlation on held-out benchmarks)
  - Input validation catches malformed data

**Definition of Done**:
- Every tool and result includes transparency about source trust
- Researcher can make informed decisions

**Effort**: M (3–4 days)  
**Owner**: TBD  
**Depends on**: Stories 4.1, 6.1

---

## Epic 8: MVP Chemistry Pilot

**Goal**: Validate the entire pipeline with a real chemistry task and public benchmarks.

**Effort**: 2 weeks (L)  
**Priority**: High (validates all above epics)

---

### Story 8.1: Define Chemistry Benchmark Task

**As a** project lead  
**I want to** select a molecular property prediction task with public data and baselines  
**So that** we can validate the entire pipeline end-to-end

**Acceptance Criteria**:
- [ ] Task selection: Molecular solubility prediction (SMILES → predicted log solubility)
  - Rationale: Abundant public data (SMILES databases), fast to compute (no expensive QM), easy to validate (literature benchmarks available)
  - Data source: Curated SMILES + solubility from ChEMBL, PubChem, or FDA dataset
  - Baseline model: RDKit + simple property-based predictor (first milestone) OR transfer learning from pre-trained model (stretch)
  - Benchmark set: 20 molecules with measured solubility (from literature or reputable database)
- [ ] Baseline performance: Run reference implementation (e.g., RDKit TPSA correlation with solubility), measure accuracy vs benchmark
  - Target baseline: MSE ~0.5 (log solubility units)
  - Expected vs literature: "Similar models report MSE 0.3–0.6 (Smith et al. 2020)"
- [ ] Acceptance criteria template:
  - "Predicted solubility must match literature baselines within MSE < 0.5"
  - "Uncertainty estimates must have >80% coverage (actual error within 95% CI)"
- [ ] Documentation: `docs/chemistry_pilot/solubility_task_definition.md` with:
  - Data description, baseline model, expected performance, literature references, validation protocol
  - Researcher-facing plain English: "Predict solubility of aspirin (and 19 other molecules) using RDKit features + ML"

**Definition of Done**:
- Task is well-defined and scoped
- Baseline is established
- Benchmark data is curated and versioned

**Effort**: M (3–4 days)  
**Owner**: TBD  
**Depends on**: None (parallel to other epics)

---

### Story 8.2: Implement Solubility Prediction Tool Integration

**As a** code generator  
**I want to** generate code that can execute solubility prediction using RDKit + scikit-learn  
**So that** the orchestrator can run the chemistry benchmark

**Acceptance Criteria**:
- [ ] Tool wrapper: `modules/04_method_discovery/tools/rdkit_solubility_predictor.py`
  - Input: CSV with SMILES column
  - Process:
    1. Sanitize SMILES (validate with RDKit)
    2. Extract features (TPSA, molecular weight, LogP, etc.)
    3. Load pre-trained sklearn model (or simple linear regression on benchmark set)
    4. Predict + output uncertainty
  - Output: CSV with predicted_solubility, uncertainty_std columns
  - Error handling: Invalid SMILES → skip with warning
- [ ] Code template: `modules/06_code_configuration_builder/templates/template_rdkit_solubility.py`
  - Parameterized: Input file, output file, model path, feature list
  - Inline tests: "import rdkit succeeds", "sample SMILES prediction works"
- [ ] Model artifact: Pre-trained model serialized and versioned
  - `experiments/baselines/solubility_model_v1.pkl`
  - Training data: 100 molecules from ChEMBL with measured solubility
  - Document: Training methodology, feature engineering, cross-validation results
- [ ] Tests:
  - Prediction on benchmark set: Verify MSE < 0.7 (stretch: < 0.5)
  - Uncertainty estimates are reasonable (coverage > 80%)

**Definition of Done**:
- Solubility prediction tool is integrated and benchmarked
- Code generation produces runnable scripts

**Effort**: M (4–5 days)  
**Owner**: TBD  
**Depends on**: Stories 4.1, 5.1, 8.1

---

### Story 8.3: End-to-End Integration Test (Mock Researcher Interaction)

**As a** integration tester  
**I want to** run a complete pipeline from a researcher's natural language request through final approval  
**So that** we can validate the entire system works together

**Acceptance Criteria**:
- [ ] Test scenario: Researcher requests:
  - "Predict the water solubility of aspirin at 25°C"
  - System flow:
    1. Intake NLU parses into IntentSpec (objective: solubility prediction, compound: aspirin, constraint: aqueous, 25°C)
    2. Clarification Q&A (confirm molecule identity, ask for additional molecules if researcher wants batch prediction)
    3. Goal decomposer produces GoalGraph (get SMILES → validate SMILES → predict solubility → validate vs baselines)
    4. Method discovery finds RDKit+ML as top candidate
    5. Plan synthesizer creates ExecutionPlan with cost/time estimate
    6. Researcher approves plan (via CLI)
    7. Code generator creates main.py + requirements.txt
    8. Local executor runs main.py, captures output
    9. Result interpreter parses CSV output
    10. Cross-validator compares vs literature baseline
    11. Validation passes (within MSE threshold) → ACCEPT
    12. Researcher reviews final result and approves publication
- [ ] Mocking: All LLM calls return deterministic responses (seeded); external APIs return fixture data
- [ ] Assertions:
  - Each stage completes with no errors
  - Session state transitions are correct
  - Final result is reproducible (rerun with same input produces same output)
  - Provenance log contains all events
- [ ] Timing: E2E test completes in <1 min (no network delays, mocked LLM latency)
- [ ] Tests: `tests/integration/test_e2e_solubility_pipeline.py`

**Definition of Done**:
- E2E flow is end-to-end functional
- All modules integrate without error
- Reproducibility verified

**Effort**: L (1–2 weeks)  
**Owner**: TBD  
**Depends on**: All prior epics

---

### Story 8.4: MVP Demo & Feedback Iteration

**As a** project lead  
**I want to** demonstrate the system to chemistry researchers and collect feedback  
**So that** we can prioritize refinements for Phase 2

**Acceptance Criteria**:
- [ ] Demo setup:
  - Live walkthrough of solubility prediction pipeline
  - Researcher submits NL request, watches system discover, plan, execute, validate
  - Expected duration: 10–15 min (with mocked LLM latency added for realism)
  - Fallback: Pre-recorded video if live demo infrastructure unavailable
- [ ] Feedback collection:
  - Survey: Clarity of NL parsing, utility of discovered tools, quality of explanations, ease of approval workflow
  - 5-point scale per aspect
  - Open-ended: What's missing? What was confusing? What would you change?
- [ ] Feedback synthesis: `docs/feedback/phase_1_demo_synthesis.md`
  - High-priority gaps (block Phase 2)
  - Nice-to-haves (backlog for Phase 3)
- [ ] Iterate:
  - Fix any critical bugs discovered during demo
  - Update backlog priorities based on feedback

**Definition of Done**:
- Demo runs successfully
- Feedback is synthesized and prioritized
- Backlog is updated

**Effort**: M (3–4 days)  
**Owner**: TBD  
**Depends on**: Story 8.3

---

## Stretch Goals (Post-MVP)

These are valuable but not critical for MVP validation.

### S1: Web UI for Approval & Result Review
- Replace CLI with simple web interface (FastAPI + React)
- Show plan visually, approve/reject/edit buttons
- Display results with interactive plots (D3/Plotly)

### S2: Multi-Tool Composition (Pipeline Chaining)
- Some chemistry workflows need sequential tools (e.g., structure preparation → property prediction → uncertainty estimation)
- Extend plan synthesizer to chain tools

### S3: Batch Prediction
- Researcher: "Predict solubility for this CSV of 1000 molecules"
- System: Chunk into parallel batch, aggregate results, validate

### S4: Correction Loop Auto-Activation
- Instead of asking researcher to approve each correction, auto-trigger up to 3 corrections if improvement is >10%
- Notify researcher after converging or hitting iteration limit

### S5: Literature Integration
- When displaying results, automatically cite relevant papers from the baseline database
- Link to PubMed / arXiv / DOI

---

## Summary Table: Effort & Dependencies

| Epic | Stories | Effort | Critical Path | Depends On |
|------|---------|--------|----------------|-----------|
| 1. Contracts | 1.1–1.6 | 2w (M+L) | **YES** | None |
| 2. Control Plane | 2.1–2.5 | 2w (L) | **YES** | Epic 1 |
| 3. Researcher UX | 3.1–3.3 | 2w (L) | YES | Epics 1, 2 |
| 4. Discovery & Planning | 4.1–4.4 | 2.5w (L) | **YES** | Epics 1, 2, 3 |
| 5. Code Gen & Execution | 5.1–5.3 | 2w (L) | **YES** | Epics 1, 2, 4 |
| 6. Validation & Correction | 6.1–6.3 | 2w (L) | YES | Epics 1, 5 |
| 7. Provenance & Governance | 7.1–7.3 | 2w (L) | YES | Epics 1, 2, 5, 6 |
| 8. Chemistry Pilot | 8.1–8.4 | 2w (L) | YES | Epics 1–7 |
| **Total** | **~40 stories** | **~16–18 weeks (1 FTE)** | — | — |

**Fast-track (2 FTE in parallel)**: 8–10 weeks

---

## Metrics & Success Criteria

Track throughout implementation:

1. **Coverage**: % of module interfaces with implementations
2. **Test Coverage**: % code lines covered by unit/integration tests (target: >80%)
3. **Contract Validation**: % of inter-module interactions validating against schemas (target: 100%)
4. **Reproducibility**: % of historical runs that replay identically (target: 100%)
5. **E2E Latency**: Time from request to result (target: <5 min per iteration)
6. **Chemistry Accuracy**: Solubility prediction MSE on benchmark vs literature baseline (target: <0.5)

---

## Next Steps

1. **Prioritize and staff** Epic 1 stories (schemas) — unlock parallel work on other epics
2. **Draft technical design** for control plane (Epic 2) — finalize architecture decisions
3. **Prototype** Intake NLU (Story 4.1) — validate LLM prompting strategy
4. **Set up** CI/CD pipeline, testing infra, documentation site
5. **Schedule** weekly sync to review progress and unblock dependencies

