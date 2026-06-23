# TWAIN Pipeline Flow & Module Interaction Diagram

## Complete System Dataflow

```mermaid
graph TD
    subgraph "Researcher Interface"
        A["🧪 Researcher<br/>Natural Language Request"]
    end

    subgraph "01_Intake & Clarification"
        B["01_Intake_NLU<br/>Parse Request→IntentSpec"]
        C["02_Clarification_Dialogue<br/>Q&A until Confident"]
    end

    subgraph "02_Planning Phase"
        D["03_Goal_Decomposer<br/>Break into SubGoals→GoalGraph"]
        E["04_Method_Discovery<br/>Search Registry & External→CandidateSet"]
        F["05_Plan_Synthesis<br/>Select Tool→ExecutionPlan"]
    end

    subgraph "03_Approval Gate"
        G["13_Human_In_The_Loop<br/>Show Plan to Researcher"]
        H["16_Agent_Mesh_Control_Plane<br/>State Machine, Retry Logic"]
    end

    subgraph "04_Code Generation"
        I["06_Code_Config_Builder<br/>Generate Scripts→RunBundle"]
        J["15_Policy_Safety<br/>License & Policy Check"]
    end

    subgraph "05_Execution"
        K["07_Runtime_Orchestrator<br/>Coordinate Lifecycle"]
        L["08_Execution_Adapter<br/>Run Code (Local/HPC)"]
        M["09_Observability_Monitor<br/>Capture Logs & Metrics"]
    end

    subgraph "06_Validation & Interpretation"
        N["10_Result_Interpreter<br/>Parse Output→ResultPackage"]
        O["11_Cross_Validation<br/>vs Baselines→ValidationReport"]
    end

    subgraph "07_Self-Correction Loop"
        P["12_Self_Correction<br/>Diagnose & Propose Fixes"]
        Q["Decision: Accept or Rerun"]
    end

    subgraph "08_Final Review & Publishing"
        R["13_Human_In_The_Loop<br/>Final Approval"]
        S["14_Provenance_Memory<br/>Archive Run Record"]
        T["✅ Final Artifacts<br/>Shared with Collaborators"]
    end

    subgraph "Cross-Cutting Concerns"
        U["14_Provenance_Memory<br/>Immutable Event Log"]
        V["16_Control_Plane<br/>Timeouts, Budgets, Retries"]
        W["15_Policy_Governance<br/>License Enforcement"]
    end

    %% Main Flow
    A --> B
    B --> C
    C --> D
    D --> E
    E --> F
    F --> G
    G -->|Approved| I
    G -->|Rejected| D
    G -->|Edited| F
    I --> J
    J -->|Blocked| G
    J -->|Approved| K
    K --> L
    L --> M
    M --> N
    N --> O
    O --> Q
    Q -->|Accept| R
    Q -->|Rerun| P
    P -->|Minor Fixes| F
    P -->|Major Changes| D
    R -->|Approved| S
    R -->|Override| S
    S --> T

    %% Cross-Cutting
    B -.->|Event Log| U
    C -.->|Event Log| U
    D -.->|Event Log| U
    E -.->|Event Log| U
    F -.->|Event Log| U
    K -.->|State Machine| V
    L -.->|Timeout| V
    I -.->|Check| W
    E -.->|Trust Score| W

    %% Styling
    classDef researcher fill:#e1f5ff,stroke:#01579b,stroke-width:2px
    classDef agent fill:#f3e5f5,stroke:#4a148c,stroke-width:2px
    classDef approval fill:#fff3e0,stroke:#e65100,stroke-width:2px
    classDef support fill:#e8f5e9,stroke:#1b5e20,stroke-width:2px
    classDef final fill:#c8e6c9,stroke:#2e7d32,stroke-width:3px

    class A researcher
    class B,C,D,E,F,K,L,M,N,O,P agent
    class G,R approval
    class I,J,S,T support
    class U,V,W support
```

---

## Module Interaction Matrix

