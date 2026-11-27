-- Migration: account_scoped_block_keys
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Published block keys per account (ADR 0020). A block derived from an account's
-- agent or skill is that account's, so two accounts may each publish a block with
-- the same key; a platform block (no account) keeps a key no account block uses and
-- every account may read it.
--
-- 1. owner_key (the block's account as text, empty for a platform block) joins the
--    key in the primary key: (owner_key, block_key).
-- 2. Inputs, outputs and runtime dependencies name the block by both columns
--    (block_owner, block_key); older rows take the owner of the one block their key
--    named. A runtime dependency that named a block of another account keeps its
--    dependency_ref and loses only the link (block_key becomes NULL).
-- 3. A key is a platform key or an account key, never both, so a reader that resolves
--    a key within an account (its own block, else a platform block) finds one block.
-- 4. A step may name a block key its account can see: its own or a platform block.
-- 5. The platform-or-account rule of item 3 is checked under a transaction advisory
--    lock on the key, so two concurrent writers of one key cannot both pass; a
--    runtime dependency that names a block_key always names its block_owner (the
--    composite foreign key is not checked while one of its columns is NULL).
--
-- Every statement is safe to run again and is a no-op on a fresh installation. The
-- migration deletes no row. Rollback is described in ADR 0020.

ALTER TABLE ONLY public.registry_published_block_contracts
    ADD COLUMN IF NOT EXISTS owner_key text GENERATED ALWAYS AS (COALESCE((account_id)::text, ''::text)) STORED;

ALTER TABLE ONLY public.registry_published_block_inputs
    ADD COLUMN IF NOT EXISTS block_owner text DEFAULT ''::text NOT NULL;

ALTER TABLE ONLY public.registry_published_block_outputs
    ADD COLUMN IF NOT EXISTS block_owner text DEFAULT ''::text NOT NULL;

ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD COLUMN IF NOT EXISTS block_owner text;

UPDATE public.registry_published_block_inputs AS i
   SET block_owner = b.owner_key
  FROM public.registry_published_block_contracts AS b
 WHERE b.block_key = i.block_key
   AND NOT EXISTS (
       SELECT 1 FROM public.registry_published_block_contracts AS own
        WHERE own.block_key = i.block_key AND own.owner_key = i.block_owner);

UPDATE public.registry_published_block_outputs AS o
   SET block_owner = b.owner_key
  FROM public.registry_published_block_contracts AS b
 WHERE b.block_key = o.block_key
   AND NOT EXISTS (
       SELECT 1 FROM public.registry_published_block_contracts AS own
        WHERE own.block_key = o.block_key AND own.owner_key = o.block_owner);

UPDATE public.workflow_runtime_dependencies AS d
   SET block_owner = b.owner_key
  FROM public.registry_published_block_contracts AS b
 WHERE d.block_key IS NOT NULL
   AND d.block_owner IS NULL
   AND b.block_key = d.block_key
   AND (b.account_id IS NULL OR b.account_id = d.account_id);

UPDATE public.workflow_runtime_dependencies
   SET block_key = NULL
 WHERE block_key IS NOT NULL
   AND block_owner IS NULL;

ALTER TABLE ONLY public.registry_published_block_inputs
    DROP CONSTRAINT IF EXISTS registry_published_block_inputs_block_key_fkey;
ALTER TABLE ONLY public.registry_published_block_inputs
    DROP CONSTRAINT IF EXISTS registry_published_block_inputs_block_fkey;
ALTER TABLE ONLY public.registry_published_block_outputs
    DROP CONSTRAINT IF EXISTS registry_published_block_outputs_block_key_fkey;
ALTER TABLE ONLY public.registry_published_block_outputs
    DROP CONSTRAINT IF EXISTS registry_published_block_outputs_block_fkey;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    DROP CONSTRAINT IF EXISTS workflow_runtime_dependencies_block_key_fkey;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    DROP CONSTRAINT IF EXISTS workflow_runtime_dependencies_block_fkey;
ALTER TABLE ONLY public.registry_published_block_inputs
    DROP CONSTRAINT IF EXISTS registry_published_block_inputs_block_key_field_id_key;
ALTER TABLE ONLY public.registry_published_block_inputs
    DROP CONSTRAINT IF EXISTS registry_published_block_inputs_block_field_key;
ALTER TABLE ONLY public.registry_published_block_outputs
    DROP CONSTRAINT IF EXISTS registry_published_block_outputs_block_key_field_id_key;
ALTER TABLE ONLY public.registry_published_block_outputs
    DROP CONSTRAINT IF EXISTS registry_published_block_outputs_block_field_key;
ALTER TABLE ONLY public.registry_published_block_contracts
    DROP CONSTRAINT IF EXISTS registry_published_block_contracts_pkey;

ALTER TABLE ONLY public.registry_published_block_contracts
    ADD CONSTRAINT registry_published_block_contracts_pkey PRIMARY KEY (owner_key, block_key);
ALTER TABLE ONLY public.registry_published_block_inputs
    ADD CONSTRAINT registry_published_block_inputs_block_field_key UNIQUE (block_owner, block_key, field_id);
ALTER TABLE ONLY public.registry_published_block_outputs
    ADD CONSTRAINT registry_published_block_outputs_block_field_key UNIQUE (block_owner, block_key, field_id);
ALTER TABLE ONLY public.registry_published_block_inputs
    ADD CONSTRAINT registry_published_block_inputs_block_fkey FOREIGN KEY (block_owner, block_key) REFERENCES public.registry_published_block_contracts(owner_key, block_key) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.registry_published_block_outputs
    ADD CONSTRAINT registry_published_block_outputs_block_fkey FOREIGN KEY (block_owner, block_key) REFERENCES public.registry_published_block_contracts(owner_key, block_key) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_block_fkey FOREIGN KEY (block_owner, block_key) REFERENCES public.registry_published_block_contracts(owner_key, block_key) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    DROP CONSTRAINT IF EXISTS workflow_runtime_dependencies_block_owner_check;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_block_owner_check CHECK (((block_key IS NULL) OR (block_owner IS NOT NULL)));

CREATE OR REPLACE FUNCTION public.registry_published_block_key_scope() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    -- Writers of one key queue on a transaction lock, so the second one checks
    -- after the first committed (READ COMMITTED: each statement sees the commits
    -- before it) and two concurrent writers cannot both pass.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('flow-steward:published-block-key:' || NEW.block_key, 0));
    IF EXISTS (
        SELECT 1 FROM public.registry_published_block_contracts AS b
         WHERE b.block_key = NEW.block_key AND b.account_id IS NULL
    ) AND EXISTS (
        SELECT 1 FROM public.registry_published_block_contracts AS b
         WHERE b.block_key = NEW.block_key AND b.account_id IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'published block key % is used by a platform block and an account block', NEW.block_key
            USING ERRCODE = 'unique_violation';
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_registry_published_block_key_scope ON public.registry_published_block_contracts;
CREATE TRIGGER trg_registry_published_block_key_scope
    AFTER INSERT OR UPDATE OF account_id, block_key
    ON public.registry_published_block_contracts
    FOR EACH ROW EXECUTE FUNCTION public.registry_published_block_key_scope();

CREATE OR REPLACE FUNCTION public.registry_workflow_steps_same_account() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    block text;
BEGIN
    IF COALESCE(NEW.agent_id, '') <> '' AND EXISTS (
        SELECT 1 FROM public.registry_agents AS a
         WHERE a.agent_id = NEW.agent_id
           AND a.account_id IS NOT NULL AND a.account_id <> NEW.account_id
    ) THEN
        RAISE EXCEPTION 'step % names an agent of another account', NEW.step_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF COALESCE(NEW.skill_id, '') <> '' AND EXISTS (
        SELECT 1 FROM public.registry_skills AS s
         WHERE s.skill_id = NEW.skill_id
           AND s.account_id IS NOT NULL AND s.account_id <> NEW.account_id
    ) THEN
        RAISE EXCEPTION 'step % names a skill of another account', NEW.step_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    FOREACH block IN ARRAY ARRAY[
        NEW.block_key,
        NEW.override ->> 'published_block_key',
        NEW.override ->> 'block_key'
    ] LOOP
        IF COALESCE(block, '') <> '' AND EXISTS (
            SELECT 1 FROM public.registry_published_block_contracts AS b
             WHERE b.block_key = block
        ) AND NOT EXISTS (
            SELECT 1 FROM public.registry_published_block_contracts AS b
             WHERE b.block_key = block
               AND (b.account_id IS NULL OR b.account_id = NEW.account_id)
        ) THEN
            RAISE EXCEPTION 'step % names a published block of another account', NEW.step_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;
