import { useI18n } from "@/lib/i18n/useI18n";
import { useMemo, useState } from "react";

import { ChevronRight } from "lucide-react";

import { DsAlert, DsBadge, DsDialog, DsSelect } from "@/components/shared/design-system";
import type {
  WorkspaceDeleteDataConnectionAction,
  WorkspaceDeletePlan,
  WorkspaceDeleteProjectAction,
} from "@/pages/registry/workspaces/api";

type ProjectDecision = {
  action: "trash" | "reassign";
  target_workspace_id: string;
};

type ConnectionDecision = {
  action: "" | "block" | "reassign" | "delete";
  target_workspace_id: string;
};

type Props = {
  busy?: boolean;
  error?: string;
  onClose: () => void;
  onConfirm: (
    projectActions: WorkspaceDeleteProjectAction[],
    connectionActions: WorkspaceDeleteDataConnectionAction[],
  ) => void;
  open: boolean;
  plan: WorkspaceDeletePlan | null;
};

function buildInitialDecisions(plan: WorkspaceDeletePlan | null): Record<string, ProjectDecision> {
  const next: Record<string, ProjectDecision> = {};
  for (const project of plan?.projects || []) {
    if (project.status !== "active") {
      continue;
    }
    next[project.project_id] = { action: "trash", target_workspace_id: "" };
  }
  return next;
}

function buildInitialConnectionDecisions(
  plan: WorkspaceDeletePlan | null,
): Record<string, ConnectionDecision> {
  const next: Record<string, ConnectionDecision> = {};
  for (const connection of plan?.data_connections || []) {
    if (connection.status === "active") {
      next[connection.connection_id] = { action: "", target_workspace_id: "" };
    }
  }
  return next;
}

type ContentProps = {
  activePlan: WorkspaceDeletePlan;
  busy: boolean;
  error: string;
  initialDecisions: Record<string, ProjectDecision>;
  initialConnectionDecisions: Record<string, ConnectionDecision>;
  onClose: () => void;
  onConfirm: (
    projectActions: WorkspaceDeleteProjectAction[],
    connectionActions: WorkspaceDeleteDataConnectionAction[],
  ) => void;
};

