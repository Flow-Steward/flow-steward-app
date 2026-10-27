import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useQuery } from "@tanstack/react-query";
import { RefreshCw, Search } from "lucide-react";

import { ConfirmActionDialog } from "@/components/shared/confirm-action-dialog";
import { InlineConfirm } from "@/components/shared/inline-confirm";
import {
  LifecyclePlanModal,
  type LifecyclePlan,
} from "@/components/shared/lifecycle-plan-modal";
import {
  executeLifecyclePlan,
  fetchLifecyclePlan,
  fetchTrashList,
  hardDeleteTrashItem,
  restoreTrashItem,
  type TrashItem,
} from "@/pages/trash/api";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import {
  AdvancedTableFilters,
  BulkActionBar,
  DsAlert,
  DsPageHeader,
  DsTable,
  PageContainer,
  Pagination,
  RowActionMenu,
  type DsFilterField,
} from "@/components/shared/design-system";
import { columnsFromGridConfig, visibleRowActions } from "@/components/shared/data-grid-adapter";

import "@/pages/trash/trash.css";

type TrashGridRow = {
  trash_key: string;
  category: string;
  category_label: string;
  entity_id: string;
  entity_type: string;
  entity_type_label: string;
  project_id: string;
  label: string;
  deleted_at_display: string;
  deleted_by: string;
  _raw: TrashItem;
};



function getTrashTypeOptions(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { value: "workspace", label: t("react.trash.workspaces", "Workspaces") },
  { value: "project", label: t("monitoring.stat.projects.label", "Projects") },
  { value: "job", label: t("jobs.page_title", "Jobs") },
  { value: "user", label: t("react.trash.users", "Users") },
  { value: "registry_agent", label: t("registry.agents.title", "Agents") },
  { value: "registry_tool", label: t("registry.agent_detail.tab.tools", "Tools") },
  { value: "mcp_connector", label: t("react.trash.mcp_connectors", "MCP connectors") },
  { value: "registry_skill", label: t("registry.agent_detail.tab.skills", "Skills") },
  { value: "registry_workflow", label: t("project_edit.tab.workflows", "Workflows") },
  { value: "registry_guardrail", label: t("registry.guardrails.title", "Guardrails") },
  { value: "registry_data_contract", label: t("react.trash.data_contracts", "Data contracts") },
  { value: "dataset", label: t("react.trash.datasets", "Datasets") },
  { value: "workspace_data_connection", label: t("project_detail.connections.title", "Connections") },
  { value: "project_settings_revision", label: t("react.trash.project_setting_revisions", "Project setting revisions") },
] as const;
}



function getTrashTypeLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  dataset: t("react.trash.dataset", "Dataset"),
  job: t("dashboard.context.job_fallback", "Job"),
  mcp_connector: t("react.trash.mcp_connector", "MCP Connector"),
  project: t("events.columns.project", "Project"),
  project_settings_revision: t("trash.type.project_settings_revision", "Project Settings Revision"),
  registry_agent: t("events.columns.agent", "Agent"),
  registry_data_contract: t("trash.type.registry_data_contract", "Data Contract"),
  registry_guardrail: t("registry.guardrail_detail.title_single", "Guardrail"),
  registry_skill: t("registry.skills.title_single", "Skill"),
  registry_tool: t("registry.tools.title_single", "Tool"),
  registry_workflow: t("jobs.columns.workflow", "Workflow"),
  user: t("trash.type.user", "User"),
  workspace: t("project_mode.workspace", "Workspace"),
  workspace_data_connection: t("react.trash.connection", "Connection"),
};
}



function getTrashCategoryLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  admin: t("settings.access.level.admin", "Admin"),
  application: t("react.trash.application", "Application"),
  data: t("react.trash.data", "Data"),
  registry: t("registry.nav.home", "Registry"),
  security: t("account.security.title", "Security"),
  system: t("processes.system", "System"),
};
}

function supportsLifecyclePlan(entityType: string): boolean {
  return entityType === "project" || entityType === "workspace" || entityType === "mcp_connector";
}

