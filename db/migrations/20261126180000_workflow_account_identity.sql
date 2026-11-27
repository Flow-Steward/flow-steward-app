-- Migration: workflow_account_identity
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Workflow identity is account-qualified (ADR 0020, step C4b). Every workflow has an
-- owning account: an existing row without one is given to the account of the
-- extension install that projected it, otherwise to the installation's default
-- account. A workflow key is unique within its account, so two accounts may use the
-- same key. Definition rows (Groups, For each containers, steps), project bindings,
-- schedules, entrypoints, reactions, runtime dependencies, notification routes,
-- secret bindings and approvals, event and invocation logs, workflow-scoped role
-- assignments and approval step approvers name the workflow's account next to its
-- id, and the database refuses a reference to another account's workflow.
--
-- An existing row whose account differs from its workflow's account cannot be
-- carried over without guessing; the migration then stops with the table name
-- before it changes anything that matters (ADR 0020 describes the manual fix). The
-- DDL repeats db/schema/; every statement is safe to run again and is a no-op on a
-- fresh installation. Rollback is described in ADR 0020.

-- 1. Every workflow has an owning account.
UPDATE public.registry_workflows w
   SET account_id = i.account_id
  FROM public.extension_installs i
 WHERE w.account_id IS NULL AND w.source_install_id IS NOT NULL
   AND i.install_id = w.source_install_id;
UPDATE public.registry_workflows
   SET account_id = '00000000-0000-0000-0000-000000000002'::uuid
 WHERE account_id IS NULL;
ALTER TABLE ONLY public.registry_workflows
    ALTER COLUMN account_id SET DEFAULT '00000000-0000-0000-0000-000000000002'::uuid;
ALTER TABLE ONLY public.registry_workflows ALTER COLUMN account_id SET NOT NULL;

-- 2. Rows that reference a workflow and carry no account yet get one.
ALTER TABLE ONLY public.registry_workflow_groups ADD COLUMN IF NOT EXISTS account_id uuid;
ALTER TABLE ONLY public.registry_workflow_for_each ADD COLUMN IF NOT EXISTS account_id uuid;
ALTER TABLE ONLY public.registry_workflow_steps ADD COLUMN IF NOT EXISTS account_id uuid;
ALTER TABLE ONLY public.project_workflows ADD COLUMN IF NOT EXISTS account_id uuid;
ALTER TABLE ONLY public.approval_step_approvers ADD COLUMN IF NOT EXISTS account_id uuid;
ALTER TABLE ONLY public.job_reaction_events ADD COLUMN IF NOT EXISTS account_id uuid;
ALTER TABLE ONLY public.user_roles ADD COLUMN IF NOT EXISTS workflow_account_id uuid;

UPDATE public.registry_workflow_groups t
   SET account_id = w.account_id
  FROM public.registry_workflows w
 WHERE t.account_id IS NULL AND w.workflow_id = t.workflow_id;
UPDATE public.registry_workflow_for_each t
   SET account_id = w.account_id
  FROM public.registry_workflows w
 WHERE t.account_id IS NULL AND w.workflow_id = t.workflow_id;
UPDATE public.registry_workflow_steps t
   SET account_id = w.account_id
  FROM public.registry_workflows w
 WHERE t.account_id IS NULL AND w.workflow_id = t.owner_workflow_id;
UPDATE public.project_workflows t
   SET account_id = p.account_id
  FROM public.projects p
 WHERE t.account_id IS NULL AND p.id = t.project_id;
UPDATE public.approval_step_approvers t
   SET account_id = w.account_id
  FROM public.registry_workflows w
 WHERE t.account_id IS NULL AND w.workflow_id = t.workflow_id;
UPDATE public.job_reaction_events t
   SET account_id = j.account_id
  FROM public.jobs j
 WHERE t.account_id IS NULL AND j.id = t.job_id;
UPDATE public.job_reaction_events t
   SET account_id = w.account_id
  FROM public.registry_workflows w
 WHERE t.account_id IS NULL AND t.workflow_id IS NOT NULL AND w.workflow_id = t.workflow_id;
UPDATE public.user_roles t
   SET workflow_account_id = p.account_id
  FROM public.projects p
 WHERE t.workflow_account_id IS NULL AND t.workflow_id IS NOT NULL AND p.id = t.project_id;
UPDATE public.scheduled_jobs t
   SET account_id = p.account_id
  FROM public.projects p
 WHERE t.account_id IS NULL AND p.id = t.project_id;
