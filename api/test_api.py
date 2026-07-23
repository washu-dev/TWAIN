import pytest
from fastapi.testclient import TestClient

from main import _extract_result, app

client = TestClient(app)


class TestHealthEndpoint:
    def test_health_check_returns_ok(self):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestEndpointAccess:
    def test_health_always_accessible(self):
        response = client.get("/api/health")
        assert response.status_code == 200


class TestExtractResult:
    def test_pulls_last_json_line_from_stdout(self):
        stdout = (
            "Composition: N2O\n"
            "band_gap (eV): 6.7296\n"
            '{"property": "band_gap", "band_gap": 6.7296, "band_gap_unit": "eV"}\n'
        )
        result = _extract_result({"stdout": stdout})
        assert result == {"property": "band_gap", "band_gap": 6.7296, "band_gap_unit": "eV"}

    def test_none_when_no_json_line(self):
        assert _extract_result({"stdout": "just logs, no json\n"}) is None

    def test_none_for_non_dict_or_missing_stdout(self):
        assert _extract_result(None) is None
        assert _extract_result("raw text") is None
        assert _extract_result({}) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
