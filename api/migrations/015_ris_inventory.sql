-- RIS inventory (P1, #185): what the cluster envs ACTUALLY contain, observed by a
-- read-only Slurm job, so planning stops trusting the spec files. The cluster
-- monitor (runner/inventory.py) submits scripts/ris/inventory.sh when the
-- newest snapshot is older than TWAIN_INVENTORY_HOURS, and ingests its output.
-- Idempotent: safe to run repeatedly.
CREATE TABLE IF NOT EXISTS ris_inventory (
    id           BIGSERIAL PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'submitted'
                 CHECK (status IN ('submitted', 'ingested', 'failed')),
    ris_job_id   TEXT,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    taken_at     TIMESTAMPTZ,            -- when the job looked (its own clock)
    envs_root    TEXT,
    -- {"<env>": {"version": "2026-10-08", "python": "3.11.15",
    --            "packages": {"<name>": "<version>", ...}, "error": null}}
    envs         JSONB,
    modules      JSONB,                  -- `module -t spider`, one entry per line
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ris_inventory_latest
    ON ris_inventory (status, finished_at DESC);