UPDATE public.workflow_runtime_dependencies t
   SET account_id = w.account_id
  FROM public.registry_workflows w
 WHERE t.account_id IS NULL AND w.workflow_id = t.workflow_id;
UPDATE public.registry_workflow_groups
   SET account_id = '00000000-0000-0000-0000-000000000002'::uuid
 WHERE account_id IS NULL;
UPDATE public.registry_workflow_for_each
   SET account_id = '00000000-0000-0000-0000-000000000002'::uuid
 WHERE account_id IS NULL;
UPDATE public.registry_workflow_steps
   SET account_id = '00000000-0000-0000-0000-000000000002'::uuid
 WHERE account_id IS NULL;
UPDATE public.approval_step_approvers
   SET account_id = '00000000-0000-0000-0000-000000000002'::uuid
 WHERE account_id IS NULL;
UPDATE public.workflow_runtime_dependencies
   SET account_id = '00000000-0000-0000-0000-000000000002'::uuid
 WHERE account_id IS NULL;

ALTER TABLE ONLY public.registry_workflow_groups
    ALTER COLUMN account_id SET DEFAULT '00000000-0000-0000-0000-000000000002'::uuid;
ALTER TABLE ONLY public.registry_workflow_groups ALTER COLUMN account_id SET NOT NULL;
ALTER TABLE ONLY public.registry_workflow_for_each
    ALTER COLUMN account_id SET DEFAULT '00000000-0000-0000-0000-000000000002'::uuid;
ALTER TABLE ONLY public.registry_workflow_for_each ALTER COLUMN account_id SET NOT NULL;
ALTER TABLE ONLY public.registry_workflow_steps
    ALTER COLUMN account_id SET DEFAULT '00000000-0000-0000-0000-000000000002'::uuid;
ALTER TABLE ONLY public.registry_workflow_steps ALTER COLUMN account_id SET NOT NULL;
ALTER TABLE ONLY public.project_workflows ALTER COLUMN account_id SET NOT NULL;
ALTER TABLE ONLY public.approval_step_approvers
    ALTER COLUMN account_id SET DEFAULT '00000000-0000-0000-0000-000000000002'::uuid;
ALTER TABLE ONLY public.approval_step_approvers ALTER COLUMN account_id SET NOT NULL;
ALTER TABLE ONLY public.scheduled_jobs ALTER COLUMN account_id SET NOT NULL;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    ALTER COLUMN account_id SET DEFAULT '00000000-0000-0000-0000-000000000002'::uuid;
ALTER TABLE ONLY public.workflow_runtime_dependencies ALTER COLUMN account_id SET NOT NULL;

-- A project binding takes its project's account when the writer names none.
CREATE OR REPLACE FUNCTION public.project_workflows_copy_account() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.account_id IS NULL THEN
        SELECT p.account_id INTO NEW.account_id FROM public.projects AS p WHERE p.id = NEW.project_id;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_project_workflows_account ON public.project_workflows;
CREATE TRIGGER trg_project_workflows_account
    BEFORE INSERT OR UPDATE OF project_id, account_id ON public.project_workflows
    FOR EACH ROW EXECUTE FUNCTION public.project_workflows_copy_account();

-- A workflow-scoped role assignment names its project's account for the workflow.
CREATE OR REPLACE FUNCTION public.user_roles_copy_workflow_account() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.workflow_id IS NULL THEN
        NEW.workflow_account_id := NULL;
    ELSE
        SELECT p.account_id INTO NEW.workflow_account_id
          FROM public.projects AS p WHERE p.id = NEW.project_id;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_user_roles_workflow_account ON public.user_roles;
CREATE TRIGGER trg_user_roles_workflow_account
    BEFORE INSERT OR UPDATE OF project_id, workflow_id, workflow_account_id ON public.user_roles
    FOR EACH ROW EXECUTE FUNCTION public.user_roles_copy_workflow_account();

-- 3. Stop before any constraint changes when a row names another account's workflow.
DO $$
DECLARE
    spec text[];
    offending bigint;