function buildTrashRows(items: TrashItem[], timezone: string, t: I18nContextValue["t"] = fallbackContext.t): TrashGridRow[] {
  return items.map((item) => {
    const entityType = firstStringFromUnknownValue([item.entity_type, ""]).trim();
    const entityId = firstStringFromUnknownValue([item.entity_id, ""]).trim();
    const category = firstStringFromUnknownValue([item.category, item.entity_category, ""]).trim();
    const projectId = firstStringFromUnknownValue([item.project_id, ""]).trim();
    const trashKey = projectId
      ? `${entityType}:${projectId}:${entityId}`
      : `${entityType}:${entityId}`;
    return {
      trash_key: trashKey,
      category,
      category_label: getTrashCategoryLabels(t)[category] || category.replaceAll("_", " ") || "Uncategorized",
      entity_id: entityId,
      entity_type: entityType,
      entity_type_label: getTrashTypeLabels(t)[entityType] || entityType.replaceAll("_", " "),
      project_id: projectId,
      label: firstStringFromUnknownValue([item.label, entityId, "—"]),
      // The user's configured zone, like every other timestamp in the app.
      deleted_at_display: formatUserDateTime(item.deleted_at, timezone),
      deleted_by: firstStringFromUnknownValue([item.deleted_by, "—"]),
      _raw: item,
    };
  });
}

function buildTrashGridConfig(t: I18nContextValue["t"] = fallbackContext.t): Record<string, unknown> {
  return {
    columns: [
      {
        key: "category_label",
        label: t("registry.skills.field.category", "Category"),
        type: "text",
        truncate: true,
        truncate_width: "clamp(8rem, 12vw, 12rem)",
        width: "140px",
      },
      {
        key: "entity_type_label",
        label: t("trash.columns.type", "Type"),
        type: "text",
        truncate: true,
        truncate_width: "clamp(10rem, 16vw, 14rem)",
        width: "160px",
      },
      { key: "entity_id", label: "ID", type: "text", truncate: true, truncate_width: "12rem", width: "150px" },
      {
        key: "label",
        label: t("trash.columns.name_summary", "Name / Summary"),
        type: "text",
        truncate: true,
        truncate_width: "clamp(16rem, 34vw, 34rem)",
      },
      { key: "deleted_by", label: t("trash.columns.deleted_by", "Deleted By"), type: "text", width: "150px" },
      { key: "deleted_at_display", label: t("trash.columns.deleted_at", "Deleted At"), type: "text", width: "180px" },
    ],
    row_actions: [
      { action_id: "restore", label: t("project_edit.diff.restore", "Restore"), variant: "primary" },
      { action_id: "hard_delete", label: t("trash.action.purge", "Purge"), variant: "danger" },
    ],
    bulk_actions: [
      { action_id: "bulk_restore", label: t("trash.bulk_actions.restore", "Restore Selected"), variant: "primary" },
      { action_id: "bulk_hard_delete", label: t("trash.bulk_actions.purge", "Purge Selected"), variant: "danger" },
    ],
    id_field: "trash_key",
    pagination: { page_size_default: 25 },
    empty_state: {
      title: t("trash.empty_title", "No deleted items found"),
      description: t("react.trash.try_adjusting_search_or_refresh_if_items_were_just_deleted", "Try adjusting search or refresh if items were just deleted."),
    },
  };
}


type TrashAction = "restore" | "purge";

/**
 * Where the question is asked follows the canon's routing. Restore is reversible
 * but the row leaves the list, so it is an Inline confirm: in the row for a row
 * action, in the bulk bar for a selection. Purge is irreversible: a Dialog.
 */
type TrashConfirmation = {
  kind: TrashAction;
  rows: TrashGridRow[];
  surface: "row" | "bulk" | "dialog";
  /** A row action leaves the selection alone; a bulk action owns it. */
  fromSelection: boolean;
};

type TrashOutcome = { row: TrashGridRow; error?: string };

const PER_PAGE_OPTIONS = [25, 50, 100, 200] as const;

