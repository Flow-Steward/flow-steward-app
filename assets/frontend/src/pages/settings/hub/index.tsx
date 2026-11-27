import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  type ReactNode,
  type SyntheticEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import { Link, useLocation, useNavigate } from "react-router";

import type { RegisteredPageRecord, SettingsPageRecord } from "@/lib/auth/AuthProvider";
import { hasCapability } from "@/lib/auth/capabilities";
import { sessionPermissions } from "@/lib/auth/permissions";
import { pageOpensWithoutAdministration } from "@/lib/auth/settings-page-access";
import { useAuth } from "@/lib/auth/useAuth";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import { fetchJson } from "@/lib/http/client";
import {
  DsAlert,
  DsCard,
  DsDialog,
  DsPageHeader,
  DsTable,
  PageContainer,
  type DsTableColumn,
} from "@/components/shared/design-system";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { SettingsEmailPage } from "@/pages/settings/email";
import { SettingsGeneralPage } from "@/pages/settings/general";
import { SettingsNotificationsPage } from "@/pages/settings/notifications";
import { SettingsProvidersPage } from "@/pages/settings/providers";
import { SettingsRetentionPage } from "@/pages/settings/retention";
import { SettingsSecurityPage } from "@/pages/settings/security";
import { SettingsServiceAccountsPage } from "@/pages/settings/service-accounts";
import { SettingsUsersPage } from "@/pages/settings/users";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { trimRepeatedEnd } from "@/lib/utils/string-normalization";
import { replaceDelimitedTokens } from "@/lib/utils/template-tokens";
import {
  isFeatureNotEntitled,
  isSameOriginApiPath,
  normalizeResourceReportSurface,
  type ResourceReportSurface,
} from "@/pages/settings/hub/resource-report";
import { RegisteredPageUnavailable, ResourceReportPanel } from "@/pages/settings/hub/resource-report-panel";
import {
  choiceLabel,
  choiceOptionsFromPayload,
  type ChoiceOption,
} from "@/pages/settings/hub/choice-options";
import { normalizeStatusDocumentSurface, type StatusDocumentSurface } from "@/pages/settings/hub/status-document";
import { StatusDocumentPanel } from "@/pages/settings/hub/status-document-panel";
import {
  type ControlSurfaceField,
  type FormValues,
  type OptionsByPath,
  defaultFormValues,
  fieldOptions,
  normalizeField,
  payloadFromValues,
  valuesFromRecord,
} from "@/pages/settings/hub/resource-admin-fields";
import { ResourceForm } from "@/pages/settings/hub/resource-admin-form";
import { SettingsSecretStoragePage } from "@/pages/settings/secret-storage";

type SettingsTab = {
  controlSurface?: SettingsControlSurface;
  /** A read-only `resource_report` surface; drawn by ResourceReportPanel. */
  reportSurface?: ResourceReportSurface;
  /** A `status_document` surface; drawn by StatusDocumentPanel. */
  statusSurface?: StatusDocumentSurface;
  pageKey: string;
  path: string;
  title: string;
};

type ControlSurfaceResource = {
  /** False hides the delete action: the module serves no delete for this resource. */
  allowDelete: boolean;
  /** False hides the edit action: the module serves no update for this resource. */
  allowEdit: boolean;
  collectionKey: string;
  description: string;
  displayField: string;
  emptyLabel?: string;
  fields: ControlSurfaceField[];
  idField: string;
  itemPath: string;
  key: string;
  listPath: string;
  recordKey: string;
  /** Buttons next to Edit and Delete that POST to a path built from the row's id. */
  rowActions: RowAction[];
  /** False keeps the record id out of the table (a generated key, a UUID). */
  showId: boolean;
  summaryFields: SummaryField[];
  title: string;
};

/**
 * A declared row action (ADR 0082): `pathTemplate` lies in the page's API tree and
 * carries one `{name}` token, which is filled with the row's record id.
 */
type RowAction = {
  confirm: string;
  key: string;
  label: string;
  pathTemplate: string;
  successText: string;
};

/** A value the table summary shows, with an optional caption ("Linked: …"). */
type SummaryField = { key: string; label: string };

type SettingsControlSurface = {
  description: string;
  kind: string;
  resources: ControlSurfaceResource[];
};

type AdminRecord = Record<string, unknown>;

type ResourceCollectionResponse = Record<string, unknown>;

type SaveResponse = Record<string, unknown>;

