"""Job tickets: a Slurm job's only way to S3 (#170).

Presigning is exercised with a real boto3 client and dummy credentials --
signing is local, so these run offline -- and the DB is faked.
"""
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from botocore.config import Config
from fastapi.testclient import TestClient

import job_tickets
from main import app

client = TestClient(app)
TICKET = {"run_id": "f7945069", "attempt": 2, "s3_prefix": "runs/f7945069/attempt-2"}


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("TWAIN_RUN_BUCKET", "twain-run-data-test")
    return boto3.client("s3", region_name="us-east-1", aws_access_key_id="AKIDTEST",
                        aws_secret_access_key="secret", endpoint_url="https://s3.us-east-1.amazonaws.com",
                        config=Config(signature_version="s3v4"))


def _key_of(url):
    return urlparse(url).path.split("/", 2)[2]


class TestPresign:
    def test_input_get_and_output_put_are_scoped_to_the_attempt(self, s3):
        out = job_tickets.presign(TICKET, [
            {"name": "input/bundle.tar.gz", "method": "GET"},
            {"name": "output/outputs.tar.gz", "method": "PUT"}], client=s3)
        get, put = out["urls"]["input/bundle.tar.gz"], out["urls"]["output/outputs.tar.gz"]
        assert _key_of(get) == "runs/f7945069/attempt-2/input/bundle.tar.gz"
        assert _key_of(put) == "runs/f7945069/attempt-2/output/outputs.tar.gz"
        assert parse_qs(urlparse(put).query)["X-Amz-Expires"] == [str(job_tickets.URL_TTL_SECONDS)]
        assert urlparse(get).netloc.startswith("twain-run-data-test.") or "twain-run-data-test" in urlparse(get).path

    @pytest.mark.parametrize("obj, status", [
        ({"name": "input/bundle.tar.gz", "method": "PUT"}, 403),     # can't overwrite inputs
        ({"name": "output/x", "method": "GET"}, 403),                # GET reads input/ only
        ({"name": "input/../../other-run/x", "method": "GET"}, 400), # traversal
        ({"name": "/etc/passwd", "method": "GET"}, 400),
        ({"name": "secrets/key", "method": "GET"}, 400),             # outside input/ output/
        ({"name": "input/x", "method": "DELETE"}, 403),
    ])
    def test_refusals(self, s3, obj, status):
        with pytest.raises(job_tickets.TicketError) as info:
            job_tickets.presign(TICKET, [obj], client=s3)
        assert info.value.status == status

    def test_unconfigured_bucket_is_503(self, monkeypatch):
        monkeypatch.delenv("TWAIN_RUN_BUCKET", raising=False)
        with pytest.raises(job_tickets.TicketError) as info:
            job_tickets.presign(TICKET, [{"name": "input/b", "method": "GET"}], client=object())
        assert info.value.status == 503


class FakeCursor:
    def __init__(self, row):
        self.row, self.sql, self.params = row, None, None

    def execute(self, sql, params):
        self.sql, self.params = sql, params

    def fetchone(self):
        return self.row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, row):
        self.cur = FakeCursor(row)

    def cursor(self):
        return self.cur

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestRedeem:
    def test_only_the_hash_is_looked_up_and_expiry_is_enforced_in_sql(self, monkeypatch):
        conn = FakeConn(("run-1", 1, "runs/run-1/attempt-1"))
        monkeypatch.setattr(job_tickets, "get_connection", lambda: conn)
        assert job_tickets.redeem("tok-abc") == {
            "run_id": "run-1", "attempt": 1, "s3_prefix": "runs/run-1/attempt-1"}
        assert conn.cur.params == (job_tickets.token_hash("tok-abc"),)
        assert "tok-abc" not in str(conn.cur.params) and "expires_at > now()" in conn.cur.sql

    def test_unknown_or_expired_is_401(self, monkeypatch):
        monkeypatch.setattr(job_tickets, "get_connection", lambda: FakeConn(None))
        with pytest.raises(job_tickets.TicketError) as info:
            job_tickets.redeem("nope")
        assert info.value.status == 401

    def test_missing_token_never_reaches_the_db(self, monkeypatch):
        monkeypatch.setattr(job_tickets, "get_connection", lambda: pytest.fail("queried"))
        with pytest.raises(job_tickets.TicketError):
            job_tickets.redeem("")


class TestRoute:
    def test_ticket_header_buys_urls(self, monkeypatch, s3):
        monkeypatch.setattr(job_tickets, "redeem", lambda tok: TICKET if tok == "good" else None)
        monkeypatch.setattr(job_tickets, "_s3", lambda: s3)
        r = client.post("/api/job-tickets/urls", headers={"X-TWAIN-Ticket": "good"},
                        json={"objects": [{"name": "input/bundle.tar.gz", "method": "GET"}]})
        assert r.status_code == 200 and "input/bundle.tar.gz" in r.json()["urls"]

    def test_no_ticket_is_401_and_needs_no_user_login(self, monkeypatch):
        monkeypatch.setattr(job_tickets, "get_connection", lambda: FakeConn(None))
        r = client.post("/api/job-tickets/urls", json={"objects": []})
        assert r.status_code == 401
