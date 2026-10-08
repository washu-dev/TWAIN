"""Outbound notifications for suspended runs (Phase 2).

When a run suspends waiting on the researcher — a clarification question, the
heavy-calc confirmation, or the plan-approval gate — the runner releases the
process (Phase 1). This module is how the run *reaches back out* so the user
knows there's something waiting, and can return whenever they're ready. That is
the end-state Robert described: leave a session any time, get pinged, resume.

The notifier is a plain ``(session_id, reason, message) -> None`` callable so it
is trivial to fake in tests and swap per deployment. The default dispatches on
``TWAIN_NOTIFY_BACKEND``:

* ``log``      (default) — write a line to the runner log. Zero infra; fine for
  dev and for deployments that rely on the user keeping the tab open / SSE.
* ``sns``      — SMS the owner's ``users.phone`` via SNS (needs AWS creds).
* ``ses``      — email via AWS SES (needs AWS creds + verified identities).
* ``sendgrid`` — email via the SendGrid HTTP API. Key in ``TWAIN_SENDGRID_API_KEY``
  (put it in the repo-root ``.env`` next to ``API_KEY``; cloud pulls it from
  Secrets Manager). Sender in ``TWAIN_NOTIFY_FROM`` (a SendGrid-verified sender).
  No AWS or extra pip dependency — it POSTs with stdlib ``urllib``.

``boto3`` is imported lazily, only when an AWS backend is selected, so the default
``log`` path keeps the runner import-light and dependency-free.

Per-user routing (Phase 2): the notifier reaches the *specific* researcher who
owns the run, and nobody else. :func:`make_notifier` resolves the owner's contact
(``{"email", "name", "phone"}``) from the ``users`` table via the run's
conversation and passes it as ``recipient``; each backend then targets it —
SES/SendGrid email ``recipient["email"]``, SNS texts ``recipient["phone"]``.

There is **no global fallback destination**. A run whose owner has no address (or
no phone) is simply not notified, logged at WARNING; an owner lookup that fails
likewise sends nothing. Redirecting to an operator inbox or a shared SNS topic
put one person's research prompt, results and gate questions in front of
somebody who could neither act on them nor turn them off — the owner's
notification preferences live on the owner's row — so the only correct
destination is the owner, and the only alternative is silence.

Flood rails: notifications are de-duplicated and rate-capped per run before they
reach a backend (see :func:`_throttle`), because "best-effort, never fails the
run" must not also mean "will mail the same thing a thousand times if something
upstream loops".
"""
import email.utils
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("twain.runner.notify")

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"

# A short, human-facing reason label per reason. The first two are *suspend*
# reasons (the run needs the user); the rest are terminal reasons (the run
# finished) — see _drive_run / _finalize_cancelled in runner.py.
_REASON_LABEL = {
    "input": "needs your input",
    "approval": "is waiting for your approval",
    "completed": "has finished",
    "failed": "failed",
    "terminated": "was terminated",
}

# Every reason a user can opt in/out of on the Settings page. Keep in lockstep
# with _REASON_LABEL and the api's notification-preferences endpoint.
NOTIFY_KINDS = tuple(_REASON_LABEL)


def _env_seconds(name: str, default: float) -> float:
    """Read a non-negative float env override, falling back to ``default``."""
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value >= 0 else default


# ---- flood rails --------------------------------------------------------------
# A notification is a side effect on somebody's inbox, so "don't say the same
# thing twice" and "never storm an inbox" belong here, at the one place every
# send passes through, rather than in each of the callers that decides to notify.
# Without them a run that gets driven repeatedly (a redundant resume, a reaped
# job re-driven after a crash, a bug in the driver) mails the owner once per
# drive -- which is how one run produced ~1900 emails.
#
# Two independent rails, both per run:
#   * de-dup: an identical (reason, message) send inside DEDUPE_SECONDS is a
#     repeat of one the researcher already has, so drop it. The window is short
#     on purpose -- a rejected plan is revised and re-posted within minutes, and
#     that second approval card carries the same words but is a real second ask.
#     It only has to cover a loop re-notifying back-to-back; the cap covers the
#     slow case.
#   * cap: at most MAX_PER_HOUR sends per run per hour, whatever they say. This
#     is the rail that bounds a storm nobody predicted.
# Both are advisory ceilings an operator can widen/narrow via the env; set
# either to 0 to disable that rail.
#
# Scope is this process (a dict, not a table): the runner is long-lived, so this
# catches a storm as it builds without adding a migration or a DB round-trip to
# every notification. It is a backstop, not the fix -- a driver that re-notifies
# is a bug to fix at the source (see runner._drive_run).
NOTIFY_DEDUPE_SECONDS = _env_seconds("TWAIN_NOTIFY_DEDUPE_SECONDS", 120.0)
NOTIFY_MAX_PER_HOUR = _env_seconds("TWAIN_NOTIFY_MAX_PER_HOUR", 12.0)

