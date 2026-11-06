import { useState } from "react";

import { useQuery } from "@tanstack/react-query";

import { DsAlert, DsBadge, DsDialog, DsToast } from "@/components/shared/design-system";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import type { ArchiveLayout } from "@/pages/settings/account/agent-package-archive";
import { PlatformClientStep } from "@/pages/settings/account/ai-agents-platform";
import { fetchAgentAccessToken, revokePersonalToken } from "@/pages/settings/shared/api";
import type { AgentAccessToken, AgentSetupStep } from "@/pages/settings/shared/types";

type Translate = I18nContextValue["t"];

const TOKEN_PLACEHOLDER = "{token}";

/** The step text with the secret, or a visible placeholder when there is none. */
function fillToken(content: string, secret: string, placeholder: string): string {
  return content.split(TOKEN_PLACEHOLDER).join(secret || placeholder);
}

function formatDay(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString();
}

function downloadBlob(fileName: string, blob: Blob) {
  // A blob handle, never the content in a URL; the file never leaves this browser.
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
}

function stepTitle(step: AgentSetupStep, t: Translate) {
  return t(step.title_key, step.title, step.values);
}

function stepDescription(step: AgentSetupStep, t: Translate) {
  return step.description_key ? t(step.description_key, step.description, step.values) : "";
}

/** Steps 4 and 5 of the wizard: what to do in the client, as the server described it. */
export function AgentSetupSteps({
  packageId = "",
  packageVersion = "",
  secret,
  steps,
}: Readonly<{ packageId?: string; packageVersion?: string; secret: string; steps: AgentSetupStep[] }>) {
  const { t } = useI18n();
  const [feedback, setFeedback] = useState<{ tone: "success" | "danger"; message: string } | null>(null);
  const placeholder = t("react.agent_access.token_placeholder", "<your personal token>");

  async function copy(text: string) {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard API unavailable");
      await navigator.clipboard.writeText(text);
      setFeedback({ tone: "success", message: t("react.agent_access.copied", "Copied.") });
    } catch {
      setFeedback({ tone: "danger", message: t("react.agent_access.copy_failed", "Could not copy. Select the text and copy it manually.") });
    }
    globalThis.setTimeout(() => setFeedback(null), 2400);
  }

  async function downloadArchive(step: AgentSetupStep) {
    try {
      // Loaded on demand: the package files are bundled only into this chunk.
      const { buildArchive } = await import("@/pages/settings/account/agent-package-archive");
      downloadBlob(step.file_name, buildArchive(step.archive_layout as ArchiveLayout, packageId, { servers: fillToken(step.content, secret, ""), manifestDir: step.manifest_dir }));
      setFeedback({ tone: "success", message: t("react.agent_access.downloaded", "Downloaded {name}.", { name: step.file_name }) });
    } catch {
      setFeedback({ tone: "danger", message: t("react.agent_access.download_failed", "The file could not be built. Reload the page and try again.") });
    }
    globalThis.setTimeout(() => setFeedback(null), 2400);
  }

  return (
    <ol className="fs-panel-stack">
      {steps.map((step) => {
        const description = stepDescription(step, t);
        const text = fillToken(step.content, secret, placeholder);
        const waitsForToken = step.uses_token && !secret;
        if (step.kind === "notice") {
          return <li key={step.id}><DsAlert title={stepTitle(step, t)}>{description}</DsAlert></li>;
        }
        return (
          <li className="fs-panel-group" key={step.id}>
            <h3 className="fs-panel-title">{stepTitle(step, t)}</h3>
            {description ? <p className="fs-field-helper">{description}</p> : null}
            {step.kind === "platform" ? (
              <PlatformClientStep mcpUrl={step.content} packageVersion={packageVersion} step={step} />
            ) : step.kind === "package" ? (
              <>
                {waitsForToken ? <span className="fs-field-helper">{t("react.agent_access.package_after_token", "The download is ready once the token is created.")}</span> : null}
                <div className="fs-footer-actions">
                  <button className="fs-button primary" disabled={waitsForToken} onClick={() => ignoreAsyncResult(downloadArchive(step))} type="button">{t("react.agent_access.download_file", "Download {name}", { name: step.file_name })}</button>
                </div>
              </>
            ) : step.kind === "link" ? (
              <div className="fs-footer-actions">
                <a className="fs-button" href={step.url} rel="noreferrer noopener" target="_blank">
                  {step.url.includes("/releases/") ? t("react.agent_access.open_release", "Open the release") : t("react.agent_access.open_readme", "Open the README")}
                </a>
              </div>
            ) : (
              <>
                <pre aria-label={stepTitle(step, t)} className="fs-log-output">{text}</pre>
                {waitsForToken ? <span className="fs-field-helper">{t("react.agent_access.token_appears_here", "The token appears here once it is created. It is shown only once.")}</span> : null}
                <div className="fs-footer-actions">
                  <button className="fs-button" disabled={waitsForToken} onClick={() => ignoreAsyncResult(copy(text))} type="button">{t("react.agent_access.copy", "Copy")}</button>
                  {step.kind === "file" && step.file_name ? (
                    <button className="fs-button ghost" disabled={waitsForToken} onClick={() => downloadBlob(step.file_name, new Blob([text], { type: "text/plain;charset=utf-8" }))} type="button">{t("react.agent_access.download_file", "Download {name}", { name: step.file_name })}</button>
                  ) : null}
                </div>
              </>
            )}
          </li>
        );
      })}
      {feedback ? <DsToast tone={feedback.tone}>{feedback.message}</DsToast> : null}
    </ol>
  );
}

