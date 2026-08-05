#!/usr/bin/env python3
"""ASE molecular-dynamics template (TWAIN code_configuration_builder).

WHAT THIS SCRIPT DOES
    Builds an atomic system, attaches an ASE calculator (EMT by default), runs a
    short Velocity-Verlet molecular-dynamics trajectory, and extracts a
    per-frame trajectory (step, time, temperature, potential/kinetic/total
    energy). Results are saved as a CSV and summarised against the plan's
    acceptance criteria.

INPUTS
    --input   JSON describing the system, shaped like the TWAIN ASE contract::

                  {"atoms": [{"species": "Cu", "coordinates": [0, 0, 0]}, ...],
                   "cell":  [[a,0,0],[0,b,0],[0,0,c]],
                   "pbc": true,
                   "coordinateSystem": "cartesian"}

              (A ``{"structure": {...}}`` wrapper is also accepted.) When
              --smoke is passed, a tiny built-in FCC-copper cell is used.
    --config  config.yaml; its ``parameters`` block (steps, timestep_fs,
              temperature_K) overrides the baked-in defaults when PyYAML exists.

OUTPUTS
    * A trajectory CSV at --output (one row per frame).
    * A JSON summary on stdout (n_frames, final temperature, mean total energy,
      energy drift) for the downstream interpreter/validator.

The ASE import is lazy (inside :func:`run`) so this module imports and its
doctests run even without ASE installed -- the smoke tests report the missing
dependency instead.

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
TOOL_NAME = "{TOOL_NAME}"        # human-readable tool name, e.g. "ASE"
TOOL_IMPORT = "{TOOL_IMPORT}"    # importable module, e.g. "ase"
MODEL_NAME = "{MODEL_NAME}"      # calculator/method label, e.g. "emt_nve"
INPUT_FILE = "{INPUT_FILE}"      # default system input path (JSON)
OUTPUT_FILE = "{OUTPUT_FILE}"    # default trajectory output path (CSV)
CONFIG_FILE = "{CONFIG_FILE}"    # runtime config path (YAML)
GENERATED_AT = "{GENERATED_AT}"  # originating plan timestamp (provenance)
_CONFIG_JSON = r"""{CONFIG_JSON}"""        # baked hyperparameters (JSON)
_ACCEPTANCE_JSON = r"""{ACCEPTANCE_JSON}"""  # baked acceptance criteria (JSON)


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

    >>> _load_config("no_such_config_xyz.yaml", {"steps": 5})
    {'steps': 5}
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


def _summary_stats(values):
    """Return count/mean/min/max for a list of numbers (empty-safe).

    >>> _summary_stats([1.0, 2.0, 3.0])
    {'count': 3, 'mean': 2.0, 'min': 1.0, 'max': 3.0}
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
    ...     {"final_temperature_K": 305.0},
    ...     [{"metric_name": "final_temperature_K", "target_value": 300.0, "tolerance": 10.0}],
    ... )
    {'final_temperature_K': {'value': 305.0, 'target': 300.0, 'tolerance': 10.0, 'passed': True}}

    A metric the plan named but gave no number for is reported, not judged
    -- passed is None, which is neither a pass nor a fail:

    >>> _check_acceptance(
    ...     {"final_temperature_K": 305.0},
    ...     [{"metric_name": "final_temperature_K", "target_value": None,
    ...       "tolerance": None}],
    ... )
    {'final_temperature_K': {'value': 305.0, 'target': None, 'tolerance': None, 'passed': None}}
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


# No built-in sample system: TWAIN never fabricates a placeholder. A molecular-
# dynamics run reads its atomic system from --input; with none provided, run()
# fails loudly rather than substituting a stand-in material.


