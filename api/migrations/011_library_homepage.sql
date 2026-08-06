-- Where to read about each library or engine.
--
-- The URL is static identity, not probed state, so by rights it belongs beside the
-- registry entry rather than in this snapshot of what the cluster has. It travels
-- through here anyway for one concrete reason: the API image is built from ./api
-- alone (see .github/workflows/deploy-api.yml), so configs/ is not in it and the
-- API cannot read the registries at request time. The runner can -- it already
-- reads both to build this table -- so it publishes the URL along with the verdict.
--
-- Sourced from each registry's `homepage`, falling back to `repo_url` for a library
-- that declares only a repository. NULL is allowed and simply renders no link.
ALTER TABLE library_availability
    ADD COLUMN IF NOT EXISTS homepage TEXT;
