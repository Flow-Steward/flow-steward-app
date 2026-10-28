import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useState, type ReactNode } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { Link } from "react-router";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { PageContainer } from "@/components/shared/design-system";
import { useConfirmDialog } from "@/components/shared/use-confirm-dialog";
import { useAuth } from "@/lib/auth/useAuth";
import {
  fetchChangelogDiff,
  fetchChangelogHistory,
  fetchChangelogList,
  fetchChangelogSearch,
  fetchRegistryStats,
  fetchRegistryValidation,
  restoreChangelogEntry,
  type ChangelogEntry,
} from "@/pages/registry/changelog/api";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { HttpError } from "@/lib/http/errors";
import {
  changelogActionLabel,
  changelogComparisonLabel,
  changelogRevisionLabel,
  changelogRevisionRestoreCopy,
  changelogRevisionPresentation,
  workflowChangePathLabel,
} from "@/pages/registry/shared/changelog-revision";
import {
  useWorkflowRestoreContext,
  type WorkflowRestoreContext,
} from "@/pages/registry/changelog/use-workflow-restore-context";

const ENTITY_TYPE_OPTIONS = [
  "",
  "agent",
  "tool",
  "skill",
  "workflow",
  "guardrail",
  "data_contract",
  "field",
  "published_block",
] as const;

function choose<T>(condition: boolean, yes: T, no: T): T {
  return condition ? yes : no;
}

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

function renderChoice(condition: boolean, yes: ReactNode, no: ReactNode): ReactNode {
  return condition ? yes : no;
}

function anyTrue(values: boolean[]): boolean {
  return values.some(Boolean);
}

function allTrue(values: boolean[]): boolean {
  return values.every(Boolean);
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function arrayOrEmpty<T>(value: T[] | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

function selectedChangelogEntry(
  rows: ChangelogEntry[] | undefined,
  selectedEntryId: string,
): ChangelogEntry | null {
  const entries = arrayOrEmpty(rows);
  return entries.find((row) => entryRef(row) === selectedEntryId)
    ?? entries.find((row) => Boolean(entryRef(row)))
    ?? null;
}

function entryRef(row: ChangelogEntry) {
  return firstStringFromUnknownValue([row.id, ""]);
}

function objectOrEmpty(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function listFromUnknown(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((row): row is Record<string, unknown> => Boolean(row) && typeof row === "object")
    : [];
}

function normalizeText(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
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

function formatUserDateTime(value: unknown, timezone?: string): string {
  const raw = firstStringFromUnknownValue([value, ""]).trim();
  if (!raw) {
    return "—";
  }
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) {
    return raw;
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  }).format(date);
}

function summarizeStats(stats: Record<string, unknown> | undefined, t: I18nContextValue["t"] = fallbackContext.t) {
  if (!stats || typeof stats !== "object") {
    return t("react.registry.display_stats_unavailable", "Stats unavailable");
  }
  const parts = Object.entries(stats)
    .filter(([, value]) => typeof value === "number")
    .slice(0, 6)
    .map(([key, value]) => `${key}: ${value}`);
  return parts.length ? parts.join(" • ") : t("react.registry.display_stats_loaded", "Stats loaded");
}

function registryEntityRoute(entityType: string, entityId: string): string {
  const normalizedType = firstStringFromUnknownValue([entityType, ""]).trim().toLowerCase();
  const normalizedId = encodeURIComponent(firstStringFromUnknownValue([entityId, ""]).trim());
  if (!normalizedType || !normalizedId) {
    return "";
  }
  if (normalizedType === "agent") return `/registry/agents/${normalizedId}`;
  if (normalizedType === "tool") return `/registry/tools/${normalizedId}`;
  if (normalizedType === "skill") return `/registry/skills/${normalizedId}`;
  if (normalizedType === "workflow") return `/registry/workflows/${normalizedId}`;
  if (normalizedType === "guardrail") return `/registry/guardrails/${normalizedId}`;
  if (normalizedType === "data_contract") return `/registry/data-contracts/${normalizedId}`;
  if (normalizedType === "field") return `/registry/fields/${normalizedId}`;
  if (normalizedType === "published_block") return `/registry/published-blocks/${normalizedId}`;
  return "";
}

type RegistryValidationIssue = {
  severity: "error" | "warning";
  entityType: string;
  entityId: string;
  rule: string;
  message: string;
};

function validationRule(message: string): string {
  const normalized = message.toLowerCase();
  if (normalized.includes("not found in registry")) return "missing_reference";
  if (normalized.includes("is unused") || normalized.includes("used by no")) return "unused_entity";
  if (normalized.includes("is disabled")) return "disabled_reference";
  if (normalized.includes("missing input_schema")) return "missing_input_schema";
  if (normalized.includes("missing output_schema")) return "missing_output_schema";
  if (normalized.includes("drifts from")) return "schema_drift";
  if (normalized.includes("deprecated prompts.output_format")) return "deprecated_output_format";
  if (normalized.includes("duplicates structured i/o")) return "duplicate_contract";
  return "registry_integrity";
}

function validationIssueFromMessage(
  severity: RegistryValidationIssue["severity"],
  value: unknown,
): RegistryValidationIssue {
  const message = stringFromUnknownValue(value);
  const match = /\b(Agent|Skill|Workflow|Tool|Guardrail|Data contract|Field|Published block) '([^']+)'/u.exec(message);
  return {
    severity,
    entityType: (match?.[1] || "registry").toLowerCase().replaceAll(" ", "_"),
    entityId: match?.[2] || "registry",
    rule: validationRule(message),
    message,
  };
}