function getFallbackSettingsTabs(t: I18nContextValue["t"] = fallbackContext.t): SettingsTab[] {
  return [
  { pageKey: "general", path: "/settings/general", title: t("registry.workflow_detail.settings.general", "General") },
  { pageKey: "security", path: "/settings/security", title: t("account.security.title", "Security") },
  { pageKey: "email", path: "/settings/email", title: t("settings.tab.email", "Email") },
  { pageKey: "service_accounts", path: "/settings/service-accounts", title: t("react.settings.service_accounts", "Service Accounts") },
  { pageKey: "retention", path: "/settings/retention", title: t("settings.retention.title", "Retention") },
  { pageKey: "notifications", path: "/settings/notifications", title: t("settings.tab.notifications", "Notifications") },
  { pageKey: "model_providers", path: "/settings/providers", title: t("settings.tab.model_providers", "Chat Models") },
];
}

const SETTINGS_PAGE_KEY_TO_PATH: Record<string, string> = {
  email: "/settings/email",
  general: "/settings/general",
  model_providers: "/settings/providers",
  notifications: "/settings/notifications",
  providers: "/settings/providers",
  retention: "/settings/retention",
  secret_storage: "/settings/secret-storage",
  security: "/settings/security",
  service_accounts: "/settings/service-accounts",
  users: "/settings/users",
};

function normalizePageKey(value: string): string {
  return firstStringFromUnknownValue([value, ""])
    .trim()
    .toLowerCase()
    .replaceAll("-", "_");
}

function summaryFieldList(value: unknown): SummaryField[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => {
      if (item && typeof item === "object" && !Array.isArray(item)) {
        const entry = item as Record<string, unknown>;
        const key = firstStringFromUnknownValue([entry.key, ""]).trim();
        return key ? { key, label: firstStringFromUnknownValue([entry.label, ""]).trim() } : null;
      }
      const key = firstStringFromUnknownValue([item, ""]).trim();
      return key ? { key, label: "" } : null;
    })
    .filter(isTruthy);
}

const PATH_TOKEN = /\{[A-Za-z_]\w*\}/g;

function normalizeRowAction(value: unknown): RowAction | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const key = firstStringFromUnknownValue([record.key, ""]).trim();
  const label = firstStringFromUnknownValue([record.label, ""]).trim();
  const method = firstStringFromUnknownValue([record.method, "POST"]).trim().toUpperCase() || "POST";
  const pathTemplate = firstStringFromUnknownValue([record.path_template, ""]).trim();
  const tokens = pathTemplate.match(PATH_TOKEN) ?? [];
  // The server checked these when the page was registered; a row action that
  // still fails them is left out rather than sent anywhere.
  if (
    !key
    || !label
    || method !== "POST"
    || !isSameOriginApiPath(pathTemplate)
    || tokens.length !== 1
    || !pathTemplate.split("/").includes(tokens[0])
  ) {
    return null;
  }
  return {
    confirm: firstStringFromUnknownValue([record.confirm, ""]).trim(),
    key,
    label,
    pathTemplate,
    successText: firstStringFromUnknownValue([record.success_text, ""]).trim(),
  };
}

function normalizeRowActions(value: unknown): RowAction[] {
  return Array.isArray(value) ? value.map(normalizeRowAction).filter(isTruthy) : [];
}

/** An ISO date-time a module returns; the summary shows it in the user's zone. */
const ISO_DATE_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function normalizeResource(value: unknown): ControlSurfaceResource | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const key = firstStringFromUnknownValue([record.key, ""]).trim();
  const title = firstStringFromUnknownValue([record.title, key]).trim();
  const listPath = firstStringFromUnknownValue([record.list_path, ""]).trim();
  const itemPath = firstStringFromUnknownValue([record.item_path, ""]).trim();
  const idField = firstStringFromUnknownValue([record.id_field, ""]).trim();
  const fields = Array.isArray(record.fields)
    ? record.fields.map(normalizeField).filter(isTruthy)
    : [];
  if (!key || !title || !isSameOriginApiPath(listPath) || !idField || fields.length === 0) {
    return null;
  }
  if (itemPath && !isSameOriginApiPath(itemPath)) {
    return null;
  }
  return {
    allowDelete: record.allow_delete !== false,
    allowEdit: record.allow_edit !== false,
    collectionKey: firstStringFromUnknownValue([record.collection_key, "items"]).trim() || "items",
    description: firstStringFromUnknownValue([record.description, ""]).trim(),
    displayField: firstStringFromUnknownValue([record.display_field, idField]).trim() || idField,
    emptyLabel: typeof record.empty_label === "string" ? record.empty_label : undefined,
    fields,
    idField,
    itemPath,
    key,
    listPath,
    recordKey: firstStringFromUnknownValue([record.record_key, "item"]).trim() || "item",
    rowActions: normalizeRowActions(record.row_actions),
    showId: record.show_id !== false,
    summaryFields: summaryFieldList(record.summary_fields),
    title,
  };
}

