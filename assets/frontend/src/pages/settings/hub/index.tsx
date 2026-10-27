import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  type Dispatch,
  type SetStateAction,
  type SyntheticEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";

import { Link, useLocation, useNavigate } from "react-router";

import type { RegisteredPageRecord, SettingsPageRecord } from "@/lib/auth/AuthProvider";
import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import {
  DsAlert,
  DsCard,
  DsCheckbox,
  DsDialog,
  DsPageHeader,
  DsSelect,
  DsTable,
  type DsTableColumn,
} from "@/components/shared/design-system";
import { SettingsGeneralPage } from "@/pages/settings/general";
import { SettingsNotificationsPage } from "@/pages/settings/notifications";
import { SettingsProvidersPage } from "@/pages/settings/providers";
import { SettingsRetentionPage } from "@/pages/settings/retention";
import { SettingsSecurityPage } from "@/pages/settings/security";
import { SettingsServiceAccountsPage } from "@/pages/settings/service-accounts";
import { SettingsUsersPage } from "@/pages/settings/users";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { trimRepeatedEdges, trimRepeatedEnd } from "@/lib/utils/string-normalization";
import { replaceDelimitedTokens } from "@/lib/utils/template-tokens";

function resourceSaveLabel(saving: boolean, editing: boolean, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (saving) return t("project_edit.security.saving", "Saving...");
  return editing ? t("registry.common.save", "Save") : t("projects.modal.create", "Create");
}

type SettingsTab = {
  controlSurface?: SettingsControlSurface;
  pageKey: string;
  path: string;
  title: string;
};

type ControlSurfaceField = {
  createOnly: boolean;
  defaultValue: string | boolean | number;
  key: string;
  label: string;
  omitBlankOnUpdate: boolean;
  options: string[];
  placeholder: string;
  required: boolean;
  type: "checkbox" | "csv" | "json" | "number" | "select" | "textarea" | "text";
};

type ControlSurfaceResource = {
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
  summaryFields: string[];
  title: string;
};

type SettingsControlSurface = {
  description: string;
  kind: string;
  resources: ControlSurfaceResource[];
};

type AdminRecord = Record<string, unknown>;

type ResourceCollectionResponse = Record<string, unknown>;

type FormValues = Record<string, string | boolean>;

type SaveResponse = Record<string, unknown>;



function getFallbackSettingsTabs(t: I18nContextValue["t"] = fallbackContext.t): SettingsTab[] {
  return [
  { pageKey: "general", path: "/settings/general", title: t("registry.workflow_detail.settings.general", "General") },
  { pageKey: "security", path: "/settings/security", title: t("account.security.title", "Security") },
  { pageKey: "service_accounts", path: "/settings/service-accounts", title: t("react.settings.service_accounts", "Service Accounts") },
  { pageKey: "retention", path: "/settings/retention", title: t("settings.retention.title", "Retention") },
  { pageKey: "notifications", path: "/settings/notifications", title: t("settings.tab.notifications", "Notifications") },
  { pageKey: "model_providers", path: "/settings/providers", title: t("settings.tab.model_providers", "Model Providers") },
];
}

const SETTINGS_PAGE_KEY_TO_PATH: Record<string, string> = {
  general: "/settings/general",
  model_providers: "/settings/providers",
  notifications: "/settings/notifications",
  providers: "/settings/providers",
  retention: "/settings/retention",
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

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((item) => firstStringFromUnknownValue([item, ""]).trim()).filter(isTruthy);
}

function normalizeField(value: unknown): ControlSurfaceField | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const key = firstStringFromUnknownValue([record.key, ""]).trim();
  const label = firstStringFromUnknownValue([record.label, key]).trim();
  if (!key || !label) {
    return null;
  }
  const rawType = firstStringFromUnknownValue([record.type, "text"]).trim();
  const type = ["checkbox", "csv", "json", "number", "select", "textarea"].includes(rawType)
    ? (rawType as ControlSurfaceField["type"])
    : "text";
  const defaultValue =
    typeof record.default === "boolean" || typeof record.default === "number"
      ? record.default
      : firstStringFromUnknownValue([record.default, ""]);
  return {
    createOnly: Boolean(record.create_only),
    defaultValue,
    key,
    label,
    omitBlankOnUpdate: Boolean(record.omit_blank_on_update),
    options: stringList(record.options),
    placeholder: firstStringFromUnknownValue([record.placeholder, ""]).trim(),
    required: Boolean(record.required),
    type,
  };
}