function tokenState(token: AgentAccessToken, t: Translate = fallbackContext.t) {
  if (token.revoked) return { tone: "neutral" as const, label: t("react.settings.revoked", "Revoked") };
  if (token.expired || (token.expires_at && new Date(token.expires_at).getTime() < Date.now())) {
    return { tone: "neutral" as const, label: t("react.settings.expired", "Expired") };
  }
  return { tone: "green" as const, label: t("account_settings.status.active", "Active") };
}

/**
 * The facts the token record holds: expiry, scopes, projects and last use. Only that
 * the token was used, never which client used it; copying or downloading the
 * settings is not a connection.
 */
export function AgentTokenUsage({
  onRevoked,
  tokenId,
}: Readonly<{ onRevoked: () => void; tokenId: string }>) {
  const { t } = useI18n();
  const tokenQuery = useQuery({
    queryKey: ["settings", "account", "agent-access", "token", tokenId],
    queryFn: () => fetchAgentAccessToken(tokenId),
    retry: false,
  });
  const [confirming, setConfirming] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState("");
  const token = tokenQuery.data?.token;

  async function revoke() {
    setRevoking(true);
    setRevokeError("");
    try {
      await revokePersonalToken(tokenId);
      setConfirming(false);
      await tokenQuery.refetch();
      onRevoked();
    } catch (error) {
      setRevokeError(error instanceof Error ? error.message : t("react.agent_access.revoke_failed", "The token could not be revoked."));
    } finally {
      setRevoking(false);
    }
  }

  if (tokenQuery.isError) {
    return <DsAlert title={t("react.agent_access.token_status_unavailable", "The token's state could not be loaded")} tone="warning">{t("react.settings.check_the_connection_and_try_again", "Check the connection and try again.")}</DsAlert>;
  }
  if (!token) return <p className="fs-field-helper">{t("react.agent_access.loading_token", "Loading the token's state…")}</p>;
  const state = tokenState(token, t);
  const projects = token.allowed_project_ids || [];
  return (
    <div className="fs-panel-group">
      <dl className="fs-form-grid">
        <div className="fs-field"><dt className="fs-field-label">{t("account_settings.card.status", "Status")}</dt><dd><DsBadge tone={state.tone}>{state.label}</DsBadge></dd></div>
        <div className="fs-field"><dt className="fs-field-label">{t("react.settings.last_used", "Last used")}</dt><dd>{token.last_used_at ? t("react.oauth.access_used_on", "Access used {value1}", { value1: formatDay(token.last_used_at) }) : t("react.oauth.not_used_yet", "Not used yet")}</dd></div>
        <div className="fs-field"><dt className="fs-field-label">{t("account.tokens.expires", "Expires")}</dt><dd>{formatDay(token.expires_at)}</dd></div>
        <div className="fs-field"><dt className="fs-field-label">{t("react.oauth.column_projects", "Projects")}</dt><dd>{projects.length ? t("react.oauth.project_count", "{value1} projects", { value1: String(projects.length) }) : t("react.oauth.all_projects", "All projects within your access")}</dd></div>
        <div className="fs-field full"><dt className="fs-field-label">{t("react.settings.scopes", "Scopes")}</dt><dd><code className="fs-value-chip fs-settings-wrap">{(token.scopes || []).join(", ")}</code></dd></div>
      </dl>
      {token.last_used_at ? null : <span className="fs-field-helper">{t("react.agent_access.not_used_help", "Copying or downloading the settings does not count. The time appears after the first request with this token.")}</span>}
      <div className="fs-footer-actions">
        <button className="fs-button" disabled={tokenQuery.isFetching} onClick={() => ignoreAsyncResult(tokenQuery.refetch())} type="button">{t("react.agent_access.check_again", "Check again")}</button>
        {token.revoked ? null : <button className="fs-button danger" onClick={() => setConfirming(true)} type="button">{t("react.settings.revoke_token", "Revoke token")}</button>}
      </div>
      <DsDialog busy={revoking} closeLabel={t("react.settings.close_revoke_confirmation", "Close revoke confirmation")} description={t("react.settings.clients_using_this_token_lose_access_immediately_this_action_cannot_be_undone", "Clients using this token lose access immediately. This action cannot be undone.")} eyebrow={t("react.agent_access.title", "AI agents")} footer={<><button className="fs-button" disabled={revoking} onClick={() => setConfirming(false)} type="button">{t("common.cancel", "Cancel")}</button><button className="fs-button danger" disabled={revoking} onClick={() => ignoreAsyncResult(revoke())} type="button">{revoking ? t("react.settings.revoking", "Revoking…") : t("react.settings.revoke_token", "Revoke token")}</button></>} onClose={() => setConfirming(false)} open={confirming} title={t("react.settings.revoke_value1", "Revoke “{value1}”?", { value1: token.label || t("react.settings.token_lowercase", "token") })} variant="confirm">
        <DsAlert tone="danger" title={t("react.settings.immediate_access_loss", "Immediate access loss")}>{revokeError || t("react.settings.confirm_only_after_the_client_has_another_credential", "Confirm only after the client has another credential.")}</DsAlert>
      </DsDialog>
    </div>
  );
}
