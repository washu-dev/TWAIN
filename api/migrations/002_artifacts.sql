-- Captured stage artifacts: the JSON specs (intent, goal graph, discovery,
-- execution plan, budget, execution result) and the generated RunBundle code
-- files (run_bundle/main.py, config.yaml, requirements.txt, …). The runner
-- writes these when a run finishes so the API can serve them without touching
-- the runner's filesystem. Idempotent.
CREATE TABLE IF NOT EXISTS artifacts (
    id         BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    name       TEXT NOT NULL,                 -- 'execution_plan', 'run_bundle/main.py', …
    kind       TEXT NOT NULL DEFAULT 'text',  -- json | python | yaml | text
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, name)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts (session_id, name);
