"""Method discovery registry: dataclasses and loader for module 04_method_discovery.

Mirrors schemas/discovery_registry.schema.json. Provides:
  * RegistryEntry / DiscoveryRegistry dataclasses with nested-dict coercion
    (same pattern as intake.intent_spec and plan_synthesizer.execution_plan).
  * RegistryLoader: reads the registry JSON from disk and caches it in memory,
    invalidating the cache when the file's modification time changes.

The registry is the curated catalog that the discovery engine searches against
(Story 4.1). Story 4.2 scores these entries; Story 4.3 augments them with
external sources.
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Union
import json


class LicenseClass(str, Enum):
    PERMISSIVE = "permissive"
    COPYLEFT = "copyleft"
    RESTRICTIVE = "restrictive"


class Maturity(str, Enum):
    ALPHA = "alpha"
    BETA = "beta"
    STABLE = "stable"
    DEPRECATED = "deprecated"


def _repo_root() -> Path:
    """Locate the repo root by walking up to the directory holding pixi.toml.

    Robust to this module's depth in the tree (e.g. top-level vs modules/NN_*).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pixi.toml").exists():
            return parent
    return here.parents[1]


# Default location of the seed registry, relative to the repo root.
DEFAULT_REGISTRY_PATH = _repo_root() / "configs" / "discovery_registry.json"


@dataclass
class RegistryEntry:
    id: str
    name: str
    version: str
    description: str
    capability_tags: List[str]
    input_formats: List[str]
    output_properties: List[str]
    license: str
    license_class: Union[str, LicenseClass]
    maturity: Union[str, Maturity]
    trust_tier: int
    author: Optional[str] = None
    repo_url: Optional[str] = None
    # The project's own site, shown as a link on the app's library list. Declared
    # here because RegistryEntry(**entry) passes every key straight in, so an
    # undeclared field is a TypeError rather than an ignored extra -- unlike
    # CalculatorEntry, which filters to its known fields. Adding `homepage` to the
    # registry without this line broke 93 tests through RegistryLoader.
    homepage: Optional[str] = None
    paper_doi: Optional[str] = None
    uncertainty_method: Optional[str] = None
    stars: Optional[int] = None
    citations: Optional[int] = None
    has_tests: bool = False
    has_examples: bool = False

    def __post_init__(self):
        if not self.id or type(self.id) is not str:
            raise ValueError("RegistryEntry id must be a non-empty str")
        if not self.name or type(self.name) is not str:
            raise ValueError("RegistryEntry name must be a non-empty str")
        if not self.version or type(self.version) is not str:
            raise ValueError("RegistryEntry version must be a non-empty str")
        if not self.description or type(self.description) is not str:
            raise ValueError("RegistryEntry description must be a non-empty str")

        for attr in ("capability_tags", "input_formats", "output_properties"):
            value = getattr(self, attr)
            if type(value) is not list or any(type(v) is not str for v in value):
                raise ValueError(f"RegistryEntry {attr} must be a list of str")
        if len(self.capability_tags) == 0:
            raise ValueError("RegistryEntry capability_tags must not be empty")

        if not self.license or type(self.license) is not str:
            raise ValueError("RegistryEntry license must be a non-empty str (SPDX id)")

        if type(self.license_class) is str:
            try:
                self.license_class = LicenseClass(self.license_class)
            except ValueError:
                raise ValueError(
                    f"Invalid license_class. Viable options: {[c.value for c in LicenseClass]}"
                )
        elif not isinstance(self.license_class, LicenseClass):
            raise ValueError("license_class must be a LicenseClass enum or matching string")

        if type(self.maturity) is str:
            try:
                self.maturity = Maturity(self.maturity)
            except ValueError:
                raise ValueError(
                    f"Invalid maturity. Viable options: {[m.value for m in Maturity]}"
                )
        elif not isinstance(self.maturity, Maturity):
            raise ValueError("maturity must be a Maturity enum or matching string")

        if type(self.trust_tier) is not int or not (0 <= self.trust_tier <= 3):
            raise ValueError("RegistryEntry trust_tier must be an int in [0, 3]")

        if self.author is not None and type(self.author) is not str:
            raise ValueError("RegistryEntry author must be a str when provided")
        if self.repo_url is not None and type(self.repo_url) is not str:
            raise ValueError("RegistryEntry repo_url must be a str when provided")
        if self.paper_doi is not None and type(self.paper_doi) is not str:
            raise ValueError("RegistryEntry paper_doi must be a str when provided")
        if self.uncertainty_method is not None and type(self.uncertainty_method) is not str:
            raise ValueError("RegistryEntry uncertainty_method must be a str when provided")

        if self.stars is not None and (type(self.stars) is not int or self.stars < 0):
            raise ValueError("RegistryEntry stars must be a non-negative int when provided")
        if self.citations is not None and (type(self.citations) is not int or self.citations < 0):
            raise ValueError("RegistryEntry citations must be a non-negative int when provided")
        if type(self.has_tests) is not bool:
            raise ValueError("RegistryEntry has_tests must be a bool")
        if type(self.has_examples) is not bool:
            raise ValueError("RegistryEntry has_examples must be a bool")

    @property
    def has_paper(self) -> bool:
        """A canonical paper exists iff a DOI is recorded."""
        return bool(self.paper_doi)


