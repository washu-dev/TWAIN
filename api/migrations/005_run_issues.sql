-- User-submitted GitHub issues filed from a run window.
--
-- A researcher watching a run can report a problem without leaving TWAIN; the
-- API attaches the run's own data (state, toolset, errors, transcript tail) to
-- the issue so a maintainer can act on it without asking what happened. This
-- table is the local record of every submission, so the run window can show
-- what has already been reported and the link survives even when GitHub was
-- unreachable (status 'queued' / 'failed'). Idempotent.
CREATE TABLE IF NOT EXISTS run_issues (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    user_id         UUID REFERENCES users (id) ON DELETE SET NULL,
    category        TEXT NOT NULL DEFAULT 'other'
                    CHECK (category IN ('bug', 'library', 'result', 'other')),
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    -- The exact run snapshot that was attached, kept verbatim: the run keeps
    -- moving after the report is filed, so re-deriving it later would not
    -- reproduce what the reporter actually saw.
    run_context     JSONB,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('created', 'queued', 'failed')),
    issue_number    INTEGER,
    issue_url       TEXT,
    error           TEXT,                          -- why filing failed, when it did
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_run_issues_conversation
    ON run_issues (conversation_id, id DESC);
