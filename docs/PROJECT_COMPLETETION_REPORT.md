# TWAIN — Project Completion Report

**Date:** 2026-07-24
**Scope:** 53 GitHub issues (Epics 1–8 + 21 ungrouped items in `washu-dev/TWAIN`) cross-referenced against the code on `master`.
**Method:** Each issue's acceptance criteria were audited against the actual code **independent of the issue's open/closed flag**, so the status below reflects the code, not the tracker.
**Caveat:** Reflects the **Issues** backlog, not the GitHub Projects v2 board columns.

---

## 1. Headline

| Measure | Result |
|---|---|
| **Code-verified fully done** | **~32 / 53 (60%)** |
| Partial (built, gaps vs acceptance criteria) | 11 (21%) |
| Missing / not started | 8 (15%) |
| Won't-do / tracking-only | 2 (#2 FreeBird, #63 npm-audit) |
| **Weighted completion** | **~70%** |
| GitHub tracker state | 24 closed / 29 open |

**The tracker materially understates reality:** ~32 items are code-complete but only 24 are marked closed. Entire Epics 4 and 6 (7 stories) are fully implemented and tested yet still OPEN.

**One-line verdict:** the *engine* (schemas → discovery → planning → codegen → execution incl. Slurm → result parsing → validation → self-correction) is ~90% built and well-tested; what's missing is the **governance layer (Epic 7)**, the **human final-approval gate (Story 3.3)**, and the **chemistry-MVP proof + end-to-end demo (Epic 8)** — plus a backlog-hygiene pass, since a third of completed work is still marked open.

---

## 2. Status by epic

