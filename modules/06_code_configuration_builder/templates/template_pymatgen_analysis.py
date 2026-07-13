#!/usr/bin/env python3
"""Pymatgen structure-analysis template (TWAIN code_configuration_builder).

WHAT THIS SCRIPT DOES
    Loads a crystal/molecular structure, uses Pymatgen to compute a set of
    materials properties (formula, site count, cell volume, density, lattice
    parameters), checks them against the plan's acceptance criteria, and saves
    the results to a CSV.

INPUTS
    --input   JSON describing the structure, shaped like the TWAIN Pymatgen
              contract::

                  {"lattice": [[a,0,0],[0,b,0],[0,0,c]],
                   "atoms":   [{"species": "Fe", "coordinates": [0, 0, 0]},
                               {"species": "Fe", "coordinates": [0.5,0.5,0.5]}],
                   "coordinateSystem": "fractional"}

              When --smoke is passed (or the file is absent) a tiny built-in
              sample structure (BCC iron) is used instead.
    --config  config.yaml; its ``parameters`` block overrides the baked-in
              defaults when PyYAML is available.

OUTPUTS
    * A CSV at --output with one row of computed properties.
    * A JSON summary on stdout whose keys match the acceptance-metric names, so
      the interpreter/validator downstream can consume it directly.

The heavy Pymatgen import lives inside :func:`run` so this module imports (and
its doctests run) even in an environment where Pymatgen is not installed -- the
smoke tests are what surface that as a missing dependency.

Run:  python main.py            # real run on --input
      python main.py --smoke    # tiny sample-data sanity run
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
# Each is a string literal so this file is valid, importable Python whether or
# not the placeholders have been substituted yet.
TOOL_NAME = "{TOOL_NAME}"        # human-readable tool name, e.g. "Pymatgen"
TOOL_IMPORT = "{TOOL_IMPORT}"    # importable module, e.g. "pymatgen"
MODEL_NAME = "{MODEL_NAME}"      # analysis/method label, e.g. "structure_analysis"
INPUT_FILE = "{INPUT_FILE}"      # default structure input path (JSON)
OUTPUT_FILE = "{OUTPUT_FILE}"    # default results output path (CSV)
CONFIG_FILE = "{CONFIG_FILE}"    # runtime config path (YAML)
GENERATED_AT = "{GENERATED_AT}"  # originating plan timestamp (provenance)
_CONFIG_JSON = r"""{CONFIG_JSON}"""        # baked hyperparameters (JSON)
_ACCEPTANCE_JSON = r"""{ACCEPTANCE_JSON}"""  # baked acceptance criteria (JSON)
_STRUCTURE_JSON = r"""{STRUCTURE_JSON}"""  # target structure from the IntentSpec (JSON)


# ─────────────────────────── pure helpers (tool-free) ───────────────────────
def _parse_json(raw, fallback):
    """Parse an embedded JSON blob, tolerating an unsubstituted placeholder.

    >>> _parse_json('{"steps": 3}', {})
    {'steps': 3}
    >>> _parse_json('{CONFIG_JSON}', {'steps': 10})
    {'steps': 10}
    """
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return fallback
    return value if value is not None else fallback


def _load_config(config_file, defaults):
    """Overlay ``config.yaml``'s ``parameters`` over ``defaults`` (PyYAML optional).

    Missing file or missing PyYAML simply yields the defaults, so the script is
    runnable with only the standard library.

    >>> _load_config("no_such_config_xyz.yaml", {"steps": 5})
    {'steps': 5}
    """
    cfg = dict(defaults)
    try:
        import yaml  # optional; only used to honour an edited config.yaml
    except ImportError:
        return cfg
    path = Path(config_file)
    if not path.is_file():
        return cfg
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a malformed config falls back to defaults
        return cfg
    params = doc.get("parameters", {}) if isinstance(doc, dict) else {}
    if isinstance(params, dict):
        cfg.update(params)
    return cfg


def _rows_to_csv_text(rows, fieldnames):
    """Render dict ``rows`` to CSV text with a header (stdlib only).

    >>> print(_rows_to_csv_text([{"a": 1, "b": 2}], ["a", "b"]).strip())
    a,b
    1,2
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _check_acceptance(metrics, criteria):
    """Compare computed ``metrics`` against acceptance ``criteria``.

    Each criterion is ``{"metric_name", "target_value", "tolerance"}``; a metric
    passes when ``abs(value - target) <= tolerance``.

    >>> _check_acceptance(
    ...     {"volume": 23.6},
    ...     [{"metric_name": "volume", "target_value": 23.5, "tolerance": 0.2}],
    ... )
    {'volume': {'value': 23.6, 'target': 23.5, 'tolerance': 0.2, 'passed': True}}
    """
    report = {}
    for crit in criteria or []:
        name = crit.get("metric_name")
        if name is None or name not in metrics:
            continue
        value = metrics[name]
        target = crit.get("target_value", 0.0)
        tol = crit.get("tolerance", 0.0)
        report[name] = {
            "value": value,
            "target": target,
            "tolerance": tol,
            "passed": abs(float(value) - float(target)) <= float(tol),
        }
    return report


