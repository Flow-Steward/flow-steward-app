-- Migration: extension_install_requests
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Requests to install an extension (ADR 0084, "Requesting an extension"). Extension
-- code is installed once per installation, only by the installation administrator; an
-- account administrator of a company account asks for one catalog extension here
-- instead of outside the product. Installing it enables it for the asking account.
-- extension_id is the catalog id; extension_name is the catalog name when the request
-- was made. One pending request per account and extension; approved, declined and
-- cancelled requests are kept as the record of the decision.
--
-- Every statement is safe to run again. The DDL is the end of
-- db/schema/001_schema_part_12.sql.

CREATE TABLE IF NOT EXISTS public.extension_install_requests (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    extension_id text NOT NULL,
    extension_name text DEFAULT ''::text NOT NULL,
    requested_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
    note text DEFAULT ''::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    decided_by uuid REFERENCES public.users(id) ON DELETE SET NULL,
    decided_at timestamp with time zone,
    decline_reason text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT extension_install_requests_extension_id_check
        CHECK (extension_id <> '' AND length(extension_id) <= 200),
    CONSTRAINT extension_install_requests_extension_name_check
        CHECK (length(extension_name) <= 200),
    CONSTRAINT extension_install_requests_note_check CHECK (length(note) <= 500),
    CONSTRAINT extension_install_requests_decline_reason_check
        CHECK (length(decline_reason) <= 500),
    CONSTRAINT extension_install_requests_status_check
        CHECK (status IN ('pending', 'approved', 'declined', 'cancelled')),
    CONSTRAINT extension_install_requests_decided_check
        CHECK ((status = 'pending') = (decided_at IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS extension_install_requests_pending_key
    ON public.extension_install_requests USING btree (account_id, extension_id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_extension_install_requests_account
    ON public.extension_install_requests USING btree (account_id, created_at);
