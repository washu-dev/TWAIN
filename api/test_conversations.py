from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import conversations
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
    def test_start_forwards_max_cost(self, mock_create):
        response = client.post(
            "/api/conversations", json={"request": "predict solubility", "max_cost": 2.5}
        )
        assert response.status_code == 200
        assert mock_create.call_args.kwargs["max_cost"] == 2.5

    @patch("conversations.create_conversation", return_value=CONVERSATION)
    def test_start_defaults_max_cost_to_none(self, mock_create):
        response = client.post("/api/conversations", json={"request": "predict solubility"})
        assert response.status_code == 200
        assert mock_create.call_args.kwargs["max_cost"] is None

    def test_start_rejects_nonpositive_max_cost(self):
        response = client.post(
            "/api/conversations", json={"request": "predict solubility", "max_cost": 0}
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
        mock_add.assert_called_once_with("conv-1", "approve", slurm_request=None, acceptance_metrics=None)

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


class TestTerminate:
    @patch("conversations.request_termination", return_value={**MESSAGE, "kind": "terminate"})
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_terminate_records_request(self, _mock_conv, mock_term):
        response = client.post("/api/conversations/conv-1/terminate")
        assert response.status_code == 200
        assert response.json()["data"]["kind"] == "terminate"
        mock_term.assert_called_once_with("conv-1")

    @patch("conversations.get_conversation",
           return_value={**CONVERSATION, "status": "completed"})
    def test_terminate_conflicts_when_already_finished(self, _mock_conv):
        response = client.post("/api/conversations/conv-1/terminate")
        assert response.status_code == 409

    @patch("conversations.get_conversation", return_value=None)
    def test_terminate_404_when_not_owner(self, _mock_conv):
        response = client.post("/api/conversations/x/terminate")
        assert response.status_code == 404


class TestRerun:
    @patch("conversations.rerun_conversation",
           return_value={**CONVERSATION, "status": "running", "current_state": "CLARIFY"})
    @patch("conversations.get_conversation", return_value={**CONVERSATION, "status": "completed"})
    def test_rerun_enqueues_and_returns_running(self, _conv, mock_rerun):
        response = client.post("/api/conversations/conv-1/rerun", json={"state": "CLARIFY"})
        assert response.status_code == 200
        assert response.json()["data"]["current_state"] == "CLARIFY"
        mock_rerun.assert_called_once_with(
            "conv-1", "user-1", "CLARIFY", feedback=None, request=None,
            slurm_request=None, acceptance_metrics=None)

    @patch("conversations.rerun_conversation",
           return_value={**CONVERSATION, "current_state": "PLAN"})
    @patch("conversations.get_conversation", return_value={**CONVERSATION, "status": "completed"})
    def test_rerun_normalizes_state_case(self, _conv, mock_rerun):
        response = client.post("/api/conversations/conv-1/rerun", json={"state": "plan"})
        assert response.status_code == 200
        mock_rerun.assert_called_once_with(
            "conv-1", "user-1", "PLAN", feedback=None, request=None,
            slurm_request=None, acceptance_metrics=None)

    @patch("conversations.rerun_conversation",
           return_value={**CONVERSATION, "status": "running", "current_state": "DISCOVER"})
    @patch("conversations.get_conversation", return_value={**CONVERSATION, "status": "completed"})
    def test_rerun_forwards_revision_feedback(self, _conv, mock_rerun):
        # Mid-session revision: the researcher's "here's what to change" rides
        # the rerun job so the runner folds it into the intent before replanning.
        response = client.post(
            "/api/conversations/conv-1/rerun",
            json={"state": "DISCOVER", "feedback": "  use xtb instead of DFT  "},
        )
        assert response.status_code == 200
        mock_rerun.assert_called_once_with(
            "conv-1", "user-1", "DISCOVER", feedback="use xtb instead of DFT",
            request=None, slurm_request=None, acceptance_metrics=None)

    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_rerun_rejects_unknown_state(self, _conv):
        response = client.post("/api/conversations/conv-1/rerun", json={"state": "BOGUS"})
        assert response.status_code == 422

    @patch("conversations.get_conversation", return_value=None)
    def test_rerun_404_when_not_owner(self, _conv):
        response = client.post("/api/conversations/nope/rerun", json={"state": "CLARIFY"})
        assert response.status_code == 404

    @patch("conversations.rerun_conversation", side_effect=ValueError("still active"))
    @patch("conversations.get_conversation", return_value=CONVERSATION)
    def test_rerun_409_when_run_still_active(self, _conv, _rerun):
        response = client.post("/api/conversations/conv-1/rerun", json={"state": "CLARIFY"})
        assert response.status_code == 409


class TestDelete:
    @patch("conversations.delete_conversation", return_value=True)
    def test_delete_ok(self, mock_del):
        response = client.delete("/api/conversations/conv-1")
        assert response.status_code == 200
        assert response.json()["data"]["deleted"] is True
        mock_del.assert_called_once_with("conv-1", "user-1")

    @patch("conversations.delete_conversation", return_value=False)
    def test_delete_404_when_not_owner(self, _mock):
        response = client.delete("/api/conversations/nope")
        assert response.status_code == 404


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


class TestRerunFromIntakeWithAnEditedRequest:
    """Re-running from Intake re-reads the opening prompt, so the researcher can
    change what they asked for instead of restarting the same request."""

    @patch("conversations.rerun_conversation",
           return_value={**CONVERSATION, "status": "running", "current_state": "INTAKE"})
    @patch("conversations.get_conversation", return_value={**CONVERSATION, "status": "completed"})
    def test_an_edited_request_is_forwarded(self, _conv, mock_rerun):
        response = client.post(
            "/api/conversations/conv-1/rerun",
            json={"state": "INTAKE", "request": "  compute the bandgap of germanium  "},
        )
        assert response.status_code == 200
        mock_rerun.assert_called_once_with(
            "conv-1", "user-1", "INTAKE", feedback=None,
            request="compute the bandgap of germanium", slurm_request=None, acceptance_metrics=None)

    @patch("conversations.rerun_conversation")
    @patch("conversations.get_conversation", return_value={**CONVERSATION, "status": "completed"})
    def test_an_edited_request_is_refused_for_a_later_stage(self, _conv, mock_rerun):
        """Every stage after intake works from the spec intake produced, so an
        edit there would silently do nothing -- say so instead of accepting it."""
        response = client.post(
            "/api/conversations/conv-1/rerun",
            json={"state": "PLAN", "request": "something else entirely"},
        )
        assert response.status_code == 422
        assert "only applies when re-running from INTAKE" in response.json()["detail"]
        mock_rerun.assert_not_called()

    @patch("conversations.rerun_conversation",
           return_value={**CONVERSATION, "current_state": "INTAKE"})
    @patch("conversations.get_conversation", return_value={**CONVERSATION, "status": "completed"})
    def test_a_blank_edit_falls_back_to_the_original_request(self, _conv, mock_rerun):
        response = client.post(
            "/api/conversations/conv-1/rerun",
            json={"state": "INTAKE", "request": "   "},
        )
        assert response.status_code == 200
        mock_rerun.assert_called_once_with(
            "conv-1", "user-1", "INTAKE", feedback=None, request=None,
            slurm_request=None, acceptance_metrics=None)


class TestRerunWithEditedResources:
    """Re-running the SAME plan with different resources.

    The pairing with `request` is deliberate: each structured edit is accepted
    only at the stage that actually consumes it, and refused elsewhere rather
    than accepted and silently dropped.
    """

    @patch("conversations.rerun_conversation",
           return_value={**CONVERSATION, "current_state": "EXECUTE"})
    @patch("conversations.get_conversation", return_value={**CONVERSATION, "status": "completed"})
    def test_resources_are_forwarded_after_plan(self, _conv, mock_rerun):
        response = client.post(
            "/api/conversations/conv-1/rerun",
            json={"state": "EXECUTE",
                  "slurm_request": {"cpu_count": 24, "max_time": 4.0}},
        )
        assert response.status_code == 200
        mock_rerun.assert_called_once_with(
            "conv-1", "user-1", "EXECUTE", feedback=None, request=None,
            slurm_request={"cpu_count": 24, "max_time": 4.0}, acceptance_metrics=None)

    @pytest.mark.parametrize("state", ["INTAKE", "DISCOVER", "PLAN"])
    @patch("conversations.rerun_conversation")
    @patch("conversations.get_conversation", return_value={**CONVERSATION, "status": "completed"})
    def test_resources_are_refused_at_or_before_plan(self, _conv, mock_rerun, state):
        """Re-running PLAN synthesizes a fresh plan, which would discard them."""
        response = client.post(
            "/api/conversations/conv-1/rerun",
            json={"state": state, "slurm_request": {"cpu_count": 24}},
        )
        assert response.status_code == 422
        assert "after PLAN" in response.json()["detail"]
        mock_rerun.assert_not_called()

    @patch("conversations.rerun_conversation",
           return_value={**CONVERSATION, "current_state": "EXECUTE"})
    @patch("conversations.get_conversation", return_value={**CONVERSATION, "status": "completed"})
    def test_a_note_and_resources_travel_together(self, _conv, mock_rerun):
        """The note applies to every stage; the fields only to this one."""
        response = client.post(
            "/api/conversations/conv-1/rerun",
            json={"state": "EXECUTE", "feedback": "give it more memory",
                  "slurm_request": {"ram": 64}},
        )
        assert response.status_code == 200
        mock_rerun.assert_called_once_with(
            "conv-1", "user-1", "EXECUTE", feedback="give it more memory",
            request=None, slurm_request={"ram": 64}, acceptance_metrics=None)


class TestRerunStatusGate:
    """A suspended run can be redirected; one that is actually being driven cannot.

    A suspended run is checkpointed with its process released, so no runner is
    driving it and no job is in flight -- which is what the restriction was
    protecting against. This is what lets the accept-or-rerun question offer
    "re-run from a step" rather than only the automatic correction loop.
    """

    def _conn(self, status):
        """A fake connection replaying the reads rerun_conversation performs."""
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"status": status},                                  # the status gate
            {"content": "compute the bandgap of silicon"},       # opening request
            {"params": {"max_cost": 2.0}},                       # original start job
            {**CONVERSATION, "status": "running"},               # the UPDATE ... RETURNING
        ]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn, cursor

    @pytest.mark.parametrize("status", ["awaiting_input", "awaiting_approval",
                                        "completed", "error", "cancelled"])
    def test_a_suspended_or_finished_run_can_be_redirected(self, status):
        conn, cursor = self._conn(status)
        with patch("conversations.get_connection", return_value=conn):
            result = conversations.rerun_conversation("conv-1", "user-1", "PLAN")
        assert result is not None
        conn.commit.assert_called_once()
        assert any("INSERT INTO jobs" in str(c) for c in cursor.execute.call_args_list)

    def test_a_running_run_is_refused(self):
        conn, _ = self._conn("running")
        with patch("conversations.get_connection", return_value=conn):
            with pytest.raises(ValueError, match="still active"):
                conversations.rerun_conversation("conv-1", "user-1", "PLAN")
        conn.commit.assert_not_called()

    def test_the_edited_request_reaches_the_job_and_the_transcript(self):
        conn, cursor = self._conn("awaiting_input")
        with patch("conversations.get_connection", return_value=conn):
            conversations.rerun_conversation(
                "conv-1", "user-1", "INTAKE", request="compute the bandgap of germanium")
        calls = [c.args for c in cursor.execute.call_args_list if len(c.args) > 1]
        job = next(a for a in calls if "INSERT INTO jobs" in a[0])
        assert "compute the bandgap of germanium" in job[1][1]
        assert "silicon" not in job[1][1]          # the original did not win
        # and it is visible on the transcript rather than only implied
        assert any("edited request" in str(a[1]) for a in calls)

    def test_the_abandoned_pass_questions_are_retired(self):
        """Choosing to re-run IS the answer to whatever was outstanding. Left
        looking unanswered, it makes the next gate believe it has already asked,
        so the run suspends without posting and the researcher waits on a
        question that never comes."""
        conn, cursor = self._conn("awaiting_input")
        with patch("conversations.get_connection", return_value=conn):
            conversations.rerun_conversation("conv-1", "user-1", "CLARIFY")
        retire = next(
            c.args for c in cursor.execute.call_args_list
            if len(c.args) > 1 and "UPDATE messages SET state = 'consumed'" in c.args[0]
        )
        assert "role = 'assistant'" in retire[0]
        assert retire[1] == ("conv-1", list(conversations.QUESTION_KINDS))
        # every gate's kind, not just the two that used to share 'clarification'
        assert "validation_gate" in conversations.QUESTION_KINDS
        assert "heavy_confirm" in conversations.QUESTION_KINDS


