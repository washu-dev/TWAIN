"""Codegen engine: ExecutionPlan -> runnable RunBundle (Story 5.1).

This is the heart of the code-configuration builder. Given an ``ExecutionPlan``
(the artifact Story 1.3 / the plan synthesizer produces) it emits a **RunBundle**
-- a self-contained directory the execution adapter (Story 5.2) can run with no
manual editing:

    main.py           the generated, executable script (a template with the
                      plan's specifics substituted in)
    config.yaml       parameters, input/output paths, and the SLURM resource
                      request, for the scheduler and for reproducibility
    requirements.txt  pinned Python dependencies (see dependency_inferencer)
    inline_tests.py   smoke tests run *before* the real execution to catch a
                      missing dependency or a syntax error early

How a template becomes ``main.py``
----------------------------------
Templates in ``templates/`` are ordinary, valid Python modules that carry
UPPER_SNAKE placeholder tokens *inside string literals* (``TOOL_NAME =
"{TOOL_NAME}"``) plus two JSON blobs embedded in raw triple-quoted strings
(``_CONFIG_JSON``/``_ACCEPTANCE_JSON``). Because the tokens live in strings and
the heavy scientific import is lazy, a template is valid, importable Python both
*before* and *after* substitution -- so its embedded doctests run in any
environment. Rendering is a set of exact ``str.replace`` calls, and every render
is validated two ways: no placeholder token may remain, and the result must
``compile()``.

Template selection is by tool: Pymatgen and ASE have bespoke templates; RDKit
maps to property-prediction or graph-manipulation depending on the objective;
any other importable tool falls back to a generic runner that loads the tool
dynamically. Nothing here needs the network or an LLM, so bundle generation is
deterministic and reproducible.
"""
from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

# Sibling modules -- import via the package alias when available (tests, the
# state machine), else by bare name when this file is run from its own dir.
try:  # pragma: no cover - import shim
    from code_gen import dependency_inferencer as _depinf
    from code_gen import smoke_test_generator as _smoke
except ImportError:  # pragma: no cover
    import dependency_inferencer as _depinf
    import smoke_test_generator as _smoke

canonical_tool_key = _depinf.canonical_tool_key

# Placeholder tokens the engine fills. Every one must be gone after rendering.
PLACEHOLDER_KEYS = [
    "TOOL_NAME", "TOOL_IMPORT", "MODEL_NAME", "INPUT_FILE", "OUTPUT_FILE",
    "CONFIG_FILE", "GENERATED_AT", "CONFIG_JSON", "ACCEPTANCE_JSON",
]
# Any leftover ``{UPPER_SNAKE}`` token after rendering is an unfilled placeholder
# (a template bug). JSON object braces never match: ``{`` is always followed by
# ``"`` or ``}`` in our emitted JSON, not an uppercase letter.
_LEFTOVER_PLACEHOLDER = re.compile(r"\{[A-Z][A-Z0-9_]{2,}\}")


@dataclass(frozen=True)
class TemplateSpec:
    """A template file plus the defaults used when rendering it."""

    filename: str
    model_name: str
    input_file: str
    output_file: str
    default_params: Dict[str, object] = field(default_factory=dict)


# Tool (canonical key) -> template. RDKit is resolved to one of two templates by
# objective; unmapped tools fall back to the generic runner.
_PYMATGEN = TemplateSpec("template_pymatgen_analysis.py", "structure_analysis", "structure.json", "results.csv", {"round_digits": 4})
_ASE = TemplateSpec("template_molecular_dynamics.py", "emt_md", "system.json", "trajectory.csv", {"steps": 20, "timestep_fs": 1.0, "temperature_K": 300.0})
_RDKIT_PROPERTY = TemplateSpec("template_property_prediction.py", "rdkit_descriptors", "molecules.csv", "predictions.csv", {"round_digits": 4})
_RDKIT_MANIP = TemplateSpec("template_rdkit_manipulation.py", "graph_manipulation", "molecules.smi", "graph_features.csv", {"canonical": True})
_GENERIC = TemplateSpec("template_generic.py", "generic_run", "input.json", "results.csv", {})

_RDKIT_MANIP_HINTS = (
    "manipul", "scaffold", "graph", "canonical", "substructure",
    "fingerprint", "representation", "fragment",
)


