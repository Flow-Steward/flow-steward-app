import type { I18nContextValue } from "@/lib/i18n/context";

/** The session permissions that decide the grid's row and bulk actions (ADR 0076). */
export type JobsGridRoles = Readonly<{
  canMutate: boolean;
  isAdmin: boolean;
}>;

/** The jobs grid as a data-grid config: its columns, row and bulk actions, and empty state. */
export function buildJobsGridConfig(t: I18nContextValue["t"], roleFlags: JobsGridRoles): Record<string, unknown> {
  const rowActions: Record<string, unknown>[] = [{ action_id: "view", label: t("jobs.actions.view", "View"), variant: "ghost" }];
  if (roleFlags.canMutate) {
    rowActions.push({
      action_id: "cancel",
      label: t("common.cancel", "Cancel"),
      variant: "danger",
      visible_if: { field: "status", in: ["in_progress", "resume_pending", "pending", "awaiting_approval", "awaiting_timer", "awaiting_child", "awaiting_event"] },
    });
  }
  const bulkActions: Record<string, unknown>[] = [];
  if (roleFlags.canMutate) {
    bulkActions.push({ action_id: "bulk_cancel", label: t("jobs.actions.cancel_selected", "Cancel Selected"), variant: "danger" });
  }
  if (roleFlags.isAdmin) {
    bulkActions.push({ action_id: "bulk_delete", label: t("project_detail.jobs.delete_selected", "Delete Selected"), variant: "ghost" });
  }
  return {
    columns: [
      {
        key: "_workflow_display",
        label: t("jobs.columns.workflow", "Workflow"),
        type: "text",
        width: "320px",
      },
      {
        key: "project_name",
        label: t("events.columns.project", "Project"),
        type: "text",
        width: "130px",
        truncate: true,
        truncate_width: "clamp(9rem, 14vw, 12rem)",
        tooltip_key: "project_name",
      },
      {
        key: "status",
        label: t("account_settings.card.status", "Status"),
        type: "badge",
        width: "140px",
        badge_map: {
          failed: "red",
          stuck: "red",
          orphaned: "amber",
          max_iterations: "red",
          triage_needed: "red",
          awaiting_approval: "red",
          blocked: "amber",
          retryable_interrupted_step: "amber",
          external_wait: "amber",
          in_progress: "accent",
          awaiting_timer: "accent",
          awaiting_child: "accent",
          awaiting_event: "accent",
          resume_pending: "accent",
          pending: "accent",
          completed: "green",
          cancelled: "neutral",
        },
      },
      {
        key: "_started_display",
        label: t("project_detail.jobs.grid.started", "Started"),
        type: "text",
        width: "180px",
        tooltip_key: "_started_tooltip",
      },
      {
        key: "_completed_display",
        label: t("jobs.detail.step.completed", "Completed"),
        type: "text",
        width: "180px",
        tooltip_key: "_completed_tooltip",
      },
      { key: "_duration", label: t("jobs.columns.duration", "Duration"), type: "text", width: "88px" },
    ],
    row_actions: rowActions,
    bulk_actions: bulkActions,
    id_field: "job_id",
    detail: { navigate: true },
    pagination: {
      page_size_default: 25,
      page_size_options: [10, 25, 50],
    },
    empty_state: {
      title: t("jobs.empty.title", "No jobs found"),
      description: t("jobs.empty.description", "Try adjusting your search or filters."),
    },
  };
}
