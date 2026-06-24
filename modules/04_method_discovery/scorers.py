"""Candidate scoring rubric for module 04_method_discovery (Story 4.2).

Ranks RegistryEntry candidates against a discovery query by combining five
component scores into a composite in [0, 1]:

    composite = 0.40 * relevance
              + 0.20 * maturity
              + 0.15 * license
              + 0.15 * trust
              + 0.10 * reproducibility

Component definitions:
  * relevance       fraction of the query's capability tags matched by the tool
                    (with a small bonus for matching the requested input format).
  * maturity        release stage (alpha/beta/stable/deprecated) blended with
                    community adoption (stars + citations).
  * license         permissive=1.0, copyleft=0.5, restrictive=0.0.
  * trust           trust tier 0=1.0, 1=0.95, 2=0.8, 3=0.5.
  * reproducibility evidence: has_paper, has_tests, has_examples (each 1/3).

The scorer is deterministic: ties in composite score are broken by entry id so
top-k rankings are stable.
"""

from dataclasses import dataclass, field
from math import log10
from typing import Dict, List, Optional, Union

from method_discovery.registry_loader import (
    LicenseClass,
    Maturity,
    RegistryEntry,
)

# Composite weights (sum to 1.0).
WEIGHTS: Dict[str, float] = {
    "relevance": 0.40,
    "maturity": 0.20,
    "license": 0.15,
    "trust": 0.15,
    "reproducibility": 0.10,
}

_STAGE_WEIGHT: Dict[Maturity, float] = {
    Maturity.ALPHA: 0.3,
    Maturity.BETA: 0.6,
    Maturity.STABLE: 0.9,
    Maturity.DEPRECATED: 0.0,
}

_LICENSE_SCORE: Dict[LicenseClass, float] = {
    LicenseClass.PERMISSIVE: 1.0,
    LicenseClass.COPYLEFT: 0.5,
    LicenseClass.RESTRICTIVE: 0.0,
}

_TRUST_SCORE: Dict[int, float] = {0: 1.0, 1: 0.95, 2: 0.8, 3: 0.5}

# Adoption saturates at 10^5 combined stars + citations.
_ADOPTION_SATURATION_LOG = 5.0


@dataclass
class DiscoveryQuery:
    """A lightweight discovery request derived from the upstream IntentSpec/GoalGraph.

    `capability_tags` is the set of capabilities the goal needs (e.g.
    ["property_prediction"]). `input_format` is the data format on hand (e.g.
    "SMILES"), used for a small relevance bonus when a tool accepts it.
    """

    capability_tags: List[str]
    input_format: Optional[str] = None

    def __post_init__(self):
        if type(self.capability_tags) is not list or len(self.capability_tags) == 0:
            raise ValueError("DiscoveryQuery capability_tags must be a non-empty list")
        if any(type(t) is not str for t in self.capability_tags):
            raise ValueError("DiscoveryQuery capability_tags must be a list of str")
        if self.input_format is not None and type(self.input_format) is not str:
            raise ValueError("DiscoveryQuery input_format must be a str when provided")


@dataclass
class ComponentScores:
    relevance: float
    maturity: float
    license: float
    trust: float
    reproducibility: float

    def as_dict(self) -> Dict[str, float]:
        return {
            "relevance": self.relevance,
            "maturity": self.maturity,
            "license": self.license,
            "trust": self.trust,
            "reproducibility": self.reproducibility,
        }


@dataclass
class ScoredCandidate:
    entry: RegistryEntry
    components: ComponentScores
    composite: float
    rank: int = 0


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def relevance_score(entry: RegistryEntry, query: DiscoveryQuery) -> float:
    """Fraction of query capability tags matched, plus a small input-format bonus.

    Base score is the overlap coefficient over the query's tags (case-insensitive).
    If the tool also accepts the requested input format, add a 0.1 bonus (capped at 1.0).
    """
    tool_tags = {t.lower() for t in entry.capability_tags}
    wanted = {t.lower() for t in query.capability_tags}
    matched = len(wanted & tool_tags)
    base = matched / len(wanted)

    bonus = 0.0
    if query.input_format is not None:
        formats = {f.lower() for f in entry.input_formats}
        if query.input_format.lower() in formats:
            bonus = 0.1
    return _clamp01(base + bonus)


def adoption_score(entry: RegistryEntry) -> float:
    """Community-adoption proxy in [0, 1] from stars + citations (log-scaled)."""
    total = (entry.stars or 0) + (entry.citations or 0)
    if total <= 0:
        return 0.0
    return _clamp01(log10(total + 1) / _ADOPTION_SATURATION_LOG)


def maturity_score(entry: RegistryEntry) -> float:
    """Blend release stage (70%) with community adoption (30%).

    Monotonic in stars/citations when the stage is held fixed.
    """
    stage = _STAGE_WEIGHT[entry.maturity]
    return _clamp01(0.7 * stage + 0.3 * adoption_score(entry))


def license_score(entry: RegistryEntry) -> float:
    return _LICENSE_SCORE[entry.license_class]


def trust_score(entry: RegistryEntry) -> float:
    return _TRUST_SCORE[entry.trust_tier]


def reproducibility_score(entry: RegistryEntry) -> float:
    """Evidence of reproducibility: paper, tests, examples (each worth 1/3)."""
    signals = [entry.has_paper, entry.has_tests, entry.has_examples]
    return sum(1 for s in signals if s) / 3.0


def component_scores(entry: RegistryEntry, query: DiscoveryQuery) -> ComponentScores:
    return ComponentScores(
        relevance=relevance_score(entry, query),
        maturity=maturity_score(entry),
        license=license_score(entry),
        trust=trust_score(entry),
        reproducibility=reproducibility_score(entry),
    )


def composite_score(components: ComponentScores) -> float:
    parts = components.as_dict()
    total = sum(WEIGHTS[name] * parts[name] for name in WEIGHTS)
    return _clamp01(total)


def score_entry(entry: RegistryEntry, query: DiscoveryQuery) -> ScoredCandidate:
    comps = component_scores(entry, query)
    return ScoredCandidate(entry=entry, components=comps, composite=composite_score(comps))


def rank_candidates(
    entries: List[RegistryEntry],
    query: DiscoveryQuery,
    top_k: Optional[int] = 3,
) -> List[ScoredCandidate]:
    """Score and rank candidates, returning the top-k (default 3).

    Ranking is deterministic: sorted by composite score descending, ties broken
    by entry id ascending. `rank` is assigned 1-based. Pass top_k=None for all.
    """
    scored = [score_entry(e, query) for e in entries]
    scored.sort(key=lambda c: (-c.composite, c.entry.id))
    for i, c in enumerate(scored, start=1):
        c.rank = i
    if top_k is not None:
        return scored[:top_k]
    return scored


if __name__ == "__main__":
    from method_discovery.registry_loader import RegistryLoader

    loader = RegistryLoader()
    q = DiscoveryQuery(capability_tags=["property_prediction"], input_format="SMILES")
    for cand in rank_candidates(loader.entries(), q):
        c = cand.components
        print(
            f"#{cand.rank} {cand.entry.id:16s} composite={cand.composite:.3f}  "
            f"(rel={c.relevance:.2f} mat={c.maturity:.2f} lic={c.license:.2f} "
            f"trust={c.trust:.2f} repro={c.reproducibility:.2f})"
        )
