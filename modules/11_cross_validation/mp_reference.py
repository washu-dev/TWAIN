"""Materials Project reference values as a live baseline source.

``configs/baselines.json`` is a hand-curated snapshot: every record was entered
by someone who checked the value and its unit. That does not scale to solid-state
runs -- a researcher asking for the bulk modulus of CaPt2 "validated against the
Materials Project reference value" gets no comparison at all, and VALIDATE has to
say the result was delivered without external validation.

This module answers those lookups from the Materials Project summary API. It
duck-types :meth:`BaselineDB.lookup`, so it drops into the same
:func:`~cross_validation.baseline_validator.compare` call through
:class:`~cross_validation.baseline_validator.ChainedBaselines` with the curated
snapshot still taking precedence.

Three properties of the design matter more than the lookup itself:

* **It never raises.** A missing key, a network failure, an unknown property, an
  ambiguous formula -- every one returns ``None``, which ``compare()`` already
  handles as "unmatched", and VALIDATE already falls back to the plan's own
  acceptance criteria. A reference source that can fail a run is worse than no
  reference source.
* **It only answers for properties whose unit it can state.** MP reports a
  formation energy per atom in eV/atom; a heat-of-formation run reports kJ/mol.
  Subtracting those is a silently wrong comparison, which is why every entry in
  :data:`MP_PROPERTIES` carries a unit and ``compare()`` refuses a pair whose
  units disagree.
* **One request per run.** All supported fields come back in a single summary
  query, cached on the instance, so a 6-metric result is one HTTP call and the
  metrics MP knows nothing about (``r2``, ``n_points``) never reach the network.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from cross_validation.baseline_validator import BaselineRecord

logger = logging.getLogger(__name__)

# mp-api's own per-request default is 20s. See MaterialsProjectBaselines._rester
# for why the rester has to be constructed directly for this to take effect.
DEFAULT_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class MPProperty:
    """One MP summary field TWAIN can compare against.

    ``unit`` is the unit MP reports the field in, and it is not decoration: it is
    what lets ``compare()`` reject a prediction measured in something else.
    ``pick`` pulls a scalar out of a structured field -- MP's ``bulk_modulus`` is
    a dict of Voigt/Reuss/VRH averages, not a number.
    """

    field: str
    unit: str
    pick: Optional[Callable[[Any], Any]] = None

    def value(self, raw: Any) -> Optional[float]:
        if raw is None:
            return None
        if self.pick is not None:
            raw = self.pick(raw)
        if raw is None or isinstance(raw, bool):
            return None
        return float(raw) if isinstance(raw, (int, float)) else None


def _vrh(value: Any) -> Optional[float]:
    """The Voigt-Reuss-Hill average -- the value MP's own UI shows as "the" modulus."""
    if isinstance(value, dict):
        return value.get("vrh")
    return getattr(value, "vrh", None)


# Canonical property name -> the MP field that answers it. Keys are the property
# names TWAIN's baseline layer uses (see _BASELINE_PROPERTY_ALIASES in the state
# machine, which maps a plan's metric name onto one of these).
#
# Every entry is intensive or explicitly per-atom, and that is a hard rule rather
# than a coincidence. MP's `volume` and `total_magnetization` are properties of
# whichever cell MP stored, so comparing them against a run that used a different
# cell convention is a factor-of-N error in the same unit -- which the unit guard
# cannot see. The CaPt2 run reported V0 for a 6-atom primitive cell; MP may hold
# the 24-atom conventional one. Neither field is offered, so no such comparison
# can be made by accident. Add a convention-dependent property only alongside a
# way to state and check the convention.
MP_PROPERTIES: Dict[str, MPProperty] = {
    "bulk_modulus": MPProperty("bulk_modulus", "GPa", _vrh),
    "shear_modulus": MPProperty("shear_modulus", "GPa", _vrh),
    "band_gap": MPProperty("band_gap", "eV"),
    "formation_energy_per_atom": MPProperty("formation_energy_per_atom", "eV/atom"),
    "energy_above_hull": MPProperty("energy_above_hull", "eV/atom"),
    "density": MPProperty("density", "g/cm^3"),
}

