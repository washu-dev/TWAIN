# TWAIN Backlog Overview

**Version**: 1.0  
**Date**: 2026-06-11  
**Status**: Kanban board with 31+ stories ready for implementation

---

## Executive Summary

The TWAIN backlog is organized into **8 epics** across **16-18 weeks** (1 FTE) or **8-10 weeks** (2 FTE).

**GitHub Project**: https://github.com/orgs/washu-dev/projects/13  
**Repository**: https://github.com/washu-dev/TWAIN

---

## Epic Summary

| # | Epic | Stories | Effort | Priority | Critical Path | Status |
|---|------|---------|--------|----------|----------------|--------|
| 1 | **Schemas** | 6 stories | 2w | **P0** | **YES** | Ready |
| 2 | **Control Plane** | 5 stories | 2w | **P0** | **YES** | Ready |
| 3 | **Researcher UX** | 3 stories | 2w | **P1** | YES | Ready |
| 4 | **Discovery & Planning** | 4 stories | 2.5w | **P1** | **YES** | Ready |
| 5 | **Code Gen & Execution** | 3 stories | 2w | **P1** | **YES** | Ready |
| 6 | **Validation & Correction** | 3 stories | 2w | **P2** | YES | Ready |
| 7 | **Provenance & Governance** | 3 stories | 2w | **P2** | YES | Ready |
| 8 | **Chemistry Pilot** | 4 stories | 2w | **P3** | YES | Ready |
| **TOTAL** | — | **31+ stories** | **16–18w** | — | — | ✅ |

---

## Priority Tiers (How to Use)

### P0 - Critical Path (Blocks Everything)
- **Epics 1 & 2**: Schemas and Control Plane
- **Must complete first**: Lock schemas early, unlock parallel work
- **Estimated duration**: 4 weeks (Phases 1-2)
- **Action**: Staff 1 senior engineer + 1 mid-level engineer on these epics

### P1 - Core Features (Unlock First E2E Run)
- **Epics 3, 4, 5**: UX gates, discovery/planning, code gen & execution
- **Can start after**: Phases 1-2 complete
- **Estimated duration**: 5-6 weeks (Phases 3-5, can parallelize)
- **Action**: Assign 2-3 engineers after Epic 2; can work in parallel

### P2 - Support & Validation
- **Epics 6 & 7**: Result validation, self-correction, provenance, governance
- **Can start after**: Phase 5 (code execution working)
- **Estimated duration**: 4 weeks (Phases 6-7)
- **Action**: Assign 1-2 engineers

### P3 - Integration & Demo
- **Epic 8**: Chemistry pilot, end-to-end validation, demo
- **Can start after**: All prior epics substantially complete
- **Estimated duration**: 2 weeks (Phase 8)
- **Action**: Assign 1 engineer + chemistry domain expert

---

## Effort Breakdown (by Label)

### Low Effort (1-2 days, ≈ 2-4 issues)
- Schema definition stories
- Simpler modules

### Medium Effort (3-5 days, ≈ 12-16 issues)
- Most control plane stories
- Integration components

### High Effort (1-2 weeks, ≈ 8-10 issues)
- Complex state machines
- Code generation and planning
- Validation loops

---

## Kanban Board Columns

The GitHub Project board uses **5 columns** (no sprints):

1. **📋 Backlog** — Not yet started; waiting for dependencies
2. **🎯 Ready** — Dependencies met, ready for eng to pick up
3. **🚀 In Progress** — Actively being worked on
4. **🔍 In Review** — Code review in progress
5. **✅ Done** — Merged, tests pass, validated

**Flow**: Backlog → Ready → In Progress → In Review → Done

---

## Critical Path & Dependencies

### Phase 1: Schemas (Weeks 1-2) — P0
Stories: 1.1 → 1.2 → 1.3 → 1.4 → 1.5 → 1.6  
**Blocks**: Everything else  
**Assign to**: 1-2 senior/mid-level engineers (pair recommended)

### Phase 2: Control Plane (Weeks 2-4) — P0
Stories: 2.1 → 2.2 → 2.3 → 2.4 → 2.5  
**Blocks**: UX, discovery, execution  
**Depends on**: Epic 1  
**Assign to**: 1 senior engineer (distributed systems expertise)

