-- What TWAIN can actually run, published by the runner.
--
-- The registries (configs/discovery_registry.json, configs/calculator_registry.json)
-- declare what TWAIN KNOWS ABOUT. Whether a library is actually installed is a
-- property of the cluster envs under twain-envs/, which only the runner can see --
-- the API is a separate deployable on ECS with no access to that filesystem. So the
-- runner probes each registry entry in the provisioned envs and publishes the
-- verdict here, and the API serves this table. A provision run followed by the
-- usual runner restart therefore refreshes the list on its own.
--
-- Keyed on (kind, name): the same name can be both a library and a calculator
-- (xtb is registered as each), and they are genuinely different rows.
CREATE TABLE IF NOT EXISTS library_availability (
    kind        TEXT NOT NULL,            -- 'library' | 'calculator'
    name        TEXT NOT NULL,            -- display name, e.g. 'Quantum ESPRESSO'
    import_name TEXT,                     -- module probed, NULL when unknown
    version     TEXT,                     -- version the registry declares
    description TEXT,
    installed   BOOLEAN NOT NULL,
    env         TEXT,                     -- cluster env providing it, when installed
    detail      TEXT,                     -- why not installed / how it was found
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, name)
);

CREATE INDEX IF NOT EXISTS idx_library_availability_installed
    ON library_availability (installed, kind, name);
