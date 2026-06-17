# TWAIN Architecture Diagrams Index

All diagrams are in **draw.io format** (.drawio files) and can be opened with:
- **draw.io** (online): https://draw.io
- **draw.io Desktop** (offline app)
- **VS Code**: With drawio extension
- **GitHub**: Native preview (read-only)

---

## 📊 Diagram Catalog

### 1. **System Dataflow** (`01_system_dataflow.drawio`)
**Purpose**: Complete pipeline showing how a researcher request flows through all 16 modules  
**Best For**: Architecture overview, onboarding new team members, presentations  
**Shows**:
- 6 phases: Intake → Planning → Code Gen → Execution → Validation → Archive
- 16 module boxes with colors: purple (agents), blue (input), orange (approval), green (support)
- Cross-cutting concerns: Control Plane, Provenance, Policy & Governance
- Main flow with decision branches (approve/reject/edit)

**Key Takeaway**: Researcher input flows left-to-right through intelligent agents, with approval gates and support layers

---

### 2. **State Machine** (`02_state_machine.drawio`)
**Purpose**: State transitions and major decisions throughout a run  
**Best For**: Understanding run lifecycle, debugging state issues, designing control flow  
**Shows**:
- 15 major states (INTAKE → CLARIFY → DECOMPOSE → PLAN → AWAIT_APPROVAL → EXECUTE → VALIDATE → ARCHIVE)
- Transition guards (e.g., "Can't execute without approved plan")
- Loops for clarification and correction
- Approval gates with yes/no/edit branches
- Decision points for validation acceptance

**Key Takeaway**: System never enters invalid states; researcher can always reject/edit plans or overrides

---

### 3. **Epic Dependencies** (`03_epic_dependencies.drawio`)
**Purpose**: Implementation roadmap showing which epics block others  
**Best For**: Project planning, resource allocation, critical path analysis  
**Shows**:
- 8 epics arranged by critical path (red) and parallelizable (orange) work
- Dependencies with arrows (what blocks what)
- Effort estimates (2w, 2.5w, etc.) and timeline labels
- Staffing options: 1 FTE (18w), 2 FTE (8–10w), 3 FTE (6–7w)
- Summary box showing sequential critical path vs parallel work

**Key Takeaway**: Epic 1 (schemas) is critical path; Phases 3–7 can overlap for faster delivery

---

### 4. **Data Contracts Flow** (`04_data_contracts.drawio`)
**Purpose**: All inter-module data passing with validation checkpoints  
**Best For**: Understanding contract-first design, troubleshooting data mismatches  
**Shows**:
- 10 major data structures (IntentSpec → RefinedIntentSpec → GoalGraph → ... → ProvenanceEvent)
- Each box shows sample fields
- Green validation checkpoints between each stage
- Two stages: intake/planning (top row) → execution/archive (bottom row)

**Key Takeaway**: Every inter-module handoff is validated; no implicit data assumptions

---

### 5. **Error Recovery** (`05_error_recovery.drawio`)
**Purpose**: How the system classifies and recovers from execution failures  
**Best For**: Implementing resilience, testing error paths, understanding retry strategies  
**Shows**:
- 7 error types (network timeout, dependency, OOM, invalid input, crash, timeout, policy blocked)
- Recovery strategy for each (retry, install dependency, reduce batch, ask researcher, fallback tool, extend time)
- Retry limit decision (replan vs escalate)
- Researcher decision gate (retry, abandon, edit input, clarify intent)
- Final states (success, failed, restart)

**Key Takeaway**: Transient errors auto-recover; permanent errors escalate to researcher with clear options

---

### 6. **Runtime Timeline** (`06_runtime_timeline.drawio`)
**Purpose**: Wall-clock execution example showing time per phase  
**Best For**: Setting expectations, tuning estimates, understanding bottlenecks  
**Shows**:
- 13 phases from request to archive
- Time, agent, activity, and duration for each
- Example: aspirin solubility prediction
- Total: 7m 47s (includes 4m researcher approval wait)
- Breakdown: LLM 7k tokens, compute 2.5min, cost $0.08