BEGIN
    FOREACH spec SLICE 1 IN ARRAY ARRAY[
        ARRAY['registry_workflow_groups', 'account_id', 'workflow_id'],
        ARRAY['registry_workflow_for_each', 'account_id', 'workflow_id'],
        ARRAY['registry_workflow_steps', 'account_id', 'owner_workflow_id'],
        ARRAY['project_workflows', 'account_id', 'workflow_id'],
        ARRAY['scheduled_jobs', 'account_id', 'workflow_id'],
        ARRAY['workflow_entrypoints', 'account_id', 'workflow_id'],
        ARRAY['workflow_reactions', 'account_id', 'workflow_id'],
        ARRAY['workflow_runtime_dependencies', 'account_id', 'workflow_id'],
        ARRAY['notification_routes', 'account_id', 'workflow_id'],
        ARRAY['catalog_secret_bindings', 'account_id', 'workflow_id'],
        ARRAY['catalog_secret_approvals', 'account_id', 'workflow_id'],
        ARRAY['domain_event_ledger', 'account_id', 'workflow_id'],
        ARRAY['event_dispatches', 'account_id', 'workflow_id'],
        ARRAY['extension_invocation_logs', 'account_id', 'workflow_id'],
        ARRAY['job_reaction_events', 'account_id', 'workflow_id'],
        ARRAY['user_roles', 'workflow_account_id', 'workflow_id']
    ] LOOP
        EXECUTE format(
            'SELECT count(*) FROM public.%I t WHERE t.%I IS NOT NULL AND t.%I IS NOT NULL '
            'AND NOT EXISTS (SELECT 1 FROM public.registry_workflows w '
            'WHERE w.account_id = t.%I AND w.workflow_id = t.%I)',
            spec[1], spec[3], spec[2], spec[2], spec[3]
        ) INTO offending;
        IF offending > 0 THEN
            RAISE EXCEPTION
                'workflow account identity: % row(s) of public.% name a workflow of another account (ADR 0020, C4b)',
                offending, spec[1];
        END IF;
    END LOOP;
END;
$$;

-- 4. Drop every reference to the global workflow identity, then the identity itself.
ALTER TABLE ONLY public.scheduled_jobs DROP CONSTRAINT IF EXISTS fk_scheduled_jobs_workflow;
ALTER TABLE ONLY public.scheduled_jobs DROP CONSTRAINT IF EXISTS scheduled_jobs_workflow_id_fkey;
ALTER TABLE ONLY public.workflow_entrypoints DROP CONSTRAINT IF EXISTS workflow_entrypoints_workflow_id_fkey;
ALTER TABLE ONLY public.workflow_reactions DROP CONSTRAINT IF EXISTS workflow_reactions_workflow_id_fkey;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    DROP CONSTRAINT IF EXISTS workflow_runtime_dependencies_workflow_id_fkey;
ALTER TABLE ONLY public.domain_event_ledger DROP CONSTRAINT IF EXISTS domain_event_ledger_workflow_id_fkey;
ALTER TABLE ONLY public.event_dispatches DROP CONSTRAINT IF EXISTS event_dispatches_workflow_id_fkey;
ALTER TABLE ONLY public.extension_invocation_logs
    DROP CONSTRAINT IF EXISTS extension_invocation_logs_workflow_id_fkey;
ALTER TABLE ONLY public.job_reaction_events DROP CONSTRAINT IF EXISTS job_reaction_events_workflow_id_fkey;
ALTER TABLE ONLY public.notification_routes DROP CONSTRAINT IF EXISTS notification_routes_workflow_id_fkey;
ALTER TABLE ONLY public.project_workflows DROP CONSTRAINT IF EXISTS project_workflows_workflow_id_fkey;
ALTER TABLE ONLY public.project_workflows DROP CONSTRAINT IF EXISTS project_workflows_project_account_fkey;
ALTER TABLE ONLY public.catalog_secret_bindings DROP CONSTRAINT IF EXISTS catalog_secret_bindings_workflow_id_fkey;
ALTER TABLE ONLY public.catalog_secret_approvals DROP CONSTRAINT IF EXISTS catalog_secret_approvals_workflow_id_fkey;
ALTER TABLE ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_workflow_id_fkey;
ALTER TABLE ONLY public.registry_workflow_steps DROP CONSTRAINT IF EXISTS registry_workflow_steps_owner_workflow_id_fkey;
ALTER TABLE ONLY public.registry_workflow_steps DROP CONSTRAINT IF EXISTS registry_workflow_steps_group_fkey;
ALTER TABLE ONLY public.registry_workflow_steps DROP CONSTRAINT IF EXISTS registry_workflow_steps_for_each_fkey;
ALTER TABLE ONLY public.registry_workflow_for_each DROP CONSTRAINT IF EXISTS registry_workflow_for_each_group_fkey;
ALTER TABLE ONLY public.registry_workflow_for_each
    DROP CONSTRAINT IF EXISTS registry_workflow_for_each_workflow_id_fkey;
