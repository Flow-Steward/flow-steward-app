import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createModelProvider,
  deleteModelProvider,
  fetchModelCatalogSettings,
  fetchModelProvidersCollection,
  reverifyModelProvider,
  updateModelProvider,
} from "@/pages/settings/shared/api";
import {
  firstStringFromUnknownValue,
  stringFromUnknownValue,
} from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { DsAlert, DsDialog, DsSelect, DsTable, RowActionMenu, Search } from "@/components/shared/design-system";
import { columnsFromGridConfig, visibleRowActions } from "@/components/shared/data-grid-adapter";
import { invalidateModelRoutingQueries, providerErrorMessage } from "@/lib/utils/model-provider-state";
import { normalizeModelOptions, type ModelOption } from "@/lib/utils/model-options";

import "@/pages/settings/shared/settings.css";

type Entity = Record<string, unknown>;

type ProviderFormState = {
  api_key: string;
  api_version: string;
  base_url: string;
  default_model: string;
  deployment_name: string;
  name: string;
  provider_type: string;
  upstream_revision: string;
};



function getProviderTypes(t: I18nContextValue["t"] = fallbackContext.t) {
  return [
  { value: "openai", label: t("react.settings.openai_official", "OpenAI (official)") },
  { value: "anthropic", label: t("react.settings.anthropic_official", "Anthropic (official)") },
  { value: "google", label: t("react.settings.google_official", "Google (official)") },
  { value: "openai_compatible", label: t("react.settings.openai_compatible", "OpenAI Compatible") },
  { value: "azure_openai", label: t("settings.model_providers.option.azure_openai", "Azure OpenAI") },
];
}

const OFFICIAL_PROVIDER_TYPES = new Set(["openai", "anthropic", "google"]);

function providerPayload(form: ProviderFormState): Entity {
  const providerType = form.provider_type.trim();
  return {
    api_key: form.api_key.trim(),
    default_model: form.default_model.trim(),
    name: form.name.trim(),
    provider_type: providerType,
    upstream_revision: form.upstream_revision.trim(),
    ...(!OFFICIAL_PROVIDER_TYPES.has(providerType)
      ? { base_url: form.base_url.trim() }
      : {}),
    ...(providerType === "azure_openai"
      ? {
          api_version: form.api_version.trim(),
          deployment_name: form.deployment_name.trim(),
        }
      : {}),
  };
}

function emptyProviderForm(providerType = "openai"): ProviderFormState {
  return {
    api_key: "",
    api_version: "",
    base_url: "",
    default_model: "",
    deployment_name: "",
    name: "",
    provider_type: providerType,
    upstream_revision: "",
  };
}

type ProviderDialogProps = {
  editingProviderId: string;
  form: ProviderFormState;
  message: string;
  nameRef: RefObject<HTMLInputElement | null>;
  onClose: () => void;
  onSubmit: (event: React.SyntheticEvent<HTMLFormElement>) => void;
  open: boolean;
  saving: boolean;
  selectedModelOptions: ModelOption[];
  setForm: React.Dispatch<React.SetStateAction<ProviderFormState>>;
  updateField: <K extends keyof ProviderFormState>(
    key: K,
    value: ProviderFormState[K],
  ) => void;
  usesAzureEndpoint: boolean;
  usesOfficialEndpoint: boolean;
};

function submitLabel(saving: boolean, editing: boolean, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (saving) return t("project_edit.security.saving", "Saving...");
  return editing ? t("react.settings.save_provider", "Save provider") : t("react.settings.create_provider", "Create provider");
}

