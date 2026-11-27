-- Migration: account_scoped_registry_ops
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Account-scoped registry history (ADR 0020, step C5). A workflow, a field and a
-- data contract have a key that is unique only within an account, so their history
-- is kept per account: every entry names the account whose entity changed, and two
-- accounts with the same key never share history.
--
-- 1. Entries written before this rule may lack the account. An entry names it in
--    its snapshot when the entity had one; otherwise the entity belonged to the
--    installation account, the only owner registry catalogs had before C4. Entries
--    of other registry entities (agents, skills, tools, published blocks) take the
--    account their snapshot names; an entry without one stays a platform object's.
-- 2. A check keeps every later entry of a workflow, field or data contract owned.
--
-- Every statement is safe to run again and is a no-op on a fresh installation.
-- Rollback is described in ADR 0020.

UPDATE public.registry_changelog AS c
   SET account_id = COALESCE(
           (SELECT a.id
              FROM public.accounts AS a
             WHERE a.id = CASE
                       WHEN (c.snapshot ->> 'account_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                       THEN (c.snapshot ->> 'account_id')::uuid
                   END),
           '00000000-0000-0000-0000-000000000002'::uuid)
 WHERE c.account_id IS NULL
   AND c.entity_type IN ('workflow', 'field', 'data_contract');

UPDATE public.registry_changelog AS c
   SET account_id = a.id
  FROM public.accounts AS a
 WHERE c.account_id IS NULL
   AND c.entity_type NOT IN ('workflow', 'field', 'data_contract')
   AND a.id = CASE
           WHEN (c.snapshot ->> 'account_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           THEN (c.snapshot ->> 'account_id')::uuid
       END;

ALTER TABLE ONLY public.registry_changelog
    DROP CONSTRAINT IF EXISTS registry_changelog_account_keyed_owner_check;
ALTER TABLE ONLY public.registry_changelog
    ADD CONSTRAINT registry_changelog_account_keyed_owner_check CHECK (((entity_type <> ALL (ARRAY['workflow'::text, 'field'::text, 'data_contract'::text])) OR (account_id IS NOT NULL)));
