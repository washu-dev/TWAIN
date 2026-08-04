"""Unit tests for Phase 2 owner-targeted notifications.

No AWS or Postgres needed: the DB is a fake exposing ``owner_contact`` and, for
the SES/SNS backends, a fake ``boto3`` is installed into ``sys.modules`` so we can
assert the dispatch targets the run *owner* (their email / phone) and falls back
to the configured default address when the owner has no contact on file.
"""
import io
import json
import sys
import types

import pytest

from runner import notifications
from runner.notifications import default_notifier, make_notifier

SESSION = "conv-1"


@pytest.fixture(autouse=True)
def _fresh_throttle():
    """The flood rails keep per-run state in the module, so clear it per test."""
    notifications.reset_notify_throttle()
    yield
    notifications.reset_notify_throttle()


# ── _compose: subject carries the request + a short run id ────────────────────
class TestCompose:
    def test_subject_includes_request_and_short_id(self):
        subject, body = notifications._compose(
            "68566684-9121-4d90-a59d-e669c8ce63a8", "approval", "Plan ready",
            request="Predict the band gap of silicon",
        )
        # The prompt + a short id let a researcher tell same-prompt runs apart.
        assert "Predict the band gap of silicon" in subject
        assert "68566684" in subject
        assert "waiting for your approval" in subject
        assert "Predict the band gap of silicon" in body

    def test_completed_and_failed_reasons(self):
        done, _ = notifications._compose("s-1", "completed", "All done")
        failed, _ = notifications._compose("s-1", "failed", "Boom")
        assert "finished" in done
        assert "failed" in failed

    def test_long_request_is_truncated(self):
        subject, _ = notifications._compose("s-1", "input", "Q", request="x" * 200)
        assert "…" in subject and len(subject) < 130

    def test_no_request_still_composes(self):
        subject, _ = notifications._compose("abcd1234-0000", "input", "Q")
        assert "needs your input" in subject
        assert "abcd1234" in subject


# ── _resume_hint: the email's "return to your run" link ───────────────────────
class TestResumeHint:
    def test_link_appends_the_conversation_path(self, monkeypatch):
        monkeypatch.setenv("TWAIN_APP_URL", "https://app.example.edu")
        assert "https://app.example.edu/conversations/conv-1" in \
            notifications._resume_hint("conv-1")

    def test_page_path_in_app_url_is_stripped_to_the_origin(self, monkeypatch):
        # Operators paste whatever page they had open (".../dashboard") into
        # TWAIN_APP_URL; the deep link only exists at the site root, and the
        # leftover path made every email link hit "Unmatched route".
        monkeypatch.setenv("TWAIN_APP_URL", "https://app.example.edu/dashboard")
        hint = notifications._resume_hint("conv-1")
        assert "https://app.example.edu/conversations/conv-1" in hint
        assert "/dashboard" not in hint

    def test_unset_app_url_means_no_hint(self, monkeypatch):
        monkeypatch.delenv("TWAIN_APP_URL", raising=False)
        assert notifications._resume_hint("conv-1") == ""


class FakeContactDB:
    """Minimal RunnerDB stand-in: the owner_contact + run_title lookups the notifier uses."""

    def __init__(self, owner=None, raises=False, title="Predict the band gap of silicon"):
        self._owner = owner
        self._raises = raises
        self._title = title

    def owner_contact(self, session_id):
        if self._raises:
            raise RuntimeError("db down")
        return self._owner

    def run_title(self, session_id):
        return self._title


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
            lambda sid, reason, msg, recipient=None, request=None: seen.update(
                sid=sid, reason=reason, msg=msg, recipient=recipient, request=request
            ),
        )
        owner = {"email": "researcher@wustl.edu", "name": "R", "phone": None}
        make_notifier(FakeContactDB(owner))(SESSION, "input", "Which solvent?")
        assert seen["recipient"] == owner
        assert seen == {
            "sid": SESSION, "reason": "input", "msg": "Which solvent?", "recipient": owner,
            "request": "Predict the band gap of silicon",  # forwarded for the subject line
        }

    def test_lookup_failure_falls_back_to_no_recipient(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            notifications, "default_notifier",
            lambda sid, reason, msg, recipient=None, request=None: seen.update(recipient=recipient),
        )
        # A DB error must not raise out of the notifier (best-effort).
        make_notifier(FakeContactDB(raises=True))(SESSION, "input", "Q")
        assert seen["recipient"] is None


