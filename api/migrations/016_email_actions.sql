-- One-tap answers from email (Approve / Reject, Run now / Not now, Accept / Try a
-- correction). The worker issues one random token per choice when it emails a
-- gate; only its SHA-256 is stored. A token answers exactly ONE gate (the
-- message it was issued for), once, before it expires -- and every sibling is
-- spent with it, so the other button in the same email can't answer too.
-- Idempotent: safe to run repeatedly.
CREATE TABLE IF NOT EXISTS email_actions (
    id              BIGSERIAL PRIMARY KEY,
    token_hash      TEXT NOT NULL UNIQUE,
    session_id      UUID NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    gate_message_id BIGINT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    gate_kind       TEXT NOT NULL,       -- approval_request | heavy_confirm | validation_gate
    choice          TEXT NOT NULL,       -- what the button sends: approve, reject, yes, no, accept, rerun
    label           TEXT NOT NULL,       -- the button text, for the confirmation page
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_email_actions_gate
    ON email_actions (session_id, gate_message_id);
