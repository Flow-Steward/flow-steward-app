-- Migration: secret_backends
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Secret backends (ADR 0066 amendment 2026-11-26): per-account
-- configurations of an external store a platform module registers, and the remote
-- locator of each reference whose value is kept there. Both tables stay empty
-- without such a module. The DDL is the end of db/schema/001_schema_part_12.sql.
-- Idempotent.
CREATE TABLE IF NOT EXISTS public.secret_backend_configs (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    backend_kind text NOT NULL CHECK (backend_kind ~ '^[a-z][a-z0-9_.-]{0,63}$' AND backend_kind <> 'local'),
    name text NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 200),
    non_secret_config jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(non_secret_config) = 'object'),
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text DEFAULT ''::text NOT NULL,
    CONSTRAINT secret_backend_configs_account_name_key UNIQUE (account_id, name),
    CONSTRAINT secret_backend_configs_id_account_kind_key UNIQUE (id, account_id, backend_kind)
);

CREATE TABLE IF NOT EXISTS public.secret_backend_locators (
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    ref_key text NOT NULL CHECK (char_length(ref_key) BETWEEN 1 AND 512),
    backend_config_id uuid NOT NULL,
    backend_kind text NOT NULL,
    locator jsonb NOT NULL CHECK (jsonb_typeof(locator) = 'object'),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    CONSTRAINT secret_backend_locators_pkey PRIMARY KEY (account_id, ref_key),
    CONSTRAINT secret_backend_locators_config_fkey FOREIGN KEY (backend_config_id, account_id, backend_kind)
        REFERENCES public.secret_backend_configs(id, account_id, backend_kind)
);

CREATE INDEX IF NOT EXISTS idx_secret_backend_locators_config
    ON public.secret_backend_locators USING btree (backend_config_id);