function normalizeControlSurface(value: unknown): SettingsControlSurface | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const record = value as Record<string, unknown>;
  const kind = firstStringFromUnknownValue([record.kind, ""]).trim();
  const resources = Array.isArray(record.resources)
    ? record.resources
        .map(normalizeResource)
        .filter(isTruthy)
    : [];
  if (!kind || resources.length === 0) {
    return undefined;
  }
  return {
    description: firstStringFromUnknownValue([record.description, ""]).trim(),
    kind,
    resources,
  };
}

/**
 * The hub's tabs from the bootstrap. Someone who does not administer the platform
 * sees only the module pages they may open, and never the built-in fallback tabs.
 */
function normalizeTabs(
  records: SettingsPageRecord[],
  t: I18nContextValue["t"] = fallbackContext.t,
  administers = true,
): SettingsTab[] {
  const tabs = records
    .filter((record) => {
      const pageKey = normalizePageKey(firstStringFromUnknownValue([record.page_key, ""]));
      if (!administers && (SETTINGS_PAGE_KEY_TO_PATH[pageKey] || !pageOpensWithoutAdministration(record))) {
        return false;
      }
      return record.settings_tab && pageKey !== "extensions";
    })
    .map((record) => ({
      controlSurface: normalizeControlSurface(record.control_surface),
      reportSurface: normalizeResourceReportSurface(record.control_surface),
      statusSurface: normalizeStatusDocumentSurface(record.control_surface),
      pageKey: normalizePageKey(firstStringFromUnknownValue([record.page_key, ""])),
      path: firstStringFromUnknownValue([record.path, ""]).trim() || "/settings",
      title: firstStringFromUnknownValue([record.title, record.page_key, ""]).trim(),
      sortOrder: Number(record.sort_order || 100),
    }))
    .filter((record) => Boolean(record.pageKey) && Boolean(record.title))
    .sort((left, right) => left.sortOrder - right.sortOrder || left.pageKey.localeCompare(right.pageKey))
    .map(({ controlSurface, reportSurface, statusSurface, pageKey, path, title }) => ({ controlSurface, reportSurface, statusSurface, pageKey, path, title }));
  return tabs.length || !administers ? tabs : getFallbackSettingsTabs(t);
}

function tabPath(tab: SettingsTab): string {
  const normalizedKey = normalizePageKey(tab.pageKey);
  const mappedPath = SETTINGS_PAGE_KEY_TO_PATH[normalizedKey];
  if (mappedPath) {
    return mappedPath;
  }
  if (tab.path.startsWith("/settings")) {
    return tab.path;
  }
  return "/settings";
}

function RegisteredTabFallback({ tab }: Readonly<{ tab: SettingsTab }>) {
  const { t } = useI18n();
  const openPath = tabPath(tab);
  return (
    <DsCard
      description={t("react.settings.this_settings_area_is_available_as_a_dedicated_react_screen", "This settings area is available as a dedicated React screen.")}
      footer={
        <Link className="fs-button" to={openPath}>
          {t("settings.common.open", "Open")} {tab.title}
        </Link>
      }
      title={tab.title}
    />
  );
}

function recordsFromPayload(payload: ResourceCollectionResponse, resource: ControlSurfaceResource): AdminRecord[] {
  const rows = payload[resource.collectionKey];
  if (!Array.isArray(rows)) {
    return [];
  }
  return rows
    .filter((row): row is AdminRecord => Boolean(row) && typeof row === "object" && !Array.isArray(row))
    .filter((row) => Boolean(firstStringFromUnknownValue([row[resource.idField], ""]).trim()));
}

function recordLabel(record: AdminRecord, resource: ControlSurfaceResource): string {
  return (
    firstStringFromUnknownValue([record[resource.displayField], ""]).trim()
    || firstStringFromUnknownValue([record[resource.idField], ""]).trim()
    || "record"
  );
}

function summaryValue(
  value: unknown,
  options: readonly ChoiceOption[],
  timezone: string,
  t: I18nContextValue["t"],
): string {
  if (Array.isArray(value)) {
    return value.map((item) => choiceLabel(options, stringFromUnknownValue(item))).join(", ");
  }
  if (typeof value === "boolean") {
    return value ? t("react.settings.summary_enabled", "enabled") : t("react.settings.summary_disabled", "disabled");
  }
  const text = stringFromUnknownValue(value ?? "").trim();
  if (ISO_DATE_TIME.test(text)) {
    return formatUserDateTime(text, timezone);
  }
  return choiceLabel(options, text);
}

function recordSummary(
  record: AdminRecord,
  resource: ControlSurfaceResource,
  optionsByPath: OptionsByPath = {},
  timezone = "",
  t: I18nContextValue["t"] = fallbackContext.t,
): string {
  return resource.summaryFields
    .map((field) => {
      const declared = resource.fields.find((item) => item.key === field.key);
      const options = declared ? fieldOptions(declared, optionsByPath) : [];
      const text = summaryValue(record[field.key], options, timezone, t);
      return text && field.label ? `${field.label}: ${text}` : text;
    })
    .filter(isTruthy)
    .join(" · ");
}