function ProviderDialog({
  editingProviderId,
  form,
  message,
  nameRef,
  onClose,
  onSubmit,
  open,
  saving,
  selectedModelOptions,
  setForm,
  updateField,
  usesAzureEndpoint,
  usesOfficialEndpoint,
}: Readonly<ProviderDialogProps>) {
  const { t } = useI18n();
  return (
    <DsDialog
      busy={saving}
      closeLabel={t("react.settings.close_provider_dialog", "Close provider dialog")}
      description={
        editingProviderId
          ? t("react.settings.update_provider_routing_details", "Update provider routing details.")
          : t("react.settings.create_a_provider_profile_for_model_routing", "Create a provider profile for model routing.")
      }
      eyebrow={t("react.shared.model_providers", "Model providers")}
      footer={
        <>
          <button className="fs-button" disabled={saving} onClick={onClose} type="button">
            {t("common.cancel", "Cancel")} </button>
          <button className="fs-button primary" disabled={saving} form="provider-form" type="submit">
            {submitLabel(saving, Boolean(editingProviderId), t)}
          </button>
        </>
      }
      initialFocusRef={nameRef}
      onClose={onClose}
      open={open}
      title={editingProviderId ? t("react.settings.edit_provider", "Edit provider") : t("react.settings.add_provider", "Add provider")}
    >
      <form className="fs-dialog-form" id="provider-form" noValidate onSubmit={onSubmit}>
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="provider-name">{t("projects.grid.column.name", "Name")}</label>
          <input
            className="fs-input"
            id="provider-name"
            onChange={(event) => updateField("name", event.target.value)}
            ref={nameRef}
            value={form.name}
          />
        </div>
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="provider-type">{t("react.settings.provider_type", "Provider type")}</label>
          <DsSelect
            ariaLabel={t("react.settings.provider_type", "Provider type")}
            id="provider-type"
            onChange={(providerType) => {
              setForm((current) => ({
                ...current,
                api_version: providerType === "azure_openai" ? current.api_version : "",
                base_url: OFFICIAL_PROVIDER_TYPES.has(providerType) ? "" : current.base_url,
                deployment_name: providerType === "azure_openai" ? current.deployment_name : "",
                provider_type: providerType,
              }));
            }}
            options={getProviderTypes(t)}
            value={form.provider_type}
          />
        </div>
        <div className="fs-form-grid">
          {!usesOfficialEndpoint ? (
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="provider-base-url">{t("settings.model_providers.fields.base_url", "Base URL")}</label>
              <input
                className="fs-input"
                id="provider-base-url"
                onChange={(event) => updateField("base_url", event.target.value)}
                value={form.base_url}
              />
            </div>
          ) : null}
          {/* Alone in the row when the endpoint is fixed, so it takes the whole row
              instead of sitting at half width beside an empty cell. */}
          <div className={usesOfficialEndpoint ? "fs-field fs-settings-span" : "fs-field"}>
            <label className="fs-field-label" htmlFor="provider-default-model">{t("react.settings.default_model", "Default model")}</label>
            <input
              className="fs-input"
              id="provider-default-model"
              list={selectedModelOptions.length ? "provider-default-model-options" : undefined}
              onChange={(event) => updateField("default_model", event.target.value)}
              value={form.default_model}
            />
            {selectedModelOptions.length ? (
              <datalist id="provider-default-model-options">
                {selectedModelOptions.map((model) => (
                  <option key={model.modelId} value={model.modelId}>
                    {model.label}
                  </option>
                ))}
              </datalist>
            ) : null}
          </div>
        </div>
        {usesAzureEndpoint ? (
          <div className="fs-form-grid">
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="provider-api-version">{t("react.settings.api_version", "API version")}</label>
              <input
                className="fs-input"
                id="provider-api-version"
                onChange={(event) => updateField("api_version", event.target.value)}
                value={form.api_version}
              />
            </div>
            <div className="fs-field">
              <label className="fs-field-label" htmlFor="provider-deployment-name">{t("react.settings.deployment_name", "Deployment name")}</label>
              <input
                className="fs-input"
                id="provider-deployment-name"
                onChange={(event) => updateField("deployment_name", event.target.value)}
                value={form.deployment_name}
              />
            </div>
          </div>
        ) : null}
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="provider-upstream-revision">{t("react.settings.upstream_revision", "Upstream revision")}</label>
          <input
            className="fs-input"
            id="provider-upstream-revision"
            onChange={(event) => updateField("upstream_revision", event.target.value)}
            value={form.upstream_revision}
          />
          <span className="fs-field-helper">{t("react.settings.optional_immutable_revision_or_digest", "Optional immutable revision or digest.")}</span>
        </div>
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="provider-api-key">{t("react.settings.api_key", "API key")}</label>
          <input
            className="fs-input"
            id="provider-api-key"
            onChange={(event) => updateField("api_key", event.target.value)}
            type="password"
            value={form.api_key}
          />
        </div>
        {message ? <DsAlert title={message} tone="warning" /> : null}
      </form>
    </DsDialog>
  );
}