function rowName(row: TrashGridRow): string {
  return `${row.entity_type_label} ${row.label}`;
}

function actionsLabel(row: TrashGridRow, t: I18nContextValue["t"] = fallbackContext.t): string {
  return t("react.trash.actions_for_value1", "Actions for {value1}", { value1: String(rowName(row)) });
}

function selectLabel(row: TrashGridRow, t: I18nContextValue["t"] = fallbackContext.t): string {
  return t("react.trash.select_value1", "Select {value1}", { value1: String(rowName(row)) });
}

/**
 * Focus the first target that still exists, never `body`. The first attempt is a
 * microtask, which runs after React has committed the state change and before
 * the browser paints, so the removed control is never replaced by `body` in
 * between. Waiting for frames is only the fallback when the target is not there
 * yet; frames are slow in a background window.
 */
function focusFirstAvailable(selectors: string[]) {
  const attempt = () => {
    for (const selector of selectors) {
      const target = document.querySelector<HTMLElement>(selector);
      if (target && !target.hasAttribute("disabled")) {
        target.focus();
        return true;
      }
    }
    return false;
  };
  queueMicrotask(() => {
    if (!attempt()) requestAnimationFrame(() => requestAnimationFrame(attempt));
  });
}

async function runTrashAction(kind: TrashAction, rows: TrashGridRow[], t: I18nContextValue["t"]): Promise<TrashOutcome[]> {
  const outcomes: TrashOutcome[] = [];
  for (const row of rows) {
    try {
      if (kind === "restore") {
        await restoreTrashItem(row.entity_type, row.entity_id, row.project_id);
      } else {
        await hardDeleteTrashItem(row.entity_type, row.entity_id, row.project_id);
      }
      outcomes.push({ row });
    } catch (error) {
      outcomes.push({ row, error: error instanceof Error ? error.message : t("react.trash.request_failed", "The request failed.") });
    }
  }
  return outcomes;
}

function confirmationCopy(confirmation: TrashConfirmation, t: I18nContextValue["t"] = fallbackContext.t) {
  const restoring = confirmation.kind === "restore";
  const [first] = confirmation.rows;
  const count = confirmation.rows.length;
  if (count === 1) {
    const type = first.entity_type_label;
    return {
      title: restoring ? t("react.trash.restore_item_question", "Restore {type} “{label}”?", { type: type.toLowerCase(), label: first.label }) : t("react.trash.purge_item_question", "Purge {type} “{label}”?", { type: type.toLowerCase(), label: first.label }),
      consequence: restoring
        ? t("react.trash.it_leaves_trash_and_is_available_again", "It leaves Trash and is available again.")
        : t("react.trash.it_is_deleted_permanently_not_moved_anywhere", "It is deleted permanently, not moved anywhere."),
      confirmLabel: restoring ? t("react.trash.restore_item_action", "Restore {type}", { type: type.toLowerCase() }) : t("react.trash.purge_item_action", "Purge {type}", { type: type.toLowerCase() }),
    };
  }
  return {
    title: restoring ? t("react.trash.restore_items_question", "Restore {count} items?", { count }) : t("react.trash.purge_items_question", "Purge {count} items?", { count }),
    consequence: restoring
      ? t("react.trash.they_leave_trash_and_are_available_again", "They leave Trash and are available again.")
      : t("react.trash.they_are_deleted_permanently_not_moved_anywhere", "They are deleted permanently, not moved anywhere."),
    confirmLabel: restoring ? t("react.trash.restore_items_action", "Restore {count} items", { count }) : t("react.trash.purge_items_action", "Purge {count} items", { count }),
  };
}