_HOUR = 3600.0
_throttle_lock = threading.Lock()
# session_id -> [(monotonic_sent_at, fingerprint), ...] within the last hour.
_recent_sends: dict[str, list[tuple[float, str]]] = {}


def reset_notify_throttle() -> None:
    """Forget every recorded send (tests; a fresh process starts empty anyway)."""
    with _throttle_lock:
        _recent_sends.clear()


def _fingerprint(reason: str, message: str) -> str:
    """Identity of a notification: same reason + same words = the same email."""
    digest = hashlib.sha256(f"{reason}\x00{message}".encode("utf-8", "replace"))
    return digest.hexdigest()[:16]


def _throttle(session_id: str, reason: str, message: str) -> str | None:
    """Record this send, or return why it must be suppressed.

    ``None`` means "send it" (and the attempt is now on the books). A returned
    string is a human-readable reason the caller logs — a duplicate of a recent
    notification, or this run's hourly ceiling.

    An *attempt* is what's recorded, not a confirmed delivery: a send that the
    backend then rejects still counts. Nothing retries a notification, so there
    is no second attempt to protect, and counting attempts is what keeps a
    failing backend from being hammered once per drive.
    """
    now = time.monotonic()
    fingerprint = _fingerprint(reason, message)
    with _throttle_lock:
        window = max(NOTIFY_DEDUPE_SECONDS, _HOUR)
        recent = [entry for entry in _recent_sends.get(session_id, [])
                  if now - entry[0] < window]
        if NOTIFY_DEDUPE_SECONDS and any(
            fp == fingerprint and now - sent_at < NOTIFY_DEDUPE_SECONDS
            for sent_at, fp in recent
        ):
            _recent_sends[session_id] = recent
            return (
                f"an identical '{reason}' notification was sent in the last "
                f"{int(NOTIFY_DEDUPE_SECONDS)}s"
            )
        in_last_hour = sum(1 for sent_at, _fp in recent if now - sent_at < _HOUR)
        if NOTIFY_MAX_PER_HOUR and in_last_hour >= NOTIFY_MAX_PER_HOUR:
            _recent_sends[session_id] = recent
            return (
                f"this run already sent {in_last_hour} notifications in the last hour "
                f"(cap: TWAIN_NOTIFY_MAX_PER_HOUR={int(NOTIFY_MAX_PER_HOUR)})"
            )
        recent.append((now, fingerprint))
        _recent_sends[session_id] = recent
        return None


def notification_allowed(prefs: dict | None, reason: str) -> bool:
    """Whether the owner's preferences permit sending this ``reason``.

    ``prefs`` is the ``users.notify_prefs`` JSONB:
    ``{"enabled": bool, "kinds": {reason: bool, ...}}`` — every key optional,
    and a missing key means "send" (so ``{}``/None keep today's behavior and a
    malformed value can never silence a user who didn't opt out).

    >>> notification_allowed(None, "completed")
    True
    >>> notification_allowed({"enabled": False}, "completed")
    False
    >>> notification_allowed({"kinds": {"completed": False}}, "completed")
    False
    >>> notification_allowed({"kinds": {"completed": False}}, "terminated")
    True
    """
    if not isinstance(prefs, dict):
        return True
    if prefs.get("enabled") is False:
        return False
    kinds = prefs.get("kinds")
    if isinstance(kinds, dict) and kinds.get(reason) is False:
        return False
    return True


