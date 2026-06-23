"""Merge local + external discovery results and orchestrate a rate-limited session (Story 4.3).

`merge_entries` produces the deduplicated union of the local seed registry and
externally discovered candidates, preferring local (vetted) entries on conflict.

`DiscoverySession` ties the PyPI and GitHub adapters together and enforces a hard
cap on external queries per session (default 3) so a single discovery run cannot
hammer external APIs.
"""

import re
from typing import List, Optional

from method_discovery.registry_loader import RegistryEntry
from method_discovery.scorers import DiscoveryQuery
from method_discovery.sources.github_adapter import GitHubAdapter
from method_discovery.sources.pypi_adapter import PyPIAdapter

DEFAULT_MAX_EXTERNAL_QUERIES = 3

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _canonical_name(entry: RegistryEntry) -> str:
    """Source-agnostic dedup key derived from the tool's display name.

    Uses the name rather than the id because external ids carry source-specific
    context (e.g. GitHub's 'owner/repo'). Non-alphanumeric characters are removed
    so 'openff-toolkit' and 'openff_toolkit' collapse to the same key.
    """
    return _NON_ALNUM.sub("", entry.name.lower())


def merge_entries(
    local: List[RegistryEntry],
    external: List[RegistryEntry],
) -> List[RegistryEntry]:
    """Union of local + external entries, deduplicated by canonical name.

    Local entries always win over external ones with the same canonical name.
    Among competing external entries, the one with more stars wins (ties: first seen).
    """
    result: List[RegistryEntry] = list(local)
    seen = {_canonical_name(e) for e in local}

    best_external: dict = {}
    for e in external:
        key = _canonical_name(e)
        if key in seen:
            continue
        incumbent = best_external.get(key)
        if incumbent is None or (e.stars or 0) > (incumbent.stars or 0):
            best_external[key] = e

    result.extend(best_external.values())
    return result


class RateLimitExceeded(RuntimeError):
    """Raised when a session is asked to exceed its external-query budget in strict mode."""


class DiscoverySession:
    """A single discovery run that augments local entries with external sources.

    Enforces `max_external_queries`: each PyPI package lookup and each GitHub
    search counts as one query. Once the budget is spent, further external work
    is skipped (or raises in strict mode), but local results are always returned.
    """

    def __init__(
        self,
        local_entries: List[RegistryEntry],
        pypi: Optional[PyPIAdapter] = None,
        github: Optional[GitHubAdapter] = None,
        max_external_queries: int = DEFAULT_MAX_EXTERNAL_QUERIES,
        strict: bool = False,
    ):
        self.local_entries = list(local_entries)
        self.pypi = pypi
        self.github = github
        self.max_external_queries = max_external_queries
        self.strict = strict
        self.external_queries_used = 0

    @property
    def budget_remaining(self) -> int:
        return max(0, self.max_external_queries - self.external_queries_used)

    def _consume_query(self) -> bool:
        if self.external_queries_used >= self.max_external_queries:
            if self.strict:
                raise RateLimitExceeded(
                    f"External query budget of {self.max_external_queries} exhausted"
                )
            return False
        self.external_queries_used += 1
        return True

    def discover(
        self,
        query: DiscoveryQuery,
        pypi_packages: Optional[List[str]] = None,
        github_query: Optional[str] = None,
    ) -> List[RegistryEntry]:
        """Return the merged local + external candidate set for a query.

        `pypi_packages` are specific package names to look up; `github_query` is a
        free-text repo search. The goal's capability tags are passed to adapters as
        fallback tags so externally discovered tools remain relevance-rankable.
        """
        external: List[RegistryEntry] = []
        fallback_tags = query.capability_tags

        if self.pypi and pypi_packages:
            for name in pypi_packages:
                if not self._consume_query():
                    break
                external.extend(self.pypi.lookup(name, fallback_tags=fallback_tags))

        if self.github and github_query:
            if self._consume_query():
                external.extend(self.github.search(github_query, fallback_tags=fallback_tags))

        return merge_entries(self.local_entries, external)