function createTrashRowReplacement({
  confirmation,
  error,
  pending,
  onCancel,
  onConfirm,
}: Readonly<{
  confirmation: TrashConfirmation | null;
  error: string;
  pending: boolean;
  onCancel: () => void;
  onConfirm: () => Promise<void>;
}>, t: I18nContextValue["t"] = fallbackContext.t) {
  const copy = confirmation ? confirmationCopy(confirmation, t) : null;
  return (row: Record<string, unknown>) => {
    if (confirmation?.surface !== "row" || confirmation.rows[0].trash_key !== row.trash_key) return null;
    return (
      <InlineConfirm
        confirmLabel={copy?.confirmLabel ?? ""}
        error={error}
        onCancel={onCancel}
        onConfirm={() => ignoreAsyncResult(onConfirm())}
        pending={pending}
        pendingLabel={t("react.trash.restoring", "Restoring…")}
        question={copy?.title ?? ""}
        tone="primary"
      />
    );
  };
}

function outcomeMessage(kind: TrashAction, outcomes: TrashOutcome[], t: I18nContextValue["t"] = fallbackContext.t): string {
  const done = outcomes.filter((outcome) => !outcome.error);
  if (outcomes.length === 1) {
    const [only] = outcomes;
    return kind === "restore" ? t("react.trash.item_restored_feedback", "{type} “{label}” restored.", { type: only.row.entity_type_label, label: only.row.label }) : t("react.trash.item_purged_feedback", "{type} “{label}” purged.", { type: only.row.entity_type_label, label: only.row.label });
  }
  return done.length === outcomes.length
    ? (kind === "restore" ? t("react.trash.items_restored_feedback", "{count} items restored.", { count: done.length }) : t("react.trash.items_purged_feedback", "{count} items purged.", { count: done.length }))
    : (kind === "restore" ? t("react.trash.items_restored_partial", "{count} of {total} items restored.", { count: done.length, total: outcomes.length }) : t("react.trash.items_purged_partial", "{count} of {total} items purged.", { count: done.length, total: outcomes.length }));
}

