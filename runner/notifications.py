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

``boto3`` (and, for per-user routing, ``RunnerDB``) are imported lazily, only when
an AWS backend is selected, so the default ``log`` path keeps the runner
import-light and dependency-free.

Per-user routing: the SES backend emails the *specific* researcher who owns the
run — resolved from the ``users`` table via the run's conversation — so a
suspended run reaches the person who left it, not one global inbox. It falls back
to ``TWAIN_NOTIFY_EMAIL`` when the owner has no email on file (or the lookup
fails). SNS still publishes to a single topic (``users`` has no phone column yet),
which is the right fan-out target for SMS until per-user phone numbers exist.
"""
import json
import logging
import os
import urllib.request

logger = logging.getLogger("twain.runner.notify")

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"

# A short, human-facing reason label per suspend reason, used in the message.
_REASON_LABEL = {
    "input": "needs your input",
    "approval": "is waiting for your approval",
}


def _resume_hint(session_id: str) -> str:
    """A link back to the conversation, if the app URL is configured."""
    base = os.getenv("TWAIN_APP_URL", "").rstrip("/")
    return f" Open {base}/conversations/{session_id} to continue." if base else ""


def _compose(session_id: str, reason: str, message: str) -> tuple[str, str]:
    """Return a (subject, body) pair for the notification."""
    label = _REASON_LABEL.get(reason, "needs your attention")
    subject = f"TWAIN run {label}"
    body = f"Your TWAIN run {label}.\n\n{message}{_resume_hint(session_id)}"
    return subject, body


def default_notifier(session_id: str, reason: str, message: str) -> None:
    """Dispatch a suspend notification via the configured backend.

    Never raises: a notification failure must not fail (or unpause) the run — the
    user can always come back on their own — so problems are logged, not thrown.
    """
    backend = os.getenv("TWAIN_NOTIFY_BACKEND", "log").strip().lower()
    try:
        if backend == "sns":
            _notify_sns(session_id, reason, message)
        elif backend == "ses":
            _notify_ses(session_id, reason, message)
        elif backend == "sendgrid":
            _notify_sendgrid(session_id, reason, message)
        else:
            if backend != "log":
                logger.warning("[notify] unknown TWAIN_NOTIFY_BACKEND %r; logging only", backend)
            subject, body = _compose(session_id, reason, message)
            logger.info("[notify] %s — %s | %s", session_id, subject, body)
    except Exception as exc:  # noqa: BLE001 -- notifications are best-effort
        logger.warning("[notify] failed to notify for %s (%s): %s", session_id, reason, exc)


def _notify_sns(session_id: str, reason: str, message: str) -> None:
    topic = os.getenv("TWAIN_NOTIFY_SNS_TOPIC_ARN")
    if not topic:
        raise RuntimeError("TWAIN_NOTIFY_BACKEND=sns but TWAIN_NOTIFY_SNS_TOPIC_ARN is unset")
    import boto3  # lazy: only when the SNS backend is actually used

    subject, body = _compose(session_id, reason, message)
    client = boto3.client("sns", region_name=os.getenv("AWS_REGION", "us-east-1"))
    client.publish(TopicArn=topic, Subject=subject[:100], Message=body)


def _owner_email(session_id: str) -> str | None:
    """Best-effort email of the run's owner, or None. Never raises.

    Lazy-imports ``RunnerDB`` so the ``log`` backend and the unit tests stay
    DB-free; any lookup failure (unknown session, DB down, non-UUID id) degrades
    to None so the caller falls back to the configured global address.
    """
    try:
        from runner.db import RunnerDB

        return RunnerDB().get_session_owner_email(session_id)
    except Exception as exc:  # noqa: BLE001 -- routing must never fail the notify
        logger.warning("[notify] owner-email lookup failed for %s: %s", session_id, exc)
        return None


def _resolve_recipient(session_id: str) -> str | None:
    """Who to email for this run: its owner, else the global TWAIN_NOTIFY_EMAIL."""
    return _owner_email(session_id) or os.getenv("TWAIN_NOTIFY_EMAIL")


def _notify_ses(session_id: str, reason: str, message: str) -> None:
    to_addr = _resolve_recipient(session_id)
    from_addr = os.getenv("TWAIN_NOTIFY_FROM", to_addr)
    if not to_addr:
        raise RuntimeError(
            "TWAIN_NOTIFY_BACKEND=ses but no recipient "
            "(run owner has no email and TWAIN_NOTIFY_EMAIL is unset)"
        )
    import boto3  # lazy: only when the SES backend is actually used

    subject, body = _compose(session_id, reason, message)
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


def _notify_sendgrid(session_id: str, reason: str, message: str) -> None:
    """Email the run's owner via the SendGrid HTTP API.

    Recipient is the run owner (per-user routing), falling back to
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
    to_addr = _resolve_recipient(session_id)
    if not to_addr:
        raise RuntimeError(
            "TWAIN_NOTIFY_BACKEND=sendgrid but no recipient "
            "(run owner has no email and TWAIN_NOTIFY_EMAIL is unset)"
        )
    from_addr = os.getenv("TWAIN_NOTIFY_FROM")
    if not from_addr:
        raise RuntimeError(
            "TWAIN_NOTIFY_BACKEND=sendgrid but TWAIN_NOTIFY_FROM is unset "
            "(SendGrid requires a verified sender address)"
        )
    subject, body = _compose(session_id, reason, message)
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
    request = urllib.request.Request(
        SENDGRID_API_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310 (fixed https URL)
        if resp.status not in (200, 202):  # SendGrid returns 202 Accepted on success
            raise RuntimeError(f"SendGrid returned HTTP {resp.status}")
