#!/usr/bin/env python3
"""RDKit molecular-graph manipulation template (TWAIN code_configuration_builder).

WHAT THIS SCRIPT DOES
    Loads molecules as SMILES and performs molecular-graph operations with
    RDKit: canonicalisation, molecular-formula derivation, atom/bond/ring
    counting, aromatic-ring detection, and Bemis-Murcko scaffold extraction.
    The per-molecule graph features are written to a CSV and summarised against
    the plan's acceptance criteria.

INPUTS
    --input   Text/CSV file of SMILES -- one SMILES per line, or a CSV with a
              ``smiles`` column. When --smoke is passed, a tiny built-in set is
              used instead.
    --config  config.yaml; its ``parameters`` block overrides the baked-in
              defaults when PyYAML is available.

OUTPUTS
    * A per-molecule CSV at --output (canonical SMILES + graph features).
    * A JSON summary on stdout (n_molecules, n_valid, mean atom count) for the
      downstream interpreter/validator.

The RDKit import is lazy (inside :func:`run`) so this module imports and its
doctests run even without RDKit installed -- the smoke tests report the missing
dependency instead.

Run:  python main.py            # real run on --input
      python main.py --smoke    # tiny sample-data sanity run
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

# --- substitution placeholders (filled in by CodegenEngine) ------------------
TOOL_NAME = "{TOOL_NAME}"        # human-readable tool name, e.g. "RDKit"
TOOL_IMPORT = "{TOOL_IMPORT}"    # importable module, e.g. "rdkit"
MODEL_NAME = "{MODEL_NAME}"      # operation label, e.g. "graph_manipulation"
INPUT_FILE = "{INPUT_FILE}"      # default SMILES input path
OUTPUT_FILE = "{OUTPUT_FILE}"    # default features CSV output path
CONFIG_FILE = "{CONFIG_FILE}"    # runtime config path (YAML)
GENERATED_AT = "{GENERATED_AT}"  # originating plan timestamp (provenance)
_CONFIG_JSON = r"""{CONFIG_JSON}"""        # baked hyperparameters (JSON)
_ACCEPTANCE_JSON = r"""{ACCEPTANCE_JSON}"""  # baked acceptance criteria (JSON)


# ─────────────────────────── pure helpers (tool-free) ───────────────────────
def _parse_json(raw, fallback):
    """Parse an embedded JSON blob, tolerating an unsubstituted placeholder.

    >>> _parse_json('{"kekulize": true}', {})
    {'kekulize': True}
    >>> _parse_json('{CONFIG_JSON}', {'kekulize': False})
    {'kekulize': False}
    """
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return fallback
    return value if value is not None else fallback


def _load_config(config_file, defaults):
    """Overlay ``config.yaml``'s ``parameters`` over ``defaults`` (PyYAML optional).

    >>> _load_config("no_such_config_xyz.yaml", {"kekulize": False})
    {'kekulize': False}
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

    >>> print(_rows_to_csv_text([{"smiles": "CCO", "num_atoms": 3}],
    ...                          ["smiles", "num_atoms"]).strip())
    smiles,num_atoms
    CCO,3
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _summary_stats(values):
    """Return count/mean/min/max for a list of numbers (empty-safe).

    >>> _summary_stats([3, 9])
    {'count': 2, 'mean': 6.0, 'min': 3, 'max': 9}
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
    ...     {"mean_num_atoms": 6.0},
    ...     [{"metric_name": "mean_num_atoms", "target_value": 6.0, "tolerance": 1.0}],
    ... )
    {'mean_num_atoms': {'value': 6.0, 'target': 6.0, 'tolerance': 1.0, 'passed': True}}
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


def _parse_smiles(text):
    """Parse SMILES from one-per-line text or a CSV with a ``smiles`` column.

    >>> _parse_smiles("CCO\\nCC(=O)O\\n")
    ['CCO', 'CC(=O)O']
    >>> _parse_smiles("smiles,name\\nc1ccccc1,benzene")
    ['c1ccccc1']
    """
    stripped = text.strip()
    if not stripped:
        return []
    first_line = stripped.splitlines()[0]
    # CSV path: a comma in the header/first line and a 'smiles' token present.
    if "," in first_line and "smiles" in first_line.lower():
        reader = csv.DictReader(io.StringIO(text))
        fields = reader.fieldnames or []
        target = next((f for f in fields if f and f.strip().lower() == "smiles"), fields[0] if fields else None)
        return [(r.get(target) or "").strip() for r in reader if target and (r.get(target) or "").strip()]
    return [line.strip() for line in stripped.splitlines() if line.strip()]


def _sample_smiles():
    """Return a tiny built-in molecule set for smoke runs.

    >>> _sample_smiles()[2]
    'c1ccccc1'
    """
    return ["CCO", "CC(=O)O", "c1ccccc1", "C1CCCCC1"]


# ───────────────────────────── defaults from plan ───────────────────────────
DEFAULTS = _parse_json(_CONFIG_JSON, {"canonical": True})
ACCEPTANCE = _parse_json(_ACCEPTANCE_JSON, [])


# ─────────────────────────────── real work ──────────────────────────────────
def run(config, input_path, output_path, *, smoke=False):
    """Perform molecular-graph operations per SMILES, write a features CSV.

    Returns batch summary metrics as a plain dict.
    """
    from rdkit import Chem  # heavy import, kept lazy
    from rdkit.Chem import rdMolDescriptors
    from rdkit.Chem.Scaffolds import MurckoScaffold

    if smoke or not Path(input_path).is_file():
        smiles = _sample_smiles()
    else:
        smiles = _parse_smiles(Path(input_path).read_text(encoding="utf-8"))

    canonical = bool(config.get("canonical", True))
    rows = []
    atom_counts = []
    for smi in smiles:
        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            rows.append({"input_smiles": smi, "valid": False, "canonical_smiles": "",
                         "formula": "", "num_atoms": "", "num_bonds": "",
                         "num_rings": "", "num_aromatic_rings": "", "scaffold": ""})
            continue
        atom_counts.append(mol.GetNumAtoms())
        scaffold = MurckoScaffold.GetScaffoldForMol(mol)
        rows.append({
            "input_smiles": smi,
            "valid": True,
            "canonical_smiles": Chem.MolToSmiles(mol, canonical=canonical),
            "formula": rdMolDescriptors.CalcMolFormula(mol),
            "num_atoms": mol.GetNumAtoms(),
            "num_bonds": mol.GetNumBonds(),
            "num_rings": rdMolDescriptors.CalcNumRings(mol),
            "num_aromatic_rings": rdMolDescriptors.CalcNumAromaticRings(mol),
            "scaffold": Chem.MolToSmiles(scaffold) if scaffold is not None else "",
        })

    fieldnames = ["input_smiles", "valid", "canonical_smiles", "formula", "num_atoms",
                  "num_bonds", "num_rings", "num_aromatic_rings", "scaffold"]
    Path(output_path).write_text(_rows_to_csv_text(rows, fieldnames), encoding="utf-8")

    stats = _summary_stats(atom_counts)
    metrics = {
        "operation": MODEL_NAME,
        "n_molecules": len(smiles),
        "n_valid": len(atom_counts),
        "mean_num_atoms": round(stats["mean"], 4),
    }
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=f"{TOOL_NAME} graph manipulation ({MODEL_NAME}).")
    parser.add_argument("--input", default=INPUT_FILE, help="SMILES input (lines or CSV)")
    parser.add_argument("--output", default=OUTPUT_FILE, help="features CSV output")
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
    raise SystemExit(main())
