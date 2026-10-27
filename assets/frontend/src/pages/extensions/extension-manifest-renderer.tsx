import {
  ExternalResourcePicker,
  type ExternalResourcePickerHostDeclaration,
} from "@/components/extensions/external-resource-picker";
import { createExternalResourcePickerBridge } from "@/components/extensions/external-resource-picker-bridge";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { columnIdentity, sourceDeclaration } from "@/pages/extensions/extension-locale";
import {
  actionVisibleForRow,
  badgeTone,
  componentQueryParamsForComponent,
  connectionSecretRefs,
  deriveHealthStatusFromChecks,
  formatGridValue,
  listFromUnknown,
  manifestQueryValues,
  objectOrEmpty,
  stringFrom,
  valueAtPath,
  visibleIfRows,
  type ManifestAction,
} from "@/pages/extensions/extension-manifest-utils";
import { renderMarkdownBlocks } from "@/pages/extensions/extension-markdown";
import { ChevronRight } from "lucide-react";
import type { ReactNode } from "react";

import { DsAlert, DsBadge } from "@/components/shared/design-system";
import "@/pages/extensions/extension-manifest.css";

import { renderConnectionRow } from "./extension-manifest-connections";
import { manifestFieldName, renderManifestField } from "./extension-manifest-fields";
import {
  connectionSaveLabel,
  connectionSelectedType,
  connectionTypeField,
  connectionTypeVariants,
  connectionVariantData,
  extensionBrowserAssetUrl,
  fieldVisibleForValues,
  firstPopulatedItems,
  manifestFormActions,
  manifestSummaryEntries,
  platformText,
  webhookConfigurationLabel,
  type ManifestRenderContext,
} from "./extension-manifest-render-model";
export function renderManifestComponent(
  component: Record<string, unknown>,
  context: ManifestRenderContext, t: I18nContextValue["t"] = fallbackContext.t
) {
  const type = stringFrom(component.type || "section");
  const componentId = stringFrom(component.component_id || component.id || component.key || type);
  const title = stringFrom(component.title || component.label || componentId || type);
  const sourceComponent = sourceDeclaration(component);
  // React keys follow the declaration, so a language switch never remounts a block.
  const blockKey = stringFrom(
    sourceComponent.component_id || sourceComponent.title || sourceComponent.label || componentId || type,
  );
  const uiSchema = objectOrEmpty(component.ui_schema);
  const queryPayload = objectOrEmpty(context.componentQueryData[componentId]);
  const queryValues = manifestQueryValues(queryPayload);
  const values = objectOrEmpty(
    context.formValuesByComponent[componentId] || queryValues || component.values,
  );
  const queryResult = objectOrEmpty(queryPayload.result || queryPayload);
  const componentErrorEntry = context.componentErrors[componentId];
  const componentError = stringFrom(componentErrorEntry?.message);
  const invalidFieldPath = stringFrom(componentErrorEntry?.fieldPath);
  // A message shown beside the offending control does not also belong at the top
  // of the form: the same sentence twice reads as two separate problems.
  const componentAlert = (rendered: Array<Record<string, unknown>>) =>
    invalidFieldPath && rendered.some((field) => manifestFieldName(field) === invalidFieldPath)
      ? ""
      : componentError;
  const summaryEntries = manifestSummaryEntries(queryResult.summary || queryPayload.summary);

  function renderExternalResourcePicker() {
    const data = objectOrEmpty(component.data);
    const targetComponentId = stringFrom(data.target_component_id);
    try {
      const declaration = data as unknown as ExternalResourcePickerHostDeclaration;
      const executeBootstrap = (actionId: string) =>
        context.onExternalResourcePickerBootstrap(actionId, component);
      const bridge = context.externalResourcePickerBridgeFactory
        ? context.externalResourcePickerBridgeFactory(declaration, executeBootstrap)
        : createExternalResourcePickerBridge(declaration, {
            executeBootstrap,
            resolveAssetScript: (assetRef) => extensionBrowserAssetUrl({
              extensionId: context.extensionId,
              extensionInstallId: context.extensionInstallId,
              extensionReleaseId: context.extensionReleaseId,
              extensionDescriptorSha256: context.extensionDescriptorSha256,
              projectId: context.projectId,
              assetRef,
            }),
          });
      return (
        <ExternalResourcePicker
          bridge={bridge}
          declaration={declaration}
          disabled={context.busy || !targetComponentId}
          onSelection={(rows) => {
            const selection = rows[0] || {};
            for (const [fieldName, value] of Object.entries(selection)) {
              context.onFieldChange(targetComponentId, fieldName, value);
            }
          }}
        />
      );
    } catch {
      return (
        <DsAlert title={stringFrom(data.error_message, t("react.extensions.selection_could_not_be_applied_try_again", "Selection could not be applied. Try again."))} tone="danger" />
      );
    }
  }

  function renderForm() {
    const fields = listFromUnknown(uiSchema.fields);
    const sections = listFromUnknown(uiSchema.sections);
    const formAlert = componentAlert([
      ...fields,
      ...sections.flatMap((section) => listFromUnknown(objectOrEmpty(section).fields)),
    ]);
    return (
      <form
        className="fs-manifest-block"
        key={blockKey}
        onSubmit={(event) => event.preventDefault()}
      >
        <div>
          <p className="fs-manifest-title">{title}</p>
          {stringFrom(uiSchema.description || component.description) ? (
            <p className="fs-manifest-description">{stringFrom(uiSchema.description || component.description)}</p>
          ) : null}
        </div>
        {formAlert ? <DsAlert title={formAlert} tone="danger" /> : null}
        {summaryEntries.length ? (
          <dl className="fs-manifest-summary">
            {summaryEntries.map((entry) => (
              <div key={`${entry.label}:${entry.value}`}>
                <dt>{entry.label}:</dt>
                <dd>{entry.value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
        {sections.length ? (
          sections.map((section) => {
            const fieldIds = Array.isArray(section.field_ids) ? section.field_ids.map((item) => stringFrom(item)) : [];
            const sectionFields = fields.filter((field) => fieldIds.includes(stringFrom(field.field_id || field.id || field.path)));
            return (
              <div className="fs-manifest-inset" key={stringFrom(sourceDeclaration(section).section_id || sourceDeclaration(section).title)}>
                <div>
                  <p className="fs-manifest-section-title">{stringFrom(section.title || t("react.registry.section", "Section"))}</p>
                  {stringFrom(section.description) ? <p className="fs-manifest-description">{stringFrom(section.description)}</p> : null}
                </div>
                <div className="fs-manifest-fields">
                  {sectionFields.map((field) =>
                    renderManifestField({
                      componentId,
                      field,
                      values,
                      onFieldChange: (fieldName, nextValue) => {
                        context.onFieldChange(componentId, fieldName, nextValue);
                      },
                      invalidFieldPath,
                      invalidMessage: componentError,
                      t,
                    }),
                  )}
                </div>
              </div>
            );
          })
        ) : (
          <div className="fs-manifest-fields">
            {fields.map((field) =>
              renderManifestField({
                componentId,
                field,
                values,
                onFieldChange: (fieldName, nextValue) => {
                  context.onFieldChange(componentId, fieldName, nextValue);
                },
                invalidFieldPath,
                invalidMessage: componentError,
                t,
              }),
            )}
          </div>
        )}
        <div className="fs-manifest-actions">
          {manifestFormActions(component, t).map((action, index) => (
            <button
              className={index === 0 ? "fs-button sm primary" : "fs-button sm"}
              disabled={context.busy}
              key={`${action.kind}:${action.actionId || action.queryId}`}
              onClick={(event) => {
                context.onManifestAction(action, event.currentTarget.form, component);
              }}
              type="button"
            >
              {action.label}
            </button>
          ))}
        </div>
      </form>
    );
  }

  function renderConnectionForm( ) {
    const data = objectOrEmpty(component.data);
    if ((context.oauthConnectionProviderIds || []).includes(stringFrom(data.oauth_provider_id))) {
      return null;
    }
    const fields = listFromUnknown(component.fields);
    const defaults = objectOrEmpty(component.defaults);
    const selectedConnectionType = connectionSelectedType(data, defaults, values);
    const selectedVariant = connectionVariantData(data, selectedConnectionType);
    const variantDefaults = objectOrEmpty(selectedVariant.defaults);
    const effectiveValues = {
      ...defaults,
      ...variantDefaults,
      ...values,
      ...(connectionTypeField(data) ? { [connectionTypeField(data)]: selectedConnectionType } : {}),
    };
    const activeFields = fields.filter((field) => fieldVisibleForValues(field, effectiveValues));
    const connectionType = stringFrom(
      selectedVariant.connection_type || selectedConnectionType || data.connection_type || component.connection_type,
    );
    const effectiveData = {
      ...data,
      ...selectedVariant,
      connection_type: connectionType,
    };
    const oauthConnection = context.oauthConnectionTypes.includes(connectionType);
    const connectIntent = stringFrom(data.oauth_intent);
    const reconnectIntent = stringFrom(data.reconnect_intent, connectIntent);
    const accountLabelText = stringFrom(data.account_label, t("react.extensions.account", "account"));
    const variantKeys = Object.keys(connectionTypeVariants(data));
    const rows = context.projectConnections.filter((row) => {
      const extensionId = stringFrom(row.extension_id);
      const extensionInstallId = stringFrom(row.extension_install_id);
      const rowType = stringFrom(row.connection_type);
      return (
        extensionId === context.extensionId
        && (!context.extensionInstallId || extensionInstallId === context.extensionInstallId)
        && (
          variantKeys.length
            ? variantKeys.includes(rowType) || rowType === connectionType
            : !connectionType || rowType === connectionType
        )
      );
    });
    const selectedId = stringFrom(values.connection_id);
    const selectedRow = rows.find((row) => stringFrom(row.connection_id) === selectedId);
    const savedSecretPaths = new Set(
      Object.keys(connectionSecretRefs(objectOrEmpty(selectedRow).secret_refs)),
    );
    return (
      <div className="fs-manifest-block" key={componentId}>
        <div className="fs-manifest-head">
          <div>
            <p className="fs-manifest-title">{title}</p>
            {stringFrom(component.description) ? <p className="fs-manifest-description">{stringFrom(component.description)}</p> : null}
          </div>
          <span className="fs-manifest-meta">{rows.length}{" "}{t("react.workflows.configured", "configured")}</span>
        </div>
        {componentAlert(activeFields) ? <DsAlert title={componentAlert(activeFields)} tone="danger" /> : null}
        {rows.length ? (
          <div className="fs-manifest-list">
            {rows.map((row) =>
              renderConnectionRow({
                component,
                connectIntent,
                context,
                oauthConnection,
                reconnectIntent,
                row,
                selectedId,
              }, t),
            )}
          </div>
        ) : null}
        <form className="fs-manifest-inset" onSubmit={(event) => event.preventDefault()}>
          <div className="fs-manifest-fields">
            {activeFields.map((field) =>
              renderManifestField({
                componentId,
                field,
                values: effectiveValues,
                onFieldChange: (fieldName, nextValue) => context.onFieldChange(componentId, fieldName, nextValue),
                invalidFieldPath,
                invalidMessage: componentError,
                savedSecretPaths,
                t,
              }),
            )}
          </div>
          <div className="fs-manifest-actions">
            <button className="fs-button sm primary" disabled={context.busy || (oauthConnection && !selectedRow && Boolean(context.connectionCreationBlockedReason))} onClick={() => context.onConnectionSave(component, effectiveValues)} title={oauthConnection && !selectedRow ? context.connectionCreationBlockedReason : undefined} type="button">
              {connectionSaveLabel(selectedRow, oauthConnection, accountLabelText, t)}
            </button>
            {manifestFormActions(component, t).map((action) => (
              <button
                className="fs-button sm"
                disabled={context.busy}
                key={`${action.kind}:${action.actionId || action.queryId}`}
                onClick={(event) => {
                  context.onManifestAction(action, event.currentTarget.form, component, {
                    values: effectiveValues,
                    data: effectiveData,
                  });
                }}
                type="button"
              >
                {action.label}
              </button>
            ))}
            {selectedRow ? (
              <button className="fs-button sm ghost" onClick={() => context.onConnectionSelect(component, null)} type="button">{t("react.extensions.new_connection", "New connection")}</button>
            ) : null}
          </div>
        </form>
      </div>
    );
  }

  function renderStateEditor( ) {
    const data = objectOrEmpty(component.data);
    const namespace = stringFrom(data.namespace || component.namespace);
    const keyPrefix = stringFrom(data.state_key_prefix || component.state_key_prefix);
    const keyField = stringFrom(data.key_field || "state_key");
    const fields = listFromUnknown(component.fields);
    const rows = context.projectState.filter((row) => {
      const stateKey = stringFrom(row.state_key);
      return stringFrom(row.namespace) === namespace && (!keyPrefix || stateKey.startsWith(keyPrefix));
    });
    const selectedKey = stringFrom(values.state_key);
    const hasSelectedRow = rows.some((row) => stringFrom(row.state_key) === selectedKey);
    const defaults = objectOrEmpty(component.defaults);
    const effectiveValues = Object.keys(values).length ? values : defaults;
    const activeFields = fields.filter((field) => fieldVisibleForValues(field, effectiveValues));
    return (
      <div className="fs-manifest-block" key={componentId}>
        <div className="fs-manifest-head">
          <div>
            <p className="fs-manifest-title">{title}</p>
            {stringFrom(component.description) ? <p className="fs-manifest-description">{stringFrom(component.description)}</p> : null}
          </div>
          <span className="fs-manifest-meta">{rows.length}{" "}{t("react.extensions.saved", "saved")}</span>
        </div>
        {componentAlert(activeFields) ? <DsAlert title={componentAlert(activeFields)} tone="danger" /> : null}
        {rows.map((row) => {
          const rowKey = stringFrom(row.state_key);
          return (
            <div className="fs-manifest-row" key={rowKey}>
              <div className="fs-manifest-row-copy">
                <p className="fs-manifest-row-name">{rowKey.startsWith(keyPrefix) ? rowKey.slice(keyPrefix.length) : rowKey}</p>
                <p className="fs-manifest-row-detail">{t("react.extensions.revision", "Revision")}{" "}{stringFrom(row.revision, "1")}</p>
              </div>
              <div className="fs-manifest-actions">
                <button className="fs-button sm" onClick={() => context.onProjectStateSelect(component, row)} type="button">{t("extensions.repeater.edit", "Edit")}</button>
                <button className="fs-button sm ghost-danger" onClick={() => context.onProjectStateDelete(component, row)} type="button">{t("settings.model_providers.delete", "Delete")}</button>
              </div>
            </div>
          );
        })}
        <form className="fs-manifest-inset" onSubmit={(event) => event.preventDefault()}>
          <input name={keyField} type="hidden" value={stringFrom(effectiveValues[keyField])} />
          <div className="fs-manifest-fields">
            {activeFields.map((field) =>
              renderManifestField({
                componentId,
                field,
                values: effectiveValues,
                onFieldChange: (fieldName, nextValue) => context.onFieldChange(componentId, fieldName, nextValue),
                invalidFieldPath,
                invalidMessage: componentError,
                t,
              }),
            )}
          </div>
          <div className="fs-manifest-actions">
            <button className="fs-button sm primary" disabled={context.busy} onClick={() => context.onProjectStateSave(component, effectiveValues)} type="button">{hasSelectedRow ? t("users.modal.save", "Save") : t("projects.modal.create", "Create")}</button>
            {hasSelectedRow ? <button className="fs-button sm ghost" onClick={() => context.onProjectStateSelect(component, null)} type="button">{t("react.extensions.new", "New")}</button> : null}
          </div>
        </form>
      </div>
    );
  }

  function renderStatsRow() {
    const responseItems = listFromUnknown(queryPayload.items);
    const resultItems = listFromUnknown(queryResult.items);
    const statItems = firstPopulatedItems(
      responseItems,
      resultItems,
      listFromUnknown(component.items),
    );
    return (
      <div className="fs-manifest-stats" key={blockKey}>
        {statItems.map((item, index) => (
          <div className="fs-manifest-stat" key={`${stringFrom(item.label)}:${index}`}>
            <p className="fs-manifest-stat-label">{stringFrom(item.label)}</p>
            <p className="fs-manifest-stat-value">{stringFrom(item.value, "—")}</p>
            {stringFrom(item.trend) ? <p className="fs-manifest-stat-trend">{stringFrom(item.trend)}</p> : null}
          </div>
        ))}
      </div>
    );
  }

  function renderHealthPanel( ) {
    const checks = listFromUnknown(queryResult.checks).length
      ? listFromUnknown(queryResult.checks)
      : listFromUnknown(component.checks);
    const status = deriveHealthStatusFromChecks(
      stringFrom(
        queryResult.status || queryPayload.status || objectOrEmpty(queryPayload.summary).status || "unknown",
      ),
      checks,
    );
    const provider = stringFrom(queryResult.extension_id || queryResult.provider || context.extensionId);
    const runtime = stringFrom(queryResult.runtime || "");
    return (
      <div className="fs-manifest-block" key={componentId}>
        <p className="fs-manifest-title">{title}</p>
        <dl className="fs-kv">
          <div className="fs-kv-row"><dt>{platformText(context, "extensions.runtime.status", t("extensions.runtime.status", "Status"))}</dt><dd>{status}</dd></div>
          {runtime ? <div className="fs-kv-row"><dt>{platformText(context, "extensions.runtime.runtime", "Runtime")}</dt><dd>{runtime}</dd></div> : null}
          <div className="fs-kv-row"><dt>{platformText(context, "extensions.runtime.provider", t("extensions.runtime.provider", "Provider"))}</dt><dd>{provider || t("project_detail.connections.status.unknown", "unknown")}</dd></div>
        </dl>
        {checks.map((check, index) => {
          // A check may carry one contextual action: where to go to fix it.
          const action = objectOrEmpty(check.action);
          const actionLabel = stringFrom(action.label);
          const actionPageId = stringFrom(action.page_id || action.pageId);
          const actionAnchorId = stringFrom(
            action.anchor_id || action.anchorId || action.component_id || action.componentId,
          );
          const navigate = context.onNavigateToAnchor;
          const readinessLabel = stringFrom(check.label);
          return (
            <div className="fs-manifest-row is-top" key={`${stringFrom(check.name)}:${index}`}>
              <div className="fs-manifest-row-copy">
                <p className="fs-manifest-row-name">
                  <span>{stringFrom(check.name || `Check ${index + 1}`)}</span>
                  {readinessLabel ? <DsBadge>{readinessLabel}</DsBadge> : null}
                </p>
                <p className="fs-manifest-row-detail">{stringFrom(check.detail || check.status)}</p>
              </div>
              {navigate && actionLabel && actionPageId ? (
                <button
                  className="fs-button sm"
                  onClick={() => navigate(actionPageId, actionAnchorId)}
                  type="button"
                >
                  {actionLabel}
                </button>
              ) : null}
            </div>
          );
        })}
      </div>
    );
  }

  function renderCallbackSetup( ) {
    const callbackId = stringFrom(component.callback_id || component.callbackId);
    if (!context.canManageProject) {
      return (
        <div className="fs-manifest-block" key={blockKey}>
          <div>
            <p className="fs-manifest-title">{title}</p>
            <p className="fs-manifest-meta fs-manifest-code">{callbackId || "callback"}</p>
            <p className="fs-manifest-description">{t("react.extensions.callback_setup_is_managed_by_project_managers", "Callback setup is managed by project managers.")}</p>
          </div>
        </div>
      );
    }
    const registration = context.callbacks.find((item) => item.callback_id === callbackId);
    const configured = Boolean(registration?.id);
    const lastSeen = stringFrom(registration?.last_seen_at) || t("react.extensions.display_not_seen", "not seen");
    const lastError = stringFrom(registration?.last_error);
    const webhookStatus = objectOrEmpty(
      registration?.webhook_status ||
        context.callbackStatuses[stringFrom(registration?.id) || callbackId],
    );
    const urlConfigured = webhookStatus.url_configured;
    const pendingCount = stringFrom(webhookStatus.pending_update_count);
    const webhookError = stringFrom(webhookStatus.last_error_message);
    return (
      <div className="fs-manifest-block" key={blockKey}>
        <div className="fs-manifest-head">
          <div>
            <p className="fs-manifest-title">{title}</p>
            <p className="fs-manifest-meta fs-manifest-code">{callbackId || "callback"}</p>
          </div>
          <DsBadge tone={configured && registration?.enabled ? "green" : "neutral"}>
            {configured && registration?.enabled ? t("react.extensions.registered", "registered") : t("react.extensions.not_registered", "not registered")}
          </DsBadge>
        </div>
        <dl className="fs-kv">
          <div className="fs-kv-row">
            <dt>{platformText(context, "extensions.callback.url", "Callback URL")}</dt>
            <dd>{stringFrom(registration?.callback_url) || "/webhook/extensions/.../<redacted>"}</dd>
          </div>
          <div className="fs-kv-row"><dt>{platformText(context, "extensions.callback.last_seen", "Last seen")}</dt><dd>{lastSeen}</dd></div>
          <div className="fs-kv-row"><dt>{platformText(context, "extensions.callback.webhook", "Webhook")}</dt><dd>{webhookConfigurationLabel(urlConfigured, t)}</dd></div>
          <div className="fs-kv-row"><dt>{platformText(context, "extensions.callback.pending", "Pending")}</dt><dd>{pendingCount || t("project_detail.connections.status.unknown", "unknown")}</dd></div>
        </dl>
        {lastError ? <DsAlert title={lastError} tone="danger" /> : null}
        {webhookError ? <DsAlert title={webhookError} tone="warning" /> : null}
        <div className="fs-manifest-actions">
          <button
            className="fs-button sm primary"
            disabled={context.busy || !callbackId || !context.projectId}
            onClick={() => context.onCallbackSetup(component, "configure")}
            type="button"
          >
            {platformText(context, "extensions.callback.configure", "Create and Configure")}
          </button>
          <button
            className="fs-button sm"
            disabled={context.busy}
            onClick={() => context.onCallbackSetup(component, "status")}
            type="button"
          >
            {platformText(context, "extensions.callback.refresh", "Refresh Status")}
          </button>
        </div>
      </div>
    );
  }

  function renderLogList() {
    return (
      <div className="fs-manifest-block" key={blockKey}>
        <p className="fs-manifest-title">{title}</p>
        <div className="fs-manifest-list">
          {listFromUnknown(component.items).map((item, index) => (
            <div className="fs-manifest-row" key={`${stringFrom(item.message)}:${index}`}>
              <div className="fs-manifest-row-copy">
                <p className="fs-manifest-row-name">{stringFrom(item.message)}</p>
                <p className="fs-manifest-row-detail">{stringFrom(item.severity)}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    );
  }

  function renderMarkdown() {
    const content = stringFrom(
      component.content ||
        component.body ||
        queryResult.content ||
        queryResult.body ||
        queryPayload.content ||
        queryPayload.body,
    );
    if (component.collapsible === true) {
      // Documentation that must not compete with the controls: closed until asked for.
      return (
        <details className="fs-settings-section" key={componentId} open={component.open === true}>
          <summary>
            <ChevronRight aria-hidden="true" className="fs-disclosure-chevron" />
            {title}
          </summary>
          {content ? <div className="fs-settings-section-body">{renderMarkdownBlocks(content)}</div> : null}
        </details>
      );
    }
    return (
      <div className="fs-manifest-block" key={componentId}>
        <p className="fs-manifest-title">{title}</p>
        {content ? renderMarkdownBlocks(content) : null}
      </div>
    );
  }

  function renderGrid( ) {
    const columns = listFromUnknown(component.columns);
    const queryMeta = objectOrEmpty(context.componentQueryParams[componentId]);
    const rows = listFromUnknown(queryResult.items).length
      ? listFromUnknown(queryResult.items)
      : listFromUnknown(queryResult.connectors);
    const currentPage = Math.max(1, Number(queryResult.page || queryMeta.page || 1));
    const pageSize = Math.max(
      1,
      Number(
        queryResult.page_size ||
          queryMeta.page_size ||
          componentQueryParamsForComponent(component).page_size ||
          20,
      ),
    );
    const responseIsPaginated =
      Object.hasOwn(queryResult, "page") || Object.hasOwn(queryResult, "page_size");
    const visibleRows = responseIsPaginated
      ? rows
      : rows.slice((currentPage - 1) * pageSize, currentPage * pageSize);
    const totalRows = Math.max(0, Number(queryResult.total || rows.length || 0));
    const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
    const hasMore = Boolean(queryResult.has_more) || currentPage < totalPages;
    const emptyState = objectOrEmpty(component.empty_state);
    const parsedRowActions: Array<ManifestAction | null> = listFromUnknown(component.row_actions)
      .map((item) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) {
          return null;
        }
        const action = item;
        return {
          actionId: stringFrom(action.action_id || action.id),
          queryId: stringFrom(action.query_id),
          label: stringFrom(action.label || action.title || action.action_id, t("react.extensions.run", "Run")),
          kind:
            stringFrom(action.kind || action.type).toLowerCase() === "query" || action.query_id
              ? ("query" as const)
              : ("action" as const),
          scope: stringFrom(action.scope || "row"),
          visibleIf: visibleIfRows(action.visible_if),
        } satisfies ManifestAction;
      });
    const rowActions = parsedRowActions.filter(
      (item): item is ManifestAction => Boolean(item && (item.actionId || item.queryId)),
    );
    return (
      <div className="fs-manifest-block" key={componentId}>
        <p className="fs-manifest-title">{title}</p>
        {componentError ? <DsAlert title={componentError} tone="danger" /> : null}
        {visibleRows.length ? (
          <>
            <div className="fs-manifest-table-scroll">
              <table className="fs-table embedded">
                <thead>
                  <tr>
                    {columns.map((column) => (
                      <th key={columnIdentity(column)} scope="col">
                        {stringFrom(column.label || column.key)}
                      </th>
                    ))}
                    {rowActions.length ? <th scope="col">{platformText(context, "extensions.table.actions", t("extensions.table.actions", "Actions"))}</th> : null}
                  </tr>
                </thead>
                <tbody>
                  {visibleRows.map((row, index) => (
                    <tr key={`${componentId}:${index}`}>
                      {columns.map((column) => {
                        const columnKey = columnIdentity(column);
                        const rawValue = valueAtPath(objectOrEmpty(row), columnKey);
                        const columnType = stringFrom(column.type || "text").toLowerCase();
                        const monospace = Boolean(column.monospace || columnType === "id");
                        return (
                          <td key={`${componentId}:${index}:${columnKey}`}>
                            {columnType === "badge" ? (
                              <DsBadge
                                tone={badgeTone(
                                  stringFrom(rawValue).toLowerCase(),
                                  objectOrEmpty(column.badge_map),
                                )}
                              >
                                {formatGridValue(rawValue, column, t)}
                              </DsBadge>
                            ) : (
                              <span className={monospace ? "fs-manifest-code" : undefined}>
                                {formatGridValue(rawValue, column, t)}
                              </span>
                            )}
                          </td>
                        );
                      })}
                      {rowActions.length ? (
                        <td>
                          <div className="fs-manifest-actions">
                            {rowActions
                              .filter((action) => actionVisibleForRow(action, objectOrEmpty(row)))
                              .map((action) => {
                                const rowEntity = objectOrEmpty(row);
                                const rowItemId = stringFrom(
                                  rowEntity.delivery_id ||
                                    rowEntity.id ||
                                    rowEntity.item_id ||
                                    rowEntity.route_id,
                                );
                                return (
                                  <button
                                    className="fs-button sm"
                                    disabled={context.busy}
                                    key={`${componentId}:${index}:${action.kind}:${action.actionId || action.queryId}`}
                                    onClick={() =>
                                      context.onManifestAction(action, null, component, {
                                        target: {
                                          ...rowEntity,
                                          scope: action.scope || "row",
                                          item_ids: rowItemId ? [rowItemId] : [],
                                        },
                                        data: rowEntity,
                                      })
                                    }
                                    type="button"
                                  >
                                    {action.label}
                                  </button>
                                );
                              })}
                          </div>
                        </td>
                      ) : null}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {totalRows > pageSize ? (
              <div className="fs-manifest-pager">
                <span>{t("react.extensions.page", "Page")}{" "}{currentPage}{" "}{t("react.registry.of", "of")}{" "}{totalPages} · {totalRows}{" "}{t("react.workflows.total", "total")}</span>
                <div className="fs-manifest-actions">
                  <button
                    className="fs-button sm"
                    disabled={context.busy || currentPage <= 1}
                    onClick={() =>
                      context.onComponentQueryParamsChange(componentId, {
                        ...queryMeta,
                        page: Math.max(1, currentPage - 1),
                        page_size: pageSize,
                      })
                    }
                    type="button"
                  >{t("react.extensions.previous", "Previous")}</button>
                  <button
                    className="fs-button sm"
                    disabled={context.busy || !hasMore}
                    onClick={() =>
                      context.onComponentQueryParamsChange(componentId, {
                        ...queryMeta,
                        page: currentPage + 1,
                        page_size: pageSize,
                      })
                    }
                    type="button"
                  >{t("react.extensions.next", "Next")}</button>
                </div>
              </div>
            ) : null}
          </>
        ) : (
          <div className="fs-manifest-empty">
            <p className="fs-manifest-empty-title">{stringFrom(emptyState.title, t("react.extensions.no_rows_yet", "No rows yet"))}</p>
            {stringFrom(emptyState.description) ? (
              <p className="fs-empty-copy">{stringFrom(emptyState.description)}</p>
            ) : null}
          </div>
        )}
      </div>
    );
  }

  const renderers: Record<string, () => ReactNode> = {
    callback_setup: renderCallbackSetup,
    connection_form: renderConnectionForm,
    external_resource_picker: renderExternalResourcePicker,
    form: renderForm,
    grid: renderGrid,
    health_panel: renderHealthPanel,
    log_list: renderLogList,
    markdown: renderMarkdown,
    state_editor: renderStateEditor,
    stats_row: renderStatsRow,
  };
  const renderer = renderers[type];
  if (renderer) {
    return renderer();
  }

  return (
    <div className="fs-manifest-block" key={blockKey}>
      <p className="fs-manifest-title">{title}</p>
    </div>
  );
}
