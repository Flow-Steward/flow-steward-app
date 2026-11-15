import { useState } from "react";
import { DsAlert, DsBadge } from "@/components/shared/design-system";
import { Button } from "@/components/ui/button";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { objectOrEmpty, stringFrom } from "./extension-manifest-utils";
import {
  manifestActionBusy, manifestActionKey, platformText, pressManifestAction,
  webhookConfigurationLabel, type ManifestRenderContext,
} from "./extension-manifest-render-model";
import { callbackSetupCredentialKey, callbackSetupRegistration, callbackSetupTarget } from "./callback-setup-scope";

export function ExtensionCallbackSetup({ component, context, title, componentId, t = fallbackContext.t }: {
  component: Record<string, unknown>;
  context: ManifestRenderContext;
  title: string;
  componentId: string;
  t?: I18nContextValue["t"];
}) {
  const [copyFailed, setCopyFailed] = useState(false);
  const target = callbackSetupTarget(component, context.callbackDeclarations || [], context.selectedConnectionId);
  if (!context.canManageProject || context.canManageCallbacks === false) {
    return (
      <div className="fs-manifest-block">
        <div>
          <p className="fs-manifest-title">{title}</p>
          <p className="fs-manifest-meta fs-manifest-code">{target.callbackId || "callback"}</p>
          <p className="fs-manifest-description">{t("react.extensions.callback_setup_is_managed_by_project_managers", "Callback setup is managed by project managers.")}</p>
        </div>
      </div>
    );
  }
  const unavailable = !target.callbackId || !context.projectId || (target.connectionRequired && !target.connectionId);
  const registration = unavailable ? undefined : callbackSetupRegistration(context.callbacks, target.callbackId, target.connectionId);
  const registered = Boolean(registration?.id && registration.enabled);
  const credential = !unavailable && target.manual
    ? context.callbackSetupCredentials?.[callbackSetupCredentialKey(component, target.connectionId)] : undefined;
  const setupError = !unavailable
    ? context.callbackSetupErrors?.[callbackSetupCredentialKey(component, target.connectionId)] : undefined;
  const verifyToken = credential?.verifyToken;
  const webhookStatus = objectOrEmpty(registration?.webhook_status || context.callbackStatuses[stringFrom(registration?.id)]);
  const lastError = stringFrom(registration?.last_error);
  const webhookError = !target.manual && stringFrom(webhookStatus.last_error_message);

  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopyFailed(false);
    } catch {
      setCopyFailed(true);
    }
  }

  return (
    <div className="fs-manifest-block">
      <div className="fs-manifest-head">
        <div>
          <p className="fs-manifest-title">{title}</p>
          <p className="fs-manifest-meta fs-manifest-code">{target.callbackId || "callback"}</p>
        </div>
        <DsBadge tone={registered ? "green" : "neutral"}>
          {registered ? t("react.extensions.registered", "registered") : t("react.extensions.not_registered", "not registered")}
        </DsBadge>
      </div>
      <dl className="fs-kv">
        <div className="fs-kv-row">
          <dt>{platformText(context, "extensions.callback.url", "Callback URL")}</dt>
          <dd>{credential?.callbackUrl || stringFrom(registration?.callback_url) || "/webhook/extensions/.../<redacted>"}</dd>
        </div>
        {credential?.verifyToken ? <div className="fs-kv-row">
          <dt>{platformText(context, "extensions.callback.verify_token", "Verify token")}</dt>
          <dd>{credential.verifyToken}</dd>
        </div> : null}
        <div className="fs-kv-row"><dt>{platformText(context, "extensions.callback.last_seen", "Last seen")}</dt><dd>{stringFrom(registration?.last_seen_at) || t("react.extensions.display_not_seen", "not seen")}</dd></div>
        {!target.manual ? <>
          <div className="fs-kv-row"><dt>{platformText(context, "extensions.callback.webhook", "Webhook")}</dt><dd>{webhookConfigurationLabel(webhookStatus.url_configured, t)}</dd></div>
          <div className="fs-kv-row"><dt>{platformText(context, "extensions.callback.pending", "Pending")}</dt><dd>{stringFrom(webhookStatus.pending_update_count) || t("project_detail.connections.status.unknown", "unknown")}</dd></div>
        </> : null}
      </dl>
      {setupError ? <DsAlert title={setupError} tone="danger" /> : null}
      {lastError ? <DsAlert title={lastError} tone="danger" /> : null}
      {webhookError ? <DsAlert title={webhookError} tone="warning" /> : null}
      {copyFailed ? <DsAlert title={platformText(context, "extensions.callback.copy_failed", "Could not copy. Select and copy the setup value manually.")} tone="warning" /> : null}
      {credential ? <>
        <p className="fs-manifest-description">{platformText(context, "extensions.callback.one_time", "Copy these one-time setup values before leaving this page.")}</p>
        <div className="fs-manifest-actions">
          <Button onClick={() => { void copy(credential.callbackUrl); }} size="sm" type="button" variant="secondary">
            {platformText(context, "extensions.callback.copy_url", "Copy Callback URL")}
          </Button>
          {verifyToken ? <Button onClick={() => { void copy(verifyToken); }} size="sm" type="button" variant="secondary">
            {platformText(context, "extensions.callback.copy_token", "Copy Verify Token")}
          </Button> : null}
        </div>
      </> : null}
      <div className="fs-manifest-actions">
        <Button
          busy={manifestActionBusy(context, manifestActionKey("callback", componentId, "configure"))}
          busyLabel={t("react.settings.creating", "Creating…")}
          disabled={unavailable}
          onClick={() => pressManifestAction(context, manifestActionKey("callback", componentId, "configure"), () => context.onCallbackSetup(component, "configure"))}
          size="sm" type="button" variant="primary"
        >
          {target.manual ? platformText(context, "extensions.callback.create", "Create Callback") : platformText(context, "extensions.callback.configure", "Create and Configure")}
        </Button>
        <Button
          busy={manifestActionBusy(context, manifestActionKey("callback", componentId, "status"))}
          busyLabel={t("react.shared.refreshing", "Refreshing…")}
          disabled={unavailable}
          onClick={() => pressManifestAction(context, manifestActionKey("callback", componentId, "status"), () => context.onCallbackSetup(component, "status"))}
          size="sm" type="button" variant="secondary"
        >
          {platformText(context, "extensions.callback.refresh", "Refresh Status")}
        </Button>
      </div>
    </div>
  );
}
