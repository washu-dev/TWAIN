"""Unit tests for the code_configuration_builder (Story 5.1).

Covers the four acceptance-test requirements and the definition of done:

  * Generated scripts are syntactically valid Python  (TestGeneratedScriptValidity)
  * Generated scripts run without errors on sample data (TestRunsOnSampleData)
  * Inline tests detect missing dependencies            (TestSmokeDetectsMissingDeps)
  * RunBundle executes without manual edits / smoke passes before real execution
                                                         (TestDefinitionOfDone)

plus the supporting units: dependency inference + PyPI availability, template
library (existence + doctests + placeholders), template selection, the YAML
emitter, and RunBundle assembly.

Run from the repo root with:  pixi run pytest tests/unit/test_codegen.py
"""
import doctest
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

from code_gen import codegen_engine as engine_mod
from code_gen import dependency_inferencer as depinf
from code_gen import smoke_test_generator as smoke
from code_gen.codegen_engine import CodegenEngine, RunBundle, dump_yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "modules" / "06_code_configuration_builder" / "templates"
EXAMPLE_PLAN = REPO_ROOT / "schemas" / "examples" / "execution_plan_example.json"

TEMPLATE_FILES = [
    "template_property_prediction.py",
    "template_molecular_dynamics.py",
    "template_rdkit_manipulation.py",
    "template_pymatgen_analysis.py",
    "template_generic.py",
]


def make_plan(tool_name, *, version=1.0, metrics=None, safety=None):
    """A minimal, schema-shaped ExecutionPlan dict for a given tool."""
    return {
        "selected_method": {"tool_name": tool_name, "tool_version": version},
        "compute_estimate": {"cpu_hours": 1.0},
        "slurm_request": {"cpu_count": 8, "gpu_count": 1, "max_time": 24.0, "ram": 16},
        "cost_estimate": {"min_tokens": 100, "min_cost": 1.0},
        "metadata": {"timestamp": "2026-06-15T12:00:00Z", "goal_id": "g1", "candidate_rank": 1},
        "acceptance_metrics": metrics if metrics is not None else [
            {"metric_name": "density", "target_value": 7.8, "tolerance": 0.5}
        ],
        "safety_notes": safety if safety is not None else ["Verify SLURM partition limits"],
    }


# ═══════════════════════════════════════════════════════════════════════════
# Dependency inference
# ═══════════════════════════════════════════════════════════════════════════
class TestDependencyInference:
    def test_pymatgen_pinned(self):
        deps = depinf.infer("Pymatgen")
        lines = [d.requirement_line() for d in deps]
        assert "pymatgen==2024.6.10" in lines
        assert all("==" in ln for ln in lines), "every dependency must be version-pinned"

    def test_ase_pinned(self):
        lines = [d.requirement_line() for d in depinf.infer("ASE")]
        assert "ase==3.23.0" in lines

    def test_deepchem_maps_to_package(self):
        # "deepchem" -> pip install deepchem (+ its sklearn extra).
        names = [d.package for d in depinf.infer("deepchem")]
        assert "deepchem" in names
        assert "scikit-learn" in names

    def test_common_runtime_always_present(self):
        for tool in ("Pymatgen", "ASE", "rdkit", "deepchem"):
            packages = {d.package for d in depinf.infer(tool)}
            assert {"numpy", "pandas", "PyYAML"} <= packages

    def test_unknown_tool_is_unpinned_but_usable(self):
        deps = depinf.infer("TotallyMadeUpTool")
        tool_dep = deps[0]
        assert tool_dep.pinned is False
        assert tool_dep.version is None
        # runtime packages still present
        assert {"numpy", "pandas", "PyYAML"} <= {d.package for d in deps}

    def test_import_names_differ_from_pypi_names(self):
        # scikit-learn installs as 'scikit-learn' but imports as 'sklearn'.
        names = depinf.import_names("scikit-learn")
        assert names[0].package == "scikit-learn"
        assert names[0].import_name == "sklearn"

    def test_requirements_txt_shape(self):
        txt = depinf.requirements_txt("Pymatgen")
        assert txt.startswith("#")
        assert "pymatgen==2024.6.10" in txt
        assert txt.endswith("\n")


