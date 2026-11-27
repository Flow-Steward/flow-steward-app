import { useI18n } from "@/lib/i18n/useI18n";
import type { I18nContextValue } from "@/lib/i18n/context";
import { type SyntheticEvent, useMemo, useState } from "react";

import { ChevronRight } from "lucide-react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { BlankCell, DsAlert, DsBadge, DsCard, DsTable, type DsTableColumn } from "@/components/shared/design-system";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { Button } from "@/components/ui/button";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { isTruthy } from "@/lib/utils/unknown-values";
import {
  type ControlSurfaceField,
  type FormResource,
  type FormValues,
  defaultFormValues,
  fieldConditionHolds,
  normalizeField,
  payloadFromValues,
  valuesFromRecord,
  visibleFormFields,
} from "@/pages/settings/hub/resource-admin-fields";
import { ResourceFormField } from "@/pages/settings/hub/resource-admin-form";
import {
  type SecretStorageConfig,
  type SecretStorageHealth,
  type SecretStorageKind,
  SECRET_STORAGE_QUERY_KEY,
  checkSecretStorageConfig,
  createSecretStorageConfig,
  fetchSecretStorage,
  updateSecretStorageConfig,
} from "@/pages/settings/secret-storage/api";

const BASE_KEY = "secret-storage";
const CONFIG_KEY = "secret-storage-settings";

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "";
}

/** Name, kind (asked once, when the storage is added) and whether it is turned on. */
function baseResource(kinds: SecretStorageKind[], t: I18nContextValue["t"]): FormResource {
  const fields = [
    {
      key: "name",
      label: t("react.settings.secret_storage.name", "Name"),
      required: true,
      help: t("react.settings.secret_storage.name_help", "How this storage is called in lists, for example Production vault."),
    },
    {
      key: "backend",
      type: "select",
      label: t("react.settings.secret_storage.kind", "Kind of storage"),
      required: true,
      create_only: true,
      options: kinds.map((kind) => ({ value: kind.kind, label: kind.label })),
      default: kinds[0]?.kind ?? "",
    },
    {
      key: "enabled",
      type: "checkbox",
      label: t("react.settings.secret_storage.turned_on", "Turned on"),
      default: true,
      help: t("react.settings.secret_storage.turned_on_help", "While it is off, secrets kept there cannot be read."),
    },
  ];
  return { key: BASE_KEY, fields: fields.map(normalizeField).filter(isTruthy) };
}

/** The add-on's own settings form; it never asks for a credential. */
function configResource(kind: SecretStorageKind | undefined): FormResource {
  const fields: ControlSurfaceField[] = (kind?.config_fields ?? []).map(normalizeField).filter(isTruthy);
  return { key: CONFIG_KEY, fields };
}

function sameSettings(left: Record<string, unknown>, right: Record<string, unknown>): boolean {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  return [...keys].every((key) => JSON.stringify(left[key] ?? null) === JSON.stringify(right[key] ?? null));
}

function healthText(health: SecretStorageHealth | undefined, t: I18nContextValue["t"]): { text: string; tone: "green" | "danger" | "neutral" } {
  if (!health) return { text: t("react.settings.secret_storage.health_unknown", "Not checked"), tone: "neutral" };
  if (health.healthy) return { text: t("react.settings.secret_storage.health_ok", "Connected"), tone: "green" };
  if (health.code === "turned_off") return { text: t("react.settings.secret_storage.health_off", "Turned off"), tone: "neutral" };
  if (health.code === "not_installed") {
    return { text: t("react.settings.secret_storage.health_not_installed", "Add-on not installed"), tone: "danger" };
  }
  return { text: t("react.settings.secret_storage.health_failed", "Cannot connect"), tone: "danger" };
}

