-- Migration: connection_poll_delivery
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Crash-safe connection polls for any extension. The DDL is the
-- two project_connections columns in db/schema/001_schema_part_02.sql and the
-- block at the end of db/schema/001_schema_part_12.sql, made idempotent: a fresh
-- installation is unchanged and an upgraded one gets the same objects. Existing
-- connections start at state_version 0 and config_generation 1.
ALTER TABLE ONLY public.project_connections ADD COLUMN IF NOT EXISTS state_version bigint DEFAULT 0 NOT NULL;
ALTER TABLE ONLY public.project_connections ADD COLUMN IF NOT EXISTS config_generation bigint DEFAULT 1 NOT NULL;

-- A connection_poll result is judged against its connection as it is now. The
-- poll transaction locks the connection row, compares the state_version and
-- config_generation the poll started from, and writes the events, their
-- dispatch records and the cursor together. config_generation moves whenever
-- the polled configuration changes: the connection is enabled or disabled,
-- changes type, gets other settings (the oauth lifecycle block excluded, which
-- every token refresh rewrites) or is reauthorized. Every such UPDATE holds the
-- same row lock as the poll transaction.
CREATE OR REPLACE FUNCTION public.project_connection_bump_config_generation() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog
    AS $$
BEGIN
    IF NEW.config_generation = OLD.config_generation AND (
        NEW.enabled IS DISTINCT FROM OLD.enabled
        OR NEW.connection_type IS DISTINCT FROM OLD.connection_type
        OR (NEW.connection_config - 'oauth') IS DISTINCT FROM (OLD.connection_config - 'oauth')
        OR NEW.oauth_authorization_revision IS DISTINCT FROM OLD.oauth_authorization_revision
    ) THEN
        NEW.config_generation := OLD.config_generation + 1;
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_project_connections_config_generation
    BEFORE UPDATE ON public.project_connections
    FOR EACH ROW
    EXECUTE FUNCTION public.project_connection_bump_config_generation();

-- One row per provider event, unique on its provider-given source_id. A replayed
-- poll page writes no second row and no new dispatch records.
CREATE TABLE IF NOT EXISTS public.provider_event_inbox (
    event_row_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    provider_key text NOT NULL CHECK (btrim(provider_key) <> ''::text),
    source_id text NOT NULL CHECK (char_length(source_id) BETWEEN 1 AND 512),
    event_type text NOT NULL CHECK (btrim(event_type) <> ''::text),
    payload jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'::text),
    connection_id text,
    config_generation bigint,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT provider_event_inbox_source_key UNIQUE (project_id, provider_key, source_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_event_inbox_received ON public.provider_event_inbox USING btree (received_at);

-- One row per workflow a provider event starts, keyed like its delivery
-- (project|workflow|task). A job is created, the delivery recorded and the row
-- marked accepted in one transaction; a periodic sweep retries pending rows.
CREATE TABLE IF NOT EXISTS public.provider_event_dispatches (
    delivery_key text PRIMARY KEY CHECK (char_length(delivery_key) BETWEEN 1 AND 1024),
    event_row_id uuid NOT NULL REFERENCES public.provider_event_inbox(event_row_id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    provider_key text NOT NULL,
    workflow_id text NOT NULL,
    trigger_id text NOT NULL,
    connection_id text,
    config_generation bigint,
    status text DEFAULT 'pending'::text NOT NULL,
    job_id uuid,
    reason text DEFAULT ''::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT provider_event_dispatches_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'accepted'::text, 'failed'::text])))
);

CREATE INDEX IF NOT EXISTS idx_provider_event_dispatches_pending ON public.provider_event_dispatches USING btree (updated_at) WHERE (status = 'pending'::text);
CREATE INDEX IF NOT EXISTS idx_provider_event_dispatches_event ON public.provider_event_dispatches USING btree (event_row_id);