| → | 01 | 02 | 03 | 04 | 05 | 06 | 07 | 08 | 09 | 10 | 11 | 12 | 13 | 14 | 15 | 16 |
|---|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|----|
| **01_Intake** | — | ✓ | ✓ | — | — | — | — | — | — | — | — | — | ✓ | ✓ | — | ✓ |
| **02_Clarify** | — | — | ✓ | — | — | — | — | — | — | — | — | — | ✓ | ✓ | — | ✓ |
| **03_Decompose** | — | — | — | ✓ | — | — | — | — | — | — | — | — | — | ✓ | — | ✓ |
| **04_Discovery** | — | — | — | — | ✓ | — | — | — | — | — | — | — | — | ✓ | ✓ | ✓ |
| **05_PlanSynth** | — | — | — | — | — | — | — | — | — | — | — | — | ✓ | ✓ | ✓ | ✓ |
| **06_CodeGen** | — | — | — | — | — | — | ✓ | — | — | — | — | — | — | ✓ | ✓ | — |
| **07_Orchestrate** | — | — | — | — | — | — | — | ✓ | ✓ | ✓ | ✓ | ✓ | — | ✓ | — | ✓ |
| **08_ExecAdapter** | — | — | — | — | — | — | — | — | ✓ | — | — | — | — | ✓ | — | ✓ |
| **09_Monitor** | — | — | — | — | — | — | — | — | — | ✓ | — | — | — | ✓ | — | ✓ |
| **10_Interpret** | — | — | — | — | — | — | — | — | — | — | ✓ | ✓ | — | ✓ | — | — |
| **11_CrossVal** | — | — | — | — | — | — | — | — | — | — | — | ✓ | — | ✓ | — | ✓ |
| **12_Correct** | — | — | ✓ | ✓ | ✓ | — | — | — | — | — | — | — | ✓ | ✓ | — | ✓ |
| **13_HITL** | — | — | — | — | — | — | — | — | — | — | — | — | — | ✓ | — | ✓ |
| **14_Provenance** | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |
| **15_Policy** | — | — | — | ✓ | ✓ | ✓ | — | — | — | — | — | — | — | — | — | ✓ |
| **16_ControlPlane** | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |

Legend: ✓ = direct interaction; — = no direct interaction

---

## State Machine: Major Transitions

```mermaid
stateDiagram-v2
    [*] --> INTAKE
    
    INTAKE --> CLARIFY: Request parsed
    CLARIFY --> CLARIFY: Ask Q&A (loop)
    CLARIFY --> DECOMPOSE: Confidence ≥ threshold
    
    DECOMPOSE --> DISCOVER: Goals identified
    DISCOVER --> PLAN_SYNTH: Candidates ranked
    
    PLAN_SYNTH --> AWAIT_APPROVAL: Plan ready
    AWAIT_APPROVAL --> APPROVE: Researcher: Yes
    AWAIT_APPROVAL --> REJECT: Researcher: No
    AWAIT_APPROVAL --> EDIT: Researcher: Edit
    
    REJECT --> DECOMPOSE: Re-decompose
    EDIT --> PLAN_SYNTH: Re-plan
    
    APPROVE --> CODE_GEN: Plan approved
    CODE_GEN --> POLICY_CHECK: Code generated
    POLICY_CHECK --> EXECUTE: Policy OK
    POLICY_CHECK --> BLOCKED: Policy blocked (feedback to researcher)
    BLOCKED --> AWAIT_APPROVAL: Re-plan with constraints
    
    EXECUTE --> MONITOR: Running
    MONITOR --> MONITOR: Track progress
    MONITOR --> INTERPRET: Execution done
    
    INTERPRET --> VALIDATE: Results parsed
    VALIDATE --> VALIDATE_APPROVED: Metrics match criteria ✓
    VALIDATE --> VALIDATE_REJECTED: Metrics don't match criteria ✗
    VALIDATE --> VALIDATE_MARGINAL: Borderline (review)
    
    VALIDATE_APPROVED --> FINAL_REVIEW: Results good
    VALIDATE_REJECTED --> CORRECT: Diagnose failure
    VALIDATE_MARGINAL --> FINAL_REVIEW: Human decides
    
    CORRECT --> CORRECT_PLAN: Propose fixes
    CORRECT_PLAN --> DECIDE_RERUN: Expected gain > cost?
    DECIDE_RERUN --> EXECUTE: Approved: Rerun
    DECIDE_RERUN --> ASK_RESEARCHER: Denied: Ask human
    
    ASK_RESEARCHER --> FINAL_REVIEW: Researcher reviews proposals
    
    FINAL_REVIEW --> ACCEPT: Yes
    FINAL_REVIEW --> OVERRIDE: No, but use anyway
    FINAL_REVIEW --> MODIFY: Request changes
    
    MODIFY --> CLARIFY: Restart with new intent
    ACCEPT --> ARCHIVE: Results accepted
    OVERRIDE --> ARCHIVE: Results overridden (flagged)
    
    ARCHIVE --> [*]
```