function StorageForm({
  editing,
  kinds,
  onClose,
  onSaved,
}: Readonly<{
  editing: SecretStorageConfig | null;
  kinds: SecretStorageKind[];
  onClose: () => void;
  onSaved: (message: string) => void;
}>) {
  const { t } = useI18n();
  const base = useMemo(() => baseResource(kinds, t), [kinds, t]);
  const [baseValues, setBaseValues] = useState<FormValues>(() =>
    editing ? valuesFromRecord(base, editing as unknown as Record<string, unknown>) : defaultFormValues(base),
  );
  const kindKey = editing ? editing.backend : String(baseValues.backend ?? "");
  const kind = kinds.find((candidate) => candidate.kind === kindKey);
  const settings = useMemo(() => configResource(kind), [kind]);
  const [settingsKind, setSettingsKind] = useState(kindKey);
  const [settingsValues, setSettingsValues] = useState<FormValues>(() =>
    editing ? valuesFromRecord(settings, editing.config) : defaultFormValues(settings),
  );
  const [error, setError] = useState("");
  if (settingsKind !== kindKey) {
    // Another kind has another form: start it from its own defaults.
    setSettingsKind(kindKey);
    setSettingsValues(defaultFormValues(settings));
  }

  const save = useMutation({
    mutationFn: async () => {
      const options = { editing: false, optionsByPath: {} };
      const config = payloadFromValues(settings, settingsValues, options, t);
      // A setting the form does not show is not part of the configuration at all.
      for (const field of settings.fields) {
        if (!fieldConditionHolds(field, settingsValues, settings.fields, options.optionsByPath)) delete config[field.key];
      }
      const baseOptions = { editing: Boolean(editing), optionsByPath: {} };
      const values = payloadFromValues(base, baseValues, baseOptions, t);
      const name = String(values.name ?? "").trim();
      if (!name) throw new Error(t("react.settings.resource_field_required", "{field} is required.", { field: base.fields[0]?.label ?? "" }));
      if (editing) {
        return updateSecretStorageConfig(editing.id, {
          name,
          enabled: Boolean(values.enabled),
          // Unchanged settings are not sent: renaming or turning off needs nothing more.
          ...(sameSettings(config, editing.config) ? {} : { config }),
        });
      }
      return createSecretStorageConfig({ backend: kindKey, name, config, enabled: Boolean(values.enabled) });
    },
    onSuccess: (saved) =>
      onSaved(
        editing
          ? t("react.settings.secret_storage.updated", "{value0} saved.", { value0: saved.name })
          : t("react.settings.secret_storage.created", "{value0} added.", { value0: saved.name }),
      ),
    onError: (failure) => setError(errorText(failure)),
  });

  const submit = (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    save.mutate();
  };
  const renderField = (resource: FormResource, values: FormValues, setValues: typeof setBaseValues) =>
    function render(field: ControlSurfaceField) {
      return (
        <ResourceFormField
          editingId={editing?.id ?? ""}
          field={field}
          formValues={values}
          key={`${resource.key}-${field.key}`}
          optionsByPath={{}}
          resource={resource}
          setFormValues={setValues}
        />
      );
    };
  const baseFields = visibleFormFields(base, baseValues, editing?.id ?? "", {});
  const settingsFields = visibleFormFields(settings, settingsValues, "", {});
  const advanced = settingsFields.filter((field) => field.advanced);
  return (
    <form
      aria-label={editing ? t("react.settings.secret_storage.edit_title", "Edit {value0}", { value0: editing.name }) : t("react.settings.secret_storage.add", "Add storage")}
      className="fs-surface-radius fs-surface-inset border border-subtle fs-panel-group p-4"
      noValidate
      onSubmit={submit}
    >
      {error ? <DsAlert title={t("react.settings.secret_storage.not_saved", "Not saved")} tone="danger">{error}</DsAlert> : null}
      <div className="fs-form-grid">
        {baseFields.map(renderField(base, baseValues, setBaseValues))}
        {settingsFields.filter((field) => !field.advanced).map(renderField(settings, settingsValues, setSettingsValues))}
      </div>
      {advanced.length ? (
        <details className="fs-disclosure">
          <summary>
            <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
            {t("react.settings.advanced_settings", "Advanced settings")}
          </summary>
          <div className="fs-disclosure-body fs-form-grid">{advanced.map(renderField(settings, settingsValues, setSettingsValues))}</div>
        </details>
      ) : null}
      <div className="fs-footer-actions">
        <Button busy={save.isPending} busyLabel={t("react.settings.saving", "Saving…")} type="submit" variant="primary">
          {editing ? t("registry.common.save", "Save") : t("react.settings.secret_storage.add", "Add storage")}
        </Button>
        <button className="fs-button" disabled={save.isPending} onClick={onClose} type="button">
          {t("common.cancel", "Cancel")}
        </button>
      </div>
    </form>
  );
}

/**
 * Settings → Secret storage: where the account's secret values are kept. Core keeps
 * them encrypted in its own database; an installed add-on may offer an external
 * secret manager, which an administrator sets up here.
 */
