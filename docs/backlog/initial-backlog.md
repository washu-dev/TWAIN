# TWAIN Initial Backlog (Seed)

## Epic 1: Contract-First Foundation

1. Define JSON Schemas for `IntentSpec`, `GoalGraph`, `ExecutionPlan`, `ValidationReport`, `CorrectionPlan`.
2. Add schema versioning policy and migration rules.
3. Create contract validation test suite.

## Epic 2: Control Plane And Orchestration

1. Implement agent state machine and transition guards.
2. Add retry, timeout, and budget policies.
3. Add event bus for inter-agent messaging.

## Epic 3: Researcher-In-The-Loop UX

1. Build approval checkpoints (plan, rerun, final acceptance).
2. Build clarification loop with confidence threshold.
3. Add request-edit and override workflow.

## Epic 4: Discovery And Planning

1. Implement method discovery registry adapter interface.
2. Implement candidate scoring rubric with explainability.
3. Implement plan synthesis with risk and cost estimates.

## Epic 5: Code Generation And Execution

1. Build run bundle generator (scripts + config + manifests).
2. Add local execution adapter and session monitor.
3. Add standardized runtime diagnostics capture.

## Epic 6: Validation And Self-Correction

1. Implement result normalization and KPI extraction.
2. Implement cross-validation against baseline and prior runs.
3. Implement reflection planner and bounded iterative rerun loop.

## Epic 7: Provenance, Safety, Governance

1. Define provenance data model and immutable event capture.
2. Add policy enforcement hooks pre-plan and pre-execution.
3. Add license and source trust checks for discovered methods.

## Epic 8: MVP Chemistry Pilot

1. Select first publicly available chemistry model/toolchain for pilot.
2. Build one complete end-to-end pipeline with reproducible demo.
3. Benchmark against at least one prior literature/reference workflow.

## Candidate Story Format

Use this template when expanding stories:

- As a researcher, I want ...
- Acceptance criteria:
  - Given ...
  - When ...
  - Then ...
- Telemetry:
  - Success metrics captured
  - Failure signals captured