# Spellings a plan or a generated script uses for the same quantity. The lookup
# lowercases and strips before consulting this, so only genuinely different
# spellings need an entry.
MP_PROPERTY_ALIASES: Dict[str, str] = {
    "bulk_modulus_gpa": "bulk_modulus",
    "bulk_modulus_k": "bulk_modulus",
    "k_vrh": "bulk_modulus",
    "kvrh": "bulk_modulus",
    "b0": "bulk_modulus",
    "shear_modulus_gpa": "shear_modulus",
    "g_vrh": "shear_modulus",
    "gvrh": "shear_modulus",
    "bandgap": "band_gap",
    "band_gap_ev": "band_gap",
    "electronic_band_gap": "band_gap",
    "gap": "band_gap",
    "formation_energy": "formation_energy_per_atom",
    "formation_energy_ev_per_atom": "formation_energy_per_atom",
    "e_form": "formation_energy_per_atom",
    "mass_density": "density",
}

# Fields fetched in the one summary request. The identity fields are for the
# citation string and for choosing between polymorphs.
_IDENTITY_FIELDS = (
    "material_id",
    "formula_pretty",
    "symmetry",
    "energy_above_hull",
    "theoretical",
)


def canonical_property(name: Any) -> Optional[str]:
    """The MP property a metric name refers to, or None if MP cannot answer it."""
    if name is None:
        return None
    key = str(name).strip().lower()
    key = MP_PROPERTY_ALIASES.get(key, key)
    return key if key in MP_PROPERTIES else None


