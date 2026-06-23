"""GitHub registry adapter (Story 4.3).

Searches GitHub repositories matching goal-derived keywords and normalizes the
results into `RegistryEntry` objects.

Same design principles as the PyPI adapter: injectable fetcher + clock, 1-day
TTL cache, graceful failure handling, and external trust tier 2.
"""

import json
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.error import URLError, HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from method_discovery.license_utils import classify_license
from method_discovery.registry_loader import Maturity, RegistryEntry

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page={per_page}"
DEFAULT_TTL_SECONDS = 86_400  # 1 day
EXTERNAL_TRUST_TIER = 2


def _default_fetch(url: str) -> dict:
    req = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "TWAIN-discovery/0.1",
        },
    )
    with urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted URL template)
        return json.loads(resp.read().decode("utf-8"))


def normalize_github(repo: dict, fallback_tags: Optional[List[str]] = None) -> RegistryEntry:
    """Convert one GitHub search 'items' element into a RegistryEntry."""
    full_name = repo.get("full_name") or repo.get("name") or "unknown"
    name = repo.get("name") or full_name

    topics = repo.get("topics") or []
    tags = [str(t) for t in topics if t]
    if not tags:
        tags = list(fallback_tags) if fallback_tags else ["external_candidate"]

    license_obj = repo.get("license") or {}
    license_str = (license_obj.get("spdx_id") or license_obj.get("key") or "NOASSERTION")
    if license_str in (None, "", "NOASSERTION"):
        license_str = "NOASSERTION"

    owner = repo.get("owner") or {}
    return RegistryEntry(
        id=f"github:{full_name.lower()}",
        name=name,
        version="unknown",
        description=repo.get("description") or name,
        capability_tags=tags,
        input_formats=[],
        output_properties=[],
        license=license_str,
        license_class=classify_license(license_str),
        maturity=Maturity.BETA,  # repos rarely declare maturity; assume beta
        trust_tier=EXTERNAL_TRUST_TIER,
        author=owner.get("login") or None,
        repo_url=repo.get("html_url") or None,
        stars=repo.get("stargazers_count"),
    )


class GitHubAdapter:
    def __init__(
        self,
        fetch: Optional[Callable[[str], dict]] = None,
        clock: Optional[Callable[[], float]] = None,
        cache_ttl: int = DEFAULT_TTL_SECONDS,
        per_page: int = 5,
    ):
        self._fetch = fetch or _default_fetch
        self._clock = clock or time.time
        self._cache_ttl = cache_ttl
        self._per_page = per_page
        # query string -> (timestamp, entries)
        self._cache: Dict[str, Tuple[float, List[RegistryEntry]]] = {}

    def _cache_get(self, key: str) -> Optional[List[RegistryEntry]]:
        hit = self._cache.get(key)
        if hit is None:
            return None
        ts, value = hit
        if self._clock() - ts > self._cache_ttl:
            del self._cache[key]
            return None
        return value

    def search(self, query: str, fallback_tags: Optional[List[str]] = None) -> List[RegistryEntry]:
        """Search repositories for `query`; returns up to `per_page` normalized entries."""
        key = query.lower()
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        try:
            url = GITHUB_SEARCH_URL.format(query=quote(query), per_page=self._per_page)
            payload = self._fetch(url)
            items = payload.get("items", []) or []
            entries = [normalize_github(item, fallback_tags=fallback_tags) for item in items]
        except (HTTPError, URLError, KeyError, ValueError, json.JSONDecodeError):
            entries = []

        self._cache[key] = (self._clock(), entries)
        return entries
