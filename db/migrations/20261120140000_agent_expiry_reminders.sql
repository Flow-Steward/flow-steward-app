-- Migration: agent_expiry_reminders
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- When the owner was reminded that an AI agent connection is about to stop working
-- (ADR 0076 §5). The DDL is the block at the end of db/schema/001_schema_part_12.sql,
-- written so that it is a no-op on a fresh installation, which already has both
-- columns. Existing tokens and grants start without a reminder sent.

ALTER TABLE public.personal_access_tokens
    ADD COLUMN IF NOT EXISTS expiry_reminder_sent_at timestamp with time zone;
ALTER TABLE public.oauth_grants
    ADD COLUMN IF NOT EXISTS expiry_reminder_sent_at timestamp with time zone;