def _resume_hint(session_id: str) -> str:
    """A link back to the conversation, if the app URL is configured.

    The base is normalized to its ORIGIN (scheme + host): operators tend to
    paste the URL of whatever page they had open (".../dashboard") into
    ``TWAIN_APP_URL``, and the ``/conversations/<id>`` deep link only exists at
    the site root -- a leftover page path made every email link land on
    expo-router's "Unmatched route" screen.
    """
    base = os.getenv("TWAIN_APP_URL", "").strip().rstrip("/")
    if not base:
        return ""
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme and parsed.netloc:
        base = f"{parsed.scheme}://{parsed.netloc}"
    return f" Open {base}/conversations/{session_id} to continue."


def _short_id(session_id: str) -> str:
    """The leading segment of the session UUID — enough to tell two runs apart."""
    return session_id.split("-", 1)[0] if session_id else session_id


def _compose(
    session_id: str, reason: str, message: str, request: str | None = None
) -> tuple[str, str]:
    """Return a (subject, body) pair for the notification.

    ``request`` is the run's title (its originating prompt); when present it and a
    short run id go into the subject so a researcher with several runs can tell
    the emails apart (otherwise every run of the same prompt looks identical).
    """
    label = _REASON_LABEL.get(reason, "needs your attention")
    req = (request or "").strip()
    if len(req) > 60:
        req = req[:57] + "…"
    tag = f' "{req}"' if req else ""
    subject = f"TWAIN run{tag} {label} ({_short_id(session_id)})"
    body = f"Your TWAIN run{tag} {label}.\n\n{message}{_resume_hint(session_id)}"
    return subject, body


def make_notifier(db):
    """Build a notifier that targets each run's *owner*, resolved via ``db``.

    Returns a ``(session_id, reason, message) -> None`` callable — the interface
    the bridges expect — that looks up the owner's contact through
    ``db.owner_contact(session_id)`` and hands it to :func:`default_notifier` as
    ``recipient``. A lookup failure degrades to ``recipient=None`` (so the backend
    uses the configured global fallback): routing must never fail the run.

    This is also where the flood rails apply (:func:`_throttle`): every
    production notification goes through the notifier built here, so a duplicate
    or a storm is stopped once, for every backend, rather than in each gate.
    """
    def _notifier(session_id: str, reason: str, message: str) -> None:
        try:
            recipient = db.owner_contact(session_id)
        except Exception as exc:  # noqa: BLE001 -- routing must never fail the notify
            logger.warning("[notify] owner lookup failed for %s: %s", session_id, exc)
            recipient = None
        # Honor the owner's Settings-page preferences. Only an explicit opt-out
        # suppresses the send; a failed lookup (recipient None) falls through so
        # a DB blip can't silently mute someone who wanted the email.
        if not notification_allowed((recipient or {}).get("notify_prefs"), reason):
            logger.info(
                "[notify] %s: owner opted out of '%s' notifications; skipping",
                session_id, reason,
            )
            return
        # Flood rails: a duplicate or a storm is dropped here, loudly, so the log
        # shows what was withheld (and why) instead of the inbox showing it twice.
        suppressed = _throttle(session_id, reason, message)
        if suppressed:
            logger.warning(
                "[notify] %s: dropped '%s' notification — %s", session_id, reason, suppressed
            )
            return
        try:
            request = db.run_title(session_id)
        except Exception as exc:  # noqa: BLE001 -- a missing title must not fail the notify
            logger.warning("[notify] title lookup failed for %s: %s", session_id, exc)
            request = None
        default_notifier(session_id, reason, message, recipient=recipient, request=request)

    return _notifier


def _recipient_email(recipient: dict | None, session_id: str = "") -> str | None:
    """The run owner's email address, or None if there isn't one on file.

    There is deliberately no fallback address. A run's notifications belong to the
    one researcher who owns it: mailing anyone else — an operator inbox, a shared
    address — hands them somebody else's prompt, results and gate prompts, and
    leaves the recipient unable to act on or silence them (preferences live on the
    owner's row). ``users.email`` is nullable, and an Entra token carrying no
    ``preferred_username``/``email``/``upn`` claim stores an empty one, so this
    genuinely happens; when it does the right outcome is *no email*.

    Logged at WARNING rather than passed over: a researcher who never hears back
    is a data problem to fix (populate ``users.email``), not a quiet non-event.
    """
    owner_email = (recipient or {}).get("email")
    if owner_email:
        return owner_email
    logger.warning(
        "[notify] %s: no email on file for this run's owner — nothing sent "
        "(populate users.email for that account; notifications are never "
        "redirected to another address)",
        session_id or "<unknown run>",
    )
    return None


