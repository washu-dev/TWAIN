"""GitHub issue filing for run reports submitted from the app.

A researcher watching a run can report a problem from the run window. The point
of doing it *in* TWAIN rather than on GitHub is that the run's own data rides
along automatically -- which state it was in, the toolset planning chose, the
errors it hit, the transcript tail -- so a maintainer never has to ask "what were
you running?".

This module owns the GitHub side of a *run report*: the category -> label
mapping, the issue body, and the REST call. Assembling the run snapshot and
recording the submission is ``run_issues``.

Not to be confused with ``github_issues``, which files a plain title+body issue
for the generic ``POST /api/issues`` (used for one-tap engine-provisioning
requests). That module owns credential resolution for the whole API -- env var
first, then AWS Secrets Manager -- and this one defers to it, so a deployment
configures one GitHub PAT and both paths pick it up.

The API is a standalone deployable (its own requirements/Dockerfile) and cannot
import the pipeline's ``method_discovery.library_requests``, so the client here
is deliberately independent of it. The two do share one label on purpose: a user
asking for an uninstalled library files under ``LibraryAddition``, the same tag
discovery uses when it reaches for a library that isn't installed, so both kinds
of request triage as one list.

Everything degrades: with no token configured the submission is still recorded
locally (status ``queued``) and the user is told so. ``transport`` is injectable,
so the whole path is testable without network access.
"""
import json
import logging
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import github_issues

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 10

# Every issue filed from the app carries this, so "reported from a run" is one
# GitHub filter regardless of category.
RUN_REPORT_LABEL = "RunReport"

# The categories the run window offers -> the GitHub label each triages under.
# 'library' reuses the pipeline's own tag on purpose (see the module docstring).
CATEGORY_LABELS = {
    "bug": "BugReport",
    "library": "LibraryAddition",
    "result": "ResultDiscrepancy",
    "other": RUN_REPORT_LABEL,
}
CATEGORIES = tuple(CATEGORY_LABELS)

LABEL_COLORS = {
    "BugReport": "d73a4a",
    "LibraryAddition": "1d76db",
    "ResultDiscrepancy": "fbca04",
    RUN_REPORT_LABEL: "5319e7",
}

# Caps on user-supplied text, so one report can't post an unbounded body.
MAX_TITLE = 160
MAX_DESCRIPTION = 8000

_MARKER = "twain-run-issue"


def resolve_repo() -> str | None:
    """``owner/repo`` the issues go to, or None when unconfigured.

    Deferred to ``github_issues`` so a run report and a generic issue land on the
    same repo. No git remote exists inside the API container, so unlike the
    pipeline side this is configuration-only (``GITHUB_ISSUE_REPO``).
    """
    return (github_issues.GITHUB_ISSUE_REPO or "").strip() or None


def resolve_token() -> str | None:
    """The PAT, or None when the deployment has none configured.

    ``github_issues`` is the single credential authority for the API: env var
    first, then AWS Secrets Manager. Deferring to it means one PAT serves both
    the generic issue endpoint and run reports.
    """
    return github_issues.token_configured()


