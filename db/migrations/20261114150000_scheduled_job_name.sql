-- Migration: scheduled_job_name
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- A schedule has a name (A56). Several schedules of one workflow (one per
-- supplier site, say) are told apart by it, and the jobs a schedule starts are
-- titled with it. Existing schedules get an empty name; the interface shows the
-- workflow for them until someone names them.
--
-- The column is the one db/schema/001_schema_part_04.sql declares on
-- public.scheduled_jobs, with IF NOT EXISTS added: a fresh installation (which
-- already has it) is unchanged, and an upgraded one gets the same column.
ALTER TABLE public.scheduled_jobs
    ADD COLUMN IF NOT EXISTS name text DEFAULT ''::text NOT NULL;
