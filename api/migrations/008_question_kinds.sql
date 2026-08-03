-- 008: give every gate that asks the researcher a question its own message kind.
--
-- CLARIFY, the heavy-calculation confirmation, the accept-or-rerun validation
-- gate and the BUILD gate's "what should change?" all posted kind='clarification'
-- and were told apart only by *position* — whichever question was most recent in
-- the transcript was assumed to be the asker's own. That held while there were
-- two of them and both were answered before the run moved on. It broke once a
-- question could be abandoned: re-running from the validation gate left its
-- question looking outstanding, so the next gate believed it had already asked
-- and suspended the run without posting anything, and the next thing the
-- researcher typed was consumed as plan-revision feedback.
--
-- Naming each one makes the gates identify their own question instead of
-- inferring it. Existing rows keep kind='clarification' and stay valid; they
-- simply lose the per-gate buttons in the UI, and typing still answers them.
-- Re-runnable: drop + re-add keeps this idempotent for dev.sh, which applies
-- every migration on each start.

ALTER TABLE messages DROP CONSTRAINT IF EXISTS messages_kind_check;
ALTER TABLE messages ADD CONSTRAINT messages_kind_check
    CHECK (kind IN ('chat', 'clarification', 'approval_request',
                    'approval_response', 'terminate',
                    'heavy_confirm', 'validation_gate', 'revision_request'));
