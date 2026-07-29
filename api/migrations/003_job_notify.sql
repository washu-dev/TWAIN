-- TWAIN async runner — wake the runner the instant a job is queued.
-- Idempotent: safe to run repeatedly.
--
-- The runner LISTENs on 'twain_jobs' (see runner/db.py JobNotifyWaiter) instead
-- of polling every second. This trigger NOTIFYs that channel whenever a queued
-- job is inserted (a 'start' from a new conversation, or a 'resume' from a user
-- reply / approval), so a released runner wakes immediately. The runner keeps a
-- generous fallback poll as a safety net in case a notification is ever missed.

CREATE OR REPLACE FUNCTION notify_new_job() RETURNS trigger AS $$
BEGIN
    -- Payload is the session id so a listener could route/filter; the runner just
    -- treats any notification as "there may be work" and re-runs claim_job().
    PERFORM pg_notify('twain_jobs', NEW.session_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_notify_new_job ON jobs;
CREATE TRIGGER trg_notify_new_job
    AFTER INSERT ON jobs
    FOR EACH ROW
    WHEN (NEW.status = 'queued')
    EXECUTE FUNCTION notify_new_job();
