-- Migration: scoped_role_users
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Project-scoped role limits (ADR 0076 section 8, ADR 0082). A row marks a person
-- whose access is limited to the projects that have a project-scoped user_roles
-- row. The marker is durable: deleting the last listed project removes the scoped
-- user_roles rows by cascade but keeps the person limited. The DDL is the end of
-- db/schema/001_schema_part_12.sql. Idempotent.
CREATE TABLE IF NOT EXISTS public.scoped_role_users (
    user_id uuid PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    account_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    created_by text DEFAULT ''::text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_roles_project
    ON public.user_roles USING btree (project_id)
    WHERE (project_id IS NOT NULL);
