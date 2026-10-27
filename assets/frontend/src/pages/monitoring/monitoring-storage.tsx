import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { Trash2 } from "lucide-react";
import { DsAlert, DsBadge, DsCard, DsTable, DsValueChip, type DsTableColumn } from "@/components/shared/design-system";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { type Entity, type OrphanStorageRow, displayValue, labelFromKey, unknownRows, orphanStorageScope, orphanStorageTitle, orphanCleanupSupported, orphanStorageDetailEntries } from "./monitoring-data";
import { DetailRow, PanelHeading, EmptyState } from "./monitoring-display";

function OrphanStorageItem({
  row,
  cleaning,
  onClean,
}: Readonly<{
  row: OrphanStorageRow;
  cleaning: boolean;
  onClean: (row: OrphanStorageRow) => void;
}>) {
  const { t } = useI18n();
  const scope = orphanStorageScope(row);
  const cleanupSupported = orphanCleanupSupported(row);
  const detailEntries = orphanStorageDetailEntries(row);
  return (
    <div className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4">
      <div className="fs-panel-heading">
        <div>
          <h3 className="fs-panel-title">{orphanStorageTitle(row, t)}</h3>
          {scope ? (
            <p>
              {t("registry.guardrails.column.scope", "Scope")} <DsValueChip>{scope}</DsValueChip>
            </p>
          ) : null}
        </div>
        <div className="fs-monitoring-status">
          <DsBadge tone="danger">{firstStringFromUnknownValue([row.backend, "orphan"])}</DsBadge>
          {cleanupSupported ? (
            <button className="fs-button danger" disabled={cleaning} onClick={() => onClean(row)} type="button">
              <Trash2 aria-hidden="true" />
              <span>{cleaning ? t("react.monitoring.cleaning", "Cleaning") : t("react.monitoring.clean_orphan_data", "Clean orphan data")}</span>
            </button>
          ) : null}
        </div>
      </div>
      {row.resolution_hint ? <p className="fs-ink-secondary">{displayValue(row.resolution_hint)}</p> : null}
      {detailEntries.length ? (
        <div className="fs-detail-rows" data-columns="2">
          {detailEntries.map(([key, value]) => (
            <DetailRow key={key} label={labelFromKey(key, t)} title={value} value={value} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function OrphanStorageGroup({
  rows,
  feedback,
  loading,
  pending,
  target,
  onClean,
}: Readonly<{
  rows: OrphanStorageRow[];
  feedback: { message: string; tone: "success" | "danger" } | null;
  loading: boolean;
  pending: boolean;
  target: string;
  onClean: (row: OrphanStorageRow) => void;
}>) {
  const { t } = useI18n();
  return (
    <div className="fs-panel-group">
      <PanelHeading
        description={t("react.monitoring.scopes_with_no_matching_project_row_trashed_project_data_is_retained_for_restore_and_removed_on_hard_delete", "Scopes with no matching project row. Trashed project data is retained for restore and removed on hard delete.")}
        title={t("react.monitoring.orphaned_scope_data", "Orphaned scope data")}
      />
      {feedback ? <DsAlert title={feedback.message} tone={feedback.tone} /> : null}
      {rows.map((row, index) => {
        const rowTarget = `${row.backend}:${orphanStorageScope(row)}`;
        return (
          <OrphanStorageItem
            cleaning={pending && target === rowTarget}
            key={`${rowTarget}:${index}`}
            onClean={onClean}
            row={row}
          />
        );
      })}
      {!rows.length && !loading ? (
        <article className="fs-card fs-table-card">
          <div className="fs-table-state">
            <strong>{t("react.monitoring.no_orphaned_storage_groups_detected", "No orphaned storage groups detected.")}</strong>
            <span>{t("react.monitoring.project_storage_is_either_active_retained_for_trash_restore_or_already_clean", "Project storage is either active, retained for trash restore, or already clean.")}</span>
          </div>
        </article>
      ) : null}
    </div>
  );
}

function storageColumns(kind: "memory" | "vector", t: I18nContextValue["t"] = fallbackContext.t): Array<DsTableColumn<Entity>> {
  const columns: Array<DsTableColumn<Entity>> = [
    {
      key: "project",
      header: t("events.columns.project", "Project"),
      cell: (row) => firstStringFromUnknownValue([row.project_name, row.project_id, t("dashboard.classic.breakdown.unknown_project", "Unknown")]),
    },
    {
      key: "state",
      header: t("monitoring.table.state", "State"),
      cell: (row) => <DsBadge>{firstStringFromUnknownValue([row.project_state, "unknown"])}</DsBadge>,
    },
    { key: "records", header: t("react.monitoring.records", "Records"), align: "right", cell: (row) => stringFromUnknownValue(row.records ?? 0) },
  ];
  if (kind === "memory") {
    columns.push(
      { key: "entities", header: t("react.monitoring.entities", "Entities"), align: "right", cell: (row) => stringFromUnknownValue(row.entities ?? 0) },
      { key: "edges", header: t("react.monitoring.edges", "Edges"), align: "right", cell: (row) => stringFromUnknownValue(row.edges ?? 0) },
    );
  }
  return columns;
}

function StorageBackendGroup({
  title,
  storage,
  kind,
  loading,
}: Readonly<{
  title: string;
  storage: Entity;
  kind: "memory" | "vector";
  loading: boolean;
}>) {
  const { t } = useI18n();
  const rows = unknownRows(storage.rows).slice(0, 20);
  const description =
    kind === "vector"
      ? t("react.monitoring.backend_value1_count_mode_value2", "Backend {value1} · count mode {value2}", { value1: String(firstStringFromUnknownValue([storage.backend, "null"])), value2: String(firstStringFromUnknownValue([storage.count_mode, "n/a"])) })
      : undefined;
  return (
    <div className="fs-panel-group">
      <PanelHeading description={description} title={title} />
      <article className="fs-card fs-table-card">
        <DsTable
          ariaLabel={t("react.monitoring.value1_storage", "{value1} storage", { value1: String(title) })}
          busy={loading}
          columns={storageColumns(kind, t)}
          rowKey={(row) => firstStringFromUnknownValue([row.storage_scope, row.project_id, row.project_name, ""])}
          rows={rows}
          state={!loading && !rows.length ? <EmptyState>{t("react.monitoring.no_project_rows_reported", "No project rows reported.")}</EmptyState> : null}
        />
      </article>
    </div>
  );
}

export function MonitoringStorage({
  backendMatrix,
  summary,
  memory,
  vector,
  orphanRows,
  feedback,
  loading,
  cleanupPending,
  cleanupTarget,
  onClean,
}: Readonly<{
  backendMatrix: Entity;
  summary: Entity;
  memory: Entity;
  vector: Entity;
  orphanRows: OrphanStorageRow[];
  feedback: { message: string; tone: "success" | "danger" } | null;
  loading: boolean;
  cleanupPending: boolean;
  cleanupTarget: string;
  onClean: (row: OrphanStorageRow) => void;
}>) {
  const { t } = useI18n();
  const summaryEntries = Object.entries(summary);
  const backendEntries = Object.entries(backendMatrix);
  return (
    <>
      <DsCard description={t("react.monitoring.backend_scoped_project_data_and_orphaned_leftovers", "Backend-scoped project data and orphaned leftovers.")} title={t("react.monitoring.storage_summary", "Storage summary")}>
        {summaryEntries.length ? (
          <div className="fs-detail-rows" data-columns="2">
            {summaryEntries.map(([key, value]) => (
              <DetailRow key={key} label={labelFromKey(key, t)} title={displayValue(value)} value={displayValue(value)} />
            ))}
          </div>
        ) : null}
        {!loading && !summaryEntries.length ? <p className="fs-empty-copy">{t("react.monitoring.no_storage_summary_returned", "No storage summary returned.")}</p> : null}
      </DsCard>
      <DsCard description={t("react.monitoring.configured_memory_and_vector_backends", "Configured memory and vector backends.")} title={t("react.monitoring.backend_inventory", "Backend inventory")}>
        {backendEntries.length ? (
          <div className="fs-detail-rows" data-columns="2">
            {backendEntries.map(([key, value]) => (
              <DetailRow key={key} label={labelFromKey(key, t)} value={<DsValueChip>{value ? displayValue(value) : t("react.monitoring.unset", "unset")}</DsValueChip>} />
            ))}
          </div>
        ) : null}
        {!loading && !backendEntries.length ? <p className="fs-empty-copy">{t("react.monitoring.no_backend_inventory_returned", "No backend inventory returned.")}</p> : null}
      </DsCard>
      <OrphanStorageGroup
        feedback={feedback}
        loading={loading}
        onClean={onClean}
        pending={cleanupPending}
        rows={orphanRows}
        target={cleanupTarget}
      />
      <StorageBackendGroup kind="memory" loading={loading} storage={memory} title={t("react.monitoring.memory", "Memory")} />
      <StorageBackendGroup kind="vector" loading={loading} storage={vector} title={t("projects.grid.column.vector", "Vector")} />
    </>
  );
}
