-- TWAIN terminate lifecycle statuses.
-- Idempotent: safe to run repeatedly.
--
-- The Terminate button flips a conversation to 'cancelling'
-- (api/conversations.py request_termination) and the runner settles it as
-- 'cancelled' (runner/runner.py _finalize_cancelled), but the status CHECK
-- introduced in 004_job_recovery.sql predates the Terminate feature and lists
-- neither value -- so every Terminate click died as an opaque 500 on the
-- constraint violation. Widen the check to the full lifecycle the API and
-- runner actually write.
ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_status_check;
ALTER TABLE conversations ADD CONSTRAINT conversations_status_check
    CHECK (status IN (
        'running', 'awaiting_input', 'awaiting_approval',
        'completed', 'error', 'rejected',
        'cancelling', 'cancelled'
    )) NOT VALID;
