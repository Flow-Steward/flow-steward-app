-- Migration: inbound_oauth_tables
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- Transaction control:
--   -- transaction: true   (default) the runner wraps this file in a
--                          single transaction together with the tracking
--                          insert; any failure rolls the whole migration back.
--   -- transaction: false  use for statements that cannot run inside a
--                          transaction block, e.g. CREATE INDEX CONCURRENTLY.
--                          The runner records durable in-progress state first;
--                          interrupted outcomes require manual reconciliation.
-- transaction: true

-- Inbound OAuth for the MCP server (ADR 0047), for databases installed before 0.9.7.
--
-- 0.9.7 added these five tables, their constraints and their indexes to the
-- db/schema/ baseline only. The baseline is loaded once, into an empty database, so
-- a database installed by 0.9.6 or earlier never received them and inbound OAuth
-- reported storage_missing after the update.
--
-- The statements below are the baseline's own DDL, byte for byte, with IF NOT EXISTS
-- added. A fresh installation creates the tables from db/schema/ and then runs this
-- migration, which changes nothing there; an upgraded installation gets exactly the
-- objects a fresh one has. Every constraint is declared inside its CREATE TABLE, so
-- it exists precisely when its table does. Keep this file and the baseline block in
-- 001_schema_part_12.sql identical apart from IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS public.oauth_clients (
    client_id text PRIMARY KEY CHECK (char_length(client_id) BETWEEN 1 AND 2048),
    client_name text DEFAULT ''::text NOT NULL,
    redirect_uris_jsonb jsonb NOT NULL CHECK (jsonb_typeof(redirect_uris_jsonb) = 'array'),
    metadata_sha256 text NOT NULL,
    fetched_at timestamp with time zone NOT NULL,
    cache_expires_at timestamp with time zone NOT NULL,
    first_seen_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS public.oauth_grants (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    account_id uuid NOT NULL,
    client_id text NOT NULL REFERENCES public.oauth_clients(client_id) ON DELETE RESTRICT,
    client_name_snapshot text NOT NULL,
    resource text NOT NULL,
    preset text NOT NULL,
    scopes_jsonb jsonb NOT NULL CHECK (jsonb_typeof(scopes_jsonb) = 'array'),
    allowed_project_ids_jsonb jsonb NOT NULL CHECK (jsonb_typeof(allowed_project_ids_jsonb) = 'array'),
    allowed_workflow_ids_jsonb jsonb DEFAULT '[]'::jsonb NOT NULL
        CHECK (jsonb_typeof(allowed_workflow_ids_jsonb) = 'array'),
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    revoked_reason text,
    CONSTRAINT chk_oauth_grants_lifetime CHECK (
        expires_at > created_at AND expires_at <= created_at + interval '90 days'
    )
);

CREATE TABLE IF NOT EXISTS public.oauth_authorizations (
    id uuid PRIMARY KEY,
    client_id text NOT NULL REFERENCES public.oauth_clients(client_id) ON DELETE CASCADE,
    redirect_uri text NOT NULL,
    resource text NOT NULL,
    code_challenge text NOT NULL,
    state text,
    requested_scopes_jsonb jsonb NOT NULL CHECK (jsonb_typeof(requested_scopes_jsonb) = 'array'),
    browser_binding_hash text NOT NULL,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    decided_at timestamp with time zone,
    decision text CHECK (decision IN ('approved', 'denied')),
    user_id uuid REFERENCES public.users(id) ON DELETE CASCADE,
    account_id uuid,
    preset text,
    scopes_jsonb jsonb,
    allowed_project_ids_jsonb jsonb,
    grant_expires_at timestamp with time zone,
    code_hash text UNIQUE,
    code_expires_at timestamp with time zone,
    code_consumed_at timestamp with time zone,
    grant_id uuid REFERENCES public.oauth_grants(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS public.oauth_access_tokens (
    id uuid PRIMARY KEY,
    token_hash text NOT NULL UNIQUE,
    grant_id uuid NOT NULL REFERENCES public.oauth_grants(id) ON DELETE CASCADE,
    scopes_jsonb jsonb NOT NULL CHECK (jsonb_typeof(scopes_jsonb) = 'array'),
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL
);

CREATE TABLE IF NOT EXISTS public.oauth_refresh_tokens (
    id uuid PRIMARY KEY,
    token_hash text NOT NULL UNIQUE,
    grant_id uuid NOT NULL REFERENCES public.oauth_grants(id) ON DELETE CASCADE,
    parent_id uuid REFERENCES public.oauth_refresh_tokens(id) ON DELETE SET NULL,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone
);

CREATE INDEX IF NOT EXISTS idx_oauth_grants_user_revoked ON public.oauth_grants (user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_oauth_access_tokens_grant ON public.oauth_access_tokens (grant_id);
CREATE INDEX IF NOT EXISTS idx_oauth_refresh_tokens_grant ON public.oauth_refresh_tokens (grant_id);
CREATE INDEX IF NOT EXISTS idx_oauth_authorizations_expires ON public.oauth_authorizations (expires_at);
