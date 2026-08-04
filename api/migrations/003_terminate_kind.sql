-- 003: allow the 'terminate' control message kind.
--
-- The Terminate button (POST /api/conversations/{id}/terminate) records the
-- user's request as a messages row with kind='terminate'; the runner polls for
-- it between stages, blocking waits, and Slurm squeue checks. Re-runnable:
-- drop + re-add keeps this idempotent for dev.sh, which applies every
-- migration on each start.

ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_kind_check;

-- NOTE: dev.sh re-applies every migration on each start (api/migrations/*.sql
-- with ON_ERROR_STOP=1), so this file runs again on databases that later
-- migrations have already moved past. A plain ADD CONSTRAINT re-validates every
-- existing row against THIS era's list and fails on values a later migration
-- legitimately allows, aborting startup. NOT VALID skips that historical scan
-- while still enforcing the constraint on new writes, so the replay is safe and
-- the final constraint is whatever the newest migration installs.
-- Superseded by 008_question_kinds.sql, which widens this to the per-gate kinds.
ALTER TABLE messages ADD CONSTRAINT messages_kind_check
    CHECK (kind IN ('chat', 'clarification', 'approval_request',
                    'approval_response', 'terminate')) NOT VALID;