### Phase 3: Researcher UX (Weeks 3-5) — P1
Stories: 3.1 → 3.2 → 3.3  
**Enables**: approval gates  
**Depends on**: Epics 1, 2  
**Assign to**: 1 mid-level engineer  
**Can parallelize with**: Phase 4

### Phase 4: Discovery & Planning (Weeks 3-6) — P1
Stories: 4.1 → 4.2 → 4.3 → 4.4  
**Enables**: tool discovery, cost estimation  
**Depends on**: Epics 1, 2, 3  
**Assign to**: 1 mid-level engineer  
**Can parallelize with**: Phase 3

### Phase 5: Code Gen & Execution (Weeks 5-7) — P1
Stories: 5.1 → 5.2 → 5.3  
**Enables**: first end-to-end run  
**Depends on**: Epics 1, 2, 4  
**Assign to**: 1 senior + 1 mid-level engineer

### Phase 6: Validation & Correction (Weeks 7-9) — P2
Stories: 6.1 → 6.2 → 6.3  
**Enables**: result interpretation, auto-correction  
**Depends on**: Epics 1, 5  
**Assign to**: 1 mid-level engineer

### Phase 7: Provenance & Governance (Weeks 8-10) — P2
Stories: 7.1 → 7.2 → 7.3  
**Enables**: audit trail, policy enforcement  
**Depends on**: Epics 1, 2, 5, 6  
**Assign to**: 1 senior engineer (governance/security expertise)

### Phase 8: Chemistry Pilot (Weeks 10-12) — P3
Stories: 8.1 → 8.2 → 8.3 → 8.4  
**Enables**: demo, validation  
**Depends on**: All prior epics  
**Assign to**: 1 mid-level engineer + chemistry domain expert

---

## Resource Recommendations

### Staffing Scenarios

**Scenario A: 1 FTE (18 weeks)**
- 1 senior full-stack engineer
- Phases execute sequentially
- Good for: proof-of-concept, learning the codebase

**Scenario B: 2 FTE (9-10 weeks)** ⭐ **RECOMMENDED**
- 1 senior engineer (Epics 1, 2, 5, 7)
- 1 mid-level engineer (Epics 1, 3, 4, 6, 8)
- Phases 1-2 sequential, Phases 3-7 overlap
- Good for: MVP timeline, balanced team

**Scenario C: 3 FTE (6-7 weeks)**
- 1 senior (architecture, control plane)
- 2 mid-level (discovery/planning, code gen/execution, validation/governance in parallel)
- Aggressive, requires strong async communication

---

## Definition of Ready (DoR)

A story is **Ready** when:
- [ ] Dependencies are met (all blocked-by issues are Done)
- [ ] Acceptance criteria are clear and testable
- [ ] Effort estimate is documented
- [ ] Module paths and file names are specified
- [ ] Test plan is outlined

---

## Definition of Done (DoD)

A story is **Done** when:
- [ ] Acceptance criteria all marked complete (☑)
- [ ] Code is written and reviewed
- [ ] Unit tests pass (>80% coverage)
- [ ] Integration tests pass (if applicable)
- [ ] Schemas validate (if applicable)
- [ ] Documentation updated
- [ ] PR merged to main

---

## Success Metrics

Track these metrics throughout implementation:

| Metric | Target | How to Measure |
|--------|--------|---|
| **Issue Closure Rate** | 1 issue/day (2 FTE) | GitHub project burn-down |
| **Test Coverage** | >80% | `coverage` report in CI |
| **Schema Compliance** | 100% | Contract tests pass |
| **Reproducibility** | 100% | Replay tests pass |
| **E2E Latency** | <5 min | Time request → result |
| **Chemistry MSE** | <0.5 | Solubility benchmark |

---

## How to Use This Backlog

### Project Manager / Scrum Master
1. Monitor GitHub Project board for flow (Backlog → Done)
2. Unblock issues in "In Progress" stuck >2 days
3. Track velocity (issues/week)
4. Escalate blockers

