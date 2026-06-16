# TWAIN Architecture Conformance & Migration Guide

**Status**: Active — `feature/architecture` is the governing blueprint ("source of truth")
**Purpose**: Map all existing work on topic branches onto the 16-module architecture, and
spell out what must change to conform. Use this as the running checklist while we refactor.

> Decision (2026-06-16): The architecture defined in
> [`domain-neutral-agentic-pipeline.md`](domain-neutral-agentic-pipeline.md) is now the rule of
> thumb. Existing branches were built independently and will be refactored to conform.

---

## 1. The core problem

`feature/architecture` is an **orphan branch** (no shared git history with `master` or the topic
branches). It holds the design docs and an empty `modules/01..16` skeleton, but **no code**. All
real code lives on topic branches that were built *before* this blueprint was adopted, so they do
not follow its module layout or its data contracts.

Two conventions and two contract families are currently in conflict and must be unified:

- **Directory layout**: `modules/NN_name/` (this branch) vs. flat per-concern folders
  (`intake/`, `plan_synthesizer/`, `Intelligence Layer/`, `Job Monitoring/`, `Benchmark/`).
- **"Intent" contract** (most important): there are **two incompatible intent schemas**.
  - `Schema` branch `IntentSpec` = `objective` / `domain` / `system_descriptors` /
    `acceptance_metrics` / `metadata` (matches this architecture's Story 1.1).
  - `master` / `Benchmark` / `ExecutionLog` / `SemanticParsing` `IntentSpecification` =
    `userConstraints` + `query.chemical` / `query.ase` (the Pymatgen/ASE domain payload).
  These are not versions of the same thing — they live at different layers (see §4).

---

## 2. Branch → architecture module map

| Topic branch | What it contains | Target module(s) | Conformance status |
|---|---|---|---|
| `SpeechRecognition` | `SpeechRecognition.py` voice capture | front-end of **01 Intake NLU** | Needs relocation + IntentSpec output |
| `SemanticParsing` | `Intelligence Layer/` FSM `Prompter`, `AgentInterface` (MS OAuth LLM), `PromptCompiler`, prompts | **01 Intake NLU** + **02 Clarification** (+ a gate that is really **13 Human-in-the-loop**) | Mixed concerns; emits engine fragments, not IntentSpec |
| `Schema` | `schemas/*.schema.json`, `intake/intent_spec.py`, `plan_synthesizer/execution_plan.py` + `plan_validator.py`, `tests/unit/` | **Epic 1 contracts** (01 IntentSpec, 05 ExecutionPlan, 10 ResultPackage, 11 ValidationReport) | Closest to blueprint; incomplete + narrow |
| `Benchmark` | `run_benchmark.py` (ASE/pymatgen via EMT), references, structures, results | **08 Execution Adapter** + **10 Result Interpreter** + **11 Cross-Validation** + Epic 8 pilot | Consumes the *old* `IntentSpecification`; cross-engine check is reusable |
| `ExecutionLog` | `Job Monitoring/ExecutionLog.py` (SQLite jobs), `ProvenanceStore.py` | **09 Observability Monitor** + **14 Provenance Memory** | Good fit; relocate + align to ProvenanceEvent contract |
| `WorkflowManagers` | (empty) | **07 Runtime Orchestrator** / **16 Control Plane** | Greenfield — build to spec |
| `master` | `Schema/`, `docs/`, `pixi.toml`/`pixi.lock` | env + domain schemas | `pixi` env management should be adopted here |

---

## 3. What must change, per area

### 3.1 Epic 1 contracts (`Schema` branch) — closest, but not conformant yet
- `intent_spec.schema.json` `domain` enum is only `["materials"]`; blueprint wants
  `molecular_chemistry | materials | quantum | reactions`. The Python `Domain` enum already lists
  `MATERIALS`/`QUANTUM` — schema and dataclass disagree. Unify them.
- `system_descriptors` hard-requires `formula` + `molecule.SMILES`; blueprint treats these as
  optional descriptors. Loosen to match.
- `plan_validator.PlanValidator.validate_plan()` is an empty stub — Story 1.3 requires real
  feasibility checks (resource availability, cost ≤ budget).
- Missing Story 1.5/1.6 contracts: `CorrectionPlan`, `ProvenanceEvent`, and the versioning policy.
- Action: finish + lock Epic 1 here first; it is the critical path that unblocks everything.

### 3.2 Two intent schemas — the central reconciliation
- Keep the `Schema` branch `IntentSpec` as the **high-level parsed research intent** (output of 01).
- Reclassify the `userConstraints` + `query.chemical/query.ase` document (Pymatgen/ASE) as a
  **lower-layer payload** — it belongs around **06 Code/Config Builder → 08 Execution** as the
  engine-specific `RunBundle`, not as the top-level intent.
- The pipeline becomes: `IntentSpec` (01) → … → `ExecutionPlan` (05) → engine payload
  (`query.chemical`/`query.ase`) generated at 06 → executed at 08.
- This resolves the conflict instead of forcing one schema to win.

### 3.3 Semantic parsing (`SemanticParsing`) — split by concern
- The `Prompter` FSM blends three architecture modules: extraction (01), follow-up Q&A (02), and
  the YES/NO subject confirmation gate (13 Human-in-the-loop). Split accordingly.
- It currently emits per-engine fragments (`{"pymatgen": {...}}`/`{"ase": {...}}`) into `data.json`.
  Under the blueprint, 01/02 must emit **`IntentSpec`/`RefinedIntentSpec`**; engine fragments move
  downstream to plan/codegen.
- `AgentInterface` (MS OAuth LLM client) is shared infra — promote to `interfaces/` rather than
  living inside one module.

### 3.4 Execution + benchmarking (`Benchmark`)
- `run_benchmark.py` validates against the monolithic `Schema.json` (the file we already decided to
  delete) and reads `query.chemical`/`query.ase`. Repoint it at the new contracts once §3.2 lands.
- The ASE↔pymatgen cross-engine EMT agreement check is genuinely reusable as **11 Cross-Validation**
  logic and as the Epic 8 pilot harness — keep it, relocate it.
- Note the pilot drift: blueprint recommends **molecular solubility**; current benchmark does
  H2O/Cu/graphite energetics. Decide which is the real first pilot (record as an ADR).

### 3.5 Observability + provenance (`ExecutionLog`)
- `ExecutionLog.py` (SQLite `jobs`: status/timestamps) → **09**; `ProvenanceStore.py`
  (`assumptions`/`decisions`) → **14**. Both are solid fits.
- Align table columns to the not-yet-defined `ProvenanceEvent` schema (Story 1.5) so capture is
  contract-driven. This also confirms the implicit decision: **SQLite is the provenance store**.

### 3.6 Cross-cutting
- Adopt `pixi` (already used on `master`/most branches) on the architecture branch for env mgmt.
- The architecture branch shares no history with the others; pick a reconciliation path (see §6).

---

## 4. Layering cheat-sheet (resolves "which schema is which")

```
NL / voice ─▶ IntentSpec (01)            ← Schema-branch IntentSpec (objective/domain/...)
           ─▶ RefinedIntentSpec (02)
           ─▶ GoalGraph (03)
           ─▶ CandidateMethodSet (04)
           ─▶ ExecutionPlan (05)         ← Schema-branch execution_plan.schema.json
           ─▶ RunBundle (06)             ← query.chemical / query.ase (Pymatgen/ASE payload)
           ─▶ ExecutionEvents (08)       ← Benchmark runner + ExecutionLog jobs
           ─▶ ResultPackage (10) / ValidationReport (11)  ← Benchmark cross-engine check
   provenance of every step (14)         ← ProvenanceStore
```

---

## 5. Open decisions to record as ADRs (`docs/decisions/` is empty)

Several were already decided *implicitly* by existing code — make them explicit:
1. LLM provider = MS-OAuth-fronted agent API (per `AgentInterface`). Confirm + document fallback.
2. Provenance/observability store = SQLite (per `ExecutionLog`/`ProvenanceStore`).
3. Env management = `pixi` (per `master`).
4. First pilot domain = solubility (blueprint) vs. energetics (current benchmark) — pick one.
5. Orchestrator framework (custom state machine recommended) — still open.
6. External tool-discovery trust policy — still open.

---

## 6. Recommended sequence

1. **Reconcile histories**: decide whether topic branches merge into `feature/architecture`
   (requires `--allow-unrelated-histories`) or whether these docs move into `master`. Until this is
   chosen, nothing can be integrated.
2. **Lock the layout**: choose one convention. Recommended — keep the per-concern folders that code
   already uses and update the blueprint's `modules/NN_*` references to match, rather than moving
   working code into numbered folders.
3. **Finish + freeze Epic 1 contracts** on `Schema` (§3.1), including the §3.2 layering split.
4. **Repoint consumers** (`Benchmark`, `SemanticParsing`, `ExecutionLog`) at the frozen contracts.
5. **Relocate** each branch's code to its target module and delete the obsolete monolithic
   `Schema.json`.
6. **Write the ADRs** in §5 as the changes land.

---

**Created**: 2026-06-16 · **Owner**: TBD · **Tracks**: Epics 1–8 in `docs/backlog/DETAILED_BACKLOG.md`
