import { useI18n } from "@/lib/i18n/useI18n";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useState } from "react";
import { useSearchParams } from "react-router";

import { Card, CardContent } from "@/components/ui/card";
import { useAuth } from "@/lib/auth/useAuth";
import { queryKeys } from "@/lib/query/keys";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import {
  fetchProjectHistory,
  fetchProjectHistoryDiff,
  restoreProjectHistoryRevision,
} from "@/pages/projects/history/api";
import { DsDialog } from "@/components/shared/design-system";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";

type Entity = Record<string, unknown>;

function objectOrEmpty(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

function normalizeValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "string" || typeof value === "number") {
    return stringFromUnknownValue(value);
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (Array.isArray(value)) {
    return `${value.length} items`;
  }
  // AC17-R100: "object" is not a value a reader can act on, and it reads like
  // content rather than like absence. A field whose shape we cannot render is
  // empty as far as the page is concerned, so it gets the same marker as empty.
  return "\u2014";
}

function decodeFlashValue(value: string): string {
  if (!value) {
    return "";
  }
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

type HistoryEntriesProps = {
  error: unknown;
  isLoading: boolean;
  onSelect: (revisionId: string) => void;
  rows: Entity[];
  userTimezone: string;
};

function HistoryEntries({
  error,
  isLoading,
  onSelect,
  rows,
  userTimezone,
}: Readonly<HistoryEntriesProps>) {
  const { t } = useI18n();
  if (isLoading) {
    return (
      <div className="inline-flex items-center gap-2 text-sm fs-ink-secondary">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t("react.projects.loading_history", "Loading history…")} </div>
    );
  }
  if (error) {
    return (
      <div className="fs-surface-radius border border-danger fs-surface-danger px-3 py-2 text-sm fs-ink-danger">
        {error instanceof Error ? error.message : t("react.projects.failed_to_load_history", "Failed to load history.")}
      </div>
    );
  }
  if (!rows.length) {
    return (
      <div className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-secondary">
        {t("react.projects.no_history_entries_found", "No history entries found.")} </div>
    );
  }
  return rows.map((row, index) => {
    const entryId = firstStringFromUnknownValue([row.id, index]);
    const timestamp = formatUserDateTime(row.created_at, userTimezone);
    const author = firstStringFromUnknownValue([row.author, ""]).trim();
    const action = firstStringFromUnknownValue([row.action, ""]).trim();
    const source = normalizeValue(row.source);
    return (
      <div
        className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-3 text-sm fs-ink-secondary"
        key={entryId}
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{t("react.projects.revision", "Revision")} {entryId}</span>
          {action ? (
            <span className="rounded-full border border-subtle px-2 py-0.5 text-[11px] uppercase">
              {action}
            </span>
          ) : null}
          <span className="text-xs fs-ink-tertiary">{timestamp}</span>
        </div>
        <div className="mt-2">
          <button className="fs-button secondary sm" onClick={() => onSelect(entryId)} type="button">
            {t("react.projects.view_diff", "View diff")} </button>
        </div>
        {author ? <p className="mt-1 text-xs fs-ink-tertiary">{t("react.projects.author", "author:")} {author}</p> : null}
        <div className="mt-2 grid gap-1 sm:grid-cols-2 lg:grid-cols-3">
          <div className="fs-surface-radius border border-subtle fs-surface-inset px-2 py-1 text-xs">
            <span className="fs-ink-tertiary">{t("react.projects.source", "source:")} </span>
            <span>{source}</span>
          </div>
        </div>
      </div>
    );
  });
}

