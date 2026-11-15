import { useLayoutEffect, useRef, useState } from "react";
import { useI18n } from "@/lib/i18n/useI18n";
import { isHttpError } from "@/lib/http/errors";
import { createExtensionCallbackRegistration, refreshExtensionCallbackStatus } from "./api";
import { listFromUnknown, objectOrEmpty, stringFrom } from "./extension-manifest-utils";
import { pageIdentity } from "./extension-locale";
import {
  callbackSetupCredentialKey,
  callbackSetupRegistration,
  callbackSetupTarget,
  type CallbackSetupCredentials,
} from "./callback-setup-scope";
import type { useExtensionsHubState } from "./use-extensions-hub-state";
import type { useExtensionLifecycleActions } from "./use-extension-lifecycle-actions";

export function useExtensionCallbackSetup(
  hub: ReturnType<typeof useExtensionsHubState>,
  lifecycle: ReturnType<typeof useExtensionLifecycleActions>,
) {
  const { t } = useI18n();
  const extensionId = hub.detailExtension?.extension_id || "";
  const canManageCallbacks = Boolean(hub.callbackOwnerScopeKey) && hub.canManageProject;
  const declarations = listFromUnknown(objectOrEmpty(hub.manifestQuery.data).callbacks);
  const scopeKey = JSON.stringify([
    hub.callbackOwnerScopeKey, extensionId, hub.extensionInstallId,
    hub.activePageProjectId, hub.pageConnection.selectedId,
    pageIdentity(hub.activeManifestPage || {}), hub.canManageProject,
    declarations.map((item) => [item.callback_id, item.connection_scoped]),
    listFromUnknown(hub.activeManifestPage?.components)
      .filter((item) => item.type === "callback_setup")
      .map((item) => [callbackSetupCredentialKey(item, ""), item.setup_mode]),
  ]);
  const [state, setState] = useState<{ scopeKey: string; generation: number; credentials: CallbackSetupCredentials; errors: Record<string, string> }>({
    scopeKey, generation: 0, credentials: {}, errors: {},
  });
  // Adjust during render so the previous scope cannot flash or survive A → B → A.
  if (state.scopeKey !== scopeKey) {
    setState({ scopeKey, generation: state.generation + 1, credentials: {}, errors: {} });
  }
  const generation = state.generation;
  const committed = useRef({ scopeKey, generation, active: false });
  useLayoutEffect(() => {
    committed.current = { scopeKey, generation, active: true };
    return () => { committed.current.active = false; };
  }, [scopeKey, generation]);

  function isCurrent() {
    return canManageCallbacks && committed.current.active && committed.current.scopeKey === scopeKey
      && committed.current.generation === generation;
  }

  function setStatus(message: string) {
    if (isCurrent()) hub.setStatusById((current) => ({ ...current, [extensionId]: message }));
  }

  async function handleCallbackSetup(component: Record<string, unknown>, action: "configure" | "status") {
    if (!isCurrent() || !canManageCallbacks || !extensionId) return;
    const target = callbackSetupTarget(component, declarations, hub.pageConnection.selectedId);
    if (!target.callbackId) return;
    if (!hub.activePageProjectId) {
      setStatus(t("react.extensions.display_select_project_before_callbacks", "Select a project before configuring callbacks."));
      return;
    }
    if (target.connectionRequired && !target.connectionId) {
      setStatus(t("extensions.callback.select_connection", "Select a connection before configuring callbacks."));
      return;
    }
    const setupKey = callbackSetupCredentialKey(component, target.connectionId);
    function setSetupError(message?: string) {
      if (!isCurrent()) return;
      setState((current) => {
        if (current.scopeKey !== scopeKey || current.generation !== generation) return current;
        const errors = { ...current.errors };
        if (message) errors[setupKey] = message;
        else delete errors[setupKey];
        return { ...current, errors };
      });
    }
    await lifecycle.withBusy(extensionId, async () => {
      if (!isCurrent()) return;
      setSetupError();
      try {
        if (action === "status") {
          if (!target.manual) {
            const registration = callbackSetupRegistration(hub.callbacksQuery.data || [], target.callbackId, target.connectionId);
            if (!registration?.id) {
              setStatus(t("react.extensions.display_create_callback_before_refresh", "Create the callback registration before refreshing webhook status."));
              return;
            }
            setStatus(t("react.extensions.display_refreshing_webhook_status", "Refreshing webhook status..."));
            const payload = await refreshExtensionCallbackStatus(extensionId, registration.id, {
              action_id: stringFrom(component.status_action_id || "get_webhook_status"),
              runtime: { fake_mode: false },
            });
            if (!isCurrent()) return;
            hub.setCallbackStatuses((current) => ({ ...current, [registration.id]: objectOrEmpty(payload.webhook_status) }));
          }
          if (!isCurrent()) return;
          await hub.callbacksQuery.refetch({ throwOnError: true });
          setStatus(target.manual
            ? t("extensions.callback.refreshed", "Callback status refreshed.")
            : t("react.extensions.display_webhook_status_refreshed", "Webhook status refreshed."));
          return;
        }
        setStatus(t("react.extensions.display_creating_callback_registration", "Creating callback registration..."));
        const registration = await createExtensionCallbackRegistration(extensionId, target.callbackId, {
          project_id: hub.activePageProjectId,
          ...(target.connectionRequired ? { connection_id: target.connectionId } : {}),
          enabled: true,
          configure: !target.manual,
          ...(!target.manual ? { configure_action_id: stringFrom(component.configure_action_id || "configure_webhook") } : {}),
        });
        if (!isCurrent()) return;
        if (target.manual && registration.callback_url) {
          const key = callbackSetupCredentialKey(component, target.connectionId);
          const credential = {
            callbackUrl: registration.callback_url,
            ...(registration.callback_secret ? { verifyToken: registration.callback_secret } : {}),
          };
          setState((current) => current.scopeKey === scopeKey && current.generation === generation
            ? { ...current, credentials: { ...current.credentials, [key]: credential } } : current);
        }
        if (!target.manual) {
          hub.setCallbackStatuses((current) => ({ ...current, [registration.id]: objectOrEmpty(registration.webhook_status) }));
        }
        await hub.callbacksQuery.refetch({ throwOnError: true });
        setStatus(target.manual
          ? t("extensions.callback.created", "Callback registered. Copy the one-time setup values before leaving this page.")
          : t("react.extensions.display_callback_registered_webhook_configured", "Callback registered and webhook configured."));
      } catch (error) {
        if (!isCurrent()) return;
        if (!target.manual && isHttpError(error)) {
          const payload = objectOrEmpty(error.payload);
          const registrationId = stringFrom(payload.id);
          if (registrationId) {
            hub.setCallbackStatuses((current) => ({ ...current, [registrationId]: objectOrEmpty(payload.webhook_status) }));
          }
          await hub.queryClient.invalidateQueries({ queryKey: ["extensions", "callbacks", extensionId, hub.activePageProjectId] });
        }
        // Callback responses may contain credentials; never reflect their error payloads.
        const failure = target.manual
          ? t("extensions.callback.failed", "Callback setup failed. Refresh the status and try again.")
          : t("react.extensions.display_webhook_configuration_failed", "Webhook configuration failed.");
        setSetupError(failure);
        setStatus(failure);
      }
    });
  }

  return {
    handleCallbackSetup,
    canManageCallbacks,
    callbackSetupCredentials: state.scopeKey === scopeKey && canManageCallbacks ? state.credentials : {},
    callbackSetupErrors: state.scopeKey === scopeKey && canManageCallbacks ? state.errors : {},
  };
}
