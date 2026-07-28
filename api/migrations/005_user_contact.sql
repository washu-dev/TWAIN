-- TWAIN async runner — per-user notification contact (Phase 2).
-- Idempotent: safe to run repeatedly.
--
-- Phase 2 reaches back out to the researcher when a run suspends (a clarification,
-- the heavy-calc confirmation, or the plan-approval gate). Until now the runner
-- could only notify one *configured* address/topic (TWAIN_NOTIFY_EMAIL / an SNS
-- topic) — not the person who actually owns the run. The owner's email already
-- lives on `users.email` (populated from Entra ID at login); this adds an optional
-- phone so the SNS/SMS backend can text that specific researcher.
--
-- The runner resolves the owner via conversations.id (== the engine session_id)
-- -> conversations.user_id -> users (see runner/db.py owner_contact), and falls
-- back to the configured default address when a user has no contact on file.

ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;  -- E.164, e.g. +13145550123
