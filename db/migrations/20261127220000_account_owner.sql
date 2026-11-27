-- Migration: account_owner
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- The company owner (ADR 0085, "Company owner"). Every account has one owner: a
-- person who is a member with the admin role there and who keeps the ultimate rights
-- (nobody else removes, demotes, deactivates or deletes them, or touches their
-- sign-in). The first active administrator of an account becomes its owner (the
-- trigger below); an existing account gets its earliest active administrator
-- membership, ties broken by the person's creation time and id. An account without
-- an active administrator keeps no owner until it gets one. The owner offers
-- ownership to another active administrator, who accepts or declines it
-- (account_owner_transfers); the installation administrator may reassign it.
--
-- Every statement is safe to run again. The DDL is the end of
-- db/schema/001_schema_part_12.sql.

ALTER TABLE ONLY public.accounts
    ADD COLUMN IF NOT EXISTS owner_user_id uuid REFERENCES public.users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_accounts_owner_user
    ON public.accounts USING btree (owner_user_id)
    WHERE owner_user_id IS NOT NULL;

UPDATE public.accounts AS a
   SET owner_user_id = first_admin.user_id
  FROM (
      SELECT DISTINCT ON (m.account_id)
             m.account_id,
             m.user_id
        FROM public.user_account_memberships AS m
        INNER JOIN public.roles AS r ON m.role_id = r.id
        INNER JOIN public.users AS u ON m.user_id = u.id
       WHERE r.key = 'admin' AND u.is_active AND u.deleted_at IS NULL
       ORDER BY m.account_id, m.created_at, u.created_at, u.id
  ) AS first_admin
 WHERE a.id = first_admin.account_id AND a.owner_user_id IS NULL;

CREATE OR REPLACE FUNCTION public.accounts_first_admin_becomes_owner() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.role_id IS NOT NULL AND EXISTS (
        SELECT 1
          FROM public.roles AS r
          JOIN public.users AS u ON u.id = NEW.user_id
         WHERE r.id = NEW.role_id
           AND r.key = 'admin'
           AND u.is_active
           AND u.deleted_at IS NULL
    ) THEN
        UPDATE public.accounts
           SET owner_user_id = NEW.user_id
         WHERE id = NEW.account_id AND owner_user_id IS NULL;
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS trg_user_account_memberships_first_admin_owner
    ON public.user_account_memberships;
CREATE TRIGGER trg_user_account_memberships_first_admin_owner
    AFTER INSERT OR UPDATE OF role_id ON public.user_account_memberships
    FOR EACH ROW EXECUTE FUNCTION public.accounts_first_admin_becomes_owner();

CREATE TABLE IF NOT EXISTS public.account_owner_transfers (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    from_user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    to_user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    decided_at timestamp with time zone,
    CONSTRAINT account_owner_transfers_people_check CHECK (from_user_id <> to_user_id),
    CONSTRAINT account_owner_transfers_status_check
        CHECK (status IN ('pending', 'accepted', 'declined', 'cancelled')),
    CONSTRAINT account_owner_transfers_decided_check
        CHECK ((status = 'pending') = (decided_at IS NULL)),
    CONSTRAINT account_owner_transfers_lifetime_check CHECK (expires_at > created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS account_owner_transfers_pending_key
    ON public.account_owner_transfers USING btree (account_id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_account_owner_transfers_recipient
    ON public.account_owner_transfers USING btree (to_user_id)
    WHERE status = 'pending';
