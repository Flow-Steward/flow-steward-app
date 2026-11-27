-- Migration: roles_per_account
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Roles per account (ADR 0085). A person's role is held on the account membership,
-- and the role of the account a request acts in applies.
--
-- 1. user_account_memberships.role_id names the person's one system role in that
--    account; NULL grants nothing.
-- 2. Every live person gets a membership of their home account (users.account_id)
--    when they had none, so nobody loses the access the home account gave them.
-- 3. Every membership without a role gets the person's highest global system role
--    (admin > operator > viewer). A person with no global system role keeps NULL (no
--    access); the notice below reports how many memberships that is.
-- 4. Only after the backfill, the global user_roles rows (no workspace, project or
--    workflow) are deleted in the same transaction, and a check refuses a new one:
--    user_roles keeps scoped rows only. The notice reports global rows of roles that
--    are not system roles, which have no membership equivalent.
-- 5. scoped_role_users is keyed (user_id, account_id): a person can be limited in
--    one account and unrestricted in another.
--
-- Every statement is safe to run again: the backfill touches memberships without a
-- role only, and the global rows it reads are gone after the first run. The DDL is
-- the end of db/schema/001_schema_part_12.sql. Rollback is described in ADR 0085.

ALTER TABLE ONLY public.user_account_memberships
    ADD COLUMN IF NOT EXISTS role_id uuid;

ALTER TABLE ONLY public.user_account_memberships
    DROP CONSTRAINT IF EXISTS user_account_memberships_role_id_fkey;
ALTER TABLE ONLY public.user_account_memberships
    ADD CONSTRAINT user_account_memberships_role_id_fkey FOREIGN KEY (role_id)
        REFERENCES public.roles(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_user_account_memberships_role
    ON public.user_account_memberships USING btree (account_id, role_id);

INSERT INTO public.user_account_memberships (user_id, account_id, is_default)
SELECT u.id,
       u.account_id,
       NOT EXISTS (
           SELECT 1 FROM public.user_account_memberships AS d
            WHERE d.user_id = u.id AND d.is_default)
  FROM public.users AS u
  JOIN public.accounts AS a ON a.id = u.account_id
 WHERE u.deleted_at IS NULL
   AND NOT EXISTS (
       SELECT 1 FROM public.user_account_memberships AS m
        WHERE m.user_id = u.id AND m.account_id = u.account_id)
ON CONFLICT (user_id, account_id) DO NOTHING;

UPDATE public.user_account_memberships AS m
   SET role_id = best.role_id
  FROM (
      SELECT DISTINCT ON (ur.user_id) ur.user_id, ur.role_id
        FROM public.user_roles AS ur
        JOIN public.roles AS r ON r.id = ur.role_id
       WHERE ur.workspace_id IS NULL
         AND ur.project_id IS NULL
         AND ur.workflow_id IS NULL
         AND r.key IN ('admin', 'operator', 'viewer')
       ORDER BY ur.user_id,
                CASE r.key WHEN 'admin' THEN 3 WHEN 'operator' THEN 2 ELSE 1 END DESC
  ) AS best
 WHERE best.user_id = m.user_id
   AND m.role_id IS NULL;

DO $$
DECLARE
    without_role bigint;
    other_global bigint;
BEGIN
    SELECT count(*) INTO without_role
      FROM public.user_account_memberships AS m
      JOIN public.users AS u ON u.id = m.user_id
     WHERE m.role_id IS NULL
       AND u.deleted_at IS NULL;
    SELECT count(*) INTO other_global
      FROM public.user_roles AS ur
      JOIN public.roles AS r ON r.id = ur.role_id
     WHERE ur.workspace_id IS NULL
       AND ur.project_id IS NULL
       AND ur.workflow_id IS NULL
       AND r.key NOT IN ('admin', 'operator', 'viewer');
    RAISE NOTICE 'roles_per_account: % membership(s) of live people have no role (no access); % global row(s) of other roles removed',
        without_role, other_global;
END;
$$;

DELETE FROM public.user_roles AS ur
 WHERE ur.workspace_id IS NULL
   AND ur.project_id IS NULL
   AND ur.workflow_id IS NULL;

ALTER TABLE ONLY public.user_roles
    DROP CONSTRAINT IF EXISTS user_roles_scoped_only_check;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_scoped_only_check
        CHECK (((workspace_id IS NOT NULL) OR (project_id IS NOT NULL) OR (workflow_id IS NOT NULL)));

ALTER TABLE ONLY public.scoped_role_users
    DROP CONSTRAINT IF EXISTS scoped_role_users_pkey;
ALTER TABLE ONLY public.scoped_role_users
    ADD CONSTRAINT scoped_role_users_pkey PRIMARY KEY (user_id, account_id);
