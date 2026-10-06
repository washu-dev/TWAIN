-- Event-driven runner (P2, #171): SQS dispatch of jobs + cluster jobs that run
-- without a process waiting on them. Idempotent: safe to run repeatedly.

-- 1. 'dispatching': a job handed to SQS (TWAIN_DISPATCH=sqs) rather than left
--    for a polling runner. The login-node runner claims only status 'queued',
--    so jobs in this state can never be picked up by old code -- the fence that
--    retires it without touching its account. A worker claims one by id, from
--    the SQS message: UPDATE ... WHERE id = $1 AND status = 'dispatching'.
--    NOT VALID: new writes are checked; replaying this file never re-scans rows.
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
    CHECK (status IN ('queued', 'dispatching', 'claimed', 'running', 'done', 'error')) NOT VALID;

-- 2. Transactional outbox. The row IS the event; published_at is set once the
--    SQS send succeeded. A relay re-sends any 'dispatching' row still
--    unpublished after a grace period, so a failed send never loses a job.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_jobs_unpublished
    ON jobs (created_at)
    WHERE status = 'dispatching' AND published_at IS NULL;

-- 3. Cluster jobs: a Slurm job submitted through the RIS API that the run is
--    PAUSED on (no process waits for it). The cluster monitor (runner/monitor.py)
--    polls the non-terminal ones -- woken early by ris-api webhooks -- publishes
--    their progress and log, and enqueues the run's resume when one finishes.
CREATE TABLE IF NOT EXISTS cluster_jobs (
    ris_job_id    TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    attempt       INTEGER NOT NULL,
    s3_prefix     TEXT,
    status        TEXT NOT NULL DEFAULT 'submitted'
                  CHECK (status IN ('submitted', 'finished', 'collected', 'cancelled')),
    slurm_state   TEXT,                 -- last seen: PENDING, RUNNING, COMPLETED, ...
    node          TEXT,
    reason        TEXT,                 -- Slurm's pending reason
    log_offset    BIGINT NOT NULL DEFAULT 0,
    detail        JSONB,                -- what the run needs to collect: job name, request, ...
    submitted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_polled_at TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    collected_at  TIMESTAMPTZ,
    UNIQUE (session_id, attempt)
);
CREATE INDEX IF NOT EXISTS idx_cluster_jobs_open
    ON cluster_jobs (last_polled_at NULLS FIRST)
    WHERE status = 'submitted';
CREATE INDEX IF NOT EXISTS idx_cluster_jobs_session ON cluster_jobs (session_id, attempt);
