"""Unit tests for Story 4.3: external registry adapters + merger.

Covers:
  * License classification.
  * PyPI / GitHub normalization into RegistryEntry.
  * Adapter caching (TTL), cache-miss refetch, and graceful failure.
  * merge_entries deduplication (local-wins, star-preference among externals).
  * DiscoverySession external-query rate limiting (lenient and strict).

All network access is stubbed via injectable fetchers, so these run offline.

Run from the repo root with:  pixi run pytest tests/unit/test_external_discovery.py
"""
import pytest

from method_discovery.license_utils import classify_license
from method_discovery.registry_loader import LicenseClass, Maturity, RegistryEntry
from method_discovery.registry_merger import (
    DiscoverySession,
    RateLimitExceeded,
    merge_entries,
)
from method_discovery.scorers import DiscoveryQuery
from method_discovery.sources.github_adapter import GitHubAdapter, normalize_github
from method_discovery.sources.pypi_adapter import PyPIAdapter, normalize_pypi


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class StubFetcher:
    """Callable that returns canned payloads keyed by substring, counting calls."""

    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        for needle, payload in self.responses.items():
            if needle in url:
                return payload
        raise KeyError(f"no stub for {url}")


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


PYPI_DEEPCHEM = {
    "info": {
        "name": "deepchem",
        "version": "2.8.0",
        "summary": "Deep learning for chemistry.",
        "license": "MIT",
        "keywords": "machine_learning property_prediction",
        "author": "DeepChem",
        "home_page": "https://deepchem.io",
        "project_urls": {"Source": "https://github.com/deepchem/deepchem"},
        "classifiers": ["Development Status :: 5 - Production/Stable"],
    }
}

GITHUB_SEARCH = {
    "items": [
        {
            "full_name": "grimme-lab/xtb",
            "name": "xtb",
            "description": "Semiempirical tight binding.",
            "stargazers_count": 700,
            "topics": ["quantum_chemistry", "chemistry"],
            "license": {"spdx_id": "LGPL-3.0"},
            "owner": {"login": "grimme-lab"},
            "html_url": "https://github.com/grimme-lab/xtb",
        }
    ]
}


# --------------------------------------------------------------------------- #
# License classification
# --------------------------------------------------------------------------- #
class TestClassifyLicense:
    @pytest.mark.parametrize(
        "spdx,expected",
        [
            ("MIT", LicenseClass.PERMISSIVE),
            ("Apache-2.0", LicenseClass.PERMISSIVE),
            ("BSD-3-Clause", LicenseClass.PERMISSIVE),
            ("GPL-3.0-only", LicenseClass.COPYLEFT),
            ("LGPL-3.0", LicenseClass.COPYLEFT),
            ("AGPL-3.0", LicenseClass.COPYLEFT),
            ("MPL-2.0", LicenseClass.COPYLEFT),
            ("NOASSERTION", LicenseClass.RESTRICTIVE),
            ("", LicenseClass.RESTRICTIVE),
            ("Proprietary", LicenseClass.RESTRICTIVE),
        ],
    )
    def test_classification(self, spdx, expected):
        assert classify_license(spdx) == expected

    def test_lgpl_not_misclassified_as_permissive(self):
        assert classify_license("LGPLv3") == LicenseClass.COPYLEFT


# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #
class TestNormalize:
    def test_pypi_normalize(self):
        e = normalize_pypi(PYPI_DEEPCHEM)
        assert e.id == "pypi:deepchem"
        assert e.name == "deepchem"
        assert e.trust_tier == 2
        assert e.license_class == LicenseClass.PERMISSIVE
        assert e.maturity == Maturity.STABLE
        assert "property_prediction" in e.capability_tags

    def test_pypi_normalize_fallback_tags(self):
        payload = {"info": {"name": "obscure", "version": "0.1", "summary": "x"}}
        e = normalize_pypi(payload, fallback_tags=["property_prediction"])
        assert e.capability_tags == ["property_prediction"]

    def test_github_normalize(self):
        e = normalize_github(GITHUB_SEARCH["items"][0])
        assert e.id == "github:grimme-lab/xtb"
        assert e.stars == 700
        assert e.trust_tier == 2
        assert e.license_class == LicenseClass.COPYLEFT
        assert "quantum_chemistry" in e.capability_tags


# --------------------------------------------------------------------------- #
# PyPI adapter
# --------------------------------------------------------------------------- #
class TestPyPIAdapter:
    def test_lookup_success(self):
        fetch = StubFetcher({"deepchem": PYPI_DEEPCHEM})
        adapter = PyPIAdapter(fetch=fetch)
        result = adapter.lookup("deepchem")
        assert len(result) == 1
        assert result[0].id == "pypi:deepchem"
        assert len(fetch.calls) == 1

    def test_cache_prevents_refetch(self):
        fetch = StubFetcher({"deepchem": PYPI_DEEPCHEM})
        adapter = PyPIAdapter(fetch=fetch, clock=FakeClock())
        adapter.lookup("deepchem")
        adapter.lookup("deepchem")
        assert len(fetch.calls) == 1

    def test_cache_expires_after_ttl(self):
        fetch = StubFetcher({"deepchem": PYPI_DEEPCHEM})
        clock = FakeClock(1000.0)
        adapter = PyPIAdapter(fetch=fetch, clock=clock, cache_ttl=100)
        adapter.lookup("deepchem")
        clock.t += 101
        adapter.lookup("deepchem")
        assert len(fetch.calls) == 2

    def test_negative_result_cached(self):
        fetch = StubFetcher(error=ValueError("boom"))
        adapter = PyPIAdapter(fetch=fetch, clock=FakeClock())
        assert adapter.lookup("nope") == []
        assert adapter.lookup("nope") == []
        assert len(fetch.calls) == 1  # negative result cached, no refetch

    def test_failure_returns_empty(self):
        fetch = StubFetcher(error=ValueError("boom"))
        adapter = PyPIAdapter(fetch=fetch)
        assert adapter.lookup("anything") == []