def issues_enabled() -> bool:
    """Whether a submission can actually reach GitHub right now.

    ``TWAIN_RUN_ISSUES=0`` forces off (useful in staging, so test reports don't
    land on the tracker); otherwise it's on exactly when a repo and token resolve.
    """
    if (os.getenv("TWAIN_RUN_ISSUES") or "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool(resolve_repo() and resolve_token())


def clean_title(title: str) -> str:
    """One trimmed line, length-capped -- a GitHub title can't be multi-line."""
    collapsed = re.sub(r"\s+", " ", (title or "").strip())
    return collapsed[:MAX_TITLE]


def _default_transport(method: str, url: str, token: str, payload: dict | None):
    """One authenticated GitHub REST call -> ``(status, decoded_json)``."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    # The URL is always built from GITHUB_API + a fixed path, never user input.
    request = Request(  # noqa: S310 (https://api.github.com only)
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
            "User-Agent": "TWAIN-api/0.1",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body.strip() else {})
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw.strip() else {}
        except ValueError:
            return exc.code, {"message": raw}


class GitHubIssueClient:
    """Minimal GitHub Issues client: create an issue with labels."""

    def __init__(self, repo: str | None = None, token: str | None = None, *, transport=None):
        self.repo = repo if repo is not None else resolve_repo()
        self.token = token if token is not None else resolve_token()
        self._transport = transport or _default_transport

    def _call(self, method: str, path: str, payload: dict | None = None):
        return self._transport(method, f"{GITHUB_API}{path}", self.token, payload)

    def ensure_labels(self, labels: list[str]) -> None:
        """Create any missing labels, so the tags read as real triage categories.

        The issues API would create them implicitly but without a colour. A 422
        (already exists) and any transport failure are both fine to ignore --
        the issue itself still gets the label.
        """
        for name in labels:
            try:
                self._call("POST", f"/repos/{self.repo}/labels", {
                    "name": name,
                    "color": LABEL_COLORS.get(name, "ededed"),
                    "description": "Filed from a TWAIN run window",
                })
            except (URLError, OSError, ValueError) as exc:
                logger.debug("label ensure for %s failed: %s", name, exc)

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        """File the issue. Returns ``{status, issue_number, issue_url, error}``.

        Never raises: a report that can't reach GitHub is still worth recording
        locally, and the caller tells the user which happened.
        """
        try:
            self.ensure_labels(labels)
            status, payload = self._call("POST", f"/repos/{self.repo}/issues", {
                "title": title, "body": body, "labels": labels,
            })
        except (URLError, OSError, ValueError, TypeError) as exc:
            logger.warning("filing run issue failed: %s", exc)
            return {"status": "failed", "issue_number": None, "issue_url": None,
                    "error": f"Could not reach GitHub: {exc}"}
        if status in (200, 201) and isinstance(payload, dict) and payload.get("number"):
            return {"status": "created", "issue_number": int(payload["number"]),
                    "issue_url": payload.get("html_url"), "error": None}
        message = payload.get("message") if isinstance(payload, dict) else str(payload)
        logger.warning("filing run issue rejected (HTTP %s): %s", status, message)
        return {"status": "failed", "issue_number": None, "issue_url": None,
                "error": f"GitHub rejected the issue (HTTP {status}): {message}"}


def labels_for(category: str) -> list[str]:
    """The labels one submission gets: its category, plus the app-wide tag."""
    label = CATEGORY_LABELS.get(category, RUN_REPORT_LABEL)
    return [label] if label == RUN_REPORT_LABEL else [label, RUN_REPORT_LABEL]


def _fence(value, language: str = "json") -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    return f"```{language}\n{text}\n```"


def _details(summary: str, content: str) -> str:
    """A collapsed block, so a long attachment doesn't bury the user's report."""
    return f"<details>\n<summary>{summary}</summary>\n\n{content}\n\n</details>"


def render_issue_body(context: dict, *, category: str, description: str,
                      reporter: dict | None = None) -> str:
    """The issue body: the user's report first, then the run's own data.

    Order matters -- a maintainer should read what the researcher said before
    wading into the snapshot, so every attachment below it is collapsed.
    """
    reporter = reporter or {}
    who = reporter.get("email") or reporter.get("name") or reporter.get("id") or "unknown"
    run_id = context.get("run_id") or "unknown"
    lines = [
        f"_Reported from the TWAIN run window by {who}._",
        "",
        "## What the researcher reported",
        "",
        (description or "").strip() or "_(no description given)_",
        "",
        "## Run",
        "",
        "| | |",
        "|---|---|",
        f"| Run id | `{run_id}` |",
        f"| Title | {context.get('title') or '—'} |",
        f"| Category | `{category}` |",
        f"| Status | `{context.get('status') or '—'}` |",
        f"| State reached | `{context.get('current_state') or '—'}` |",
        f"| Started | {context.get('created_at') or '—'} |",
        f"| Last activity | {context.get('updated_at') or '—'} |",
    ]

    method = context.get("selected_method")
    if method:
        toolset = ", ".join(f"`{lib}`" for lib in (method.get("libraries") or [])) or "—"
        calculator = method.get("calculator")
        lines += [
            f"| Toolset | {toolset} |",
            f"| Calculator | {f'`{calculator}`' if calculator else '—'} |",
        ]
    if context.get("requested_property"):
        lines.append(f"| Property | `{context['requested_property']}` |")

    if context.get("safety_notes"):
        lines += ["", "### Plan notes shown to the researcher", ""]
        lines += [f"- {note}" for note in context["safety_notes"]]

    if context.get("library_requests"):
        lines += ["", "### Libraries this run wanted but couldn't use", ""]
        lines += [
            f"- `{req.get('library')}` ({req.get('status')})"
            + (f" — {req.get('issue_url')}" if req.get("issue_url") else "")
            for req in context["library_requests"]
        ]

    if context.get("errors"):
        lines += ["", "### Errors", "", _fence(context["errors"])]

    if context.get("execution_result"):
        lines += ["", _details("Execution result", _fence(context["execution_result"]))]

    if context.get("recent_messages"):
        transcript = "\n\n".join(
            f"**{m.get('role')}** ({m.get('kind')} @ {m.get('state') or '—'}): {m.get('content')}"
            for m in context["recent_messages"]
        )
        lines += ["", _details("Transcript (most recent turns)", transcript)]

    if context.get("artifacts"):
        listing = "\n".join(
            f"- `{a.get('name')}` ({a.get('kind')}, {a.get('size')} bytes)"
            for a in context["artifacts"]
        )
        lines += ["", _details(
            f"Artifacts produced ({len(context['artifacts'])})",
            listing + "\n\nFull contents are held against the run id above; fetch them with "
            f"`GET /api/conversations/{run_id}/artifacts/<name>`.",
        )]

    if context.get("truncated"):
        lines += ["", "> Some attachments were truncated to keep this issue readable; "
                  "the full record is held against the run id."]

    lines += ["", f"<!-- {_MARKER}:{run_id} -->"]
    return "\n".join(lines)