class TestModuleDoctests:
    """The engine/inferencer/generator carry doctests; run them here so a plain
    ``pytest tests/`` (no --doctest-modules) still exercises them."""

    @pytest.mark.parametrize("module", [depinf, smoke, engine_mod], ids=lambda m: m.__name__)
    def test_module_doctests_pass(self, module):
        result = doctest.testmod(module, verbose=False)
        assert result.failed == 0, f"{module.__name__}: {result.failed} doctest failure(s)"


class TestPyPIAvailability:
    """The PyPI existence check, exercised offline via an injected fetch."""

    @staticmethod
    def _fake_fetch(catalog):
        def fetch(url):
            name = url.split("/pypi/")[1].split("/")[0]
            if name in catalog:
                return catalog[name]
            raise HTTPError(url, 404, "Not Found", {}, None)
        return fetch

    def test_found_package(self):
        fetch = self._fake_fetch({"pymatgen": {"releases": {"2024.6.10": []}}})
        assert depinf.is_available_on_pypi("pymatgen", fetch=fetch) is True

    def test_found_version(self):
        fetch = self._fake_fetch({"ase": {"releases": {"3.23.0": [], "3.22.1": []}}})
        assert depinf.is_available_on_pypi("ase", "3.23.0", fetch=fetch) is True
        assert depinf.is_available_on_pypi("ase", "9.9.9", fetch=fetch) is False

    def test_missing_package_is_false(self):
        fetch = self._fake_fetch({})
        assert depinf.is_available_on_pypi("no-such-pkg", fetch=fetch) is False

    def test_network_error_is_none(self):
        def boom(url):
            raise HTTPError(url, 503, "Service Unavailable", {}, None)
        assert depinf.is_available_on_pypi("pymatgen", fetch=boom) is None

    def test_check_dependencies_maps_each_requirement(self):
        catalog = {
            "pymatgen": {"releases": {"2024.6.10": []}},
            "numpy": {"releases": {"1.26.4": []}},
            "pandas": {"releases": {"2.2.2": []}},
            "PyYAML": {"releases": {"6.0.2": []}},
        }
        report = depinf.check_dependencies("Pymatgen", fetch=self._fake_fetch(catalog))
        assert report["pymatgen==2024.6.10"] is True
        assert all(v is True for v in report.values())