---

## Data Flow: Contract Sequence

```mermaid
sequenceDiagram
    participant R as Researcher
    participant NLU as 01_Intake_NLU
    participant C as 02_Clarify
    participant D as 03_Decompose
    participant DISC as 04_Discovery
    participant PS as 05_PlanSynth
    participant HITL as 13_HITL

    R->>NLU: "Predict solubility of aspirin"
    NLU->>NLU: Parse NL → IntentSpec
    NLU->>C: IntentSpec (confidence=0.6 on compound_id)
    
    C->>C: Identify missing: Is it acetylsalicylic acid?
    C->>R: "Which aspirin? (CAS: 50-78-2) or variant?"
    R->>C: "CAS 50-78-2, water solvent, 25°C"
    C->>C: Update IntentSpec (confidence=0.95)
    C->>D: RefinedIntentSpec
    
    D->>D: Decompose → Goals: [GetSMILES, ValidateSMILES, PredictSolubility, ValidateVsBaseline]
    D->>D: DAG: sequential, no parallelism
    D->>DISC: GoalGraph
    
    DISC->>DISC: Query registry: "SMILES→property, solubility"
    DISC->>DISC: Find: RDKit (★0.95), DeepChem (★0.85), Chemprop (★0.80)
    DISC->>PS: CandidateMethodSet
    
    PS->>PS: Pick RDKit, estimate cost (10 LLM tokens, 2 min compute, $0.05)
    PS->>PS: Set acceptance: "MSE < 0.5 vs literature"
    PS->>HITL: ExecutionPlan
    
    HITL->>R: "Plan: Use RDKit features + LinearRegression. Cost: $0.05, time: 2min. Approve?"
    R->>HITL: ✓ Approve
    HITL-->>PS: ApprovalDecision
```

---

## Error Recovery Paths

```mermaid
graph TD
    E["Execution<br/>Fails"] --> M1{Classify<br/>Error}
    
    M1 -->|Network<br/>Timeout| R1["Retry<br/>(exponential<br/>backoff)"]
    M1 -->|Missing<br/>Dependency| R2["Suggest<br/>pip install<br/>+ Rerun"]
    M1 -->|OOM| R3["Reduce<br/>Batch Size<br/>+ Rerun"]
    M1 -->|Invalid<br/>SMILES| R4["Ask Researcher<br/>for Corrected<br/>Input"]
    M1 -->|Tool<br/>Crashed| R5["Propose Next-Best<br/>Tool + Replan"]
    M1 -->|Timeout| R6["Extend Wall-Clock<br/>Time + Rerun"]
    
    R1 --> DR{Retries<br/>Exhausted?}
    R2 --> DR
    R3 --> DR
    R6 --> DR
    
    R4 --> FAIL["Escalate to<br/>Researcher"]
    R5 --> REPLAN["Replan from<br/>Step 4"]
    
    DR -->|No| RETRY["Retry"]
    DR -->|Yes| FAIL
    
    RETRY --> E
    REPLAN --> DISC["Discovery"]
    FAIL --> ASK["Researcher<br/>Decision<br/>Gate"]
    
    ASK -->|Retry| RETRY
    ASK -->|Abandon| ABORT["Mark Run<br/>as Failed<br/>+ Archive"]
    ASK -->|Edit Input| C["Clarify<br/>Stage"]
    
    style E fill:#ff6b6b
    style M1 fill:#ffd93d
    style FAIL fill:#ff6b6b
    style ABORT fill:#ff6b6b
    style REPLAN fill:#6bcf7f
    style C fill:#6bcf7f
```