# --------------------------------------------------------------------------- #
# GitHub adapter
# --------------------------------------------------------------------------- #
class TestGitHubAdapter:
    def test_search_success(self):
        fetch = StubFetcher({"api.github.com": GITHUB_SEARCH})
        adapter = GitHubAdapter(fetch=fetch)
        results = adapter.search("quantum chemistry")
        assert len(results) == 1
        assert results[0].id == "github:grimme-lab/xtb"

    def test_search_cached(self):
        fetch = StubFetcher({"api.github.com": GITHUB_SEARCH})
        adapter = GitHubAdapter(fetch=fetch, clock=FakeClock())
        adapter.search("quantum chemistry")
        adapter.search("quantum chemistry")
        assert len(fetch.calls) == 1

    def test_failure_returns_empty(self):
        fetch = StubFetcher(error=ValueError("boom"))
        adapter = GitHubAdapter(fetch=fetch)
        assert adapter.search("anything") == []


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #
def make_local(id_, name=None, stars=None):
    return RegistryEntry(
        id=id_,
        name=name or id_,
        version="1.0",
        description="local tool",
        capability_tags=["property_prediction"],
        input_formats=["SMILES"],
        output_properties=["solubility"],
        license="MIT",
        license_class="permissive",
        maturity="stable",
        trust_tier=1,
        stars=stars,
    )


class TestMerge:
    def test_external_dedup_against_local(self):
        local = [make_local("deepchem")]
        external = [normalize_pypi(PYPI_DEEPCHEM)]  # canonical name 'deepchem'
        merged = merge_entries(local, external)
        ids = [e.id for e in merged]
        assert ids == ["deepchem"]  # local wins, external dropped

    def test_external_added_when_new(self):
        local = [make_local("rdkit")]
        external = [normalize_pypi(PYPI_DEEPCHEM)]
        merged = merge_entries(local, external)
        ids = {e.id for e in merged}
        assert ids == {"rdkit", "pypi:deepchem"}

    def test_external_dedup_prefers_more_stars(self):
        low = normalize_github(
            {"full_name": "x/tool", "name": "tool", "stargazers_count": 10,
             "license": {"spdx_id": "MIT"}, "owner": {"login": "x"}}
        )
        high = normalize_github(
            {"full_name": "y/tool", "name": "tool", "stargazers_count": 999,
             "license": {"spdx_id": "MIT"}, "owner": {"login": "y"}}
        )
        merged = merge_entries([], [low, high])
        assert len(merged) == 1
        assert merged[0].stars == 999


# --------------------------------------------------------------------------- #
# DiscoverySession rate limiting
# --------------------------------------------------------------------------- #
class TestDiscoverySession:
    def test_combines_local_and_external(self):
        pypi = PyPIAdapter(fetch=StubFetcher({"deepchem": PYPI_DEEPCHEM}), clock=FakeClock())
        session = DiscoverySession(local_entries=[make_local("rdkit")], pypi=pypi)
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        merged = session.discover(q, pypi_packages=["deepchem"])
        ids = {e.id for e in merged}
        assert "rdkit" in ids and "pypi:deepchem" in ids

    def test_rate_limit_caps_queries(self):
        # Every package resolves, but only 3 lookups are allowed.
        fetch = StubFetcher({"pypi.org": PYPI_DEEPCHEM})
        pypi = PyPIAdapter(fetch=fetch, clock=FakeClock())
        session = DiscoverySession(
            local_entries=[], pypi=pypi, max_external_queries=3
        )
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        session.discover(q, pypi_packages=["a", "b", "c", "d", "e"])
        assert session.external_queries_used == 3
        assert session.budget_remaining == 0

    def test_github_query_counts_against_budget(self):
        pypi = PyPIAdapter(fetch=StubFetcher({"pypi.org": PYPI_DEEPCHEM}), clock=FakeClock())
        github = GitHubAdapter(fetch=StubFetcher({"api.github.com": GITHUB_SEARCH}), clock=FakeClock())
        session = DiscoverySession(
            local_entries=[], pypi=pypi, github=github, max_external_queries=2
        )
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        # 2 pypi lookups consume the whole budget; github search is skipped.
        merged = session.discover(q, pypi_packages=["a", "b"], github_query="chem")
        assert session.external_queries_used == 2
        ids = {e.id for e in merged}
        assert "github:grimme-lab/xtb" not in ids

    def test_strict_mode_raises_when_exhausted(self):
        pypi = PyPIAdapter(fetch=StubFetcher({"pypi.org": PYPI_DEEPCHEM}), clock=FakeClock())
        session = DiscoverySession(
            local_entries=[], pypi=pypi, max_external_queries=1, strict=True
        )
        q = DiscoveryQuery(capability_tags=["property_prediction"])
        with pytest.raises(RateLimitExceeded):
            session.discover(q, pypi_packages=["a", "b"])
