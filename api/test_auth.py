from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import auth
import database
from auth import (
    _identity_from_claims,
    email_allowed,
    get_current_user,
    mint_interim_token,
    require_admin,
    verify_interim_token,
)
from main import app

client = TestClient(app)

REGULAR_USER = {
    "id": "u1", "subject": "s1", "email": "user@wustl.edu",
    "name": "Reg User", "role": "user",
}
ADMIN_USER = {
    "id": "a1", "subject": "s2", "email": "admin@wustl.edu",
    "name": "Admin", "role": "admin",
}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


# ── Fake DB connection so database.* bodies run without a real Postgres ────────
class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._cursor = _FakeCursor(rows)

    def cursor(self, **_kwargs):
        return self._cursor

    def commit(self):
        pass

    def close(self):
        pass


class TestIdentity:
    def test_prefers_oid_and_lowercases_email(self):
        sub, email, name = _identity_from_claims(
            {"oid": "abc", "preferred_username": "A@WUSTL.EDU", "name": "Alice"}
        )
        assert sub == "abc"
        assert email == "a@wustl.edu"
        assert name == "Alice"

    def test_falls_back_to_sub_and_email(self):
        sub, email, _ = _identity_from_claims({"sub": "xyz", "email": "b@wustl.edu"})
        assert sub == "xyz"
        assert email == "b@wustl.edu"


class TestRequireAdmin:
    def test_blocks_regular_user(self):
        with pytest.raises(HTTPException) as exc:
            require_admin(REGULAR_USER)
        assert exc.value.status_code == 403

    def test_allows_admin(self):
        assert require_admin(ADMIN_USER) is ADMIN_USER


class TestMeEndpoint:
    def test_returns_current_user(self):
        app.dependency_overrides[get_current_user] = lambda: REGULAR_USER
        response = client.get("/api/me")
        assert response.status_code == 200
        assert response.json()["data"]["email"] == "user@wustl.edu"

    def test_requires_a_token(self):
        response = client.get("/api/me")
        assert response.status_code == 401


class TestNotifyPrefsEndpoint:
    @patch("main.set_notify_prefs",
           return_value={"enabled": True, "kinds": {"completed": False}})
    def test_put_replaces_the_callers_prefs(self, mock_set):
        app.dependency_overrides[get_current_user] = lambda: REGULAR_USER
        response = client.put(
            "/api/me/notifications",
            json={"enabled": True, "kinds": {"completed": False}},
        )
        assert response.status_code == 200
        assert response.json()["data"]["kinds"] == {"completed": False}
        user_id, prefs = mock_set.call_args.args
        assert user_id == "u1"
        assert prefs == {"enabled": True, "kinds": {"completed": False}}

    def test_unknown_kind_is_rejected(self):
        # The kinds must match what the runner actually emails about, or a typo
        # silently opts out of nothing.
        app.dependency_overrides[get_current_user] = lambda: REGULAR_USER
        response = client.put(
            "/api/me/notifications",
            json={"enabled": True, "kinds": {"pager": False}},
        )
        assert response.status_code == 422
        assert "pager" in response.json()["detail"]

    def test_requires_a_token(self):
        response = client.put("/api/me/notifications", json={"enabled": True})
        assert response.status_code == 401


class TestAdminEndpoints:
    def test_list_users_forbidden_for_regular(self):
        app.dependency_overrides[get_current_user] = lambda: REGULAR_USER
        response = client.get("/api/admin/users")
        assert response.status_code == 403

    @patch("main.list_users", return_value=[REGULAR_USER, ADMIN_USER])
    def test_list_users_ok_for_admin(self, _mock):
        app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        response = client.get("/api/admin/users")
        assert response.status_code == 200
        assert response.json()["count"] == 2

    @patch("main.set_user_role", return_value={**REGULAR_USER, "role": "admin"})
    def test_set_role_ok(self, mock_set):
        app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        response = client.patch("/api/admin/users/u1/role", json={"role": "admin"})
        assert response.status_code == 200
        assert response.json()["data"]["role"] == "admin"
        mock_set.assert_called_once_with("u1", "admin")

    @patch("main.set_user_role", return_value=None)
    def test_set_role_missing_user_404(self, _mock):
        app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        response = client.patch("/api/admin/users/nope/role", json={"role": "admin"})
        assert response.status_code == 404

    def test_set_role_forbidden_for_regular(self):
        app.dependency_overrides[get_current_user] = lambda: REGULAR_USER
        response = client.patch("/api/admin/users/u1/role", json={"role": "admin"})
        assert response.status_code == 403

    def test_set_role_rejects_invalid_role(self):
        app.dependency_overrides[get_current_user] = lambda: ADMIN_USER
        response = client.patch("/api/admin/users/u1/role", json={"role": "root"})
        assert response.status_code == 422


