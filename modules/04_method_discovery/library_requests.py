"""Missing-library requests + ``LibraryAddition`` GitHub issues (module 04_method_discovery).

TWAIN only ever commits to a library it can actually import where the run happens
(see ``StateMachine._installed_candidates`` / ``_library_importable``). That
guarantee stays exactly as it was: a plan never names a tool the run can't load,
and the researcher is always told which installed tool was used instead.

What this module adds is that the *wish* is no longer dropped on the floor. When
discovery -- or the researcher -- asks for a library that isn't in the preset
install set, the ask is:

  1. recorded in a durable, deduplicated ledger (``logs/library_requests.json``),
  2. filed as a GitHub issue tagged ``LibraryAddition`` so someone can add it to
     ``pixi.toml`` + the registries, and
  3. surfaced back to the researcher as a plain note on the ExecutionPlan --
     "your request was recorded; this run stuck to the preset libraries".

Everything degrades gracefully. With no GitHub credentials the request is still
ledgered (status ``queued``) and still surfaced; only the issue is skipped. A
network failure is caught, never raised: an install wish must not be able to
break planning. All I/O is injectable (``transport``, ``ledger_path``, ``clock``)
so the whole path is testable offline.

Dedup is by canonical library name and is two-layered: the local ledger avoids
re-filing on every run, and a GitHub search for the hidden marker
``<!-- twain-library-request:<name> -->`` avoids duplicate issues even when the
ledger is wiped or a second runner races us.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import twain_paths

logger = logging.getLogger(__name__)

# The unique tag every automated install request carries, so the whole set is one
# GitHub filter: `is:issue label:LibraryAddition`.
LIBRARY_ADDITION_LABEL = "LibraryAddition"
LABEL_COLOR = "1d76db"
LABEL_DESCRIPTION = "Automated TWAIN request to add a scientific library to the preset install set"

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 10

# Where a request came from. Kept as plain strings (not an enum) because they are
# written verbatim into the ledger/artifact JSON and read by the UI.
SOURCE_USER = "user"                      # the researcher named the library
SOURCE_LLM = "llm_discovery"              # the discovery LLM picked it
SOURCE_RANKING = "discovery_ranking"      # it out-ranked every installed candidate

# Request lifecycle, as recorded in the ledger and shown on the plan.
STATUS_CREATED = "issue_created"
STATUS_EXISTS = "issue_exists"
STATUS_QUEUED = "queued"        # no GitHub configured -- ledgered only
STATUS_FAILED = "issue_failed"  # GitHub configured but the call failed

_MARKER = "twain-library-request"


def canonical_name(name: str) -> str:
    """Dedup key for a library name: ``"Open Babel"`` and ``openbabel`` are one ask.

    >>> canonical_name("Open Babel")
    'open-babel'
    >>> canonical_name("  scikit_learn ")
    'scikit-learn'
    """
    slug = re.sub(r"[\s_]+", "-", (name or "").strip().lower())
    return re.sub(r"-+", "-", slug).strip("-")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LibraryRequest:
    """One recorded ask for a library that isn't in the preset install set."""

    library: str
    source: str = SOURCE_LLM
    reason: str = ""
    alternative: Optional[str] = None   # the installed library used instead
    objective: Optional[str] = None
    run_id: Optional[str] = None
    canonical: str = ""
    status: str = STATUS_QUEUED
    issue_number: Optional[int] = None
    issue_url: Optional[str] = None
    first_requested_at: str = field(default_factory=_now)
    last_requested_at: str = field(default_factory=_now)
    occurrences: int = 1

    def __post_init__(self):
        if not self.library or type(self.library) is not str:
            raise ValueError("LibraryRequest library must be a non-empty str")
        if not self.canonical:
            self.canonical = canonical_name(self.library)

    def as_dict(self) -> dict:
        return asdict(self)

    def note(self) -> str:
        """The researcher-facing sentence for this request.

        Says three things, in this order: what was asked for, that this run did
        NOT use it (the preset-library guarantee holds), and that the ask has been
        recorded -- with the issue link when there is one.
        """
        who = {
            SOURCE_USER: "You asked for",
            SOURCE_LLM: "Discovery selected",
            SOURCE_RANKING: "Discovery's top-ranked tool was",
        }.get(self.source, "Discovery selected")
        note = (f"{who} '{self.library}', which is not installed in the run "
                f"environment. TWAIN must stick to its preset (installed) "
                f"libraries, so this run used ")
        note += f"'{self.alternative}' instead. " if self.alternative else "an installed library instead. "
        if self.reason:
            note += f"({self.reason}) "
        if self.issue_url:
            verb = "Filed" if self.status == STATUS_CREATED else "Tracked by"
            note += (f"{verb} a '{LIBRARY_ADDITION_LABEL}' request to install it: "
                     f"{self.issue_url}")
        elif self.status == STATUS_QUEUED:
            note += (f"The install request was recorded locally (tag "
                     f"'{LIBRARY_ADDITION_LABEL}'); no GitHub credentials are "
                     f"configured, so no issue was filed.")
        else:
            note += (f"The install request was recorded locally (tag "
                     f"'{LIBRARY_ADDITION_LABEL}'), but filing the GitHub issue "
                     f"failed -- see the logs.")
        return note


