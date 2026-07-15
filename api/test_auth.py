from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import database
from auth import _identity_from_claims, get_current_user, require_admin
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