**Key Takeaway**: MVP runs complete in <10 minutes; biggest overhead is dependency installation + human waits

---

## 🎨 Color Scheme

All diagrams use consistent colors:
- **Purple** (`#f3e5f5` / `#4a148c`): Agent modules
- **Light Blue** (`#e3f2fd` / `#1565c0`): Processing/system boxes
- **Green** (`#c8e6c9` / `#1b5e20`): Success, support, governance
- **Orange** (`#fff3e0` / `#e65100`): Human decision gates, approval
- **Red** (`#ffcdd2` / `#c62828`): Errors, failures, critical path
- **Light Coral** (`#f8cecc`): Input data

---

## 📖 How to Use These Diagrams

### For Architects
1. Start with **Diagram 1** (System Dataflow) for big picture
2. Deep-dive **Diagram 2** (State Machine) to understand state transitions
3. Use **Diagram 4** (Data Contracts) to validate contract designs
4. Use **Diagram 5** (Error Recovery) when designing resilience

### For Project Managers
1. Use **Diagram 3** (Epic Dependencies) for sprint planning
2. Use **Diagram 6** (Runtime Timeline) to set delivery expectations
3. Track progress against Epics 1–8

### For Engineers
1. Start with **Diagram 1** for module overview
2. Deep-dive your module in **Diagram 2** (state) or **Diagram 4** (contracts)
3. Use **Diagram 5** for error handling strategy
4. Test against **Diagram 6** timeline expectations

### For Presentations
1. Start with **Diagram 1** (big picture, 2-3 min)
2. Zoom into your specific module's flow
3. Use **Diagram 6** for Q&A on performance

---

## 🔧 Editing Tips

To open and edit diagrams:

1. **Online (draw.io)**:
   - Visit https://draw.io
   - File → Open → Upload `.drawio` file
   - Make changes and export

2. **Desktop (draw.io)**:
   - Download from https://www.diagrams.net/
   - File → Open → Select `.drawio` file

3. **VS Code**:
   - Install `Draw.io Integration` extension
   - Open `.drawio` file directly in editor

4. **Git-friendly**:
   - `.drawio` files are XML-based (text)
   - Diff-able and mergeable like code
   - Commit changes normally

---

## 📝 Diagrams vs Markdown

| Need | Use |
|------|-----|
| **Visual system overview** | Diagram 1 (System Dataflow) |
| **Understanding state machine** | Diagram 2 (State Machine) |
| **Project planning** | Diagram 3 (Epic Dependencies) + DETAILED_BACKLOG.md |
| **Data contracts & validation** | Diagram 4 (Data Contracts) + schemas/\*.schema.json |
| **Error handling strategy** | Diagram 5 (Error Recovery) + modules/\*/error_handler.py |
| **Performance tuning** | Diagram 6 (Runtime Timeline) + modules/\*/NOTES.md |
| **Architecture rationale** | REVIEW.md + docs/decisions/\*.md |
| **Implementation details** | DETAILED_BACKLOG.md + code comments |

---

## 🎯 Diagram Completeness Checklist

- [x] **Diagram 1**: All 16 modules shown, cross-cutting concerns attached, approval gates marked
- [x] **Diagram 2**: All states, transitions, guards, loops for clarification/correction
- [x] **Diagram 3**: Critical path (Epics 1–2, 5–8), parallel work (Epics 3–4, 6–7), dependencies clear
- [x] **Diagram 4**: All data contracts, validation checkpoints, field samples
- [x] **Diagram 5**: Error types, recovery strategies, decision points, final states
- [x] **Diagram 6**: 13 phases, realistic timing, example run, resource breakdown

---

## 🚀 Next Steps

1. **Review diagrams** in draw.io or GitHub preview
2. **Update if needed**: Styles, labels, architecture changes
3. **Use in sprints**: Reference during stand-ups, design reviews, onboarding
4. **Link from docs**: Update markdown docs to reference relevant diagrams
5. **Iterate**: As implementation progresses, diagrams become source of truth for system state

---

**Last Updated**: 2026-06-03  
**Status**: Ready for implementation  
**Format**: draw.io XML (GitHub-viewable, locally-editable)
