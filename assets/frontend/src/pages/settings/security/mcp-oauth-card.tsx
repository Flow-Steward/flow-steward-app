import { useId, useState } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { DsAlert, DsCard } from "@/components/shared/design-system";
import { DsSwitch } from "@/components/shared/ds-switch";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { fetchMcpOAuthSetting, saveMcpOAuthSetting } from "@/pages/settings/shared/api";
import { oauthNotice } from "@/pages/settings/shared/oauth-copy";

const MCP_OAUTH_QUERY_KEY = ["settings", "security", "mcp-oauth"] as const;

/** The account switch for cloud AI apps over OAuth (ADR 0047, decision 16). */
export function McpOAuthCard() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const descriptionId = useId();
  const settingQuery = useQuery({ queryKey: MCP_OAUTH_QUERY_KEY, queryFn: fetchMcpOAuthSetting, retry: false });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const enabled = Boolean(settingQuery.data?.enabled);
  const blockers = (settingQuery.data?.readiness?.blockers || []).filter((code) => code !== "disabled_by_admin");

  async function toggle(next: boolean) {
    setSaving(true);
    setSaveError("");
    try {
      const saved = await saveMcpOAuthSetting(next);
      queryClient.setQueryData(MCP_OAUTH_QUERY_KEY, saved);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t("react.oauth.setting_not_saved", "The setting was not saved."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DsCard description={t("react.oauth.setting_description", "Let ChatGPT and Claude connect to this account over OAuth after a user signs in and approves the access.")} title={t("react.oauth.setting_title", "Cloud AI apps (OAuth)")}>
      {settingQuery.isError ? (
        <DsAlert title={t("react.oauth.setting_unavailable", "The setting could not be loaded")} tone="danger">{t("react.settings.check_the_connection_and_try_again", "Check the connection and try again.")}</DsAlert>
      ) : (
        <div className="fs-field">
          <span className="fs-footer-actions">
            <DsSwitch checked={enabled} describedBy={descriptionId} disabled={saving || settingQuery.isLoading} label={t("react.oauth.setting_switch", "Allow cloud AI apps to connect")} onCheckedChange={(next) => ignoreAsyncResult(toggle(next))} />
            <strong>{t("react.oauth.setting_switch", "Allow cloud AI apps to connect")}</strong>
          </span>
          <span className="fs-field-helper" id={descriptionId}>{t("react.oauth.setting_help", "Off by default. It works only when this installation has a public HTTPS address. Turning it off immediately rejects every OAuth token of this account; the granted access stays listed and can be revoked. Personal access tokens are not affected.")}</span>
        </div>
      )}
      {blockers.map((code) => {
        const notice = oauthNotice(code, t);
        return <DsAlert key={code} title={notice.title} tone="warning">{notice.copy}</DsAlert>;
      })}
      {saveError ? <DsAlert title={t("react.oauth.setting_not_saved", "The setting was not saved.")} tone="danger">{saveError}</DsAlert> : null}
    </DsCard>
  );
}
