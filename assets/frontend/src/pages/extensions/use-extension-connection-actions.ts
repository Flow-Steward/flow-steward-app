import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useState } from "react";

import { type PendingConnectionDisconnect } from "@/pages/extensions/extension-manifest-render-model";
import {
  stringFrom,
  objectOrEmpty,
  listFromUnknown,
  stringListFromUnknown,
} from "@/pages/extensions/extension-manifest-utils";
import {
  startProjectConnectionOAuth,
  disconnectProjectConnectionOAuth,
  deleteProjectConnection,
  putExtensionProjectState,
  deleteExtensionProjectState,
} from "@/pages/projects/connections/api";
import {
  connectionTypeField,
  connectionSelectedType,
  connectionConfigFromRow,
  connectionDefaultsForValues,
  manifestFieldPath,
  manifestFieldIsSecret,
  connectionDataForValues,
  manifestFieldValue,
} from "./connection-form-support";
import {
  oauthStartErrorMessage,
  connectionAccountTarget,
  choose,
  anyTrue,
  oauthCallbackMessageFor,
} from "./hub-support";
import {
  prepareConnectionSave,
  runConnectionPreSaveActions,
  connectionPayloadForSave,
  persistPreparedConnection,
  recoverFailedConnectionSave,
} from "./connection-save";
import { connectionSaveErrorMessage, errorFieldPath } from "./manifest-action-support";

import type { useExtensionsHubState } from "./use-extensions-hub-state";
import type { useExtensionLifecycleActions } from "./use-extension-lifecycle-actions";

type ExtensionConfirmation = {
  title: string;
  consequence: string;
  confirmLabel: string;
  pendingLabel: string;
  irreversible: boolean;
  tone: "danger" | "primary";
  failure?: string;
  run?: () => Promise<void>;
  settle: (confirmed: boolean) => void;
};

function continueOAuthAuthorization(payload: Record<string, unknown>, t: I18nContextValue["t"] = fallbackContext.t) {
  const authorizationUrl = stringFrom(payload.authorization_url);
  if (!authorizationUrl) {
    throw new Error(t("react.extensions.oauth_authorization_url_is_missing", "OAuth authorization URL is missing."));
  }
  globalThis.window.location.assign(authorizationUrl);
}

