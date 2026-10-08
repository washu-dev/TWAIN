# Pipeline modules

The pipeline that turns a request into a validated result. It runs inside the
worker ([`runner/`](../runner/README.md)) as a state machine:

```
INTAKE → CLARIFY → DECOMPOSE → DISCOVER → PLAN ─(approval gate)→ BUILD → REPAIR → EXECUTE
       → INTERPRET → VALIDATE → ACCEPT → TERMINATE
                       └─ CORRECT / REPLAN loops back on a result that doesn't validate
```

The web app's tracker groups those states into five phases: **Plan** (INTAKE…PLAN),
**Build** (BUILD, REPAIR), **Run on RIS** (EXECUTE), **Check** (INTERPRET,
VALIDATE, CORRECT, REPLAN) and **Results** (ACCEPT, TERMINATE).

Directory names start with digits, so they can't be imported as packages.
`07_runtime_orchestrator/_bootstrap.py` puts them on `sys.path` and registers
short aliases (`intake`, `goal_decomposer`, `method_discovery`,
`plan_synthesizer`, `code_gen`, `execution_adapter`, `result_interpreter`,
`cross_validation`, `self_correction`, `provenance_memory`); `tests/conftest.py`
mirrors it. Shared paths live in `../twain_paths.py`. Diagram
[07](../docs/architecture/07_deployment_dependencies.drawio) shows the
dependency edges.

| Module | State(s) | What it holds | Depends on |
|---|---|---|---|
| `01_intake_nlu` | INTAKE, CLARIFY | `IntentSpec`. The parsing is done by the LLM in the state machine, validated against `schemas/intent_spec.schema.json` | none |
| `02_clarification_dialogue` | | *Placeholder.* Clarification lives in `statemachine.clarify()` + `runner/bridges.py` | |
| `03_goal_decomposer` | DECOMPOSE | Goal graph, cycle check, topological order | none |
| `04_method_discovery` | DISCOVER, PLAN, REPLAN | Registry loading (`configs/discovery_registry.json`, `calculator_registry.json`), weighted scoring, LLM toolset choice (`llm_discovery.py`), licence checks, `LibraryAddition` requests. The external PyPI/GitHub search adapters exist but aren't wired in | none |
| `05_plan_synthesis` | PLAN, REPLAN | `ExecutionPlan` / `SlurmRequest`, cost and risk estimates, plan validation | 04 |
| `06_code_configuration_builder` | BUILD, REPAIR, EXECUTE (runtime repair) | `codegen_engine.py` (LLM + templates → run bundle), `templates/` (5), `bundle_helpers/` (pseudopotentials, thermochemistry), `script_doctor.py` (review, smoke, fix), `dependency_inferencer.py` (tool → packages, conda-only / cluster-unrunnable lists, PyPI checks), `smoke_test_generator.py` | none (legacy `code_gen.py` → 16) |
| `07_runtime_orchestrator` | all | `Orchestrator`, `RunSession`, `error_handler.py` (classification, failure cards, next steps), `_bootstrap.py` | 16, 14, 05, 01 |
| `08_execution_adapter` | EXECUTE | Local and Docker adapters; Slurm through the **RIS API** (`ris_api_client.py`, `ris_api_adapter.py`) or SSH; `slurm_execution_adapter.py` (job script, env selection, detached submit/collect, exit-code classification); `s3_transport.py`; `cluster_profile.py` (`configs/clusters/*.json`); `job_activity.py` (the subtasks the app shows) | 05 |
| `09_observability_monitor` | | *Placeholder.* See `job_activity.py`, the event bus, `runner/monitor.py` | |
| `10_result_interpreter` | INTERPRET | Output extractors (CSV, JSON, log, RDKit), metric normalisation, confidence | none |
| `11_cross_validation` | VALIDATE, ACCEPT, PLAN | Baselines (`configs/baselines.json`), Materials Project references (`mp_reference.py`, `MP_API_KEY`), plausibility (`configs/physical_ranges.json`), acceptance thresholds | none |
| `12_self_correction_reflection` | CORRECT, REPLAN | Failure classifier and correction strategies | none |
| `13_human_in_the_loop` | | *Placeholder.* Gates live in `runner/bridges.py` + the state machine | |
| `14_provenance_memory` | all | Hash-chained provenance log; SQLite session store (Postgres in production through `runner/pg_store.py`) | none |
| `15_policy_safety_governance` | | *Placeholder.* Partly covered by the risk assessor, plan validator, licence checks and budget tracker | |
| `16_agent_mesh_control_plane` | all | **`statemachine.py`**: one handler per state, guards, artifacts, cluster env checks. Planning uses the RIS inventory through `use_cluster_inventory()`. Also `AgentInterface.py` (LLM gateway client: Claude through `aiapi.wustl.edu`, model `TWAIN_AGENT_MODEL`), prompts, event bus, budget, retries | 01, 03, 04, 05, 06, 07, 08, 10, 11, 12 |

Outside the repository, at plan, build and run time, the pipeline calls:
- the LLM gateway;
- PyPI's JSON API (is a package installable?);
- Materials Project (baselines, formula checks);
- GitHub (`LibraryAddition` issues);
- the RIS API.

Tests: `pixi run test`; the unit tests are in `tests/unit`.