/** A required reference with nothing to choose: the record cannot be created yet. */
function missingReference(resource: ControlSurfaceResource, optionsByPath: OptionsByPath): ControlSurfaceField | null {
  return (
    resource.fields.find(
      (field) =>
        field.required &&
        Boolean(field.optionsPath) &&
        field.optionsPath in optionsByPath &&
        fieldOptions(field, optionsByPath).length === 0,
    ) ?? null
  );
}

const RECORD_ID_PATH_CHARACTERS = new Set(["/", "\\", "?", "#"]);

// A record id is substituted into a request path. "." and ".." survive
// encodeURIComponent and the browser would normalise them out of the resource
// tree; separators and control characters must never reach the path at all.
function isUsableRecordId(id: string): boolean {
  if (!id || id === "." || id === "..") return false;
  for (const character of id) {
    const code = character.codePointAt(0) ?? 0;
    if (code < 0x20 || (code >= 0x7f && code <= 0x9f) || RECORD_ID_PATH_CHARACTERS.has(character)) return false;
  }
  try {
    // A lone UTF-16 surrogate cannot be percent-encoded.
    encodeURIComponent(id);
  } catch {
    return false;
  }
  return true;
}

function resolveItemPath(resource: ControlSurfaceResource, id: string): string | null {
  if (!isUsableRecordId(id)) return null;
  const encodedId = encodeURIComponent(id);
  if (resource.itemPath) {
    return resource.itemPath.includes("{")
      ? replaceDelimitedTokens(resource.itemPath, "{", "}", () => encodedId)
      : `${trimRepeatedEnd(resource.itemPath, "/")}/${encodedId}`;
  }
  return `${trimRepeatedEnd(resource.listPath, "/")}/${encodedId}`;
}

function resolveRowActionPath(action: RowAction, id: string): string | null {
  if (!isUsableRecordId(id)) return null;
  const encodedId = encodeURIComponent(id);
  return replaceDelimitedTokens(action.pathTemplate, "{", "}", () => encodedId);
}

function resourceRecordColumns({
  onDelete, onEdit, onRowAction, optionsByPath, resource, saving, t, timezone,
}: Readonly<{
  onDelete: (record: AdminRecord) => void;
  onEdit: (record: AdminRecord) => void;
  onRowAction: (record: AdminRecord, action: RowAction) => void;
  optionsByPath: OptionsByPath;
  resource: ControlSurfaceResource;
  saving: boolean;
  t: I18nContextValue["t"];
  timezone: string;
}>): Array<DsTableColumn<AdminRecord>> {
  const summary = (record: AdminRecord) => recordSummary(record, resource, optionsByPath, timezone, t);
  return [
    {
      key: "record",
      header: t("react.settings.record", "Record"),
      cell: (record) => (
        <span className="fs-user-copy">
          <strong>{recordLabel(record, resource)}</strong>
          {resource.showId ? <span>{firstStringFromUnknownValue([record[resource.idField], ""]).trim()}</span> : null}
        </span>
      ),
    },
    {
      key: "summary",
      header: t("jobs.columns.summary", "Summary"),
      truncate: "28rem",
      tooltip: (record) => summary(record) || undefined,
      cell: (record) => summary(record) || null,
    },
    {
      key: "actions",
      header: "",
      ariaLabel: t("monitoring.table.actions", "Actions"),
      cell: (record) => (
        <span className="fs-footer-actions">
          {resource.rowActions.map((action) => (
            <button className="fs-button ghost" disabled={saving} key={action.key} onClick={() => onRowAction(record, action)} type="button">
              {action.label}
            </button>
          ))}
          {resource.allowEdit ? (
            <button className="fs-button ghost" disabled={saving} onClick={() => onEdit(record)} type="button">
              {t("projects.grid.action.edit", "Edit")} </button>
          ) : null}
          {resource.allowDelete ? (
            <button className="fs-button ghost-danger" disabled={saving} onClick={() => onDelete(record)} type="button">
              {t("projects.grid.action.delete", "Delete")} </button>
          ) : null}
        </span>
      ),
    },
  ];
}