export function SettingsSecretStoragePage() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [formFor, setFormFor] = useState<"closed" | "new" | string>("closed");
  const [feedback, setFeedback] = useState("");

  const storageQuery = useQuery({ queryKey: SECRET_STORAGE_QUERY_KEY, queryFn: fetchSecretStorage });
  const loaderShown = useLoaderDelay(storageQuery.isLoading);
  const kinds = storageQuery.data?.backends ?? [];
  const configs = storageQuery.data?.configs ?? [];
  const checked = configs.filter((config) => config.enabled && config.installed);
  const healthQueries = useQueries({
    queries: checked.map((config) => ({
      queryKey: [...SECRET_STORAGE_QUERY_KEY, "health", config.id],
      queryFn: () => checkSecretStorageConfig(config.id),
      retry: false,
    })),
  });
  const healthById = new Map<string, { health?: SecretStorageHealth; checking: boolean; refetch: () => unknown }>(
    checked.map((config, index) => [
      config.id,
      {
        health: healthQueries[index]?.data,
        checking: Boolean(healthQueries[index]?.isFetching),
        refetch: () => healthQueries[index]?.refetch(),
      },
    ]),
  );
  const addable = kinds.filter((kind) => kind.entitled);
  const editing = configs.find((config) => config.id === formFor) ?? null;

  const description = t(
    "react.settings.secret_storage.description",
    "Secret values are kept encrypted in this installation. An add-on can keep them in an external secret manager instead; set it up here, then move secrets there from Registry → Secrets.",
  );

  if (loaderShown) {
    return <DsLoader label={t("react.settings.secret_storage.loading", "Loading secret storage…")} variant="wide-block" />;
  }
  if (storageQuery.isError) {
    return (
      <DsAlert tone="danger" title={t("react.settings.secret_storage.load_failed", "Could not load secret storage")}>
        {errorText(storageQuery.error)}
      </DsAlert>
    );
  }
  if (storageQuery.isLoading) return null;
  if (kinds.length === 0) {
    return (
      <DsCard
        description={t(
          "react.settings.secret_storage.add_on_only",
          "Secret values are kept encrypted in this installation's database. Keeping them in an external secret manager is an add-on: once one is installed, you can set it up here.",
        )}
        title={t("react.settings.secret_storage.title", "Secret storage")}
      />
    );
  }

  const columns: Array<DsTableColumn<SecretStorageConfig>> = [
    {
      key: "name",
      header: t("react.settings.secret_storage.name", "Name"),
      cell: (config) => (
        <span className="fs-user-copy">
          <strong>{config.name}</strong>
          <span>{config.backend_label}</span>
        </span>
      ),
    },
    {
      key: "enabled",
      header: t("react.settings.secret_storage.state", "State"),
      cell: (config) => {
        if (!config.installed) return <DsBadge tone="danger">{t("react.settings.secret_storage.health_not_installed", "Add-on not installed")}</DsBadge>;
        return config.enabled
          ? <DsBadge tone="green">{t("react.settings.secret_storage.on", "On")}</DsBadge>
          : <DsBadge>{t("react.settings.secret_storage.off", "Off")}</DsBadge>;
      },
    },
    {
      key: "health",
      header: t("react.settings.secret_storage.connection", "Connection"),
      cell: (config) => {
        if (!config.enabled || !config.installed) return <BlankCell />;
        const entry = healthById.get(config.id);
        if (entry?.checking) return <span className="fs-ink-tertiary">{t("react.settings.secret_storage.checking", "Checking…")}</span>;
        const state = healthText(entry?.health, t);
        return <DsBadge tone={state.tone}>{state.text}</DsBadge>;
      },
    },
    {
      key: "actions",
      header: "",
      ariaLabel: t("monitoring.table.actions", "Actions"),
      cell: (config) => {
        const entry = healthById.get(config.id);
        return (
          <span className="fs-footer-actions">
            {entry ? (
              <button className="fs-button ghost" disabled={entry.checking} onClick={() => entry.refetch()} type="button">
                {t("react.settings.secret_storage.check", "Check connection")}
              </button>
            ) : null}
            {config.installed ? (
              <button className="fs-button ghost" onClick={() => { setFeedback(""); setFormFor(config.id); }} type="button">
                {t("projects.grid.action.edit", "Edit")}
              </button>
            ) : null}
          </span>
        );
      },
    },
  ];

  return (
    <>
      <DsCard description={description} title={t("react.settings.secret_storage.title", "Secret storage")}>
        <div className="fs-panel-heading">
          <p className="fs-ink-secondary">
            {t("react.settings.secret_storage.count", "Configured storages: {value0}", { value0: String(configs.length) })}
          </p>
          <button
            className="fs-button primary"
            disabled={addable.length === 0 || formFor !== "closed"}
            onClick={() => { setFeedback(""); setFormFor("new"); }}
            type="button"
          >
            {t("react.settings.secret_storage.add", "Add storage")}
          </button>
        </div>
        {addable.length === 0 ? (
          <DsAlert
            title={t("react.settings.secret_storage.not_entitled", "The installed secret storage add-on is not available for this account.")}
            tone="neutral"
          />
        ) : null}
        {feedback ? <DsAlert title={feedback} tone="success" /> : null}
      </DsCard>
      {formFor !== "closed" && (formFor === "new" || editing) ? (
        <StorageForm
          editing={editing}
          key={formFor}
          kinds={editing ? kinds : addable}
          onClose={() => setFormFor("closed")}
          onSaved={(message) => {
            setFormFor("closed");
            setFeedback(message);
            ignoreAsyncResult(queryClient.invalidateQueries({ queryKey: SECRET_STORAGE_QUERY_KEY }));
          }}
        />
      ) : null}
      <article className="fs-card fs-table-card">
        <DsTable
          ariaLabel={t("react.settings.secret_storage.title", "Secret storage")}
          busy={storageQuery.isFetching}
          columns={columns}
          rowKey={(config) => config.id}
          rows={configs}
          state={
            configs.length === 0 ? (
              <div className="fs-table-state">
                <strong>{t("react.settings.secret_storage.empty", "No external storage is set up. Values stay in this installation.")}</strong>
              </div>
            ) : null
          }
        />
      </article>
    </>
  );
}