class TestUserCrud:
    @patch("database.get_connection")
    def test_upsert_user_returns_row(self, mock_conn):
        row = {**REGULAR_USER, "created_at": None, "last_login_at": None}
        mock_conn.return_value = _FakeConn([row])
        out = database.upsert_user("s1", "user@wustl.edu", "Reg User")
        assert out["subject"] == "s1"

    @patch("database.get_connection")
    def test_upsert_admin_bootstrap_sets_admin_role_param(self, mock_conn):
        conn = _FakeConn([ADMIN_USER])
        mock_conn.return_value = conn
        database.upsert_user("s2", "admin@wustl.edu", "Admin", bootstrap_admin=True)
        # the role param passed to INSERT should be 'admin'
        _sql, params = conn._cursor.executed[0]
        assert params[3] == "admin"

    @patch("database.get_connection")
    def test_list_users(self, mock_conn):
        mock_conn.return_value = _FakeConn([REGULAR_USER, ADMIN_USER])
        assert len(database.list_users()) == 2

    @patch("database.get_connection")
    def test_set_user_role(self, mock_conn):
        mock_conn.return_value = _FakeConn([{**REGULAR_USER, "role": "admin"}])
        assert database.set_user_role("u1", "admin")["role"] == "admin"

    def test_set_user_role_rejects_bad_role(self):
        with pytest.raises(ValueError, match="role must be one of"):
            database.set_user_role("u1", "root")


# ── Interim email auth (pre-SSO) ──────────────────────────────────────────────
class TestInterimEmailPolicy:
    def test_allows_configured_domain(self):
        with patch.object(auth, "INTERIM_ALLOWED_DOMAINS", {"wustl.edu"}), \
             patch.object(auth, "INTERIM_ALLOWED_EMAILS", set()):
            assert email_allowed("Alice@WUSTL.EDU") is True
            assert email_allowed("bob@gmail.com") is False

    def test_allows_explicit_allowlist_email(self):
        with patch.object(auth, "INTERIM_ALLOWED_DOMAINS", set()), \
             patch.object(auth, "INTERIM_ALLOWED_EMAILS", {"ext@partner.org"}):
            assert email_allowed("ext@partner.org") is True
            assert email_allowed("other@partner.org") is False

    def test_rejects_malformed(self):
        assert email_allowed("not-an-email") is False
        assert email_allowed("") is False


class TestInterimToken:
    USER = {"subject": "interim:a@wustl.edu", "email": "a@wustl.edu", "role": "user"}

    def test_mint_and_verify_roundtrip(self):
        with patch.object(auth, "INTERIM_JWT_SECRET", "s3cret"):
            claims = verify_interim_token(mint_interim_token(self.USER))
        assert claims["sub"] == "interim:a@wustl.edu"
        assert claims["email"] == "a@wustl.edu"
        assert claims["role"] == "user"

    def test_verify_rejects_wrong_secret(self):
        with patch.object(auth, "INTERIM_JWT_SECRET", "s3cret"):
            token = mint_interim_token(self.USER)
        with patch.object(auth, "INTERIM_JWT_SECRET", "different"), \
             pytest.raises(HTTPException) as exc:
            verify_interim_token(token)
        assert exc.value.status_code == 401

    def test_verify_unconfigured_raises_401(self):
        with patch.object(auth, "INTERIM_JWT_SECRET", ""), \
             pytest.raises(HTTPException) as exc:
            verify_interim_token("whatever")
        assert exc.value.status_code == 401