# ── notify_prefs: the owner's Settings-page choices gate every send ────────────
class TestNotifyPrefs:
    def _capture(self, monkeypatch):
        sent = []
        monkeypatch.setattr(
            notifications, "default_notifier",
            lambda sid, reason, msg, recipient=None, request=None: sent.append(reason),
        )
        return sent

    def test_opted_out_kind_is_skipped(self, monkeypatch):
        sent = self._capture(monkeypatch)
        owner = {"email": "r@wustl.edu", "notify_prefs": {"kinds": {"completed": False}}}
        notify = make_notifier(FakeContactDB(owner))
        notify(SESSION, "completed", "done")   # opted out
        notify(SESSION, "failed", "boom")      # still on
        assert sent == ["failed"]

    def test_master_switch_silences_everything(self, monkeypatch):
        sent = self._capture(monkeypatch)
        owner = {"email": "r@wustl.edu", "notify_prefs": {"enabled": False}}
        notify = make_notifier(FakeContactDB(owner))
        for reason in notifications.NOTIFY_KINDS:
            notify(SESSION, reason, "x")
        assert sent == []

    def test_empty_prefs_send_everything(self, monkeypatch):
        sent = self._capture(monkeypatch)
        owner = {"email": "r@wustl.edu", "notify_prefs": {}}
        make_notifier(FakeContactDB(owner))(SESSION, "terminated", "stopped")
        assert sent == ["terminated"]

    def test_lookup_failure_fails_open(self, monkeypatch):
        # A DB blip must not silently mute a user who never opted out.
        sent = self._capture(monkeypatch)
        make_notifier(FakeContactDB(raises=True))(SESSION, "completed", "done")
        assert sent == ["completed"]

    def test_notification_allowed_semantics(self):
        allowed = notifications.notification_allowed
        assert allowed(None, "completed")
        assert allowed({}, "completed")
        assert not allowed({"enabled": False}, "completed")
        assert not allowed({"kinds": {"completed": False}}, "completed")
        assert allowed({"kinds": {"completed": False}}, "terminated")
        assert allowed("garbage", "completed")  # malformed prefs never mute

    def test_terminated_reason_has_a_label(self):
        subject, _ = notifications._compose("s-1", "terminated", "stopped")
        assert "was terminated" in subject


