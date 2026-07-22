-- 003: allow the 'terminate' control message kind.
--
-- The Terminate button (POST /api/conversations/{id}/terminate) records the
-- user's request as a messages row with kind='terminate'; the runner polls for
-- it between stages, blocking waits, and Slurm squeue checks. Re-runnable:
-- drop + re-add keeps this idempotent for dev.sh, which applies every
-- migration on each start.

ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_kind_check;
ALTER TABLE messages ADD CONSTRAINT messages_kind_check
    CHECK (kind IN ('chat', 'clarification', 'approval_request',
                    'approval_response', 'terminate'));
