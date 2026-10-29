import { ChevronRight, RefreshCw } from "lucide-react";

import { CatalogIcon } from "@/components/shared/catalog-browse";
import { extensionIconUrl } from "@/pages/extensions/extension-icon";
import { mcpCatalogIconUrl } from "@/pages/registry/mcp/api";
import { connectorToolCategory } from "./detail-tool-categories";
import { DsSelect, RequiredLabel } from "@/components/shared/design-system";
import { IconButton } from "@/components/ui/icon-button";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { ignoreAsyncResult } from "@/lib/utils/async";
import {
  connectorDefaultOutputMappingForOperation,
  listFromUnknown,
  objectOrEmpty,
  textOrEmpty,
  workflowVariableSuggestionPanelId,
} from "@/pages/workflows/detail/detail-helpers";
import { useId, type ReactNode } from "react";
import { HttpMapField, HttpStatusesField, HttpValueField } from "./detail-http-request-value-fields";
import { httpReadinessDisplay, type HttpField } from "./detail-http-request-fields";
import { useHttpRequestIntent, type HttpFieldFeedback } from "./detail-http-request-intent";
import { numericDraftFromInput, numericInputValue, stepNumericFieldErrors } from "./detail-step-numeric-fields";

type WorkflowStepSmartSetupContext = Record<string, any>;

/** The Connection select's last option: opens the extension's setup page instead of binding. */
const ADD_CONNECTION_OPTION = "__add_connection__";

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

function connectorSourcePresentation(connectorSource: string, connectorId: string, t: I18nContextValue["t"] = fallbackContext.t) {
  const isStandardToolAction = connectorSource === "standard_tool_action"
    || connectorId.startsWith("connector.std.");
  const isToolAction = connectorSource === "mcp_connector"
    || isStandardToolAction
    || (connectorSource === "registry_tool" && connectorId.startsWith("connector."));
  return {
    connectorSelectLabel: isToolAction ? t("react.workflows.display_tool", "Tool") : t("react.workflows.display_action_source", "Action source"),
    isToolAction,
  };
}

function staleOperationNotice(availability: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const messages: Record<string, string> = {
    active: t("react.workflows.display_saved_action_unavailable", "Saved action is no longer available for this installed release."),
    disabled: t("react.workflows.display_saved_action_extension_disabled", "This extension is disabled—ask an admin to enable it before this saved step can run."),
    removed: t("react.workflows.display_saved_action_extension_removed", "This extension was removed—reinstall it or replace this saved step before it can run."),
  };
  return messages[availability]
    ?? t("react.workflows.could_not_verify_this_extension_right_now_retry_or_contact_an_admin", "Could not verify this extension right now; retry or contact an admin.");
}

function staleConnectionNotice({
  connectionNeedsProjectLink,
  connectionTypeLabel,
  connectionUnavailable,
  missingConnection,
}: {
  connectionNeedsProjectLink: boolean;
  connectionTypeLabel: string;
  connectionUnavailable: boolean;
  missingConnection: boolean;
}, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (connectionNeedsProjectLink) {
    return t("react.workflows.display_link_project_choose_connection", "Link this workflow to a project to choose a {type} for this action.", { type: connectionTypeLabel });
  }
  if (connectionUnavailable) {
    return t("react.workflows.display_connect_compatible_connection", "Connect a compatible {type} before using this action.", { type: connectionTypeLabel });
  }
  if (missingConnection) {
    return t("react.workflows.display_choose_action_connection", "Choose a {type} for this action.", { type: connectionTypeLabel });
  }
  // The saved one was deleted or disabled (often by recreating it), not
  // removed by a release change: say so, and point at the replacement.
  return t("react.workflows.display_saved_connection_removed", "The saved {type} was removed. Choose another one for this action.", { type: connectionTypeLabel });
}

/** An action from an extension shows that extension's logo beside its name. */
function connectorOptionIcon(connectorRows: unknown, connectorId: string): ReactNode {
  const row = listFromUnknown(connectorRows).find((item) => textOrEmpty(item.connector_id) === connectorId);
  if (!row) return undefined;
  const category = connectorToolCategory(row);
  if (category === "extension_tools") {
    const extensionId = textOrEmpty(row.extension_id) || connectorId.replace(/^extension\./, "");
    return <CatalogIcon size="inline" src={extensionIconUrl(extensionId)} />;
  }
  if (category === "mcp_tools") {
    const itemId = textOrEmpty(row.catalog_item_id);
    return <CatalogIcon size="inline" src={itemId ? mcpCatalogIconUrl(itemId) : undefined} />;
  }
  return undefined;
}