function WorkspaceDeletePlanModalContent({
  activePlan,
  busy,
  error,
  initialDecisions,
  initialConnectionDecisions,
  onClose,
  onConfirm,
}: Readonly<ContentProps>) {
  const { t } = useI18n();
  const [decisions, setDecisions] = useState<Record<string, ProjectDecision>>(initialDecisions);
  const [connectionDecisions, setConnectionDecisions] = useState<
    Record<string, ConnectionDecision>
  >(initialConnectionDecisions);
  const targetWorkspaces = activePlan.target_workspaces || [];
  const clientValidationError = useMemo(() => {
    const missingTarget = Object.entries(decisions).some(
      ([, value]) => value.action === "reassign" && !value.target_workspace_id,
    );
    if (missingTarget) {
      return t("react.shared.project_destination_required", "Choose a destination workspace for every reassigned project.");
    }
    const activeConnections = (activePlan.data_connections || []).filter(
      (connection) => connection.status === "active",
    );
    const missingConnectionAction = activeConnections.some(
      (connection) => !connectionDecisions[connection.connection_id]?.action,
    );
    if (missingConnectionAction) {
      return t("react.shared.connection_action_required", "Choose an explicit action for every live Data Connection.");
    }
    const blockedConnection = activeConnections.find(
      (connection) => connectionDecisions[connection.connection_id]?.action === "block",
    );
    if (blockedConnection) {
      return t("react.shared.workspace_deletion_blocked_connection", "Workspace deletion is blocked by {label}.", { label: blockedConnection.label });
    }
    const linkedConnection = activeConnections.find((connection) => {
      const action = connectionDecisions[connection.connection_id]?.action;
      return connection.linked_dataset_count > 0 && (action === "reassign" || action === "delete");
    });
    if (linkedConnection) {
      return t("react.shared.connection_linked_datasets_block", "{label} still has linked datasets.", { label: linkedConnection.label });
    }
    const missingConnectionTarget = activeConnections.some(
      (connection) =>
        connectionDecisions[connection.connection_id]?.action === "reassign" &&
        !connectionDecisions[connection.connection_id]?.target_workspace_id,
    );
    if (missingConnectionTarget) {
      return t("react.shared.connection_destination_required", "Choose a destination workspace for every reassigned Data Connection.");
    }
    return "";
  }, [activePlan.data_connections, connectionDecisions, decisions, t]);

  const allProjects = activePlan.projects || [];
  const activeProjects = allProjects.filter((project) => project.status === "active");
  const trashedProjects = allProjects.filter((project) => project.status !== "active");
  const datasets = activePlan.datasets || [];
  const dataConnections = activePlan.data_connections || [];
  const activeConnections = dataConnections.filter((connection) => connection.status === "active");
  const trashedConnections = dataConnections.filter((connection) => connection.status !== "active");
  const projectActions: WorkspaceDeleteProjectAction[] = activeProjects.map((project) => {
    const decision = decisions[project.project_id] || { action: "trash", target_workspace_id: "" };
    return {
      project_id: project.project_id,
      action: decision.action,
      target_workspace_id: decision.target_workspace_id || undefined,
    };
  });
  const reassignedCount = projectActions.filter((item) => item.action === "reassign").length;
  const deletedCount = projectActions.length - reassignedCount;
  const connectionActions: WorkspaceDeleteDataConnectionAction[] = dataConnections.flatMap(
    (connection) => {
      if (connection.status !== "active") {
        return [];
      }
      const decision = connectionDecisions[connection.connection_id];
      if (!decision?.action) {
        return [];
      }
      return [
        {
          connection_id: connection.connection_id,
          action: decision.action,
          target_workspace_id: decision.target_workspace_id || undefined,
        },
      ];
    },
  );

  const targetOptions = [
    // A real entry, never a placeholder: `DsSelect` renders `placeholder` as
    // non-selectable text, which would make "no destination yet" unreachable.
    { value: "", label: t("react.shared.choose_workspace", "Choose workspace") },
    ...targetWorkspaces.map((workspace) => ({
      value: workspace.workspace_id,
      label: workspace.label,
    })),
  ];

  return (
    <DsDialog
      busy={busy}
      description={activePlan.workspace.label}
      eyebrow={t("project_mode.workspace", "Workspace")}
      footer={
        <>
          <button className="fs-button" onClick={onClose} type="button">
            {t("common.cancel", "Cancel")} </button>
          <button
            className="fs-button danger"
            disabled={
              busy ||
              Boolean(activePlan.validation_errors?.length) ||
              Boolean(clientValidationError)
            }
            onClick={() => onConfirm(projectActions, connectionActions)}
            type="button"
          >
            {t("react.shared.move_workspace_to_trash", "Move workspace to trash")} </button>
        </>
      }
      onClose={onClose}
      open
      title={t("react.shared.workspace_delete_plan", "Workspace delete plan")}
      variant="form"
    >
      <div className="fs-dialog-form">
        <p>
          {t("react.shared.active_child_projects", "Active child projects:")} {activeProjects.length}{t("react.shared.delete_with_workspace", ". Delete with workspace:")} {deletedCount}{t("react.shared.reassign", ". Reassign:")} {reassignedCount}.
        </p>
        <p className="fs-field-helper">
          {t("react.shared.projects_marked_for_reassignment_will_stay_active_and_be_moved_before_this_workspace_goes_to_trash", "Projects marked for reassignment will stay active and be moved before this workspace goes to trash.")} </p>

        {activePlan.warnings?.length ? (
          <DsAlert title={t("react.shared.review_before_continuing", "Review before continuing")} tone="warning">
            {activePlan.warnings.join(" ")}
          </DsAlert>
        ) : null}
        {activePlan.validation_errors?.length ? (
          <DsAlert title={t("react.shared.this_plan_cannot_run", "This plan cannot run")} tone="danger">
            {activePlan.validation_errors.join(" ")}
          </DsAlert>
        ) : null}
        {error ? (
          <DsAlert title={t("react.shared.workspace_delete_failed", "Workspace delete failed")} tone="danger">
            {error}
          </DsAlert>
        ) : null}
        {clientValidationError ? <DsAlert title={clientValidationError} tone="danger" /> : null}

        {activeProjects.length ? (
          <section>
            <h3 className="fs-panel-title">{t("react.shared.active_projects", "Active projects (")}{activeProjects.length})</h3>
            {activeProjects.map((project) => {
              const decision = decisions[project.project_id] || {
                action: "trash" as const,
                target_workspace_id: "",
              };
              const actionId = `workspace-delete-action-${project.project_id}`;
              const targetId = `workspace-delete-target-${project.project_id}`;
              return (
                <div
                  className="fs-surface-radius fs-surface-inset border border-subtle p-3"
                  key={project.project_id}
                >
                  <p>
                    <strong>{project.label}</strong>
                  </p>
                  <p className="fs-field-helper">{t("events.columns.project", "Project")} {project.project_id}</p>
                  <div className="fs-field">
                    <label className="fs-field-label" htmlFor={actionId}>
                      {t("react.shared.delete_action", "Delete action")} </label>
                    <DsSelect
                      ariaLabel={t("react.shared.delete_action", "Delete action")}
                      id={actionId}
                      onChange={(value) =>
                        setDecisions((current) => ({
                          ...current,
                          [project.project_id]: {
                            action: value === "reassign" ? "reassign" : "trash",
                            target_workspace_id:
                              value === "reassign"
                                ? current[project.project_id]?.target_workspace_id || ""
                                : "",
                          },
                        }))
                      }
                      options={[
                        { value: "trash", label: t("react.shared.delete_with_workspace_2", "Delete with workspace") },
                        // `DsOption` carries no `disabled`, and adding one is a
                        // shared-component change this slice puts out of scope.
                        // A project the API says cannot be reassigned therefore
                        // does not offer the choice at all, rather than offering
                        // one that would submit a payload the API rejects.
                        ...(project.can_reassign
                          ? [{ value: "reassign", label: t("react.shared.keep_project_by_moving", "Keep project by moving") }]
                          : []),
                      ]}
                      value={decision.action}
                    />
                  </div>
                  {decision.action === "reassign" ? (
                    <div className="fs-field">
                      <label className="fs-field-label" htmlFor={targetId}>
                        {t("react.shared.target_workspace", "Target workspace")} </label>
                      <DsSelect
                        ariaLabel={t("react.shared.target_workspace", "Target workspace")}
                        id={targetId}
                        onChange={(value) =>
                          setDecisions((current) => ({
                            ...current,
                            [project.project_id]: {
                              action: "reassign",
                              target_workspace_id: value,
                            },
                          }))
                        }
                        options={targetOptions}
                        value={decision.target_workspace_id}
                      />
                    </div>
                  ) : null}
                </div>
              );
            })}
          </section>
        ) : null}

        {activeConnections.length ? (
          <section>
            <h3 className="fs-panel-title">{t("react.shared.live_data_connections", "Live data connections (")}{activeConnections.length})</h3>
            {activeConnections.map((connection) => {
              const decision = connectionDecisions[connection.connection_id] || {
                action: "" as const,
                target_workspace_id: "",
              };
              const actionId = `workspace-connection-action-${connection.connection_id}`;
              return (
                <div
                  className="fs-surface-radius fs-surface-inset border border-subtle p-3"
                  key={connection.connection_id}
                >
                  <p>
                    <strong>{connection.label}</strong>
                  </p>
                  <p className="fs-field-helper">
                    {connection.status} · {connection.linked_dataset_count} {t("react.shared.linked_datasets", "linked datasets")} </p>
                  <div className="fs-field">
                    <label className="fs-field-label" htmlFor={actionId}>
                      {t("react.shared.data_connection_action_for", "Data connection action for")} {connection.label}
                    </label>
                    <DsSelect
                      ariaLabel={t("react.shared.data_connection_action_for_value1", "Data connection action for {value1}", { value1: String(connection.label) })}
                      id={actionId}
                      onChange={(value) => {
                        const action = value as ConnectionDecision["action"];
                        setConnectionDecisions((current) => ({
                          ...current,
                          [connection.connection_id]: {
                            action,
                            target_workspace_id:
                              action === "reassign"
                                ? current[connection.connection_id]?.target_workspace_id || ""
                                : "",
                          },
                        }));
                      }}
                      // Every action stays offered even when the connection has
                      // linked datasets: the rule that rejects those two lives in
                      // the client validation below, and hiding them would make
                      // its message unreachable.
                      options={[
                        { value: "", label: t("react.shared.choose_action", "Choose action") },
                        { value: "block", label: t("react.shared.keep_connection_and_block_deletion", "Keep connection and block deletion") },
                        { value: "reassign", label: t("react.shared.move_connection_to_another_workspace", "Move connection to another workspace") },
                        { value: "delete", label: t("react.shared.delete_connection_with_workspace", "Delete connection with workspace") },
                      ]}
                      value={decision.action}
                    />
                  </div>
                  {decision.action === "reassign" ? (
                    <div className="fs-field">
                      <label
                        className="fs-field-label"
                        htmlFor={`workspace-connection-target-${connection.connection_id}`}
                      >
                        {t("react.shared.target_workspace_for", "Target workspace for")} {connection.label}
                      </label>
                      <DsSelect
                        ariaLabel={t("react.shared.target_workspace_for_value1", "Target workspace for {value1}", { value1: String(connection.label) })}
                        id={`workspace-connection-target-${connection.connection_id}`}
                        onChange={(value) =>
                          setConnectionDecisions((current) => ({
                            ...current,
                            [connection.connection_id]: {
                              action: "reassign",
                              target_workspace_id: value,
                            },
                          }))
                        }
                        options={targetOptions}
                        value={decision.target_workspace_id}
                      />
                    </div>
                  ) : null}
                </div>
              );
            })}
          </section>
        ) : null}

        {datasets.length ? (
          <section>
            <h3 className="fs-panel-title">{t("react.shared.datasets_2", "Datasets (")}{datasets.length})</h3>
            {datasets.map((dataset) => (
              <div
                className="fs-surface-radius fs-surface-inset border border-subtle p-3"
                key={dataset.dataset_id}
              >
                <p>
                  <strong>{dataset.label}</strong>
                </p>
                <DsBadge>{dataset.status}</DsBadge>
              </div>
            ))}
          </section>
        ) : null}

        {/* The plan lists every already-trashed child. Collapsed, they stop
            pushing the decisions — and the confirm button — off the screen. */}
        {trashedProjects.length || trashedConnections.length ? (
          <details className="fs-disclosure">
            <summary>
              <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
              {t("react.shared.already_trashed", "Already trashed (")}{trashedProjects.length + trashedConnections.length})
            </summary>
            <div className="fs-disclosure-body">
              {trashedProjects.map((project) => (
                <p key={project.project_id}>
                  {project.label} <DsBadge>{t("react.shared.already_trashed_2", "already trashed")}</DsBadge>
                </p>
              ))}
              {trashedConnections.map((connection) => (
                <p key={connection.connection_id}>
                  {connection.label} <DsBadge>{connection.status}</DsBadge>
                </p>
              ))}
            </div>
          </details>
        ) : null}
      </div>
    </DsDialog>
  );
}

export function WorkspaceDeletePlanModal({ busy = false, error = "", onClose, onConfirm, open, plan }: Readonly<Props>) {
  const planKey = useMemo(() => JSON.stringify(plan || null), [plan]);
  const initialDecisions = useMemo(() => buildInitialDecisions(plan), [plan]);
  const initialConnectionDecisions = useMemo(
    () => buildInitialConnectionDecisions(plan),
    [plan],
  );

  if (!open || !plan) {
    return null;
  }

  return (
    <WorkspaceDeletePlanModalContent
      key={planKey}
      activePlan={plan}
      busy={busy}
      error={error}
      initialDecisions={initialDecisions}
      initialConnectionDecisions={initialConnectionDecisions}
      onClose={onClose}
      onConfirm={onConfirm}
    />
  );
}
