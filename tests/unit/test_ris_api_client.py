"""Unit tests for the RIS API HTTP client (module 08).

All HTTP goes through an injected fake session, so these tests exercise the
real request-building / error-handling logic fully offline -- no network, no
live RIS API.

Run from the repo root with:  pixi run pytest tests/unit/test_ris_api_client.py
"""
import pytest
import requests
from execution_adapter.ris_api_client import DEFAULT_BASE_URL, RisApiClient, RisApiError

# ── fixtures ──────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeSession:
    """Records calls and returns a scripted response keyed by (method, path)."""

    def __init__(self, responses):
        self.responses = responses  # {(method, path): FakeResponse}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = url[len(DEFAULT_BASE_URL):]
        return self.responses[(method, path)]


class RaisingSession:
    def request(self, method, url, **kwargs):
        raise requests.ConnectionError("connection refused")


def _client(responses):
    return RisApiClient(token="pat-123", session=FakeSession(responses))


# ── construction ────────────────────────────────────────────────────────────

def test_missing_token_raises_lazily_on_first_call_not_at_construction(monkeypatch):
    # A developer's .env (loaded by other modules' load_dotenv) may set one.
    monkeypatch.delenv("RIS_API_TOKEN", raising=False)
    # Constructing a client (or an adapter that holds one) shouldn't require a
    # token up front -- only actually calling the API should.
    client = RisApiClient(token=None, session=FakeSession({}))
    with pytest.raises(RisApiError, match="RIS_API_TOKEN"):
        client.get_job("42")


def test_token_from_env(monkeypatch):
    monkeypatch.setenv("RIS_API_TOKEN", "pat-from-env")
    client = RisApiClient(session=FakeSession({}))
    assert client._headers()["Authorization"] == "Bearer pat-from-env"


def test_base_url_defaults_and_strips_trailing_slash():
    client = RisApiClient(token="t", session=FakeSession({}), base_url=DEFAULT_BASE_URL + "/")
    assert client.base_url == DEFAULT_BASE_URL


# ── jobs ──────────────────────────────────────────────────────────────────────

def test_submit_job_returns_job_id_and_sends_bearer_auth():
    session = FakeSession({("POST", "/jobs"): FakeResponse(201, {"job_id": "42"})})
    client = RisApiClient(token="pat-123", session=session)
    job_id = client.submit_job({"job_name": "twain-x"})
    assert job_id == "42"
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url == DEFAULT_BASE_URL + "/jobs"
    assert kwargs["headers"]["Authorization"] == "Bearer pat-123"
    assert kwargs["json"] == {"job_name": "twain-x"}


def test_submit_job_sends_idempotency_key_header_when_given():
    session = FakeSession({("POST", "/jobs"): FakeResponse(201, {"job_id": "42"})})
    client = RisApiClient(token="t", session=session)
    client.submit_job({"job_name": "x"}, idempotency_key="run-7")
    _, _, kwargs = session.calls[0]
    assert kwargs["headers"]["Idempotency-Key"] == "run-7"


def test_submit_job_omits_idempotency_key_header_when_not_given():
    session = FakeSession({("POST", "/jobs"): FakeResponse(201, {"job_id": "42"})})
    client = RisApiClient(token="t", session=session)
    client.submit_job({"job_name": "x"})
    _, _, kwargs = session.calls[0]
    assert "Idempotency-Key" not in kwargs["headers"]


def test_get_job_returns_parsed_body():
    detail = {"job_id": "42", "state": "RUNNING"}
    client = _client({("GET", "/jobs/42"): FakeResponse(200, detail)})
    assert client.get_job("42") == detail


def test_cancel_job_sends_delete_with_no_body_by_default():
    session = FakeSession({("DELETE", "/jobs/42"): FakeResponse(204, text="")})
    client = RisApiClient(token="t", session=session)
    client.cancel_job("42")
    method, _url, kwargs = session.calls[0]
    assert method == "DELETE" and kwargs["json"] is None


def test_cancel_job_sends_signal_when_given():
    session = FakeSession({("DELETE", "/jobs/42"): FakeResponse(204, text="")})
    client = RisApiClient(token="t", session=session)
    client.cancel_job("42", signal="KILL")
    _, _, kwargs = session.calls[0]
    assert kwargs["json"] == {"signal": "KILL"}


def test_accounting_returns_parsed_body():
    body = {"job_id": "42", "state": "COMPLETED", "exit_code": "0:0",
            "elapsed": "00:05:00", "max_rss": "512000K"}
    client = _client({("GET", "/jobs/42/accounting"): FakeResponse(200, body)})
    assert client.accounting("42") == body


