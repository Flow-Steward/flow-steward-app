-- Migration: registry_same_account_refs
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Same-account references (ADR 0020, step C4c). A workflow step names agents,
-- skills and published blocks that are platform objects (no account) or objects of
-- the step's own account; a hook binding names a project of its own account, and
-- each binding revision belongs to its binding's account and project.
--
-- An existing row that breaks one of these rules cannot be carried over without
-- guessing; the migration then stops with the table name before it changes
-- anything (ADR 0020 describes the manual fix). The DDL repeats db/schema/; every
-- statement is safe to run again and is a no-op on a fresh installation. Rollback
-- is described in ADR 0020.

-- 1. Stop when a row already crosses accounts.
DO $$
DECLARE
    offending bigint;
BEGIN
    SELECT count(*) INTO offending
      FROM public.registry_workflow_steps AS s
     WHERE (COALESCE(s.agent_id, '') <> '' AND EXISTS (
               SELECT 1 FROM public.registry_agents AS a
                WHERE a.agent_id = s.agent_id
                  AND a.account_id IS NOT NULL AND a.account_id <> s.account_id))
        OR (COALESCE(s.skill_id, '') <> '' AND EXISTS (
               SELECT 1 FROM public.registry_skills AS k
                WHERE k.skill_id = s.skill_id
                  AND k.account_id IS NOT NULL AND k.account_id <> s.account_id))
        OR EXISTS (
               SELECT 1 FROM public.registry_published_block_contracts AS b
                WHERE b.block_key IN (
                          s.block_key,
                          s.override ->> 'published_block_key',
                          s.override ->> 'block_key')
                  AND b.account_id IS NOT NULL AND b.account_id <> s.account_id);
    IF offending > 0 THEN
        RAISE EXCEPTION
            'same-account references: % row(s) of public.registry_workflow_steps name an agent, skill or published block of another account (ADR 0020, C4c)',
            offending;
    END IF;

    SELECT count(*) INTO offending
      FROM public.registry_workflow_hook_bindings AS hb
      JOIN public.projects AS p ON p.id = hb.project_id
     WHERE p.account_id IS DISTINCT FROM hb.account_id;
    IF offending > 0 THEN
        RAISE EXCEPTION
            'same-account references: % row(s) of public.registry_workflow_hook_bindings name a project of another account (ADR 0020, C4c)',
            offending;
    END IF;

    SELECT count(*) INTO offending
      FROM public.registry_workflow_hook_binding_revisions AS r
      JOIN public.registry_workflow_hook_bindings AS hb ON hb.id = r.binding_id
     WHERE (r.account_id, r.project_id) IS DISTINCT FROM (hb.account_id, hb.project_id);
    IF offending > 0 THEN
        RAISE EXCEPTION
            'same-account references: % row(s) of public.registry_workflow_hook_binding_revisions belong to another account or project than their binding (ADR 0020, C4c)',
            offending;
    END IF;
END;
$$;

-- 2. The rules, as db/schema/001_schema_part_12.sql states them.
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
               AND b.account_id IS NOT NULL AND b.account_id <> NEW.account_id
        ) THEN
            RAISE EXCEPTION 'step % names a published block of another account', NEW.step_id
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_registry_workflow_steps_same_account ON public.registry_workflow_steps;
CREATE TRIGGER trg_registry_workflow_steps_same_account
    BEFORE INSERT OR UPDATE OF account_id, agent_id, skill_id, block_key, override
    ON public.registry_workflow_steps
    FOR EACH ROW EXECUTE FUNCTION public.registry_workflow_steps_same_account();

ALTER TABLE ONLY public.registry_workflow_hook_bindings
    DROP CONSTRAINT IF EXISTS registry_workflow_hook_bindings_project_account_fkey;
ALTER TABLE ONLY public.registry_workflow_hook_bindings
    ADD CONSTRAINT registry_workflow_hook_bindings_project_account_fkey FOREIGN KEY (project_id, account_id)
    REFERENCES public.projects(id, account_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.registry_workflow_hook_binding_revisions
    DROP CONSTRAINT IF EXISTS registry_workflow_hook_binding_revisions_scope_fkey;
ALTER TABLE ONLY public.registry_workflow_hook_bindings
    DROP CONSTRAINT IF EXISTS registry_workflow_hook_bindings_id_scope_key;
ALTER TABLE ONLY public.registry_workflow_hook_bindings
    ADD CONSTRAINT registry_workflow_hook_bindings_id_scope_key UNIQUE (id, account_id, project_id);
ALTER TABLE ONLY public.registry_workflow_hook_binding_revisions
    ADD CONSTRAINT registry_workflow_hook_binding_revisions_scope_fkey FOREIGN KEY (binding_id, account_id, project_id)
    REFERENCES public.registry_workflow_hook_bindings(id, account_id, project_id) ON DELETE CASCADE;
