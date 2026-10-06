"""Tests for the RIS API webhook receiver (ris_webhooks.py + POST /api/ris/webhooks).

Signature checks run against the Standard Webhooks reference vector, so a
receiver that passes here verifies what any conforming signer -- ris-api
included -- sends. The DB write is faked; the SQL itself is migration 012.
"""
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

import ris_webhooks
from main import app

# Standard Webhooks reference vector (standard-webhooks spec test suite).
REF_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"  # gitleaks:allow -- the spec's public test vector
REF_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
REF_TS = 1614265330
REF_BODY = b'{"test": 2432232314}'
REF_SIG = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def _headers(msg_id=REF_ID, ts=REF_TS, sig=REF_SIG):
    return {"webhook-id": msg_id, "webhook-timestamp": str(ts), "webhook-signature": sig}


def _sign(secret, msg_id, ts, body: bytes) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


# ── verify ─────────────────────────────────────────────────────────────────────

class TestVerify:
    def test_accepts_the_reference_vector(self):
        ris_webhooks.verify(REF_SECRET, _headers(), REF_BODY, now=REF_TS)

    def test_accepts_when_any_listed_signature_matches(self):
        # Secret rotation: a signer may send old and new signatures together.
        sigs = f"v1,bm90LXRoZS1yaWdodC1vbmU= {REF_SIG}"
        ris_webhooks.verify(REF_SECRET, _headers(sig=sigs), REF_BODY, now=REF_TS)

    @pytest.mark.parametrize("headers, body", [
        (_headers(sig="v1,Zm9yZ2VkLXNpZ25hdHVyZQ=="), REF_BODY),   # forged
        (_headers(), b'{"test": 2432232315}'),                     # tampered body
        (_headers(msg_id="msg_other"), REF_BODY),                  # id swapped
        (_headers(sig=REF_SIG.replace("v1,", "v2,")), REF_BODY),   # unknown version
    ])
    def test_refuses_a_bad_signature(self, headers, body):
        with pytest.raises(ris_webhooks.WebhookError) as info:
            ris_webhooks.verify(REF_SECRET, headers, body, now=REF_TS)
        assert info.value.status == 401

    @pytest.mark.parametrize("skew", [-301, 301])
    def test_refuses_a_stale_or_future_timestamp(self, skew):
        with pytest.raises(ris_webhooks.WebhookError, match="tolerance"):
            ris_webhooks.verify(REF_SECRET, _headers(), REF_BODY, now=REF_TS + skew)

    @pytest.mark.parametrize("missing", ["webhook-id", "webhook-timestamp", "webhook-signature"])
    def test_refuses_missing_headers(self, missing):
        headers = _headers()
        del headers[missing]
        with pytest.raises(ris_webhooks.WebhookError) as info:
            ris_webhooks.verify(REF_SECRET, headers, REF_BODY, now=REF_TS)
        assert info.value.status == 401

    def test_a_malformed_secret_is_a_server_problem_not_the_senders(self):
        with pytest.raises(ris_webhooks.WebhookError) as info:
            ris_webhooks.verify("whsec_***not-base64***", _headers(), REF_BODY, now=REF_TS)
        assert info.value.status == 503


class TestSecret:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("RIS_WEBHOOK_SECRET", REF_SECRET + "\n")
        assert ris_webhooks.webhook_secret() == REF_SECRET

    def test_unconfigured_is_503_so_ris_api_keeps_retrying(self, monkeypatch):
        monkeypatch.delenv("RIS_WEBHOOK_SECRET", raising=False)

        def boom(_secret_id):
            raise RuntimeError("no such secret")
        monkeypatch.setattr(ris_webhooks, "read_secret", boom)
        with pytest.raises(ris_webhooks.WebhookError) as info:
            ris_webhooks.webhook_secret()
        assert info.value.status == 503


# ── the route ──────────────────────────────────────────────────────────────────

EVENT = {
    "data": {"attempt": 1, "ended_at": "2026-10-05T17:02:11+00:00", "exit_code": "0:0",
             "job_id": "3186130", "job_name": "twain-sess1", "previous_state": "RUNNING",
             "state": "COMPLETED", "state_reason": None},
    "timestamp": "2026-10-05T17:03:40.118+00:00",
    "type": "job.completed",
}


@pytest.fixture
def route(monkeypatch):
    """The route with the reference secret and an in-memory event store."""
    monkeypatch.setenv("RIS_WEBHOOK_SECRET", REF_SECRET)
    stored = {}

    def record(webhook_id, event):
        if webhook_id in stored:
            return False
        stored[webhook_id] = event
        return True
    monkeypatch.setattr(ris_webhooks, "record_event", record)
    return TestClient(app), stored


def _post(client, body: bytes, msg_id="msg_1", ts=None, sig=None):
    ts = int(time.time()) if ts is None else ts
    sig = sig or _sign(REF_SECRET, msg_id, ts, body)
    return client.post("/api/ris/webhooks", content=body, headers={
        **_headers(msg_id=msg_id, ts=ts, sig=sig), "content-type": "application/json"})


def test_a_signed_event_is_recorded(route):
    client, stored = route
    body = json.dumps(EVENT, separators=(",", ":"), sort_keys=True).encode()
    resp = _post(client, body)
    assert resp.status_code == 204
    assert stored["msg_1"]["data"]["job_id"] == "3186130"


def test_a_redelivery_is_acknowledged_but_not_stored_twice(route):
    client, stored = route
    body = json.dumps(EVENT).encode()
    assert _post(client, body).status_code == 204
    assert _post(client, body).status_code == 204
    assert len(stored) == 1


def test_a_forged_event_is_refused_and_not_stored(route):
    client, stored = route
    resp = _post(client, json.dumps(EVENT).encode(), sig="v1,Zm9yZ2VkLXNpZ25hdHVyZQ==")
    assert resp.status_code == 401
    assert stored == {}


def test_a_signed_body_that_is_not_an_event_is_400(route):
    client, stored = route
    assert _post(client, b"[1, 2, 3]").status_code == 400
    assert stored == {}


def test_an_oversized_body_is_refused_before_verification(route):
    client, stored = route
    resp = _post(client, b"x" * (70 * 1024))
    assert resp.status_code == 413
    assert stored == {}


def test_the_test_event_is_accepted(route):
    # `POST /webhooks/{id}/test` in ris-api sends webhook.test with no job id.
    client, stored = route
    body = json.dumps({"data": {}, "timestamp": "2026-10-05T00:00:00+00:00",
                       "type": "webhook.test"}).encode()
    assert _post(client, body, msg_id="msg_test").status_code == 204
    assert stored["msg_test"]["type"] == "webhook.test"
