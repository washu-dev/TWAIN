-- Job tickets -- how a Slurm job on RIS moves files through S3 with no AWS
-- credentials on the cluster (#170). Idempotent: safe to run repeatedly.
--
-- When the runner/worker submits an attempt it stores a random ticket here
-- (only its SHA-256 -- the token itself travels in the job's environment and is
-- never stored) scoped to one run attempt's S3 prefix. The job trades it at
-- POST /api/job-tickets/urls (api/job_tickets.py) for short-lived presigned
-- URLs at the moment it needs them: GET for input/, PUT for output/. URLs are
-- minted on demand because a URL signed at submit time with the API's
-- temporary role credentials would die with them -- hours before a job that
-- waited in the queue gets to use it.

CREATE TABLE IF NOT EXISTS job_tickets (
    token_hash   TEXT PRIMARY KEY,                 -- sha256(token), hex
    run_id       TEXT NOT NULL,
    attempt      INTEGER NOT NULL,
    s3_prefix    TEXT NOT NULL,                    -- runs/<run_id>/attempt-<n>
    expires_at   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    use_count    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS job_tickets_run_idx ON job_tickets (run_id, attempt);
CREATE INDEX IF NOT EXISTS job_tickets_expiry_idx ON job_tickets (expires_at);
