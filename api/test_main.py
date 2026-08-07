"""Tests for the API entry point itself (main.py).

Currently the health endpoint, which is the only thing about a deployed API that
can be checked without credentials.
"""
from fastapi.testclient import TestClient

from main import app, git_sha

client = TestClient(app)


class TestHealthReportsItsCommit:
    """"Did the API deploy?" had no answer before this.

    /api/health returned a bare {"status": "ok"} from every version, FastAPI's
    ``version`` is hardcoded, and CloudFront serves the SPA for /openapi.json -- so
    telling one release from another meant authenticating and probing behaviour.
    An unauthenticated curl can now answer it.
    """

    def test_health_still_reports_ok(self):
        """The field every uptime check already reads must not move."""
        body = client.get("/api/health").json()
        assert body["status"] == "ok"

    def test_health_carries_the_commit(self, monkeypatch):
        monkeypatch.setenv("TWAIN_GIT_SHA", "b4d8b0dcafebabe")
        body = client.get("/api/health").json()
        assert body["commit"] == "b4d8b0dcafebabe"

    def test_a_checkout_says_unknown_rather_than_guessing(self, monkeypatch):
        """Running from source is not a release, and "unknown" is distinguishable
        from a SHA -- unlike an empty string, which reads as a missing field."""
        monkeypatch.delenv("TWAIN_GIT_SHA", raising=False)
        assert git_sha() == "unknown"
        assert client.get("/api/health").json()["commit"] == "unknown"

    def test_the_endpoint_needs_no_credentials(self):
        """The whole point: verifiable from outside, with no token."""
        assert client.get("/api/health").status_code == 200
