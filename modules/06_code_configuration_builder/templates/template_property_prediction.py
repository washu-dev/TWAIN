#!/usr/bin/env python3
"""Molecular property-prediction template (TWAIN code_configuration_builder).

WHAT THIS SCRIPT DOES
    Loads a list of molecules as SMILES, computes/predicts per-molecule
    properties with the selected cheminformatics tool (RDKit descriptors by
    default -- molecular weight, LogP, TPSA, ring/atom counts), saves a tidy CSV
    (one row per molecule), and summarises the batch against the plan's
    acceptance criteria.

INPUTS
    --input   CSV with a ``smiles`` column (a header is required; the first
              column is used if no ``smiles`` column is present). When --smoke
              is passed, a tiny built-in molecule set is used instead.
    --config  config.yaml; its ``parameters`` block overrides the baked-in
              defaults when PyYAML is available.

OUTPUTS
    * A per-molecule CSV at --output.
    * A JSON summary on stdout (n_molecules, n_valid, mean descriptors) for the
      downstream interpreter/validator.

The cheminformatics import is lazy (inside :func:`run`) so this module imports
and its doctests run even without RDKit installed -- the smoke tests report the
missing dependency instead.

Run:  python main.py            # real run on --input CSV
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
TOOL_NAME = "{TOOL_NAME}"        # human-readable tool name, e.g. "RDKit"
TOOL_IMPORT = "{TOOL_IMPORT}"    # importable module, e.g. "rdkit"
MODEL_NAME = "{MODEL_NAME}"      # model/descriptor label, e.g. "rdkit_descriptors"
INPUT_FILE = "{INPUT_FILE}"      # default SMILES CSV input path
OUTPUT_FILE = "{OUTPUT_FILE}"    # default predictions CSV output path
CONFIG_FILE = "{CONFIG_FILE}"    # runtime config path (YAML)
GENERATED_AT = "{GENERATED_AT}"  # originating plan timestamp (provenance)
_CONFIG_JSON = r"""{CONFIG_JSON}"""        # baked hyperparameters (JSON)
_ACCEPTANCE_JSON = r"""{ACCEPTANCE_JSON}"""  # baked acceptance criteria (JSON)


# ─────────────────────────── pure helpers (tool-free) ───────────────────────
def _parse_json(raw, fallback):
    """Parse an embedded JSON blob, tolerating an unsubstituted placeholder.

    >>> _parse_json('{"round_digits": 3}', {})
    {'round_digits': 3}
    >>> _parse_json('{CONFIG_JSON}', {'round_digits': 4})
    {'round_digits': 4}
    """
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return fallback
    return value if value is not None else fallback


def _load_config(config_file, defaults):
    """Overlay ``config.yaml``'s ``parameters`` over ``defaults`` (PyYAML optional).

    >>> _load_config("no_such_config_xyz.yaml", {"round_digits": 4})
    {'round_digits': 4}
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

    >>> print(_rows_to_csv_text([{"smiles": "CCO", "mol_weight": 46.07}],
    ...                          ["smiles", "mol_weight"]).strip())
    smiles,mol_weight
    CCO,46.07
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _summary_stats(values):
    """Return count/mean/min/max for a list of numbers (empty-safe).

    >>> _summary_stats([2.0, 4.0])
    {'count': 2, 'mean': 3.0, 'min': 2.0, 'max': 4.0}
    >>> _summary_stats([])
    {'count': 0, 'mean': 0.0, 'min': 0.0, 'max': 0.0}
    """
    vals = list(values)
    if not vals:
        return {"count": 0, "mean": 0.0, "min": 0.0, "max": 0.0}
    return {"count": len(vals), "mean": sum(vals) / len(vals), "min": min(vals), "max": max(vals)}


def _check_acceptance(metrics, criteria):
    """Compare computed ``metrics`` against acceptance ``criteria``.

    >>> _check_acceptance(
    ...     {"mean_mol_weight": 60.0},
    ...     [{"metric_name": "mean_mol_weight", "target_value": 60.0, "tolerance": 5.0}],
    ... )
    {'mean_mol_weight': {'value': 60.0, 'target': 60.0, 'tolerance': 5.0, 'passed': True}}
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


def _parse_smiles_csv(text, column="smiles"):
    """Extract the SMILES column from CSV ``text``.

    Matches ``column`` case-insensitively; falls back to the first column.

    >>> _parse_smiles_csv("smiles,name\\nCCO,ethanol\\nCC(=O)O,acetic acid")
    ['CCO', 'CC(=O)O']
    >>> _parse_smiles_csv("SMILES\\nc1ccccc1")
    ['c1ccccc1']
    """
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    target = None
    for field in fieldnames:
        if field and field.strip().lower() == column.lower():
            target = field
            break
    if target is None and fieldnames:
        target = fieldnames[0]
    out = []
    for row in reader:
        val = (row.get(target) or "").strip() if target else ""
        if val:
            out.append(val)
    return out


# No built-in sample molecules: TWAIN never fabricates a placeholder. Predictions
# run on the real SMILES set from --input; with none provided, run() fails loudly
# rather than substituting stand-in molecules.


# ───────────────────────────── defaults from plan ───────────────────────────
DEFAULTS = _parse_json(_CONFIG_JSON, {"round_digits": 4})
ACCEPTANCE = _parse_json(_ACCEPTANCE_JSON, [])


# ─────────────────────────────── real work ──────────────────────────────────
def run(config, input_path, output_path, *, smoke=False):
    """Compute RDKit descriptors for each SMILES, write a per-molecule CSV.

    Returns batch summary metrics as a plain dict.
    """
    from rdkit import Chem  # heavy import, kept lazy
    from rdkit.Chem import Crippen, Descriptors, rdMolDescriptors

    if Path(input_path).is_file():
        smiles = _parse_smiles_csv(Path(input_path).read_text(encoding="utf-8"))
    else:
        raise SystemExit(
            "no molecules to predict: this bundle needs a real SMILES set via "
            "--input <molecules.csv>. TWAIN refuses to fabricate placeholder molecules."
        )

    digits = int(config.get("round_digits", 4))
    rows = []
    weights = []
    for smi in smiles:
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            rows.append({"smiles": smi, "valid": False, "mol_weight": "",
                         "logp": "", "tpsa": "", "num_atoms": "", "num_rings": ""})
            continue
        mw = round(Descriptors.MolWt(mol), digits)
        weights.append(mw)
        rows.append({
            "smiles": smi,
            "valid": True,
            "mol_weight": mw,
            "logp": round(Crippen.MolLogP(mol), digits),
            "tpsa": round(Descriptors.TPSA(mol), digits),
            "num_atoms": mol.GetNumAtoms(),
            "num_rings": rdMolDescriptors.CalcNumRings(mol),
        })

    fieldnames = ["smiles", "valid", "mol_weight", "logp", "tpsa", "num_atoms", "num_rings"]
    Path(output_path).write_text(_rows_to_csv_text(rows, fieldnames), encoding="utf-8")

    stats = _summary_stats(weights)
    metrics = {
        "model": MODEL_NAME,
        "n_molecules": len(smiles),
        "n_valid": len(weights),
        "mean_mol_weight": round(stats["mean"], 4),
    }
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=f"{TOOL_NAME} property prediction ({MODEL_NAME}).")
    parser.add_argument("--input", default=INPUT_FILE, help="SMILES CSV input")
    parser.add_argument("--output", default=OUTPUT_FILE, help="predictions CSV output")
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