export function ConnectorStepSetup({ ctx }: Readonly<{ ctx: WorkflowStepSmartSetupContext }>) {
  const { t } = useI18n();
  const {
    connectorConnectionRequired,
    connectorConnectionSetupHref,
    connectorConnectionsRefreshing,
    refreshConnectorConnections,
    connectorConnectionOptions,
    extensionInstallAvailability,
    connectorConnectionRows,
    extensionConnectionReadinessMessage,
    selectedConnectionTypeLabel,
    connectorOperationOptions,
    connectorOperationRows,
    connectorOptions,
    connectorRows,
    connectorOutputRows,
    connectorParameterRows,
    renderConnectorInputMappingEditor,
    renderConnectorOutputMappingEditor,
    renderContractChips,
    replaceStepKindConfigField,
    selectedConnectorId,
    selectedConnectorOperation,
    selectedConnectorOperationId,
    selectedConnectorRow,
    staleConnectorConnectionId,
    missingConnectorConnection,
    connectorConnectionUnavailable,
    connectorConnectionNeedsProjectLink,
    staleConnectorOperationId,
    stepKindConfig,
    stepToolCategory,
    updateStepKindConfig,
  } = ctx;

      const connectorSource = textOrEmpty(selectedConnectorRow.source) || (stepToolCategory === "mcp_tools" ? "mcp_connector" : stepToolCategory === "connector" ? "standard_tool_action" : "extension");
      const {
        connectorSelectLabel,
        isToolAction,
      } = connectorSourcePresentation(connectorSource, selectedConnectorId, t);
      const selectedToolUrl = textOrEmpty(selectedConnectorRow.tool_url);
      const selectedOperationDescription = textOrEmpty(
        selectedConnectorOperation.description,
      );
      const selectedConnectionBinding = stepKindConfig.connection_binding && typeof stepKindConfig.connection_binding === "object"
        ? stepKindConfig.connection_binding
        : {};
      const selectedConnectionId = textOrEmpty(selectedConnectionBinding.connection_id);
      const hasConnectorConnections =
        listFromUnknown(connectorConnectionRows).length > 0 ||
        Boolean(selectedConnectionId);
      // AC17-R65: the Connection select (or its auto-select) owns
      // connection_binding.connection_id, and the host fills the rest. An author never edits
      // the binding as free key/value text: they could only guess at system keys. A binding
      // that carries more than connection_id came from an imported definition; it is shown
      // read-only with a way to clear it.
      const customBindingEntries = Object.entries(objectOrEmpty(selectedConnectionBinding)).filter(
        ([key]) => key !== "connection_id",
      );
      const staleOperationMessage = staleOperationNotice(extensionInstallAvailability, t);
      // One compatible connection in the project: offer it instead of making
      // the author find the select below and pick the only choice.
      const selectableConnectionOptions = connectorConnectionOptions.filter(
        (option: { disabled?: boolean }) => !option.disabled,
      );
      const soleConnectionOption: { label: string; value: string } | null =
        selectableConnectionOptions.length === 1 ? selectableConnectionOptions[0] : null;
      const connectionFieldShown = hasConnectorConnections || Boolean(connectorConnectionRequired && connectorConnectionSetupHref);
      // A required connection not chosen yet (or a saved one now gone) with a connection to pick: the field says it.
      const connectionChoosableHere = connectionFieldShown && !connectorConnectionNeedsProjectLink && !connectorConnectionUnavailable &&
        (Boolean(missingConnectorConnection) || (Boolean(staleConnectorConnectionId) && selectableConnectionOptions.length > 0));
      const connectionNeedsChoice = Boolean(staleConnectorConnectionId || missingConnectorConnection);
      const connectionSourceName = connectorOptions.find((option: { label: string; value: string }) => option.value === selectedConnectorId)?.label
        || selectedConnectorId;
      const connectionHelpId = `${selectedConnectorId || "connector"}-connection-help`;
      const staleConnectionMessage = staleConnectionNotice({
        connectionNeedsProjectLink: connectorConnectionNeedsProjectLink,
        connectionTypeLabel: selectedConnectionTypeLabel,
        connectionUnavailable: connectorConnectionUnavailable,
        missingConnection: missingConnectorConnection,
      }, t);
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.tool_action_section", "Tool action")}</p>
          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              {connectorSelectLabel}
              <DsSelect
                ariaLabel={connectorSelectLabel}
                searchable
                className="mt-1 fs-select-constrained"
                onChange={(nextValue) => {
                  const connectorId = nextValue;
                  const connector = listFromUnknown(connectorRows).find(
                    (row) => textOrEmpty(row.connector_id) === connectorId,
                  );
                  updateStepKindConfig("connector", {
                    connector_id: connectorId,
                    operation_id: "",
                    function_name: "",
                    action_id: "",
                    extension_id: textOrEmpty(connector?.extension_id),
                    connector_install_id: textOrEmpty(
                      connector?.connector_install_id || connector?.extension_install_id,
                    ),
                    input_mapping: {},
                    output_mapping: {},
                    connection_binding: {},
                  });
                }}
                value={selectedConnectorId}
                options={[({ value: "", label: isToolAction ? t("react.workflows.choose_tool", "Choose tool") : t("react.workflows.choose_action_source", "Choose action source") }), ...connectorOptions.map((option: { disabled?: boolean; label: string; value: string }) => (
                  ({ value: option.value, label: option.label, disabled: option.disabled, icon: connectorOptionIcon(connectorRows, option.value) })
                ))]}
              />
            </label>
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("security_overview.columns.action", "Action")}</span>
              <DsSelect
                ariaLabel={t("security_overview.columns.action", "Action")}
                searchable
                className="mt-1 fs-select-constrained"
                onChange={(nextValue) => {
                  const operationId = nextValue;
                  const operation = listFromUnknown(connectorOperationRows).find(
                    (row) =>
                      textOrEmpty(
                        row.operation_id || row.id || row.key || row.name,
                      ) === operationId,
                  );
                  updateStepKindConfig("connector", {
                    connector_id:
                      textOrEmpty(operation?.runtime_connector_id) || selectedConnectorId,
                    operation_id: operationId,
                    function_name: "",
                    action_id: "",
                    input_mapping: {},
                    output_mapping:
                      connectorDefaultOutputMappingForOperation(operation),
                  });
                }}
                value={selectedConnectorOperationId}
                options={[({ value: "", label: t("react.workflows.choose_action", "Choose action") }), ...connectorOperationOptions.map((option: { disabled?: boolean; label: string; value: string }) => (
                  ({ value: option.value, label: option.label, disabled: option.disabled })
                ))]}
              />
            </label>
          </div>
          {renderWhen(Boolean(staleConnectorOperationId), (
            <p className="rounded border border-[var(--fs-status-danger-border)] bg-[var(--fs-status-danger-bg)] px-2 py-1 text-xs text-[var(--fs-status-danger)]">
              {staleOperationMessage}
            </p>
          ))}
          {/* A missing connection is said under the Connection field, where it is chosen;
              only a saved connection that is gone, or one that cannot be chosen here, is an alert. */}
          {renderWhen(Boolean(staleConnectorConnectionId || missingConnectorConnection) && !connectionChoosableHere, (
            <p className="rounded border border-[var(--fs-status-danger-border)] bg-[var(--fs-status-danger-bg)] px-2 py-1 text-xs text-[var(--fs-status-danger)]">
              {staleConnectionMessage}
            </p>
          ))}
          {renderWhen(Boolean(extensionConnectionReadinessMessage), (
            <p
              className="rounded border border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] px-2 py-1 text-xs text-[var(--fs-status-attention-ink)]"
              role="alert"
            >
              {extensionConnectionReadinessMessage}
            </p>
          ))}
          {connectorConnectionRequired && connectorConnectionSetupHref &&
            ((Boolean(staleConnectorConnectionId || missingConnectorConnection) && selectableConnectionOptions.length === 0) || extensionConnectionReadinessMessage) ? (
            <p className="rounded border border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] px-3 py-2 text-xs text-[var(--fs-status-attention-ink)]">
              {t("react.workflows.this_action_needs_a_compatible_connection_in_this_project_create_or_repair_it_on_the_extension_s_connection_pa", "This action needs a compatible connection in this project. Create or repair it on the extension's connection page; its setup and authorization are managed by the extension. Your workflow draft stays open here.")}</p>
          ) : null}
          {renderWhen(Boolean(selectedConnectorId), (
            // Reference only: the fields below carry the same contract, so it starts closed.
            <details className="fs-settings-section">
              <summary>
                <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
                {t("react.workflows.action_details", "Action details")}</summary>
              <div className="fs-settings-section-body space-y-3">
                {renderWhen(Boolean(selectedOperationDescription), (
                  <p className="text-xs text-[var(--fs-text-secondary)]">
                    {selectedOperationDescription}
                  </p>
                ))}
                {renderWhen(Boolean(selectedToolUrl), (
                  <a
                    className="inline-block rounded border border-[var(--fs-border-selected)] bg-[var(--fs-action-primary-wash)] px-2 py-1 text-[var(--fs-text-primary)] hover:border-[var(--fs-border-selected)]"
                    href={selectedToolUrl}
                  >
                    {t("react.workflows.open_tool", "Open tool")}</a>
                ))}
                {renderContractChips(t("react.workflows.display_action_inputs", "Action inputs"), connectorParameterRows)}
                {renderContractChips(t("react.workflows.display_action_returns", "Action returns"), connectorOutputRows)}
              </div>
            </details>
          ))}
          {renderWhen(connectionFieldShown, (
            // One control: pick a connection, or add one in the extension (new tab) and refresh here.
            <div className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.connection", "Connection")}</span>
              <div className="mt-1 flex items-center gap-2">
                <DsSelect
                  ariaLabel={t("react.workflows.connection", "Connection")}
                  className="min-w-0 flex-1"
                  describedBy={connectionHelpId}
                  invalid={connectionNeedsChoice && connectionChoosableHere}
                  onChange={(nextValue) => {
                    if (nextValue === ADD_CONNECTION_OPTION) {
                      window.open(connectorConnectionSetupHref, "_blank", "noopener,noreferrer");
                      return;
                    }
                    replaceStepKindConfigField(
                      "connector",
                      "connection_binding",
                      nextValue
                        ? {
                            ...selectedConnectionBinding,
                            connection_id: nextValue,
                          }
                        : {},
                    );
                  }}
                  value={selectedConnectionId}
                  options={[
                    ({ value: "", label: connectorConnectionRequired ? t("react.workflows.choose_connection", "Choose connection") : t("react.workflows.auto_select_connection", "Auto-select connection") }),
                    ...connectorConnectionOptions.map((option: { disabled?: boolean; label: string; value: string }) => (
                      ({ value: option.value, label: option.label, disabled: option.disabled })
                    )),
                    ...(connectorConnectionSetupHref ? [{ value: ADD_CONNECTION_OPTION, label: t("react.workflows.add_new_connection", "Add new connection…") }] : []),
                  ]}
                />
                <IconButton
                  aria-busy={connectorConnectionsRefreshing}
                  icon={RefreshCw}
                  label={t("react.workflows.refresh_connections", "Refresh connections")}
                  onClick={() => ignoreAsyncResult(refreshConnectorConnections())}
                />
              </div>
              <p className="fs-field-helper mt-1" id={connectionHelpId}>
                {staleConnectorConnectionId && connectionChoosableHere
                  ? staleConnectionMessage
                  : t("react.workflows.connection_help_for_source", "Choose a {source} connection for this action.", { source: connectionSourceName })}
              </p>
              {connectionNeedsChoice && soleConnectionOption ? (
                <button
                  className="fs-button sm secondary mt-2"
                  onClick={() =>
                    replaceStepKindConfigField("connector", "connection_binding", {
                      ...selectedConnectionBinding,
                      connection_id: soleConnectionOption.value,
                    })
                  }
                  type="button"
                >
                  {t("react.workflows.use", "Use")}{" "}{soleConnectionOption.label}
                </button>
              ) : null}
            </div>
          ))}
          {renderConnectorInputMappingEditor()}
          {renderConnectorOutputMappingEditor()}
          {renderWhen(customBindingEntries.length > 0, (
            <div
              className="space-y-2 rounded border border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] px-3 py-2 text-xs text-[var(--fs-status-attention-ink)]"
              data-testid="connector-imported-binding"
            >
              <p>{t("react.workflows.imported_connection_binding_notice", "These connection settings came from an imported definition and cannot be edited here. The Connection field chooses the connection.")}</p>
              <ul className="space-y-0.5 font-mono">
                {customBindingEntries.map(([key, value]) => (
                  <li key={key}>{key}: {typeof value === "string" ? value : JSON.stringify(value)}</li>
                ))}
              </ul>
              <button
                className="fs-button sm secondary"
                onClick={() =>
                  replaceStepKindConfigField(
                    "connector",
                    "connection_binding",
                    selectedConnectionId ? { connection_id: selectedConnectionId } : {},
                  )}
                type="button"
              >
                {t("react.workflows.clear_imported_connection_binding", "Clear connection settings")}
              </button>
            </div>
          ))}
        </div>
      );
    }