ALTER TABLE ONLY public.registry_workflow_groups DROP CONSTRAINT IF EXISTS registry_workflow_groups_workflow_id_fkey;
ALTER TABLE ONLY public.registry_workflows
    DROP CONSTRAINT IF EXISTS registry_workflows_source_install_account_fkey;

ALTER TABLE ONLY public.registry_workflows DROP CONSTRAINT IF EXISTS registry_workflows_workflow_key_key;
ALTER TABLE ONLY public.registry_workflows DROP CONSTRAINT IF EXISTS registry_workflows_workflow_id_key;
ALTER TABLE ONLY public.registry_workflow_groups
    DROP CONSTRAINT IF EXISTS registry_workflow_groups_workflow_id_group_id_key;
ALTER TABLE ONLY public.registry_workflow_groups
    DROP CONSTRAINT IF EXISTS registry_workflow_groups_workflow_id_id_key;
ALTER TABLE ONLY public.registry_workflow_for_each
    DROP CONSTRAINT IF EXISTS registry_workflow_for_each_workflow_id_for_each_id_key;
ALTER TABLE ONLY public.registry_workflow_for_each
    DROP CONSTRAINT IF EXISTS registry_workflow_for_each_workflow_id_id_key;
ALTER TABLE ONLY public.registry_workflow_steps
    DROP CONSTRAINT IF EXISTS registry_workflow_steps_owner_workflow_id_step_id_key;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    DROP CONSTRAINT IF EXISTS workflow_runtime_dependencies_workflow_id_step_id_kind_ref_key;
ALTER TABLE ONLY public.catalog_secret_bindings DROP CONSTRAINT IF EXISTS catalog_secret_bindings_workflow_field_key;
ALTER TABLE ONLY public.catalog_secret_approvals DROP CONSTRAINT IF EXISTS catalog_secret_approvals_workflow_key;
ALTER TABLE ONLY public.approval_step_approvers DROP CONSTRAINT IF EXISTS approval_step_approvers_pkey;
ALTER TABLE ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_workflow_account_check;

-- 5. The account-qualified identity and the keys that follow it.
ALTER TABLE ONLY public.registry_workflows DROP CONSTRAINT IF EXISTS registry_workflows_account_id_workflow_key_key;
ALTER TABLE ONLY public.registry_workflows
    ADD CONSTRAINT registry_workflows_account_id_workflow_key_key UNIQUE (account_id, workflow_key);
ALTER TABLE ONLY public.registry_workflows DROP CONSTRAINT IF EXISTS registry_workflows_account_id_workflow_id_key;
ALTER TABLE ONLY public.registry_workflows
    ADD CONSTRAINT registry_workflows_account_id_workflow_id_key UNIQUE (account_id, workflow_id);
ALTER TABLE ONLY public.registry_workflow_groups
    DROP CONSTRAINT IF EXISTS registry_workflow_groups_account_id_workflow_id_group_id_key;
ALTER TABLE ONLY public.registry_workflow_groups
    ADD CONSTRAINT registry_workflow_groups_account_id_workflow_id_group_id_key UNIQUE (account_id, workflow_id, group_id);
ALTER TABLE ONLY public.registry_workflow_groups
    DROP CONSTRAINT IF EXISTS registry_workflow_groups_account_id_workflow_id_id_key;
ALTER TABLE ONLY public.registry_workflow_groups
    ADD CONSTRAINT registry_workflow_groups_account_id_workflow_id_id_key UNIQUE (account_id, workflow_id, id);
ALTER TABLE ONLY public.registry_workflow_for_each
    DROP CONSTRAINT IF EXISTS registry_workflow_for_each_account_workflow_for_each_key;
ALTER TABLE ONLY public.registry_workflow_for_each
    ADD CONSTRAINT registry_workflow_for_each_account_workflow_for_each_key UNIQUE (account_id, workflow_id, for_each_id);
ALTER TABLE ONLY public.registry_workflow_for_each
    DROP CONSTRAINT IF EXISTS registry_workflow_for_each_account_id_workflow_id_id_key;