def _sample_structure():
    """Return the target structure baked in from the IntentSpec, or a tiny
    built-in sample (BCC iron) when none was supplied (e.g. a smoke run with no
    material). Baking the requested material here is what makes "the density of
    silicon" analyse silicon instead of this iron placeholder.

    >>> s = _sample_structure()
    >>> s["atoms"][0]["species"], len(s["atoms"])
    ('Fe', 2)
    """
    baked = _parse_json(_STRUCTURE_JSON, {})
    if isinstance(baked, dict) and baked.get("atoms") and baked.get("lattice"):
        return baked
    a = 2.87
    return {
        "lattice": [[a, 0.0, 0.0], [0.0, a, 0.0], [0.0, 0.0, a]],
        "atoms": [
            {"species": "Fe", "coordinates": [0.0, 0.0, 0.0]},
            {"species": "Fe", "coordinates": [0.5, 0.5, 0.5]},
        ],
        "coordinateSystem": "fractional",
    }


def _structure_from_dict(data):
    """Split a structure dict into ``(lattice, species, coords, cartesian)``.

    Pure parsing (no Pymatgen), so it is unit-testable anywhere.

    >>> lat, sp, co, cart = _structure_from_dict(_sample_structure())
    >>> sp, cart
    (['Fe', 'Fe'], False)
    >>> co[1]
    [0.5, 0.5, 0.5]
    """
    lattice = data["lattice"]
    species = [atom["species"] for atom in data["atoms"]]
    coords = [list(atom["coordinates"]) for atom in data["atoms"]]
    cartesian = str(data.get("coordinateSystem", "fractional")).lower() == "cartesian"
    return lattice, species, coords, cartesian


# ───────────────────────────── defaults from plan ───────────────────────────
DEFAULTS = _parse_json(_CONFIG_JSON, {"round_digits": 4})
ACCEPTANCE = _parse_json(_ACCEPTANCE_JSON, [])


# ─────────────────────────────── real work ──────────────────────────────────
def run(config, input_path, output_path, *, smoke=False):
    """Load a structure with Pymatgen, compute properties, write a CSV.

    Returns the computed metrics as a plain dict.
    """
    from pymatgen.core import Lattice, Structure  # heavy import, kept lazy

    if smoke or not Path(input_path).is_file():
        data = _sample_structure()
    else:
        data = json.loads(Path(input_path).read_text(encoding="utf-8"))

    lattice_matrix, species, coords, cartesian = _structure_from_dict(data)
    structure = Structure(
        Lattice(lattice_matrix), species, coords, coords_are_cartesian=cartesian
    )

    digits = int(config.get("round_digits", 4))
    metrics = {
        "formula": structure.composition.reduced_formula,
        "num_sites": structure.num_sites,
        "volume": round(structure.volume, digits),
        "density": round(float(structure.density), digits),
        "a": round(structure.lattice.a, digits),
        "b": round(structure.lattice.b, digits),
        "c": round(structure.lattice.c, digits),
    }

    fieldnames = list(metrics.keys())
    Path(output_path).write_text(
        _rows_to_csv_text([metrics], fieldnames), encoding="utf-8"
    )
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=f"{TOOL_NAME} structure analysis ({MODEL_NAME}).")
    parser.add_argument("--input", default=INPUT_FILE, help="structure JSON input")
    parser.add_argument("--output", default=OUTPUT_FILE, help="CSV results output")
    parser.add_argument("--config", default=CONFIG_FILE, help="config.yaml with parameters")
    parser.add_argument("--smoke", action="store_true", help="tiny sample-data sanity run")
    args = parser.parse_args(argv)

    config = _load_config(args.config, DEFAULTS)
    metrics = run(config, args.input, args.output, smoke=args.smoke)

    summary = dict(metrics)
    summary["acceptance"] = _check_acceptance(metrics, ACCEPTANCE)
    summary["tool"] = TOOL_NAME
    summary["output_file"] = str(args.output)
    json.dump(summary, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    # Anchor relative paths (results, tool scratch files) to the bundle dir so
    # a manual run from anywhere doesn't litter the caller's working directory.
    os.chdir(Path(__file__).resolve().parent)
    raise SystemExit(main())
