-- Migration: external_sign_in
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- External sign-in (ADR 0083): external identities keyed by issuer and subject
-- within the one account that linked them, single-use sign-in attempts of any redirecting sign-in
-- method, and whether each session was issued locally or by external sign-in. The
-- DDL is the end of db/schema/001_schema_part_12.sql. Existing sessions become
-- 'local'. Idempotent.
CREATE TABLE IF NOT EXISTS public.external_identities (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    method text NOT NULL,
    option_key text DEFAULT ''::text NOT NULL,
    issuer text NOT NULL,
    subject text NOT NULL,
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    last_login_at timestamp with time zone,
    CONSTRAINT external_identities_account_issuer_subject_key
        UNIQUE (account_id, issuer, subject)
);

CREATE INDEX IF NOT EXISTS idx_external_identities_user
    ON public.external_identities USING btree (user_id);

CREATE INDEX IF NOT EXISTS idx_external_identities_account
    ON public.external_identities USING btree (account_id);

CREATE TABLE IF NOT EXISTS public.external_sign_in_states (
    state_hash text PRIMARY KEY,
    method text NOT NULL,
    option_key text NOT NULL,
    browser_binding_hash text NOT NULL,
    method_data_encrypted text DEFAULT ''::text NOT NULL,
    redirect_uri text NOT NULL,
    return_path text DEFAULT '/'::text NOT NULL,
    link_user_id uuid REFERENCES public.users(id) ON DELETE CASCADE,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_external_sign_in_states_expires
    ON public.external_sign_in_states USING btree (expires_at);

ALTER TABLE ONLY public.auth_sessions
    ADD COLUMN IF NOT EXISTS auth_method text DEFAULT 'local'::text NOT NULL
        CONSTRAINT auth_sessions_auth_method_check
        CHECK (auth_method = ANY (ARRAY['local'::text, 'external'::text]));

ALTER TABLE ONLY public.auth_sessions
    ADD COLUMN IF NOT EXISTS external_identity_id uuid
        REFERENCES public.external_identities(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_auth_sessions_external_identity
    ON public.auth_sessions USING btree (external_identity_id)
    WHERE (external_identity_id IS NOT NULL);
