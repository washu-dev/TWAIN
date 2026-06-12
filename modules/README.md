# Module Hierarchy

Independent modules for the domain-neutral agentic simulation pipeline:

1. `01_intake_nlu` - Natural language request parsing into structured intent.
2. `02_clarification_dialogue` - Missing detail detection and targeted Q&A.
3. `03_goal_decomposer` - Decompose intent into executable sub-goals.
4. `04_method_discovery` - Discover and rank public models/tools/workflows.
5. `05_plan_synthesis` - Build executable plan with constraints and budgets.
6. `06_code_configuration_builder` - Generate scripts, configs, manifests.
7. `07_runtime_orchestrator` - Coordinate lifecycle of all run stages.
8. `08_execution_adapter` - Backend abstraction for local/HPC/cloud execution.
9. `09_observability_monitor` - Logs, metrics, traces, anomaly detection.
10. `10_result_interpreter` - Parse outputs and produce findings.
11. `11_cross_validation` - Compare against prior runs and references.
12. `12_self_correction_reflection` - Diagnose failures and propose reruns.
13. `13_human_in_the_loop` - Researcher approvals and intervention points.
14. `14_provenance_memory` - Persist full run provenance and decisions.
15. `15_policy_safety_governance` - Policy, licensing, and safety enforcement.
16. `16_agent_mesh_control_plane` - Agent routing, state, resilience controls.