function ResourceRecords({
  loading,
  onDelete,
  onEdit,
  onRowAction,
  optionsByPath,
  records,
  resource,
  saving,
}: Readonly<{
  loading: boolean;
  onDelete: (record: AdminRecord) => void;
  onEdit: (record: AdminRecord) => void;
  onRowAction: (record: AdminRecord, action: RowAction) => void;
  optionsByPath: OptionsByPath;
  records: AdminRecord[];
  resource: ControlSurfaceResource;
  saving: boolean;
}>) {
  const { t } = useI18n();
  const timezone = useUserTimezone();
  const columns = resourceRecordColumns({ onDelete, onEdit, onRowAction, optionsByPath, resource, saving, t, timezone });
  const loaderShown = useLoaderDelay(loading && !records.length);
  let tableState: ReactNode = null;
  if (loaderShown) {
    tableState = <div className="fs-table-state"><DsLoader variant="wide-block" /></div>;
  } else if (!loading && !records.length) {
    tableState = (
      <div className="fs-table-state">
        <strong>{resource.emptyLabel ?? t("react.settings.no_records_configured", "No records are configured.")}</strong>
      </div>
    );
  }
  return (
    <article className="fs-card fs-table-card">
      <DsTable
        ariaLabel={resource.title}
        busy={loading}
        columns={columns}
        rowKey={(record) => firstStringFromUnknownValue([record[resource.idField], ""]).trim()}
        rows={records}
        state={tableState}
      />
    </article>
  );
}

