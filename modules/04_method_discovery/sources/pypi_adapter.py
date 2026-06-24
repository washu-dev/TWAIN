"""PyPI registry adapter (Story 4.3).

Looks up Python packages on PyPI and normalizes their metadata into
`RegistryEntry` objects so the discovery engine can rank them alongside the
local seed registry.

Design notes:
  * The HTTP fetcher is injectable (`fetch` callable) so the adapter is fully
    testable offline; the default uses the stdlib (no extra dependency).
  * Results are cached in-memory with a 1-day TTL; the clock is injectable.
  * Network/parse failures are swallowed and yield no candidate (discovery is
    best-effort augmentation, not a hard dependency).
  * External candidates default to trust tier 2 (popular OSS) until vetted.
"""

import json
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from method_discovery.license_utils import classify_license
from method_discovery.registry_loader import Maturity, RegistryEntry

PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
DEFAULT_TTL_SECONDS = 86_400  # 1 day
EXTERNAL_TRUST_TIER = 2


def _default_fetch(url: str) -> dict:
    """Fetch and parse JSON from a URL using the standard library."""
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "TWAIN-discovery/0.1"})
    with urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted URL templates)
        return json.loads(resp.read().decode("utf-8"))


def _maturity_from_classifiers(classifiers: List[str]) -> Maturity:
    """Map PyPI 'Development Status' trove classifiers to a Maturity stage."""
    text = " ".join(classifiers).lower()
    if "production/stable" in text or "6 - mature" in text:
        return Maturity.STABLE
    if "4 - beta" in text:
        return Maturity.BETA
    if "3 - alpha" in text or "2 - pre-alpha" in text or "1 - planning" in text:
        return Maturity.ALPHA
    if "7 - inactive" in text:
        return Maturity.DEPRECATED
    return Maturity.BETA


def _license_from_info(info: dict, classifiers: List[str]) -> str:
    """Best-effort license string from the 'license' field or trove classifiers."""
    lic = (info.get("license") or "").strip()
    if lic and len(lic) < 40:  # skip full license texts that some packages dump here
        return lic
    for c in classifiers:
        if c.startswith("License ::"):
            return c.split("::")[-1].strip()
    return "NOASSERTION"


def normalize_pypi(payload: dict, fallback_tags: Optional[List[str]] = None) -> RegistryEntry:
    """Convert a PyPI `/pypi/<pkg>/json` payload into a RegistryEntry."""
    info = payload.get("info", {})
    classifiers = info.get("classifiers", []) or []
    name = info.get("name") or "unknown"

    keywords = info.get("keywords") or ""
    if isinstance(keywords, str):
        tags = [k.strip() for k in keywords.replace(",", " ").split() if k.strip()]
    else:
        tags = [str(k) for k in keywords]
    if not tags:
        tags = list(fallback_tags) if fallback_tags else ["external_candidate"]

    project_urls = info.get("project_urls") or {}
    repo_url = (
        project_urls.get("Source")
        or project_urls.get("Homepage")
        or project_urls.get("Repository")
        or info.get("home_page")
        or None
    )

    license_str = _license_from_info(info, classifiers)
    return RegistryEntry(
        id=f"pypi:{name.lower()}",
        name=name,
        version=info.get("version") or "unknown",
        description=info.get("summary") or name,
        capability_tags=tags,
        input_formats=[],
        output_properties=[],
        license=license_str,
        license_class=classify_license(license_str),
        maturity=_maturity_from_classifiers(classifiers),
        trust_tier=EXTERNAL_TRUST_TIER,
        author=info.get("author") or None,
        repo_url=repo_url,
    )


class PyPIAdapter:
    def __init__(
        self,
        fetch: Optional[Callable[[str], dict]] = None,
        clock: Optional[Callable[[], float]] = None,
        cache_ttl: int = DEFAULT_TTL_SECONDS,
    ):
        self._fetch = fetch or _default_fetch
        self._clock = clock or time.time
        self._cache_ttl = cache_ttl
        # package name -> (timestamp, entry or None)
        self._cache: Dict[str, Tuple[float, Optional[RegistryEntry]]] = {}

    def _cache_get(self, key: str):
        hit = self._cache.get(key)
        if hit is None:
            return None
        ts, value = hit
        if self._clock() - ts > self._cache_ttl:
            del self._cache[key]
            return None
        return (value,)  # wrap so a cached None is distinguishable from a miss

    def lookup(self, package: str, fallback_tags: Optional[List[str]] = None) -> List[RegistryEntry]:
        """Look up a single package. Returns a 0- or 1-element list.

        Empty list means not found or the fetch failed; a cached negative result
        is honoured so repeated misses don't re-hit the network within the TTL.
        """
        key = package.lower()
        cached = self._cache_get(key)
        if cached is not None:
            entry = cached[0]
            return [entry] if entry is not None else []

        try:
            payload = self._fetch(PYPI_JSON_URL.format(package=package))
            entry = normalize_pypi(payload, fallback_tags=fallback_tags)
        except (HTTPError, URLError, KeyError, ValueError, json.JSONDecodeError):
            entry = None

        self._cache[key] = (self._clock(), entry)
        return [entry] if entry is not None else []