def formula_is_known(formula: str, *, api_key: Optional[str] = None,
                     client_factory: Optional[Callable[[str], Any]] = None,
                     timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Optional[bool]:
    """Whether Materials Project holds ANY entry for ``formula``.

    ``None`` means "could not tell" -- no key, no network, an unusable formula --
    and is deliberately distinct from ``False``. Callers must not treat the two
    alike: absence of evidence is not evidence of absence.

    Used as a plausibility signal on a requested composition. It is a WARNING and
    never a veto, for two reasons. MP is not exhaustive, and it is not the right
    authority for a molecule at all. But zero entries is a strong hint: measured
    against MP, NaCl2 (which cannot exist -- sodium is monovalent) returns nothing,
    while every real compound tried returns something, including the intermetallics
    CaPt2, FeAl and Ni3Al. Oxidation-state reasoning was the obvious alternative and
    is unusable here: it calls CaPt2, FeAl and Ni3Al all implausible, so it would
    have blocked the CaPt2 run that validated against MP to 1.4%.
    """
    key = api_key if api_key is not None else os.environ.get("MP_API_KEY")
    if not key or not isinstance(formula, str) or not formula.strip():
        return None
    try:
        if client_factory is not None:
            rester = client_factory(key)
        else:
            from mp_api.client.routes.materials.summary import SummaryRester
            rester = SummaryRester(api_key=key, timeout=timeout,
                                   notify_db_version=False, mute_progress_bars=True)
        with rester as client:
            docs = client.search(formula=formula.strip(), fields=["material_id"])
        return bool(docs)
    except Exception as exc:  # noqa: BLE001 - a hint must never fail a run
        logger.info("[plan] could not check %s against Materials Project: %s",
                    formula, exc)
        return None


class MaterialsProjectBaselines:
    """Live MP reference values for ONE material, shaped like a ``BaselineDB``.

    Scoped to a single material rather than keyed by name because the lookup
    protocol passes a display name ("Calcium diplatinide") while MP needs a
    formula or an mp-id. The state machine builds one of these per run from the
    plan's resolved target system, so ``molecule`` is only carried through onto
    the record for the report.
    """

    def __init__(
        self,
        formula: Optional[str] = None,
        *,
        mp_id: Optional[str] = None,
        space_group_number: Optional[int] = None,
        api_key: Optional[str] = None,
        client_factory: Optional[Callable[[str], Any]] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.formula = (formula or "").strip() or None
        self.mp_id = (mp_id or "").strip() or None
        self.space_group_number = space_group_number
        self.api_key = api_key if api_key is not None else os.environ.get("MP_API_KEY")
        self.client_factory = client_factory
        self.timeout = timeout
        # None = not fetched yet; {} = fetched and nothing usable came back. The
        # distinction is what keeps a miss from re-querying once per metric.
        self._doc: Optional[Dict[str, Any]] = None
        self._fetched = False
        self.unavailable_reason: Optional[str] = None

    # -- availability ------------------------------------------------------- #
    @property
    def configured(self) -> bool:
        """True when there is both a key and something to look the material up by."""
        return bool(self.api_key) and bool(self.formula or self.mp_id)

    # -- the BaselineDB protocol -------------------------------------------- #
    def lookup(self, molecule: str, prop: str) -> Optional[BaselineRecord]:
        prop_key = canonical_property(prop)
        if prop_key is None:
            return None  # r2, n_points, a_conv_Ang: never worth a request
        if not self.configured:
            self._note_unavailable(
                "MP_API_KEY is not set" if not self.api_key
                else "no formula or mp-id to look the material up by")
            return None
        doc = self._summary()
        if not doc:
            return None
        spec = MP_PROPERTIES[prop_key]
        value = spec.value(doc.get(spec.field))
        if value is None:
            return None  # MP has the material but not this property (e.g. no elasticity)
        return BaselineRecord(
            molecule=str(molecule),
            property=str(prop),
            literature_value=value,
            literature_source=self._citation(doc),
            unit=spec.unit,
        )

    # -- internals ---------------------------------------------------------- #
    def _note_unavailable(self, reason: str) -> None:
        if self.unavailable_reason is None:
            self.unavailable_reason = reason
            logger.info("[validate] no Materials Project reference: %s", reason)

    def _summary(self) -> Optional[Dict[str, Any]]:
        if self._fetched:
            return self._doc
        self._fetched = True
        try:
            self._doc = self._fetch()
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            # Deliberately broad: an auth error, a DNS failure, an API schema
            # change, a pydantic validation error. None of them is a reason to
            # fail a run that already computed its answer.
            self._note_unavailable(f"{type(exc).__name__}: {exc}")
            self._doc = None
        if self._doc is None and self.unavailable_reason is None:
            self._note_unavailable(
                f"no Materials Project entry matched {self.mp_id or self.formula}")
        return self._doc

    def _fetch(self) -> Optional[Dict[str, Any]]:
        fields = list(_IDENTITY_FIELDS) + [p.field for p in MP_PROPERTIES.values()]
        with self._rester() as rester:
            if self.mp_id:
                docs = rester.search(material_ids=[self.mp_id], fields=fields)
            else:
                docs = rester.search(formula=self.formula, fields=fields)
            return self._choose(docs)

    def _rester(self):
        """The summary rester, constructed so the timeout actually applies.

        Neither obvious route works: ``MPRester(timeout=...)`` does not forward
        the value to its sub-resters, and ``MPRester().materials.summary`` is a
        LazyImport proxy that raises AttributeError on assignment. Both were
        verified against mp-api 0.46.4. Constructing the rester directly is the
        one way to bound the request, which matters because this runs inside
        VALIDATE -- an unbounded call would stall a finished run.
        """
        if self.client_factory is not None:
            return self.client_factory(self.api_key)
        # Imported here: mp-api is optional, and nothing above needs it.
        from mp_api.client.routes.materials.summary import SummaryRester

        return SummaryRester(
            api_key=self.api_key,
            timeout=self.timeout,
            notify_db_version=False,
            mute_progress_bars=True,
        )

    def _choose(self, docs) -> Optional[Dict[str, Any]]:
        """Pick one entry: the requested space group if named, else most stable.

        A formula query returns every polymorph MP holds. Comparing a C15 Laves
        phase against whichever entry happened to come back first would be a
        wrong number presented as a reference, so the space group the plan
        resolved wins; failing that, the lowest energy above hull, which is the
        entry MP itself treats as the ground state.
        """
        rows = [self._as_dict(d) for d in (docs or [])]
        rows = [r for r in rows if r]
        if not rows:
            return None
        if self.space_group_number is not None:
            matched = [r for r in rows
                       if self._space_group(r) == int(self.space_group_number)]
            if matched:
                rows = matched
        return min(rows, key=self._hull_distance)

    @staticmethod
    def _hull_distance(row: Dict[str, Any]) -> float:
        value = row.get("energy_above_hull")
        return float(value) if isinstance(value, (int, float)) else float("inf")

    @staticmethod
    def _space_group(row: Dict[str, Any]) -> Optional[int]:
        symmetry = row.get("symmetry")
        number = (symmetry.get("number") if isinstance(symmetry, dict)
                  else getattr(symmetry, "number", None))
        return int(number) if isinstance(number, (int, float)) else None

    @staticmethod
    def _as_dict(doc) -> Dict[str, Any]:
        """Summary docs are pydantic models by default, dicts when asked.

        Read the model by attribute rather than through ``model_dump()``: the dump
        re-encodes ``material_id`` from ``MPID(mp-842)`` into its AlphaID form
        ("mp-aaaaabgk"), and a citation the researcher cannot paste into
        materialsproject.org is worse than no citation. Every field this module
        wants is a top-level scalar or mapping, so attribute access loses nothing.
        """
        if isinstance(doc, dict):
            return doc
        fields = list(_IDENTITY_FIELDS) + [p.field for p in MP_PROPERTIES.values()] + ["structure"]
        return {f: getattr(doc, f, None) for f in fields}

    def _citation(self, doc: Dict[str, Any]) -> str:
        mp_id = doc.get("material_id") or self.mp_id or "unknown"
        formula = doc.get("formula_pretty") or self.formula or ""
        # Flagged explicitly: MP's "theoretical" entries are computed, not
        # measured, and calling a DFT number a literature value would overstate
        # what the run was checked against.
        kind = "computed" if doc.get("theoretical") else "MP entry"
        return f"Materials Project {mp_id}{f' ({formula})' if formula else ''} [{kind}]"


def reference_structure(formula: Optional[str] = None, *, mp_id: Optional[str] = None,
                        space_group_number: Optional[int] = None,
                        api_key: Optional[str] = None,
                        client_factory: Optional[Callable[[str], Any]] = None,
                        timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Optional[Dict[str, Any]]:
    """Materials Project's primitive cell for a crystal, for BUILD (or None).

    ``{"mp_id", "formula", "space_group_number", "space_group", "primitive_sites",
    "volume_per_atom", "poscar"}`` -- the cell itself (VASP POSCAR text, which
    ``ase.io.read(..., format="vasp")`` loads) plus the two facts the structure
    guard needs: the space group alone did not catch run 75f06090, whose 4-atom
    "diamond Si" was Fd-3m too, but had twice the atoms per primitive cell and
    half the volume per atom. ``None`` -- no key, no network, no entry -- is
    "no reference", never a failure: BUILD carries on without one.
    """
    finder = MaterialsProjectBaselines(formula, mp_id=mp_id, space_group_number=space_group_number,
                                       api_key=api_key, client_factory=client_factory,
                                       timeout=timeout)
    if not finder.configured:
        return None
    try:
        with finder._rester() as rester:
            fields = list(_IDENTITY_FIELDS) + ["structure"]
            if finder.mp_id:
                docs = rester.search(material_ids=[finder.mp_id], fields=fields)
            else:
                docs = rester.search(formula=finder.formula, fields=fields)
        doc = finder._choose(docs)
        structure = (doc or {}).get("structure")
        if structure is None:
            return None
        if isinstance(structure, dict):
            from pymatgen.core import Structure
            structure = Structure.from_dict(structure)
        primitive = structure.get_primitive_structure()
        symmetry = doc.get("symmetry") or {}
        symbol = (symmetry.get("symbol") if isinstance(symmetry, dict)
                  else getattr(symmetry, "symbol", None))
        return {
            "mp_id": str(doc.get("material_id")),
            "formula": doc.get("formula_pretty"),
            "space_group_number": MaterialsProjectBaselines._space_group(doc),
            "space_group": symbol,
            "primitive_sites": len(primitive),
            "volume_per_atom": round(primitive.volume / len(primitive), 4),
            "poscar": primitive.to(fmt="poscar"),
        }
    except Exception as exc:  # noqa: BLE001 - a missing reference must never fail BUILD
        logger.info("[build] no Materials Project structure for %s: %s",
                    mp_id or formula, exc)
        return None
