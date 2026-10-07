"""Job dispatch: the transactional outbox in front of SQS (P2, #171)."""
import json
from unittest.mock import MagicMock, patch

import pytest

import conversations
import dispatch


class FakeSQS:
    def __init__(self, fail_on=()):
        self.sent, self.fail_on = [], set(fail_on)

    def send_message(self, QueueUrl, **kw):
        job = json.loads(kw["MessageBody"])["job_id"]
        if job in self.fail_on:
            raise RuntimeError("throttled")
        self.sent.append(kw)


@pytest.fixture
def sqs_mode(monkeypatch):
    monkeypatch.setenv("TWAIN_DISPATCH", "sqs")
    monkeypatch.setenv("TWAIN_JOB_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/1/twain-jobs.fifo")


def test_db_mode_is_the_old_behaviour(monkeypatch):
    monkeypatch.delenv("TWAIN_DISPATCH", raising=False)
    assert dispatch.initial_status() == "queued"
    assert dispatch.publish(MagicMock(), [(1, "s", "start")], client=FakeSQS()) == 0


def test_sqs_mode_inserts_rows_the_old_runner_cannot_claim(sqs_mode):
    # The login-node runner claims only 'queued'.
    assert dispatch.initial_status() == "dispatching"


def test_a_message_is_ordered_per_run_and_deduplicated_per_job(sqs_mode):
    sqs, conn = FakeSQS(), MagicMock()
    assert dispatch.publish(conn, [(41, "run-a", "start"), (42, "run-b", "resume")], client=sqs) == 2
    first = sqs.sent[0]
    assert json.loads(first["MessageBody"]) == {"job_id": 41, "session_id": "run-a", "kind": "start"}
    assert first["MessageGroupId"] == "run-a" and first["MessageDeduplicationId"] == "job-41"
    marked = conn.cursor.return_value.__enter__.return_value.execute.call_args.args
    assert "published_at = now()" in marked[0] and marked[1] == ([41, 42],)


def test_a_failed_send_is_left_for_the_relay_not_raised(sqs_mode):
    sqs, conn = FakeSQS(fail_on={42}), MagicMock()
    assert dispatch.publish(conn, [(41, "a", "start"), (42, "b", "start")], client=sqs) == 1
    marked = conn.cursor.return_value.__enter__.return_value.execute.call_args.args
    assert marked[1] == ([41],)          # 42 stays unpublished -> the relay re-sends it


def test_no_queue_url_dispatches_nothing_and_keeps_the_row(sqs_mode, monkeypatch):
    monkeypatch.delenv("TWAIN_JOB_QUEUE_URL")
    assert dispatch.publish(MagicMock(), [(1, "s", "start")], client=FakeSQS()) == 0


def test_a_new_run_is_published_only_after_its_transaction_commits(sqs_mode):
    order = []
    cursor = MagicMock()
    cursor.fetchone.side_effect = [{"id": "conv-1", "title": "t"}, {"id": 9}]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.commit.side_effect = lambda: order.append("commit")
    with patch("conversations.get_connection", return_value=conn), \
         patch("dispatch.publish", side_effect=lambda c, jobs, **k: order.append(("publish", jobs))):
        conversations.create_conversation("user-1", "predict the solubility of aspirin")
    inserted = next(c.args for c in cursor.execute.call_args_list if "INSERT INTO jobs" in c.args[0])
    assert inserted[1][2] == "dispatching"
    assert order == ["commit", ("publish", [(9, "conv-1", "start")])]


def test_terminate_wakes_a_paused_run(sqs_mode):
    cursor = MagicMock()
    cursor.fetchone.side_effect = [{"id": 1, "kind": "terminate"}, {"id": 12}]
    conn = MagicMock()
    conn.cursor.return_value = cursor
    with patch("conversations.get_connection", return_value=conn), \
         patch("dispatch.publish") as publish:
        conversations.request_termination("conv-1")
    assert any("'resume'" in c.args[0] for c in cursor.execute.call_args_list)
    publish.assert_called_once_with(conn, [(12, "conv-1", "resume")])