| Epic | Theme | Verified status |
|---|---|---|
| **1** | Data-contract schemas | ✅ **100%** — 6/6 done, 213 tests green |
| **2** | Agent runtime | 🟢 **~80%** — state machine / budget / session solid; event bus (#28) & retry circuit-breaker (#29) untested despite CLOSED |
| **3** | Approval / human-in-loop | 🟡 **~40%** — plan-gate (#32) & clarification loop (#33) built; final acceptance+override (#34) is a stub |
| **4** | Method discovery | ✅ **100%** — 4/4 done, 144 tests, real PyPI/GitHub HTTP adapters (all still OPEN) |
| **5** | Codegen & execution | ✅ **100%** — incl. full Slurm pipeline; 1 stale failing test |
| **6** | Results / validation / self-correction | ✅ **100%** — 3/3 done, 74 tests (all still OPEN) |
| **7** | Provenance / policy / trust | 🔴 **~30%** — provenance log real (#45); policy (#46) & trust (#47) module empty |
| **8** | Chemistry benchmark & MVP | 🔴 **~15%** — scaffolding only; no solubility tool, model, real E2E test, or demo |

---

## 3. Detailed findings by epic

### Epic 1 — Data-contract schemas (#21–26, all CLOSED) — ✅ 100%
All six stories DONE; 185 unit + 28 contract tests pass. Cosmetic only: some issues closed with acceptance-criteria checkboxes unticked; acceptance criteria reference flat paths (`intake/`, `goal_decomposer/`, …) while code lives under numbered `modules/NN_*`.

| Issue | Verdict | Evidence |
|---|---|---|
| 21 IntentSpec | DONE | `schemas/intent_spec.schema.json`, `modules/01_intake_nlu/intent_spec.py`, `tests/unit/test_intent_spec.py` |
| 22 GoalGraph | DONE | `schemas/goal_graph.schema.json`, `modules/03_goal_decomposer/graph_builder.py` (cycle detection + Kahn topo-sort) |
| 23 ExecutionPlan | DONE | `schemas/execution_plan.schema.json`, `modules/05_plan_synthesis/{execution_plan,plan_validator}.py` |
| 24 ResultPackage + ValidationReport | DONE | `schemas/{result_package,validation_report}.schema.json` + interpreter/cross-validation modules |
| 25 CorrectionPlan + ProvenanceEvent | DONE | schemas present; append-only hash-chained `modules/14_provenance_memory/event_log.py` |
| 26 Contract Validation Suite | DONE | `tests/contract/test_schema_consistency.py` (28 pass) + ADR `docs/decisions/schema_versioning.md` |

### Epic 2 — Agent runtime (#27–31, all CLOSED) — 🟢 ~80%
| Issue | Verdict | Notes |
|---|---|---|
| 27 State Machine | DONE | `modules/16_agent_mesh_control_plane/statemachine.py`, guard table, crash recovery; 848-line test |
| 28 Event Bus | **PARTIAL / discrepancy** | `event_bus.py` pub/sub + history, but **no tests**, no deterministic replay, no handler interface (all in AC) |
| 29 Retry & Backoff | **PARTIAL** | `retry_policy.py` backoff + circuit breaker exist, but **circuit breaker untested** |
| 30 Timeout & Budget | DONE | `budget_tracker.py` + per-stage timeouts; best-effort thread shutdown caveat |
| 31 Session & Persistence | DONE | `session.py` + SQLite `store.py`; deterministic resume tested; no concurrent-isolation test |

### Epic 3 — Approval / human-in-loop (#32–34, all OPEN) — 🟡 ~40%
Functionality built as a web/runner/state-machine design rather than the CLI-module layout the issues prescribe (`modules/13_human_in_the_loop/`, `modules/02_clarification_dialogue/` are empty `.gitkeep`).

| Issue | Verdict | Notes |
|---|---|---|
| 32 Approval Gate | **PARTIAL / discrepancy** | Plan gate enforced (`statemachine.py`, `runner/`, API `/api/conversations/{id}/approval`); missing "edit" command, push notifications |
| 33 Clarification Loop | **PARTIAL / discrepancy** | 0.8 confidence threshold + bounded 3-round loop fully implemented; no standalone CLI |
| 34 Final Acceptance & Override | **MISSING** | `accept()` is a passthrough stub; no human final-review gate, no override/rationale capture |

### Epic 4 — Method discovery (#35–38, all OPEN) — ✅ 100%
All four DONE; 144 tests pass. External adapters make **real** PyPI/GitHub HTTP calls (injectable fetchers for offline tests). All still OPEN — ready to close.

| Issue | Verdict | Evidence |
|---|---|---|
| 35 Discovery Registry | DONE | `registry_loader.py`, 17-entry `configs/discovery_registry.json` |
| 36 Candidate Scoring Rubric | DONE | `scorers.py` (relevance .40/maturity .20/license .15/trust .15/repro .10), deterministic top-k |
| 37 External Registry Adapters | DONE | `sources/{pypi,github}_adapter.py`, dedup/merge, rate-limit cap 3 |
| 38 Plan Synthesis w/ Risk & Cost | DONE | `plan_synthesizer.py`, `cost_estimator.py`, `risk_assessor.py`, `plan_validator.py` |

### Epic 5 — Codegen & execution (#39, 40, 41, 56) — ✅ 100%
| Issue | State | Verdict | Notes |
|---|---|---|---|
| 39 Code Generation | CLOSED | DONE | `codegen_engine.py` (RunBundle), templates, 67 tests |
| 40 Local Execution Adapter | OPEN | **DONE / discrepancy** | `local_adapter.py` + SIGTERM→SIGKILL + resource monitor; wired into state machine — should close |
| 41 Session & Event Loop | CLOSED | DONE | `orchestrator.py`; **1 stale failing test** — `test_orchestrator.py:478` asserts 20-min EXECUTE timeout, code is now 2 h |
| 56 Slurm commands | OPEN | **DONE / discrepancy** | `slurm_adapter.py` + `slurm_execution_adapter.py` + staging + cluster profiles + engine routing; 43 tests — should close (prod needs VPN+SSH) |

### Epic 6 — Results / validation / self-correction (#42–44, all OPEN) — ✅ 100%
All three DONE; 74 tests pass; modules chain cleanly (extract → normalize → cross-validate → verdict → reflect → bounded rerun). All still OPEN — should close.

| Issue | Verdict | Evidence |
|---|---|---|
| 42 Result Parsing & Metric Extraction | DONE | `modules/10_result_interpreter/extractors/` (csv/json/log/rdkit), `metric_normalizer.py`, `confidence_estimator.py` |
| 43 Cross-Validation Against Baselines | DONE | `configs/baselines.json`, `baseline_validator.py` (RMSE/Pearson), `acceptance_judge.py` |
| 44 Self-Correction Reflection | DONE | `failure_classifier.py`, `strategies/`, `rerun_controller.py` (cap 5, convergence stop) |

### Epic 7 — Provenance / policy / trust (#45–47, all OPEN) — 🔴 ~30%
| Issue | Verdict | Notes |
|---|---|---|
| 45 Provenance Event Log | **PARTIAL / discrepancy** | Real hash-chained log + tamper-detection tests; but JSONL (not SQLite+backup), no remote export, event-type set diverges from AC |
| 46 Policy Enforcement | **MISSING** | `modules/15_policy_safety_governance/` is empty `.gitkeep` |
| 47 Trust Scoring & Source Validation | **MISSING** | Same empty module; only tangential overlap in Story 4.2 candidate scoring |

### Epic 8 — Chemistry benchmark & MVP (#48–51, all OPEN) — 🔴 ~15%
| Issue | Verdict | Notes |
|---|---|---|
| 48 Define Chemistry Benchmark Task | **PARTIAL** | `configs/baselines.json` (14 logS values) + `goal_graph_molecular_solubility.json` example; no task-definition doc, 14 not 20 molecules, no RDKit baseline / MSE target |
| 49 Solubility Prediction Tool Integration | **MISSING** | No tool wrapper, no trained model (`experiments/` empty), no template |
| 50 End-to-End Integration Test | **PARTIAL / discrepancy** | `tests/integration/test_e2e_happy_path.py` is **schema-validation only**, not the real mocked-LLM full flow |
| 51 MVP Demo & Feedback Iteration | **MISSING** | No demo/feedback artifacts |

### Ungrouped (#1–18, 55, 58, 63)
| Issue | State | Verdict | Notes |
|---|---|---|---|
| 1 Pymatgen Schema | CLOSED | DONE | `schemas/PymatgenSchema.json` (minor: dead `lattice` conditional) |
| 2 FreeBird.jl Schema | CLOSED | **Won't-do** | No file; Julia dropped, backlogged |
| 3 ASE Schema | CLOSED | DONE | `schemas/AseSchema.json` |
| 4 GROMACS Schema | OPEN | MISSING | Not started (state correct) |
| 5 Provenance Log | OPEN | **DONE / discrepancy** | `modules/14_provenance_memory/event_log.py` + tests |
| 6 General Schema Fields | OPEN | **DONE / discrepancy** | `schemas/MetadataSchema.json` |
| 7 Execution Log | CLOSED | DONE | `runner/db.py` jobs/run_events tables |
| 8 Benchmark & Test Library | CLOSED | **Discrepancy** | Test suite on mainline, but `Benchmark/` lives only on unmerged `origin/Benchmark` branch |
| 9 Workflow Registry | OPEN | PARTIAL | Only discovery/calculator registries; no dedicated workflow registry |
| 10 implement pici | OPEN | **Discrepancy** | Typo-duplicate of completed #11 — should close |
| 11 implement pixi | CLOSED | DONE | `pixi.toml` + `pixi.lock` |
| 12 Fix ExecutionLog v1 bugs | CLOSED | DONE | Refactored into `event_log.py`/`store.py`/`runner/db.py`; cited bugs fixed |
| 13 gitignore .idea | CLOSED | DONE | `.gitignore` has `.idea/` |
| 14 Sync .yaml in Schema | CLOSED | DONE | Resolved by deletion |
| 15 Query subjects physics/biophysics/materials | OPEN | PARTIAL | Only "materials" defined |
| 16 Research workflow managers | OPEN | MISSING | Research task; no deliverable found |
| 17 SpeechRecognition | OPEN | MISSING | No code/deps in `app/` |
| 18 Schema Validator | CLOSED | **PARTIAL / buggy** | `schema_validator.py` has 2 bugs (`self.schemaFile` undefined; `__main__` calls non-existent `validate()`) |
| 55 Goal Decomposer (Claude→DAG) | CLOSED | DONE | `statemachine.py` `_build_goal_graph` + `AgentInterface` (claude-opus-4-8) + acyclicity gate |
| 58 Add SSO to app/ | OPEN | **DONE / discrepancy** | Entra OIDC + PKCE in `app/src/hooks/useAuth.tsx`, `constants/theme.ts` — should close |
| 63 Track npm audit (uuid) | OPEN | Tracking (correctly open) | uuid@7.0.3 via Expo prebuild tooling; upstream-blocked |

---

## 4. Discrepancies (tracker ≠ code)

**Done but still OPEN → should close (11):** #35, #36, #37, #38, #42, #43, #44, #40, #56, #5, #6, #58 (SSO), #10 (dup of #11).

**Closed but weaker than claimed → reopen or file follow-up (4):**
- **#28 Event Bus** — no tests, no deterministic replay, no handler interface.
- **#29 Retry/Backoff** — circuit breaker untested.
- **#8 Benchmark & Test Library** — `Benchmark/` suite only on unmerged `origin/Benchmark` branch.
- **#18 Schema Validator** — two live bugs.

---

## 5. Remaining work (net-new), prioritized

1. **Epic 8 — MVP proof (highest risk):** solubility tool wrapper (#49), trained model, a *real* end-to-end pipeline test with mocked LLMs (#50), benchmark task doc + demo (#48/#51).
2. **Epic 7 — governance:** implement `modules/15_policy_safety_governance/` — policy enforcement (#46) + trust scoring (#47).
3. **Epic 3 — final acceptance & override (#34):** human final-review gate + override/rationale capture.
4. **Test coverage:** event bus + circuit breaker; fix stale failing test (`test_orchestrator.py:478`).
5. **Smaller:** GROMACS schema (#4), SpeechRecognition (#17), physics/biophysics query subjects (#15), workflow registry scoping (#9), fix #18 validator bugs, merge/re-scope `Benchmark/` (#8).

---

## 6. Test health

Large green suite where implemented: Epic 1 (185 unit + 28 contract), Epic 4 (144), Epic 6 (74), Epic 5 (166 pass / 1 fail / 1 skip). Untested islands: event bus, circuit breaker, and the full chemistry pipeline integration.
