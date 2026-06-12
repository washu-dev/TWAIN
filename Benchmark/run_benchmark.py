"""TWAIN benchmark runner.

Reads an IntentSpecification JSON, validates it against the schema, runs the
requested calculation through one or both engines, and reports the results.

Engines:
  ase       builds an ase.Atoms from the `query.ase` block
  pymatgen  builds a pymatgen Molecule/Structure from the `query.chemical`
            block, then converts to ase.Atoms via AseAtomsAdaptor

Both engines execute with ASE's local EMT calculator, so results from the two
input representations should agree within tolerance -- that agreement is the
cross-engine consistency check. (Pymatgen's MP input sets target VASP, which
needs the cluster; that path comes later.)

Input formats:
  full spec   an IntentSpecification document (validated against Schema.json)
  fragment    a per-engine fragment as emitted by the Intelligence Layer's
              semantic parser, e.g. {"pymatgen": {...}} (validated against
              Schema/PymatgenSchema.json); pass --fragment pymatgen

Usage:
    python Benchmark/run_benchmark.py <spec.json> [--engine ase|pymatgen|both]
    python Benchmark/run_benchmark.py <fragment.json> --fragment pymatgen
    ... --write-reference   store the result(s) as reference output
    ... --check             compare the result(s) against stored references
"""

import argparse
import json
import sys
import time
from pathlib import Path

from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "Schema" / "Schema.json"
FRAGMENT_SCHEMAS = {
    "pymatgen": REPO_ROOT / "Schema" / "PymatgenSchema.json",
    "ase": REPO_ROOT / "Schema" / "AseSchema.json",
}
# where each fragment's engine block lives inside a full IntentSpecification
FRAGMENT_QUERY_KEY = {
    "pymatgen": "chemical",
    "ase": "ase",
}
REFERENCES_DIR = Path(__file__).resolve().parent / "references"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Tolerances: results are floats and may differ slightly across
# platforms/library versions, so exact equality is too strict.
ENERGY_TOL_EV = 1e-6
POSITION_TOL_ANGSTROM = 1e-4

# The Pymatgen block expresses the calculation as an MP input-set method;
# locally we map it onto the equivalent EMT calculation.
METHOD_TO_CALC_TYPE = {
    "MPRelaxSet": "relax",
    "MPStaticSet": "static",
}

DEFAULT_RELAX = {"optimizer": "BFGS", "fmax": 0.05, "maxSteps": 200}


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


def load_and_validate_fragment(fragment_path: Path, kind: str) -> dict:
    """Validate a per-engine fragment and wrap it as a full spec.

    The semantic parser emits engine fragments without userConstraints, so we
    attach synthetic benchmark metadata to reuse the normal engine paths.
    """
    schema_path = FRAGMENT_SCHEMAS[kind]
    with open(schema_path) as f:
        schema = json.load(f)
    with open(fragment_path) as f:
        fragment = json.load(f)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(fragment), key=str)
    if errors:
        print(f"Fragment {fragment_path} FAILS validation against {schema_path.name}:")
        for e in errors:
            print(f"  - {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}")
        sys.exit(1)
    if kind not in fragment:
        print(f"Fragment has no top-level '{kind}' key")
        sys.exit(1)
    print(f"Fragment validated against Schema/{schema_path.name}")

    return {
        "userConstraints": {
            "metadata": {
                "user": "benchmark-suite",
                "submissionId": f"fragment-{fragment_path.stem}",
                "inputType": "chemistry",
                "timeStamp": "1970-01-01T00:00:00Z",
            }
        },
        "query": {FRAGMENT_QUERY_KEY[kind]: fragment[kind]},
    }


def normalize_species(symbols):
    # tolerate lowercase symbols like "au" in specs
    return [s.capitalize() for s in symbols]


def run_calculation(atoms, calc_type: str, relax_params: dict) -> dict:
    """Attach EMT and run; shared by both engines."""
    from ase.calculators.emt import EMT

    atoms.calc = EMT()
    start = time.perf_counter()
    steps = None

    if calc_type == "static":
        energy = atoms.get_potential_energy()
    elif calc_type == "relax":
        from ase import optimize

        optimizer_cls = getattr(optimize, relax_params.get("optimizer", "BFGS"))
        opt = optimizer_cls(atoms, logfile=None)
        opt.run(fmax=relax_params.get("fmax", 0.05), steps=relax_params.get("maxSteps", 200))
        steps = opt.get_number_of_steps()
        energy = atoms.get_potential_energy()
    else:
        print(f"calculationType '{calc_type}' is not implemented yet (supported: static, relax)")
        sys.exit(1)

    return {
        "calculationType": calc_type,
        "energy_eV": energy,
        "finalPositions": atoms.get_positions().tolist(),
        "optimizerSteps": steps,
        "walltime_s": round(time.perf_counter() - start, 4),
    }


def run_ase(spec: dict) -> dict:
    import ase
    from ase import Atoms

    ase_block = spec["query"].get("ase")
    if ase_block is None:
        print("Spec has no query.ase block; cannot run the ase engine")
        sys.exit(1)

    structure = ase_block["structure"]
    calc_block = ase_block["calculation"]

    symbols = normalize_species([a["species"] for a in structure["atoms"]])
    coords = [a["coordinates"] for a in structure["atoms"]]
    cell = structure.get("cell")
    fractional = structure.get("coordinateSystem", "cartesian") == "fractional"

    kwargs = {"symbols": symbols, "pbc": structure.get("pbc", False)}
    if cell is not None:
        kwargs["cell"] = cell
    if fractional:
        if cell is None:
            print("fractional coordinates require a cell")
            sys.exit(1)
        kwargs["scaled_positions"] = coords
    else:
        kwargs["positions"] = coords

    if calc_block["calculator"] != "emt":
        print(f"Calculator '{calc_block['calculator']}' is not runnable locally; only 'emt' for now")
        sys.exit(1)

    result = run_calculation(
        Atoms(**kwargs),
        calc_block["calculationType"],
        calc_block.get("relax", DEFAULT_RELAX),
    )
    return {
        "engine": "ase",
        "engineVersion": ase.__version__,
        "submissionId": spec["userConstraints"]["metadata"]["submissionId"],
        "calculator": "emt",
        **result,
    }


