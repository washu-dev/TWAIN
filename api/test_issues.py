from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import github_issues
from auth import get_current_user
from main import app

client = TestClient(app)

USER = {
    "id": "user-1", "subject": "s1", "email": "u@wustl.edu",
    "name": "Test User", "role": "user",
}


@pytest.fixture(autouse=True)
def _as_user():
    app.dependency_overrides[get_current_user] = lambda: USER
    yield
    app.dependency_overrides.clear()


class TestCreateIssueEndpoint:
    @patch(
        "github_issues.create_issue",
        return_value={"number": 7, "url": "https://github.com/washu-dev/TWAIN/issues/7",
                      "repo": "washu-dev/TWAIN"},
    )
    def test_creates_issue_and_forwards_user_email(self, mock_create):
        response = client.post(
            "/api/issues", json={"title": "App crashes on submit", "body": "steps..."}
        )
        assert response.status_code == 201
        assert response.json()["data"]["number"] == 7
        # The signed-in user's email/name come from the token, not the request body.
        kwargs = mock_create.call_args.kwargs
        assert kwargs["email"] == "u@wustl.edu"
        assert kwargs["name"] == "Test User"
        assert kwargs["title"] == "App crashes on submit"

    def test_rejects_empty_title(self):
        response = client.post("/api/issues", json={"title": "   ", "body": "x"})
        assert response.status_code == 422

    @patch("github_issues.create_issue", side_effect=github_issues.GitHubError(401, "bad token"))
    def test_maps_github_failure_to_502(self, _mock):
        response = client.post("/api/issues", json={"title": "hi"})
        assert response.status_code == 502
        assert "Could not create GitHub issue" in response.json()["detail"]


class TestCreateIssueHelper:
    def test_composes_body_with_email_and_posts_expected_payload(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ISSUE_TOKEN", "test-pat")
        github_issues._load_token.cache_clear()
        captured = {}

        def fake_fetch(url, payload, token):
            captured["url"] = url
            captured["payload"] = payload
            captured["token"] = token
            return {"number": 42, "html_url": "https://example/issues/42"}

        result = github_issues.create_issue(
            title="  Title  ", body="Body text", email="u@wustl.edu",
            name="Test User", fetch=fake_fetch,
        )

        assert result == {
            "number": 42,
            "url": "https://example/issues/42",
            "repo": github_issues.GITHUB_ISSUE_REPO,
        }
        assert captured["token"] == "test-pat"
        assert captured["url"].endswith(f"/repos/{github_issues.GITHUB_ISSUE_REPO}/issues")
        assert captured["payload"]["title"] == "Title"  # trimmed
        # Email is embedded in the body for attribution.
        assert "u@wustl.edu" in captured["payload"]["body"]
        assert "Test User" in captured["payload"]["body"]
        assert "Submitted via TWAIN" in captured["payload"]["body"]

    def test_rejects_empty_title(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ISSUE_TOKEN", "test-pat")
        github_issues._load_token.cache_clear()
        with pytest.raises(github_issues.GitHubError) as exc:
            github_issues.create_issue(
                title="   ", body="x", email="u@wustl.edu", fetch=lambda *a: {}
            )
        assert exc.value.status == 422
