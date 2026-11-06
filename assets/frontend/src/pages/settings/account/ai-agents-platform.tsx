import { useId, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { DsAlert, DsBadge, DsDialog, DsToast, DsValueChip, RequiredLabel } from "@/components/shared/design-system";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { createPlatformClient, fetchPlatformClients, revokePlatformClient } from "@/pages/settings/shared/api";
import type { AgentSetupStep, PlatformClient, PlatformClientsView } from "@/pages/settings/shared/types";

const PLATFORM_CLIENTS_QUERY_KEY = ["settings", "account", "agent-access", "platform-clients"] as const;

type Feedback = { tone: "success" | "danger"; message: string };

function downloadBlob(fileName: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
}

/** The newest registration of the preset that still works. */
function activeClient(view: PlatformClientsView | undefined, preset: string): PlatformClient | undefined {
  return (view?.clients || []).filter((client) => client.preset === preset && !client.revoked_at).at(-1);
}

/**
 * A `platform` step (ADR 0061): an administrator registers the platform's OAuth
 * client here, copies the values the platform's own registration asks for, pastes
 * back the registration ID it returns, and downloads the agent package. Everyone
 * else sees who does this step.
 */
export function PlatformClientStep({
  mcpUrl,
  packageVersion,
  step,
}: Readonly<{ mcpUrl: string; packageVersion: string; step: AgentSetupStep }>) {
  const { t } = useI18n();
  const isAdmin = useIsAdmin();
  const queryClient = useQueryClient();
  const referenceFieldId = useId();
  const preset = step.platform_preset || "";
  const clientsQuery = useQuery({ queryKey: PLATFORM_CLIENTS_QUERY_KEY, queryFn: fetchPlatformClients, enabled: isAdmin, retry: false });
  const [busy, setBusy] = useState(false);
  const [confirmingRevoke, setConfirmingRevoke] = useState(false);
  const [referenceId, setReferenceId] = useState("");
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [actionError, setActionError] = useState("");

  if (!isAdmin) {
    return (
      <DsAlert title={t("react.agent_access.platform_admin_only_title", "An administrator does this step")}>
        {t("react.agent_access.platform_admin_only_copy", "Ask a Flow Steward administrator to register Microsoft 365 on this page and build the agent. Once your organisation has added the agent, open it in Copilot and sign in to Flow Steward when it asks.")}
      </DsAlert>
    );
  }
  if (clientsQuery.isError) {
    return <DsAlert title={t("react.agent_access.platform_unavailable", "The Microsoft 365 registration could not be loaded")} tone="danger">{t("react.settings.check_the_connection_and_try_again", "Check the connection and try again.")}</DsAlert>;
  }
  if (!clientsQuery.data) return <p className="fs-field-helper">{t("react.agent_access.platform_loading", "Loading the registration…")}</p>;

  const view = clientsQuery.data;
  const client = activeClient(view, preset);
  const portal = view.portal;
  const presetInfo = view.presets.find((item) => item.id === preset);
  const revokedCount = view.clients.filter((item) => item.preset === preset && item.revoked_at).length;

  function show(next: Feedback) {
    setFeedback(next);
    globalThis.setTimeout(() => setFeedback(null), 2400);
  }

  async function copy(text: string) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
      await navigator.clipboard.writeText(text);
      show({ tone: "success", message: t("react.agent_access.copied", "Copied.") });
    } catch {
      show({ tone: "danger", message: t("react.agent_access.copy_failed", "Could not copy. Select the text and copy it manually.") });
    }
  }

  async function run(action: () => Promise<PlatformClientsView>) {
    setBusy(true);
    setActionError("");
    try {
      queryClient.setQueryData(PLATFORM_CLIENTS_QUERY_KEY, await action());
      return true;
    } catch (error) {
      setActionError(error instanceof Error ? error.message : t("react.agent_access.platform_action_failed", "The change was not saved."));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function revoke(clientId: string) {
    if (await run(() => revokePlatformClient(clientId))) {
      setConfirmingRevoke(false);
      setReferenceId("");
    }
  }

  async function download(clientId: string) {
    try {
      // Loaded on demand: the package files are bundled only into this chunk.
      const { buildM365Package } = await import("@/pages/settings/account/m365-agent-package");
      downloadBlob(step.file_name, buildM365Package({ mcpUrl, clientId, referenceId }, packageVersion));
      show({ tone: "success", message: t("react.agent_access.downloaded", "Downloaded {name}.", { name: step.file_name }) });
    } catch {
      show({ tone: "danger", message: t("react.agent_access.download_failed", "The file could not be built. Reload the page and try again.") });
    }
  }

  const rows: Array<{ label: string; value: string; copyable: boolean }> = client && portal ? [
    { label: t("react.agent_access.portal_client_id", "Client ID"), value: client.client_id, copyable: true },
    { label: t("react.agent_access.portal_base_url", "Base URL"), value: portal.base_url, copyable: true },
    { label: t("react.agent_access.portal_authorization_endpoint", "Authorization endpoint"), value: portal.authorization_endpoint, copyable: true },
    { label: t("react.agent_access.portal_token_endpoint", "Token endpoint"), value: portal.token_endpoint, copyable: true },
    { label: t("react.agent_access.portal_refresh_endpoint", "Refresh endpoint"), value: portal.refresh_endpoint, copyable: true },
    { label: t("react.agent_access.portal_scope", "Scope"), value: portal.scope, copyable: true },
    { label: t("react.agent_access.portal_client_secret", "Client secret"), value: t("react.agent_access.portal_leave_empty", "Leave empty"), copyable: false },
    { label: t("react.agent_access.portal_pkce", "Enable PKCE"), value: t("react.agent_access.portal_on", "On"), copyable: false },
    { label: t("react.agent_access.portal_restrict_app", "Restrict usage by app"), value: t("react.agent_access.portal_any_app", "Any Teams app"), copyable: false },
  ] : [];
  const referenceReady = /^[A-Za-z0-9+/=._-]{8,512}$/.test(referenceId.trim());

  return (
    <div className="fs-panel-stack">
      {client ? null : (
        <div className="fs-panel-group">
          <p className="fs-field-helper">{t("react.agent_access.platform_create_help", "Flow Steward gives Microsoft 365 its own client ID. Microsoft returns people only to its documented address, and nobody can type another one.")}</p>
          {presetInfo ? <p className="fs-field-helper">{t("react.agent_access.platform_redirect", "Microsoft returns people to:")} <DsValueChip>{presetInfo.redirect_uris.join(", ")}</DsValueChip></p> : null}
          <div className="fs-footer-actions">
            <button className="fs-button primary" disabled={busy || !portal} onClick={() => ignoreAsyncResult(run(() => createPlatformClient(preset)))} type="button">{busy ? t("react.settings.creating", "Creating…") : t("react.agent_access.platform_create", "Create the Microsoft 365 client")}</button>
          </div>
          {revokedCount ? <span className="fs-field-helper">{t("react.agent_access.platform_revoked_count", "Revoked earlier: {value1}", { value1: String(revokedCount) })}</span> : null}
        </div>
      )}
      {portal ? null : <DsAlert title={t("react.agent_access.platform_not_ready_title", "Cloud AI apps are not ready")} tone="warning">{t("react.agent_access.platform_not_ready_copy", "Fix what step 3 names first: Microsoft needs this installation's public HTTPS address.")}</DsAlert>}
      {client && portal ? (
        <>
          <div className="fs-panel-group">
            <h4 className="fs-panel-title">{t("react.agent_access.portal_title", "In the Teams Developer Portal")}</h4>
            <p className="fs-field-helper">{t("react.agent_access.portal_help", "Open Tools > OAuth client registration > New OAuth client registration and enter these values. Choose your organisation under Restrict usage by org.")}</p>
            <dl className="fs-form-grid">
              {rows.map((row) => (
                <div className="fs-field full" key={row.label}>
                  <dt className="fs-field-label">{row.label}</dt>
                  <dd className="fs-footer-actions">
                    <DsValueChip>{row.value}</DsValueChip>
                    {row.copyable ? <button aria-label={t("react.agent_access.copy_value", "Copy {name}", { name: row.label })} className="fs-button ghost" onClick={() => ignoreAsyncResult(copy(row.value))} type="button">{t("react.agent_access.copy", "Copy")}</button> : null}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
          <div className="fs-panel-group">
            <label className="fs-field" htmlFor={referenceFieldId}>
              <RequiredLabel>{t("react.agent_access.reference_id", "OAuth client registration ID")}</RequiredLabel>
            </label>
            <input autoComplete="off" className="fs-input" id={referenceFieldId} onChange={(event) => setReferenceId(event.target.value)} spellCheck={false} value={referenceId} />
            <span className="fs-field-helper">{t("react.agent_access.reference_id_help", "The Developer Portal shows it after you save the registration. It is not a secret; it tells Microsoft which registration the agent signs in with.")}</span>
            <div className="fs-footer-actions">
              <button className="fs-button primary" disabled={!referenceReady} onClick={() => ignoreAsyncResult(download(client.client_id))} type="button">{t("react.agent_access.download_file", "Download {name}", { name: step.file_name })}</button>
            </div>
          </div>
          <div className="fs-panel-group">
            <span className="fs-footer-actions">
              <DsBadge tone="green">{t("account_settings.status.active", "Active")}</DsBadge>
              <span className="fs-field-helper">{t("react.agent_access.platform_revoke_help", "Revoking ends every Microsoft 365 sign-in to this installation at once. Create a new client and a new registration to connect again.")}</span>
            </span>
            <div className="fs-footer-actions">
              <button className="fs-button danger" disabled={busy} onClick={() => setConfirmingRevoke(true)} type="button">{t("react.agent_access.platform_revoke", "Revoke the Microsoft 365 client")}</button>
            </div>
          </div>
          <DsDialog busy={busy} closeLabel={t("react.settings.close_revoke_confirmation", "Close revoke confirmation")} description={t("react.agent_access.platform_revoke_help", "Revoking ends every Microsoft 365 sign-in to this installation at once. Create a new client and a new registration to connect again.")} eyebrow={t("react.agent_access.title", "AI agents")} footer={<><button className="fs-button" disabled={busy} onClick={() => setConfirmingRevoke(false)} type="button">{t("common.cancel", "Cancel")}</button><button className="fs-button danger" disabled={busy} onClick={() => ignoreAsyncResult(revoke(client.client_id))} type="button">{busy ? t("react.settings.revoking", "Revoking…") : t("react.agent_access.platform_revoke", "Revoke the Microsoft 365 client")}</button></>} onClose={() => setConfirmingRevoke(false)} open={confirmingRevoke} title={t("react.agent_access.platform_revoke_confirm", "Revoke the Microsoft 365 client?")} variant="confirm">
            <DsAlert tone="danger" title={t("react.settings.immediate_access_loss", "Immediate access loss")}>{actionError || t("react.agent_access.platform_revoke_copy", "Everyone using the agent loses access now, and the agent package stops working.")}</DsAlert>
          </DsDialog>
        </>
      ) : null}
      {actionError && !confirmingRevoke ? <DsAlert title={t("react.agent_access.platform_action_failed", "The change was not saved.")} tone="danger">{actionError}</DsAlert> : null}
      {feedback ? <DsToast tone={feedback.tone}>{feedback.message}</DsToast> : null}
    </div>
  );
}