def run_pymatgen(spec: dict) -> dict:
    import pymatgen.core
    from pymatgen.core import Lattice, Molecule, Structure
    from pymatgen.io.ase import AseAtomsAdaptor

    chemical = spec["query"].get("chemical")
    if chemical is None:
        print("Spec has no query.chemical block; cannot run the pymatgen engine")
        sys.exit(1)

    species = normalize_species([a["species"] for a in chemical["atoms"]])
    coords = [a["coordinates"] for a in chemical["atoms"]]

    # "structure" is Schema.json vocabulary; "lattice" is PymatgenSchema.json
    # (fragment) vocabulary for the same concept -- see enum-drift issue.
    if chemical["category"] in ("structure", "lattice"):
        # pymatgen Structures take fractional coordinates by convention
        pmg_obj = Structure(Lattice(chemical["lattice"]), species, coords)
    else:
        pmg_obj = Molecule(species, coords)

    method = chemical["experiment"]["method"]
    calc_type = METHOD_TO_CALC_TYPE.get(method)
    if calc_type is None:
        print(f"experiment.method '{method}' has no local equivalent (supported: {list(METHOD_TO_CALC_TYPE)})")
        sys.exit(1)

    atoms = AseAtomsAdaptor.get_atoms(pmg_obj)
    result = run_calculation(atoms, calc_type, DEFAULT_RELAX)
    return {
        "engine": "pymatgen",
        "engineVersion": pymatgen.core.__version__,
        "submissionId": spec["userConstraints"]["metadata"]["submissionId"],
        "calculator": "emt",
        "method": method,
        **result,
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
        print(f"FAIL energy: delta {energy_delta:.2e} eV > {ENERGY_TOL_EV}")
        ok = False

    for i, (got, want) in enumerate(zip(result["finalPositions"], reference["finalPositions"])):
        delta = max(abs(g - w) for g, w in zip(got, want))
        if delta > POSITION_TOL_ANGSTROM:
            print(f"FAIL atom {i} position: max delta {delta:.2e} A > {POSITION_TOL_ANGSTROM}")
            ok = False

    print(f"[{result['engine']}] reference check {'PASSED' if ok else 'FAILED'}")
    return ok


def compare_engines(ase_result: dict, pmg_result: dict) -> bool:
    energy_delta = abs(ase_result["energy_eV"] - pmg_result["energy_eV"])
    max_pos_delta = max(
        abs(g - w)
        for got, want in zip(ase_result["finalPositions"], pmg_result["finalPositions"])
        for g, w in zip(got, want)
    )
    ok = energy_delta <= ENERGY_TOL_EV and max_pos_delta <= POSITION_TOL_ANGSTROM
    print("--- engine comparison (ase vs pymatgen) ---")
    print(f"energy delta:        {energy_delta:.3e} eV (tol {ENERGY_TOL_EV})")
    print(f"max position delta:  {max_pos_delta:.3e} A  (tol {POSITION_TOL_ANGSTROM})")
    print(f"engines {'AGREE' if ok else 'DISAGREE'}")
    return ok


ENGINES = {"ase": run_ase, "pymatgen": run_pymatgen}


def main():
    parser = argparse.ArgumentParser(description="TWAIN benchmark runner")
    parser.add_argument("spec", type=Path, help="IntentSpecification JSON file (or engine fragment with --fragment)")
    parser.add_argument("--engine", choices=["ase", "pymatgen", "both"], default="ase")
    parser.add_argument("--fragment", choices=sorted(FRAGMENT_SCHEMAS), help="treat input as a per-engine fragment from the semantic parser")
    parser.add_argument("--write-reference", action="store_true", help="store result(s) as reference output")
    parser.add_argument("--check", action="store_true", help="compare result(s) against stored references")
    args = parser.parse_args()

    if args.fragment:
        spec = load_and_validate_fragment(args.spec, args.fragment)
        engine_names = [args.fragment]
    else:
        spec = load_and_validate_spec(args.spec)
        engine_names = ["ase", "pymatgen"] if args.engine == "both" else [args.engine]

    ok = True
    results = {}
    for name in engine_names:
        result = ENGINES[name](spec)
        results[name] = result
        print(json.dumps({k: v for k, v in result.items() if k != "finalPositions"}, indent=2))

        reference_path = REFERENCES_DIR / f"{args.spec.stem}_{name}.json"
        if args.write_reference:
            REFERENCES_DIR.mkdir(exist_ok=True)
            # walltime varies run-to-run; keeping it out of references means
            # regenerating them doesn't dirty git when physics is unchanged
            reference = {k: v for k, v in result.items() if k != "walltime_s"}
            with open(reference_path, "w") as f:
                json.dump(reference, f, indent=2)
            print(f"Reference written to {reference_path.relative_to(REPO_ROOT)}")
        elif args.check:
            ok = check_against_reference(result, reference_path) and ok
        else:
            RESULTS_DIR.mkdir(exist_ok=True)
            out = RESULTS_DIR / f"{args.spec.stem}_{name}.json"
            with open(out, "w") as f:
                json.dump(result, f, indent=2)
            print(f"Result written to {out.relative_to(REPO_ROOT)}")

    if len(results) == 2:
        ok = compare_engines(results["ase"], results["pymatgen"]) and ok

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