class TestLoginEndpoint:
    def test_login_503_when_unconfigured(self):
        with patch.object(auth, "INTERIM_JWT_SECRET", ""):
            response = client.post("/api/auth/login", json={"email": "a@wustl.edu"})
        assert response.status_code == 503

    def test_login_403_when_email_not_allowed(self):
        with patch.object(auth, "INTERIM_JWT_SECRET", "s3cret"), \
             patch.object(auth, "INTERIM_ALLOWED_DOMAINS", {"wustl.edu"}), \
             patch.object(auth, "INTERIM_ALLOWED_EMAILS", set()):
            response = client.post("/api/auth/login", json={"email": "x@gmail.com"})
        assert response.status_code == 403

    @patch("main.upsert_user")
    def test_login_success_returns_token_and_user(self, mock_upsert):
        mock_upsert.return_value = {
            "id": "u1", "subject": "interim:a@wustl.edu",
            "email": "a@wustl.edu", "name": "a@wustl.edu", "role": "user",
        }
        with patch.object(auth, "INTERIM_JWT_SECRET", "s3cret"), \
             patch.object(auth, "INTERIM_ALLOWED_DOMAINS", {"wustl.edu"}):
            response = client.post("/api/auth/login", json={"email": "A@wustl.edu"})
        assert response.status_code == 200
        body = response.json()["data"]
        assert body["user"]["email"] == "a@wustl.edu"
        assert body["token"]
        assert mock_upsert.call_args.args[0] == "interim:a@wustl.edu"


class TestInterimTokenAcceptedByApi:
    @patch("auth.upsert_user")
    def test_me_accepts_interim_bearer_token(self, mock_upsert):
        user = {
            "id": "u1", "subject": "interim:a@wustl.edu",
            "email": "a@wustl.edu", "name": "a@wustl.edu", "role": "user",
        }
        mock_upsert.return_value = user
        with patch.object(auth, "INTERIM_JWT_SECRET", "s3cret"), \
             patch.object(auth, "AUTH_DISABLED", False):
            token = mint_interim_token(
                {"subject": "interim:a@wustl.edu", "email": "a@wustl.edu", "role": "user"}
            )
            response = client.get(
                "/api/me", headers={"Authorization": f"Bearer {token}"}
            )
        assert response.status_code == 200
        assert response.json()["data"]["email"] == "a@wustl.edu"


class TestEntraConfig:
    """Lazy resolution of the Entra tenant/audience: env-first, then Secrets Manager."""

    @staticmethod
    def _reset():
        auth._sso_config.cache_clear()

    def test_env_takes_precedence_and_skips_secrets(self, monkeypatch):
        self._reset()
        monkeypatch.setenv("ENTRA_TENANT_ID", "tenant-xyz")
        monkeypatch.setenv("ENTRA_API_AUDIENCE", "aud-abc")
        with patch("auth.get_secret", side_effect=AssertionError("must not read secrets")):
            assert auth._sso_config() == ("tenant-xyz", "aud-abc")
        self._reset()

    def test_falls_back_to_secrets_manager(self, monkeypatch):
        self._reset()
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_API_AUDIENCE", raising=False)
        secrets = {"TWAIN/sso/TENANT_ID": "t-sm ", "TWAIN/sso/APP_ID": " a-sm"}
        with patch("auth.get_secret", side_effect=lambda k: secrets[k]):
            assert auth._sso_config() == ("t-sm", "a-sm")  # values are stripped
        self._reset()

    def test_unconfigured_raises_500(self, monkeypatch):
        self._reset()
        monkeypatch.delenv("ENTRA_TENANT_ID", raising=False)
        monkeypatch.delenv("ENTRA_API_AUDIENCE", raising=False)
        with patch("auth.get_secret", side_effect=Exception("no aws")), \
             pytest.raises(HTTPException) as exc:
            auth._sso_config()
        assert exc.value.status_code == 500
        self._reset()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
