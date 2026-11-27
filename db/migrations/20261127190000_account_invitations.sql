-- Migration: account_invitations
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Invitations to join an account (ADR 0085, consent). An administrator adds directly
-- only people who already belong to an account the administrator manages; anyone
-- else is invited by email and becomes a member only when they accept. The invitation
-- is accepted with its single-use link: token_hash is the SHA-256 of the unguessable
-- token in that link (the token itself is never stored), and whoever opens the link
-- while signed in answers it. invited_email is the address as the administrator typed
-- it, shown in their list only; user_id is the person who answered. One pending
-- invitation per account and address (case-insensitive); a new one replaces its token.
-- Accepted, declined and cancelled invitations are kept as the record of the decision.
--
-- Every statement is safe to run again. The DDL is the end of
-- db/schema/001_schema_part_12.sql.

CREATE TABLE IF NOT EXISTS public.account_invitations (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    invited_email text NOT NULL,
    user_id uuid REFERENCES public.users(id) ON DELETE SET NULL,
    role text NOT NULL,
    invited_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
    token_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    CONSTRAINT account_invitations_token_hash_key UNIQUE (token_hash),
    CONSTRAINT account_invitations_email_check
        CHECK (invited_email <> '' AND length(invited_email) <= 320),
    CONSTRAINT account_invitations_token_hash_check CHECK (length(token_hash) = 64),
    CONSTRAINT account_invitations_role_check
        CHECK (role IN ('admin', 'operator', 'viewer')),
    CONSTRAINT account_invitations_status_check
        CHECK (status IN ('pending', 'accepted', 'declined', 'cancelled')),
    CONSTRAINT account_invitations_lifetime_check CHECK (expires_at > created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS account_invitations_pending_key
    ON public.account_invitations USING btree (account_id, lower(invited_email))
    WHERE status = 'pending';