# ═══════════════════════════════════════════════════════════════════════════
# Template library
# ═══════════════════════════════════════════════════════════════════════════
class TestTemplateLibrary:
    def test_required_templates_exist(self):
        # The three named templates from the story, plus the pymatgen + generic ones.
        for name in TEMPLATE_FILES:
            assert (TEMPLATES_DIR / name).is_file(), f"missing template {name}"

    @pytest.mark.parametrize("name", TEMPLATE_FILES)
    def test_raw_template_is_valid_python(self, name):
        # Templates must be valid, importable Python *before* substitution too
        # (placeholders live inside string literals; heavy imports are lazy).
        source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        compile(source, name, "exec")

    @pytest.mark.parametrize("name", TEMPLATE_FILES)
    def test_template_has_placeholders_and_docstring(self, name):
        source = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        assert '"""' in source and "INPUTS" in source and "OUTPUTS" in source
        assert "{TOOL_NAME}" in source and "{INPUT_FILE}" in source and "{OUTPUT_FILE}" in source

    @pytest.mark.parametrize("name", TEMPLATE_FILES)
    def test_template_doctests_pass(self, name):
        # The "unit tests embedded as doctests" acceptance item.
        spec = importlib.util.spec_from_file_location(name[:-3], TEMPLATES_DIR / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = doctest.testmod(module, verbose=False)
        assert result.failed == 0, f"{name}: {result.failed} doctest failure(s)"


# ═══════════════════════════════════════════════════════════════════════════
# Template selection + YAML emitter
# ═══════════════════════════════════════════════════════════════════════════
class TestTemplateSelection:
    @pytest.mark.parametrize("tool,expected", [
        ("Pymatgen", "template_pymatgen_analysis.py"),
        ("pymatgen", "template_pymatgen_analysis.py"),
        ("ASE", "template_molecular_dynamics.py"),
        ("rdkit", "template_property_prediction.py"),
        ("VASP", "template_generic.py"),
        ("deepchem", "template_generic.py"),
    ])
    def test_selection_by_tool(self, tool, expected):
        assert CodegenEngine().select_template(tool).filename == expected

    def test_rdkit_manipulation_hint(self):
        spec = CodegenEngine().select_template("rdkit", hint="compute Murcko scaffold graph")
        assert spec.filename == "template_rdkit_manipulation.py"


class TestYamlEmitter:
    def test_round_trips_via_pyyaml(self):
        yaml = pytest.importorskip("yaml")
        doc = {
            "tool": "ASE",
            "note": "has: a colon",
            "flag": True,
            "empty_map": {},
            "empty_list": [],
            "parameters": {"steps": 5, "temp": 300.0},
            "items": ["a", "b"],
            "criteria": [{"metric_name": "energy", "target_value": -5.2, "tolerance": 0.1}],
        }
        loaded = yaml.safe_load(dump_yaml(doc))
        assert loaded == doc

    def test_scalar_quoting(self):
        assert dump_yaml("plain").strip() == "plain"
        assert dump_yaml("has: colon").strip() == '"has: colon"'


# ═══════════════════════════════════════════════════════════════════════════
# RunBundle assembly
# ═══════════════════════════════════════════════════════════════════════════
class TestRunBundleAssembly:
    def test_generate_from_shipped_example(self):
        plan = json.loads(EXAMPLE_PLAN.read_text(encoding="utf-8"))
        bundle = CodegenEngine().generate(plan)
        assert isinstance(bundle, RunBundle)
        assert set(bundle.files()) == {"main.py", "config.yaml", "requirements.txt", "inline_tests.py"}

    def test_write_materializes_four_files(self, tmp_path):
        bundle = CodegenEngine().generate(make_plan("Pymatgen"))
        dest = bundle.write(tmp_path / "bundle")
        for name in ("main.py", "config.yaml", "requirements.txt", "inline_tests.py"):
            assert (dest / name).is_file()

    def test_accepts_execution_plan_dataclass(self):
        # generate() must also accept the ExecutionPlan dataclass, not just a dict.
        from plan_synthesizer.execution_plan import ExecutionPlan
        plan_obj = ExecutionPlan(**make_plan("Pymatgen"))
        bundle = CodegenEngine().generate(plan_obj)
        assert bundle.tool_name == "Pymatgen"

    def test_config_yaml_carries_params_paths_resources(self):
        yaml = pytest.importorskip("yaml")
        bundle = CodegenEngine().generate(make_plan("Pymatgen"))
        doc = yaml.safe_load(bundle.config_yaml)
        assert doc["resources"]["cpu_count"] == 8
        assert doc["resources"]["ram_gb"] == 16
        assert doc["paths"]["output"] == "results.csv"
        assert doc["parameters"]  # non-empty
        assert doc["acceptance_criteria"][0]["metric_name"] == "density"

    def test_requirements_includes_tool(self):
        bundle = CodegenEngine().generate(make_plan("ASE"))
        assert "ase==3.23.0" in bundle.requirements_txt


# ═══════════════════════════════════════════════════════════════════════════
# AC: Generated scripts are syntactically valid Python
# ═══════════════════════════════════════════════════════════════════════════
class TestGeneratedScriptValidity:
    @pytest.mark.parametrize("tool", ["Pymatgen", "ASE", "rdkit", "deepchem", "VASP"])
    def test_generated_main_compiles(self, tool):
        bundle = CodegenEngine().generate(make_plan(tool))
        compile(bundle.main_py, "main.py", "exec")  # raises SyntaxError if invalid

    @pytest.mark.parametrize("tool", ["Pymatgen", "ASE", "rdkit"])
    def test_no_unsubstituted_placeholders(self, tool):
        bundle = CodegenEngine().generate(make_plan(tool))
        assert "{TOOL_NAME}" not in bundle.main_py
        assert "{CONFIG_JSON}" not in bundle.main_py
        assert tool.lower() in bundle.main_py.lower()

    def test_all_generated_files_compile(self):
        bundle = CodegenEngine().generate(make_plan("Pymatgen"))
        compile(bundle.main_py, "main.py", "exec")
        compile(bundle.inline_tests_py, "inline_tests.py", "exec")

    def test_validate_rendered_rejects_leftover_placeholder(self):
        with pytest.raises(ValueError, match="unsubstituted placeholder"):
            CodegenEngine._validate_rendered('X = "{NOT_FILLED}"\n', "main.py")

    def test_validate_rendered_rejects_syntax_error(self):
        with pytest.raises(SyntaxError):
            CodegenEngine._validate_rendered("def broken(:\n", "main.py")


# ═══════════════════════════════════════════════════════════════════════════
# AC: Generated scripts run without errors on sample data
# ═══════════════════════════════════════════════════════════════════════════
class TestRunsOnSampleData:
    def test_pymatgen_main_runs_and_writes_csv(self, tmp_path):
        pytest.importorskip("pymatgen")
        bundle = CodegenEngine().generate(make_plan("Pymatgen"))
        dest = bundle.write(tmp_path / "pmg")
        proc = subprocess.run(
            [sys.executable, "main.py", "--smoke"],
            cwd=dest, capture_output=True, text=True, timeout=300,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["tool"] == "Pymatgen"
        assert (dest / "results.csv").is_file()

    def test_generic_main_runs_on_stdlib_tool(self, tmp_path):
        # tool_name "json" resolves to a stdlib import -> the generic runner
        # executes end-to-end with zero external deps, in any environment.
        bundle = CodegenEngine().generate(make_plan("json"))
        dest = bundle.write(tmp_path / "gen")
        proc = subprocess.run(
            [sys.executable, "main.py", "--smoke"],
            cwd=dest, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert (dest / "results.csv").is_file()


# ═══════════════════════════════════════════════════════════════════════════
# AC: Inline tests detect missing dependencies
# ═══════════════════════════════════════════════════════════════════════════
class TestSmokeDetectsMissingDeps:
    def test_missing_imports_helper(self):
        assert smoke.missing_imports(["sys", "json"]) == []
        assert smoke.missing_imports(["a_module_that_is_not_installed_zzz"]) == \
            ["a_module_that_is_not_installed_zzz"]

    def test_generated_smoke_script_is_valid(self):
        src = smoke.generate_inline_tests(tool_name="ASE", required_import_names=["ase"])
        compile(src, "inline_tests.py", "exec")
        assert "MISSING DEPENDENCY" in src

    def test_bundle_smoke_detects_guaranteed_missing_dep(self, tmp_path):
        # A fabricated tool name -> guaranteed-missing import -> exit code 2,
        # deterministically, regardless of what is installed in the test env.
        missing_tool = "twain_nonexistent_pkg_zzz"
        bundle = CodegenEngine().generate(make_plan(missing_tool))
        dest = bundle.write(tmp_path / "missing")
        proc = subprocess.run(
            [sys.executable, "inline_tests.py"],
            cwd=dest, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "MISSING DEPENDENCY" in proc.stdout
        assert missing_tool in proc.stdout

    @pytest.mark.skipif(
        importlib.util.find_spec("ase") is not None,
        reason="ASE is installed here, so the missing-dependency path can't be shown",
    )
    def test_ase_bundle_reports_missing_ase(self, tmp_path):
        bundle = CodegenEngine().generate(make_plan("ASE"))
        dest = bundle.write(tmp_path / "ase")
        proc = subprocess.run(
            [sys.executable, "inline_tests.py"],
            cwd=dest, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 2
        assert "ase" in proc.stdout


# ═══════════════════════════════════════════════════════════════════════════
# Definition of Done: bundle runs without manual edits; smoke passes first
# ═══════════════════════════════════════════════════════════════════════════
class TestDefinitionOfDone:
    def test_pymatgen_bundle_smoke_passes_before_execution(self, tmp_path):
        # inline_tests.py runs imports + syntax checks and then main.py --smoke;
        # exit 0 means the bundle is executable with no manual edits.
        pytest.importorskip("pymatgen")
        bundle = CodegenEngine().generate(make_plan("Pymatgen"))
        dest = bundle.write(tmp_path / "dod")
        proc = subprocess.run(
            [sys.executable, "inline_tests.py"],
            cwd=dest, capture_output=True, text=True, timeout=300,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "PASS" in proc.stdout