class TestActivityFeed:
    """GET /api/conversations/{id}/activity -- the live checklist's feed (#160)."""

    EVENTS = [
        {"id": 41, "event_type": "stage.progress", "created_at": "2026-10-06T08:41:40Z",
         "payload": {"stage": "EXECUTE", "step": "submit", "status": "done",
                     "label": "Submitted — Slurm job 3337323", "detail": {}}},
        {"id": 44, "event_type": "job.log", "created_at": "2026-10-06T08:43:00Z",
         "payload": {"job_id": "3337323", "text": "SCF converged\n"}},
    ]

    @patch("conversations.get_activity")
    @patch("conversations.owns_conversation", return_value=True)
    def test_returns_new_events_and_a_cursor(self, mock_owns, mock_activity):
        mock_activity.return_value = self.EVENTS
        response = client.get("/api/conversations/conv-1/activity?after=40")
        assert response.status_code == 200
        body = response.json()
        assert [e["id"] for e in body["data"]] == [41, 44] and body["next_after"] == 44
        mock_owns.assert_called_once_with("conv-1", "user-1")
        session, after, types, _limit = mock_activity.call_args.args
        assert (session, after) == ("conv-1", 40)
        assert set(types) == {"stage.progress", "job.log"}

    @patch("conversations.get_activity", return_value=[])
    @patch("conversations.owns_conversation", return_value=True)
    def test_nothing_new_keeps_the_cursor(self, _owns, _activity):
        assert client.get("/api/conversations/conv-1/activity?after=44").json() == {
            "data": [], "next_after": 44}

    @patch("conversations.get_activity")
    @patch("conversations.owns_conversation", return_value=False)
    def test_someone_elses_run_is_404(self, _owns, mock_activity):
        assert client.get("/api/conversations/conv-1/activity").status_code == 404
        mock_activity.assert_not_called()
