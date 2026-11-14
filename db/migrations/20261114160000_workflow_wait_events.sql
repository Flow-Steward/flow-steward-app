-- Migration: workflow_wait_events
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Wait for an event (ADR 0071). Events an extension delivers to waiting runs
-- (resume_waits) are kept here until a run that waits for the same account,
-- project, source, correlation key and event type takes them. Two partial
-- indexes find runs parked as awaiting_event by key and by deadline.
--
-- The DDL is the block at the end of db/schema/001_schema_part_12.sql with
-- IF NOT EXISTS added: a fresh installation (which already has the objects) is
-- unchanged, and an upgraded one gets the same objects. No existing data moves.
CREATE TABLE IF NOT EXISTS public.workflow_wait_events (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    source text NOT NULL CHECK (btrim(source) <> ''::text),
    extension_install_id text DEFAULT ''::text NOT NULL,
    correlation_key text NOT NULL CHECK (char_length(correlation_key) BETWEEN 1 AND 256),
    event_type text NOT NULL CHECK (event_type ~ '^[a-z0-9][a-z0-9_.:-]{0,127}$'::text),
    payload jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'::text),
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    consumed_at timestamp with time zone,
    consumed_job_id uuid,
    consumed_wait_id text,
    CONSTRAINT workflow_wait_events_delivery_key
        UNIQUE (project_id, source, correlation_key, event_type)
);

CREATE INDEX IF NOT EXISTS idx_workflow_wait_events_open
    ON public.workflow_wait_events (project_id, source, correlation_key)
    WHERE consumed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_workflow_wait_events_received
    ON public.workflow_wait_events (received_at);
CREATE INDEX IF NOT EXISTS idx_jobs_workflow_wait_event_key
    ON public.jobs USING btree (((meta -> 'workflow_wait'::text) ->> 'correlation_key'::text))
    WHERE (status = 'awaiting_event'::text);
CREATE INDEX IF NOT EXISTS idx_jobs_workflow_wait_event_due
    ON public.jobs USING btree (((((meta -> 'workflow_wait'::text) ->> 'due_epoch_ms'::text))::bigint), id)
    WHERE (status = 'awaiting_event'::text);
