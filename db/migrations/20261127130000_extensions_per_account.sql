-- Migration: extensions_per_account
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Extensions per account (ADR 0020, step C6): the per-account enablement of an
-- extension installed once per installation, with that account's non-secret
-- settings. The table starts empty: every existing install keeps serving the
-- account that owns it, exactly as before. The DDL is the end of
-- db/schema/001_schema_part_12.sql. Idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS ux_extension_installs_install_extension
    ON public.extension_installs USING btree (install_id, extension_id);

CREATE TABLE IF NOT EXISTS public.extension_account_enablements (
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    extension_id text NOT NULL,
    install_id text NOT NULL,
    state text DEFAULT 'enabled'::text NOT NULL CHECK (state IN ('enabled', 'disabled')),
    settings_jsonb jsonb DEFAULT '{}'::jsonb NOT NULL CHECK (jsonb_typeof(settings_jsonb) = 'object'),
    enabled_at timestamp with time zone,
    enabled_by text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by text DEFAULT ''::text NOT NULL,
    CONSTRAINT extension_account_enablements_pkey PRIMARY KEY (account_id, extension_id),
    CONSTRAINT extension_account_enablements_install_fkey FOREIGN KEY (install_id, extension_id)
        REFERENCES public.extension_installs(install_id, extension_id)
        ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_extension_account_enablements_install
    ON public.extension_account_enablements USING btree (install_id, account_id)
    WHERE (state = 'enabled'::text);
