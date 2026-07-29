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
* ``sns``      — publish to ``TWAIN_NOTIFY_SNS_TOPIC_ARN`` (SMS/fan-out).
* ``ses``      — email via AWS SES (needs AWS creds + verified identities).
* ``sendgrid`` — email via the SendGrid HTTP API. Key in ``TWAIN_SENDGRID_API_KEY``
  (put it in the repo-root ``.env`` next to ``API_KEY``; cloud pulls it from
  Secrets Manager). Sender in ``TWAIN_NOTIFY_FROM`` (a SendGrid-verified sender).
  No AWS or extra pip dependency — it POSTs with stdlib ``urllib``.

``boto3`` is imported lazily, only when an AWS backend is selected, so the default
``log`` path keeps the runner import-light and dependency-free.

Per-user routing (Phase 2): the notifier reaches the *specific* researcher who
owns the run, not one global inbox/topic. :func:`make_notifier` resolves the
owner's contact (``{"email", "name", "phone"}``) from the ``users`` table via the
run's conversation and passes it as ``recipient``; each backend then targets it —
SES/SendGrid email ``recipient["email"]`` and SNS texts ``recipient["phone"]``
directly. Every target falls back to the configured global address/topic
(``TWAIN_NOTIFY_EMAIL`` / ``TWAIN_NOTIFY_SNS_TOPIC_ARN``) when the owner has no
contact on file, and a lookup failure degrades to that fallback rather than
failing the run.
"""
import json
import logging
import os
import urllib.request

logger = logging.getLogger("twain.runner.notify")

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"

# A short, human-facing reason label per reason. The first two are *suspend*
# reasons (the run needs the user); "completed"/"failed" are terminal reasons
# (the run finished) — see _drive_run in runner.py.
_REASON_LABEL = {
    "input": "needs your input",
    "approval": "is waiting for your approval",
    "completed": "has finished",
    "failed": "failed",
}


def _resume_hint(session_id: str) -> str:
    """A link back to the conversation, if the app URL is configured."""
    base = os.getenv("TWAIN_APP_URL", "").rstrip("/")
    return f" Open {base}/conversations/{session_id} to continue." if base else ""


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
    """
    def _notifier(session_id: str, reason: str, message: str) -> None:
        try:
            recipient = db.owner_contact(session_id)
        except Exception as exc:  # noqa: BLE001 -- routing must never fail the notify
            logger.warning("[notify] owner lookup failed for %s: %s", session_id, exc)
            recipient = None
        try:
            request = db.run_title(session_id)
        except Exception as exc:  # noqa: BLE001 -- a missing title must not fail the notify
            logger.warning("[notify] title lookup failed for %s: %s", session_id, exc)
            request = None
        default_notifier(session_id, reason, message, recipient=recipient, request=request)

    return _notifier


def _recipient_email(recipient: dict | None) -> str | None:
    """The owner's email if on file, else the configured global fallback address."""
    return (recipient or {}).get("email") or os.getenv("TWAIN_NOTIFY_EMAIL")


def default_notifier(
    session_id: str, reason: str, message: str, recipient: dict | None = None,
    request: str | None = None,
) -> None:
    """Dispatch a suspend notification to the run's owner via the configured backend.

    ``recipient`` is the owner's contact (``{"email", "name", "phone"}``) as
    resolved by :func:`make_notifier`; any field may be None, in which case the
    backend falls back to the configured global address/topic. Never raises: a
    notification failure must not fail (or unpause) the run — the user can always
    come back on their own — so problems are logged, not thrown.
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
            to_addr = _recipient_email(recipient)
            logger.info(
                "[notify] %s → %s — %s | %s",
                session_id, to_addr or "<no recipient>", subject, body,
            )
    except Exception as exc:  # noqa: BLE001 -- notifications are best-effort
        logger.warning("[notify] failed to notify for %s (%s): %s", session_id, reason, exc)


def _notify_sns(
    session_id: str, reason: str, message: str, recipient: dict | None = None,
    request: str | None = None,
) -> None:
    phone = (recipient or {}).get("phone")
    topic = os.getenv("TWAIN_NOTIFY_SNS_TOPIC_ARN")
    if not phone and not topic:
        raise RuntimeError(
            "TWAIN_NOTIFY_BACKEND=sns but no target "
            "(run owner has no phone and TWAIN_NOTIFY_SNS_TOPIC_ARN is unset)"
        )
    import boto3  # lazy: only when the SNS backend is actually used

    subject, body = _compose(session_id, reason, message, request)
    client = boto3.client("sns", region_name=os.getenv("AWS_REGION", "us-east-1"))
    if phone:
        # A known phone wins over the topic: SMS the run's owner directly.
        client.publish(PhoneNumber=phone, Message=body)
    else:
        client.publish(TopicArn=topic, Subject=subject[:100], Message=body)


def _notify_ses(
    session_id: str, reason: str, message: str, recipient: dict | None = None,
    request: str | None = None,
) -> None:
    to_addr = _recipient_email(recipient)
    if not to_addr:
        # No owner email and no configured default: best-effort, so log and skip.
        logger.info("[notify] SES: no recipient for %s; skipping", session_id)
        return
    from_addr = os.getenv("TWAIN_NOTIFY_FROM", to_addr)
    import boto3  # lazy: only when the SES backend is actually used

    subject, body = _compose(session_id, reason, message, request)
    client = boto3.client("ses", region_name=os.getenv("AWS_REGION", "us-east-1"))
    client.send_email(
        Source=from_addr,
        Destination={"ToAddresses": [to_addr]},
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
    )


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

    Recipient is the run owner (``recipient["email"]``), falling back to
    ``TWAIN_NOTIFY_EMAIL``. Requires ``TWAIN_SENDGRID_API_KEY`` and a
    SendGrid-verified sender in ``TWAIN_NOTIFY_FROM``.
    """
    _ensure_env_loaded()
    api_key = os.getenv("TWAIN_SENDGRID_API_KEY")
    if not api_key:
        raise RuntimeError(
            "TWAIN_NOTIFY_BACKEND=sendgrid but TWAIN_SENDGRID_API_KEY is unset "
            "(put it in the repo-root .env, or the deployment's secret store)"
        )
    to_addr = _recipient_email(recipient)
    if not to_addr:
        # No owner email and no configured default: best-effort, so log and skip.
        logger.info("[notify] SendGrid: no recipient for %s; skipping", session_id)
        return
    from_addr = os.getenv("TWAIN_NOTIFY_FROM")
    if not from_addr:
        raise RuntimeError(
            "TWAIN_NOTIFY_BACKEND=sendgrid but TWAIN_NOTIFY_FROM is unset "
            "(SendGrid requires a verified sender address)"
        )
    subject, body = _compose(session_id, reason, message, request)
    _sendgrid_post(api_key, from_addr, to_addr, subject, body)


def _sendgrid_post(api_key: str, from_addr: str, to_addr: str, subject: str, body: str) -> None:
    """POST one plain-text mail to the SendGrid v3 API (stdlib only)."""
    payload = json.dumps(
        {
            "personalizations": [{"to": [{"email": to_addr}]}],
            "from": {"email": from_addr},
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
    with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310 (fixed https URL)
        if resp.status not in (200, 202):  # SendGrid returns 202 Accepted on success
            raise RuntimeError(f"SendGrid returned HTTP {resp.status}")
