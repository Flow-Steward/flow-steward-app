-- Migration: workflow_connection_slot_choices
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Connection slots of extension workflows (ADR 0068). A project picks one of its
-- connections for each slot an extension workflow declares; the choice is keyed by
-- install and local workflow id, never by release, so it survives updates. Deleting
-- the connection, the project or the account removes it.
--
-- The DDL is the block at the end of db/schema/001_schema_part_12.sql with
-- IF NOT EXISTS added: a fresh installation (which already has the table) is
-- unchanged, and an upgraded one gets the same objects. No existing data moves.
CREATE TABLE IF NOT EXISTS public.workflow_connection_slot_choices (
    account_id uuid NOT NULL REFERENCES public.accounts(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    source_install_id text NOT NULL CHECK (btrim(source_install_id) <> ''::text),
    local_workflow_id text NOT NULL CHECK (btrim(local_workflow_id) <> ''::text),
    slot text NOT NULL CHECK (slot ~ '^[a-z][a-z0-9_]{0,62}$'::text),
    connection_id text NOT NULL,
    chosen_by text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT workflow_connection_slot_choices_pkey
        PRIMARY KEY (account_id, project_id, source_install_id, local_workflow_id, slot),
    CONSTRAINT workflow_connection_slot_choices_connection_fkey
        FOREIGN KEY (connection_id, project_id, account_id)
        REFERENCES public.project_connections(connection_id, project_id, account_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workflow_connection_slot_choices_install
    ON public.workflow_connection_slot_choices (account_id, source_install_id);
CREATE INDEX IF NOT EXISTS idx_workflow_connection_slot_choices_connection
    ON public.workflow_connection_slot_choices (connection_id);
