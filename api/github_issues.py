"""GitHub issue creation for the TWAIN API.

Exposes a single :func:`create_issue` helper that opens an issue on the
configured repository using a *fine-grained personal access token* (PAT). Every
issue is authored by the one service identity that owns the PAT, so the
submitting user's email is recorded in the issue body for attribution.

Token resolution (first match wins):

1. ``GITHUB_ISSUE_TOKEN`` environment variable (local/dev, or an ECS secret).
2. AWS Secrets Manager entry at ``TWAIN_GITHUB_SECRET_ID``
   (default ``TWAIN/github/ISSUE_TOKEN``), read via :func:`database.read_secret`.

The PAT needs only *Issues: Read and write* on the target repository.

The network call is a thin ``urllib`` POST (matching the stdlib approach used by
the method-discovery GitHub adapter) and is injectable via the ``fetch`` argument
so tests never touch the network.
"""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from functools import lru_cache

from database import read_secret

GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_ISSUE_REPO = os.getenv("GITHUB_ISSUE_REPO", "washu-dev/TWAIN")
GITHUB_SECRET_ID = os.getenv("TWAIN_GITHUB_SECRET_ID", "TWAIN/github/ISSUE_TOKEN")
ISSUE_LABELS = [
    label.strip()
    for label in os.getenv("GITHUB_ISSUE_LABELS", "user-submitted").split(",")
    if label.strip()
]
API_TIMEOUT = float(os.getenv("GITHUB_API_TIMEOUT", "10"))
API_VERSION = "2022-11-28"

# fetch(url, payload, token) -> the created-issue dict returned by the GitHub API.
Fetcher = Callable[[str, dict, str], dict]


class GitHubError(RuntimeError):
    """Raised when GitHub rejects the request or is unreachable."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@lru_cache(maxsize=1)
def _load_token() -> str:
    """Resolve the fine-grained PAT once per process (env first, then Secrets Manager)."""
    token = os.getenv("GITHUB_ISSUE_TOKEN")
    if not token:
        token = read_secret(GITHUB_SECRET_ID)
    if not token or not token.strip():
        raise GitHubError(500, "GitHub issue token is not configured.")
    return token.strip()


def _compose_body(description: str, *, email: str, name: str | None) -> str:
    """Append a submitter footer so issues stay attributable to a real user."""
    submitter = email or "unknown"
    if name and name.strip() and name.strip().lower() != email.lower():
        submitter = f"{name.strip()} <{email}>"
    return f"{description.strip()}\n\n---\n_Submitted via TWAIN by {submitter}_"


def _default_fetch(url: str, payload: dict, token: str) -> dict:
    """POST ``payload`` to ``url`` as the PAT and return the parsed JSON response."""
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 (fully-qualified https GitHub API URL)
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Content-Type": "application/json",
            "User-Agent": "TWAIN-api/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise GitHubError(exc.code, f"GitHub API returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(502, f"Could not reach GitHub: {exc.reason}") from exc


def create_issue(
    *,
    title: str,
    body: str,
    email: str,
    name: str | None = None,
    fetch: Fetcher | None = None,
) -> dict:
    """Create an issue on ``GITHUB_ISSUE_REPO`` and return ``{number, url, repo}``.

    ``email`` / ``name`` identify the submitting user and are appended to the
    issue body; they are *not* used to authenticate (the PAT is the sole
    identity). Raises :class:`GitHubError` on an empty title or an API failure.
    """
    title = title.strip()
    if not title:
        raise GitHubError(422, "Issue title must not be empty.")

    fetch = fetch or _default_fetch
    payload: dict = {"title": title, "body": _compose_body(body, email=email, name=name)}
    if ISSUE_LABELS:
        payload["labels"] = ISSUE_LABELS

    created = fetch(
        f"{GITHUB_API_URL}/repos/{GITHUB_ISSUE_REPO}/issues", payload, _load_token()
    )
    return {
        "number": created.get("number"),
        "url": created.get("html_url"),
        "repo": GITHUB_ISSUE_REPO,
    }