function RevisionDiff({
  changes,
  error,
  isLoading,
}: Readonly<{ changes: Entity[]; error: unknown; isLoading: boolean }>) {
  const { t } = useI18n();
  if (isLoading) {
    return (
      <div className="inline-flex items-center gap-2 text-sm fs-ink-secondary">
        <Loader2 className="h-4 w-4 animate-spin" />
        {t("react.projects.loading_diff", "Loading diff…")} </div>
    );
  }
  if (error) {
    return (
      <div className="fs-surface-radius border border-selected fs-surface-attention px-3 py-2 text-sm fs-ink-attention">
        {error instanceof Error ? error.message : t("react.projects.failed_to_load_diff", "Failed to load diff.")}
      </div>
    );
  }
  if (!changes.length) {
    return (
      <div className="fs-surface-radius border border-subtle fs-surface-inset px-3 py-2 text-sm fs-ink-secondary">
        {t("react.projects.no_field_level_changes_in_the_selected_revision", "No field-level changes in the selected revision.")} </div>
    );
  }
  return (
    <div className="max-h-[70vh] overflow-auto fs-surface-radius border border-subtle">
      <table className="min-w-full divide-y divide-subtle text-xs">
        <thead className="sticky top-0 fs-surface-inset">
          <tr>
            <th className="px-3 py-2 text-left fs-ink-secondary">{t("project_history.field", "Field")}</th>
            <th className="px-3 py-2 text-left fs-ink-secondary">{t("project_history.change", "Change")}</th>
            <th className="px-3 py-2 text-left fs-ink-secondary">{t("react.projects.from", "From")}</th>
            <th className="px-3 py-2 text-left fs-ink-secondary">{t("react.projects.to", "To")}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-subtle">
          {changes.map((row, index) => (
            <tr key={`${firstStringFromUnknownValue([row.path, "row"])}:${index}`}>
              <td className="px-3 py-2 font-mono fs-ink-secondary">{firstStringFromUnknownValue([row.path, "field"])}</td>
              <td className="px-3 py-2 fs-ink-secondary">{firstStringFromUnknownValue([row.change, "changed"])}</td>
              <td className="px-3 py-2 fs-ink-secondary">{normalizeValue(row.from)}</td>
              <td className="px-3 py-2 fs-ink-secondary">{normalizeValue(row.to)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The History tab of the project page: settings revisions, their diffs and
 * restore. The page owns the header and the tab strip; this is the panel body.
 */
export function ProjectHistoryPanel({ projectId }: Readonly<{ projectId: string }>) {
  const { t } = useI18n();
  const auth = useAuth();
  const queryClient = useQueryClient();
  const userTimezone = useUserTimezone();
  const [searchParams, setSearchParams] = useSearchParams();
  const [restoreError, setRestoreError] = useState("");
  const [restoreBusy, setRestoreBusy] = useState(false);
  const selectedRevisionId = firstStringFromUnknownValue([searchParams.get("selected_revision_id"), ""]).trim();
  const flashMessage = firstStringFromUnknownValue([searchParams.get("flash"), ""]).trim();
  const roles = new Set((auth.session?.user?.roles || auth.session?.roles || [])
    .map((role) => firstStringFromUnknownValue([role, ""]).trim().toLowerCase())
    .filter(isTruthy));
  const canRestoreRevision = roles.has("admin") || roles.has("operator");
  const historyQuery = useQuery({
    queryKey: ["projects", "history", projectId],
    queryFn: () => fetchProjectHistory(projectId),
    enabled: Boolean(projectId),
  });
  const diffQuery = useQuery({
    queryKey: ["projects", "history", projectId, "diff", selectedRevisionId],
    queryFn: () => fetchProjectHistoryDiff(projectId, selectedRevisionId),
    enabled: Boolean(projectId && selectedRevisionId),
  });

  const historyRows = historyQuery.data?.history || historyQuery.data?.revisions || [];
  const rows = Array.isArray(historyRows) ? historyRows : [];
  const selectedRevision =
    rows.find((entry) => firstStringFromUnknownValue([objectOrEmpty(entry).id, ""]) === selectedRevisionId) ||
    null;
  const rawChanges = Array.isArray(diffQuery.data?.changes) ? diffQuery.data?.changes : [];
  const changes = rawChanges.map((entry) => objectOrEmpty(entry));
  const successLikeFlash =
    flashMessage || (searchParams.get("flash_type") === "info" ? "Project settings restored from revision" : "");

  function selectRevision(revisionId: string) {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set("selected_revision_id", revisionId);
    setSearchParams(nextParams, { replace: true });
  }

  function closeRevisionModal() {
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("selected_revision_id");
    setSearchParams(nextParams, { replace: true });
  }

  async function restoreSelectedRevision() {
    if (!selectedRevisionId || !globalThis.confirm(t("react.projects.confirm_restore_revision", "Restore revision #{id}?", { id: selectedRevisionId }))) {
      return;
    }
    setRestoreBusy(true);
    setRestoreError("");
    try {
      const payload = await restoreProjectHistoryRevision(projectId, selectedRevisionId);
      const newRevisionId = firstStringFromUnknownValue([payload.new_revision_id, ""]).trim();
      await historyQuery.refetch();
      await diffQuery.refetch();
      // The settings tabs of the same page show the restored values.
      await queryClient.invalidateQueries({ queryKey: queryKeys.projectDetail(projectId) });
      await queryClient.invalidateQueries({ queryKey: ["projects", projectId, "workflows"] });
      const nextParams = new URLSearchParams(searchParams);
      if (newRevisionId) {
        nextParams.set("selected_revision_id", newRevisionId);
      }
      nextParams.set("flash", `Project settings restored from revision #${selectedRevisionId}.`);
      nextParams.set("flash_type", "info");
      setSearchParams(nextParams, { replace: true });
    } catch (error) {
      setRestoreError(error instanceof Error ? error.message : t("react.projects.project_settings_restore_failed", "Project settings restore failed."));
    } finally {
      setRestoreBusy(false);
    }
  }

  return (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{t("react.projects.history", "History")}</h2>
          <p>{t("react.projects.review_settings_revisions_and_open_diffs_in_one_consistent_table", "Review settings revisions and open diffs in one consistent table.")}</p>
        </div>
      </div>

      {successLikeFlash ? (
        <Card className="fs-card fs-card-success">
          <CardContent className="text-sm fs-ink-success">
            {decodeFlashValue(successLikeFlash)}
          </CardContent>
        </Card>
      ) : null}

      {restoreError ? (
        <Card className="fs-card fs-card-danger">
          <CardContent className="text-sm fs-ink-danger">{restoreError}</CardContent>
        </Card>
      ) : null}

      <Card className="fs-card">
        <CardContent className="space-y-3">
          <HistoryEntries
            error={historyQuery.isError ? historyQuery.error : null}
            isLoading={historyQuery.isLoading}
            onSelect={selectRevision}
            rows={rows.map((entry) => objectOrEmpty(entry))}
            userTimezone={userTimezone}
          />
        </CardContent>
      </Card>

      <DsDialog
        busy={restoreBusy}
        closeLabel={t("react.projects.close_revision_diff", "Close revision diff")}
        description={selectedRevision ? t("react.projects.diff_against_current_project_settings", "Diff against current project settings.") : t("react.projects.revision_details_unavailable", "Revision details unavailable.")}
        eyebrow={t("react.projects.revision_diff", "Revision diff")}
        footer={
          <>
            {canRestoreRevision ? (
              <button
                className="fs-button danger"
                disabled={restoreBusy}
                onClick={() => {
                  ignoreAsyncResult(restoreSelectedRevision());
                }}
                type="button"
              >
                {t("react.projects.restore_this_revision", "Restore this revision")} </button>
            ) : (
              <span className="fs-field-helper">{t("react.projects.viewer_mode_is_read_only_on_project_history", "Viewer mode is read-only on project history.")}</span>
            )}
            <button className="fs-button" onClick={closeRevisionModal} type="button">{t("registry.common.close", "Close")}</button>
          </>
        }
        onClose={closeRevisionModal}
        open={Boolean(selectedRevisionId)}
        title={t("react.projects.revision_value1", "Revision #{value1}", { value1: String(selectedRevisionId) })}
      >
        <RevisionDiff
          changes={changes}
          error={diffQuery.isError ? diffQuery.error : null}
          isLoading={diffQuery.isLoading}
        />
      </DsDialog>
    </div>
  );
}
