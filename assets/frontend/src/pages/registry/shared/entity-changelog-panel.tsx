import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";

import { useQuery } from "@tanstack/react-query";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";

import { DsAlert, DsBadge, DsDrawer } from "@/components/shared/design-system";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { useConfirmDialog } from "@/components/shared/use-confirm-dialog";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import {
  fetchChangelogDiff,
  restoreChangelogEntry,
} from "@/pages/registry/changelog/api";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  changelogActionLabel,
  changelogComparisonLabel,
  changelogRevisionLabel,
  changelogRevisionRestoreCopy,
  changelogRevisionPresentation,
  workflowChangePathLabel,
} from "@/pages/registry/shared/changelog-revision";
import "@/pages/registry/shared/entity-changelog-panel.css";

type HistoryRow = Record<string, unknown>;

type RegistryEntityChangelogPanelProps = {
  entityType: string;
  entityId: string;
  rows: HistoryRow[];
  loading: boolean;
  emptyMessage?: string;
  onRestored?: () => Promise<void> | void;
  /** The revision the page shows; restore refuses when the entity changed since. */
  expectedRevision?: number;
  /** Why restore is not offered right now, such as unapplied local edits. */
  restoreDisabledReason?: string;
  /** What a restore does and does not bring back, in place of the generic note. */
  restoreNote?: string;
};

function normalizeEntryRef(value: unknown): string {
  return firstStringFromUnknownValue([value, ""]).trim();
}

function entryRef(row: HistoryRow): string {
  return (
    normalizeEntryRef(row.entry_id) ||
    normalizeEntryRef(row.id) ||
    normalizeEntryRef(row.revision_id) ||
    ""
  );
}

function rowKey(row: HistoryRow, index: number): string {
  const candidateEntry = firstStringFromUnknownValue([row.entry_id, ""]).trim();
  if (candidateEntry) {
    return `entry:${candidateEntry}`;
  }
  const candidateId = firstStringFromUnknownValue([row.id, ""]).trim();
  if (candidateId) {
    return `id:${candidateId}`;
  }
  const candidateRevision = firstStringFromUnknownValue([row.revision_id, ""]).trim();
  if (candidateRevision) {
    return `revision:${candidateRevision}`;
  }
  return `row:${index}`;
}

function objectOrEmpty(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function listFromUnknown(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter(
        (row): row is Record<string, unknown> =>
          Boolean(row) && typeof row === "object",
      )
    : [];
}

function normalizeText(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    return stringFromUnknownValue(value);
  }
  if (Array.isArray(value)) {
    return t("react.registry.value0_items", "{value0} items", { value0: value.length });
  }
  // AC17-R100: "object" is not a value a reader can act on, and it reads like
  // content rather than like absence. A field whose shape we cannot render is
  // empty as far as the page is concerned, so it gets the same marker as empty.
  return "\u2014";
}

function ChangelogRows({
  entityType,
  onSelect,
  resolvedSelectedRowKey,
  rows,
  userTimezone,
}: Readonly<{
  onSelect: (key: string) => void;
  entityType: string;
  resolvedSelectedRowKey: string;
  rows: HistoryRow[];
  userTimezone: string;
}>) {
  const { t } = useI18n();
  return (
    <div className="fs-changelog__list">
      {rows.map((row, index) => {
        const computedRowKey = rowKey(row, index);
        const reference = entryRef(row) || "—";
        return (
          <button aria-current={computedRowKey === resolvedSelectedRowKey ? "true" : undefined} className="fs-changelog__row" key={`${computedRowKey}-${index}`} onClick={() => onSelect(computedRowKey)} type="button">
            <span className="fs-changelog__row-head">
              <span className="fs-changelog__row-ref">{changelogRevisionLabel(entityType, row, t)}</span>
              <span className="fs-changelog__row-ref">{t("react.registry.entry_variant_965fdbac", "Entry #")}{reference}</span>
              <span className="fs-changelog__row-time">{formatUserDateTime(row.created_at || row.timestamp || row.updated_at, userTimezone)}</span>
            </span>
            <span className="fs-changelog__row-meta">{t("react.registry.action", "action:")}{" "}{normalizeText(changelogActionLabel(row.action || row.event || row.kind, t), t)} {" "}{t("react.registry.author", "• author:")}{" "}{normalizeText(row.author || row.actor || row.username, t)} {" "}{t("react.registry.source", "• source:")} {" "}{normalizeText(row.source || row.origin || row.provider, t)}</span>
          </button>
        );
      })}
    </div>
  );
}

