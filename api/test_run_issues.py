"""Tests for reporting a GitHub issue from a run window, run data attached.

Covers the three things that make the feature trustworthy:

  * the run's own data really does ride along (toolset, plan notes, errors,
    transcript tail, artifact names) and is bounded, not dumped whole;
  * a report is never lost — a missing token or a GitHub failure is still
    recorded against the run, with a status saying which;
  * a run belonging to someone else is a 404, and the caller cannot make TWAIN
    post an unbounded stream of issues.

Every GitHub call goes through an injected transport, so nothing here touches
the network.
"""
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import github_issues
import run_issue_github
import run_issues
from auth import get_current_user
from main import app

client = TestClient(app)

USER = {"id": "user-1", "subject": "s1", "email": "u@wustl.edu", "name": "U", "role": "user"}
CONVERSATION = {
    "id": "conv-1", "user_id": "user-1", "title": "band gap of silicon",
    "status": "error", "current_state": "EXECUTE",
    "created_at": "2026-07-30T00:00:00Z", "updated_at": "2026-07-30T00:05:00Z",
}
RUN_CONTEXT = {
    "run_id": "conv-1",
    "title": "band gap of silicon",
    "status": "error",
    "current_state": "EXECUTE",
    "created_at": "2026-07-30T00:00:00Z",
    "updated_at": "2026-07-30T00:05:00Z",
    "selected_method": {"tool_name": "ASE", "libraries": ["ASE"], "calculator": "GPAW"},
    "requested_property": "band_gap",
    "target_system": {"formula": "Si"},
    "safety_notes": ["GPAW has no osx-arm64 build; it will run in Docker."],
    "library_requests": [
        {"library": "VASP", "status": "issue_created",
         "issue_url": "https://github.com/o/r/issues/7"}
    ],
    "execution_result": {"status": "failed", "succeeded": False},
    "errors": [{"event_type": "run.error", "payload": {"message": "docker not running"},
                "created_at": "2026-07-30T00:05:00Z"}],
    "recent_messages": [
        {"role": "user", "content": "band gap of silicon", "kind": "chat",
         "state": "INTAKE", "created_at": "2026-07-30T00:00:00Z"}
    ],
    "artifacts": [{"name": "run_bundle/main.py", "kind": "python", "size": 2048}],
    "truncated": False,
}
STORED = {
    "id": 1, "conversation_id": "conv-1", "category": "bug", "title": "Run died at EXECUTE",
    "description": "Docker wasn't running", "status": "created", "issue_number": 42,
    "issue_url": "https://github.com/o/r/issues/42", "error": None,
    "created_at": "2026-07-30T00:06:00Z",
}


