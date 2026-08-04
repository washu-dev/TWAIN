"""Materials Project database-retrieval route + the bulk-modulus alias.

Regression guards for the CaPt2 failure: the researcher asked to RETRIEVE the
precomputed bulk modulus from the Materials Project, but (a) canonical_property
didn't know "bulk modulus", so the plan carried requested_property=None and
codegen shipped the fail-loud pymatgen structure_analysis template instead of
rerouting to LLM synthesis, and (b) codegen had no notion of a lookup route at
all -- the prompt never saw the objective, MP_API_KEY, or the mp-api client.

Run from the repo root with:  pixi run pytest tests/unit/test_mp_lookup.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "modules" / "06_code_configuration_builder"))

from method_discovery.calculator_registry import (  # noqa: E402
    calculators_for_property,
    canonical_property,
)
from codegen_engine import CodegenEngine, mp_lookup_requested  # noqa: E402


CAPT2_OBJECTIVE = (
    "Compute the bulk modulus of CaPt2 using data from the Materials Project, "
    "assuming the cheapest way of obtaining the result (i.e., retrieving the "
    "precomputed elasticity/bulk modulus value directly from the Materials "
    "Project database rather than running a new DFT elastic-tensor calculation)."
)

# A minimal valid script the stub "agent" returns; references pymatgen so the
# synthesis-acceptance check (must_reference) passes.
LOOKUP_SCRIPT = '''\
import argparse, csv, json, os, sys


def retrieve():
    from pymatgen.ext.matproj import MPRester
    with MPRester(os.environ["MP_API_KEY"]) as mpr:
        return 200.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="results.csv")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    if not os.environ.get("MP_API_KEY"):
        print("MP_API_KEY is not set")
        sys.exit(3)
    value = 0.0 if args.smoke else retrieve()
    print(json.dumps({"bulk_modulus_VRH": value, "tool": "Pymatgen",
                      "property": "bulk_modulus", "output_file": args.output,
                      "source": "Materials Project database retrieval"}))
    with open(args.output, "w", newline="") as f:
        csv.writer(f).writerows([["bulk_modulus_VRH"], [value]])


if __name__ == "__main__":
    main()
'''


def _lookup_plan():
    return {
        "selected_method": {"tool_name": "Pymatgen", "libraries": ["Pymatgen"],
                            "calculator": None, "calculator_import": None,
                            "calculator_library": None},
        "requested_property": "bulk_modulus",
        "acceptance_metrics": [{"metric_name": "bulk_modulus_VRH",
                                "target_value": 200.0, "tolerance": 50.0}],
        "target_system": {"kind": "crystal", "formula": "CaPt2",
                          "crystal": {"formula": "CaPt2",
                                      "phase": "cubic Laves phase (C15)",
                                      "space_group": "Fd-3m",
                                      "space_group_number": 227,
                                      "mp_id": "mp-1023"}},
        "metadata": {"timestamp": "2026-07-30T00:00:00Z"},
        "safety_notes": [],
    }


def _lookup_intent():
    return {"objective": CAPT2_OBJECTIVE}


# -- property canonicalization -----------------------------------------------

def test_bulk_modulus_is_a_recognized_property():
    # This is the routing linchpin: with None, a library-only plan renders the
    # fail-loud structure_analysis template instead of synthesizing real code.
    assert canonical_property(CAPT2_OBJECTIVE) == "bulk_modulus"
    assert canonical_property("compute the bulk modulus of CaPt2") == "bulk_modulus"
    assert canonical_property("fit a Birch-Murnaghan equation of state") == "bulk_modulus"
    assert canonical_property("elastic tensor of MgO") == "elastic_constants"


def test_dft_engines_cover_the_mechanical_properties():
    ids = {c.id for c in calculators_for_property("bulk_modulus", platform=None)}
    assert "gpaw" in ids
    ids = {c.id for c in calculators_for_property("elastic_constants", platform=None)}
    assert "gpaw" in ids


# -- lookup detection ----------------------------------------------------------

def test_mp_lookup_requested_needs_mention_and_retrieval_verb():
    assert mp_lookup_requested(CAPT2_OBJECTIVE)
    assert mp_lookup_requested("look up the stored band gap on the Materials Project")
    # a bare mention (comparison / structure source) must NOT reroute a compute task
    assert not mp_lookup_requested("compute the band gap of Si")
    assert not mp_lookup_requested("compute the bulk modulus and compare with "
                                   "the Materials Project reference afterwards")
    assert not mp_lookup_requested(None)
    assert not mp_lookup_requested("")


# -- codegen routing + prompt ---------------------------------------------------

def test_lookup_plan_synthesizes_with_database_note_and_mp_api():
    seen = {}

    def agent(prompt):
        seen["prompt"] = prompt
        return LOOKUP_SCRIPT

    bundle = CodegenEngine().generate(_lookup_plan(), intent=_lookup_intent(),
                                      agent=agent)
    # rerouted to synthesis, never the fail-loud structure_analysis template
    assert bundle.template_name == "llm_synthesized"
    prompt = seen["prompt"]
    assert "MPRester" in prompt
    assert "MP_API_KEY" in prompt
    assert "mp-1023" in prompt                     # the exact database handle
    assert "NO network request" in prompt          # smoke stays offline
    # the venv fallback can install the client the lookup needs
    assert "mp-api" in bundle.requirements_txt


def test_compute_plan_gets_no_database_note():
    seen = {}

    def agent(prompt):
        seen["prompt"] = prompt
        return LOOKUP_SCRIPT

    bundle = CodegenEngine().generate(
        _lookup_plan(), intent={"objective": "Compute the bulk modulus of CaPt2."},
        agent=agent)
    assert bundle.template_name == "llm_synthesized"
    assert "MP_API_KEY" not in seen["prompt"]
    assert "mp-api" not in bundle.requirements_txt


def test_lookup_plan_without_agent_stays_deterministic():
    # Offline there is no synthesis; the deterministic path still applies.
    bundle = CodegenEngine().generate(_lookup_plan(), intent=_lookup_intent(),
                                      agent=None)
    assert bundle.template_name != "llm_synthesized"
