"""Outbound notifications for suspended runs (Phase 2).

When a run suspends waiting on the researcher — a clarification question, the
heavy-calc confirmation, or the plan-approval gate — the runner releases the
process (Phase 1). This module is how the run *reaches back out* so the user
knows there's something waiting, and can return whenever they're ready. That is
the end-state Robert described: leave a session any time, get pinged, resume.

The notifier is a plain ``(session_id, reason, message) -> None`` callable so it
is trivial to fake in tests and swap per deployment. The default dispatches on
``TWAIN_NOTIFY_BACKEND``:

* ``log``  (default) — write a line to the runner log. Zero infra; fine for dev
  and for deployments that rely on the user keeping the tab open / SSE.
* ``sns``  — publish to ``TWAIN_NOTIFY_SNS_TOPIC_ARN`` (SMS/fan-out).
* ``ses``  — email ``TWAIN_NOTIFY_EMAIL`` from ``TWAIN_NOTIFY_FROM``.

``boto3`` is imported lazily, only when an AWS backend is selected, so the
default path keeps the runner import-light and dependency-free.

Known gap (follow-up): SNS/SES here notify a single configured topic/address,
not the specific researcher who owns the run — wiring per-user contact details
(from the ``users`` table) is a small addition once contact fields exist.
"""
import logging
import os

logger = logging.getLogger("twain.runner.notify")

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


def _notify_ses(session_id: str, reason: str, message: str) -> None:
    to_addr = os.getenv("TWAIN_NOTIFY_EMAIL")
    from_addr = os.getenv("TWAIN_NOTIFY_FROM", to_addr)
    if not to_addr:
        raise RuntimeError("TWAIN_NOTIFY_BACKEND=ses but TWAIN_NOTIFY_EMAIL is unset")
    import boto3  # lazy: only when the SES backend is actually used

    subject, body = _compose(session_id, reason, message)
    client = boto3.client("ses", region_name=os.getenv("AWS_REGION", "us-east-1"))
    client.send_email(
        Source=from_addr,
        Destination={"ToAddresses": [to_addr]},
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
    )