function ResourceAdminPanel({
  loading,
  onReload,
  optionsByPath = {},
  records,
  resource,
}: Readonly<{
  loading: boolean;
  onReload: () => Promise<void>;
  optionsByPath?: OptionsByPath;
  records: AdminRecord[];
  resource: ControlSurfaceResource;
}>) {
  const { t } = useI18n();
  const [editingId, setEditingId] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [formValues, setFormValues] = useState<FormValues>(() => defaultFormValues(resource));
  const [message, setMessage] = useState<{ text: string; tone: "success" | "danger" } | null>(null);
  const [saving, setSaving] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<AdminRecord | null>(null);
  const [pendingAction, setPendingAction] = useState<{ action: RowAction; record: AdminRecord } | null>(null);

  function beginCreate() {
    setEditingId("");
    setFormValues(defaultFormValues(resource));
    setMessage(null);
    setFormOpen(true);
  }

  function recordIdUnusable() {
    setMessage({ text: t("react.settings.record_id_is_missing", "Record ID is missing."), tone: "danger" });
  }

  function beginEdit(record: AdminRecord) {
    const id = firstStringFromUnknownValue([record[resource.idField], ""]).trim();
    if (!isUsableRecordId(id)) {
      recordIdUnusable();
      return;
    }
    setEditingId(id);
    setFormValues(valuesFromRecord(resource, record));
    setMessage(null);
    setFormOpen(true);
  }

  async function saveResource(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    const editing = Boolean(editingId);
    const target = editing ? resolveItemPath(resource, editingId) : resource.listPath;
    if (target === null) {
      recordIdUnusable();
      return;
    }
    setSaving(true);
    try {
      const payload = payloadFromValues(resource, formValues, { editing, optionsByPath }, t);
      await fetchJson<SaveResponse>(target, {
        method: editing ? "PUT" : "POST",
        body: JSON.stringify(payload),
      });
      setMessage({ text: editing ? t("react.settings.record_updated", "Record updated.") : t("react.settings.record_created", "Record created."), tone: "success" });
      setEditingId("");
      setFormValues(defaultFormValues(resource));
      setFormOpen(false);
      await onReload();
    } catch (error) {
      setMessage({ text: error instanceof Error ? error.message : t("react.settings.failed_to_save_record", "Failed to save record."), tone: "danger" });
    } finally {
      setSaving(false);
    }
  }

  function requestDelete(record: AdminRecord) {
    const id = firstStringFromUnknownValue([record[resource.idField], ""]).trim();
    if (!isUsableRecordId(id)) {
      recordIdUnusable();
      return;
    }
    setPendingDelete(record);
  }

  async function deleteResource() {
    const record = pendingDelete;
    if (!record) return;
    const id = firstStringFromUnknownValue([record[resource.idField], ""]).trim();
    const target = resolveItemPath(resource, id);
    if (target === null) {
      setPendingDelete(null);
      recordIdUnusable();
      return;
    }
    setMessage(null);
    setSaving(true);
    try {
      await fetchJson<SaveResponse>(target, { method: "DELETE" });
      setMessage({ text: t("react.settings.record_deleted", "Record deleted."), tone: "success" });
      await onReload();
    } catch (error) {
      setMessage({ text: error instanceof Error ? error.message : t("react.settings.failed_to_delete_record", "Failed to delete record."), tone: "danger" });
    } finally {
      setSaving(false);
      setPendingDelete(null);
    }
  }

  function requestRowAction(record: AdminRecord, action: RowAction) {
    const id = firstStringFromUnknownValue([record[resource.idField], ""]).trim();
    if (!isUsableRecordId(id)) {
      recordIdUnusable();
      return;
    }
    if (action.confirm) {
      setPendingAction({ action, record });
      return;
    }
    ignoreAsyncResult(runRowAction(record, action));
  }

  async function runRowAction(record: AdminRecord, action: RowAction) {
    const id = firstStringFromUnknownValue([record[resource.idField], ""]).trim();
    const target = resolveRowActionPath(action, id);
    if (target === null) {
      setPendingAction(null);
      recordIdUnusable();
      return;
    }
    setMessage(null);
    setSaving(true);
    try {
      await fetchJson<SaveResponse>(target, { method: "POST", body: JSON.stringify({}) });
      setMessage({ text: action.successText || t("react.settings.row_action_done", "Done."), tone: "success" });
      await onReload();
    } catch (error) {
      setMessage({ text: error instanceof Error ? error.message : t("react.settings.row_action_failed", "The action failed."), tone: "danger" });
    } finally {
      setSaving(false);
      setPendingAction(null);
    }
  }

  function cancelForm() {
    setFormOpen(false);
    setEditingId("");
    setFormValues(defaultFormValues(resource));
  }

  // A required reference with nothing to choose blocks creating a record: the
  // records it points to must exist first.
  const blockedBy = missingReference(resource, optionsByPath);
  return (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{resource.title}</h2>
          {resource.description ? <p>{resource.description}</p> : null}
        </div>
        <button className="fs-button primary" disabled={saving || Boolean(blockedBy)} onClick={beginCreate} type="button">
          {t("registry.agent_detail.common.add", "Add")} </button>
      </div>
      {blockedBy ? (
        <DsAlert
          title={
            blockedBy.emptyOptionsLabel ||
            t("react.settings.reference_options_missing", "Nothing to choose for {field} yet.", { field: blockedBy.label })
          }
          tone="neutral"
        />
      ) : null}
      {message ? <DsAlert title={message.text} tone={message.tone} /> : null}
      {formOpen && !(blockedBy && !editingId) ? (
        <ResourceForm
          editingId={editingId}
          formValues={formValues}
          onCancel={cancelForm}
          onSubmit={(event) => ignoreAsyncResult(saveResource(event))}
          optionsByPath={optionsByPath}
          resource={resource}
          saving={saving}
          setFormValues={setFormValues}
        />
      ) : null}
      <ResourceRecords
        loading={loading}
        onDelete={requestDelete}
        onEdit={beginEdit}
        onRowAction={requestRowAction}
        optionsByPath={optionsByPath}
        records={records}
        resource={resource}
        saving={saving}
      />
      <DsDialog
        busy={saving}
        closeLabel={t("react.settings.close_delete_confirmation", "Close delete confirmation")}
        description={t("react.settings.delete_value1_this_cannot_be_undone", "Delete {value1}? This cannot be undone.", { value1: String(pendingDelete ? recordLabel(pendingDelete, resource) : "this record") })}
        eyebrow={t("shell.nav.settings", "Settings")}
        footer={
          <>
            <button className="fs-button" disabled={saving} onClick={() => setPendingDelete(null)} type="button">
              {t("common.cancel", "Cancel")} </button>
            <Button
              busy={saving}
              busyLabel={t("react.settings.deleting", "Deleting…")}
              onClick={() => ignoreAsyncResult(deleteResource())}
              type="button"
              variant="danger"
            >
              {t("react.settings.delete_record", "Delete record")}
            </Button>
          </>
        }
        onClose={() => setPendingDelete(null)}
        open={Boolean(pendingDelete)}
        title={t("react.settings.delete_value1_record", "Delete {value1} record", { value1: String(resource.title.toLowerCase()) })}
        variant="confirm"
      />
      <DsDialog
        busy={saving}
        closeLabel={t("react.settings.close_action_confirmation", "Close confirmation")}
        description={pendingAction?.action.confirm ?? ""}
        eyebrow={t("shell.nav.settings", "Settings")}
        footer={
          <>
            <button className="fs-button" disabled={saving} onClick={() => setPendingAction(null)} type="button">
              {t("common.cancel", "Cancel")} </button>
            <Button
              busy={saving}
              onClick={() => {
                if (pendingAction) ignoreAsyncResult(runRowAction(pendingAction.record, pendingAction.action));
              }}
              type="button"
            >
              {pendingAction?.action.label ?? ""}
            </Button>
          </>
        }
        onClose={() => setPendingAction(null)}
        open={Boolean(pendingAction)}
        title={pendingAction ? `${pendingAction.action.label}: ${recordLabel(pendingAction.record, resource)}` : ""}
        variant="confirm"
      />
    </div>
  );
}