def test_stdout_returns_content_string():
    body = {"job_id": "42", "stream": "stdout", "content": "hello\n"}
    client = _client({("GET", "/jobs/42/stdout"): FakeResponse(200, body)})
    assert client.stdout("42") == "hello\n"


def test_stderr_returns_content_string():
    body = {"job_id": "42", "stream": "stderr", "content": "oops\n"}
    client = _client({("GET", "/jobs/42/stderr"): FakeResponse(200, body)})
    assert client.stderr("42") == "oops\n"


def test_preview_job_returns_parsed_body():
    body = {"can_start": True, "message": "ok"}
    client = _client({("POST", "/jobs/preview"): FakeResponse(200, body)})
    assert client.preview_job({"job_name": "x"}) == body


# ── error handling ──────────────────────────────────────────────────────────

def test_non_2xx_raises_ris_api_error_with_detail_field():
    body = {"detail": "invalid partition"}
    client = _client({("GET", "/jobs/42"): FakeResponse(422, body, text='{"detail": "invalid partition"}')})
    with pytest.raises(RisApiError, match="invalid partition"):
        client.get_job("42")


def test_non_2xx_falls_back_to_raw_text_when_body_isnt_json():
    client = _client({("GET", "/jobs/42"): FakeResponse(500, None, text="internal error")})
    with pytest.raises(RisApiError, match="internal error"):
        client.get_job("42")


def test_network_error_raises_ris_api_error():
    client = RisApiClient(token="t", session=RaisingSession())
    with pytest.raises(RisApiError, match="connection refused"):
        client.get_job("42")


def test_error_status_drives_transient_classification():
    assert RisApiError("net").transient
    assert RisApiError("x", status=429).transient
    assert RisApiError("x", status=503).transient
    assert not RisApiError("x", status=422).transient
    assert not RisApiError("x", status=401).transient


def test_http_error_carries_its_status_code():
    client = _client({("POST", "/jobs"): FakeResponse(503, text="unavailable")})
    with pytest.raises(RisApiError) as info:
        client.submit_job({"job_name": "x"})
    assert info.value.status == 503 and info.value.transient


def test_output_tail_reads_the_last_bytes_of_a_stream():
    session = FakeSession({("GET", "/jobs/7/output/stderr"): FakeResponse(
        200, {"job_id": "7", "stream": "stderr", "content": "Traceback ...",
              "offset": 10, "next_offset": 23, "size": 23, "range_end": 23,
              "eof": True, "attempt": None, "job_finished": True})})
    client = RisApiClient(token="t", session=session)

    assert client.output_tail("7", "stderr", 4096) == "Traceback ..."
    assert session.calls[0][2]["params"] == {"tail": 4096, "limit": 4096}



# ── malformed bodies + the ris-api error envelope (#152) ───────────────────────

def test_a_2xx_html_page_raises_a_transient_ris_api_error():
    client = _client({("GET", "/jobs/42"): FakeResponse(
        200, None, text="<html>502 Bad Gateway</html>")})
    with pytest.raises(RisApiError, match="unexpected body") as info:
        client.get_job("42")
    assert info.value.transient


def test_a_submit_reply_without_job_id_raises_ris_api_error():
    client = _client({("POST", "/jobs"): FakeResponse(201, {"id": "x"})})
    with pytest.raises(RisApiError, match="job_id"):
        client.submit_job({"job_name": "x"})


def test_a_2xx_list_body_raises_ris_api_error():
    client = _client({("GET", "/jobs/42/stdout"): FakeResponse(200, ["oops"])})
    with pytest.raises(RisApiError, match="unexpected body"):
        client.stdout("42")


def test_error_envelope_message_code_and_correlation_id():
    body = {"error": {"code": "VALIDATION_ERROR", "message": "partition is invalid",
                      "correlation_id": "abc-123", "details": {}}}
    client = _client({("POST", "/jobs"): FakeResponse(422, body, text="{...}")})
    with pytest.raises(RisApiError) as info:
        client.submit_job({"job_name": "x"})
    err = info.value
    assert "partition is invalid" in str(err) and "abc-123" in str(err)
    assert err.code == "VALIDATION_ERROR" and err.status == 422 and not err.transient


def test_error_with_a_list_body_falls_back_to_text():
    client = _client({("GET", "/jobs/42"): FakeResponse(400, ["bad"], text='["bad"]')})
    with pytest.raises(RisApiError, match=r'\["bad"\]'):
        client.get_job("42")


def test_auth_failure_points_at_the_token():
    body = {"error": {"code": "UNAUTHORIZED", "message": "token expired"}}
    client = _client({("GET", "/jobs/42"): FakeResponse(401, body, text="{...}")})
    with pytest.raises(RisApiError, match="RIS_API_TOKEN") as info:
        client.get_job("42")
    assert info.value.auth_failure