export function DatasetQueryStepSetup({ ctx, inputClass }: Readonly<{ ctx: WorkflowStepSmartSetupContext; inputClass: string }>) {
  const { t } = useI18n();
  const numericErrorId = useId();
  const {
    datasetQueryTemplateOptions,
    datasetQueryTemplatesQuery,
    datasetResultTargetListId,
    renderWorkflowVariableInput,
    replaceStepKindConfigField,
    selectedDatasetQueryTemplate,
    stepKindConfig,
  } = ctx;
  const errors = stepNumericFieldErrors("dataset_query", stepKindConfig, t);

      const parameterSchema = objectOrEmpty(
        selectedDatasetQueryTemplate.params_schema_jsonb ||
          selectedDatasetQueryTemplate.param_schema_jsonb ||
          selectedDatasetQueryTemplate.params_schema,
      );
      const parameterProperties = objectOrEmpty(parameterSchema.properties);
      const requiredParameters = new Set(
        (Array.isArray(parameterSchema.required) ? parameterSchema.required : []).map(
          (name) => textOrEmpty(name),
        ),
      );
      const parameterMapping = objectOrEmpty(stepKindConfig.params);
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.dataset_query", "Dataset query")}</p>
          <label className="block text-xs text-[var(--fs-text-secondary)]">
            <RequiredLabel>{t("react.workflows.saved_query", "Saved query")}</RequiredLabel>
            <DsSelect
              ariaLabel={t("react.workflows.saved_query", "Saved query")}
              className="mt-1"
              required
              onChange={(nextValue) =>
                replaceStepKindConfigField(
                  "dataset_query",
                  "query_template_id",
                  nextValue,
                )}
              value={textOrEmpty(stepKindConfig.query_template_id)}
              options={[({ value: "", label: t("react.workflows.choose_saved_query", "Choose saved query") }), ...datasetQueryTemplateOptions.map((option: { label: string; value: string }) => (
                ({ value: option.value, label: option.label })
              ))]}
            />
          </label>
          {datasetQueryTemplatesQuery.isLoading ? (
            <p className="text-xs text-[var(--fs-text-tertiary)]">{t("react.workflows.loading_saved_query_catalog", "Loading saved query catalog…")}</p>
          ) : null}
          {datasetQueryTemplatesQuery.isError ? (
            <p className="rounded-lg border border-[var(--fs-status-danger-border)] bg-[var(--fs-status-danger-bg)] px-3 py-2 text-xs text-[var(--fs-status-danger)]">
              {t("react.workflows.saved_query_catalog_could_not_be_loaded_reopen_this_step_to_retry", "Saved query catalog could not be loaded. Reopen this step to retry.")}</p>
          ) : null}
          {!datasetQueryTemplateOptions.length &&
          !datasetQueryTemplatesQuery.isLoading &&
          !datasetQueryTemplatesQuery.isError ? (
            <p className="rounded-lg border border-[var(--fs-status-attention-border)] bg-[var(--fs-status-attention-wash)] px-3 py-2 text-xs text-[var(--fs-status-attention-ink)]">
              {t("react.workflows.no_saved_queries_are_available_create_one_from_registry_datasets_before_saving_this_step", "No saved queries are available. Create one from Registry → Datasets before saving this step.")}</p>
          ) : null}
          {Object.keys(parameterProperties).length ? (
            <div className="rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
              <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-secondary)]">
                {t("react.workflows.query_parameter_mapping", "Query parameter mapping")}</p>
              <p className="mt-1 text-[11px] text-[var(--fs-text-tertiary)]">
                {t("react.workflows.use", "Use")}{" "}<span className="font-mono">literal:value</span>{t("react.workflows.a_workflow_input_such_as", ", a workflow input such as")}{" "}
                <span className="font-mono">state.inputs.name</span>{t("react.workflows.or_a_prior_step_state_output", ", or a prior-step state output.")}</p>
              <div className="mt-2 space-y-2">
                {Object.entries(parameterProperties).map(([name, definition]) => {
                  const parameter = objectOrEmpty(definition);
                  return (
                    <label
                      className="block rounded-lg border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-2 text-xs text-[var(--fs-text-secondary)]"
                      key={name}
                    >
                      <span className="flex items-center gap-2">
                        <span className="font-mono text-[var(--fs-text-primary)]">{name}</span>
                        <span>{textOrEmpty(parameter.type) || "value"}</span>
                        {requiredParameters.has(name) ? (
                          <span className="rounded border border-[var(--fs-status-attention-border)] px-1.5 py-0.5 text-[10px] uppercase text-[var(--fs-status-attention-ink)]">
                            {t("registry.workflow_detail.outputs.required_badge", "required")}</span>
                        ) : null}
                      </span>
                      {renderWorkflowVariableInput({
                        ariaLabel: t("react.workflows.query_parameter_value0", "Query parameter {value0}", { value0: name }),
                        className:
                          "mt-2 w-full rounded border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] px-2 py-1.5 text-sm text-[var(--fs-text-primary)]",
                        onChange: (nextValue: string) =>
                          replaceStepKindConfigField("dataset_query", "params", {
                            ...parameterMapping,
                            [name]: nextValue,
                          }),
                        panelId: `dataset-query-param-${name}`,
                        placeholder: `state.inputs.${name}`,
                        value: textOrEmpty(parameterMapping[name]),
                      })}
                    </label>
                  );
                })}
              </div>
            </div>
          ) : null}
          <div className="grid gap-2 sm:grid-cols-3">
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.result_target_variant_6c946b46", "Result target")}</span>
              {renderWorkflowVariableInput({
                ariaLabel: t("react.workflows.result_target_variant_6c946b46", "Result target"),
                className: inputClass,
                onChange: (nextValue: string) =>
                  replaceStepKindConfigField(
                    "dataset_query",
                    "result_target",
                    nextValue,
                  ),
                panelId: datasetResultTargetListId,
                placeholder: "state.dataset_query_result",
                value: textOrEmpty(stepKindConfig.result_target),
              })}
            </label>
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.row_limit", "Row limit")}</span>
              <input
                className={inputClass}
                onChange={(event) =>
                  replaceStepKindConfigField(
                    "dataset_query",
                    "row_limit",
                    numericDraftFromInput(event.target.value),
                  )
                }
                type="text"
                inputMode="numeric"
                value={numericInputValue(stepKindConfig.row_limit)}
                data-step-numeric-field="row_limit"
                aria-label={t("react.workflows.row_limit", "Row limit")}
                aria-invalid={errors.row_limit ? true : undefined}
                aria-describedby={errors.row_limit ? `${numericErrorId}-rows` : undefined}
              />
              {errors.row_limit ? <span className="fs-field-helper error block" id={`${numericErrorId}-rows`} role="alert">{errors.row_limit}</span> : null}
            </label>
            <label className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.timeout_seconds", "Timeout seconds")}</span>
              <input
                className={inputClass}
                onChange={(event) =>
                  replaceStepKindConfigField(
                    "dataset_query",
                    "timeout_seconds",
                    numericDraftFromInput(event.target.value),
                  )
                }
                type="text"
                inputMode="decimal"
                value={numericInputValue(stepKindConfig.timeout_seconds)}
              />
            </label>
          </div>
        </div>
      );
    }

