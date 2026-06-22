from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)

MOCK_GREETINGS = [
    {"message": "Hello, TWAIN!"},
    {"message": "Welcome to the API"},
]


class TestHealthEndpoint:
    def test_health_check_returns_ok(self):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestGreetingsEndpoint:
    @patch("main.query_greetings", return_value=MOCK_GREETINGS)
    def test_greetings_endpoint_returns_200(self, _mock):
        response = client.get("/api/greetings")
        assert response.status_code == 200

    @patch("main.query_greetings", return_value=MOCK_GREETINGS)
    def test_greetings_endpoint_response_structure(self, _mock):
        response = client.get("/api/greetings")
        data = response.json()
        assert "data" in data
        assert "count" in data
        assert "message" in data

    @patch("main.query_greetings", return_value=MOCK_GREETINGS)
    def test_greetings_endpoint_data_is_list(self, _mock):
        response = client.get("/api/greetings")
        assert isinstance(response.json()["data"], list)

    @patch("main.query_greetings", return_value=MOCK_GREETINGS)
    def test_greetings_count_matches_data_length(self, _mock):
        response = client.get("/api/greetings")
        data = response.json()
        assert data["count"] == len(data["data"])

    @patch("main.query_greetings", return_value=MOCK_GREETINGS)
    def test_greetings_items_have_message_field(self, _mock):
        response = client.get("/api/greetings")
        for item in response.json()["data"]:
            assert "message" in item

    @patch("main.query_greetings", return_value=[])
    def test_greetings_empty_returns_200(self, _mock):
        response = client.get("/api/greetings")
        assert response.status_code == 200
        assert response.json()["data"] == []

    @patch("main.query_greetings", side_effect=Exception("DB unavailable"))
    def test_greetings_db_error_returns_500(self, _mock):
        response = client.get("/api/greetings")
        assert response.status_code == 500
        assert "error" in response.json()


class TestEndpointAccess:
    def test_health_always_accessible(self):
        response = client.get("/api/health")
        assert response.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