def default_notifier(
    session_id: str, reason: str, message: str, recipient: dict | None = None,
    request: str | None = None,
) -> None:
    """Dispatch a suspend notification to the run's owner via the configured backend.

    ``recipient`` is the owner's contact (``{"email", "name", "phone"}``) as
    resolved by :func:`make_notifier`. A field the owner doesn't have means this
    run cannot be notified through that backend, and it is skipped — nothing is
    ever redirected to a global address or topic. Never raises: a notification
    failure must not fail (or unpause) the run — the user can always come back on
    their own — so problems are logged, not thrown.
    """
    backend = os.getenv("TWAIN_NOTIFY_BACKEND", "log").strip().lower()
    try:
        if backend == "sns":
            _notify_sns(session_id, reason, message, recipient, request)
        elif backend == "ses":
            _notify_ses(session_id, reason, message, recipient, request)
        elif backend == "sendgrid":
            _notify_sendgrid(session_id, reason, message, recipient, request)
        else:
            if backend != "log":
                logger.warning("[notify] unknown TWAIN_NOTIFY_BACKEND %r; logging only", backend)
            subject, body = _compose(session_id, reason, message, request)
            # The owner's address as-is: this backend only writes a log line, so
            # it reports who *would* be mailed without _recipient_email's warning.
            to_addr = (recipient or {}).get("email")
            logger.info(
                "[notify] %s → %s — %s | %s",
                session_id, to_addr or "<no recipient>", subject, body,
            )
    except Exception as exc:  # noqa: BLE001 -- notifications are best-effort
        # Best-effort must not mean invisible: a wrong key or an unverified
        # sender fails every send silently otherwise, and the first symptom is a
        # researcher saying they never heard back. ERROR, with the backend named.
        logger.error(
            "[notify] backend '%s' failed to notify %s (%s): %s",
            backend, session_id, reason, exc,
        )


def _notify_sns(
    session_id: str, reason: str, message: str, recipient: dict | None = None,
    request: str | None = None,
) -> None:
    """SMS the run's owner. No phone on file means no message — see :func:`_recipient_email`.

    There is no topic fan-out any more: a shared topic is the same wrong-recipient
    problem as a shared inbox, one delivery further away from the person who can
    act on it.
    """
    phone = (recipient or {}).get("phone")
    if not phone:
        logger.warning(
            "[notify] %s: no phone on file for this run's owner — nothing sent "
            "(add users.phone for that account)", session_id,
        )
        return
    import boto3  # lazy: only when the SNS backend is actually used

    _subject, body = _compose(session_id, reason, message, request)
    client = boto3.client("sns", region_name=os.getenv("AWS_REGION", "us-east-1"))
    client.publish(PhoneNumber=phone, Message=body)
    logger.info("[notify] SNS accepted '%s' for %s → the owner's phone", reason, session_id)


def _notify_ses(
    session_id: str, reason: str, message: str, recipient: dict | None = None,
    request: str | None = None,
) -> None:
    """Email the run's owner via SES. No address on file means no email."""
    to_addr = _recipient_email(recipient, session_id)
    if not to_addr:
        return  # the owner has no address; _recipient_email said so
    from_addr = os.getenv("TWAIN_NOTIFY_FROM", to_addr)
    import boto3  # lazy: only when the SES backend is actually used

    subject, body = _compose(session_id, reason, message, request)
    client = boto3.client("ses", region_name=os.getenv("AWS_REGION", "us-east-1"))
    client.send_email(
        Source=from_addr,
        Destination={"ToAddresses": [to_addr]},
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
    )
    logger.info("[notify] SES accepted '%s' for %s → %s", reason, session_id, to_addr)


