-- Migration: role_permissions
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Role permissions and the scope of a role assignment (ADR 0076).
--
-- The DDL is the baseline block at the end of 001_schema_part_12.sql, plus the
-- columns db/schema/001_schema_part_04.sql declares on public.roles and
-- public.user_roles and the assignment key of 001_schema_part_07.sql, written so that
-- it is a no-op on a fresh installation (which already has all of it) and gives an
-- upgraded one the same objects. Its data part is idempotent too:
--
-- * the system roles admin, operator and viewer exist, are marked is_system and carry
--   a description; an existing label is kept;
-- * operator and viewer hold exactly the permissions they had in code before this
--   release (core/application/auth/permissions.py, OPERATOR_PERMISSIONS and
--   VIEWER_PERMISSIONS); admin holds every permission implicitly and has no rows;
-- * a user with several system roles keeps only the highest (viewer < operator <
--   admin). The highest one already decided every check, so nobody's rights change.

ALTER TABLE public.roles ADD COLUMN IF NOT EXISTS is_system boolean DEFAULT false NOT NULL;
ALTER TABLE public.roles ADD COLUMN IF NOT EXISTS description text DEFAULT ''::text NOT NULL;

ALTER TABLE public.user_roles ADD COLUMN IF NOT EXISTS workspace_id uuid;
ALTER TABLE public.user_roles ADD COLUMN IF NOT EXISTS project_id uuid;
ALTER TABLE public.user_roles ADD COLUMN IF NOT EXISTS workflow_id text;
ALTER TABLE public.user_roles
    ADD COLUMN IF NOT EXISTS role_is_system boolean DEFAULT false NOT NULL;

CREATE TABLE IF NOT EXISTS public.role_permissions (
    role_id uuid NOT NULL REFERENCES public.roles(id) ON DELETE CASCADE,
    permission_key text NOT NULL
        CHECK (permission_key ~ '^[a-z][a-z_]*(\.[a-z][a-z_]*){1,2}$'::text),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT role_permissions_pkey PRIMARY KEY (role_id, permission_key)
);

-- The system roles.
INSERT INTO public.roles (key, label, is_system, description)
VALUES
    ('admin', 'Admin', true,
     'Configures the installation: users, extensions, projects, workflows, connections and secrets. Holds every permission.'),
    ('operator', 'Operator', true,
     'Does the work the admin configured: starts workflows, decides approvals, controls jobs and replaces secret values.'),
    ('viewer', 'Viewer', true,
     'Reads history, results and files. Makes no changes.')
ON CONFLICT (key) DO UPDATE
SET is_system = true,
    description = EXCLUDED.description;

INSERT INTO public.role_permissions (role_id, permission_key)
SELECT r.id, granted.permission_key
FROM (
    VALUES
        ('viewer', 'account.self'),
        ('viewer', 'jobs.read'),
        ('viewer', 'projects.read'),
        ('viewer', 'workflows.read'),
        ('viewer', 'reports.read'),
        ('viewer', 'notify.read'),
        ('viewer', 'secrets.read'),
        ('viewer', 'extensions.read'),
        ('viewer', 'platform.read'),
        ('operator', 'account.self'),
        ('operator', 'jobs.read'),
        ('operator', 'projects.read'),
        ('operator', 'workflows.read'),
        ('operator', 'reports.read'),
        ('operator', 'notify.read'),
        ('operator', 'secrets.read'),
        ('operator', 'extensions.read'),
        ('operator', 'platform.read'),
        ('operator', 'registry.read'),
        ('operator', 'account.profile.write'),
        ('operator', 'workflows.run'),
        ('operator', 'approvals.decide'),
        ('operator', 'jobs.write'),
        ('operator', 'reports.write'),
        ('operator', 'notify.dispatch'),
        ('operator', 'extensions.use'),
        ('operator', 'secrets.value.write')
) AS granted (role_key, permission_key)
JOIN public.roles AS r ON r.key = granted.role_key
ON CONFLICT (role_id, permission_key) DO NOTHING;

