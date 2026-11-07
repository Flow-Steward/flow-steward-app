import { useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { DsAlert, DsDialog, RowActionMenu } from "@/components/shared/design-system";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { fetchOAuthGrants, revokeOAuthGrant } from "@/pages/settings/shared/api";
import { oauthNotice, oauthPresetLabel } from "@/pages/settings/shared/oauth-copy";
import type { OAuthGrant } from "@/pages/settings/shared/types";

type Translate = I18nContextValue["t"];

const OAUTH_GRANTS_QUERY_KEY = ["settings", "account", "oauth-grants"] as const;

function formatDay(value: string | null | undefined) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString();
}

function statusLabel(grant: OAuthGrant, t: Translate = fallbackContext.t) {
  if (grant.status === "revoked") return t("react.settings.revoked", "Revoked");
  if (grant.status === "expired") return t("react.settings.expired", "Expired");
  return t("account_settings.status.active", "Active");
}

function lastUsedLabel(grant: OAuthGrant, t: Translate = fallbackContext.t) {
  // Only that the access was used; nothing proves which client used it.
  return grant.last_used_at
    ? t("react.oauth.access_used_on", "Access used {value1}", { value1: formatDay(grant.last_used_at) })
    : t("react.oauth.not_used_yet", "Not used yet");
}

/** Settings → Account → Personal tokens: the grants of cloud AI apps (ADR 0047, decision 16). */
export function ConnectedAppsSection() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const grantsQuery = useQuery({ queryKey: OAUTH_GRANTS_QUERY_KEY, queryFn: fetchOAuthGrants, retry: false });
  const [menuGrant, setMenuGrant] = useState<OAuthGrant | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<OAuthGrant | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [revokeError, setRevokeError] = useState("");
  const grants = grantsQuery.data?.grants || [];
  const readiness = grantsQuery.data?.readiness;
  const blockers = readiness && !readiness.ready ? readiness.blockers : [];

  async function confirmRevoke() {
    if (!revokeTarget) return;
    setRevoking(true);
    setRevokeError("");
    try {
      await revokeOAuthGrant(revokeTarget.id);
      setRevokeTarget(null);
      await queryClient.invalidateQueries({ queryKey: OAUTH_GRANTS_QUERY_KEY });
    } catch (error) {
      setRevokeError(error instanceof Error ? error.message : t("react.oauth.revoke_failed", "Access could not be revoked."));
    } finally {
      setRevoking(false);
    }
  }

  const appName = (grant: OAuthGrant | null) => grant?.client_name || grant?.client_host || t("react.oauth.app_lowercase", "app");

  return (
    <>
      <div className="fs-panel-heading"><div><h2 className="fs-panel-title">{t("react.oauth.connected_apps", "Connected AI apps")}</h2><p>{t("react.oauth.connected_apps_description", "Cloud AI apps you allowed to use Flow Steward on your behalf.")}</p></div></div>
      {blockers.map((code) => {
        const notice = oauthNotice(code, t);
        return <DsAlert key={code} title={notice.title} tone="warning">{notice.copy}</DsAlert>;
      })}
      <article className="fs-card fs-token-table-card">
        {grantsQuery.isError ? <div className="fs-card-body"><DsAlert tone="danger" title={t("react.oauth.connected_apps_unavailable", "Connected apps could not be loaded")}>{t("react.settings.check_the_connection_and_try_again", "Check the connection and try again.")}</DsAlert><button className="fs-button" onClick={() => ignoreAsyncResult(grantsQuery.refetch())} type="button">{t("job_security.retry", "Retry")}</button></div> : null}
        <div className="fs-table-scroll">
          <table aria-busy={grantsQuery.isFetching} aria-label={t("react.oauth.connected_apps", "Connected AI apps")} className="fs-table fs-token-table">
            <thead><tr><th>{t("react.oauth.column_app", "App")}</th><th>{t("account_settings.card.status", "Status")}</th><th>{t("react.oauth.column_access", "Access")}</th><th>{t("react.oauth.column_projects", "Projects")}</th><th>{t("account.tokens.created", "Created")}</th><th>{t("account.tokens.expires", "Expires")}</th><th>{t("react.settings.last_used", "Last used")}</th><th aria-label={t("monitoring.table.actions", "Actions")} /></tr></thead>
            <tbody>
              {grants.map((grant) => (
                <tr key={grant.id}>
                  <td><strong>{appName(grant)}</strong><br /><span className="fs-field-helper">{grant.client_kind === "platform" ? t("react.oauth.app_registered_by_admin_short", "Registered by an administrator") : grant.client_host}</span></td>
                  <td><span className={`fs-badge ${grant.status === "active" ? "green" : ""}`}>{statusLabel(grant, t)}</span></td>
                  <td title={grant.scopes.join(", ")}><strong>{oauthPresetLabel(grant.preset, t)}</strong><br /><span className="fs-field-helper">{grant.scopes.join(", ")}</span></td>
                  <td>{grant.allowed_project_ids.length ? t("react.oauth.project_count", "{value1} projects", { value1: String(grant.allowed_project_ids.length) }) : t("react.oauth.all_projects", "All projects within your access")}</td>
                  <td>{formatDay(grant.created_at)}</td>
                  <td>{formatDay(grant.expires_at)}</td>
                  <td>{lastUsedLabel(grant, t)}</td>
                  <td className="fs-table-action-cell">
                    {grant.status === "active" ? (
                      <RowActionMenu label={t("react.settings.actions_for_value1", "Actions for {value1}", { value1: appName(grant) })} onOpenChange={(open) => setMenuGrant(open ? grant : null)} open={menuGrant?.id === grant.id}>
                        <button onClick={() => { setRevokeTarget(grant); setMenuGrant(null); }} role="menuitem" type="button">{t("react.oauth.revoke_access", "Revoke access")}</button>
                      </RowActionMenu>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!grantsQuery.isLoading && !grantsQuery.isError && !grants.length ? <div className="fs-table-state"><strong>{t("react.oauth.no_connected_apps", "No connected AI apps")}</strong><span>{t("react.oauth.no_connected_apps_copy", "When you connect ChatGPT or Claude to Flow Steward and approve the access, the app appears here and can be revoked at any time.")}</span></div> : null}
          {grantsQuery.isLoading ? <div className="fs-table-state"><strong>{t("react.oauth.loading_connected_apps", "Loading connected apps")}</strong></div> : null}
        </div>
      </article>
      <DsDialog busy={revoking} closeLabel={t("react.settings.close_revoke_confirmation", "Close revoke confirmation")} description={t("react.oauth.revoke_description", "The app loses access immediately, including its refresh token. It has to be connected again to work.")} eyebrow={t("react.oauth.connected_apps", "Connected AI apps")} footer={<><button className="fs-button" disabled={revoking} onClick={() => setRevokeTarget(null)} type="button">{t("common.cancel", "Cancel")}</button><button className="fs-button danger" disabled={revoking} onClick={() => ignoreAsyncResult(confirmRevoke())} type="button">{revoking ? t("react.settings.revoking", "Revoking…") : t("react.oauth.revoke_access", "Revoke access")}</button></>} onClose={() => setRevokeTarget(null)} open={Boolean(revokeTarget)} title={t("react.settings.revoke_value1", "Revoke “{value1}”?", { value1: appName(revokeTarget) })} variant="confirm">
        <DsAlert tone="danger" title={t("react.settings.immediate_access_loss", "Immediate access loss")}>{revokeError || t("react.oauth.revoke_warning", "Revoked access stays listed until it is cleaned up.")}</DsAlert>
      </DsDialog>
    </>
  );
}