def _atoms_from_spec(spec):
    """Split a system dict into ``(symbols, coords, cell, pbc, cartesian)``.

    Accepts either a flat spec or one wrapped in ``{"structure": {...}}``.

    >>> _demo = {"atoms": [{"species": "X", "coordinates": [0.0, 0.0, 0.0]},
    ...                    {"species": "X", "coordinates": [1.5, 1.5, 1.5]}],
    ...          "cell": [[3.0, 0, 0], [0, 3.0, 0], [0, 0, 3.0]],
    ...          "pbc": True, "coordinateSystem": "cartesian"}
    >>> sym, coords, cell, pbc, cart = _atoms_from_spec(_demo)
    >>> sym[0], len(coords), pbc, cart
    ('X', 2, True, True)
    """
    if isinstance(spec, dict) and "structure" in spec:
        spec = spec["structure"]
    symbols = [atom["species"] for atom in spec["atoms"]]
    coords = [list(atom["coordinates"]) for atom in spec["atoms"]]
    cell = spec.get("cell", [[0.0, 0.0, 0.0]] * 3)
    pbc = bool(spec.get("pbc", True))
    cartesian = str(spec.get("coordinateSystem", "cartesian")).lower() == "cartesian"
    return symbols, coords, cell, pbc, cartesian


# ───────────────────────────── defaults from plan ───────────────────────────
DEFAULTS = _parse_json(_CONFIG_JSON, {"steps": 20, "timestep_fs": 1.0, "temperature_K": 300.0})
ACCEPTANCE = _parse_json(_ACCEPTANCE_JSON, [])


# ─────────────────────────────── real work ──────────────────────────────────
def run(config, input_path, output_path, *, smoke=False):
    """Build a system, run Velocity-Verlet MD, write a trajectory CSV.

    Returns summary metrics as a plain dict.
    """
    from ase import Atoms, units  # heavy imports, kept lazy
    from ase.calculators.emt import EMT
    from ase.md.velocitydistribution import MaxwellBoltzmannDistribution
    from ase.md.verlet import VelocityVerlet

    if Path(input_path).is_file():
        spec = json.loads(Path(input_path).read_text(encoding="utf-8"))
    else:
        raise SystemExit(
            "no atomic system to run: this molecular-dynamics bundle needs a real "
            "system via --input <system.json>. TWAIN refuses to fabricate a "
            "placeholder system."
        )

    symbols, coords, cell, pbc, cartesian = _atoms_from_spec(spec)
    if cartesian:
        atoms = Atoms(symbols=symbols, positions=coords, cell=cell, pbc=pbc)
    else:
        atoms = Atoms(symbols=symbols, scaled_positions=coords, cell=cell, pbc=pbc)
    atoms.calc = EMT()

    steps = int(config.get("steps", 5 if smoke else 20))
    if smoke:
        steps = min(steps, 5)
    timestep = float(config.get("timestep_fs", 1.0))
    temperature = float(config.get("temperature_K", 300.0))

    MaxwellBoltzmannDistribution(atoms, temperature_K=temperature)
    dyn = VelocityVerlet(atoms, timestep * units.fs)

    frames = []

    def _record():
        epot = float(atoms.get_potential_energy())
        ekin = float(atoms.get_kinetic_energy())
        frames.append({
            "step": len(frames),
            "time_fs": round(len(frames) * timestep, 4),
            "temperature_K": round(float(atoms.get_temperature()), 4),
            "epot_eV": round(epot, 6),
            "ekin_eV": round(ekin, 6),
            "etot_eV": round(epot + ekin, 6),
        })

    _record()
    for _ in range(steps):
        dyn.run(1)
        _record()

    fieldnames = ["step", "time_fs", "temperature_K", "epot_eV", "ekin_eV", "etot_eV"]
    Path(output_path).write_text(_rows_to_csv_text(frames, fieldnames), encoding="utf-8")

    etot = [f["etot_eV"] for f in frames]
    metrics = {
        "n_frames": len(frames),
        "final_temperature_K": frames[-1]["temperature_K"],
        "mean_total_energy_eV": round(_summary_stats(etot)["mean"], 6),
        "energy_drift_eV": round(etot[-1] - etot[0], 6),
    }
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=f"{TOOL_NAME} molecular dynamics ({MODEL_NAME}).")
    parser.add_argument("--input", default=INPUT_FILE, help="system JSON input")
    parser.add_argument("--output", default=OUTPUT_FILE, help="trajectory CSV output")
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
