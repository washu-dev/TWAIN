-- Shared-environment change proposals (#187). A change to a shared RIS env
-- affects every simulation that uses it, so it is never made automatically:
-- triage proposes it, an approver (TWAIN_ENV_APPROVERS) approves it from email,
-- and the cluster monitor builds a new version beside the live one, verifies it
-- and only then promotes it (scripts/ris/rebuild_envs.sh). Idempotent.
CREATE TABLE IF NOT EXISTS env_proposals (
    id            BIGSERIAL PRIMARY KEY,
    session_id    UUID REFERENCES conversations (id) ON DELETE SET NULL,  -- the run that needed it
    env           TEXT NOT NULL,           -- e.g. nwchem
    package       TEXT NOT NULL,           -- the conda-forge package to add
    module        TEXT,                    -- what the job failed to import
    reason        TEXT NOT NULL,
    spec_before   TEXT NOT NULL,           -- scripts/ris/envs/<env>.yml as shipped
    spec_after    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected', 'building',
                                    'promoted', 'failed', 'expired')),
    decided_by    TEXT,                    -- the approver whose button was used
    decided_at    TIMESTAMPTZ,
    version       TEXT,                    -- .versions/<version>/<env> once built
    ris_job_id    TEXT,
    result        TEXT,                    -- what the build/verify/promote job reported
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);
-- At most one open proposal per env + package.
CREATE UNIQUE INDEX IF NOT EXISTS idx_env_proposals_open
    ON env_proposals (env, package) WHERE status IN ('pending', 'approved', 'building');

-- Email buttons can answer a proposal (approve/reject) or offer a re-run, not
-- only a run's question: the gate message becomes optional.
ALTER TABLE email_actions ALTER COLUMN gate_message_id DROP NOT NULL;
ALTER TABLE email_actions ADD COLUMN IF NOT EXISTS proposal_id BIGINT
    REFERENCES env_proposals (id) ON DELETE CASCADE;
ALTER TABLE email_actions ADD COLUMN IF NOT EXISTS recipient TEXT;