export function HttpRequestStepSetup({ ctx, inputClass }: Readonly<{ ctx: WorkflowStepSmartSetupContext; inputClass: string }>) {
  const { t } = useI18n();
  const numericErrorId = useId();
  const {
    httpResponseTargetListId,
    renderWorkflowVariableInput,
    replaceStepKindConfigField: storeHttpField,
    stepKindConfig,
  } = ctx;
  const standaloneIntent = useHttpRequestIntent("standalone-http", ctx.onStepInputValidationChange || (() => undefined));
  const httpIntent = ctx.httpIntent || standaloneIntent;
  const readiness = httpReadinessDisplay(stepKindConfig, t);
  const server = Object.fromEntries(((ctx.httpFieldFeedback || []) as HttpFieldFeedback[])
    .filter((entry) => httpIntent.isCurrent(entry.token, entry.field))
    .map((entry) => [entry.field, entry.message]));
  const messages = { ...readiness, ...server };
  const errors = { ...messages, ...stepNumericFieldErrors("http_request", stepKindConfig, t) };
  const messageId = (field: HttpField) => `${numericErrorId}-${field}`;
  const renderMessage = (field: HttpField) => messages[field] ? <span id={messageId(field)} className="fs-field-helper error block" role={server[field] ? "alert" : undefined}>{messages[field]}</span> : null;
  const replaceStepKindConfigField = (kind: string, field: HttpField, value: unknown) => {
    httpIntent.touchField(field);
    storeHttpField(kind, field, value);
  };

      const responseMode = textOrEmpty(stepKindConfig.response_mode) || "inline";
      const maxResponseBytes = numericInputValue(stepKindConfig.max_response_bytes);
      return (
        <div className="space-y-3 rounded-xl border border-[var(--fs-border-default)] bg-[var(--fs-bg-surface-muted)] p-3">
          <p className="text-xs font-medium uppercase tracking-[0.12em] text-[var(--fs-text-tertiary)]">
            {t("react.workflows.http_request", "HTTP request")}</p>
          <div className="grid gap-2 sm:grid-cols-[140px_minmax(0,1fr)]">
            <label data-http-field="method" className="block text-xs text-[var(--fs-text-secondary)]">
              <RequiredLabel>{t("react.workflows.method", "Method")}</RequiredLabel>
              <DsSelect
                ariaLabel={t("react.workflows.method", "Method")}
                invalid={Boolean(messages.method)}
                describedBy={messages.method ? messageId("method") : undefined}
                required
                className="mt-1"
                onChange={(nextValue) =>
                  replaceStepKindConfigField(
                    "http_request",
                    "method",
                    nextValue,
                  )}
                value={textOrEmpty(stepKindConfig.method) || "GET"}
                options={[...[
                  "GET",
                  "POST",
                  "PUT",
                  "PATCH",
                  "DELETE",
                  "HEAD",
                  "OPTIONS",
                ].map((method) => (
                  ({ value: method, label: method })
                ))]}
              />
            {!["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].includes(String(stepKindConfig.method)) ? <span className="fs-field-helper">{t("react.workflows.http_method_advanced", "A stored method variant is retained. Use Advanced to edit its template/source, or choose a constant here.")}</span> : null}
              {renderMessage("method")}
            </label>
            <label data-http-field="url" className="block text-xs text-[var(--fs-text-secondary)]">
              <RequiredLabel>URL</RequiredLabel>
              {renderWorkflowVariableInput({
                ariaLabel: "URL",
                ariaInvalid: Boolean(messages.url),
                ariaDescribedBy: messages.url ? messageId("url") : undefined,
                ariaRequired: true,
                className: inputClass,
                mode: "template",
                onChange: (nextValue: string) =>
                  replaceStepKindConfigField("http_request", "url", nextValue),
                panelId: workflowVariableSuggestionPanelId("http-request-url"),
                placeholder: "https://api.example.com/items/{{ state.item_id }}",
                value: textOrEmpty(stepKindConfig.url),
              })}
            {renderMessage("url")}
            </label>
          </div>
          <div className="grid gap-2 sm:grid-cols-3">
            <label data-http-field="response_target" className="block text-xs text-[var(--fs-text-secondary)]">
              <RequiredLabel>{t("react.workflows.response_target", "Response target")}</RequiredLabel>
              {renderWorkflowVariableInput({
                ariaLabel: t("react.workflows.response_target", "Response target"),
                ariaInvalid: Boolean(messages.response_target),
                ariaDescribedBy: messages.response_target ? messageId("response_target") : undefined,
                ariaRequired: true,
                className: inputClass,
                onChange: (nextValue: string) =>
                  replaceStepKindConfigField(
                    "http_request",
                    "response_target",
                    nextValue,
                  ),
                panelId: httpResponseTargetListId,
                placeholder: "state.api_response",
                value: textOrEmpty(stepKindConfig.response_target),
              })}
            {renderMessage("response_target")}
            <span className="fs-field-helper">
              {responseMode === "artifact"
                ? t("react.workflows.http_artifact_target_guidance", "Artifact responses save a file handle at this target.")
                : t("react.workflows.http_inline_target_guidance", "The response body is saved at this target. Select the target directly in later steps; do not append .body.")}
            </span>
            </label>
            <HttpStatusesField label={t("react.workflows.expected_statuses", "Expected statuses")} value={stepKindConfig.expected_statuses} intent={httpIntent} message={messages.expected_statuses} serverError={Boolean(server.expected_statuses)} onChange={(value) => storeHttpField("http_request", "expected_statuses", value)} />
            <label data-http-field="timeout_seconds" className="block text-xs text-[var(--fs-text-secondary)]">
              <RequiredLabel>{t("react.workflows.timeout_seconds", "Timeout seconds")}</RequiredLabel>
              <input
                className={inputClass}
                min={0}
                onChange={(event) =>
                  replaceStepKindConfigField(
                    "http_request",
                    "timeout_seconds",
                    numericDraftFromInput(event.target.value),
                  )
                }
                step="any"
                type="number"
                value={numericInputValue(stepKindConfig.timeout_seconds)}
                required
                data-step-numeric-field="timeout_seconds"
                aria-label={t("react.workflows.timeout_seconds", "Timeout seconds")}
                aria-invalid={errors.timeout_seconds ? true : undefined}
                aria-describedby={errors.timeout_seconds ? `${numericErrorId}-timeout` : undefined}
              />
              {errors.timeout_seconds ? <span className="fs-field-helper error block" id={`${numericErrorId}-timeout`} role="alert">{errors.timeout_seconds}</span> : null}
            </label>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            <label data-http-field="response_mode" className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.response_mode", "Response mode")}</span>
              <DsSelect
                ariaLabel={t("react.workflows.response_mode", "Response mode")}
                invalid={Boolean(messages.response_mode)}
                describedBy={messages.response_mode ? messageId("response_mode") : undefined}
                className="mt-1"
                onChange={(nextValue) =>
                  replaceStepKindConfigField(
                    "http_request",
                    "response_mode",
                    nextValue,
                  )}
                value={responseMode}
                options={[({ value: "inline", label: t("react.workflows.inline", "inline") }), ({ value: "artifact", label: t("react.workflows.artifact_variant_c7c5c1d7", "artifact") })]}
              />
            {renderMessage("response_mode")}
            </label>
            <label data-http-field="max_response_bytes" className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.max_response_bytes", "Max response bytes")}</span>
              <input
                className={inputClass}
                onChange={(event) =>
                  replaceStepKindConfigField(
                    "http_request",
                    "max_response_bytes",
                    numericDraftFromInput(event.target.value),
                  )
                }
                type="text"
                inputMode="numeric"
                value={maxResponseBytes}
                data-step-numeric-field="max_response_bytes"
                aria-label={t("react.workflows.max_response_bytes", "Max response bytes")}
                aria-invalid={errors.max_response_bytes ? true : undefined}
                aria-describedby={errors.max_response_bytes ? `${numericErrorId}-bytes` : undefined}
              />
              {errors.max_response_bytes ? <span className="fs-field-helper error block" id={`${numericErrorId}-bytes`} role="alert">{errors.max_response_bytes}</span> : null}
            </label>
          </div>
          {responseMode === "artifact" ? (
            <label data-http-field="artifact_filename" className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("react.workflows.artifact_filename", "Artifact filename")}</span>
              {renderWorkflowVariableInput({
                ariaLabel: t("react.workflows.artifact_filename", "Artifact filename"),
                ariaInvalid: Boolean(messages.artifact_filename),
                ariaDescribedBy: messages.artifact_filename ? messageId("artifact_filename") : undefined,
                className: inputClass,
                mode: "template",
                onChange: (nextValue: string) =>
                  replaceStepKindConfigField("http_request", "artifact_filename", nextValue),
                panelId: workflowVariableSuggestionPanelId("http-request-artifact-filename"),
                placeholder: "download.bin",
                value: textOrEmpty(stepKindConfig.artifact_filename),
              })}
            {renderMessage("artifact_filename")}
            </label>
          ) : null}
          <div className="grid gap-2 sm:grid-cols-2">
            <label data-http-field="auth_mode" className="block text-xs text-[var(--fs-text-secondary)]">
              <span>{t("monitoring.flags.auth_mode", "Auth mode")}</span>
              <DsSelect
                ariaLabel={t("monitoring.flags.auth_mode", "Auth mode")}
                invalid={Boolean(messages.auth_mode)}
                describedBy={messages.auth_mode ? messageId("auth_mode") : undefined}
                className="mt-1"
                onChange={(nextValue) =>
                  replaceStepKindConfigField(
                    "http_request",
                    "auth_mode",
                    nextValue,
                  )}
                value={textOrEmpty(stepKindConfig.auth_mode) || "none"}
                options={[({ value: "none", label: t("registry.published_blocks.none", "none") }), ({ value: "bearer", label: "bearer" }), ({ value: "basic", label: "basic" })]}
              />
            {renderMessage("auth_mode")}
            </label>
            <label data-http-field="auth_ref" className="block text-xs text-[var(--fs-text-secondary)]">
              {String(stepKindConfig.auth_mode || "none").trim().toLowerCase() !== "none" ? <RequiredLabel>{t("react.workflows.auth_reference", "Auth reference")}</RequiredLabel> : <span>{t("react.workflows.auth_reference", "Auth reference")}</span>}
              <input
                className={inputClass}
                onChange={(event) =>
                  replaceStepKindConfigField(
                    "http_request",
                    "auth_ref",
                    event.target.value,
                  )
                }
                placeholder={t("react.workflows.secret_name", "secret name")}
                aria-label={t("react.workflows.auth_reference", "Auth reference")}
                aria-required={String(stepKindConfig.auth_mode || "none").trim().toLowerCase() !== "none"}
                aria-invalid={Boolean(messages.auth_ref)}
                aria-describedby={messages.auth_ref ? messageId("auth_ref") : undefined}
                value={textOrEmpty(stepKindConfig.auth_ref)}
              />
            {renderMessage("auth_ref")}
              <span className="fs-field-helper">{t("react.workflows.http_auth_help", "Use project secret references. Bearer uses one reference; basic uses username:password references. Authenticated requests require a literal HTTPS origin.")}</span>
            </label>
          </div>
          <HttpMapField field="headers" label={t("react.registry.headers", "Headers")} value={stepKindConfig.headers} intent={httpIntent} renderVariableInput={renderWorkflowVariableInput} message={messages.headers} onChange={(value) => storeHttpField("http_request", "headers", value)} />
          <HttpMapField field="query_params" label={t("react.workflows.display_query_parameters", "Query parameters")} value={stepKindConfig.query_params} intent={httpIntent} renderVariableInput={renderWorkflowVariableInput} message={messages.query_params} onChange={(value) => storeHttpField("http_request", "query_params", value)} />
          <HttpValueField field="body_template" label={t("react.workflows.body", "Body")} value={stepKindConfig.body_template} multiline intent={httpIntent} renderVariableInput={renderWorkflowVariableInput} message={messages.body_template} onChange={(value) => storeHttpField("http_request", "body_template", value)} />
        </div>
      );
    }