def _ensure_env_loaded() -> None:
    """Best-effort load of the repo-root ``.env`` so keys placed there are visible.

    A full runner slice already loads ``.env`` (the engine bootstrap calls
    ``load_dotenv``), but a bare invocation of the notifier — the Tier-0 smoke test
    — does not. Loading here means "put ``TWAIN_SENDGRID_API_KEY`` in ``.env``"
    works no matter how the notifier is reached. Swallows everything: dotenv is a
    convenience, not a requirement (an already-exported env var still wins).
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001, S110 -- .env loading is best-effort
        pass


def _notify_sendgrid(
    session_id: str, reason: str, message: str, recipient: dict | None = None,
    request: str | None = None,
) -> None:
    """Email the run's owner via the SendGrid HTTP API.

    The only recipient is the run's owner (``recipient["email"]``); a run whose
    owner has no address on file is not mailed at all. Requires
    ``TWAIN_SENDGRID_API_KEY`` and a SendGrid-verified sender in
    ``TWAIN_NOTIFY_FROM``.
    """
    _ensure_env_loaded()
    api_key = os.getenv("TWAIN_SENDGRID_API_KEY")
    if not api_key:
        raise RuntimeError(
            "TWAIN_NOTIFY_BACKEND=sendgrid but TWAIN_SENDGRID_API_KEY is unset "
            "(put it in the repo-root .env, or the deployment's secret store)"
        )
    to_addr = _recipient_email(recipient, session_id)
    if not to_addr:
        return  # the owner has no address; _recipient_email said so
    from_addr = os.getenv("TWAIN_NOTIFY_FROM")
    if not from_addr:
        raise RuntimeError(
            "TWAIN_NOTIFY_BACKEND=sendgrid but TWAIN_NOTIFY_FROM is unset "
            "(SendGrid requires a verified sender address)"
        )
    subject, body = _compose(session_id, reason, message, request)
    _sendgrid_post(api_key, from_addr, to_addr, subject, body)
    # An accepted send is the only positive evidence the email path works; log it
    # so "did the researcher get told?" is answerable from the runner log.
    logger.info(
        "[notify] SendGrid accepted '%s' for %s → %s", reason, session_id, to_addr
    )


def _sendgrid_sender(from_addr: str) -> dict:
    """``TWAIN_NOTIFY_FROM`` as SendGrid's sender object.

    Accepts a bare address or a display form -- "DI2 Accelerator
    <di2accelerator@wustl.edu>" -- which SendGrid rejects in its ``email`` field.
    """
    name, address = email.utils.parseaddr(from_addr)
    if not address or "@" not in address:
        raise RuntimeError(f"TWAIN_NOTIFY_FROM is not an email address: {from_addr!r}")
    return {"email": address, "name": name} if name else {"email": address}


def _sendgrid_post(api_key: str, from_addr: str, to_addr: str, subject: str, body: str) -> None:
    """POST one plain-text mail to the SendGrid v3 API (stdlib only).

    Raises ``RuntimeError`` carrying SendGrid's own explanation on a rejection:
    urlopen turns any 4xx/5xx into an ``HTTPError``, so a bad key (401) or an
    unverified sender (403) never reaches the status check below — and without
    reading the error body the log said only "HTTP Error 403: Forbidden", which
    doesn't tell an operator which of the two to fix.
    """
    payload = json.dumps(
        {
            "personalizations": [{"to": [{"email": to_addr}]}],
            "from": _sendgrid_sender(from_addr),
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 (fixed https SendGrid API URL)
        SENDGRID_API_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310 (fixed https URL)
            if resp.status not in (200, 202):  # SendGrid returns 202 Accepted on success
                raise RuntimeError(f"SendGrid returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"SendGrid rejected the send: HTTP {exc.code} {_error_body(exc)}".strip()
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"SendGrid unreachable: {exc.reason}") from exc


def _error_body(exc: urllib.error.HTTPError) -> str:
    """SendGrid's JSON explanation of a rejection, trimmed; '' if unreadable."""
    try:
        return exc.read().decode("utf-8", "replace").strip()[:400]
    except Exception:  # noqa: BLE001 -- the status code alone still gets reported
        return ""
