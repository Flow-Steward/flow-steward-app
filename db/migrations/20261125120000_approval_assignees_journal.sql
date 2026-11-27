-- Migration: approval_assignees_journal
-- Flow Steward post-1.0 SQL migration.
--
-- Write SQL statements only. Do NOT add explicit transaction-control
-- statements; the runner manages transactions and records this migration
-- in public.platform_schema_migrations after it succeeds.
--
-- transaction: true

-- Assigned approvers of an Approval step and the approval decision journal
-- (ADR 0082 section 3a). The DDL is the end of db/schema/001_schema_part_12.sql.
-- The journal has no foreign key to the job or the account: its rows outlive both.
-- Idempotent.
CREATE TABLE IF NOT EXISTS public.approval_step_approvers (
    workflow_id text NOT NULL,
    step_id text NOT NULL,
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text DEFAULT ''::text NOT NULL,
    PRIMARY KEY (workflow_id, step_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_approval_step_approvers_user
    ON public.approval_step_approvers USING btree (user_id);

CREATE TABLE IF NOT EXISTS public.approval_decision_journal (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    account_id uuid NOT NULL,
    job_id uuid NOT NULL,
    workflow_id text,
    workflow_name text,
    project_id text,
    step_id text,
    step_label text,
    started_by text,
    started_by_display_name text,
    started_at timestamp with time zone,
    decided_by text NOT NULL,
    decided_by_display_name text,
    decided_by_user_id uuid,
    decided_by_kind text NOT NULL
        CHECK (decided_by_kind = ANY (ARRAY['user'::text, 'service_account'::text, 'external'::text, 'automation'::text])),
    external_actor jsonb,
    action text NOT NULL,
    comment text,
    reviewed_values jsonb DEFAULT '{}'::jsonb NOT NULL,
    changed_values jsonb DEFAULT '{}'::jsonb NOT NULL,
    decided_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approval_decision_journal_account_decided
    ON public.approval_decision_journal USING btree (account_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_approval_decision_journal_account_workflow
    ON public.approval_decision_journal USING btree (account_id, workflow_id);