---

## Module Dependencies (Build Order)

```mermaid
graph LR
    EP1["Epic 1<br/>Schemas"]
    EP2["Epic 2<br/>Control Plane"]
    EP3["Epic 3<br/>Researcher UX"]
    EP4["Epic 4<br/>Discovery"]
    EP5["Epic 5<br/>Code Gen"]
    EP6["Epic 6<br/>Validation"]
    EP7["Epic 7<br/>Provenance"]
    EP8["Epic 8<br/>Chemistry Pilot"]
    
    EP1 --> EP2
    EP1 --> EP3
    EP1 --> EP4
    EP1 --> EP5
    EP1 --> EP6
    EP1 --> EP7
    
    EP2 --> EP5
    EP2 --> EP7
    
    EP3 --> EP5
    
    EP4 --> EP5
    
    EP5 --> EP6
    EP5 --> EP7
    
    EP6 --> EP7
    
    EP2 --> EP8
    EP3 --> EP8
    EP4 --> EP8
    EP5 --> EP8
    EP6 --> EP8
    EP7 --> EP8
    
    style EP1 fill:#ff9999,stroke:#c00
    style EP2 fill:#ff9999,stroke:#c00
    style EP8 fill:#99ccff,stroke:#00c
    
    classDef normal fill:#ffcc99,stroke:#f60
    class EP3,EP4,EP5,EP6,EP7
```

---

## Runtime Execution Timeline (Example: 1 Iteration)

```
Researcher: "Predict aspirin solubility"
│
├─ [00:00] 01_Intake_NLU (10s)
│          Parses: "aspirin", "solubility", infers aqueous
│
├─ [00:10] 02_Clarification (30s)
│          Q: "CAS 50-78-2?" → R: "Yes"
│
├─ [00:40] 03_Goal_Decomposer (5s)
│          GoalGraph: [getSmiles → validateSmiles → predict → validate]
│
├─ [00:45] 04_Method_Discovery (20s)
│          Query registry + PyPI, rank 3 candidates
│
├─ [01:05] 05_Plan_Synthesis (10s)
│          Select RDKit, estimate cost $0.05, time 2min
│
├─ [01:15] HUMAN APPROVAL (⏸ awaiting researcher)
│          Researcher sees plan, clicks "Approve"
│
├─ [05:15] 06_Code_Gen (5s)
│          Generate main.py + requirements.txt
│
├─ [05:20] 15_Policy_Check (2s)
│          License = MIT ✓, Cost OK ✓
│
├─ [05:22] 07_Runtime_Orchestrator (2s)
│          Create session, checkpoint state
│
├─ [05:24] 08_Execution_Adapter (130s)
│          Install deps (90s), run main.py (40s), capture output
│
│          [Execution events streamed to 09_Monitor]
│          ├─ 05:24 - pip install rdkit (90s)
│          ├─ 06:54 - Execute prediction script
│          ├─ 07:34 - Output written to solubility.csv
│          └─ 07:35 - Execution complete, exit code 0
│
├─ [07:35] 10_Result_Interpreter (3s)
│          Parse CSV: predicted_solubility=2.3 (log units), std_dev=0.15
│
├─ [07:38] 11_Cross_Validation (2s)
│          vs literature baseline: literature=2.1, error=0.2
│          VALIDATION: ACCEPTED (within MSE < 0.5 threshold)
│
├─ [07:40] 12_Self_Correction (skip)
│          Validation passed, no correction needed
│
├─ [07:41] FINAL REVIEW (⏸ awaiting researcher)
│          Show: Predicted=2.3, Literature=2.1, Error=0.2
│          Researcher: "Looks good!"
│
├─ [07:45] 14_Provenance_Memory (2s)
│          Archive session event log + artifacts
│
└─ [07:47] ✅ DONE
          Publish: Code, config, results, provenance JSON
          Researcher: "Share with collaborators" → GCS URL

Total: 7 min 47 sec (vs estimate 5 min — overhead from deps + safety checks)
```

