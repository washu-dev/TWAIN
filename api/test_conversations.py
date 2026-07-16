from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from auth import get_current_user
from main import app

client = TestClient(app)

USER = {"id": "user-1", "subject": "s1", "email": "u@wustl.edu", "name": "U", "role": "user"}
CONVERSATION = {
    "id": "conv-1", "user_id": "user-1", "title": "predict solubility",
    "status": "running", "current_state": "INTAKE",
    "created_at": "2026-07-07T00:00:00Z", "updated_at": "2026-07-07T00:00:00Z",
}
MESSAGE = {
    "id": 1, "role": "user", "content": "hi", "kind": "chat",
    "state": None, "created_at": "2026-07-07T00:00:00Z",
}


@pytest.fixture(autouse=True)
def _as_user():
    app.dependency_overrides[get_current_user] = lambda: USER
    yield
    app.dependency_overrides.clear()


class TestStart:
    @patch("conversations.create_conversation", return_value=CONVERSATION)
    def test_start_returns_conversation(self, mock_create):
        response = client.post("/api/conversations", json={"request": "predict solubility"})
        assert response.status_code == 200
        assert response.json()["data"]["id"] == "conv-1"
        mock_create.assert_called_once()

    def test_start_rejects_empty_request(self):
        response = client.post("/api/conversations", json={"request": "   "})
        assert response.status_code == 422

    @patch("conversations.create_conversation", return_value=CONVERSATION)
    def test_start_forwards_compute_target(self, mock_create):
        response = client.post(
            "/api/conversations",
            json={"request": "band gap of silicon", "compute_target": "slurm"},
        )
        assert response.status_code == 200
        assert mock_create.call_args.kwargs["compute_target"] == "slurm"

    def test_start_rejects_unknown_compute_target(self):
        response = client.post(
            "/api/conversations",
            json={"request": "band gap of silicon", "compute_target": "mainframe"},
        )
        assert response.status_code == 422


class TestListAndGet:
    @patch("conversations.list_conversations", return_value=[CONVERSATION])
    def test_list(self, _mock):
        response = client.get("/api/conversations")
        assert response.status_code == 200
        assert response.json()["count"] == 1

    @patch("conversations.list_messages", return_value=[MESSAGE])
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_detail_includes_messages(self, _mock_conv, _mock_msgs):
        response = client.get("/api/conversations/conv-1")
        assert response.status_code == 200
        assert response.json()["data"]["messages"][0]["content"] == "hi"

    @patch("conversations.get_conversation", return_value=None)
    def test_detail_404_when_not_owner(self, _mock):
        response = client.get("/api/conversations/nope")
        assert response.status_code == 404


class TestMessagesAndApproval:
    @patch("conversations.add_message", return_value=MESSAGE)
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_post_message(self, _mock_conv, mock_add):
        response = client.post("/api/conversations/conv-1/messages", json={"content": "aspirin"})
        assert response.status_code == 200
        mock_add.assert_called_once_with("conv-1", "aspirin")

    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_post_message_rejects_empty(self, _mock_conv):
        response = client.post("/api/conversations/conv-1/messages", json={"content": ""})
        assert response.status_code == 422

    @patch("conversations.get_conversation", return_value=None)
    def test_post_message_404_when_not_owner(self, _mock_conv):
        response = client.post("/api/conversations/x/messages", json={"content": "hi"})
        assert response.status_code == 404

    @patch("conversations.add_approval_response", return_value={**MESSAGE, "kind": "approval_response"})
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_post_approval(self, _mock_conv, mock_add):
        response = client.post("/api/conversations/conv-1/approval", json={"decision": "approve"})
        assert response.status_code == 200
        mock_add.assert_called_once_with("conv-1", "approve", slurm_request=None)

    @patch("conversations.add_approval_response", return_value={**MESSAGE, "kind": "approval_response"})
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_post_approval_with_slurm_overrides(self, _mock_conv, mock_add):
        body = {
            "decision": "approve",
            "slurm_request": {"cpu_count": 16, "gpu_count": 0, "ram": 32, "max_time": 1.0},
        }
        response = client.post("/api/conversations/conv-1/approval", json=body)
        assert response.status_code == 200
        assert mock_add.call_args.kwargs["slurm_request"]["ram"] == 32

    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_post_approval_rejects_bad_decision(self, _mock_conv):
        response = client.post("/api/conversations/conv-1/approval", json={"decision": "maybe"})
        assert response.status_code == 422


class TestStream:
    @patch("conversations.get_conversation_status", return_value="completed")
    @patch("conversations.get_events", return_value=[
        {"id": 1, "seq": 0, "event_type": "stage.completed",
         "payload": {"to": "PLAN"}, "created_at": "2026-07-07T00:00:00Z"},
    ])
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_stream_emits_events_then_done(self, _mock_conv, _mock_events, _mock_status):
        response = client.get("/api/conversations/conv-1/stream")
        assert response.status_code == 200
        body = response.text
        assert "event: stage.completed" in body
        assert "event: done" in body


ARTIFACTS = [
    {"name": "execution_plan", "kind": "json", "size": 120},
    {"name": "run_bundle/main.py", "kind": "python", "size": 340},
    {"name": "run_bundle/requirements.txt", "kind": "text", "size": 20},
]


class TestReportAndArtifacts:
    @patch("conversations.list_artifacts", return_value=ARTIFACTS)
    @patch("conversations.get_artifact")
    @patch("conversations.get_conversation", return_value={**CONVERSATION, "status": "completed"})
    def test_report_assembles_summary_and_artifacts(self, _conv, mock_get, _list):
        mock_get.side_effect = lambda _sid, name: (
            {"name": name, "kind": "json", "content": '{"cost": 0.5}'}
            if name == "execution_plan" else None
        )
        response = client.get("/api/conversations/conv-1/report")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["plan"] == {"cost": 0.5}
        assert data["execution_result"] is None
        assert len(data["artifacts"]) == 3

    @patch("conversations.list_artifacts", return_value=ARTIFACTS)
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_list_artifacts(self, _conv, _list):
        response = client.get("/api/conversations/conv-1/artifacts")
        assert response.status_code == 200
        assert response.json()["count"] == 3

    @patch("conversations.get_artifact",
           return_value={"name": "run_bundle/main.py", "kind": "python", "content": "import pymatgen"})
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_get_artifact_with_slash_name(self, _conv, _get):
        response = client.get("/api/conversations/conv-1/artifacts/run_bundle/main.py")
        assert response.status_code == 200
        assert response.json()["data"]["content"] == "import pymatgen"

    @patch("conversations.get_artifact", return_value=None)
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_get_artifact_404(self, _conv, _get):
        response = client.get("/api/conversations/conv-1/artifacts/nope.json")
        assert response.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