function RegisteredResourceAdminSurface({
  surface,
  tab,
}: Readonly<{
  surface: SettingsControlSurface;
  tab: SettingsTab;
}>) {
  const { t } = useI18n();
  const [recordsByResource, setRecordsByResource] = useState<Record<string, AdminRecord[]>>({});
  const [optionsByPath, setOptionsByPath] = useState<OptionsByPath>({});
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [unavailable, setUnavailable] = useState<string | null>(null);

  const loadSurface = useCallback(async () => {
    setLoading(true);
    setMessage("");
    setUnavailable(null);
    // The choices of reference fields are records too: they load with the lists,
    // so a provider added above is offered by the rules below at once.
    const optionPaths = [
      ...new Set(surface.resources.flatMap((resource) => resource.fields.map((field) => field.optionsPath).filter(isTruthy))),
    ];
    const optionsRequest = Promise.all(
      optionPaths.map(async (path) => {
        try {
          return [path, choiceOptionsFromPayload(await fetchJson<unknown>(path))] as const;
        } catch {
          // Without its options a field offers nothing, and nothing is blocked.
          return null;
        }
      }),
    );
    try {
      const entries = await Promise.all(
        surface.resources.map(async (resource) => {
          const payload = await fetchJson<ResourceCollectionResponse>(resource.listPath);
          return [resource.key, recordsFromPayload(payload, resource)] as const;
        }),
      );
      setRecordsByResource(Object.fromEntries(entries));
      setOptionsByPath(Object.fromEntries((await optionsRequest).filter(isTruthy)));
    } catch (error) {
      setRecordsByResource({});
      if (isFeatureNotEntitled(error)) {
        setUnavailable(error instanceof Error ? error.message : "");
      } else {
        setMessage(error instanceof Error ? error.message : t("react.settings.failed_to_load_this_settings_area", "Failed to load this settings area."));
      }
    } finally {
      setLoading(false);
    }
  }, [surface.resources, t]);

  useEffect(() => {
    ignoreAsyncResult(loadSurface());
  }, [loadSurface]);

  const recordCount = surface.resources.reduce(
    (total, resource) => total + (recordsByResource[resource.key] || []).length,
    0,
  );

  if (unavailable !== null) {
    return <RegisteredPageUnavailable description={unavailable} />;
  }

  return (
    <>
      <DsCard description={surface.description || undefined} title={tab.title}>
        <div className="fs-panel-heading">
          <p className="fs-ink-secondary">{t("react.settings.records_count", "{count} records", { count: recordCount })}</p>
          <Button busy={loading} busyLabel={t("registry.index.reloading", "Reloading…")} onClick={() => ignoreAsyncResult(loadSurface())} type="button" variant="secondary">
            {t("account.tokens.refresh", "Refresh")} </Button>
        </div>
        {message ? <DsAlert title={message} tone="warning" /> : null}
      </DsCard>
      {surface.resources.map((resource) => (
        <ResourceAdminPanel
          key={resource.key}
          loading={loading}
          onReload={loadSurface}
          optionsByPath={optionsByPath}
          records={recordsByResource[resource.key] || []}
          resource={resource}
        />
      ))}
    </>
  );
}

export function RegisteredSettingsResourceAdminPage({
  page,
}: Readonly<{
  page: RegisteredPageRecord | SettingsPageRecord;
}>) {
  const { t } = useI18n();
  const pageKey = normalizePageKey(firstStringFromUnknownValue([page.page_key, page.key, ""]));
  const title = firstStringFromUnknownValue([page.title, page.label, pageKey, t("monitoring.settings", "Settings")]).trim();
  const path = firstStringFromUnknownValue([page.path, ""]).trim();
  const surface = normalizeControlSurface(page.control_surface);
  const reportSurface = normalizeResourceReportSurface(page.control_surface);
  const statusSurface = normalizeStatusDocumentSurface(page.control_surface);
  const tab = {
    controlSurface: surface,
    reportSurface,
    statusSurface,
    pageKey,
    path,
    title,
  };

  if (reportSurface) {
    return <ResourceReportPanel surface={reportSurface} title={title} />;
  }
  if (statusSurface) {
    return <StatusDocumentPanel surface={statusSurface} title={title} />;
  }
  if (surface?.kind === "resource_admin") {
    return <RegisteredResourceAdminSurface surface={surface} tab={tab} />;
  }
  return <RegisteredTabFallback tab={tab} />;
}