@dataclass
class DiscoveryRegistry:
    registry_version: str
    entries: List[Union[RegistryEntry, dict]] = field(default_factory=list)

    def __post_init__(self):
        if not self.registry_version or type(self.registry_version) is not str:
            raise ValueError("DiscoveryRegistry registry_version must be a non-empty str")
        if type(self.entries) is not list:
            raise ValueError("DiscoveryRegistry entries must be a list")

        coerced: List[RegistryEntry] = []
        seen_ids: set = set()
        for e in self.entries:
            if type(e) is dict:
                e = RegistryEntry(**e)
            elif type(e) is not RegistryEntry:
                raise ValueError("DiscoveryRegistry entries items must be dict or RegistryEntry")
            if e.id in seen_ids:
                raise ValueError(f"Duplicate registry entry id: {e.id!r}")
            seen_ids.add(e.id)
            coerced.append(e)
        self.entries = coerced

    def by_id(self) -> Dict[str, RegistryEntry]:
        return {e.id: e for e in self.entries}


class RegistryLoader:
    """Load the discovery registry from disk with mtime-based cache invalidation.

    The first call to `load()` reads and parses the file; subsequent calls return
    the cached `DiscoveryRegistry` unless the file's modification time has changed,
    in which case it is reloaded. Pass `force=True` to bypass the cache.
    """

    def __init__(self, path: Union[str, Path] = DEFAULT_REGISTRY_PATH):
        self.path = Path(path)
        self._cache: Optional[DiscoveryRegistry] = None
        self._cached_mtime: Optional[float] = None

    def load(self, force: bool = False) -> DiscoveryRegistry:
        if not self.path.exists():
            raise FileNotFoundError(f"Registry file not found: {self.path}")
        mtime = self.path.stat().st_mtime
        if force or self._cache is None or mtime != self._cached_mtime:
            with open(self.path, "r") as f:
                raw = json.load(f)
            self._cache = DiscoveryRegistry(**raw)
            self._cached_mtime = mtime
        return self._cache

    def entries(self, force: bool = False) -> List[RegistryEntry]:
        return self.load(force=force).entries

    def find_by_capability(self, tag: str, force: bool = False) -> List[RegistryEntry]:
        """Return entries advertising the given capability tag (case-insensitive)."""
        needle = tag.lower()
        return [
            e for e in self.entries(force=force)
            if any(t.lower() == needle for t in e.capability_tags)
        ]

    def find_by_input_format(self, fmt: str, force: bool = False) -> List[RegistryEntry]:
        """Return entries that accept the given input data format (case-insensitive)."""
        needle = fmt.lower()
        return [
            e for e in self.entries(force=force)
            if any(f.lower() == needle for f in e.input_formats)
        ]


if __name__ == "__main__":
    loader = RegistryLoader()
    registry = loader.load()
    print(f"Loaded registry v{registry.registry_version} with {len(registry.entries)} entries")
    for entry in registry.entries:
        print(f"  - {entry.id} ({entry.maturity.value}, trust tier {entry.trust_tier})")