# ── flood rails: de-dup + hourly cap (the 1900-email incident) ─────────────────
class TestFloodRails:
    def _capture(self, monkeypatch):
        sent = []
        monkeypatch.setattr(
            notifications, "default_notifier",
            lambda sid, reason, msg, recipient=None, request=None: sent.append((reason, msg)),
        )
        return sent

    def test_identical_notification_is_sent_once(self, monkeypatch):
        # The same run being driven twice (a redundant resume, a re-queued job)
        # must not mail the same thing twice.
        sent = self._capture(monkeypatch)
        notify = make_notifier(FakeContactDB({"email": "r@wustl.edu"}))
        for _ in range(50):
            notify(SESSION, "completed", "Run complete.")
        assert sent == [("completed", "Run complete.")]

    def test_a_different_message_still_gets_through(self, monkeypatch):
        sent = self._capture(monkeypatch)
        notify = make_notifier(FakeContactDB({"email": "r@wustl.edu"}))
        notify(SESSION, "input", "Which solvent?")
        notify(SESSION, "input", "Which temperature?")   # a real second question
        notify(SESSION, "approval", "Which solvent?")    # same words, other gate
        assert len(sent) == 3

    def test_hourly_cap_stops_a_storm_of_distinct_messages(self, monkeypatch):
        monkeypatch.setattr(notifications, "NOTIFY_MAX_PER_HOUR", 5)
        sent = self._capture(monkeypatch)
        notify = make_notifier(FakeContactDB({"email": "r@wustl.edu"}))
        for i in range(200):
            notify(SESSION, "input", f"question {i}")  # each one unique
        assert len(sent) == 5

    def test_the_cap_is_per_run(self, monkeypatch):
        monkeypatch.setattr(notifications, "NOTIFY_MAX_PER_HOUR", 1)
        sent = self._capture(monkeypatch)
        notify = make_notifier(FakeContactDB({"email": "r@wustl.edu"}))
        notify("run-a", "input", "Q")
        notify("run-a", "input", "Q2")   # over run-a's cap
        notify("run-b", "input", "Q")    # a different run is unaffected
        assert len(sent) == 2

    def test_suppression_is_logged_not_silent(self, monkeypatch, caplog):
        self._capture(monkeypatch)
        notify = make_notifier(FakeContactDB({"email": "r@wustl.edu"}))
        notify(SESSION, "completed", "Run complete.")
        with caplog.at_level("WARNING", logger="twain.runner.notify"):
            notify(SESSION, "completed", "Run complete.")
        assert "dropped 'completed'" in caplog.text

    def test_rails_can_be_disabled_by_an_operator(self, monkeypatch):
        monkeypatch.setattr(notifications, "NOTIFY_DEDUPE_SECONDS", 0)
        monkeypatch.setattr(notifications, "NOTIFY_MAX_PER_HOUR", 0)
        sent = self._capture(monkeypatch)
        notify = make_notifier(FakeContactDB({"email": "r@wustl.edu"}))
        for _ in range(20):
            notify(SESSION, "completed", "Run complete.")
        assert len(sent) == 20


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

    def test_falling_back_to_the_global_inbox_is_flagged(self, monkeypatch, caplog):
        # users.email is nullable (and an Entra token with no email claim stores
        # an empty one), so a run whose owner has no address on file quietly mails
        # the *operator*, who could neither act on it nor switch it off. Now it
        # sends nothing — and says which run it withheld, so the missing
        # users.email can be found and fixed.
        monkeypatch.delenv("TWAIN_NOTIFY_BACKEND", raising=False)
        monkeypatch.setenv("TWAIN_NOTIFY_EMAIL", "operator@wustl.edu")
        with caplog.at_level("WARNING", logger="twain.runner.notify"):
            assert notifications._recipient_email({"email": ""}, SESSION) is None
        assert "no email on file" in caplog.text
        assert SESSION in caplog.text
        # The old fallback env var is not consulted at all any more.
        assert "operator@wustl.edu" not in caplog.text

    def test_owner_with_an_email_is_not_flagged(self, monkeypatch, caplog):
        monkeypatch.delenv("TWAIN_NOTIFY_BACKEND", raising=False)
        with caplog.at_level("WARNING", logger="twain.runner.notify"):
            default_notifier(SESSION, "completed", "done", recipient={"email": "r@wustl.edu"})
        assert "no email on file" not in caplog.text

    def test_no_recipient_is_not_fatal(self, monkeypatch, caplog):
        monkeypatch.delenv("TWAIN_NOTIFY_BACKEND", raising=False)
        with caplog.at_level("INFO", logger="twain.runner.notify"):
            default_notifier(SESSION, "input", "Q", recipient=None)
        assert "<no recipient>" in caplog.text


# ── SES backend: the owner's address, or nothing ───────────────────────────────
class TestSesBackend:
    def test_emails_the_run_owner(self, monkeypatch, fake_boto3):
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "ses")
        default_notifier(
            SESSION, "input", "Which solvent?",
            recipient={"email": "owner@wustl.edu"},
        )
        assert fake_boto3.emails[0]["Destination"]["ToAddresses"] == ["owner@wustl.edu"]

    def test_owner_without_an_address_gets_no_email(self, monkeypatch, fake_boto3):
        # Even with the old fallback address configured: no owner address, no send.
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "ses")
        monkeypatch.setenv("TWAIN_NOTIFY_EMAIL", "operator@wustl.edu")
        default_notifier(SESSION, "input", "Q", recipient={"email": None})
        assert fake_boto3.emails == []

    def test_unknown_owner_gets_no_email(self, monkeypatch, fake_boto3):
        # An owner lookup that failed (recipient=None) sends nothing, never raises.
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "ses")
        default_notifier(SESSION, "input", "Q", recipient=None)
        assert fake_boto3.emails == []


