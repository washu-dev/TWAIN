import pytest
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


class TestHealthEndpoint:
    """Tests for the health check endpoint."""

    def test_health_check_returns_ok(self):
        """Test that /api/health returns status ok."""
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestGreetingsEndpoint:
    """Tests for the /api/greetings endpoint."""

    def test_greetings_endpoint_returns_200(self):
        """Test that /api/greetings returns HTTP 200."""
        response = client.get("/api/greetings")
        assert response.status_code == 200

    def test_greetings_endpoint_response_structure(self):
        """Test that /api/greetings returns expected JSON structure."""
        response = client.get("/api/greetings")
        data = response.json()

        assert "data" in data
        assert "count" in data
        assert "message" in data

    def test_greetings_endpoint_data_is_list(self):
        """Test that greetings data is a list."""
        response = client.get("/api/greetings")
        data = response.json()

        assert isinstance(data["data"], list)

    def test_greetings_endpoint_count_matches_data_length(self):
        """Test that count field matches the length of data."""
        response = client.get("/api/greetings")
        data = response.json()

        assert data["count"] == len(data["data"])

    def test_greetings_endpoint_empty_response(self):
        """Test that endpoint handles empty greetings gracefully."""
        response = client.get("/api/greetings")
        data = response.json()

        # Should return 200 even if empty
        assert response.status_code == 200
        assert data["count"] == 0 or len(data["data"]) > 0

    def test_greetings_response_has_message_field_in_items(self):
        """Test that greeting items contain 'message' field (if data exists)."""
        response = client.get("/api/greetings")
        data = response.json()

        if data["data"]:  # Only test if there are greetings
            for greeting in data["data"]:
                assert "message" in greeting


class TestEndpointAccess:
    """Tests for endpoint accessibility."""

    def test_greetings_endpoint_accessible(self):
        """Test that /api/greetings is accessible."""
        response = client.get("/api/greetings")
        assert response.status_code in [200, 500]  # Either success or DB connection error

    def test_health_endpoint_always_accessible(self):
        """Test that /api/health is always accessible."""
        response = client.get("/api/health")
        assert response.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