# --------------------------------------------------------------------------- #
# Minimal YAML emitter (controlled input -> valid block YAML; no PyYAML needed).
# --------------------------------------------------------------------------- #
def _yaml_scalar(value) -> str:
    """Render a scalar as YAML.

    >>> _yaml_scalar(True), _yaml_scalar(None), _yaml_scalar(3), _yaml_scalar(1.5)
    ('true', 'null', '3', '1.5')
    >>> _yaml_scalar("plain"), _yaml_scalar("has: colon")
    ('plain', '"has: colon"')
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if _needs_quote(text):
        return json.dumps(text)  # double-quoted form is valid YAML
    return text


def _needs_quote(text: str) -> bool:
    """Whether a string must be quoted to round-trip as YAML."""
    if text == "" or text.strip() != text:
        return True
    if text.lower() in {"true", "false", "null", "yes", "no", "on", "off", "~"}:
        return True
    if re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", text):
        return True  # looks like a number
    return any(ch in text for ch in ":#{}[],&*!|>'\"%@`") or text[0] in "-?"


def dump_yaml(obj, _indent: int = 0) -> str:
    """Serialize dict/list/scalar ``obj`` to block-style YAML.

    Handles exactly the shapes the engine emits (nested dicts, lists of scalars,
    lists of flat dicts, scalars). JSON is valid YAML, but this produces the
    readable block form the config file wants.

    >>> print(dump_yaml({"tool": "ASE", "parameters": {"steps": 5}}).strip())
    tool: ASE
    parameters:
      steps: 5
    >>> print(dump_yaml({"notes": ["a", "b"]}).strip())
    notes:
      - a
      - b
    """
    pad = "  " * _indent
    lines: List[str] = []
    if isinstance(obj, dict):
        if not obj:
            return f"{pad}{{}}\n" if _indent == 0 else "{}"
        for key, val in obj.items():
            if isinstance(val, dict) and val:
                lines.append(f"{pad}{key}:")
                lines.append(dump_yaml(val, _indent + 1).rstrip("\n"))
            elif isinstance(val, list) and val:
                lines.append(f"{pad}{key}:")
                lines.append(_dump_list(val, _indent + 1).rstrip("\n"))
            elif isinstance(val, dict):  # empty
                lines.append(f"{pad}{key}: {{}}")
            elif isinstance(val, list):  # empty
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(val)}")
        return "\n".join(lines) + "\n"
    if isinstance(obj, list):
        return _dump_list(obj, _indent)
    return f"{pad}{_yaml_scalar(obj)}\n"


def _dump_list(items: List, indent: int) -> str:
    pad = "  " * indent
    lines: List[str] = []
    for item in items:
        if isinstance(item, dict) and item:
            # "- k: v" on the first key, remaining keys aligned under it.
            keys = list(item.items())
            first_key, first_val = keys[0]
            lines.append(f"{pad}- {first_key}: {_yaml_scalar(first_val)}")
            for k, v in keys[1:]:
                lines.append(f"{pad}  {k}: {_yaml_scalar(v)}")
        else:
            lines.append(f"{pad}- {_yaml_scalar(item)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# RunBundle
# --------------------------------------------------------------------------- #
@dataclass
class RunBundle:
    """The four files that make up a runnable bundle, plus provenance metadata.

    :meth:`write` materializes them into a directory the execution adapter can
    ``pip install -r requirements.txt`` and then ``python main.py`` -- no manual
    edits required.
    """

    main_py: str
    config_yaml: str
    requirements_txt: str
    inline_tests_py: str
    tool_name: str
    template_name: str
    entrypoint: str = "main.py"

    def files(self) -> Dict[str, str]:
        """Map of filename -> contents for the bundle."""
        return {
            "main.py": self.main_py,
            "config.yaml": self.config_yaml,
            "requirements.txt": self.requirements_txt,
            "inline_tests.py": self.inline_tests_py,
        }

    def write(self, dest: Union[str, Path]) -> Path:
        """Write all four files into ``dest`` (created if needed); return it."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for name, content in self.files().items():
            (dest / name).write_text(content, encoding="utf-8")
        return dest


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class CodegenEngine:
    """Turns an ExecutionPlan into a :class:`RunBundle`."""

    def __init__(self, templates_dir: Optional[Union[str, Path]] = None):
        self.templates_dir = (
            Path(templates_dir) if templates_dir
            else Path(__file__).resolve().parent / "templates"
        )

    # -- template selection --------------------------------------------------
    def select_template(self, tool_name: str, *, hint: str = "") -> TemplateSpec:
        """Pick the template for a tool (RDKit disambiguated by ``hint``).

        >>> CodegenEngine().select_template("Pymatgen").filename
        'template_pymatgen_analysis.py'
        >>> CodegenEngine().select_template("ASE").filename
        'template_molecular_dynamics.py'
        >>> CodegenEngine().select_template("rdkit", hint="scaffold graph ops").filename
        'template_rdkit_manipulation.py'
        >>> CodegenEngine().select_template("rdkit").filename
        'template_property_prediction.py'
        >>> CodegenEngine().select_template("VASP").filename
        'template_generic.py'
        """
        key = canonical_tool_key(tool_name)
        hint = (hint or "").lower()
        if key == "pymatgen":
            return _PYMATGEN
        if key == "ase":
            return _ASE
        if key == "rdkit":
            if any(word in hint for word in _RDKIT_MANIP_HINTS):
                return _RDKIT_MANIP
            return _RDKIT_PROPERTY
        return _GENERIC

    # -- rendering -----------------------------------------------------------
    def _render(self, filename: str, substitutions: Dict[str, str]) -> str:
        template_path = self.templates_dir / filename
        text = template_path.read_text(encoding="utf-8")
        for key, value in substitutions.items():
            text = text.replace("{" + key + "}", str(value))
        return text

    @staticmethod
    def _validate_rendered(source: str, filename: str = "main.py") -> None:
        """Guard a rendered script: no leftover placeholder, and it compiles."""
        leftovers = _LEFTOVER_PLACEHOLDER.findall(source)
        if leftovers:
            raise ValueError(
                f"unsubstituted placeholder(s) left in {filename}: {sorted(set(leftovers))}"
            )
        compile(source, filename, "exec")  # raises SyntaxError if invalid

    # -- plan normalization --------------------------------------------------
    @staticmethod
    def _plan_to_dict(plan) -> dict:
        if isinstance(plan, (str, Path)):
            return json.loads(Path(plan).read_text(encoding="utf-8"))
        if dataclasses.is_dataclass(plan) and not isinstance(plan, type):
            return dataclasses.asdict(plan)
        if isinstance(plan, dict):
            return plan
        raise TypeError(f"unsupported ExecutionPlan type: {type(plan)!r}")

    @staticmethod
    def _hint_from(plan: dict, intent: Optional[dict]) -> str:
        parts: List[str] = []
        if isinstance(intent, dict):
            parts.append(str(intent.get("objective", "")))
        for metric in plan.get("acceptance_metrics", []) or []:
            if isinstance(metric, dict):
                parts.append(str(metric.get("metric_name", "")))
        return " ".join(parts)

    # -- main entry ----------------------------------------------------------
    def generate(self, plan, *, intent: Optional[dict] = None) -> RunBundle:
        """Build a :class:`RunBundle` from an ExecutionPlan.

        ``plan`` may be an ``ExecutionPlan`` dataclass, a plain dict, or a path
        to the plan's JSON artifact. ``intent`` (optional) sharpens RDKit
        template selection via the objective text.
        """
        plan = self._plan_to_dict(plan)
        method = plan.get("selected_method", {}) or {}
        tool_name = method.get("tool_name") or "unknown-tool"

        spec = self.select_template(tool_name, hint=self._hint_from(plan, intent))
        tool_deps = _depinf.import_names(tool_name)
        tool_import = tool_deps[0].import_name if tool_deps else canonical_tool_key(tool_name)
        generated_at = (plan.get("metadata") or {}).get("timestamp", "") or ""
        acceptance = plan.get("acceptance_metrics", []) or []
        params = dict(spec.default_params)

        substitutions = {
            "TOOL_NAME": tool_name,
            "TOOL_IMPORT": tool_import,
            "MODEL_NAME": spec.model_name,
            "INPUT_FILE": spec.input_file,
            "OUTPUT_FILE": spec.output_file,
            "CONFIG_FILE": "config.yaml",
            "GENERATED_AT": generated_at,
            "CONFIG_JSON": json.dumps(params),
            "ACCEPTANCE_JSON": json.dumps(acceptance),
        }
        main_py = self._render(spec.filename, substitutions)
        self._validate_rendered(main_py, "main.py")

        config_yaml = dump_yaml(
            self._config_doc(plan, spec, tool_name, tool_import, params, generated_at, acceptance)
        )
        requirements_txt = _depinf.requirements_txt(tool_name)
        import_names = [dep.import_name for dep in tool_deps]
        inline_tests_py = _smoke.generate_inline_tests(
            tool_name=tool_name,
            required_import_names=import_names,
            main_filename="main.py",
            output_filename=spec.output_file,
            run_smoke=True,
        )

        return RunBundle(
            main_py=main_py,
            config_yaml=config_yaml,
            requirements_txt=requirements_txt,
            inline_tests_py=inline_tests_py,
            tool_name=tool_name,
            template_name=spec.filename,
        )

    @staticmethod
    def _config_doc(plan, spec, tool_name, tool_import, params, generated_at, acceptance) -> dict:
        slurm = plan.get("slurm_request", {}) or {}
        return {
            "tool": tool_name,
            "tool_import": tool_import,
            "template": spec.filename,
            "model": spec.model_name,
            "generated_at": generated_at,
            "entrypoint": "main.py",
            "paths": {"input": spec.input_file, "output": spec.output_file},
            "parameters": params,
            "resources": {
                "cpu_count": slurm.get("cpu_count"),
                "gpu_count": slurm.get("gpu_count"),
                "max_time_hours": slurm.get("max_time"),
                "ram_gb": slurm.get("ram"),
            },
            "acceptance_criteria": [
                {
                    "metric_name": m.get("metric_name"),
                    "target_value": m.get("target_value"),
                    "tolerance": m.get("tolerance"),
                }
                for m in acceptance if isinstance(m, dict)
            ],
            "safety_notes": list(plan.get("safety_notes", []) or []),
        }


if __name__ == "__main__":  # pragma: no cover - manual smoke of the module
    import sys

    if len(sys.argv) < 2:
        print("Usage: python codegen_engine.py <execution_plan.json> [dest_dir]")
        raise SystemExit(1)
    engine = CodegenEngine()
    bundle = engine.generate(sys.argv[1])
    dest = sys.argv[2] if len(sys.argv) > 2 else "run_bundle"
    path = bundle.write(dest)
    print(f"RunBundle for {bundle.tool_name} ({bundle.template_name}) -> {path}")