export function TrashPage() {
  const { t } = useI18n();
  const initialQuery = useMemo(() => new URLSearchParams(globalThis.location?.search || ""), []);
  const [search, setSearch] = useState(() => initialQuery.get("entity_id") || "");
  const [typeFilter, setTypeFilter] = useState(() => {
    const requested = initialQuery.get("entity_type") || "";
    return requested === "all" ? "" : requested;
  });
  const projectId = initialQuery.get("project_id") || "";
  const userTimezone = useUserTimezone();
  const [categoryFilter, setCategoryFilter] = useState("");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState<number>(PER_PAGE_OPTIONS[0]);
  const [actionError, setActionError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<TrashConfirmation | null>(null);
  const [confirmationError, setConfirmationError] = useState("");
  const [modalError, setModalError] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [modalPlan, setModalPlan] = useState<LifecyclePlan | null>(null);
  const [modalTarget, setModalTarget] = useState<{
    entityId: string;
    entityType: string;
    projectId: string;
  } | null>(null);
  const [selectedKeys, setSelectedKeys] = useState<ReadonlySet<string>>(new Set());
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const rowsByKeyRef = useRef<Record<string, TrashGridRow>>({});

  const openLifecycleModal = useCallback(
    async (
      entityType: string,
      entityId: string,
      actionType: "restore" | "hard_delete",
      targetProjectId: string,
    ) => {
      setActionBusy(true);
      setActionError("");
      setModalError("");
      try {
        const plan = await fetchLifecyclePlan(
          entityType,
          entityId,
          actionType,
          targetProjectId,
        );
        setModalPlan(plan as LifecyclePlan);
        setModalTarget({ entityType, entityId, projectId: targetProjectId });
        setModalOpen(true);
      } catch (error) {
        setActionError(error instanceof Error ? error.message : t("react.trash.failed_to_open_lifecycle_plan", "Failed to open lifecycle plan."));
      } finally {
        setActionBusy(false);
      }
    },
    [t],
  );

  const trashQuery = useQuery({
    queryKey: ["trash", "list", typeFilter, projectId],
    queryFn: () =>
      fetchTrashList({
        entityType: typeFilter,
        projectId,
        limit: 500,
      }),
    refetchInterval: 15_000,
  });
  const refetchTrash = trashQuery.refetch;

  const rows = useMemo(
    () => buildTrashRows(trashQuery.data?.items || [], userTimezone, t),
    [trashQuery.data?.items, userTimezone, t],
  );
  // Nothing is known until the first query settles. Pushing an empty dataset
  // before then makes the grid state "No deleted items found", which is a
  // definitive claim about a destructive surface - and it was wrong: a page
  // that reported zero loaded 151 items on Refresh.
  const initialLoadPending = trashQuery.isPending && !trashQuery.data;
  const categoryOptions = useMemo(() => {
    const seen = new Map<string, string>();
    rows.forEach((row) => {
      const key = row.category || "uncategorized";
      seen.set(key, row.category_label);
    });
    return Array.from(seen.entries())
      .sort((left, right) => left[1].localeCompare(right[1]))
      .map(([value, label]) => ({ value, label }));
  }, [rows]);
  useEffect(() => {
    if (categoryFilter && !categoryOptions.some((option) => option.value === categoryFilter)) {
      setCategoryFilter("");
    }
  }, [categoryOptions, categoryFilter]);
  const filteredRows = useMemo(() => {
    const query = search.trim().toLowerCase();
    return rows.filter((row) => {
      if (categoryFilter && (row.category || "uncategorized") !== categoryFilter) {
        return false;
      }
      if (!query) {
        return true;
      }
      return [row.category_label, row.entity_type_label, row.entity_id, row.label, row.deleted_by].map((value) => stringFromUnknownValue(value)).join(" ")
        .toLowerCase()
        .includes(query);
    });
  }, [rows, search, categoryFilter]);
  const pageCount = Math.max(1, Math.ceil(filteredRows.length / perPage));
  useEffect(() => {
    if (page > pageCount) setPage(pageCount);
  }, [page, pageCount]);
  const pagedRows = useMemo(
    () => filteredRows.slice((page - 1) * perPage, page * perPage),
    [filteredRows, page, perPage],
  );

  useEffect(() => {
    const rowMap: Record<string, TrashGridRow> = {};
    rows.forEach((row) => {
      rowMap[row.trash_key] = row;
    });
    rowsByKeyRef.current = rowMap;
  }, [rows]);
  // A key whose row has left Trash cannot stay selected, or the bar would count
  // an object that is no longer there. Every other key is left alone.
  useEffect(() => {
    setSelectedKeys((current) => {
      const kept = [...current].filter((key) => key in rowsByKeyRef.current);
      return kept.length === current.size ? current : new Set(kept);
    });
  }, [rows]);

  const filterFields = useMemo<DsFilterField[]>(
    () => [
      { key: "type", label: t("trash.columns.type", "Type"), options: getTrashTypeOptions(t).map((option) => ({ ...option })) },
      { key: "category", label: t("registry.skills.field.category", "Category"), options: categoryOptions },
    ],
    [categoryOptions, t],
  );

  const requestAction = useCallback(
    async (kind: TrashAction, targets: TrashGridRow[], fromSelection: boolean) => {
      if (!targets.length) return;
      setActionMessage("");
      const lifecycleRows = targets.filter((row) => supportsLifecyclePlan(row.entity_type));
      const lifecycleAction = kind === "restore" ? "restore" : "hard_delete";
      if (lifecycleRows.length > 1) {
        setActionError(
          kind === "restore"
            ? "Bulk restore for multiple workspaces/projects must be planned one container at a time."
            : "Bulk purge for multiple workspaces/projects must be planned one container at a time.",
        );
        return;
      }
      if (lifecycleRows.length === 1) {
        if (targets.length > 1) {
          setActionError(
            "Lifecycle-planned workspaces/projects cannot be mixed with other bulk trash actions. Run the container action separately.",
          );
          return;
        }
        const [row] = lifecycleRows;
        await openLifecycleModal(row.entity_type, row.entity_id, lifecycleAction, row.project_id);
        return;
      }
      setActionError("");
      setConfirmationError("");
      let surface: TrashConfirmation["surface"] = "dialog";
      if (kind === "restore") surface = fromSelection ? "bulk" : "row";
      setConfirmation({ kind, rows: targets, surface, fromSelection });
    },
    [openLifecycleModal],
  );

  const cancelConfirmation = () => {
    if (actionBusy || !confirmation) return;
    const { surface, rows: targets } = confirmation;
    setConfirmation(null);
    setConfirmationError("");
    // The object still exists, so focus returns to what opened the question.
    // The Dialog restores its own trigger; the inline surfaces do it here.
    if (surface === "row") {
      focusFirstAvailable([`[aria-label="${CSS.escape(actionsLabel(targets[0], t))}"]`]);
    } else if (surface === "bulk") {
      focusFirstAvailable(['[data-bulk-action="bulk_restore"]']);
    }
  };

  const confirmPendingAction = async () => {
    if (!confirmation) return;
    const { kind, rows: targets, fromSelection } = confirmation;
    setActionBusy(true);
    setConfirmationError("");
    try {
      const outcomes = await runTrashAction(kind, targets, t);
      const failed = outcomes.filter((outcome) => outcome.error);
      if (failed.length === outcomes.length) {
        // Nothing happened, so the question is still open: keep it on screen.
        setConfirmationError(
          failed.length === 1
            ? failed[0].error ?? "The request failed."
            : failed.map((outcome) => `${outcome.row.label}: ${outcome.error}`).join("; "),
        );
        return;
      }
      const doneKeys = new Set(outcomes.filter((outcome) => !outcome.error).map((outcome) => outcome.row.trash_key));
      setConfirmation(null);
      setActionMessage(outcomeMessage(kind, outcomes, t));
      setActionError(
        failed.length
          ? failed.map((outcome) => `${outcome.row.label}: ${outcome.error}`).join("; ")
          : "",
      );
      if (fromSelection) {
        // Bulk rules: a full success clears the selection; a failed item stays
        // selected so it can be retried. A row action never touches selection.
        setSelectedKeys(new Set(failed.map((outcome) => outcome.row.trash_key)));
        // The bar no longer has a selection to act on, so focus goes to the
        // region heading rather than back to the bar.
        focusFirstAvailable(["#trash-title"]);
        await refetchTrash();
        return;
      }
      // Focus moves before the list reloads, so it never waits on body: the
      // next surviving row, then the previous one, then the page title.
      const visible = pagedRows.map((row) => row.trash_key);
      const firstDone = visible.findIndex((key) => doneKeys.has(key));
      const after = visible.slice(firstDone + 1).find((key) => !doneKeys.has(key));
      const before = visible.slice(0, Math.max(firstDone, 0)).reverse().find((key) => !doneKeys.has(key));
      const selectors = [after, before]
        .map((key) => (key ? rowsByKeyRef.current[key] : undefined))
        .filter(isTruthy)
        .map((row) => `[aria-label="${CSS.escape(actionsLabel(row, t))}"]`);
      focusFirstAvailable([...selectors, "#trash-title"]);
      await refetchTrash();
    } finally {
      setActionBusy(false);
    }
  };

  const confirmModal = useCallback(
    async (selectedNodeIds: string[]) => {
      if (!modalTarget || !modalPlan) {
        return;
      }
      setActionBusy(true);
      setModalError("");
      try {
        await executeLifecyclePlan(
          modalTarget.entityType,
          modalTarget.entityId,
          modalPlan.action_type as "restore" | "hard_delete",
          selectedNodeIds,
          modalPlan.plan_version,
          modalTarget.projectId,
        );
        setModalOpen(false);
        setModalPlan(null);
        setModalTarget(null);
        await refetchTrash();
      } catch (error) {
        setModalError(error instanceof Error ? error.message : t("react.trash.lifecycle_action_failed", "Lifecycle action failed."));
      } finally {
        setActionBusy(false);
      }
    },
    [modalPlan, modalTarget, refetchTrash, t],
  );

  const trashConfig = useMemo(() => buildTrashGridConfig(t), [t]);

  const trashColumns = useMemo(
    () =>
      columnsFromGridConfig(trashConfig, (row) => {
        const trashRow = row as unknown as TrashGridRow;
        return (
          <RowActionMenu
            label={actionsLabel(trashRow, t)}
            onOpenChange={(open) => setRowMenu(open ? trashRow.trash_key : null)}
            open={rowMenu === trashRow.trash_key}
          >
            {visibleRowActions(trashConfig, row).map((action) => (
              <div className="fs-row-menu-group" key={action.action_id}>
                <button
                  className={action.variant === "danger" ? "danger" : undefined}
                  onClick={() => {
                    setRowMenu(null);
                    ignoreAsyncResult(requestAction(action.action_id === "restore" ? "restore" : "purge", [trashRow], false));
                  }}
                  role="menuitem"
                  type="button"
                >
                  {action.label}
                </button>
              </div>
            ))}
          </RowActionMenu>
        );
      }),
    [requestAction, rowMenu, trashConfig, t],
  );

  const filtering = Boolean(search.trim() || typeFilter || categoryFilter);
  let tableState = null;
  if (initialLoadPending) {
    tableState = (
      <div className="fs-table-state">
        <strong>{t("react.trash.loading_deleted_items", "Loading deleted items")}</strong>
        <span>{t("react.trash.the_table_keeps_its_shape_while_trash_loads", "The table keeps its shape while Trash loads.")}</span>
      </div>
    );
  } else if (!filteredRows.length) {
    tableState = (
      <div className="fs-table-state">
        <strong>{filtering ? t("react.trash.no_deleted_items_match", "No deleted items match") : t("trash.empty_title", "No deleted items found")}</strong>
        <span>{filtering ? t("react.trash.change_or_clear_the_search_and_filters", "Change or clear the search and filters.") : t("react.trash.items_land_here_when_they_are_deleted", "Items land here when they are deleted.")}</span>
      </div>
    );
  }

  const copy = confirmation ? confirmationCopy(confirmation, t) : null;
  const rowReplacement = createTrashRowReplacement({
    confirmation,
    error: confirmationError,
    pending: actionBusy,
    onCancel: cancelConfirmation,
    onConfirm: confirmPendingAction,
  }, t);

  return (
    <PageContainer className="fs-page-sections" width="wide">
      <DsPageHeader
        // Refresh reloads the whole page's data, so it sits in the header as it
        // does on the registry lists, not among the table's own filters.
        action={
          <button
            className="fs-button secondary"
            disabled={actionBusy || trashQuery.isFetching}
            onClick={() => {
              ignoreAsyncResult(refetchTrash());
            }}
            type="button"
          >
            <RefreshCw aria-hidden="true" />
            {t("account.tokens.refresh", "Refresh")} </button>
        }
        description={t("react.trash.restore_deleted_items_or_purge_them_for_good_workspaces_projects_and_mcp_connectors_are_restored_and_purged_th", "Restore deleted items, or purge them for good. Workspaces, projects and MCP connectors are restored and purged through a plan.")}
        eyebrow={t("shell.nav.system", "System")}
        title={t("settings.retention.trash.title", "Trash")}
        titleId="trash-title"
      />

      <article className="fs-card fs-table-card" data-testid="trash-grid" id="trash-grid">
        <div className="fs-table-toolbar">
          <AdvancedTableFilters
            fields={filterFields}
            onChange={(key, value) => {
              if (key === "type") setTypeFilter(value);
              if (key === "category") setCategoryFilter(value);
              setPage(1);
            }}
            onClear={() => {
              setTypeFilter("");
              setCategoryFilter("");
              setPage(1);
            }}
            values={{ type: typeFilter, category: categoryFilter }}
          />
          <label className="fs-search">
            <Search aria-hidden="true" />
            <input
              aria-label={t("react.trash.search_deleted_items", "Search deleted items")}
              id="trash-search"
              onChange={(event) => {
                setSearch(event.target.value);
                setPage(1);
              }}
              placeholder={t("react.trash.search_trash", "Search trash…")}
              type="search"
              value={search}
            />
          </label>
          <span aria-live="polite" className="fs-trash-count">
            {initialLoadPending ? "" : t("react.trash.showing_value1_of_value2", "Showing {value1} of {value2}", { value1: String(filteredRows.length), value2: String(rows.length) })}
          </span>
        </div>
        <DsTable
          ariaLabel={t("settings.retention.trash.title", "Trash")}
          busy={trashQuery.isFetching && !initialLoadPending}
          columns={trashColumns}
          onSelectionChange={setSelectedKeys}
          rowKey={(row) => firstStringFromUnknownValue([row.trash_key, ""])}
          rows={pagedRows}
          selectable
          selectedKeys={selectedKeys}
          rowReplacement={rowReplacement}
          selectionLabel={(row) => selectLabel(row as unknown as TrashGridRow, t)}
          state={tableState}
        />
        <Pagination
          onPageChange={setPage}
          onPerPageChange={(nextPerPage) => {
            setPerPage(nextPerPage);
            setPage(1);
          }}
          page={page}
          perPage={perPage}
          perPageOptions={PER_PAGE_OPTIONS}
          total={filteredRows.length}
        />
        <BulkActionBar
          actions={[
            { id: "bulk_restore", label: t("trash.bulk_actions.restore", "Restore Selected") },
            { id: "bulk_hard_delete", label: t("trash.bulk_actions.purge", "Purge Selected"), variant: "danger" },
          ]}
          busy={actionBusy}
          confirmation={
            confirmation?.surface === "bulk" ? (
              <InlineConfirm
                confirmLabel={copy?.confirmLabel ?? ""}
                error={confirmationError}
                onCancel={cancelConfirmation}
                onConfirm={() => ignoreAsyncResult(confirmPendingAction())}
                pending={actionBusy}
                pendingLabel={t("react.trash.restoring", "Restoring…")}
                question={copy?.title ?? ""}
                tone="primary"
              />
            ) : undefined
          }
          count={selectedKeys.size}
          onAction={(actionId) => {
            const targets = [...selectedKeys].map((key) => rowsByKeyRef.current[key]).filter(isTruthy);
            ignoreAsyncResult(requestAction(actionId === "bulk_restore" ? "restore" : "purge", targets, true));
          }}
          onClear={() => setSelectedKeys(new Set())}
        />
      </article>

      {actionMessage ? <DsAlert tone={actionError ? "warning" : "success"} title={actionMessage} /> : null}
      {actionError ? (
        <DsAlert title={t("react.trash.not_completed", "Not completed")} tone="danger">
          {actionError}
        </DsAlert>
      ) : null}
      {trashQuery.isError ? (
        <DsAlert title={t("react.trash.trash_could_not_be_loaded", "Trash could not be loaded")} tone="danger">
          {trashQuery.error instanceof Error ? trashQuery.error.message : t("react.trash.failed_to_load_trash", "Failed to load trash.")}
        </DsAlert>
      ) : null}

      <ConfirmActionDialog
        closeLabel={copy ? `Dismiss ${copy.title}` : undefined}
        confirmLabel={copy?.confirmLabel ?? ""}
        consequence={copy?.consequence ?? ""}
        error={confirmationError}
        eyebrow={t("settings.retention.trash.title", "Trash")}
        irreversible
        objects={
          confirmation && confirmation.rows.length > 1
            ? confirmation.rows.map((row) => `${row.entity_type_label} · ${row.label}`)
            : []
        }
        onCancel={cancelConfirmation}
        onConfirm={() => ignoreAsyncResult(confirmPendingAction())}
        open={confirmation?.surface === "dialog"}
        pending={actionBusy}
        pendingLabel={t("react.trash.purging", "Purging…")}
        title={copy?.title ?? ""}
      />

      <LifecyclePlanModal
        busy={actionBusy}
        error={modalError}
        onClose={() => {
          setModalOpen(false);
          setModalPlan(null);
          setModalTarget(null);
          setModalError("");
        }}
        onConfirm={(selectedNodeIds) => {
          ignoreAsyncResult(confirmModal(selectedNodeIds));
        }}
        open={modalOpen}
        plan={modalPlan}
      />
    </PageContainer>
  );
}
