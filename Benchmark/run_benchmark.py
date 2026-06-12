"""TWAIN benchmark runner (M1: ASE engine).

Reads an IntentSpecification JSON, validates it against the schema,
builds an ASE Atoms object from the `query.ase` block, runs the requested
calculation, and reports the result.

Usage:
    python Benchmark/run_benchmark.py Benchmark/structures/h2o_molecule.json
    ... --write-reference   store the result as the reference output
    ... --check             compare the result against the stored reference
"""

import argparse
import json
import sys
import time
from pathlib import Path

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "Schema" / "Schema.json"
REFERENCES_DIR = Path(__file__).resolve().parent / "references"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Tolerances for --check: results are floats and may differ slightly
# across platforms/library versions, so exact equality is too strict.
ENERGY_TOL_EV = 1e-6
POSITION_TOL_ANGSTROM = 1e-4


def load_and_validate_spec(spec_path: Path) -> dict:
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)
    with open(spec_path) as f:
        spec = json.load(f)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(spec), key=str)
    if errors:
        print(f"Spec {spec_path} FAILS schema validation:")
        for e in errors:
            print(f"  - {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}")
        sys.exit(1)
    print(f"Spec validated against {SCHEMA_PATH.relative_to(REPO_ROOT)}")
    return spec


def build_ase_atoms(ase_block: dict):
    from ase import Atoms

    structure = ase_block["structure"]
    symbols = [a["species"] for a in structure["atoms"]]
    coords = [a["coordinates"] for a in structure["atoms"]]
    pbc = structure.get("pbc", False)
    cell = structure.get("cell")
    fractional = structure.get("coordinateSystem", "cartesian") == "fractional"

    kwargs = {"symbols": symbols, "pbc": pbc}
    if cell is not None:
        kwargs["cell"] = cell
    if fractional:
        if cell is None:
            print("fractional coordinates require a cell")
            sys.exit(1)
        kwargs["scaled_positions"] = coords
    else:
        kwargs["positions"] = coords
    return Atoms(**kwargs)


def make_calculator(name: str):
    if name == "emt":
        from ase.calculators.emt import EMT

        return EMT()
    print(f"Calculator '{name}' is not runnable locally; only 'emt' is supported for now")
    sys.exit(1)


def run_ase(spec: dict) -> dict:
    import ase

    ase_block = spec["query"]["ase"]
    calc_block = ase_block["calculation"]
    calc_type = calc_block["calculationType"]

    atoms = build_ase_atoms(ase_block)
    atoms.calc = make_calculator(calc_block["calculator"])

    start = time.perf_counter()
    steps = None

    if calc_type == "static":
        energy = atoms.get_potential_energy()
    elif calc_type == "relax":
        from ase import optimize

        params = calc_block.get("relax", {})
        optimizer_cls = getattr(optimize, params.get("optimizer", "BFGS"))
        opt = optimizer_cls(atoms, logfile=None)
        opt.run(fmax=params.get("fmax", 0.05), steps=params.get("maxSteps", 200))
        steps = opt.get_number_of_steps()
        energy = atoms.get_potential_energy()
    else:
        print(f"calculationType '{calc_type}' is not implemented yet (M1 supports static/relax)")
        sys.exit(1)

    walltime = time.perf_counter() - start

    return {
        "engine": "ase",
        "engineVersion": ase.__version__,
        "submissionId": spec["userConstraints"]["metadata"]["submissionId"],
        "calculator": calc_block["calculator"],
        "calculationType": calc_type,
        "energy_eV": energy,
        "finalPositions": atoms.get_positions().tolist(),
        "optimizerSteps": steps,
        "walltime_s": round(walltime, 4),
    }


def check_against_reference(result: dict, reference_path: Path) -> bool:
    if not reference_path.exists():
        print(f"No reference at {reference_path}; run with --write-reference first")
        return False
    with open(reference_path) as f:
        reference = json.load(f)

    ok = True
    energy_delta = abs(result["energy_eV"] - reference["energy_eV"])
    if energy_delta > ENERGY_TOL_EV:
        print(f"FAIL energy: |{result['energy_eV']} - {reference['energy_eV']}| = {energy_delta:.2e} eV > {ENERGY_TOL_EV}")
        ok = False

    for i, (got, want) in enumerate(zip(result["finalPositions"], reference["finalPositions"])):
        delta = max(abs(g - w) for g, w in zip(got, want))
        if delta > POSITION_TOL_ANGSTROM:
            print(f"FAIL atom {i} position: max delta {delta:.2e} A > {POSITION_TOL_ANGSTROM}")
            ok = False

    print("Reference check PASSED" if ok else "Reference check FAILED")
    return ok


def main():
    parser = argparse.ArgumentParser(description="TWAIN benchmark runner")
    parser.add_argument("spec", type=Path, help="IntentSpecification JSON file")
    parser.add_argument("--write-reference", action="store_true", help="store result as reference output")
    parser.add_argument("--check", action="store_true", help="compare result against stored reference")
    args = parser.parse_args()

    spec = load_and_validate_spec(args.spec)
    if "ase" not in spec.get("query", {}):
        print("Spec has no query.ase block; M1 only runs the ASE engine")
        sys.exit(1)

    result = run_ase(spec)
    print(json.dumps({k: v for k, v in result.items() if k != "finalPositions"}, indent=2))

    reference_path = REFERENCES_DIR / f"{args.spec.stem}_ase.json"
    if args.write_reference:
        REFERENCES_DIR.mkdir(exist_ok=True)
        with open(reference_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Reference written to {reference_path.relative_to(REPO_ROOT)}")
    elif args.check:
        if not check_against_reference(result, reference_path):
            sys.exit(1)
    else:
        RESULTS_DIR.mkdir(exist_ok=True)
        out = RESULTS_DIR / f"{args.spec.stem}_ase.json"
        with open(out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"Result written to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
