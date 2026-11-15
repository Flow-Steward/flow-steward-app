import { useI18n } from "@/lib/i18n/useI18n";
import { useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";

import { DsAlert, DsBadge, DsSelect, DsTable } from "@/components/shared/design-system";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import type { ProjectListItem } from "@/pages/projects/list/api";
import {
  fetchWorkflowConnectionSlots,
  saveWorkflowConnectionSlot,
  workflowConnectionSlotsQueryKey,
  type WorkflowConnectionSlot,
} from "@/pages/workflows/shared/connection-slot-api";
import { ignoreAsyncResult } from "@/lib/utils/async";

type Translate = ReturnType<typeof useI18n>["t"];

function SlotStateBadge({ slot, t }: Readonly<{ slot: WorkflowConnectionSlot; t: Translate }>) {
  if (!slot.ready) return <DsBadge tone="orange">{t("react.workflows.connection_slot_not_ready", "Not ready")}</DsBadge>;
  if (slot.choice === "automatic") {
    return <DsBadge tone="green">{t("react.workflows.connection_slot_automatic", "Chosen automatically")}</DsBadge>;
  }
  return <DsBadge tone="green">{t("react.workflows.connection_slot_chosen", "Chosen")}</DsBadge>;
}

function renderSlotNameCell(t: Translate, slot: WorkflowConnectionSlot) {
  return (
    <div className="min-w-0">
      <p className="fs-ink-primary">{t("react.workflows.connection_slot_needs", "This workflow needs: {label}", { label: slot.label })}</p>
      {slot.reason ? <p className="text-xs fs-ink-secondary">{slot.reason}</p> : null}
    </div>
  );
}

function renderSlotSelectCell(
  { busySlot, onChoose, t }: Readonly<{ busySlot: string; onChoose: (slot: string, connectionId: string) => void; t: Translate }>,
  slot: WorkflowConnectionSlot,
) {
  return (
    <DsSelect
      ariaLabel={t("react.workflows.connection_slot_select", "Connection for {label}", { label: slot.label })}
      disabled={Boolean(busySlot)}
      emptyLabel={t("react.workflows.connection_slot_no_connections", "The project has no connection of this kind")}
      onChange={(value) => onChoose(slot.slot, value)}
      options={slot.options.map((option) => ({
        value: option.connection_id,
        label: option.display_name || option.connection_id,
        ...(option.connection_type ? { description: option.connection_type } : {}),
      }))}
      placeholder={t("react.workflows.connection_slot_choose", "Choose a connection")}
      value={slot.connection_id || ""}
    />
  );
}

function renderSlotStateCell(t: Translate, slot: WorkflowConnectionSlot) {
  return <SlotStateBadge slot={slot} t={t} />;
}

/**
 * The connections an extension workflow reads (ADR 0068). Each project picks one
 * connection per slot; with exactly one usable connection it is chosen
 * automatically. A slot without a connection keeps the workflow from running
 * in that project, and the row says why.
 */
export function WorkflowConnectionSlotsPanel({
  onProjectChange,
  projectId,
  projects,
  projectsError,
  projectsPending,
  workflowId,
}: Readonly<{
  onProjectChange: (id: string) => void;
  projectId: string;
  projects: ProjectListItem[];
  projectsError: Error | null;
  projectsPending: boolean;
  workflowId: string;
}>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [busySlot, setBusySlot] = useState("");
  const [saveError, setSaveError] = useState("");
  const queryKey = workflowConnectionSlotsQueryKey(projectId, workflowId);
  const slotsQuery = useQuery({
    queryKey,
    queryFn: () => fetchWorkflowConnectionSlots(projectId, workflowId),
    enabled: Boolean(projectId && workflowId),
    retry: false,
  });
  const loaderShown = useLoaderDelay(Boolean(projectsPending || (projectId && slotsQuery.isPending)));
  const slots = slotsQuery.data?.slots ?? [];

  const choose = async (slot: string, connectionId: string) => {
    setBusySlot(slot);
    setSaveError("");
    try {
      const result = await saveWorkflowConnectionSlot(projectId, workflowId, slot, connectionId);
      queryClient.setQueryData(queryKey, result);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusySlot("");
    }
  };

  const projectName = projects.find((project) => project.id === projectId)?.name || projectId;
  let description = t("react.workflows.connection_slots_description", "Connections this workflow reads. Each project chooses its own.");
  if (projectId) {
    description = t("react.workflows.connection_slots_in_project", "Connections in project {project}.", { project: projectName });
  }
  return (
    <article className="fs-card" id="workflow-connection-slots">
      <header className="fs-card-header">
        <h2 className="fs-card-title">{t("react.workflows.connection_slots", "Connections")}</h2>
        <p className="fs-card-description">{description}</p>
      </header>
      <div className="fs-card-body">
        {projectsError ? (
          <DsAlert title={t("react.workflows.linked_projects_could_not_be_loaded", "Linked projects could not be loaded")} tone="danger">{projectsError.message}</DsAlert>
        ) : null}
        {!projectsPending && !projectsError && !projects.length ? (
          <Link className="fs-button secondary" to={`/registry/workflows/${encodeURIComponent(workflowId)}/projects`}>{t("react.workflows.link_a_project", "Link a project")}</Link>
        ) : null}
        {projects.length > 1 ? (
          <DsSelect ariaLabel={t("react.workflows.connection_slots_project", "Project for connections")} value={projectId}
            onChange={onProjectChange} placeholder={t("react.workflows.choose_a_linked_project", "Choose a linked project")}
            options={projects.map((project) => ({ value: project.id, label: project.name || project.id }))} />
        ) : null}
        {loaderShown ? <p className="fs-empty-copy"><DsLoader variant="orbit-inline" label={t("react.workflows.connection_slots_loading", "Loading connections…")} /></p> : null}
        {slotsQuery.isError ? (
          <DsAlert title={t("react.workflows.connection_slots_load_failed", "The connections could not be loaded")} tone="danger">
            {slotsQuery.error instanceof Error ? slotsQuery.error.message : String(slotsQuery.error)}
          </DsAlert>
        ) : null}
        {saveError ? (
          <DsAlert title={t("react.workflows.connection_slot_save_failed", "The connection was not saved")} tone="danger">{saveError}</DsAlert>
        ) : null}
        {slotsQuery.data && !slotsQuery.data.ready ? (
          <DsAlert title={t("react.workflows.connection_slots_not_ready", "This workflow is not ready in this project")} tone="warning">
            {t("react.workflows.connection_slots_not_ready_detail", "Runs, webhooks and schedules are refused until every connection below is chosen.")}
          </DsAlert>
        ) : null}
        {slots.length ? (
          <DsTable
            ariaLabel={t("react.workflows.connection_slots", "Connections")}
            columns={[
              { key: "slot", header: t("react.workflows.connection_slot_needed", "Needed"), cell: renderSlotNameCell.bind(null, t) },
              {
                key: "connection",
                header: t("react.workflows.connection_slot_connection", "Connection"),
                cell: renderSlotSelectCell.bind(null, { busySlot, onChoose: (slot, id) => ignoreAsyncResult(choose(slot, id)), t }),
              },
              { key: "state", header: t("extensions.runtime.status", "Status"), cell: renderSlotStateCell.bind(null, t) },
            ]}
            embedded
            rowKey={(slot) => slot.slot}
            rows={slots}
          />
        ) : null}
      </div>
    </article>
  );
}
