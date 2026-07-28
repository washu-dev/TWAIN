"""Unit tests for Phase 2 owner-targeted notifications.

No AWS or Postgres needed: the DB is a fake exposing ``owner_contact`` and, for
the SES/SNS backends, a fake ``boto3`` is installed into ``sys.modules`` so we can
assert the dispatch targets the run *owner* (their email / phone) and falls back
to the configured default address when the owner has no contact on file.
"""
import sys
import types

import pytest

from runner import notifications
from runner.notifications import default_notifier, make_notifier

SESSION = "conv-1"


class FakeContactDB:
    """Minimal RunnerDB stand-in: just the owner_contact lookup the notifier uses."""

    def __init__(self, owner=None, raises=False):
        self._owner = owner
        self._raises = raises

    def owner_contact(self, session_id):
        if self._raises:
            raise RuntimeError("db down")
        return self._owner


class FakeSnsSes:
    """Records the last publish/send_email call so tests can assert the target."""

    def __init__(self):
        self.published = []
        self.emails = []

    def publish(self, **kwargs):
        self.published.append(kwargs)

    def send_email(self, **kwargs):
        self.emails.append(kwargs)


@pytest.fixture
def fake_boto3(monkeypatch):
    """Install a fake ``boto3`` whose client is a single recording double."""
    client = FakeSnsSes()
    module = types.SimpleNamespace(client=lambda *a, **k: client)
    monkeypatch.setitem(sys.modules, "boto3", module)
    return client


# ── make_notifier: resolve the owner and forward the recipient ────────────────
class TestMakeNotifier:
    def test_forwards_resolved_owner_as_recipient(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            notifications, "default_notifier",
            lambda sid, reason, msg, recipient=None: seen.update(
                sid=sid, reason=reason, msg=msg, recipient=recipient
            ),
        )
        owner = {"email": "researcher@wustl.edu", "name": "R", "phone": None}
        make_notifier(FakeContactDB(owner))(SESSION, "input", "Which solvent?")
        assert seen["recipient"] == owner
        assert seen == {
            "sid": SESSION, "reason": "input", "msg": "Which solvent?", "recipient": owner
        }

    def test_lookup_failure_falls_back_to_no_recipient(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            notifications, "default_notifier",
            lambda sid, reason, msg, recipient=None: seen.update(recipient=recipient),
        )
        # A DB error must not raise out of the notifier (best-effort).
        make_notifier(FakeContactDB(raises=True))(SESSION, "input", "Q")
        assert seen["recipient"] is None


# ── log backend (default): names the recipient, never raises ──────────────────
class TestLogBackend:
    def test_logs_owner_email(self, monkeypatch, caplog):
        monkeypatch.delenv("TWAIN_NOTIFY_BACKEND", raising=False)
        with caplog.at_level("INFO", logger="twain.runner.notify"):
            default_notifier(
                SESSION, "approval", "Plan ready",
                recipient={"email": "owner@wustl.edu"},
            )
        assert "owner@wustl.edu" in caplog.text

    def test_no_recipient_is_not_fatal(self, monkeypatch, caplog):
        monkeypatch.delenv("TWAIN_NOTIFY_BACKEND", raising=False)
        monkeypatch.delenv("TWAIN_NOTIFY_EMAIL", raising=False)
        with caplog.at_level("INFO", logger="twain.runner.notify"):
            default_notifier(SESSION, "input", "Q", recipient=None)
        assert "<no recipient>" in caplog.text


# ── SES backend: email the owner, else the configured default ─────────────────
class TestSesBackend:
    def test_emails_the_run_owner(self, monkeypatch, fake_boto3):
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "ses")
        monkeypatch.setenv("TWAIN_NOTIFY_EMAIL", "default@wustl.edu")
        default_notifier(
            SESSION, "input", "Which solvent?",
            recipient={"email": "owner@wustl.edu"},
        )
        assert fake_boto3.emails[0]["Destination"]["ToAddresses"] == ["owner@wustl.edu"]

    def test_falls_back_to_configured_email(self, monkeypatch, fake_boto3):
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "ses")
        monkeypatch.setenv("TWAIN_NOTIFY_EMAIL", "default@wustl.edu")
        default_notifier(SESSION, "input", "Q", recipient={"email": None})
        assert fake_boto3.emails[0]["Destination"]["ToAddresses"] == ["default@wustl.edu"]

    def test_no_address_anywhere_is_swallowed(self, monkeypatch, fake_boto3):
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "ses")
        monkeypatch.delenv("TWAIN_NOTIFY_EMAIL", raising=False)
        # No owner email and no configured default: logged, not raised, no send.
        default_notifier(SESSION, "input", "Q", recipient=None)
        assert fake_boto3.emails == []


# ── SNS backend: text the owner's phone, else publish to the topic ────────────
class TestSnsBackend:
    def test_texts_the_owner_phone_directly(self, monkeypatch, fake_boto3):
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "sns")
        monkeypatch.setenv("TWAIN_NOTIFY_SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:t")
        default_notifier(
            SESSION, "approval", "Plan ready",
            recipient={"phone": "+13145550123"},
        )
        # A known phone wins over the topic: publish directly to the number.
        assert fake_boto3.published[0]["PhoneNumber"] == "+13145550123"
        assert "TopicArn" not in fake_boto3.published[0]

    def test_falls_back_to_topic_without_a_phone(self, monkeypatch, fake_boto3):
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "sns")
        monkeypatch.setenv("TWAIN_NOTIFY_SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:t")
        default_notifier(SESSION, "input", "Q", recipient={"phone": None})
        assert fake_boto3.published[0]["TopicArn"] == "arn:aws:sns:us-east-1:1:t"
        assert "PhoneNumber" not in fake_boto3.published[0]