### Engineering Lead
1. **Weekly standup**: Check "In Progress" column
   - What's blocked?
   - Do we need to pair?
   - Are dependencies on track?
2. **Sprint planning** (optional): Pick issues from "Ready" column
3. **Code review**: Ensure DoD checklist before merge
4. **Refine**: Move issues to "Ready" as dependencies clear

### Individual Engineer
1. Pick an issue from "Ready" column
2. Move to "In Progress"
3. Follow acceptance criteria exactly
4. Write tests alongside code (TDD)
5. Submit PR, move to "In Review"
6. After merge, move to "Done"
7. Pick next issue

### Researcher (Chemistry Pilot)
- Phase 8 (Epic 8) is your involvement
- You'll review demos and provide feedback
- Target: ~week 10-12

---

## Labels Explained

### Epic Labels (`epic1` through `epic8`)
- **Purpose**: Group related stories
- **Usage**: Filter board by epic to see related work

### Priority Labels (`P0`, `P1`, `P2`, `P3`)
- **P0**: Critical path, blocks other work
- **P1**: Core features, high value
- **P2**: Support/validation features
- **P3**: Nice-to-have, demo/integration

### Effort Labels (`Low`, `Medium`, `High`)
- **Low**: 1-2 days (mainly Epic 1 schemas)
- **Medium**: 3-5 days (most modules)
- **High**: 1-2 weeks (complex state machines, integration)

---

## Common Workflows

### 🔴 Issue is Stuck (Blocked)
1. **Identify blocker**: Check "Depends on" in issue body
2. **Escalate**: Comment on blocking issue + mention owner
3. **Workaround**: Can you mock the dependency?
4. **Alternative**: Is there a different issue to work on while unblocked?

### 🟡 Issue Scope Creeps
1. **Check acceptance criteria**: Are you adding new criteria?
2. **Extract**: Create a new issue for the extra scope
3. **Link**: Reference it in the current issue ("See also: #XYZ")
4. **Finish**: Complete original scope first

### 🟢 Ready to Merge
1. **Checklist**: All acceptance criteria done?
2. **Tests**: >80% coverage? All pass?
3. **Schemas**: Validate if applicable?
4. **Docs**: README updated?
5. **Code review**: Approved?
6. **Merge**: Squash or rebase (keep history clean)

---

## Next Steps

1. **This week**:
   - [ ] Review this backlog overview
   - [ ] Confirm priority tiers with team
   - [ ] Assign Epic 1 lead (senior engineer)

2. **Next week**:
   - [ ] Start Epic 1 (schemas) stories
   - [ ] Finalize IntentSpec + ExecutionPlan schemas
   - [ ] Begin design review for Epic 2 (control plane)

3. **Ongoing**:
   - [ ] Daily standup: unblock issues
   - [ ] Weekly review: measure velocity
   - [ ] Monthly: adjust priorities based on feedback

---

## FAQs

**Q: Can we start multiple epics in parallel?**  
A: Epics 3-7 can start after Epic 2 completes (~week 4). Not recommended to start multiple P0 epics in parallel.

**Q: What if a story is larger than estimated?**  
A: Break it into subtasks (via GitHub Projects) or split into 2 issues.

**Q: How do we handle bugs discovered during implementation?**  
A: Create a new `bug:` issue, link it to the story that caused it, prioritize in the board.

**Q: Do we need a dedicated QA engineer?**  
A: No. Each engineer writes tests for their code (TDD). Validation engineer (2 FTE plan) handles acceptance.

---

## Document References

- **Project Summary**: `PROJECT_SUMMARY.md` — executive overview
- **Review & Architecture**: `REVIEW.md` — detailed architecture, gaps, recommendations
- **Implementation Roadmap**: `IMPLEMENTATION_ROADMAP.md` — phased plan, resource plan
- **Getting Started**: `GETTING_STARTED.md` — quick orientation
- **Detailed Backlog**: `docs/backlog/DETAILED_BACKLOG.md` — full user stories (40+ items)

---

**Last Updated**: 2026-06-11  
**Created By**: Claude Code  
**Status**: Ready for implementation