export function SettingsHubPage() {
  const { t } = useI18n();
  const auth = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const administers = hasCapability(sessionPermissions(auth.session), "administer_platform");
  const tabs = useMemo(
    () => normalizeTabs(auth.session?.settings_pages || [], t, administers),
    [auth.session?.settings_pages, t, administers],
  );
  const pathTab = useMemo(() => {
    const matchedTab = tabs.find((tab) => tabPath(tab) === location.pathname);
    if (matchedTab) {
      return matchedTab.pageKey;
    }
    return "";
  }, [location.pathname, tabs]);
  const activeTab = useMemo(() => {
    const requested = normalizePageKey(pathTab || tabs[0]?.pageKey || "general");
    if (tabs.some((tab) => tab.pageKey === requested)) {
      return requested;
    }
    return tabs[0]?.pageKey || "general";
  }, [pathTab, tabs]);
  const activeTabRecord = useMemo(
    () => tabs.find((tab) => tab.pageKey === activeTab) || tabs[0],
    [activeTab, tabs],
  );

  function switchTab(nextTab: string) {
    const normalizedTab = normalizePageKey(nextTab);
    const tab = tabs.find((candidate) => candidate.pageKey === normalizedTab);
    ignoreAsyncResult(navigate(tabPath(tab || { pageKey: normalizedTab, path: "", title: normalizedTab })));
  }

  function renderTabContent(tab: SettingsTab) {
    const pageKey = normalizePageKey(tab.pageKey);
    switch (pageKey) {
      case "general":
        return <SettingsGeneralPage />;
      case "notifications":
        return <SettingsNotificationsPage />;
      case "providers":
      case "model_providers":
        return <SettingsProvidersPage />;
      case "retention":
        return <SettingsRetentionPage />;
      case "users":
        return <SettingsUsersPage />;
      case "service_accounts":
        return <SettingsServiceAccountsPage />;
      case "security":
        return <SettingsSecurityPage embedded />;
      case "email":
        return <SettingsEmailPage />;
      case "secret_storage":
        return <SettingsSecretStoragePage />;
      default:
        if (tab.reportSurface) {
          return <ResourceReportPanel surface={tab.reportSurface} title={tab.title} />;
        }
        if (tab.statusSurface) {
          return <StatusDocumentPanel surface={tab.statusSurface} title={tab.title} />;
        }
        if (tab.controlSurface?.kind === "resource_admin") {
          return <RegisteredResourceAdminSurface surface={tab.controlSurface} tab={tab} />;
        }
        return <RegisteredTabFallback tab={tab} />;
    }
  }

  // The row scrolls rather than truncating, so a tab past the right edge would
  // be selected but out of sight. Bring it into view whenever it changes.
  useEffect(() => {
    const selected = document.getElementById(`settings-tab-${activeTab}`);
    if (typeof selected?.scrollIntoView === "function") {
      selected.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }, [activeTab]);

  function focusTab(index: number) {
    const next = tabs[(index + tabs.length) % tabs.length];
    if (!next) return;
    switchTab(next.pageKey);
    queueMicrotask(() => document.getElementById(`settings-tab-${next.pageKey}`)?.focus());
  }

  return (
    <PageContainer className="fs-page-sections" width="wide">
      <DsPageHeader
        description={t("react.settings.platform_configuration_one_area_per_tab", "Platform configuration, one area per tab.")}
        action={
          administers ? (
            <Link className="fs-button" to="/registry">
              {t("react.settings.registry_dashboard", "Registry dashboard")} </Link>
          ) : undefined
        }
        eyebrow={t("shell.nav.settings", "Settings")}
        title={t("react.settings.settings_hub", "Settings hub")}
      />
      <section aria-label={t("monitoring.settings", "Settings")} className="fs-settings-tabs">
        {/* `#settings-tabs [data-tab=…]` is what tests/ui addresses. */}
        <div className="fs-tabs-scroll" id="settings-tabs">
          <div aria-label={t("react.settings.settings_areas", "Settings areas")} className="fs-tabs-list" role="tablist">
            {tabs.map((tab, index) => {
              const selected = tab.pageKey === activeTab;
              return (
                <button
                  aria-controls={`${tab.pageKey}-content`}
                  aria-selected={selected}
                  className="fs-tab"
                  data-tab={tab.pageKey}
                  id={`settings-tab-${tab.pageKey}`}
                  key={tab.pageKey}
                  onClick={() => switchTab(tab.pageKey)}
                  onKeyDown={(event) => {
                    const moves: Record<string, number> = {
                      ArrowRight: index + 1,
                      ArrowLeft: index - 1,
                      Home: 0,
                      End: tabs.length - 1,
                    };
                    if (!(event.key in moves)) return;
                    event.preventDefault();
                    focusTab(moves[event.key] ?? index);
                  }}
                  role="tab"
                  tabIndex={selected ? 0 : -1}
                  type="button"
                >
                  {tab.title}
                </button>
              );
            })}
          </div>
        </div>
        {activeTabRecord ? (
          <div
            aria-labelledby={`settings-tab-${activeTabRecord.pageKey}`}
            className="fs-tabs-panel"
            id={`${activeTabRecord.pageKey}-content`}
            role="tabpanel"
          >
            <div className="fs-panel-stack">{renderTabContent(activeTabRecord)}</div>
          </div>
        ) : null}
      </section>
    </PageContainer>
  );
}
