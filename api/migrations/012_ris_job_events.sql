-- RIS API job webhooks -- wake a runner waiting on a Slurm job the moment ris-api
-- reports a state change, instead of at its next 30 s poll.
-- Idempotent: safe to run repeatedly.
--
-- POST /api/ris/webhooks (api/ris_webhooks.py) verifies each delivery's Standard
-- Webhooks signature and inserts it here. Delivery is at-least-once, so the
-- delivery id (the `webhook-id` header) is the primary key and a resend is an
-- ON CONFLICT no-op -- which also means the trigger below fires once per event.
--
-- The runner LISTENs on 'ris_job_events' while it waits on a job (see
-- runner/db.py RisJobEventWaiter) and re-polls when the payload is its job id.
-- Polling stays the source of truth: a lost or late webhook only costs latency.

CREATE TABLE IF NOT EXISTS ris_job_events (
    webhook_id  TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    job_id      TEXT,
    state       TEXT,
    payload     JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ris_job_events_job_idx ON ris_job_events (job_id, received_at);

CREATE OR REPLACE FUNCTION notify_ris_job_event() RETURNS trigger AS $$
BEGIN
    -- webhook.test events carry no job id; nothing is waiting on those.
    IF NEW.job_id IS NOT NULL THEN
        PERFORM pg_notify('ris_job_events', NEW.job_id);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_notify_ris_job_event ON ris_job_events;
CREATE TRIGGER trg_notify_ris_job_event
    AFTER INSERT ON ris_job_events
    FOR EACH ROW
    EXECUTE FUNCTION notify_ris_job_event();