@pytest.fixture(autouse=True)
def _as_user():
    app.dependency_overrides[get_current_user] = lambda: USER
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_github_env(monkeypatch):
    """Default every test to 'unconfigured'; tests that need GitHub opt in.

    The PAT is resolved by ``github_issues`` (env, then Secrets Manager) and
    memoised, so the cache is cleared and the Secrets Manager read stubbed out --
    otherwise an unconfigured test could reach for real AWS.
    """
    for var in ("GITHUB_ISSUE_TOKEN", "TWAIN_RUN_ISSUES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(github_issues, "read_secret", lambda *_a, **_k: None)
    github_issues._load_token.cache_clear()
    yield
    github_issues._load_token.cache_clear()


class FakeGitHub:
    """Injected GitHub transport; records calls, returns a canned issue."""

    def __init__(self, *, create_status=201, message="no"):
        self.create_status = create_status
        self.message = message
        self.calls = []

    def __call__(self, method, url, token, payload):
        self.calls.append((method, url, payload))
        if url.endswith("/labels"):
            return 201, {}
        if self.create_status not in (200, 201):
            return self.create_status, {"message": self.message}
        return 201, {"number": 42, "html_url": "https://github.com/o/r/issues/42"}

    @property
    def issue_payload(self):
        return next(p for m, u, p in self.calls if u.endswith("/issues"))


def _configured(monkeypatch):
    """Make the deployment look credentialed: one PAT, shared with /api/issues."""
    monkeypatch.setattr(github_issues, "GITHUB_ISSUE_REPO", "washu-dev/TWAIN")
    monkeypatch.setenv("GITHUB_ISSUE_TOKEN", "tok")
    github_issues._load_token.cache_clear()


# ── Labels + body rendering (pure, no DB) ─────────────────────────────────────
class TestLabels:
    def test_category_maps_to_its_label_plus_the_app_wide_tag(self):
        assert run_issue_github.labels_for("bug") == ["BugReport", "RunReport"]
        assert run_issue_github.labels_for("result") == ["ResultDiscrepancy", "RunReport"]

    def test_library_category_reuses_the_pipelines_own_tag(self):
        """A user asking for an uninstalled library triages with discovery's asks."""
        assert run_issue_github.labels_for("library") == ["LibraryAddition", "RunReport"]

    def test_other_is_just_the_app_wide_tag_not_duplicated(self):
        assert run_issue_github.labels_for("other") == ["RunReport"]

    def test_unknown_category_falls_back(self):
        assert run_issue_github.labels_for("nonsense") == ["RunReport"]


class TestIssueBody:
    def _body(self, **kw):
        return run_issue_github.render_issue_body(
            RUN_CONTEXT, category="bug", description="Docker wasn't running", reporter=USER, **kw
        )

    def test_users_report_comes_before_the_run_data(self):
        body = self._body()
        assert body.index("Docker wasn't running") < body.index("## Run")

    def test_run_data_is_attached(self):
        body = self._body()
        assert "`conv-1`" in body                      # the run id
        assert "`EXECUTE`" in body                     # where it got to
        assert "`ASE`" in body and "`GPAW`" in body     # the toolset planning chose
        assert "`band_gap`" in body
        assert "no osx-arm64 build" in body            # the notes the researcher saw
        assert "docker not running" in body            # the error
        assert "run_bundle/main.py" in body            # artifact names
        assert "band gap of silicon" in body           # transcript tail

    def test_library_requests_carry_their_issue_links(self):
        body = self._body()
        assert "`VASP`" in body
        assert "https://github.com/o/r/issues/7" in body

    def test_long_attachments_are_collapsed_not_inline(self):
        body = self._body()
        assert "<details>" in body
        # The user's own words are never hidden behind a disclosure.
        assert body.index("Docker wasn't running") < body.index("<details>")

    def test_marker_ties_the_issue_back_to_the_run(self):
        assert "<!-- twain-run-issue:conv-1 -->" in self._body()

    def test_reporter_is_attributed(self):
        assert "u@wustl.edu" in self._body()

    def test_empty_description_is_marked_not_blank(self):
        body = run_issue_github.render_issue_body(RUN_CONTEXT, category="other", description="  ")
        assert "_(no description given)_" in body

    def test_truncation_is_disclosed(self):
        body = run_issue_github.render_issue_body(
            {**RUN_CONTEXT, "truncated": True}, category="bug", description="x")
        assert "truncated" in body

    def test_sparse_context_still_renders(self):
        """A run that died before planning has no method/plan/artifacts."""
        body = run_issue_github.render_issue_body(
            {"run_id": "conv-9"}, category="bug", description="died early")
        assert "`conv-9`" in body and "died early" in body


class TestTitleCleaning:
    def test_collapses_newlines_and_caps_length(self):
        assert run_issue_github.clean_title("  a\nb  c ") == "a b c"
        assert len(run_issue_github.clean_title("x" * 500)) == run_issue_github.MAX_TITLE

    def test_blank_title_is_blank(self):
        assert run_issue_github.clean_title("  \n ") == ""


# ── Submitting ────────────────────────────────────────────────────────────────
@patch("run_issues.record_issue", return_value=STORED)
@patch("run_issues.count_issues", return_value=0)
@patch("run_issues.collect_run_context", return_value=RUN_CONTEXT)
@patch("conversations.get_conversation", return_value=CONVERSATION)
class TestSubmit:
    def _post(self, **overrides):
        payload = {"category": "bug", "title": "Run died at EXECUTE",
                   "description": "Docker wasn't running", **overrides}
        return client.post("/api/conversations/conv-1/issues", json=payload)

    def test_files_the_issue_with_run_data_and_returns_the_link(
        self, _conv, _ctx, _count, mock_record, monkeypatch
    ):
        _configured(monkeypatch)
        gh = FakeGitHub()
        with patch.object(run_issue_github, "_default_transport", gh):
            response = self._post()

        assert response.status_code == 200
        assert response.json()["data"]["issue_url"] == "https://github.com/o/r/issues/42"
        payload = gh.issue_payload
        assert payload["labels"] == ["BugReport", "RunReport"]
        assert payload["title"] == "Run died at EXECUTE"
        assert "`conv-1`" in payload["body"]          # the run's data went with it
        assert "docker not running" in payload["body"]
        # The snapshot is stored verbatim alongside the submission.
        assert mock_record.call_args.kwargs["run_context"] == RUN_CONTEXT
        assert mock_record.call_args.kwargs["result"]["status"] == "created"

    def test_without_credentials_the_report_is_recorded_not_lost(
        self, _conv, _ctx, _count, mock_record
    ):
        response = self._post()
        assert response.status_code == 200
        result = mock_record.call_args.kwargs["result"]
        assert result["status"] == "queued"
        assert "No GitHub credentials" in result["error"]

    def test_github_failure_is_recorded_with_the_reason(
        self, _conv, _ctx, _count, mock_record, monkeypatch
    ):
        _configured(monkeypatch)
        with patch.object(run_issue_github, "_default_transport",
                          FakeGitHub(create_status=403, message="Resource not accessible")):
            assert self._post().status_code == 200
        result = mock_record.call_args.kwargs["result"]
        assert result["status"] == "failed"
        assert "403" in result["error"] and "Resource not accessible" in result["error"]

    def test_network_error_is_recorded_not_raised(
        self, _conv, _ctx, _count, mock_record, monkeypatch
    ):
        _configured(monkeypatch)

        def boom(*_a, **_kw):
            raise OSError("connection reset")

        with patch.object(run_issue_github, "_default_transport", boom):
            assert self._post().status_code == 200
        assert mock_record.call_args.kwargs["result"]["status"] == "failed"

    def test_disabled_by_env_even_with_credentials(
        self, _conv, _ctx, _count, mock_record, monkeypatch
    ):
        _configured(monkeypatch)
        monkeypatch.setenv("TWAIN_RUN_ISSUES", "0")
        gh = FakeGitHub()
        with patch.object(run_issue_github, "_default_transport", gh):
            assert self._post().status_code == 200
        assert gh.calls == []
        assert mock_record.call_args.kwargs["result"]["status"] == "queued"

    def test_title_is_cleaned_before_filing(self, _conv, _ctx, _count, mock_record):
        self._post(title="  multi\nline  title  ")
        assert mock_record.call_args.kwargs["title"] == "multi line title"

    def test_empty_title_rejected(self, _conv, _ctx, _count, _record):
        assert self._post(title="   ").status_code == 422

    def test_empty_description_rejected(self, _conv, _ctx, _count, _record):
        assert self._post(description="  \n ").status_code == 422

    def test_unknown_category_rejected_by_the_schema(self, _conv, _ctx, _count, _record):
        assert self._post(category="sabotage").status_code == 422

    def test_category_defaults_to_other(self, _conv, _ctx, _count, mock_record):
        client.post("/api/conversations/conv-1/issues",
                    json={"title": "t", "description": "d"})
        assert mock_record.call_args.kwargs["category"] == "other"


class TestSubmitGuards:
    @patch("conversations.get_conversation", return_value=None)
    def test_someone_elses_run_is_a_404(self, _conv):
        response = client.post("/api/conversations/conv-9/issues",
                               json={"title": "t", "description": "d"})
        assert response.status_code == 404

    @patch("run_issues.count_issues", return_value=run_issues.MAX_ISSUES_PER_RUN)
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_too_many_issues_for_one_run_is_rejected(self, _conv, _count):
        response = client.post("/api/conversations/conv-1/issues",
                               json={"title": "t", "description": "d"})
        assert response.status_code == 429
        assert "already has" in response.json()["detail"]


# ── Preview + listing ─────────────────────────────────────────────────────────
class TestIssueContext:
    @patch("run_issues.list_issues", return_value=[STORED])
    @patch("run_issues.collect_run_context", return_value=RUN_CONTEXT)
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_preview_shows_what_would_be_attached(self, _conv, _ctx, _list):
        response = client.get("/api/conversations/conv-1/issue-context")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["run_context"]["run_id"] == "conv-1"
        assert data["github_configured"] is False       # no creds in this test env
        assert "bug" in data["categories"]
        assert data["submitted"][0]["issue_url"].endswith("/42")

    @patch("run_issues.list_issues", return_value=[])
    @patch("run_issues.collect_run_context", return_value=RUN_CONTEXT)
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_preview_reports_configured_repo(self, _conv, _ctx, _list, monkeypatch):
        _configured(monkeypatch)
        data = client.get("/api/conversations/conv-1/issue-context").json()["data"]
        assert data["github_configured"] is True
        assert data["repo"] == "washu-dev/TWAIN"

    @patch("conversations.get_conversation", return_value=None)
    def test_preview_404_when_not_owner(self, _conv):
        assert client.get("/api/conversations/x/issue-context").status_code == 404

    @patch("run_issues.list_issues", return_value=[STORED])
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_list_reported_issues(self, _conv, _list):
        response = client.get("/api/conversations/conv-1/issues")
        assert response.status_code == 200
        assert response.json()["count"] == 1

    @patch("conversations.get_conversation", return_value=None)
    def test_list_404_when_not_owner(self, _conv):
        assert client.get("/api/conversations/x/issues").status_code == 404


# ── Truncation helper ─────────────────────────────────────────────────────────
class TestTruncate:
    def test_short_text_untouched(self):
        assert run_issues._truncate("abc", 10) == ("abc", False)

    def test_long_text_is_cut_and_flagged(self):
        text, cut = run_issues._truncate("x" * 50, 10)
        assert cut is True
        assert text.startswith("x" * 10)
        assert "40 more characters" in text

    def test_none_is_safe(self):
        assert run_issues._truncate(None, 10) == ("", False)


class TestContextIsBounded:
    """The caps exist so an issue stays readable; assert they're actually set."""

    def test_limits_are_conservative(self):
        assert run_issues.MAX_MESSAGES <= 25
        assert run_issues.MAX_MESSAGE_CHARS <= 4000
        assert run_issues.MAX_ISSUES_PER_RUN <= 25

    def test_stored_context_is_json_serialisable(self):
        """record_issue writes run_context as JSONB, so it must survive a dump."""
        assert json.loads(json.dumps(RUN_CONTEXT, default=str))["run_id"] == "conv-1"
