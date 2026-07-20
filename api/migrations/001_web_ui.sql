-- TWAIN web UI — Phase 0 schema
-- Idempotent: safe to run repeatedly. Requires PostgreSQL 13+ (gen_random_uuid()).

-- 1. Users (identity from Entra ID + local role for admin designation).
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject       TEXT NOT NULL UNIQUE,          -- Entra oid (stable per user)
    email         TEXT,
    name          TEXT,
    role          TEXT NOT NULL DEFAULT 'user'
                  CHECK (role IN ('user', 'admin')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

-- 2. Conversations — one per run; id equals the engine's session_id.
CREATE TABLE IF NOT EXISTS conversations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title         TEXT,
    status        TEXT NOT NULL DEFAULT 'running',
    current_state TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations (user_id, updated_at DESC);

-- 3. Messages — chat turns, clarification Q&A, and approval requests/responses.
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'chat'
                    CHECK (kind IN ('chat', 'clarification', 'approval_request', 'approval_response')),
    state           TEXT,                          -- state machine state when emitted
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, id);

-- 4. Sessions — mirrors the engine RunSession JSON so PgStore satisfies the
--    existing Store interface (get/list/resume/save_session).
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    researcher_id TEXT,
    state         TEXT,
    status        TEXT,
    data          JSONB NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sessions_researcher ON sessions (researcher_id, updated_at DESC);

-- 5. Run events — append-only lifecycle/provenance stream tailed by SSE.
CREATE TABLE IF NOT EXISTS run_events (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq        INTEGER,
    event_type TEXT NOT NULL,
    payload    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_run_events_session ON run_events (session_id, id);

-- 6. Jobs — Postgres-backed queue the runner claims (SELECT ... FOR UPDATE SKIP LOCKED).
CREATE TABLE IF NOT EXISTS jobs (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('start', 'resume', 'rerun')),
    params     JSONB,
    status     TEXT NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued', 'claimed', 'running', 'done', 'error')),
    claimed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, id);
