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

    # ── a pretty-printed result is still a result ────────────────────────────
    # Generated scripts routinely print `json.dumps(obj, indent=2)`. The original
    # single-line test (`line.startswith("{") and line.endswith("}")`) could never
    # match that, so the report card said "no structured result found" while
    # showing the very JSON it had failed to parse. 8 of the 20 historical runs
    # that printed a result were affected.
    PRETTY = (
        "Optimizer: Optimization complete!\n"
        "{\n"
        '  "tool": "psi4",\n'
        '  "property": "standard_heat_of_formation",\n'
        '  "standard_heat_of_formation_kJ_per_mol": 245.699,\n'
        '  "unit": "kJ/mol",\n'
        '  "output_file": "results.csv"\n'
        "}\n"
    )

    def test_pulls_a_multi_line_indented_object(self):
        result = _extract_result({"stdout": self.PRETTY})
        assert result is not None, "pretty-printed JSON must parse"
        assert result["standard_heat_of_formation_kJ_per_mol"] == 245.699
        assert result["property"] == "standard_heat_of_formation"

    def test_the_last_object_still_wins_when_pretty_printed(self):
        stdout = self.PRETTY + '{\n  "property": "later",\n  "later": 2\n}\n'
        assert _extract_result({"stdout": stdout})["property"] == "later"

    def test_a_single_line_object_after_a_pretty_one_still_wins(self):
        stdout = self.PRETTY + '{"property": "last", "last": 1}\n'
        assert _extract_result({"stdout": stdout})["property"] == "last"

    def test_an_indented_object_is_found(self):
        """MPI-aware scripts print through helpers that may indent the block."""
        stdout = '  {\n    "property": "e", "e": 1.5\n  }\n'
        assert _extract_result({"stdout": stdout}) == {"property": "e", "e": 1.5}

    def test_a_brace_inside_log_text_does_not_start_a_parse(self):
        """Solver logs are full of braces; only a line starting with { counts."""
        stdout = (
            "SCF converged, density matrix {rms 1e-9} written\n"
            "note: see {docs} for details\n"
        )
        assert _extract_result({"stdout": stdout}) is None

    def test_an_unterminated_object_is_ignored(self):
        """A truncated tail must not take down the report."""
        stdout = '{\n  "property": "band_gap",\n  "band_gap": 6.7\n'
        assert _extract_result({"stdout": stdout}) is None

    def test_a_valid_object_still_wins_after_a_truncated_one(self):
        stdout = ('{\n  "property": "broken"\n'
                  '{"property": "good", "good": 1}\n')
        assert _extract_result({"stdout": stdout})["property"] == "good"

    def test_a_json_list_is_not_a_result(self):
        assert _extract_result({"stdout": '[1, 2, 3]\n'}) is None

    def test_nan_is_made_json_safe(self):
        """json.loads accepts bare NaN; the response renderer does not."""
        result = _extract_result({"stdout": '{"property": "x", "x": NaN}\n'})
        assert result is not None
        assert result["x"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