# ── SNS backend: the owner's phone, or nothing ─────────────────────────────────
class TestSnsBackend:
    def test_texts_the_owner_phone_directly(self, monkeypatch, fake_boto3):
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "sns")
        default_notifier(
            SESSION, "approval", "Plan ready",
            recipient={"phone": "+13145550123"},
        )
        assert fake_boto3.published[0]["PhoneNumber"] == "+13145550123"
        assert "TopicArn" not in fake_boto3.published[0]

    def test_owner_without_a_phone_gets_no_text(self, monkeypatch, fake_boto3, caplog):
        # A shared topic is the same wrong-recipient problem as a shared inbox:
        # the fan-out is gone, so an owner with no phone is simply not texted.
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "sns")
        monkeypatch.setenv("TWAIN_NOTIFY_SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:1:t")
        with caplog.at_level("WARNING", logger="twain.runner.notify"):
            default_notifier(SESSION, "input", "Q", recipient={"phone": None})
        assert fake_boto3.published == []
        assert "no phone on file" in caplog.text


# ── SendGrid backend: POST to the owner via the SendGrid HTTP API ─────────────
class TestSendGridBackend:
    def _no_env_file(self, monkeypatch):
        # Don't let a real repo .env leak into the test's env resolution.
        monkeypatch.setattr(notifications, "_ensure_env_loaded", lambda: None)

    def test_sends_to_owner_with_bearer_key(self, monkeypatch):
        self._no_env_file(monkeypatch)
        captured = {}

        class FakeResp:
            status = 202

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.headers["Authorization"]
            captured["body"] = json.loads(request.data.decode())
            return FakeResp()

        monkeypatch.setenv("TWAIN_SENDGRID_API_KEY", "SG.secret")
        monkeypatch.setenv("TWAIN_NOTIFY_FROM", "twain@twain.dev")
        monkeypatch.setattr(notifications.urllib.request, "urlopen", fake_urlopen)

        notifications._notify_sendgrid(
            SESSION, "approval", "Your plan is ready.",
            recipient={"email": "owner@wustl.edu"},
        )

        assert captured["url"] == notifications.SENDGRID_API_URL
        assert captured["auth"] == "Bearer SG.secret"
        assert captured["body"]["personalizations"][0]["to"][0]["email"] == "owner@wustl.edu"
        assert captured["body"]["from"]["email"] == "twain@twain.dev"
        assert "waiting for your approval" in captured["body"]["subject"]

    def test_owner_without_an_address_is_not_redirected(self, monkeypatch, caplog):
        # The flood landed in the wrong inbox because a blank users.email fell
        # back to the operator address. Nothing is sent now, whatever is
        # configured, and the withheld run is named in the log.
        self._no_env_file(monkeypatch)
        monkeypatch.setenv("TWAIN_SENDGRID_API_KEY", "SG.secret")
        monkeypatch.setenv("TWAIN_NOTIFY_FROM", "twain@twain.dev")
        monkeypatch.setenv("TWAIN_NOTIFY_EMAIL", "operator@wustl.edu")
        posted = []
        monkeypatch.setattr(notifications, "_sendgrid_post", lambda *a, **k: posted.append(a))
        with caplog.at_level("WARNING", logger="twain.runner.notify"):
            notifications._notify_sendgrid(SESSION, "input", "Q", recipient={"email": None})
        assert posted == []
        assert "no email on file" in caplog.text

    def test_dispatch_via_default_notifier(self, monkeypatch):
        """backend=sendgrid routes default_notifier to the SendGrid path, recipient included."""
        called = {}
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "sendgrid")
        monkeypatch.setattr(
            notifications, "_notify_sendgrid",
            lambda sid, reason, msg, recipient=None, request=None: called.update(
                sid=sid, reason=reason, recipient=recipient
            ),
        )
        default_notifier(SESSION, "input", "Q", recipient={"email": "owner@wustl.edu"})
        assert called == {
            "sid": SESSION, "reason": "input", "recipient": {"email": "owner@wustl.edu"}
        }

    def test_requires_api_key(self, monkeypatch):
        self._no_env_file(monkeypatch)
        monkeypatch.delenv("TWAIN_SENDGRID_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="TWAIN_SENDGRID_API_KEY"):
            notifications._notify_sendgrid(
                SESSION, "approval", "x", recipient={"email": "owner@wustl.edu"}
            )

    def test_requires_verified_sender(self, monkeypatch):
        self._no_env_file(monkeypatch)
        monkeypatch.setenv("TWAIN_SENDGRID_API_KEY", "SG.secret")
        monkeypatch.delenv("TWAIN_NOTIFY_FROM", raising=False)
        with pytest.raises(RuntimeError, match="TWAIN_NOTIFY_FROM"):
            notifications._notify_sendgrid(
                SESSION, "approval", "x", recipient={"email": "owner@wustl.edu"}
            )

    def test_no_recipient_is_skipped_not_raised(self, monkeypatch):
        self._no_env_file(monkeypatch)
        monkeypatch.setenv("TWAIN_SENDGRID_API_KEY", "SG.secret")
        monkeypatch.setenv("TWAIN_NOTIFY_FROM", "twain@twain.dev")
        monkeypatch.delenv("TWAIN_NOTIFY_EMAIL", raising=False)
        sent = []
        monkeypatch.setattr(
            notifications, "_sendgrid_post", lambda *a, **k: sent.append(a)
        )
        # No owner email and no configured default: skipped, not raised, no POST.
        notifications._notify_sendgrid(SESSION, "input", "Q", recipient=None)
        assert sent == []

    def test_http_rejection_reports_sendgrids_own_explanation(self, monkeypatch):
        # urlopen raises on 4xx, so the status check never sees a 403; without
        # reading the body the log said only "Forbidden", which doesn't say
        # whether the key or the sender identity is wrong.
        self._no_env_file(monkeypatch)
        monkeypatch.setenv("TWAIN_SENDGRID_API_KEY", "SG.secret")
        monkeypatch.setenv("TWAIN_NOTIFY_FROM", "twain@twain.dev")

        def forbidden(request, timeout=None):
            raise notifications.urllib.error.HTTPError(
                notifications.SENDGRID_API_URL, 403, "Forbidden", {},
                io.BytesIO(b'{"errors":[{"message":"The from address does not match a '
                           b'verified Sender Identity."}]}'),
            )

        monkeypatch.setattr(notifications.urllib.request, "urlopen", forbidden)
        with pytest.raises(RuntimeError, match="verified Sender Identity"):
            notifications._notify_sendgrid(
                SESSION, "approval", "x", recipient={"email": "owner@wustl.edu"}
            )

    def test_network_failure_names_the_cause(self, monkeypatch):
        self._no_env_file(monkeypatch)
        monkeypatch.setenv("TWAIN_SENDGRID_API_KEY", "SG.secret")
        monkeypatch.setenv("TWAIN_NOTIFY_FROM", "twain@twain.dev")

        def unreachable(request, timeout=None):
            raise notifications.urllib.error.URLError("name resolution failed")

        monkeypatch.setattr(notifications.urllib.request, "urlopen", unreachable)
        with pytest.raises(RuntimeError, match="unreachable"):
            notifications._notify_sendgrid(
                SESSION, "input", "x", recipient={"email": "owner@wustl.edu"}
            )

    def test_accepted_send_is_logged(self, monkeypatch, caplog):
        """The one positive record that the email path actually worked."""
        self._no_env_file(monkeypatch)
        monkeypatch.setenv("TWAIN_SENDGRID_API_KEY", "SG.secret")
        monkeypatch.setenv("TWAIN_NOTIFY_FROM", "twain@twain.dev")
        monkeypatch.setattr(notifications, "_sendgrid_post", lambda *a, **k: None)
        with caplog.at_level("INFO", logger="twain.runner.notify"):
            notifications._notify_sendgrid(
                SESSION, "completed", "done", recipient={"email": "owner@wustl.edu"}
            )
        assert "SendGrid accepted 'completed'" in caplog.text
        assert "owner@wustl.edu" in caplog.text

    def test_notify_failure_never_propagates(self, monkeypatch):
        """A SendGrid outage must not fail (or unpause) the run."""
        monkeypatch.setenv("TWAIN_NOTIFY_BACKEND", "sendgrid")

        def boom(*a, **k):
            raise RuntimeError("sendgrid is down")

        monkeypatch.setattr(notifications, "_notify_sendgrid", boom)
        # Swallowed by default_notifier's best-effort guard — no exception here.
        default_notifier(SESSION, "approval", "x", recipient={"email": "o@wustl.edu"})
