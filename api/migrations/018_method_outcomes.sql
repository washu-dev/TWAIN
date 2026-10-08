-- What each method did on this deployment's cluster (#188), so planning can
-- prefer one that has worked for the same property before -- the same request
-- chose xtb, RDKit and OpenMM across three runs (3e30f199, 44ad9f9e, e825d5ed).
-- One row per run per method: the method a run finished with, and any it gave
-- up on along the way (a method fallback). Idempotent.
CREATE TABLE IF NOT EXISTS method_outcomes (
    id                 BIGSERIAL PRIMARY KEY,
    session_id         UUID REFERENCES conversations (id) ON DELETE SET NULL,
    requested_property TEXT NOT NULL,      -- normalized: lower case, words joined by _
    method             TEXT NOT NULL,      -- the calculator, else the primary library (lower case)
    calculator         TEXT,
    libraries          JSONB NOT NULL DEFAULT '[]'::jsonb,
    succeeded          BOOLEAN NOT NULL,   -- the calculation ran to completion
    verdict            TEXT,               -- validation: accepted | rejected | needs_review
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_method_outcomes_run
    ON method_outcomes (session_id, method);
CREATE INDEX IF NOT EXISTS idx_method_outcomes_property
    ON method_outcomes (requested_property, created_at DESC);