export function RegistryChangelogPage() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const auth = useAuth();
  const userTimezone = firstStringFromUnknownValue([auth.session?.user?.timezone, ""]).trim();
  const [entityType, setEntityType] = useState("");
  const [entityId, setEntityId] = useState("");
  const [limit, setLimit] = useState(50);
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedEntryId, setSelectedEntryId] = useState("");
  const [revisionModalOpen, setRevisionModalOpen] = useState(false);
  const [validationText, setValidationText] = useState("");
  const [validationIssues, setValidationIssues] = useState<RegistryValidationIssue[]>([]);
  const [validationSeverity, setValidationSeverity] = useState<"" | "error" | "warning">("");
  const [restoreMessage, setRestoreMessage] = useState("");
  const [restoring, setRestoring] = useState(false);
  const { confirmDialog, requestConfirmation } = useConfirmDialog();

  const listQuery = useQuery({
    queryKey: ["registry", "changelog", "list", { entityType, entityId, limit }] as const,
    queryFn: () =>
      fetchChangelogList({
        entityType,
        entityId,
        limit,
      }),
    refetchInterval: 30_000,
  });

  const statsQuery = useQuery({
    queryKey: ["registry", "changelog", "stats"] as const,
    queryFn: fetchRegistryStats,
    staleTime: 60_000,
  });

  const normalizedSearchQuery = searchQuery.trim();
  const searchQueryResult = useQuery({
    queryKey: ["registry", "changelog", "search", normalizedSearchQuery, entityType] as const,
    queryFn: () =>
      fetchChangelogSearch({
        query: normalizedSearchQuery,
        entityType: entityType || undefined,
      }),
    enabled: normalizedSearchQuery.length >= 2,
    staleTime: 20_000,
  });

  const selectedEntry = selectedChangelogEntry(listQuery.data, selectedEntryId);

  const selectedEntityType = firstStringFromUnknownValue([selectedEntry?.entity_type, ""]).trim();
  const selectedEntityId = firstStringFromUnknownValue([selectedEntry?.entity_id, ""]).trim();
  const selectedEntryRef = choose(Boolean(selectedEntry), selectedEntry ? entryRef(selectedEntry) : "", "");
  const selectedEntityRoute = registryEntityRoute(selectedEntityType, selectedEntityId);

  const historyQuery = useQuery({
    queryKey: ["registry", "changelog", "history", selectedEntityType, selectedEntityId] as const,
    queryFn: () => fetchChangelogHistory(selectedEntityType, selectedEntityId),
    enabled: Boolean(selectedEntityType && selectedEntityId),
  });

  const selectedIndex = arrayOrEmpty(historyQuery.data).findIndex(
    (row) => entryRef(row) === selectedEntryRef,
  );
  const previousEntry = choose(
    selectedIndex >= 0,
    historyQuery.data?.[selectedIndex + 1],
    undefined,
  );
  const previousEntryId = choose(Boolean(previousEntry), previousEntry ? entryRef(previousEntry) : "", "");
  const revisionPresentation = changelogRevisionPresentation(selectedEntry, previousEntry, t);
  const restoresWorkflow = selectedEntityType === "workflow";
  const restoreContext = useWorkflowRestoreContext({
    open: revisionModalOpen && revisionPresentation.canRestore,
    entityType: selectedEntityType,
    entityId: selectedEntityId,
    entryId: selectedEntryRef,
  });
  const workflowRestoreReady = restoreContext.context.status === "ready"
    && restoreContext.context.workflowId === selectedEntityId
    && restoreContext.context.entryId === selectedEntryRef;

  const diffQuery = useQuery({
    queryKey: [
      "registry",
      "changelog",
      "diff",
      selectedEntityType,
      selectedEntityId,
      previousEntryId,
      selectedEntryRef,
    ] as const,
    queryFn: () =>
      fetchChangelogDiff({
        entityType: selectedEntityType,
        entityId: selectedEntityId,
        fromEntryId: previousEntryId || undefined,
        toEntryId: selectedEntryRef || undefined,
    }),
    enabled: allTrue([
      Boolean(selectedEntityType),
      Boolean(selectedEntityId),
      Boolean(previousEntryId),
      Boolean(selectedEntryRef),
      revisionPresentation.canCompareContent,
    ]),
  });
  const historyRows = arrayOrEmpty(historyQuery.data);
  const diffPayload = revisionPresentation.canCompareContent ? objectOrEmpty(diffQuery.data) : {};
  const diffSummary = diffPayload.summary;
  const diffSummaryRow = objectOrEmpty(diffSummary);
  const diffChanges = listFromUnknown(diffPayload.changes);

  async function refreshAll() {
    await queryClient.invalidateQueries({ queryKey: ["registry", "changelog"] });
  }

  async function runValidation() {
    try {
      const result = await fetchRegistryValidation();
      const errors = arrayOrEmpty(result.errors as unknown[] | undefined).length;
      const warnings = arrayOrEmpty(result.warnings as unknown[] | undefined).length;
      const valid = Boolean(result.valid);
      setValidationText(valid
        ? t("react.registry.display_validation_passed_counts", "Validation passed • errors: {errors} • warnings: {warnings}", { errors, warnings })
        : t("react.registry.display_validation_failed_counts", "Validation failed • errors: {errors} • warnings: {warnings}", { errors, warnings }));
      setValidationIssues([
        ...arrayOrEmpty(result.errors as unknown[] | undefined)
          .map((item) => validationIssueFromMessage("error", item)),
        ...arrayOrEmpty(result.warnings as unknown[] | undefined)
          .map((item) => validationIssueFromMessage("warning", item)),
      ]);
    } catch (error) {
      setValidationText(errorMessage(error, t("react.registry.validation_failed", "Validation failed.")));
      setValidationIssues([]);
    }
  }

  async function restoreSelectedEntry( ) {
    if (anyTrue([!selectedEntityType, !selectedEntityId, !selectedEntryRef, !revisionPresentation.canRestore])) {
      return;
    }
    // What the reader confirms is what is written: this entry, against the
    // revision read when the view opened, never a newer one read later.
    const target = { entityType: selectedEntityType, entityId: selectedEntityId, entryId: selectedEntryRef };
    let captured: WorkflowRestoreContext | null = null;
    if (restoresWorkflow) {
      if (!workflowRestoreReady) {
        return;
      }
      captured = restoreContext.context;
    }
    const restoreCopy = changelogRevisionRestoreCopy(target.entityType, target.entityId, selectedEntry, target.entryId, t);
    const confirmed = await requestConfirmation({
      body: restoreCopy.body,
      confirmLabel: t("trash.row_actions.restore", "Restore"),
      title: restoreCopy.title,
    });
    if (!confirmed) {
      return;
    }
    if (captured && !restoreContext.isCurrent(captured)) {
      // The view closed or moved to another workflow or entry while confirming.
      return;
    }
    setRestoring(true);
    setRestoreMessage("");
    try {
      await restoreChangelogEntry(
        captured ? { ...target, expectedRevision: captured.expectedRevision ?? undefined } : target,
      );
      setRestoreMessage(restoreCopy.success);
      await queryClient.invalidateQueries({ queryKey: ["registry", "changelog"] });
    } catch (error) {
      if (captured && error instanceof HttpError && error.status === 409) {
        restoreContext.markConflict(captured);
        setRestoreMessage(t(
          "react.registry.workflow_restore_conflict",
          "The workflow changed after you opened this revision, so nothing was restored. Close and reopen the revision to restore it against the current workflow.",
        ));
      } else {
        setRestoreMessage(errorMessage(error, t("react.registry.restore_failed", "Restore failed.")));
      }
    } finally {
      setRestoring(false);
    }
  }

  function workflowRestoreBaseline(): ReactNode {
    const { status, expectedRevision } = restoreContext.context;
    if (!restoresWorkflow || !revisionPresentation.canRestore || status === "idle") {
      return null;
    }
    if (status === "loading") {
      return (
        <span className="inline-flex items-center gap-2 text-xs text-slate-300">
          <Loader2 className="h-3 w-3 animate-spin" />
          {t("react.registry.workflow_restore_loading_revision", "Loading the workflow's current revision…")}
        </span>
      );
    }
    if (status === "ready") {
      return (
        <span className="text-xs text-slate-300">
          {t("react.registry.workflow_restore_current_revision", "Current workflow revision: {revision}. Restore is refused if the workflow changes before it is written.", { revision: expectedRevision ?? "" })}
        </span>
      );
    }
    if (status === "error") {
      return (
        <span className="text-xs text-rose-200" role="alert">
          {t("react.registry.workflow_restore_revision_unavailable", "The workflow's current revision could not be loaded, so restore is unavailable. Close and reopen the revision to try again.")}
        </span>
      );
    }
    return null;
  }

  return (
    <PageContainer width="wide">
      <section className="fs-panel-stack">
      <Card className="border-white/10 bg-slate-900/40">
        <CardHeader>
          <p className="text-xs font-medium uppercase tracking-[0.3em] text-cyan-200">{t("trash.category.registry", "Registry")}</p>
          <CardTitle>{t("react.registry.changelog", "Changelog")}</CardTitle>
          <CardDescription>
            {t("react.registry.audit_timeline_with_entity_history_revision_diffs_and_restore_actions", "Audit timeline with entity history, revision diffs, and restore actions.")}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-2 lg:grid-cols-5">
          <select
            className="rounded-2xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white outline-none focus:border-cyan-300/40"
            onChange={(event) => setEntityType(event.target.value)}
            value={entityType}
          >
            {ENTITY_TYPE_OPTIONS.map((option) => (
              <option key={option || "all"} value={option}>
                {option || t("react.registry.all_entity_types", "All entity types")}
              </option>
            ))}
          </select>

          <input
            className="rounded-2xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white outline-none focus:border-cyan-300/40"
            onChange={(event) => setEntityId(event.target.value)}
            placeholder={t("react.registry.entity_id", "Entity id")}
            type="text"
            value={entityId}
          />

          <select
            className="rounded-2xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white outline-none focus:border-cyan-300/40"
            onChange={(event) => setLimit(Number(event.target.value || 50))}
            value={limit}
          >
            {[20, 50, 100, 200].map((value) => (
              <option key={value} value={value}>
                {t("react.registry.limit", "Limit")}{" "}{value}
              </option>
            ))}
          </select>

          <button
            className="rounded-2xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-200 transition hover:border-cyan-300/40 disabled:opacity-50"
            disabled={listQuery.isFetching}
            onClick={() => {
              ignoreAsyncResult(refreshAll());
            }}
            type="button"
          >
            {choose(listQuery.isFetching, "Refreshing...", t("trash.refresh", "Refresh"))}
          </button>

          <button
            className="rounded-2xl border border-emerald-300/30 bg-emerald-400/10 px-3 py-2 text-sm text-emerald-100 transition hover:border-emerald-300/50"
            onClick={() => {
              ignoreAsyncResult(runValidation());
            }}
            type="button"
          >
            {t("react.registry.validate_registry", "Validate registry")}</button>
        </CardContent>
      </Card>

      <Card className="border-white/10 bg-slate-900/40">
        <CardHeader>
          <CardTitle className="text-lg">{t("registry.index.link.registry_stats", "Registry stats")}</CardTitle>
          <CardDescription>{summarizeStats((statsQuery.data || {}) as Record<string, unknown>, t)}</CardDescription>
        </CardHeader>
      </Card>

      <Card className="border-white/10 bg-slate-900/40">
        <CardHeader>
          <CardTitle className="text-lg">{t("react.registry.cross_entity_search", "Cross-entity search")}</CardTitle>
          <CardDescription>
            {t("react.registry.search_ids_display_names_and_descriptions_across_registry_entities", "Search IDs, display names, and descriptions across registry entities.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <input
            className="w-full rounded-2xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white outline-none focus:border-cyan-300/40"
            onChange={(event) => setSearchQuery(event.target.value)}
            placeholder={t("react.registry.search_registry_entities", "Search registry entities…")}
            type="search"
            value={searchQuery}
          />

          {renderWhen(
            allTrue([normalizedSearchQuery.length > 0, normalizedSearchQuery.length < 2]),
            (
            <div className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-xs text-slate-300">
              {t("react.registry.type_at_least_2_characters_to_search", "Type at least 2 characters to search.")}</div>
            ),
          )}

          {renderWhen(searchQueryResult.isLoading, (
            <div className="inline-flex items-center gap-2 text-sm text-slate-300">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("shell.search.loading", "Searching…")}</div>
          ))}

          {renderWhen(searchQueryResult.isError, (
            <div className="rounded-xl border border-rose-300/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-100">
              {errorMessage(searchQueryResult.error, t("react.registry.search_failed", "Search failed."))}
            </div>
          ))}

          {renderWhen(allTrue([
            normalizedSearchQuery.length >= 2,
            !searchQueryResult.isLoading,
            !searchQueryResult.isError,
            !arrayOrEmpty(searchQueryResult.data).length,
          ]), (
            <div className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-300">
              {t("react.registry.no_entities_matched_this_search", "No entities matched this search.")}</div>
          ))}

          <div className="grid gap-2">
            {arrayOrEmpty(searchQueryResult.data).map((row, index) => {
              const rowType = firstStringFromUnknownValue([row.entity_type, "unknown"]);
              const rowId = firstStringFromUnknownValue([row.entity_id, "—"]);
              const detailRoute = registryEntityRoute(rowType, rowId);
              return (
                <div
                  className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-200"
                  key={`${rowType}:${rowId}:${index}`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium">
                      {rowType} / {rowId}
                    </span>
                    <span className="text-xs text-slate-400">
                      {choose(row.enabled === false, t("settings.notifications.status.disabled", "disabled"), t("settings.extensions.enabled", "enabled"))}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-300">
                    {firstStringFromUnknownValue([row.display_name, row.description, t("react.registry.display_no_description", "No description")])}
                  </p>
                  {renderWhen(Boolean(detailRoute), (
                    <Link
                      className="mt-2 inline-flex text-xs text-cyan-200 hover:text-cyan-100"
                      to={detailRoute}
                    >
                      {t("react.registry.open_detail", "Open detail →")}</Link>
                  ))}
                </div>
              );
            })}
          </div>
        </CardContent>
      </Card>

      <Card className="border-white/10 bg-slate-900/40">
        <CardHeader>
          <CardTitle className="text-lg">{t("react.registry.change_entries", "Change entries")}</CardTitle>
          <CardDescription>{t("react.registry.select_an_entry_to_inspect_history_and_diff_with_the_previous_revision", "Select an entry to inspect history and diff with the previous revision.")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {renderWhen(listQuery.isLoading, (
            <div className="inline-flex items-center gap-2 text-sm text-slate-300">
              <Loader2 className="h-4 w-4 animate-spin" />
              {t("react.registry.loading_changelog", "Loading changelog…")}</div>
          ))}

          {renderWhen(listQuery.isError, (
            <div className="rounded-2xl border border-rose-300/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-100">
              {errorMessage(listQuery.error, t("react.registry.failed_to_load_changelog_entries", "Failed to load changelog entries."))}
            </div>
          ))}

          {renderWhen(allTrue([
            !listQuery.isError,
            !listQuery.isLoading,
            !arrayOrEmpty(listQuery.data).length,
          ]), (
            <div className="rounded-2xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-300">
              {t("react.registry.no_changelog_entries_found_with_current_filters", "No changelog entries found with current filters.")}</div>
          ))}

          <div className="grid gap-2">
            {arrayOrEmpty(listQuery.data).map((row) => {
              const id = entryRef(row);
              const active = Boolean(id) && id === selectedEntryRef;
              return (
                <button
                  key={id || JSON.stringify(row)}
                  className={[
                    "w-full rounded-2xl border px-3 py-2 text-left text-sm transition-colors",
                    choose(
                      active,
                      "border-cyan-300/55 bg-slate-900/40 text-slate-100 ring-1 ring-inset ring-cyan-300/25",
                      "border-white/10 bg-slate-900/40 text-slate-200 hover:border-white/30",
                    ),
                  ].map((value) => stringFromUnknownValue(value)).join(" ")}
                  onClick={() => {
                    setSelectedEntryId(id);
                    setValidationText("");
                    setValidationIssues([]);
                    setRestoreMessage("");
                    setRevisionModalOpen(true);
                  }}
                  type="button"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium">
                      {firstStringFromUnknownValue([row.entity_type, t("project_detail.connections.status.unknown", "unknown")])} / {firstStringFromUnknownValue([row.entity_id, "—"])}
                    </span>
                    <span className="text-xs text-slate-400">{changelogActionLabel(row.action || row.event || row.kind, t) || "change"}</span>
                  </div>
                  <div className="mt-1 text-xs text-slate-400"><span>{changelogRevisionLabel(firstStringFromUnknownValue([row.entity_type, ""]), row, t)}</span> • {t("react.registry.entry", "Entry #")}{id || "—"} • {firstStringFromUnknownValue([row.author || row.actor, "system"])} •{" "}
                    {formatUserDateTime(row.created_at || row.timestamp || row.updated_at, userTimezone)}
                  </div>
                </button>
              );
            })}
          </div>

          {renderWhen(Boolean(validationText), (
            <div className="rounded-2xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-200">
              {validationText}
            </div>
          ))}
          {renderWhen(validationIssues.length > 0, (
            <div className="space-y-3" aria-label={t("react.registry.registry_validation_issues", "Registry validation issues")}>
              <label className="flex items-center gap-2 text-sm text-slate-300">
                <span>{t("security_overview.columns.severity", "Severity")}</span>
                <select
                  aria-label={t("react.registry.validation_issue_severity", "Validation issue severity")}
                  className="rounded-xl border border-white/10 bg-slate-950/70 px-3 py-2 text-sm text-white"
                  onChange={(event) =>
                    setValidationSeverity(event.target.value as "" | "error" | "warning")
                  }
                  value={validationSeverity}
                >
                  <option value="">{t("users.filter.all_states", "All")}</option>
                  <option value="error">{t("registry.common.errors", "Errors")}</option>
                  <option value="warning">{t("registry.common.warnings", "Warnings")}</option>
                </select>
              </label>
              <div className="grid gap-2">
                {validationIssues
                  .filter((issue) => !validationSeverity || issue.severity === validationSeverity)
                  .map((issue, index) => {
                    const route = registryEntityRoute(issue.entityType, issue.entityId);
                    const entityLabel = `${issue.entityType.replaceAll("_", " ").replace(/^./u, (value) => value.toUpperCase())} ${issue.entityId}`;
                    return (
                      <article
                        className="rounded-2xl border border-white/10 bg-slate-950/50 px-3 py-3 text-sm text-slate-200"
                        key={`${issue.severity}-${issue.entityType}-${issue.entityId}-${issue.rule}-${index}`}
                      >
                        <div className="flex flex-wrap items-center gap-2 text-xs">
                          <span className={choose(issue.severity === "error", "text-rose-200", "text-amber-200")}>
                            {issue.severity}
                          </span>
                          <code>{issue.rule}</code>
                          {renderChoice(
                            Boolean(route),
                            <Link to={route}>{entityLabel}</Link>,
                            <span>{entityLabel}</span>,
                          )}
                        </div>
                        <p className="mt-2">{issue.message}</p>
                      </article>
                    );
                  })}
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      {renderWhen(revisionModalOpen, (
        <dialog
          aria-modal="true"
          className="fixed inset-0 z-50 m-0 flex h-full w-full max-h-none max-w-none items-start justify-center overflow-y-auto border-0 bg-slate-950/80 px-4 py-8 backdrop-blur-sm"
          open
        >
          <div className="w-full max-w-5xl rounded-3xl border border-white/10 bg-slate-950 p-5 shadow-2xl shadow-slate-950/50">
            <div className="mb-4 flex items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-medium text-white">{changelogRevisionLabel(selectedEntityType, selectedEntry, t)} • {t("react.registry.entry", "Entry #")}{selectedEntryRef || "—"}</h2>
                <p className="mt-1 text-sm text-slate-400">
                  {choose(
                    allTrue([Boolean(selectedEntityType), Boolean(selectedEntityId)]),
                    `${selectedEntityType}/${selectedEntityId}`,
                    t("react.registry.display_select_changelog_row", "Select a changelog row to inspect the revision."),
                  )}
                </p>
              </div>
              <button
                aria-label={t("react.registry.close_revision_details", "Close revision details")}
                className="rounded-xl border border-white/10 px-3 py-1.5 text-xs text-slate-200 hover:border-cyan-300/35"
                onClick={() => setRevisionModalOpen(false)}
                type="button"
              >
                {t("registry.common.close", "Close")}</button>
            </div>

            {renderChoice(historyQuery.isError, (
              <div
                className="rounded-2xl border border-rose-300/30 bg-rose-500/10 px-3 py-3 text-sm text-rose-100"
                role="alert"
              >
                {errorMessage(historyQuery.error, t("react.registry.failed_to_load_entity_history", "Failed to load entity history."))}
              </div>
            ), (
            <div className="grid gap-4 lg:grid-cols-[0.9fr_1.1fr]">
              <Card className="border-white/10 bg-slate-900/40">
                <CardHeader>
                  <CardTitle className="text-lg">{t("react.registry.entity_history", "Entity history")}</CardTitle>
                  <CardDescription>
                    {choose(
                      historyRows.length > 0,
                      t("react.registry.display_revisions_loaded", "{count} revisions loaded.", { count: historyRows.length }),
                      t("react.registry.display_history_will_appear", "History will appear here."),
                    )}
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  {renderWhen(allTrue([
                    historyQuery.isLoading,
                    Boolean(selectedEntityType),
                    Boolean(selectedEntityId),
                  ]), (
                    <div className="inline-flex items-center gap-2 text-sm text-slate-300">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      {t("react.registry.loading_history", "Loading history…")}</div>
                  ))}

                  {renderWhen(allTrue([
                    !historyQuery.isLoading,
                    !historyRows.length,
                    Boolean(selectedEntityType),
                    Boolean(selectedEntityId),
                  ]), (
                    <div className="rounded-2xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-300">
                      {t("react.registry.no_history_rows_found", "No history rows found.")}</div>
                  ))}

                  <div className="grid max-h-[56vh] gap-2 overflow-y-auto">
                    {historyRows.map((row, index) => {
                      const rowId = entryRef(row) || firstStringFromUnknownValue([row.id, index]);
                      const author = normalizeText(row.author || row.actor || row.username, t);
                      const action = normalizeText(changelogActionLabel(row.action || row.change_type || row.kind, t), t);
                      const createdAt = formatUserDateTime(row.created_at || row.timestamp || row.updated_at, userTimezone);
                      const source = normalizeText(row.source || row.origin || row.provider, t);
                      return (
                        <div
                          key={`${rowId || "history"}-${index}`}
                          className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-200"
                        >
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <span className="font-medium"><span>{changelogRevisionLabel(selectedEntityType, row, t)}</span> • {t("react.registry.entry_variant_965fdbac", "Entry #")}{rowId || "—"}</span>
                            <span className="text-xs text-slate-400">{createdAt}</span>
                          </div>
                          <div className="mt-1 text-xs text-slate-400">
                            {t("react.registry.action", "action:")}{" "}{action} {" "}{t("react.registry.author", "• author:")}{" "}{author}{" "}{t("react.registry.source", "• source:")}{" "}{source}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </CardContent>
              </Card>

              <Card className="border-white/10 bg-slate-900/40">
                <CardHeader>
                  <CardTitle className="text-lg">{revisionPresentation.title}</CardTitle>
                  <CardDescription>
                    {revisionPresentation.explanation || choose(
                      Boolean(previousEntryId),
                      changelogComparisonLabel(selectedEntityType, previousEntry, selectedEntry, previousEntryId, selectedEntryRef, t),
                      t("react.registry.no_previous_revision_available_for_the_selected_entry", "No previous revision available for the selected entry."),
                    )}
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  {renderWhen(revisionPresentation.canCompareContent && diffQuery.isLoading, (
                    <div className="inline-flex items-center gap-2 text-sm text-slate-300">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      {t("react.registry.loading_diff", "Loading diff…")}</div>
                  ))}

                  {renderWhen(revisionPresentation.canCompareContent && diffQuery.isError, (
                    <div className="rounded-2xl border border-rose-300/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-100">
                      {errorMessage(diffQuery.error, t("react.registry.failed_to_load_diff", "Failed to load diff."))}
                    </div>
                  ))}

                  {renderWhen(allTrue([
                    !diffQuery.isLoading,
                    revisionPresentation.canCompareContent,
                    Boolean(previousEntryId),
                    !diffChanges.length,
                  ]), (
                    <div className="rounded-2xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-300">
                      {t("react.registry.no_explicit_diff_rows_were_returned", "No explicit diff rows were returned.")}</div>
                  ))}

                  {renderWhen(Object.keys(diffSummaryRow).length > 0, (
                    <div className="flex flex-wrap gap-2">
                      {Object.entries(diffSummaryRow).map(([key, value]) => (
                        <span key={key} className="rounded-full border border-white/10 px-3 py-1 text-xs text-slate-200">
                          {key}: {normalizeText(value, t)}
                        </span>
                      ))}
                    </div>
                  ))}

                  <div className="grid max-h-[42vh] gap-2 overflow-y-auto">
                    {diffChanges.map((change, index) => (
                      <div
                        key={`${normalizeText(change.path || change.key, t)}-${index}`}
                        className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-200"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <span className="font-medium">
                            {normalizeText(workflowChangePathLabel(selectedEntityType, change.path || change.key || `change-${index + 1}`, t), t)}
                            {workflowChangePathLabel(selectedEntityType, change.path || change.key || "", t) !== normalizeText(change.path || change.key || "", t)
                              ? <code className="ml-2 text-xs opacity-70">{normalizeText(change.path || change.key, t)}</code>
                              : null}
                          </span>
                          <span className="text-xs text-slate-400">{normalizeText(change.change || change.type || "updated", t)}</span>
                        </div>
                        <div className="mt-1 text-xs text-slate-400">
                          {t("project_edit.diff.from", "from:")}{" "}{normalizeText(Object.prototype.hasOwnProperty.call(change, "from") ? change.from : change.before, t)} {" "}{t("react.registry.to", "• to:")}{" "}{normalizeText(Object.prototype.hasOwnProperty.call(change, "to") ? change.to : change.after, t)}
                        </div>
                      </div>
                    ))}
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    {renderWhen(revisionPresentation.canRestore, (<button
                      className="rounded-xl border border-cyan-300/35 bg-cyan-500/10 px-3 py-2 text-xs text-cyan-100 transition hover:border-cyan-300/60 disabled:opacity-50"
                      disabled={anyTrue([
                        !selectedEntityType,
                        !selectedEntityId,
                        !selectedEntryRef,
                        restoring,
                        restoresWorkflow && !workflowRestoreReady,
                      ])}
                      onClick={() => {
                        ignoreAsyncResult(restoreSelectedEntry( ));
                      }}
                      type="button"
                    >
                      {choose(restoring, t("react.registry.restoring", "Restoring..."), t("react.registry.display_restore_selected_revision", "Restore selected revision"))}
                    </button>))}
                    {workflowRestoreBaseline()}
                    {renderWhen(Boolean(selectedEntityRoute), (
                      <Link
                        className="text-xs text-cyan-200 hover:text-cyan-100"
                        to={selectedEntityRoute}
                      >
                        {t("react.registry.open_entity_detail", "Open entity detail →")}</Link>
                    ))}
                  </div>

                  {renderWhen(Boolean(restoreMessage), (
                    <div className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-200">
                      {restoreMessage}
                    </div>
                  ))}
                </CardContent>
              </Card>
            </div>
            ))}
          </div>
        </dialog>
      ))}

      <ConfirmDialog {...confirmDialog} />
      </section>
    </PageContainer>
  );
}
