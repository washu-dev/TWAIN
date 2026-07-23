-- TWAIN async runner — crash recovery + typed conversation lifecycle.
-- Idempotent: safe to run repeatedly.
--
-- Before this, a runner that died mid-slice (OOM, redeploy, SIGKILL) left its job
-- stuck in 'claimed'/'running' forever. Because claim_job serializes per session
-- (it skips a session that already has a claimed/running job), that wedged EVERY
-- future 'resume' for the session — the run was silently unrecoverable.
--
-- The fix is a lease: a claimed job is kept alive by a heartbeat while its runner
-- works; if the heartbeat goes stale (the runner died) the reaper re-queues the
-- job so another runner re-drives it from the checkpoint, or dead-letters it once
-- it has been attempted too many times. See runner/db.py (reap_stale_jobs) and
-- runner/runner.py (_Heartbeat).

-- 1. Per-job attempt counter (incremented on each claim) — bounds retries so a
--    persistently failing job dead-letters instead of looping forever.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;

-- 2. Liveness heartbeat. Stamped at claim and refreshed by the runner while the
--    slice runs; the reaper treats a job whose heartbeat is older than the lease
--    as orphaned. NULL only for pre-existing rows and jobs never claimed.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;

-- 3. Make the reaper's scan cheap: it only ever looks at in-flight jobs by their
--    heartbeat, so index exactly those.
CREATE INDEX IF NOT EXISTS idx_jobs_inflight
    ON jobs (heartbeat_at)
    WHERE status IN ('claimed', 'running');

-- 4. Type the conversation lifecycle. conversations.status was free text, so the
--    async waiting states ('awaiting_input' / 'awaiting_approval') written by the
--    runner were undeclared and a typo could slip through silently. Constrain it
--    to the statuses the UI understands (api/conversations.py, runner/bridges.py).
--    NOT VALID enforces the constraint for all new writes without failing the
--    migration on any pre-existing row (values have always come from this set).
ALTER TABLE conversations DROP CONSTRAINT IF EXISTS conversations_status_check;
ALTER TABLE conversations ADD CONSTRAINT conversations_status_check
    CHECK (status IN (
        'running', 'awaiting_input', 'awaiting_approval',
        'completed', 'error', 'rejected'
    )) NOT VALID;