-- One system role per user in the global scope: keep the highest.
DELETE FROM public.user_roles AS lower_assignment
USING public.roles AS lower_role
WHERE lower_role.id = lower_assignment.role_id
  AND lower_role.key IN ('viewer', 'operator')
  AND lower_assignment.workspace_id IS NULL
  AND lower_assignment.project_id IS NULL
  AND lower_assignment.workflow_id IS NULL
  AND EXISTS (
      SELECT 1
      FROM public.user_roles AS higher_assignment
      JOIN public.roles AS higher_role ON higher_role.id = higher_assignment.role_id
      WHERE higher_assignment.user_id = lower_assignment.user_id
        AND higher_assignment.workspace_id IS NULL
        AND higher_assignment.project_id IS NULL
        AND higher_assignment.workflow_id IS NULL
        AND (CASE higher_role.key WHEN 'admin' THEN 3 WHEN 'operator' THEN 2 ELSE 0 END)
          > (CASE lower_role.key WHEN 'operator' THEN 2 ELSE 1 END)
  );

-- The copy of roles.is_system on each assignment, before anything depends on it.
UPDATE public.user_roles AS assignment
SET role_is_system = role.is_system
FROM public.roles AS role
WHERE role.id = assignment.role_id
  AND assignment.role_is_system IS DISTINCT FROM role.is_system;

-- Constraints: dropped first so that the statement is the same on a fresh and an
-- upgraded database. The composite foreign key depends on roles_id_is_system_key and
-- is dropped before it.
ALTER TABLE ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_role_is_system_fkey;
ALTER TABLE ONLY public.roles DROP CONSTRAINT IF EXISTS roles_id_is_system_key;
ALTER TABLE ONLY public.roles
    ADD CONSTRAINT roles_id_is_system_key UNIQUE (id, is_system);

ALTER TABLE ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_user_id_role_id_key;
ALTER TABLE ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_assignment_key;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_assignment_key
        UNIQUE NULLS NOT DISTINCT (user_id, role_id, workspace_id, project_id, workflow_id);

ALTER TABLE ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_workspace_id_fkey;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_workspace_id_fkey FOREIGN KEY (workspace_id)
        REFERENCES public.workspaces(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_project_id_fkey;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_project_id_fkey FOREIGN KEY (project_id)
        REFERENCES public.projects(id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_workflow_id_fkey;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_workflow_id_fkey FOREIGN KEY (workflow_id)
        REFERENCES public.registry_workflows(workflow_id) ON DELETE CASCADE;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_role_is_system_fkey FOREIGN KEY (role_id, role_is_system)
        REFERENCES public.roles(id, is_system) ON UPDATE CASCADE ON DELETE CASCADE;
ALTER TABLE ONLY public.user_roles DROP CONSTRAINT IF EXISTS user_roles_workflow_scope_check;
ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_workflow_scope_check
        CHECK (workflow_id IS NULL OR project_id IS NOT NULL);

CREATE OR REPLACE FUNCTION public.user_roles_copy_role_is_system() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    SELECT r.is_system INTO NEW.role_is_system FROM public.roles AS r WHERE r.id = NEW.role_id;
    NEW.role_is_system := COALESCE(NEW.role_is_system, false);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_user_roles_role_is_system ON public.user_roles;
CREATE TRIGGER trg_user_roles_role_is_system
    BEFORE INSERT OR UPDATE OF role_id ON public.user_roles
    FOR EACH ROW EXECUTE FUNCTION public.user_roles_copy_role_is_system();

CREATE UNIQUE INDEX IF NOT EXISTS ux_user_roles_one_system_role_per_scope
    ON public.user_roles (user_id, workspace_id, project_id, workflow_id) NULLS NOT DISTINCT
    WHERE role_is_system;
