#!/usr/bin/env python3
"""Generic tool-runner template (TWAIN code_configuration_builder fallback).

WHAT THIS SCRIPT DOES
    A tool-agnostic fallback used when no tool-specific template applies. It
    imports the selected tool *dynamically* (by its import name), records the
    tool version, echoes the plan's acceptance criteria, and writes a small
    results CSV. It proves the selected tool loads and runs in the target
    environment and gives downstream stages a real, inspectable artifact to
    build on -- even for tools without a bespoke template yet.

INPUTS
    --input   Optional JSON input (ignored by the generic scaffold beyond
              recording its presence).
    --config  config.yaml; its ``parameters`` block overrides baked defaults
              when PyYAML is available.

OUTPUTS
    * A results CSV at --output (tool, version, criteria count).
    * A JSON summary on stdout for the downstream interpreter/validator.

The tool import is dynamic and lazy (inside :func:`run`), so this module imports
and its doctests run even when the tool is absent -- the smoke tests report the
missing dependency instead.

Run:  python main.py            # loads the tool, writes results
      python main.py --smoke    # same, minimal
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from pathlib import Path

# --- substitution placeholders (filled in by CodegenEngine) ------------------
TOOL_NAME = "{TOOL_NAME}"        # human-readable tool name
TOOL_IMPORT = "{TOOL_IMPORT}"    # importable module dynamically loaded at run time
MODEL_NAME = "{MODEL_NAME}"      # method label
INPUT_FILE = "{INPUT_FILE}"      # default input path
OUTPUT_FILE = "{OUTPUT_FILE}"    # default results output path (CSV)
CONFIG_FILE = "{CONFIG_FILE}"    # runtime config path (YAML)
GENERATED_AT = "{GENERATED_AT}"  # originating plan timestamp (provenance)
_CONFIG_JSON = r"""{CONFIG_JSON}"""        # baked hyperparameters (JSON)
_ACCEPTANCE_JSON = r"""{ACCEPTANCE_JSON}"""  # baked acceptance criteria (JSON)


# ─────────────────────────── pure helpers (tool-free) ───────────────────────
def _parse_json(raw, fallback):
    """Parse an embedded JSON blob, tolerating an unsubstituted placeholder.

    >>> _parse_json('[1, 2]', [])
    [1, 2]
    >>> _parse_json('{ACCEPTANCE_JSON}', [])
    []
    """
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return fallback
    return value if value is not None else fallback


def _load_config(config_file, defaults):
    """Overlay ``config.yaml``'s ``parameters`` over ``defaults`` (PyYAML optional).

    >>> _load_config("no_such_config_xyz.yaml", {"n": 1})
    {'n': 1}
    """
    cfg = dict(defaults)
    try:
        import yaml  # optional
    except ImportError:
        return cfg
    path = Path(config_file)
    if not path.is_file():
        return cfg
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return cfg
    params = doc.get("parameters", {}) if isinstance(doc, dict) else {}
    if isinstance(params, dict):
        cfg.update(params)
    return cfg


def _rows_to_csv_text(rows, fieldnames):
    """Render dict ``rows`` to CSV text with a header (stdlib only).

    >>> print(_rows_to_csv_text([{"tool": "X", "version": "1.0"}],
    ...                          ["tool", "version"]).strip())
    tool,version
    X,1.0
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _check_acceptance(metrics, criteria):
    """Compare computed ``metrics`` against acceptance ``criteria``.

    >>> _check_acceptance(
    ...     {"n_criteria": 1},
    ...     [{"metric_name": "n_criteria", "target_value": 1, "tolerance": 0}],
    ... )
    {'n_criteria': {'value': 1, 'target': 1, 'tolerance': 0, 'passed': True}}

    A metric the plan named but gave no number for is reported, not judged
    -- passed is None, which is neither a pass nor a fail:

    >>> _check_acceptance(
    ...     {"n_criteria": 1},
    ...     [{"metric_name": "n_criteria", "target_value": None,
    ...       "tolerance": None}],
    ... )
    {'n_criteria': {'value': 1, 'target': None, 'tolerance': None, 'passed': None}}
    """
    report = {}
    for crit in criteria or []:
        name = crit.get("metric_name")
        if name is None or name not in metrics:
            continue
        value = metrics[name]
        # A plan may name a metric with no number to hit, because the researcher
        # gave none. .get's default cannot cover that case: the key is present and
        # null, so it returns None and float(None) would crash the run *after* it
        # had already spent its allocation. Report the value as unchecked --
        # passed=None, distinct from both a real pass and a real fail.
        target = crit.get("target_value")
        tol = crit.get("tolerance")
        report[name] = {
            "value": value,
            "target": target,
            "tolerance": tol,
            "passed": (None if target is None or tol is None
                       else abs(float(value) - float(target)) <= float(tol)),
        }
    return report


# ───────────────────────────── defaults from plan ───────────────────────────
DEFAULTS = _parse_json(_CONFIG_JSON, {})
ACCEPTANCE = _parse_json(_ACCEPTANCE_JSON, [])


# ─────────────────────────────── real work ──────────────────────────────────
def run(config, input_path, output_path, *, smoke=False):
    """Dynamically import the selected tool, record its version, write a CSV.

    Returns summary metrics as a plain dict.
    """
    import importlib

    module = importlib.import_module(TOOL_IMPORT)  # raises ImportError if missing
    version = getattr(module, "__version__", "unknown")

    metrics = {
        "tool": TOOL_NAME,
        "tool_import": TOOL_IMPORT,
        "tool_version": str(version),
        "model": MODEL_NAME,
        "n_criteria": len(ACCEPTANCE),
        "input_present": bool(Path(input_path).is_file()) and not smoke,
    }
    fieldnames = list(metrics.keys())
    Path(output_path).write_text(_rows_to_csv_text([metrics], fieldnames), encoding="utf-8")
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=f"{TOOL_NAME} generic runner ({MODEL_NAME}).")
    parser.add_argument("--input", default=INPUT_FILE, help="optional JSON input")
    parser.add_argument("--output", default=OUTPUT_FILE, help="results CSV output")
    parser.add_argument("--config", default=CONFIG_FILE, help="config.yaml with parameters")
    parser.add_argument("--smoke", action="store_true", help="minimal sanity run")
    args = parser.parse_args(argv)

    config = _load_config(args.config, DEFAULTS)
    metrics = run(config, args.input, args.output, smoke=args.smoke)

    summary = dict(metrics)
    summary["acceptance"] = _check_acceptance(metrics, ACCEPTANCE)
    summary["output_file"] = str(args.output)
    json.dump(summary, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    # Anchor relative paths (results, tool scratch files) to the bundle dir so
    # a manual run from anywhere doesn't litter the caller's working directory.
    os.chdir(Path(__file__).resolve().parent)
    raise SystemExit(main())
