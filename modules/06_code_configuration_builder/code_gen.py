"""Legacy LLM-based script generator (pre-Story 5.1).

Prompts an agent to write a one-off Python script from the intent + plan. This
is retained for reference and ad-hoc use, but the pipeline's BUILD stage now
uses the deterministic, template-based :class:`codegen_engine.CodegenEngine`,
which emits a full, self-testing :class:`~codegen_engine.RunBundle` (main.py,
config.yaml, requirements.txt, inline_tests.py) with no LLM dependency.
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import twain_paths



class CodeGen:


    def __init__(self,session_id: str,*, artifacts_dir: Optional[str | Path] = None, artifact_paths: Optional[dict[str, str]] = None, agent=None):
        self.session_id = session_id
        self.artifacts_dir = Path(artifacts_dir) if artifacts_dir else twain_paths.ARTIFACTS_DIR
        self._artifact_paths = artifact_paths or {}
        self._agent = agent
        self._intent_spec: Optional[dict] = None
        self._execution_plan: Optional[dict] = None

    def _resolve_artifact_path(self, name: str) -> Path:
        if name in self._artifact_paths:
            return Path(self._artifact_paths[name])
        return self.artifacts_dir / f"{name}_{self.session_id}.json"

    def _load_artifact(self, name: str) -> Optional[dict]:
        path = self._resolve_artifact_path(name)
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @property
    def intent_spec(self) -> dict:
        if self._intent_spec is None:
            self._intent_spec = self._load_artifact("intent_spec")
            if self._intent_spec is None:
                raise FileNotFoundError(
                    f"intent_spec artifact not found: {self._resolve_artifact_path('intent_spec')}"
                )
        return self._intent_spec

    @property
    def execution_plan(self) -> dict:
        if self._execution_plan is None:
            self._execution_plan = self._load_artifact("execution_plan")
            if self._execution_plan is None:
                raise FileNotFoundError(
                    f"execution_plan artifact not found: {self._resolve_artifact_path('execution_plan')}"
                )
        return self._execution_plan

    def create_prompt(self) -> str:
        intent = self.intent_spec
        plan = self.execution_plan

        tool = plan["selected_method"]["tool_name"]
        version = plan["selected_method"]["tool_version"]
        sysd = intent.get("system_descriptors", {}) or {}
        molecule = sysd.get("molecule", {}) or {}
        crystal = sysd.get("crystal", {}) or {}
        objective = intent.get("objective", "")
        domain = intent.get("domain", "")
        formula = sysd.get("formula", "")
        # Describe the system as EITHER a crystal (periodic solid, no SMILES) or a
        # molecule, so a solid-state request isn't mislabelled with an invented SMILES.
        if crystal:
            phase = crystal.get("phase")
            cname = crystal.get("name") or crystal.get("formula") or formula
            system_line = f"CRYSTAL: {phase + ' ' if phase else ''}{cname} (formula: {formula})"
        else:
            mol_name = molecule.get("name", "unknown")
            smiles = molecule.get("SMILES", "")
            system_line = f"MOLECULE: {mol_name} (SMILES: {smiles})"
        acceptance = json.dumps(plan.get("acceptance_metrics", []), indent=2)
        slurm = plan.get("slurm_request", {})
        safety = plan.get("safety_notes", [])

        prompt = (
            "You are a computational chemistry code generator. "
            "Write a complete, self-contained Python script that accomplishes the "
            "following task.\n\n"
            f"OBJECTIVE: {objective}\n"
            f"DOMAIN: {domain}\n"
            f"TOOL/LIBRARY: {tool} (version {version})\n"
            f"{system_line}\n"
            f"FORMULA: {formula}\n\n"
            f"ACCEPTANCE METRICS (the script must compute and print these):\n{acceptance}\n\n"
        )

        prompt += (
            "REQUIREMENTS:\n"
            "1. The script must be executable with `python script.py`.\n"
            "2. Import only the specified tool/library and Python standard library modules.\n"
            "3. Print results as JSON to stdout with keys matching the acceptance metric names.\n"
            "4. Include error handling for import failures and computation errors.\n"
            "5. Do NOT include any markdown formatting — output raw Python only.\n\n"
            "Your response MUST be valid Python source code and nothing else. "
            "Do not wrap it in triple backticks or add any explanation."
        )
        return prompt

    def generate(self, *, agent=None):
        if agent is not None:
            self._agent = agent
        if self._agent is None:
            from AgentInterface import AgentInterface
            self._agent = AgentInterface()

        prompt = self.create_prompt()
        response = self._agent.callAgent(prompt)
        print(response)
        text = response["content"][0]["text"]

        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)
        path = self.artifacts_dir / f"script_{self.session_id}.py"
        path.write_text(text, encoding="utf-8")
        return text


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python code_gen.py <session_id>")
        sys.exit(1)
    session_id = sys.argv[1]
    gen = CodeGen(session_id)
    gen.generate()
    print(f"Script written to: {gen.artifacts_dir / f'script_{session_id}.py'}")