export function SettingsProvidersPage() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Entity | null>(null);
  const [editingProviderId, setEditingProviderId] = useState("");
  const [form, setForm] = useState<ProviderFormState>(() => emptyProviderForm());
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [filter, setFilter] = useState("");
  const [addOpen, setAddOpen] = useState(false);

  function buildProvidersGridConfig(): Record<string, unknown> {
    return {
      columns: [
        {
          key: "name",
          label: t("project_edit.ai_models.provider", "Provider"),
          type: "text",
          truncate: true,
          truncate_width: "18rem",
        },
        { key: "provider_type", label: t("trash.columns.type", "Type"), type: "badge", width: "170px" },
        {
          key: "base_url",
          label: t("settings.model_providers.fields.base_url", "Base URL"),
          type: "text",
          truncate: true,
          truncate_width: "22rem",
        },
        {
          key: "default_model",
          label: t("react.settings.default_model", "Default model"),
          type: "text",
          width: "170px",
        },
        {
          key: "verification_status",
          label: t("react.settings.verification", "Verification"),
          type: "badge",
          width: "140px",
        },
        {
          key: "verification_error_safe",
          label: t("react.settings.verification_error", "Verification error"),
          type: "text",
          width: "190px",
        },
      ],
      row_actions: [
        { action_id: "edit", label: t("projects.grid.action.edit", "Edit"), variant: "default" },
        { action_id: "verify", label: t("react.settings.verify", "Verify"), variant: "default" },
        { action_id: "delete", label: t("projects.grid.action.delete", "Delete"), variant: "danger" },
      ],
      id_field: "id",
      pagination: { page_size_default: 25 },
      empty_state: {
        title: t("react.settings.no_providers_found", "No providers found"),
        description: t("react.settings.add_a_provider_or_adjust_the_filter", "Add a provider or adjust the filter."),
      },
    };
  }

  const providersQuery = useQuery({
    queryKey: ["settings", "providers"],
    queryFn: fetchModelProvidersCollection,
    refetchInterval: 30_000,
  });
  const modelCatalogQuery = useQuery<{ families?: unknown }>({
    queryKey: ["settings", "model-catalog"],
    queryFn: fetchModelCatalogSettings,
  });

  const modelsByProviderType = useMemo<Record<string, ModelOption[]>>(() => {
    const catalog = modelCatalogQuery.data || {};
    const families = Array.isArray(catalog.families) ? catalog.families : [];
    return Object.fromEntries(
      families
        .filter(
          (family): family is Entity =>
            Boolean(family) && typeof family === "object",
        )
        .map((family) => [
          firstStringFromUnknownValue([family.family_id, ""]),
          normalizeModelOptions(family.models),
        ])
        .filter(([providerType]) => Boolean(providerType)),
    );
  }, [modelCatalogQuery.data]);
  const selectedModelOptions = modelsByProviderType[form.provider_type] || [];
  const usesOfficialEndpoint = OFFICIAL_PROVIDER_TYPES.has(form.provider_type);
  const usesAzureEndpoint = form.provider_type === "azure_openai";

  const providers = useMemo(
    () => (Array.isArray(providersQuery.data) ? providersQuery.data : []),
    [providersQuery.data],
  );

  const normalizedFilter = filter.trim().toLowerCase();
  const filteredProviders = useMemo(
    () =>
      providers.filter((provider) => {
        if (!normalizedFilter) {
          return true;
        }
        const text = [
          firstStringFromUnknownValue([
            provider.name,
            provider.display_name,
            "",
          ]),
          firstStringFromUnknownValue([provider.provider_type, ""]),
          firstStringFromUnknownValue([provider.base_url, ""]),
          firstStringFromUnknownValue([provider.id, ""]),
        ]
          .map((value) => stringFromUnknownValue(value))
          .join(" ")
          .toLowerCase();
        return text.includes(normalizedFilter);
      }),
    [normalizedFilter, providers],
  );
  const providerRows = useMemo(
    () =>
      filteredProviders.map((provider) => ({
        ...provider,
        id: firstStringFromUnknownValue([provider.id, ""]),
        name: firstStringFromUnknownValue([
          provider.name,
          provider.display_name,
          provider.id,
          t("project_edit.ai_models.provider", "Provider"),
        ]),
        provider_type: firstStringFromUnknownValue([
          provider.provider_type,
          "unknown",
        ]),
        base_url: firstStringFromUnknownValue([provider.base_url, t("monitoring.na", "n/a")]),
        default_model: firstStringFromUnknownValue([
          provider.default_model,
          t("monitoring.na", "n/a"),
        ]),
        verification_status: firstStringFromUnknownValue([
          provider.verification_status,
          "unverified",
        ]),
        verification_error_safe: provider.verification_error_safe
          ? providerErrorMessage(
              provider.verification_error_safe,
              t("react.extensions.verification_failed", "Verification failed."),
              t,
            )
          : "",
      })),
    [filteredProviders, t],
  );

  function updateField<K extends keyof ProviderFormState>(
    key: K,
    value: ProviderFormState[K],
  ) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function handleCreate(event: React.SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage("");
    if (!form.name.trim()) {
      setMessage(t("react.settings.provider_name_is_required", "Provider name is required."));
      return;
    }
    setSaving(true);
    try {
      const payload = providerPayload(form);
      const result = editingProviderId
        ? await updateModelProvider(editingProviderId, payload)
        : await createModelProvider(payload);
      const resultPayload =
        result && typeof result === "object" && !Array.isArray(result)
          ? (result as Entity)
          : {};
      const verificationStatus = firstStringFromUnknownValue([
        resultPayload.verification_status,
        "stale",
      ]);
      const verificationError = firstStringFromUnknownValue([
        resultPayload.verification_error_safe,
        "",
      ]);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["settings", "providers"] }),
        invalidateModelRoutingQueries(queryClient),
      ]);
      if (verificationStatus !== "passed") {
        const persistedProviderId = firstStringFromUnknownValue([
          resultPayload.id,
          "",
        ]);
        if (persistedProviderId) {
          setEditingProviderId(persistedProviderId);
        }
        const verificationDetail = verificationError
          ? `: ${providerErrorMessage(verificationError, t("react.extensions.verification_failed", "Verification failed."), t)}`
          : "";
        setMessage(
          verificationStatus === "failed"
            ? t("react.settings.provider_saved_but_model_verification_failed_value1", "Provider saved, but model verification failed{value1}.", { value1: String(verificationDetail) })
            : t("react.settings.provider_saved_but_model_verification_is_value1", "Provider saved, but model verification is {value1}.", { value1: String(verificationStatus) }),
        );
        return;
      }
      setMessage(
        editingProviderId
          ? t("react.settings.model_provider_updated", "Model provider updated.")
          : t("react.settings.model_provider_created", "Model provider created."),
      );
      setEditingProviderId("");
      setForm(emptyProviderForm(form.provider_type));
      setAddOpen(false);
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : t("react.settings.failed_to_create_provider", "Failed to create provider."),
      );
    } finally {
      setSaving(false);
    }
  }

  const handleDelete = useCallback(
    async (provider: Entity) => {
      const id = firstStringFromUnknownValue([provider.id, ""]).trim();
      if (!id) {
        setMessage(t("react.settings.provider_id_is_missing", "Provider ID is missing."));
        return;
      }
      setMessage("");
      setSaving(true);
      try {
        await deleteModelProvider(id);
        setMessage(t("react.settings.provider_value1_deleted", "Provider {value1} deleted.", { value1: String(id) }));
        await Promise.all([
          queryClient.invalidateQueries({
            queryKey: ["settings", "providers"],
          }),
          invalidateModelRoutingQueries(queryClient),
        ]);
      } catch (error) {
        setMessage(
          error instanceof Error ? error.message : t("react.settings.failed_to_delete_provider", "Failed to delete provider."),
        );
      } finally {
        setSaving(false);
        setPendingDelete(null);
      }
    },
    [queryClient, t],
  );

  const handleVerify = useCallback(
    async (provider: Entity) => {
      const id = firstStringFromUnknownValue([provider.id, ""]).trim();
      if (!id) {
        setMessage(t("react.settings.provider_id_is_missing", "Provider ID is missing."));
        return;
      }
      setMessage("");
      setSaving(true);
      try {
        const result = await reverifyModelProvider(id);
        const resultPayload =
          result && typeof result === "object" && !Array.isArray(result)
            ? (result as Entity)
            : {};
        const status = firstStringFromUnknownValue([
          resultPayload.verification_status,
          "stale",
        ]);
        const safeError = firstStringFromUnknownValue([
          resultPayload.verification_error_safe,
          "",
        ]);
        const verificationDetail = safeError
          ? `: ${providerErrorMessage(safeError, t("react.extensions.verification_failed", "Verification failed."), t)}`
          : "";
        setMessage(
          status === "passed"
            ? t("react.settings.model_provider_verified", "Model provider verified.")
            : t("react.settings.model_verification_value1_value2", "Model verification {value1}{value2}.", { value1: String(status), value2: String(verificationDetail) }),
        );
        await Promise.all([
          queryClient.invalidateQueries({
            queryKey: ["settings", "providers"],
          }),
          invalidateModelRoutingQueries(queryClient),
        ]);
      } catch (error) {
        setMessage(
          error instanceof Error ? error.message : t("react.settings.failed_to_verify_provider", "Failed to verify provider."),
        );
      } finally {
        setSaving(false);
      }
    },
    [queryClient, t],
  );

  function handleProviderAction(actionId: string, provider: Record<string, unknown>) {
    const providerId = firstStringFromUnknownValue([provider.id, ""]).trim();
    if (!providerId) {
      return;
    }
    if (actionId === "edit") {
      setEditingProviderId(providerId);
      setForm({
        api_key: "",
        api_version: firstStringFromUnknownValue([provider.api_version, ""]),
        base_url: stringFromUnknownValue(provider.base_url === "n/a" ? "" : provider.base_url || ""),
        default_model: stringFromUnknownValue(provider.default_model === "n/a" ? "" : provider.default_model || ""),
        deployment_name: firstStringFromUnknownValue([provider.deployment_name, ""]),
        name: firstStringFromUnknownValue([provider.name, ""]),
        provider_type: firstStringFromUnknownValue([provider.provider_type, "openai"]),
        upstream_revision: firstStringFromUnknownValue([provider.upstream_revision, ""]),
      });
      setAddOpen(true);
      return;
    }
    if (actionId === "delete") {
      setPendingDelete(provider);
      return;
    }
    if (actionId === "verify") {
      ignoreAsyncResult(handleVerify(provider));
    }
  }

  const providersGridConfig = buildProvidersGridConfig();

  const providerColumns = useMemo(
    () =>
      columnsFromGridConfig(providersGridConfig, (row) => {
        const providerId = firstStringFromUnknownValue([row.id, ""]).trim();
        return (
          <RowActionMenu
            label={t("react.settings.actions_for_value1", "Actions for {value1}", { value1: String(firstStringFromUnknownValue([row.name, providerId])) })}
            onOpenChange={(open) => setRowMenu(open ? providerId : null)}
            open={rowMenu === providerId}
          >
            {visibleRowActions(providersGridConfig, row).map((action) => (
              <div className="fs-row-menu-group" key={action.action_id}>
                <button
                  className={action.variant === "danger" ? "danger" : undefined}
                  onClick={() => {
                    setRowMenu(null);
                    handleProviderAction(action.action_id, row);
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
    // handleProviderAction reads the latest state on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [providersGridConfig, rowMenu, t],
  );


  const pendingDeleteLabel = pendingDelete
    ? firstStringFromUnknownValue([pendingDelete.name, pendingDelete.display_name, pendingDelete.id])
    : "";

  return (
    <>
      {providersQuery.isError ? (
        <DsAlert title={t("react.settings.providers_could_not_be_loaded", "Providers could not be loaded")} tone="danger">
          {providersQuery.error instanceof Error ? providersQuery.error.message : t("react.settings.failed_to_load_providers", "Failed to load providers.")}
        </DsAlert>
      ) : null}
      {message && !addOpen ? <DsAlert title={message} /> : null}

      <div className="fs-panel-group">
        <div className="fs-panel-heading">
          <div>
            <h2 className="fs-panel-title">{t("react.settings.configured_providers", "Configured providers")}</h2>
            <p>
              {t("react.settings.manage_model_providers_available_to_workflow_execution", "Manage model providers available to workflow execution.")} {providers.length} {t("react.settings.provider_records_discovered", "provider records discovered.")} </p>
          </div>
          <button
            className="fs-button primary"
            onClick={() => {
              setEditingProviderId("");
              setForm(emptyProviderForm());
              setMessage("");
              setAddOpen(true);
            }}
            type="button"
          >
            {t("react.settings.add_provider", "Add provider")} </button>
        </div>
        <article className="fs-card fs-table-card">
          <div className="fs-table-toolbar">
            <label className="fs-search">
              <Search aria-hidden="true" />
              <input
                aria-label={t("react.settings.filter_providers", "Filter providers")}
                onChange={(event) => setFilter(event.target.value)}
                placeholder={t("react.settings.filter_providers", "Filter providers")}
                type="search"
                value={filter}
              />
            </label>
          </div>
          <DsTable
            ariaLabel={t("react.settings.provider_settings", "Provider settings")}
            busy={providersQuery.isFetching}
            columns={providerColumns}
            rowKey={(row) => firstStringFromUnknownValue([row.id, ""])}
            rows={providerRows}
            state={
              !providersQuery.isLoading && !filteredProviders.length ? (
                <div className="fs-table-state">
                  <strong>{t("react.settings.no_providers_found_2", "No providers found.")}</strong>
                  <span>{t("react.settings.add_a_provider_or_adjust_the_filter", "Add a provider or adjust the filter.")}</span>
                </div>
              ) : null
            }
          />
        </article>
      </div>

      <ProviderDialog
        editingProviderId={editingProviderId}
        form={form}
        message={message}
        nameRef={nameRef}
        onClose={() => setAddOpen(false)}
        onSubmit={(event) => ignoreAsyncResult(handleCreate(event))}
        open={addOpen}
        saving={saving}
        selectedModelOptions={selectedModelOptions}
        setForm={setForm}
        updateField={updateField}
        usesAzureEndpoint={usesAzureEndpoint}
        usesOfficialEndpoint={usesOfficialEndpoint}
      />

      <DsDialog
        busy={saving}
        closeLabel={t("react.settings.close_delete_confirmation", "Close delete confirmation")}
        description={t("react.settings.delete_provider_value1_routes_that_use_it_stop_resolving", "Delete provider {value1}? Routes that use it stop resolving.", { value1: String(pendingDeleteLabel) })}
        eyebrow={t("react.shared.model_providers", "Model providers")}
        footer={
          <>
            <button className="fs-button" disabled={saving} onClick={() => setPendingDelete(null)} type="button">
              {t("common.cancel", "Cancel")} </button>
            <button
              className="fs-button danger"
              disabled={saving}
              onClick={() => pendingDelete && ignoreAsyncResult(handleDelete(pendingDelete))}
              type="button"
            >
              {saving ? t("react.settings.deleting", "Deleting...") : t("react.settings.delete_provider", "Delete provider")}
            </button>
          </>
        }
        onClose={() => setPendingDelete(null)}
        open={Boolean(pendingDelete)}
        title={t("react.settings.delete_provider", "Delete provider")}
        variant="confirm"
      />
    </>
  );
}