---

## Resource Utilization Profile

```
CPU:      ███░░░░░░░░░░░░░░░░ (15% avg, 40% peak during execution)
Memory:   ██░░░░░░░░░░░░░░░░░ (2% avg, 8% peak during execution)
Network:  █░░░░░░░░░░░░░░░░░░ (1% avg, used for PyPI/GitHub discovery)
Disk:     █░░░░░░░░░░░░░░░░░░ (artifacts cache, logs)

Bottlenecks:
  - Dependency installation (pip): 60–90s (first run), cached thereafter
  - Execution (tool-dependent): 10s–10min (e.g., MD simulations >> property prediction)
  - LLM latency (planning, clarification): 2–10s per call
  - Cross-validation (baseline lookup + metric computation): <5s
```

---

## Contract Validation Checkpoints

Every inter-module handoff validates contracts:

```
┌──────────────────────────┐
│ IntentSpec              │
│ {objective, domain,     │
│  system_descriptors,    │
│  constraints,           │
│  acceptance_metrics}    │
└──────────────────────────┘
         ↓
    [Validate]
         ↓
┌──────────────────────────┐
│ RefinedIntentSpec       │
│ (same schema, higher    │
│  confidence scores)     │
└──────────────────────────┘
         ↓ → 03_Decompose
    [Validate]
         ↓
┌──────────────────────────┐
│ GoalGraph               │
│ {goals[], edges[],      │
│  metadata}              │
└──────────────────────────┘
         ↓ → 04_Discovery
    [Validate]
         ↓
┌──────────────────────────┐
│ CandidateMethodSet      │
│ [{method, score,        │
│   trust_tier}]          │
└──────────────────────────┘
         ↓ → 05_PlanSynth
    [Validate]
         ↓
┌──────────────────────────┐
│ ExecutionPlan           │
│ {selected_method,       │
│  compute_estimate,      │
│  acceptance_criteria}   │
└──────────────────────────┘
         ↓ → 06_CodeGen + 13_HITL
    [Validate]
         ↓
┌──────────────────────────┐
│ RunBundle               │
│ {main.py, config.yaml, │
│  requirements.txt}      │
└──────────────────────────┘
         ↓ → 08_Executor
    [Execute & Capture]
         ↓
┌──────────────────────────┐
│ ExecutionResult         │
│ {exit_code, stdout,     │
│  stderr, artifacts}     │
└──────────────────────────┘
         ↓ → 10_Interpreter
    [Parse & Normalize]
         ↓
┌──────────────────────────┐
│ ResultPackage           │
│ {primary_metric,        │
│  secondary_metrics,     │
│  quality_flags}         │
└──────────────────────────┘
         ↓ → 11_CrossValidate
    [Validate]
         ↓
┌──────────────────────────┐
│ ValidationReport        │
│ {baseline_comparison,   │
│  acceptance_status,     │
│  diagnoses}             │
└──────────────────────────┘
         ↓ → 12_Correct (if needed) or 13_HITL (final review)
```

---

## Summary

- **Total Modules**: 16 independent agents
- **Critical Path**: Epics 1, 2, 4, 5, 6 (must complete in order)
- **Parallel Work**: Epics 3, 7 can proceed independently once Epic 1 is done
- **Convergence**: Epic 8 validates all modules integrated correctly
- **Reproducibility**: Full provenance enables deterministic replay from any historical state