export function useExtensionConnectionActions(
  hubState: ReturnType<typeof useExtensionsHubState>,
  lifecycleActions: ReturnType<typeof useExtensionLifecycleActions>,
) {
  const { t } = useI18n();
  const {
    setComponentError,
    setComponentErrors,
    detailExtension,
    extensionInstallId,
    setFormValuesByComponent,
    location,
    activePageProjectId,
    projectConnectionsQuery,
    setStatusById,
    oauthConnectionTypes,
    activeManifestPage,
    projectStateQuery,
    oauthCallbackNotice,
    oauthCallbackConnection,
    archiveLifecycleQuery,
    archiveLifecycleVerificationFailed,
    archiveLifecycleUnavailable,
  } = hubState;
  const { withBusy } = lifecycleActions;
  const [confirmation, setConfirmation] = useState<ExtensionConfirmation | null>(null);
  const [confirmationPending, setConfirmationPending] = useState(false);
  const [confirmationError, setConfirmationError] = useState("");
  const [pendingDisconnect, setPendingDisconnect] = useState<PendingConnectionDisconnect | null>(null);

  /**
   * The one blocking confirmation the page can show. With `run`, the dialog
   * performs the action itself, so it can stay open with the reason if it
   * fails; without it, it only answers the question it was asked.
   */
  function requestConfirmation(request: Omit<ExtensionConfirmation, "settle">): Promise<boolean> {
    return new Promise((resolve) => {
      setConfirmationError("");
      setConfirmation({ ...request, settle: resolve });
    });
  }

  function cancelConfirmation() {
    confirmation?.settle(false);
    setConfirmation(null);
    setConfirmationError("");
  }

  async function acceptConfirmation() {
    const current = confirmation;
    if (!current) return;
    if (!current.run) {
      current.settle(true);
      setConfirmation(null);
      return;
    }
    setConfirmationPending(true);
    setConfirmationError("");
    try {
      await current.run();
      current.settle(true);
      setConfirmation(null);
    } catch (error) {
      setConfirmationError(error instanceof Error ? error.message : stringFrom(current.failure, t("react.extensions.the_action_failed", "The action failed.")));
    } finally {
      setConfirmationPending(false);
    }
  }

  // An error that stays on screen while the user fixes the field keeps telling
  // them about a problem they have just dealt with, so editing the named field
  // takes the message down. The value is checked again on the next save.
  function clearFieldError(componentId: string, fieldName: string) {
    setComponentErrors((current) => {
      const entry = current[componentId];
      if (!entry?.fieldPath || entry.fieldPath !== fieldName) {
        return current;
      }
      const next = { ...current };
      delete next[componentId];
      return next;
    });
  }

  function clearComponentError(component: Record<string, unknown>) {
    const componentId = stringFrom(component.component_id || component.id || component.key);
    if (!componentId) {
      return;
    }
    setComponentErrors((current) => {
      if (!current[componentId]) {
        return current;
      }
      const next = { ...current };
      delete next[componentId];
      return next;
    });
  }

  function connectionMatchesCurrentInstall(row: Record<string, unknown>): boolean {
    const rowExtensionId = stringFrom(row.extension_id);
    const rowInstallId = stringFrom(row.extension_install_id);
    return (
      rowExtensionId === stringFrom(detailExtension?.extension_id)
      && (!extensionInstallId || rowInstallId === extensionInstallId)
    );
  }

  function rejectForeignInstallConnection(
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ): boolean {
    if (connectionMatchesCurrentInstall(row)) {
      return false;
    }
    setComponentError(
      component,
      t("react.extensions.this_connection_belongs_to_another_installed_release_refresh_the_page_and_choose_an_account_from_this_installa", "This connection belongs to another installed release. Refresh the page and choose an account from this installation."),
    );
    return true;
  }

  function handleConnectionSelect(
    component: Record<string, unknown>,
    row: Record<string, unknown> | null,
  ) {
    const data = objectOrEmpty(component.data);
    const typeField = connectionTypeField(data);
    if (!row) {
      const selectedType = connectionSelectedType(component, {});
      setFormValuesByComponent((current) => ({
        ...current,
        [stringFrom(component.component_id || component.id || component.key)]: {
          ...(typeField ? { [typeField]: selectedType } : {}),
          connection_id: "",
        },
      }));
      clearComponentError(component);
      return;
    }
    if (rejectForeignInstallConnection(component, row)) {
      return;
    }
    const config = connectionConfigFromRow(row.connection_config);
    const rowType = stringFrom(row.connection_type);
    const seedValues = typeField ? { [typeField]: rowType } : {};
    const defaults = connectionDefaultsForValues(component, seedValues);
    const nextValues: Record<string, unknown> = {
      ...defaults,
      ...seedValues,
      connection_id: stringFrom(row.connection_id),
      display_name: stringFrom(row.display_name),
      environment: stringFrom(row.environment),
    };
    for (const field of listFromUnknown(component.fields)) {
      const path = manifestFieldPath(field);
      if (!path || manifestFieldIsSecret(field, new Set())) {
        continue;
      }
      nextValues[path] = config[path] ?? nextValues[path] ?? "";
    }
    for (const path of stringListFromUnknown(connectionDataForValues(component, nextValues).config_fields)) {
      nextValues[path] = config[path] ?? nextValues[path] ?? "";
    }
    setFormValuesByComponent((current) => ({
      ...current,
      [stringFrom(component.component_id || component.id || component.key)]: nextValues,
    }));
    clearComponentError(component);
  }

  function oauthReturnPath(): string {
    return `${location.pathname}${location.search}`;
  }

  async function handleConnectionOAuth(
    component: Record<string, unknown>,
    row: Record<string, unknown>,
    intent: string,
  ) {
    if (rejectForeignInstallConnection(component, row)) {
      return;
    }
    if (!activePageProjectId) {
      setComponentError(component, t("react.extensions.select_a_project_before_connecting_an_account", "Select a project before connecting an account."));
      return;
    }
    const connectionId = stringFrom(row.connection_id);
    if (!connectionId) {
      setComponentError(component, t("react.extensions.save_the_connection_before_starting_oauth", "Save the connection before starting OAuth."));
      return;
    }
    const extensionKey = detailExtension?.extension_id || "";
    await withBusy(extensionKey, async () => {
      try {
        const payload = await startProjectConnectionOAuth(
          activePageProjectId,
          connectionId,
          { intent, return_path: oauthReturnPath() },
        );
        clearComponentError(component);
        continueOAuthAuthorization(payload, t);
      } catch (error) {
        setComponentError(component, oauthStartErrorMessage(error, t));
      }
    });
  }

  async function handleConnectionOAuthDisconnect(
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) {
    if (rejectForeignInstallConnection(component, row)) {
      return;
    }
    if (!activePageProjectId) {
      return;
    }
    const connectionId = stringFrom(row.connection_id);
    if (!connectionId) {
      return;
    }
    // Reversible, but reconnecting means authorizing again: Inline confirm,
    // beside the account, rather than a dialog.
    setPendingDisconnect({ connectionId, pending: false, error: "" });
  }

  async function confirmConnectionOAuthDisconnect(
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) {
    const connectionId = stringFrom(row.connection_id);
    if (!activePageProjectId || !connectionId) {
      return;
    }
    const extensionKey = detailExtension?.extension_id || "";
    setPendingDisconnect({ connectionId, pending: true, error: "" });
    await withBusy(extensionKey, async () => {
      try {
        await disconnectProjectConnectionOAuth(activePageProjectId, connectionId);
        await projectConnectionsQuery.refetch();
        clearComponentError(component);
        setPendingDisconnect(null);
        setStatusById((current) => ({
          ...current,
          [extensionKey]: "OAuth account disconnected.",
        }));
      } catch (error) {
        setPendingDisconnect({
          connectionId,
          pending: false,
          error: error instanceof Error ? error.message : t("react.extensions.failed_to_disconnect_oauth_account", "Failed to disconnect OAuth account."),
        });
      }
    });
  }

  async function handleConnectionSave(
    component: Record<string, unknown>,
    values: Record<string, unknown>,
  ) {
    if (!detailExtension?.extension_id || !activePageProjectId) {
      setComponentError(component, t("react.extensions.select_a_project_before_configuring_this_extension", "Select a project before configuring this extension."));
      return;
    }
    const extensionKey = detailExtension.extension_id;
    const componentId = stringFrom(component.component_id || component.id || component.key);
    await withBusy(extensionKey, async () => {
      let createdConnectionId = "";
      let oauthStartAttempted = false;
      try {
        const preparation = prepareConnectionSave({
          component,
          connectionMatchesCurrentInstall,
          oauthConnectionTypes,
          projectConnections: projectConnectionsQuery.data,
          values,
        }, t);
        const fieldValues = await runConnectionPreSaveActions({
          componentId,
          confirmPreSave: (request) => requestConfirmation({
            title: request.title,
            consequence: request.message,
            confirmLabel: request.confirmLabel,
            pendingLabel: request.confirmLabel,
            irreversible: false,
            tone: "primary",
          }),
          data: preparation.data,
          extensionKey,
          initialFieldValues: preparation.fieldValues,
          pageId: stringFrom(activeManifestPage?.page_id || activeManifestPage?.id || activeManifestPage?.key),
          preparation,
          projectId: activePageProjectId,
          reportStatus: (status) => setStatusById((current) => ({
            ...current,
            [extensionKey]: status,
          })),
          saveMappedValues: (mappedValues) => setFormValuesByComponent((current) => ({
            ...current,
            [componentId]: {
              ...objectOrEmpty(current[componentId]),
              ...mappedValues,
            },
          })),
        }, t);
        const payload = connectionPayloadForSave({
          component,
          extensionInstallId,
          extensionKey,
          fieldValues,
          preparation,
        });
        const connectionId = await persistPreparedConnection({
          onAuthorization: continueOAuthAuthorization,
          onConnectionCreated: (createdId) => { createdConnectionId = createdId; },
          onOAuthStart: () => { oauthStartAttempted = true; },
          payload,
          preparation,
          projectId: activePageProjectId,
          returnPath: oauthReturnPath(),
        }, t);
        await projectConnectionsQuery.refetch();
        clearComponentError(component);
        setStatusById((current) => ({
          ...current,
          [extensionKey]: choose(Boolean(preparation.existing), "Connection saved.", "Connection created."),
        }));
        if (anyTrue([Boolean(preparation.existing), !preparation.oauthConnection])) {
          handleConnectionSelect(component, {
            ...payload,
            connection_id: connectionId,
          });
        }
      } catch (error) {
        await recoverFailedConnectionSave({
          connectionId: createdConnectionId,
          oauthStartAttempted,
          projectId: activePageProjectId,
          refetch: projectConnectionsQuery.refetch,
        });
        setComponentError(
          component,
          connectionSaveErrorMessage(error, {
            oauthStartAttempted,
          }, t),
          errorFieldPath(error),
        );
      }
    });
  }

  async function handleConnectionDelete(
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) {
    if (rejectForeignInstallConnection(component, row)) {
      return;
    }
    if (!activePageProjectId) {
      return;
    }
    const connectionId = stringFrom(row.connection_id);
    const oauthConnection = oauthConnectionTypes.includes(stringFrom(row.connection_type));
    if (!connectionId) {
      return;
    }
    const projectId = activePageProjectId;
    const extensionKey = detailExtension?.extension_id || "";
    await requestConfirmation({
      title: oauthConnection
        ? t("react.extensions.remove_external_account_value0", "Remove external account {value0}?", { value0: connectionAccountTarget(row, t) })
        : t("react.extensions.delete_connection_value0", "Delete connection {value0}?", { value0: connectionAccountTarget(row, t) }),
      consequence: oauthConnection
        ? t("react.extensions.display_clears_oauth_access", "This clears its OAuth access from this project.")
        : t("react.extensions.display_connection_removed_from_project", "The connection is removed from this project."),
      confirmLabel: oauthConnection ? t("react.extensions.remove_account", "Remove account") : t("react.registry.delete_connection", "Delete connection"),
      pendingLabel: oauthConnection ? t("react.extensions.removing_account", "Removing account…") : t("react.extensions.deleting_connection", "Deleting connection…"),
      irreversible: true,
      tone: "danger",
      failure: t("react.extensions.display_failed_to_delete_connection", "Failed to delete connection."),
      run: () => withBusy(extensionKey, async () => {
        await deleteProjectConnection(projectId, connectionId);
        await projectConnectionsQuery.refetch();
        handleConnectionSelect(component, null);
        clearComponentError(component);
      }),
    });
  }

  function handleProjectStateSelect(
    component: Record<string, unknown>,
    row: Record<string, unknown> | null,
  ) {
    const componentId = stringFrom(component.component_id || component.id || component.key);
    const defaults = objectOrEmpty(component.defaults);
    if (!row) {
      setFormValuesByComponent((current) => ({ ...current, [componentId]: defaults }));
      clearComponentError(component);
      return;
    }
    const data = objectOrEmpty(component.data);
    const prefix = stringFrom(data.state_key_prefix || component.state_key_prefix);
    const stateKey = stringFrom(row.state_key);
    const keyField = stringFrom(data.key_field, "state_key");
    const value = objectOrEmpty(row.value_jsonb);
    setFormValuesByComponent((current) => ({
      ...current,
      [componentId]: {
        ...defaults,
        ...value,
        state_key: stateKey,
        [keyField]: stateKey.startsWith(prefix) ? stateKey.slice(prefix.length) : stateKey,
        revision: row.revision,
      },
    }));
    clearComponentError(component);
  }

  async function handleProjectStateSave(
    component: Record<string, unknown>,
    values: Record<string, unknown>,
  ) {
    if (!detailExtension?.extension_id || !activePageProjectId) {
      setComponentError(component, t("react.extensions.select_a_project_before_editing_extension_state", "Select a project before editing extension state."));
      return;
    }
    const extensionKey = detailExtension.extension_id;
    await withBusy(extensionKey, async () => {
      try {
        const data = objectOrEmpty(component.data);
        const namespace = stringFrom(data.namespace || component.namespace);
        const prefix = stringFrom(data.state_key_prefix || component.state_key_prefix);
        const keyField = stringFrom(data.key_field, "state_key");
        const keyValue = stringFrom(values[keyField]);
        if (!namespace || !keyValue || /[\r\n/]/.test(keyValue) || /\s/.test(keyValue)) {
          throw new Error(t("react.extensions.state_key_is_required_and_must_not_contain_spaces_slashes_or_newlines", "State key is required and must not contain spaces, slashes, or newlines."));
        }
        const stateKey = stringFrom(values.state_key) || `${prefix}${keyValue}`;
        const valueJson: Record<string, unknown> = {};
        for (const field of listFromUnknown(component.fields)) {
          const path = manifestFieldPath(field);
          if (!path || path === keyField) {
            continue;
          }
          const value = manifestFieldValue(field, values);
          if (value !== undefined) {
            valueJson[path] = value;
          }
          if (field.required && value === undefined) {
            throw new Error(t("react.workflows.value0_is_required", "{value0} is required.", { value0: stringFrom(field.label || path, t("react.extensions.required_field", "Required field")) }));
          }
        }
        const revision = Number(values.revision);
        await putExtensionProjectState(
          activePageProjectId,
          extensionKey,
          namespace,
          stateKey,
          {
            value_jsonb: valueJson,
            expected_revision: Number.isFinite(revision) && revision > 0 ? revision : null,
          },
        );
        await projectStateQuery.refetch();
        setFormValuesByComponent((current) => ({
          ...current,
          [stringFrom(component.component_id || component.id || component.key)]: objectOrEmpty(component.defaults),
        }));
        clearComponentError(component);
        setStatusById((current) => ({ ...current, [extensionKey]: "Extension state saved." }));
      } catch (error) {
        setComponentError(component, error instanceof Error ? error.message : t("react.extensions.failed_to_save_extension_state", "Failed to save extension state."));
      }
    });
  }

  async function handleProjectStateDelete(
    component: Record<string, unknown>,
    row: Record<string, unknown>,
  ) {
    if (!detailExtension?.extension_id || !activePageProjectId) {
      return;
    }
    const data = objectOrEmpty(component.data);
    const namespace = stringFrom(data.namespace || component.namespace);
    const stateKey = stringFrom(row.state_key);
    if (!namespace || !stateKey) {
      return;
    }
    const projectId = activePageProjectId;
    const extensionKey = detailExtension.extension_id;
    await requestConfirmation({
      title: t("react.extensions.delete_state_value0", "Delete state {value0}?", { value0: stateKey }),
      consequence: t("react.extensions.display_saved_state_removed", "The saved state is removed from this project."),
      confirmLabel: t("react.extensions.delete_state", "Delete state"),
      pendingLabel: t("react.extensions.deleting_state", "Deleting state…"),
      irreversible: true,
      tone: "danger",
      failure: t("react.extensions.display_failed_to_delete_extension_state", "Failed to delete extension state."),
      run: () => withBusy(extensionKey, async () => {
        const revision = Number(row.revision);
        await deleteExtensionProjectState(
          projectId,
          extensionKey,
          namespace,
          stateKey,
          { expectedRevision: Number.isFinite(revision) && revision > 0 ? revision : null },
        );
        await projectStateQuery.refetch();
        handleProjectStateSelect(component, null);
        clearComponentError(component);
      }),
    });
  }

  const oauthCallbackMessage = oauthCallbackMessageFor(
    oauthCallbackNotice,
    oauthCallbackConnection, t
  );
  const showArchiveLifecycleLoading = archiveLifecycleQuery.isPending;
  const showArchiveLifecycleVerificationFailure =
    !showArchiveLifecycleLoading && archiveLifecycleVerificationFailed;
  const showArchiveLifecycleUnavailable =
    !showArchiveLifecycleLoading
    && !archiveLifecycleVerificationFailed
    && archiveLifecycleUnavailable;
  return {
    oauthCallbackMessage,
    showArchiveLifecycleLoading,
    showArchiveLifecycleVerificationFailure,
    showArchiveLifecycleUnavailable,
    clearFieldError,
    handleConnectionSave,
    handleConnectionDelete,
    handleConnectionOAuth,
    handleConnectionOAuthDisconnect,
    handleConnectionSelect,
    handleProjectStateSave,
    handleProjectStateDelete,
    handleProjectStateSelect,
    confirmConnectionOAuthDisconnect,
    pendingDisconnect,
    setPendingDisconnect,
    confirmation,
    confirmationPending,
    confirmationError,
    cancelConfirmation,
    acceptConfirmation,
  };
}
