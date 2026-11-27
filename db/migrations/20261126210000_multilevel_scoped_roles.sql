-- Migration: multilevel_scoped_roles
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Scoped role limits on workspaces, projects and workflows (ADR 0076 section 8a).
-- A limited person's grants are user_roles rows with workspace_id (a whole
-- workspace, its future projects included), project_id (one project) or
-- project_id + workflow_id (chosen workflows of a project). The limit is read on
-- every request by user, and deleting a workspace or a workflow removes its rows
-- by cascade: both foreign keys get an index. The DDL is the end of
-- db/schema/001_schema_part_12.sql. Idempotent.
CREATE INDEX IF NOT EXISTS idx_user_roles_workspace
    ON public.user_roles USING btree (workspace_id)
    WHERE (workspace_id IS NOT NULL);

CREATE INDEX IF NOT EXISTS idx_user_roles_workflow
    ON public.user_roles USING btree (workflow_account_id, workflow_id)
    WHERE (workflow_id IS NOT NULL);