def _env_flag(name: str) -> Optional[bool]:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None  # unset or "auto"


def default_repo() -> Optional[str]:
    """``owner/repo`` for the issue tracker: env first, else the git ``origin``.

    Deriving from the remote means a fork files against the fork, with no config.
    """
    explicit = (os.environ.get("TWAIN_GITHUB_REPO") or "").strip()
    if explicit:
        return explicit
    try:
        proc = subprocess.run(["git", "remote", "get-url", "origin"],
                              cwd=str(twain_paths.REPO_ROOT),
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    m = re.search(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?\s*$", proc.stdout or "")
    return m.group(1) if m else None


def default_token() -> Optional[str]:
    for var in ("TWAIN_GITHUB_TOKEN", "GITHUB_TOKEN"):
        tok = (os.environ.get(var) or "").strip()
        if tok:
            return tok
    return None


def _default_transport(method: str, url: str, token: str,
                       payload: Optional[dict]) -> Tuple[int, dict]:
    """One authenticated GitHub REST call -> ``(status, decoded_json)``.

    The single network seam in this module: tests inject a stand-in instead.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(url, data=data, method=method, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
        "User-Agent": "TWAIN-library-requests/0.1",
        **({"Content-Type": "application/json"} if data else {}),
    })
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:  # noqa: S310 (api.github.com)
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body.strip() else {})
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body) if body.strip() else {}
        except ValueError:
            return exc.code, {"message": body}


class GitHubIssueFiler:
    """Files (and de-duplicates) ``LibraryAddition`` issues on the TWAIN repo.

    Disabled -- and therefore a no-op -- unless a repo and a token resolve, so
    offline/CI/test runs never touch the network. ``TWAIN_LIBRARY_REQUEST_ISSUES``
    forces it on (``1``) or off (``0``); unset/``auto`` means "on iff credentials".
    """

    def __init__(self, repo: Optional[str] = None, token: Optional[str] = None,
                 *, transport: Optional[Callable[..., Tuple[int, dict]]] = None,
                 enabled: Optional[bool] = None):
        self.repo = repo if repo is not None else default_repo()
        self.token = token if token is not None else default_token()
        self._transport = transport or _default_transport
        forced = _env_flag("TWAIN_LIBRARY_REQUEST_ISSUES") if enabled is None else enabled
        has_creds = bool(self.repo and self.token)
        self.enabled = has_creds if forced is None else bool(forced) and has_creds
        self._label_ensured = False

    # ── internals ─────────────────────────────────────────────────────────────
    def _call(self, method: str, path: str, payload: Optional[dict] = None):
        return self._transport(method, f"{GITHUB_API}{path}", self.token, payload)

    def _ensure_label(self) -> None:
        """Create the ``LibraryAddition`` label once per filer (422 = already there).

        The issues API does create unknown labels implicitly, but doing it
        explicitly gets the tag a colour and a description so it reads as a real
        triage category in the GitHub UI rather than a bare string.
        """
        if self._label_ensured:
            return
        self._label_ensured = True
        try:
            self._call("POST", f"/repos/{self.repo}/labels", {
                "name": LIBRARY_ADDITION_LABEL,
                "color": LABEL_COLOR,
                "description": LABEL_DESCRIPTION,
            })
        except (URLError, OSError, ValueError) as exc:  # noqa: BLE001
            logger.debug("LibraryAddition label ensure failed: %s", exc)

    def find_existing(self, canonical: str) -> Optional[Tuple[int, str]]:
        """An already-open request issue for ``canonical``, as ``(number, url)``.

        Matches on the hidden body marker, so it finds the issue no matter how the
        title was later edited. Returns None when nothing matches or the search
        can't be performed (the caller then relies on the local ledger alone).
        """
        query = quote(f'repo:{self.repo} is:issue label:{LIBRARY_ADDITION_LABEL} '
                      f'"{_MARKER}:{canonical}"', safe="")
        status, body = self._call("GET", f"/search/issues?q={query}&per_page=5")
        if status != 200 or not isinstance(body, dict):
            return None
        for item in body.get("items") or []:
            number, url = item.get("number"), item.get("html_url")
            if number and url:
                return int(number), url
        return None

    # ── public ────────────────────────────────────────────────────────────────
    def file(self, request: LibraryRequest) -> LibraryRequest:
        """File (or find) the issue for ``request`` and stamp the result onto it.

        Never raises: a failure leaves the request at ``issue_failed`` so the
        ledger + the researcher-facing note still record the ask.
        """
        if not self.enabled:
            request.status = STATUS_QUEUED
            return request
        try:
            existing = self.find_existing(request.canonical)
            if existing:
                request.issue_number, request.issue_url = existing
                request.status = STATUS_EXISTS
                return request
            self._ensure_label()
            status, body = self._call("POST", f"/repos/{self.repo}/issues", {
                "title": issue_title(request),
                "body": issue_body(request),
                "labels": [LIBRARY_ADDITION_LABEL],
            })
            if status in (200, 201) and isinstance(body, dict) and body.get("number"):
                request.issue_number = int(body["number"])
                request.issue_url = body.get("html_url")
                request.status = STATUS_CREATED
                logger.info("Filed %s issue #%s for '%s'", LIBRARY_ADDITION_LABEL,
                            request.issue_number, request.library)
            else:
                request.status = STATUS_FAILED
                logger.warning("Filing %s issue for '%s' failed (HTTP %s): %s",
                               LIBRARY_ADDITION_LABEL, request.library, status,
                               (body or {}).get("message") if isinstance(body, dict) else body)
        except (URLError, OSError, ValueError, TypeError, KeyError) as exc:  # noqa: BLE001
            request.status = STATUS_FAILED
            logger.warning("Filing %s issue for '%s' errored: %s",
                           LIBRARY_ADDITION_LABEL, request.library, exc)
        return request


def issue_title(request: LibraryRequest) -> str:
    return f"[{LIBRARY_ADDITION_LABEL}] Install '{request.library}' in the TWAIN environment"


def issue_body(request: LibraryRequest) -> str:
    """The issue text: what was asked for, why it couldn't be used, how to add it."""
    asked_by = {
        SOURCE_USER: "A researcher explicitly requested this library.",
        SOURCE_LLM: "TWAIN's method-discovery LLM selected this library for a run.",
        SOURCE_RANKING: "This library out-ranked every installed candidate for a run.",
    }.get(request.source, "TWAIN's method discovery selected this library for a run.")
    lines = [
        "_Filed automatically by TWAIN's method discovery._",
        "",
        f"{asked_by} It is **not installed** in the run environment, so TWAIN "
        "fell back to an installed preset library and told the researcher that "
        "this install request had been recorded.",
        "",
        "| | |",
        "|---|---|",
        f"| Library requested | `{request.library}` |",
        f"| Requested by | `{request.source}` |",
        f"| Used instead | {f'`{request.alternative}`' if request.alternative else '_(an installed preset library)_'} |",
        f"| First requested | {request.first_requested_at} |",
    ]
    if request.run_id:
        lines.append(f"| Run id | `{request.run_id}` |")
    if request.objective:
        lines.append(f"| Objective | {request.objective} |")
    if request.reason:
        lines += ["", f"**Why it was wanted:** {request.reason}"]
    lines += [
        "",
        "### To satisfy this request",
        "",
        f"1. Add `{request.library}` to the preset scientific libraries in "
        "`pixi.toml` (and to `[feature.sim.dependencies]` if calculator runs need "
        "it), then `pixi install`.",
        "2. Add a matching entry to `configs/discovery_registry.json` (a driver / "
        "analysis library) or `configs/calculator_registry.json` (a compute "
        "engine, with its `platforms` list) so discovery can rank it.",
        "3. Map the tool to its import name in `TOOL_REGISTRY` "
        "(`modules/06_code_configuration_builder/dependency_inferencer.py`), so "
        "the install probe can see it.",
        "4. Rebuild the linux-64 runner image (`runner/Dockerfile`) so remote runs "
        "get it too.",
        "",
        "The registries are meant to stay equal to what is actually installed, so "
        "please land steps 1 and 2 together.",
        "",
        f"<!-- {_MARKER}:{request.canonical} -->",
    ]
    return "\n".join(lines)


class LibraryRequestTracker:
    """Records missing-library asks for one run: ledger + issue + plan notes.

    One tracker per run. :meth:`record` is idempotent per library within the run
    and deduplicated across runs by the ledger, so a library TWAIN keeps reaching
    for produces exactly one issue and one note -- with a bumped ``occurrences``
    count as evidence of how often it's actually wanted.
    """

    def __init__(self, *, run_id: Optional[str] = None,
                 ledger_path: Optional[Path] = None,
                 filer: Optional[GitHubIssueFiler] = None,
                 objective: Optional[str] = None):
        self.run_id = run_id
        self.objective = objective
        self.ledger_path = Path(ledger_path) if ledger_path else twain_paths.LIBRARY_REQUESTS_PATH
        self._filer = filer  # None -> built on first use (reads env then)
        self._records: Dict[str, LibraryRequest] = {}

    @property
    def filer(self) -> GitHubIssueFiler:
        if self._filer is None:
            self._filer = GitHubIssueFiler()
        return self._filer

    @property
    def records(self) -> List[LibraryRequest]:
        """This run's requests, in the order they were first recorded."""
        return list(self._records.values())

    def as_dicts(self) -> List[dict]:
        return [r.as_dict() for r in self.records]

    def notes(self) -> List[str]:
        return [r.note() for r in self.records]

    def record(self, library: str, *, source: str = SOURCE_LLM, reason: str = "",
               alternative: Optional[str] = None) -> Optional[LibraryRequest]:
        """Record an ask for the uninstalled ``library``; file its issue if new.

        Returns the request (existing one on a repeat within this run), or None
        for an empty name. Never raises -- planning must not fail because an
        install wish couldn't be persisted.
        """
        if not library or not str(library).strip():
            return None
        key = canonical_name(library)
        if key in self._records:
            # Already asked for in this run -- e.g. DISCOVER hit it on the ranking
            # and PLAN hit it again, or the researcher and the LLM both named it.
            # One request, one note, one issue. ``occurrences`` counts *runs* that
            # wanted the library (the signal for prioritising an install), so it is
            # deliberately not bumped here.
            existing = self._records[key]
            existing.last_requested_at = _now()
            if alternative and not existing.alternative:
                existing.alternative = alternative
            return existing

        request = LibraryRequest(
            library=str(library).strip(), source=source, reason=reason,
            alternative=alternative, objective=self.objective, run_id=self.run_id,
        )
        try:
            prior = self._merge_ledger(request)
            if prior is None:                 # genuinely new -> try to file
                self.filer.file(request)
            self._persist(request)
        except (OSError, ValueError, TypeError) as exc:  # noqa: BLE001
            logger.warning("Recording library request '%s' failed: %s", library, exc)
        self._records[key] = request
        return request

    # ── ledger ────────────────────────────────────────────────────────────────
    def _load_ledger(self) -> dict:
        try:
            raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _merge_ledger(self, request: LibraryRequest) -> Optional[dict]:
        """Fold any prior ledger entry for this library into ``request``.

        Returns the prior entry (so the caller knows not to re-file an issue), or
        None when this is the first time the library has ever been asked for.
        """
        prior = self._load_ledger().get(request.canonical)
        if not isinstance(prior, dict):
            return None
        request.first_requested_at = prior.get("first_requested_at") or request.first_requested_at
        request.occurrences = int(prior.get("occurrences") or 0) + 1
        request.issue_number = prior.get("issue_number")
        request.issue_url = prior.get("issue_url")
        request.status = prior.get("status") or request.status
        # A previously-failed or credential-less attempt is worth retrying: the
        # ask is on the ledger but nobody can see it on GitHub yet.
        if not request.issue_url and self.filer.enabled:
            self.filer.file(request)
        return prior

    def _persist(self, request: LibraryRequest) -> None:
        """Write ``request`` into the ledger under an exclusive lock.

        Several runners can plan concurrently, so the read-modify-write is done
        while holding a lock on the ledger file and the replacement is atomic.
        ``flock`` is POSIX-only; without it we still write atomically, and the
        GitHub-side marker search keeps duplicate issues from being filed.
        """
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.ledger_path.with_suffix(self.ledger_path.suffix + ".lock")
        lock = None
        try:
            import fcntl
            lock = open(lock_path, "a+")  # noqa: SIM115 (released in finally)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            if lock is not None:
                lock.close()
                lock = None
        try:
            ledger = self._load_ledger()
            ledger[request.canonical] = request.as_dict()
            tmp = self.ledger_path.with_suffix(self.ledger_path.suffix + ".tmp")
            tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.ledger_path)
        finally:
            if lock is not None:
                try:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                lock.close()


if __name__ == "__main__":  # pragma: no cover - manual smoke
    import doctest
    doctest.testmod(verbose=False)
    req = LibraryRequest(library="VASP", source=SOURCE_USER, alternative="ASE",
                         reason="user asked for VASP for the band gap")
    print(issue_title(req))
    print(issue_body(req))
    print()
    print(req.note())
