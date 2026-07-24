"""Smoke-test generator for RunBundles (Story 5.1).

The execution adapter (Story 5.2) runs a bundle's ``inline_tests.py`` *before*
the real ``main.py`` so cheap, fast failures (a missing scientific package, a
syntax error in the generated script) are caught up front instead of after a
long, expensive computation has already started.

This module emits that ``inline_tests.py`` as a **self-contained, stdlib-only**
Python script -- it must run inside a freshly-created bundle directory with
nothing but the interpreter on ``PATH``, so it cannot import from this package
at runtime. The generated script performs three ordered checks:

    1. imports  -- can every required scientific package be imported?
                   (uses ``importlib.util.find_spec`` so it never executes the
                   package; the *first* thing checked, because a missing tool is
                   the most common and cheapest-to-detect failure). Exit code 2.
    2. syntax   -- does ``main.py`` compile? Exit code 3.
    3. smoke    -- (optional) run ``python main.py --smoke`` on tiny sample data
                   and confirm the expected output file was produced. Exit 4.

Exit code 0 means "safe to proceed to the real run".

The module also exposes :func:`missing_imports` -- the same import probe the
generated script uses -- so callers/tests can check dependencies in-process
without spawning a subprocess.
"""
from __future__ import annotations

import importlib.util
import json
from typing import Iterable, List

# Sentinels substituted into the template below. They deliberately use ``@@``
# delimiters (not ``{}``) so they never collide with the braces of the Python
# source we are emitting.
_TEMPLATE = '''#!/usr/bin/env python3
"""Smoke tests for the generated RunBundle (tool: @@TOOL@@).

Runs BEFORE the real execution to catch missing dependencies and syntax errors
early -- a cheap failure instead of a wasted full run.

Exit codes:
    0  all checks passed (safe to run main.py)
    2  a required dependency is missing  ->  pip install -r requirements.txt
    3  main.py has a syntax error
    4  the --smoke run failed or produced no output file

Run:  python inline_tests.py
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAIN = HERE / "@@MAIN@@"
OUTPUT = HERE / "@@OUTPUT@@"
TOOL_NAME = "@@TOOL@@"
RUN_SMOKE = @@RUN_SMOKE@@
REQUIRE_OUTPUT = @@REQUIRE_OUTPUT@@
REQUIRED_IMPORTS = @@IMPORTS@@


def missing_imports(names):
    """Return the subset of module names that cannot be imported."""
    missing = []
    for name in names:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ModuleNotFoundError, ValueError):
            spec = None
        if spec is None:
            missing.append(name)
    return missing


def check_syntax(path):
    """Compile the source at ``path`` -- raises SyntaxError if invalid."""
    source = Path(path).read_text(encoding="utf-8")
    compile(source, str(path), "exec")


def run_smoke(main_path, output_path, timeout=300):
    """Run ``python main.py --smoke`` and report (exit, stdout, stderr, produced).

    The subprocess runs with the bundle directory as its working directory --
    the same convention the execution adapter uses -- so main.py's relative
    input/output/config paths resolve inside the bundle.
    """
    proc = subprocess.run(
        [sys.executable, str(main_path), "--smoke"],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(HERE),
    )
    return proc.returncode, proc.stdout, proc.stderr, Path(output_path).is_file()


def main():
    print(f"[smoke] checking bundle for tool: {TOOL_NAME}")

    # 1) dependency check -- the earliest, cheapest failure to surface.
    missing = missing_imports(REQUIRED_IMPORTS)
    if missing:
        for name in missing:
            print(f"[smoke] MISSING DEPENDENCY: {name}")
        print("[smoke] FAIL: install requirements with `pip install -r requirements.txt`")
        return 2
    if REQUIRED_IMPORTS:
        print(f"[smoke] imports OK: {', '.join(REQUIRED_IMPORTS)}")

    # 2) syntax check -- main.py must at least compile.
    try:
        check_syntax(MAIN)
    except SyntaxError as exc:
        print(f"[smoke] SYNTAX ERROR in {MAIN.name}: {exc}")
        return 3
    print(f"[smoke] syntax OK: {MAIN.name}")

    if not RUN_SMOKE:
        print("[smoke] PASS (import + syntax checks only)")
        return 0

    # 3) tiny end-to-end run on sample data; confirm the output file appears.
    try:
        code, out, err, produced = run_smoke(MAIN, OUTPUT)
    except subprocess.TimeoutExpired:
        print("[smoke] smoke run timed out")
        return 4
    if code != 0:
        print(f"[smoke] smoke run exited with code {code}")
        if err.strip():
            print(err.strip())
        return 4
    if not produced:
        # A load-only smoke (heavy calculator: construct + exit, no compute)
        # legitimately writes nothing -- only a compute smoke owes the file.
        if REQUIRE_OUTPUT:
            print(f"[smoke] expected output not created: {OUTPUT.name}")
            return 4
        print("[smoke] smoke run OK (load-only; no output file expected)")
    else:
        print(f"[smoke] smoke run OK -> {OUTPUT.name}")
    print("[smoke] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def missing_imports(names: Iterable[str]) -> List[str]:
    """Return the module names in ``names`` that cannot be imported.

    Uses ``importlib.util.find_spec`` so no package code is executed -- this only
    answers "is it installed?". This is the exact probe the generated
    ``inline_tests.py`` embeds, exposed here for fast in-process checks.

    >>> missing_imports(["sys", "json"])
    []
    >>> missing_imports(["definitely_not_a_real_module_xyz"])
    ['definitely_not_a_real_module_xyz']
    """
    missing: List[str] = []
    for name in names:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ModuleNotFoundError, ValueError):
            spec = None
        if spec is None:
            missing.append(name)
    return missing


def generate_inline_tests(
    *,
    tool_name: str,
    required_import_names: Iterable[str],
    main_filename: str = "main.py",
    output_filename: str = "results.csv",
    run_smoke: bool = True,
    require_output: bool = True,
) -> str:
    """Render the ``inline_tests.py`` source for a bundle.

    ``required_import_names`` are the *import* names (not PyPI names) of the
    scientific packages whose absence should abort before the real run.
    ``require_output`` should be False for a load-only smoke (heavy
    calculators construct the calculator and exit without computing), where
    no output file is expected.

    >>> src = generate_inline_tests(tool_name="ASE", required_import_names=["ase"])
    >>> "MISSING DEPENDENCY" in src and "ase" in src
    True
    >>> type(compile(src, "inline_tests.py", "exec")).__name__  # syntactically valid
    'code'
    """
    imports = list(required_import_names)
    rendered = _TEMPLATE
    rendered = rendered.replace("@@TOOL@@", str(tool_name))
    rendered = rendered.replace("@@MAIN@@", main_filename)
    rendered = rendered.replace("@@OUTPUT@@", output_filename)
    rendered = rendered.replace("@@RUN_SMOKE@@", "True" if run_smoke else "False")
    rendered = rendered.replace("@@REQUIRE_OUTPUT@@", "True" if require_output else "False")
    # json.dumps yields a valid Python list literal of strings.
    rendered = rendered.replace("@@IMPORTS@@", json.dumps(imports))
    return rendered


if __name__ == "__main__":  # pragma: no cover - manual smoke of the module
    print(generate_inline_tests(tool_name="Pymatgen", required_import_names=["pymatgen"]))