function normalizeResource(value: unknown): ControlSurfaceResource | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const key = firstStringFromUnknownValue([record.key, ""]).trim();
  const title = firstStringFromUnknownValue([record.title, key]).trim();
  const listPath = firstStringFromUnknownValue([record.list_path, ""]).trim();
  const idField = firstStringFromUnknownValue([record.id_field, ""]).trim();
  const fields = Array.isArray(record.fields)
    ? record.fields.map(normalizeField).filter(isTruthy)
    : [];
  if (!key || !title || !listPath.startsWith("/") || !idField || fields.length === 0) {
    return null;
  }
  return {
    collectionKey: firstStringFromUnknownValue([record.collection_key, "items"]).trim() || "items",
    description: firstStringFromUnknownValue([record.description, ""]).trim(),
    displayField: firstStringFromUnknownValue([record.display_field, idField]).trim() || idField,
    emptyLabel: typeof record.empty_label === "string" ? record.empty_label : undefined,
    fields,
    idField,
    itemPath: firstStringFromUnknownValue([record.item_path, ""]).trim(),
    key,
    listPath,
    recordKey: firstStringFromUnknownValue([record.record_key, "item"]).trim() || "item",
    summaryFields: stringList(record.summary_fields),
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

function normalizeTabs(records: SettingsPageRecord[], t: I18nContextValue["t"] = fallbackContext.t): SettingsTab[] {
  const tabs = records
    .filter((record) => {
      const pageKey = normalizePageKey(firstStringFromUnknownValue([record.page_key, ""]));
      return record.settings_tab && pageKey !== "extensions";
    })
    .map((record) => ({
      controlSurface: normalizeControlSurface(record.control_surface),
      pageKey: normalizePageKey(firstStringFromUnknownValue([record.page_key, ""])),
      path: firstStringFromUnknownValue([record.path, ""]).trim() || "/settings",
      title: firstStringFromUnknownValue([record.title, record.page_key, ""]).trim(),
      sortOrder: Number(record.sort_order || 100),
    }))
    .filter((record) => Boolean(record.pageKey) && Boolean(record.title))
    .sort((left, right) => left.sortOrder - right.sortOrder || left.pageKey.localeCompare(right.pageKey))
    .map(({ controlSurface, pageKey, path, title }) => ({ controlSurface, pageKey, path, title }));
  return tabs.length ? tabs : getFallbackSettingsTabs(t);
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

function recordSummary(record: AdminRecord, resource: ControlSurfaceResource): string {
  return resource.summaryFields
    .map((field) => {
      const value = record[field];
      if (Array.isArray(value)) {
        return value.join(", ");
      }
      if (typeof value === "boolean") {
        return value ? "enabled" : "disabled";
      }
      return stringFromUnknownValue(value ?? "").trim();
    })
    .filter(isTruthy)
    .join(" - ");
}

function stableControlToken(value: unknown): string {
  const token = firstStringFromUnknownValue([value, ""])
    .trim()
    .toLowerCase()
    .replaceAll(/[^a-z0-9_-]+/g, "-");
  return trimRepeatedEdges(token, "-");
}

function fieldControlId(resource: ControlSurfaceResource, field: ControlSurfaceField, editingId: string) {
  return [
    "settings",
    stableControlToken(resource.key),
    stableControlToken(field.key),
    stableControlToken(editingId || "new"),
  ]
    .filter(isTruthy)
    .join("-");
}

function resolveItemPath(resource: ControlSurfaceResource, id: string): string {
  const encodedId = encodeURIComponent(id);
  if (resource.itemPath) {
    return resource.itemPath.includes("{")
      ? replaceDelimitedTokens(resource.itemPath, "{", "}", () => encodedId)
      : `${trimRepeatedEnd(resource.itemPath, "/")}/${encodedId}`;
  }
  return `${trimRepeatedEnd(resource.listPath, "/")}/${encodedId}`;
}

function defaultFormValues(resource: ControlSurfaceResource): FormValues {
  return Object.fromEntries(
    resource.fields.map((field) => [
      field.key,
      field.type === "checkbox" ? Boolean(field.defaultValue) : firstStringFromUnknownValue([field.defaultValue, ""]),
    ]),
  );
}

function valuesFromRecord(resource: ControlSurfaceResource, record: AdminRecord): FormValues {
  return Object.fromEntries(
    resource.fields.map((field) => {
      const value = record[field.key];
      if (field.type === "checkbox") {
        return [field.key, Boolean(value)];
      }
      if (field.type === "csv") {
        return [field.key, Array.isArray(value) ? value.join(", ") : stringFromUnknownValue(value ?? "")];
      }
      if (field.type === "json") {
        return [field.key, field.omitBlankOnUpdate ? "" : JSON.stringify(value ?? {}, null, 2)];
      }
      return [field.key, stringFromUnknownValue(value ?? field.defaultValue ?? "")];
    }),
  );
}

function payloadFromValues(
  resource: ControlSurfaceResource,
  values: FormValues,
  options: { editing: boolean },
  t: I18nContextValue["t"],
): Record<string, unknown> {
  const payload: Record<string, unknown> = {};
  for (const field of resource.fields) {
    const value = values[field.key];
    const textValue = typeof value === "string" ? value.trim() : "";
    if (options.editing && field.createOnly) {
      continue;
    }
    if (options.editing && field.omitBlankOnUpdate && textValue === "") {
      continue;
    }
    payload[field.key] = normalizedResourceFieldValue(
      field,
      value,
      textValue,
      options.editing,
      t,
    );
  }
  return payload;
}

function normalizedResourceFieldValue(
  field: ControlSurfaceField,
  value: string | boolean,
  textValue: string,
  editing: boolean,
  t: I18nContextValue["t"],
): unknown {
  if (
    field.required &&
    !editing &&
    field.type !== "checkbox" &&
    textValue === ""
  ) {
    throw new Error(t("react.settings.resource_field_required", "{field} is required.", { field: field.label }));
  }
  if (field.type === "checkbox") {
    return Boolean(value);
  }
  if (field.type === "csv") {
    const items = textValue
      .split(",")
      .map((item) => item.trim())
      .filter(isTruthy);
    if (field.required && items.length === 0) {
      throw new Error(t("react.settings.resource_field_required", "{field} is required.", { field: field.label }));
    }
    return items;
  }
  if (field.type === "number") {
    return textValue ? Number(textValue) : 0;
  }
  if (field.type === "json") {
    return textValue ? JSON.parse(textValue) : {};
  }
  return textValue;
}

function ResourceFieldControl({
  controlId,
  editingId,
  field,
  setFieldValue,
  value,
}: Readonly<{
  controlId: string;
  editingId: string;
  field: ControlSurfaceField;
  setFieldValue: (value: string | boolean) => void;
  value: string | boolean;
}>) {
  if (field.type === "select") {
    return (
      <DsSelect
        ariaLabel={field.label}
        id={controlId}
        onChange={setFieldValue}
        options={field.options.map((option) => ({ value: option, label: option }))}
        required={field.required}
        value={firstStringFromUnknownValue([value, ""])}
      />
    );
  }
  if (field.type === "textarea" || field.type === "json") {
    return (
      <textarea
        className={field.type === "json" ? "fs-textarea fs-code-value" : "fs-textarea"}
        id={controlId}
        name={field.key}
        onChange={(event) => setFieldValue(event.target.value)}
        placeholder={field.placeholder}
        required={field.required && !editingId}
        value={firstStringFromUnknownValue([value, ""])}
      />
    );
  }
  return (
    <input
      className="fs-input"
      id={controlId}
      name={field.key}
      onChange={(event) => setFieldValue(event.target.value)}
      placeholder={field.placeholder}
      required={field.required}
      type={field.type === "number" ? "number" : "text"}
      value={firstStringFromUnknownValue([value, ""])}
    />
  );
}

function ResourceForm({
  editingId,
  formValues,
  onCancel,
  onSubmit,
  resource,
  saving,
  setFormValues,
}: Readonly<{
  editingId: string;
  formValues: FormValues;
  onCancel: () => void;
  onSubmit: (event: SyntheticEvent<HTMLFormElement>) => void;
  resource: ControlSurfaceResource;
  saving: boolean;
  setFormValues: Dispatch<SetStateAction<FormValues>>;
}>) {
  const { t } = useI18n();
  const visibleFields = resource.fields.filter(
    (field) => !(editingId && field.createOnly),
  );
  return (
    <form
      className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4"
      noValidate
      onSubmit={onSubmit}
    >
      <div className="fs-form-grid">
        {visibleFields.map((field) => {
          const controlId = fieldControlId(resource, field, editingId);
          const setFieldValue = (value: string | boolean) =>
            setFormValues((current) => ({ ...current, [field.key]: value }));
          if (field.type === "checkbox") {
            // The checkbox carries its own label, the published control anatomy.
            return (
              <div className="fs-field" key={field.key}>
                <DsCheckbox checked={Boolean(formValues[field.key])} name={field.key} onChange={setFieldValue}>
                  {field.label}
                </DsCheckbox>
              </div>
            );
          }
          return (
            <div className="fs-field" key={field.key}>
              <label className={field.required ? "fs-field-label fs-required-label" : "fs-field-label"} htmlFor={controlId}>
                {field.label}
                {field.required ? <span aria-hidden="true" className="fs-required">*</span> : null}
              </label>
              <ResourceFieldControl
                controlId={controlId}
                editingId={editingId}
                field={field}
                setFieldValue={setFieldValue}
                value={formValues[field.key]}
              />
            </div>
          );
        })}
      </div>
      <div className="fs-footer-actions">
        <button className="fs-button primary" disabled={saving} type="submit">
          {resourceSaveLabel(saving, Boolean(editingId), t)}
        </button>
        <button className="fs-button" onClick={onCancel} type="button">
          {t("common.cancel", "Cancel")} </button>
      </div>
    </form>
  );
}

function resourceRecordColumns({
  onDelete, onEdit, resource, saving, t,
}: Readonly<{
  onDelete: (record: AdminRecord) => void;
  onEdit: (record: AdminRecord) => void;
  resource: ControlSurfaceResource;
  saving: boolean;
  t: I18nContextValue["t"];
}>): Array<DsTableColumn<AdminRecord>> {
  return [
    {
      key: "record",
      header: t("react.settings.record", "Record"),
      cell: (record) => (
        <span className="fs-user-copy">
          <strong>{recordLabel(record, resource)}</strong>
          <span>{firstStringFromUnknownValue([record[resource.idField], ""]).trim()}</span>
        </span>
      ),
    },
    {
      key: "summary",
      header: t("jobs.columns.summary", "Summary"),
      truncate: "28rem",
      tooltip: (record) => recordSummary(record, resource) || undefined,
      cell: (record) => recordSummary(record, resource) || null,
    },
    {
      key: "actions",
      header: "",
      ariaLabel: t("monitoring.table.actions", "Actions"),
      cell: (record) => (
        <span className="fs-footer-actions">
          <button className="fs-button ghost" disabled={saving} onClick={() => onEdit(record)} type="button">
            {t("projects.grid.action.edit", "Edit")} </button>
          <button className="fs-button ghost-danger" disabled={saving} onClick={() => onDelete(record)} type="button">
            {t("projects.grid.action.delete", "Delete")} </button>
        </span>
      ),
    },
  ];
}

function ResourceRecords({
  loading,
  onDelete,
  onEdit,
  records,
  resource,
  saving,
}: Readonly<{
  loading: boolean;
  onDelete: (record: AdminRecord) => void;
  onEdit: (record: AdminRecord) => void;
  records: AdminRecord[];
  resource: ControlSurfaceResource;
  saving: boolean;
}>) {
  const { t } = useI18n();
  const columns = resourceRecordColumns({ onDelete, onEdit, resource, saving, t });
  return (
    <article className="fs-card fs-table-card">
      <DsTable
        ariaLabel={resource.title}
        busy={loading}
        columns={columns}
        rowKey={(record) => firstStringFromUnknownValue([record[resource.idField], ""]).trim()}
        rows={records}
        state={
          loading || records.length ? null : (
            <div className="fs-table-state">
              <strong>{resource.emptyLabel ?? t("react.settings.no_records_configured", "No records are configured.")}</strong>
            </div>
          )
        }
      />
    </article>
  );
}

function ResourceAdminPanel({
  loading,
  onReload,
  records,
  resource,
}: Readonly<{
  loading: boolean;
  onReload: () => Promise<void>;
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

  function beginCreate() {
    setEditingId("");
    setFormValues(defaultFormValues(resource));
    setMessage(null);
    setFormOpen(true);
  }

  function beginEdit(record: AdminRecord) {
    const id = firstStringFromUnknownValue([record[resource.idField], ""]).trim();
    setEditingId(id);
    setFormValues(valuesFromRecord(resource, record));
    setMessage(null);
    setFormOpen(true);
  }

  async function saveResource(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    setSaving(true);
    try {
      const editing = Boolean(editingId);
      const payload = payloadFromValues(resource, formValues, { editing }, t);
      await fetchJson<SaveResponse>(editing ? resolveItemPath(resource, editingId) : resource.listPath, {
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
    if (!id) {
      setMessage({ text: t("react.settings.record_id_is_missing", "Record ID is missing."), tone: "danger" });
      return;
    }
    setPendingDelete(record);
  }

  async function deleteResource() {
    const record = pendingDelete;
    if (!record) return;
    const id = firstStringFromUnknownValue([record[resource.idField], ""]).trim();
    setMessage(null);
    setSaving(true);
    try {
      await fetchJson<SaveResponse>(resolveItemPath(resource, id), { method: "DELETE" });
      setMessage({ text: t("react.settings.record_deleted", "Record deleted."), tone: "success" });
      await onReload();
    } catch (error) {
      setMessage({ text: error instanceof Error ? error.message : t("react.settings.failed_to_delete_record", "Failed to delete record."), tone: "danger" });
    } finally {
      setSaving(false);
      setPendingDelete(null);
    }
  }

  function cancelForm() {
    setFormOpen(false);
    setEditingId("");
    setFormValues(defaultFormValues(resource));
  }

  return (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{resource.title}</h2>
          {resource.description ? <p>{resource.description}</p> : null}
        </div>
        <button className="fs-button primary" disabled={saving} onClick={beginCreate} type="button">
          {t("registry.agent_detail.common.add", "Add")} </button>
      </div>
      {message ? <DsAlert title={message.text} tone={message.tone} /> : null}
      {formOpen ? (
        <ResourceForm
          editingId={editingId}
          formValues={formValues}
          onCancel={cancelForm}
          onSubmit={(event) => ignoreAsyncResult(saveResource(event))}
          resource={resource}
          saving={saving}
          setFormValues={setFormValues}
        />
      ) : null}
      <ResourceRecords
        loading={loading}
        onDelete={requestDelete}
        onEdit={beginEdit}
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
            <button
              className="fs-button danger"
              disabled={saving}
              onClick={() => ignoreAsyncResult(deleteResource())}
              type="button"
            >
              {saving ? t("react.settings.deleting", "Deleting...") : t("react.settings.delete_record", "Delete record")}
            </button>
          </>
        }
        onClose={() => setPendingDelete(null)}
        open={Boolean(pendingDelete)}
        title={t("react.settings.delete_value1_record", "Delete {value1} record", { value1: String(resource.title.toLowerCase()) })}
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
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");

  const loadSurface = useCallback(async () => {
    setLoading(true);
    setMessage("");
    try {
      const entries = await Promise.all(
        surface.resources.map(async (resource) => {
          const payload = await fetchJson<ResourceCollectionResponse>(resource.listPath);
          return [resource.key, recordsFromPayload(payload, resource)] as const;
        }),
      );
      setRecordsByResource(Object.fromEntries(entries));
    } catch (error) {
      setRecordsByResource({});
      setMessage(error instanceof Error ? error.message : t("react.settings.failed_to_load_this_settings_area", "Failed to load this settings area."));
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

  return (
    <>
      <DsCard description={surface.description || undefined} title={tab.title}>
        <div className="fs-panel-heading">
          <p className="fs-ink-secondary">{t("react.settings.records_count", "{count} records", { count: recordCount })}</p>
          <button className="fs-button" disabled={loading} onClick={() => ignoreAsyncResult(loadSurface())} type="button">
            {t("account.tokens.refresh", "Refresh")} </button>
        </div>
        {message ? <DsAlert title={message} tone="warning" /> : null}
      </DsCard>
      {surface.resources.map((resource) => (
        <ResourceAdminPanel
          key={resource.key}
          loading={loading}
          onReload={loadSurface}
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
  const tab = {
    controlSurface: surface,
    pageKey,
    path,
    title,
  };

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
  const tabs = useMemo(() => normalizeTabs(auth.session?.settings_pages || [], t), [auth.session?.settings_pages, t]);
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
      default:
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
    <section className="fs-settings">
      <DsPageHeader
        description={t("react.settings.platform_configuration_one_area_per_tab", "Platform configuration, one area per tab.")}
        action={
          <Link className="fs-button" to="/registry">
            {t("react.settings.registry_dashboard", "Registry dashboard")} </Link>
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
    </section>
  );
}
