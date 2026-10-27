import { useI18n } from "@/lib/i18n/useI18n";
import { DsAlert, DsSelect } from "@/components/shared/design-system";

import { buildAgentConfigPayload, compatibleModelOptionsForProvider } from "./agent-detail-helpers";

import { providerOptionLabel } from "@/lib/utils/provider-options";
import { ignoreAsyncResult } from "@/lib/utils/async";

import { renderWhen } from "./agent-detail-page-helpers";
import { type useAgentDetailState } from "./agent-detail-state";

type AgentConfigPanelProps = Pick<
  ReturnType<typeof useAgentDetailState>,
  "setDraft"
  | "draft"
  | "providerModels"
  | "allModels"
  | "providerOptions"
  | "providerLabels"
  | "availableAgentModels"
  | "agentRouteUnavailable"
  | "modelCatalogLoading"
  | "modelCatalogUnavailable"
  | "modelOptionsQuery"
  | "facts"
  | "saving"
  | "saveEntityPatch"
  | "agentConfig"
  | "draftSeed"
  | "setSaveStatus"
>;

export function AgentConfigPanel({
  setDraft,
  draft,
  providerModels,
  allModels,
  providerOptions,
  providerLabels,
  availableAgentModels,
  agentRouteUnavailable,
  modelCatalogLoading,
  modelCatalogUnavailable,
  modelOptionsQuery,
  facts,
  saving,
  saveEntityPatch,
  agentConfig,
  draftSeed,
  setSaveStatus,
}: Readonly<AgentConfigPanelProps>) {
  const { t } = useI18n();
  return (
    <div className="fs-panel-group">
      <div className="fs-panel-heading">
        <div>
          <h2 className="fs-panel-title">{t("react.registry.agent_config", "Agent Config")}</h2>
        </div>
      </div>
      <div className="fs-field full">
        <label className="fs-field-label" htmlFor="agent-display-name">
          {t("users.modal.display_name", "Display Name")}</label>
        <input
          className="fs-input"
          id="agent-display-name"
          onChange={(event) =>
            setDraft((current) => ({ ...current, displayName: event.target.value }))
          }
          placeholder={t("react.registry.display_name", "Display name")}
          type="text"
          value={draft.displayName}
        />
      </div>
      <div className="fs-field full">
        <label className="fs-field-label" htmlFor="agent-description">
          {t("registry.workflows.simulation.description", "Description")}</label>
        <textarea
          className="fs-textarea"
          id="agent-description"
          onChange={(event) =>
            setDraft((current) => ({ ...current, description: event.target.value }))
          }
          placeholder={t("registry.workflows.simulation.description", "Description")}
          value={draft.description}
        />
      </div>
      <div className="fs-form-grid">
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="agent-category">
            {t("trash.columns.category", "Category")}</label>
          <input
            className="fs-input"
            id="agent-category"
            onChange={(event) =>
              setDraft((current) => ({ ...current, category: event.target.value }))
            }
            placeholder={t("trash.columns.category", "Category")}
            value={draft.category}
          />
        </div>
        <div className="fs-field">
          <label className="fs-field-label" htmlFor="agent-type">
            {t("trash.columns.type", "Type")}</label>
          <input
            className="fs-input"
            id="agent-type"
            onChange={(event) =>
              setDraft((current) => ({ ...current, agentType: event.target.value }))
            }
            placeholder={t("react.registry.agent_type", "agent type")}
            value={draft.agentType}
          />
        </div>
        <div className="fs-field">
          <span className="fs-field-label">{t("extensions.runtime.provider", "Provider")}</span>
          {/* `Project default` is a real selectable option, not a
              placeholder: a placeholder cannot be chosen again once the
              user has picked a provider. */}
          <DsSelect
            ariaLabel={t("extensions.runtime.provider", "Provider")}
            onChange={(nextProvider) => {
              const compatibleModelOptions = compatibleModelOptionsForProvider(
                providerModels,
                allModels,
                nextProvider,
              );
              setDraft((current) => ({
                ...current,
                provider: nextProvider,
                model:
                  !nextProvider ||
                  (current.model
                    && !compatibleModelOptions.some(
                      (option) => option.modelId === current.model,
                    ))
                    ? ""
                    : current.model,
              }));
            }}
            options={[
              { label: t("react.registry.project_default", "Project default"), value: "" },
              ...providerOptions.map((provider) => ({
                label:
                  provider === draft.provider
                  && !providerModels[provider]
                    ? t("react.registry.value0_unavailable", "{value0} (unavailable)", { value0: providerOptionLabel(providerLabels, provider) })
                    : providerOptionLabel(providerLabels, provider),
                value: provider,
              })),
            ]}
            value={draft.provider}
          />
        </div>
        <div className="fs-field">
          <span className="fs-field-label">{t("registry.agents.field.model", "Model")}</span>
          <DsSelect
            ariaLabel={t("registry.agents.field.model", "Model")}
            emptyLabel={t("react.registry.no_model_options_available", "No model options available")}
            onChange={(value) =>
              setDraft((current) => ({ ...current, model: value }))
            }
            options={[
              { label: t("react.registry.project_default", "Project default"), value: "" },
              ...availableAgentModels.map((modelOption) => ({
                label:
                  agentRouteUnavailable && modelOption.modelId === draft.model
                    ? t("react.registry.value0_unavailable", "{value0} (unavailable)", { value0: modelOption.label })
                    : modelOption.label,
                value: modelOption.modelId,
              })),
            ]}
            value={draft.model}
          />
        </div>
      </div>
      {renderWhen(modelCatalogLoading, (
        <div className="fs-table-state">
          <strong>{t("react.registry.loading_model_options", "Loading model options…")}</strong>
        </div>
      ))}
      {renderWhen(modelCatalogUnavailable, (
        <DsAlert title={t("react.registry.could_not_load_model_options", "Could not load model options.")} tone="danger">
          <button
            className="fs-button"
            onClick={() => ignoreAsyncResult(modelOptionsQuery.refetch())}
            type="button"
          >
            {t("react.registry.retry_model_options", "Retry model options")}</button>
        </DsAlert>
      ))}
      {renderWhen(agentRouteUnavailable && !modelCatalogUnavailable, (
        <DsAlert
          title={t("react.registry.this_agent_s_model_isn_t_verified", "This agent's model isn't verified")}
          tone="warning"
        >
          {t("react.registry.the_agent_can_t_use_it_until_it_is_verify_it_on_the_provider_s_extension_page_or_select_an_available_provider_", "The agent can't use it until it is. Verify it on the provider's extension page, or select an available provider and model before saving.")}</DsAlert>
      ))}
      <dl className="fs-kv is-bare">
        {facts.map(([label, value]) => (
          <div className="fs-kv-row" key={label}>
            <dt>{label}</dt>
            <dd className={value === t("react.registry.not_set", "Not set") ? "is-empty" : undefined}>{value}</dd>
          </div>
        ))}
      </dl>
      <div className="fs-badge-row">
        <button
          className="fs-button primary"
          disabled={
            saving
            || agentRouteUnavailable
            || modelCatalogUnavailable
            || modelCatalogLoading
          }
          onClick={() => {
            ignoreAsyncResult(saveEntityPatch({
              display_name: draft.displayName.trim(),
              description: draft.description.trim(),
              category: draft.category.trim(),
              agent_type_id: draft.agentType.trim(),
              config: buildAgentConfigPayload(
                agentConfig,
                draft.provider,
                draft.model,
              ),
            }));
          }}
          type="button"
        >
          {t("react.registry.save_config", "Save config")}</button>
        <button
          className="fs-button"
          onClick={() => {
            setDraft(draftSeed);
            setSaveStatus("");
          }}
          type="button"
        >
          {t("trash.reset", "Reset")}</button>
      </div>
    </div>
  );
}