function ChangelogDiffContent({
  diffChanges,
  diffError,
  diffLoading,
  diffSummaryRow,
  entityId,
  entityType,
  onRestore,
  previousEntry,
  previousEntryId,
  restoreDisabledReason,
  restoreNote,
  restoring,
  selectedEntry,
  selectedEntryRef,
  revisionPresentation,
}: Readonly<{
  diffChanges: Array<Record<string, unknown>>;
  diffError: unknown;
  diffLoading: boolean;
  diffSummaryRow: Record<string, unknown>;
  entityId: string;
  entityType: string;
  onRestore: () => void;
  previousEntry: HistoryRow | undefined;
  previousEntryId: string | null;
  restoreDisabledReason?: string;
  restoreNote?: string;
  restoring: boolean;
  selectedEntry: HistoryRow | null;
  selectedEntryRef: string | null;
  revisionPresentation: ReturnType<typeof changelogRevisionPresentation>;
}>) {
  const { t } = useI18n();
  const diffLoaderVisible = useLoaderDelay(diffLoading);
  return (
    <div className="fs-changelog__drawer-body">
      <p className="fs-changelog__compare">{revisionPresentation.explanation || (previousEntryId ? changelogComparisonLabel(entityType, previousEntry, selectedEntry, previousEntryId, selectedEntryRef || "", t) : t("react.registry.no_previous_revision_available_for_the_selected_entry", "No previous revision available for the selected entry."))}</p>
      {revisionPresentation.canRestore ? <div className="fs-changelog__restore-bar">
        <span className="fs-changelog__restore-note">{restoreDisabledReason || restoreNote || <>{t("react.registry.restore_applies_the_selected_revision_to", "Restore applies the selected revision to")}{" "}{entityType}/{entityId}.</>}</span>
        <button className="fs-button primary" disabled={!selectedEntryRef || restoring || Boolean(restoreDisabledReason)} onClick={onRestore} type="button">{restoring ? t("react.registry.restoring", "Restoring...") : t("react.registry.restore_this_revision", "Restore this revision")}</button>
      </div> : null}
      {diffLoaderVisible ? <span className="fs-changelog__loading"><DsLoader variant="orbit-inline" />{t("react.registry.loading_diff", "Loading diff…")}</span> : null}
      {diffError ? <DsAlert tone="danger" title={diffError instanceof Error ? diffError.message : t("react.registry.failed_to_load_diff", "Failed to load diff.")} /> : null}
      {revisionPresentation.canCompareContent && !diffLoading && previousEntryId && !diffChanges.length ? <p className="fs-empty-copy">{t("react.registry.no_explicit_diff_rows_were_returned", "No explicit diff rows were returned.")}</p> : null}
      {Object.keys(diffSummaryRow).length ? (
        <div className="fs-changelog__summary">{Object.entries(diffSummaryRow).map(([key, value]) => <DsBadge key={key}>{key}: {normalizeText(value, t)}</DsBadge>)}</div>
      ) : null}
      <div className="fs-changelog__changes">
        {diffChanges.map((change, index) => (
          <div className="fs-changelog__change" key={`${normalizeText(change.path || change.key, t)}-${index}`}>
            <span className="fs-changelog__change-head">
              <span className="fs-changelog__change-path">{normalizeText(workflowChangePathLabel(entityType, change.path || change.key || `change-${index + 1}`, t), t)}
            {workflowChangePathLabel(entityType, change.path || change.key || "", t) !== normalizeText(change.path || change.key || "", t)
              ? <code className="ml-2 text-xs opacity-70">{normalizeText(change.path || change.key, t)}</code>
              : null}</span>
              <span className="fs-changelog__change-kind">{normalizeText(change.change || change.type || "updated", t)}</span>
            </span>
            <span className="fs-changelog__change-values">{t("project_edit.diff.from", "from:")}{" "}{normalizeText(Object.prototype.hasOwnProperty.call(change, "from") ? change.from : change.before, t)} {" "}{t("react.registry.to", "• to:")}{" "}{normalizeText(Object.prototype.hasOwnProperty.call(change, "to") ? change.to : change.after, t)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function RegistryEntityChangelogPanel({
  entityType,
  entityId,
  rows,
  loading,
  emptyMessage,
  onRestored,
  expectedRevision,
  restoreDisabledReason,
  restoreNote,
}: Readonly<RegistryEntityChangelogPanelProps>) {
  const { t } = useI18n();
  const userTimezone = useUserTimezone();
  const historyLoaderVisible = useLoaderDelay(loading);
  const [selectedRowKey, setSelectedRowKey] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [restoreMessage, setRestoreMessage] = useState("");
  // Which published status role the result belongs in; the message alone
  // cannot tell success from failure.
  const [restoreFailed, setRestoreFailed] = useState(false);
  const { confirmDialog, requestConfirmation } = useConfirmDialog();

  useEffect(() => {
    if (!rows.length) {
      setSelectedRowKey("");
      return;
    }
    const currentExists = rows.some(
      (row, index) => rowKey(row, index) === selectedRowKey,
    );
    if (!currentExists) {
      setSelectedRowKey(rowKey(rows[0] || {}, 0));
    }
  }, [rows, selectedRowKey]);

  const resolvedSelectedRowKey =
    selectedRowKey || (rows.length ? rowKey(rows[0] || {}, 0) : "");

  const selectedEntry = useMemo(() => {
    if (!rows.length) {
      return null;
    }
    const selectedIndex = rows.findIndex(
      (row, index) => rowKey(row, index) === resolvedSelectedRowKey,
    );
    if (selectedIndex >= 0) {
      return rows[selectedIndex];
    }
    return rows[0];
  }, [resolvedSelectedRowKey, rows]);

  const selectedEntryRef = selectedEntry ? entryRef(selectedEntry) : null;
  const selectedIndex = rows.findIndex(
    (row, index) => rowKey(row, index) === resolvedSelectedRowKey,
  );
  const previousEntry =
    selectedIndex >= 0 ? rows[selectedIndex + 1] : undefined;
  const previousEntryId = previousEntry ? entryRef(previousEntry) : null;
  const revisionPresentation = changelogRevisionPresentation(selectedEntry, previousEntry, t);

  const diffQuery = useQuery({
    queryKey: [
      "registry",
      "entity-changelog",
      "diff",
      entityType,
      entityId,
      previousEntryId || "",
      selectedEntryRef || "",
    ] as const,
    queryFn: () =>
      fetchChangelogDiff({
        entityType,
        entityId,
        fromEntryId: previousEntryId || undefined,
        toEntryId: selectedEntryRef || undefined,
      }),
    enabled: Boolean(
      entityType && entityId && previousEntryId && selectedEntryRef && revisionPresentation.canCompareContent,
    ),
  });

  const diffPayload = revisionPresentation.canCompareContent ? objectOrEmpty(diffQuery.data) : {};
  const diffSummary = diffPayload.summary;
  const diffSummaryRow = objectOrEmpty(diffSummary);
  const diffChanges = listFromUnknown(diffPayload.changes);

  async function restoreSelectedEntry( ) {
    if (!entityType || !entityId || !selectedEntryRef || !revisionPresentation.canRestore || restoreDisabledReason) {
      return;
    }
    const restoreCopy = changelogRevisionRestoreCopy(entityType, entityId, selectedEntry, selectedEntryRef, t);
    const confirmed = await requestConfirmation({
      body: restoreCopy.body,
      confirmLabel: t("trash.row_actions.restore", "Restore"),
      title: restoreCopy.title,
    });
    if (!confirmed) {
      return;
    }
    setRestoring(true);
    setRestoreMessage("");
    setRestoreFailed(false);
    try {
      await restoreChangelogEntry({
        entityType,
        entityId,
        entryId: selectedEntryRef,
        expectedRevision,
      });
      setModalOpen(false);
      setRestoreMessage(
        restoreCopy.success,
      );
      if (onRestored) {
        await onRestored();
      }
    } catch (error) {
      setRestoreFailed(true);
      setRestoreMessage(
        error instanceof Error ? error.message : t("react.registry.restore_failed", "Restore failed."),
      );
    } finally {
      setRestoring(false);
    }
  }

  return (
    <div className="fs-changelog">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{t("react.registry.revision_history", "Revision history")}</h2>
          <p>{t("react.registry.select_a_row_to_open_revision_details", "Select a row to open revision details.")}</p>
        </div>
      </div>

      {historyLoaderVisible ? (
        <span className="fs-changelog__loading">
          <DsLoader variant="orbit-inline" />
          {t("react.registry.loading_history", "Loading history…")}</span>
      ) : null}

      {!loading && !rows.length ? (
        <p className="fs-empty-copy">{emptyMessage || t("react.registry.display_no_history_entries", "No history entries available.")}</p>
      ) : null}

      <ChangelogRows
        entityType={entityType}
        onSelect={(key) => { setSelectedRowKey(key); setModalOpen(true); }}
        resolvedSelectedRowKey={resolvedSelectedRowKey}
        rows={rows}
        userTimezone={userTimezone}
      />

      {restoreMessage ? (
        <DsAlert tone={restoreFailed ? "danger" : "success"} title={restoreMessage} />
      ) : null}

      <DsDrawer
        onClose={() => setModalOpen(false)}
        open={modalOpen}
        testId="changelog-revision-drawer"
        title={revisionPresentation.title}
      >
        <ChangelogDiffContent
          diffChanges={diffChanges}
          diffError={revisionPresentation.canCompareContent && diffQuery.isError ? diffQuery.error : null}
          diffLoading={revisionPresentation.canCompareContent && diffQuery.isLoading}
          diffSummaryRow={diffSummaryRow}
          entityId={entityId}
          entityType={entityType}
          onRestore={() => ignoreAsyncResult(restoreSelectedEntry( ))}
          previousEntry={previousEntry}
          previousEntryId={previousEntryId}
          restoreDisabledReason={restoreDisabledReason}
          restoreNote={restoreNote}
          restoring={restoring}
          selectedEntry={selectedEntry}
          selectedEntryRef={selectedEntryRef}
          revisionPresentation={revisionPresentation}
        />
      </DsDrawer>
      {/* The revision drawer is portaled to the body; its confirmation must sit above it,
          also where the panel lives inside a transformed container such as a builder drawer. */}
      {confirmDialog.open && globalThis.document !== undefined
        ? createPortal(<ConfirmDialog {...confirmDialog} />, document.body)
        : null}
    </div>
  );
}