ALTER TABLE ONLY public.registry_workflow_for_each
    ADD CONSTRAINT registry_workflow_for_each_account_id_workflow_id_id_key UNIQUE (account_id, workflow_id, id);
ALTER TABLE ONLY public.registry_workflow_steps
    DROP CONSTRAINT IF EXISTS registry_workflow_steps_account_owner_workflow_step_key;
ALTER TABLE ONLY public.registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_account_owner_workflow_step_key UNIQUE (account_id, owner_workflow_id, step_id);
ALTER TABLE ONLY public.workflow_runtime_dependencies
    DROP CONSTRAINT IF EXISTS workflow_runtime_dependencies_account_step_ref_key;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_account_step_ref_key UNIQUE (account_id, workflow_id, step_id, dependency_kind, dependency_ref);
ALTER TABLE ONLY public.catalog_secret_bindings
    ADD CONSTRAINT catalog_secret_bindings_workflow_field_key UNIQUE (account_id, workflow_id, field_name);
ALTER TABLE ONLY public.catalog_secret_approvals
    ADD CONSTRAINT catalog_secret_approvals_workflow_key UNIQUE (account_id, workflow_id);
ALTER TABLE ONLY public.approval_step_approvers
    ADD CONSTRAINT approval_step_approvers_pkey PRIMARY KEY (account_id, workflow_id, step_id, user_id);
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_workflow_account_check
    CHECK (((workflow_id IS NULL) OR (workflow_account_id IS NOT NULL)));

-- 6. References follow the owning account.
ALTER TABLE ONLY public.registry_workflows
    ADD CONSTRAINT registry_workflows_source_install_account_fkey FOREIGN KEY (source_install_id, account_id)
    REFERENCES public.extension_installs(install_id, account_id) ON DELETE SET NULL (source_install_id);
ALTER TABLE ONLY public.registry_workflow_groups
    ADD CONSTRAINT registry_workflow_groups_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.registry_workflow_for_each
    ADD CONSTRAINT registry_workflow_for_each_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.registry_workflow_for_each
    ADD CONSTRAINT registry_workflow_for_each_group_fkey FOREIGN KEY (account_id, workflow_id, group_id)
    REFERENCES public.registry_workflow_groups(account_id, workflow_id, id) ON DELETE CASCADE;
ALTER TABLE ONLY public.registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_owner_workflow_id_fkey FOREIGN KEY (account_id, owner_workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_group_fkey FOREIGN KEY (account_id, owner_workflow_id, group_id)
    REFERENCES public.registry_workflow_groups(account_id, workflow_id, id) ON DELETE CASCADE;
ALTER TABLE ONLY public.registry_workflow_steps
    ADD CONSTRAINT registry_workflow_steps_for_each_fkey FOREIGN KEY (account_id, owner_workflow_id, for_each_id)
    REFERENCES public.registry_workflow_for_each(account_id, workflow_id, id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_workflows
    ADD CONSTRAINT project_workflows_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.project_workflows
    ADD CONSTRAINT project_workflows_project_account_fkey FOREIGN KEY (project_id, account_id)
    REFERENCES public.projects(id, account_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE RESTRICT;
ALTER TABLE ONLY public.workflow_entrypoints
    ADD CONSTRAINT workflow_entrypoints_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.workflow_reactions
    ADD CONSTRAINT workflow_reactions_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.workflow_runtime_dependencies
    ADD CONSTRAINT workflow_runtime_dependencies_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.notification_routes
    ADD CONSTRAINT notification_routes_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.catalog_secret_bindings
    ADD CONSTRAINT catalog_secret_bindings_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.catalog_secret_approvals
    ADD CONSTRAINT catalog_secret_approvals_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.domain_event_ledger
    ADD CONSTRAINT domain_event_ledger_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE SET NULL (workflow_id);
ALTER TABLE ONLY public.event_dispatches
    ADD CONSTRAINT event_dispatches_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE SET NULL (workflow_id);
ALTER TABLE ONLY public.extension_invocation_logs
    ADD CONSTRAINT extension_invocation_logs_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE SET NULL (workflow_id);
ALTER TABLE ONLY public.job_reaction_events
    ADD CONSTRAINT job_reaction_events_workflow_id_fkey FOREIGN KEY (account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE SET NULL (workflow_id);
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_workflow_id_fkey FOREIGN KEY (workflow_account_id, workflow_id)
    REFERENCES public.registry_workflows(account_id, workflow_id) ON DELETE CASCADE;
