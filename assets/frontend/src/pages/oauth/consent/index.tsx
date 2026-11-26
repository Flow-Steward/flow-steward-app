import { useEffect, useMemo, useState, type ReactNode } from "react";

import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router";

import {
  DsAlert,
  DsCard,
  DsPageHeader,
  DsRadio,
  DsValueChip,
  MultiCombobox,
  PageContainer,
} from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { Button } from "@/components/ui/button";
import { RequireAuth } from "@/lib/auth/guards";
import { isHttpError } from "@/lib/http/errors";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { formatDate } from "@/lib/time/date-time-format";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { decideOAuthConsent, fetchOAuthConsent } from "@/pages/settings/shared/api";
import { oauthNotice, oauthPresetDescription, oauthPresetLabel, permissionText, roleLabel } from "@/pages/settings/shared/oauth-copy";
import type { OAuthConsentDecision, OAuthConsentPreset, OAuthConsentView, OAuthPresetId } from "@/pages/settings/shared/types";

import "@/pages/settings/shared/settings.css";

type Translate = I18nContextValue["t"];
type RedirectFn = (url: string) => void;

function defaultRedirect(url: string) {
  globalThis.window.location.assign(url);
}

function requestErrorCode(error: unknown): string {
  if (!isHttpError(error)) return "";
  const payload = error.payload;
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    const code = (payload as Record<string, unknown>).error;
    if (typeof code === "string") return code;
  }
  return "";
}

function requestErrorNotice(error: unknown, t: Translate = fallbackContext.t): { title: string; copy: string } {
  switch (requestErrorCode(error)) {
    case "authorization_not_found":
      return {
        title: t("react.oauth.authorization_not_found_title", "This connection request was not found"),
        copy: t("react.oauth.authorization_not_found_copy", "Start the connection again from the app."),
      };
    case "browser_mismatch":
      return {
        title: t("react.oauth.browser_mismatch_title", "Open the connection in the same browser"),
        copy: t("react.oauth.browser_mismatch_copy", "This request was started in another browser or its cookie was removed. Start the connection again from the app in this browser."),
      };
    case "authorization_already_decided":
      return {
        title: t("react.oauth.authorization_already_decided_title", "This request has already been answered"),
        copy: t("react.oauth.authorization_already_decided_copy", "Each connection request can be approved or denied once. Start the connection again from the app if needed."),
      };
    case "advanced_options_admin_only":
      return {
        title: t("react.oauth.request_failed_title", "The connection request could not be completed"),
        copy: t("react.agent_access.advanced_options_admin_only", "Your agent gets the access of your role; only an admin chooses the access, the expiry and the projects."),
      };
    case "authorization_expired":
      return {
        title: t("react.oauth.authorization_expired_title", "This connection request has expired"),
        copy: t("react.oauth.authorization_expired_copy", "Requests are valid for 10 minutes. Start the connection again from the app."),
      };
    default:
      return {
        title: t("react.oauth.request_failed_title", "The connection request could not be completed"),
        copy: error instanceof Error && error.message ? error.message : t("react.oauth.request_failed_copy", "Check the connection and try again."),
      };
  }
}

function reasonText(preset: OAuthConsentPreset, t: Translate = fallbackContext.t): string {
  if (preset.reason === "exceeds_authority") {
    return t("react.oauth.preset_exceeds_authority", "Not available: this access is more than your own role allows.");
  }
  if (preset.reason === "not_requested") {
    return t("react.oauth.preset_not_requested", "Not available: the app did not ask for this access.");
  }
  return t("react.oauth.preset_unavailable", "Not available for this request.");
}

function ConsentShell({ children, title, description }: Readonly<{ children: ReactNode; title: string; description: string }>) {
  const { t } = useI18n();
  return (
    <main className="min-h-screen px-6 py-10">
      <PageContainer width="standard">
        <div className="fs-form-block fs-panel-stack">
          <DsPageHeader description={description} eyebrow={t("react.oauth.eyebrow", "Connect an AI app")} title={title} />
          {children}
        </div>
      </PageContainer>
    </main>
  );
}

function ConsentErrorPage({ code }: Readonly<{ code: string }>) {
  const { t } = useI18n();
  const notice = oauthNotice(code, t);
  return (
    <ConsentShell description={t("react.oauth.error_description", "The app could not be connected to Flow Steward.")} title={t("react.oauth.error_title", "Connection stopped")}>
      <DsAlert title={notice.title} tone="warning">{notice.copy}</DsAlert>
    </ConsentShell>
  );
}

/** The plain sentence of what the app will be able to do "as my role" (ADR 0076 §5). */
function roleAccessSentence(view: OAuthConsentView, clientLabel: string, t: Translate): string {
  const access = view.role_access;
  if (access?.narrower) {
    return t("react.oauth.role_access_narrower", "{client} asked for less than your role allows, so it gets only this part of your access.", { client: clientLabel });
  }
  if (access?.role === "admin") {
    return t("react.oauth.role_access_admin", "{client} will be able to do what you can do in Flow Steward.", { client: clientLabel });
  }
  if (access?.role === "operator") {
    return t("react.oauth.role_access_operator", "{client} will be able to see what you see and do the work you do: start workflows, decide approvals and control jobs. It cannot change how Flow Steward is set up.", { client: clientLabel });
  }
  return t("react.oauth.role_access_viewer", "{client} will be able to see what you see in Flow Steward. It cannot change anything.", { client: clientLabel });
}

function ConsentForm({ onRedirect, view }: Readonly<{ onRedirect: RedirectFn; view: OAuthConsentView }>) {
  const { t } = useI18n();
  // ADR 0076 §5: an operator or a viewer chooses nothing; only an admin has the presets.
  const advanced = Boolean(view.advanced_options);
  const roleAccess = view.role_access;
  const available = view.presets.filter((preset) => preset.available);
  const initialPreset = available.find((preset) => preset.id === view.default_preset)?.id ?? available[0]?.id ?? null;
  const [preset, setPreset] = useState<OAuthPresetId | null>(initialPreset);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const expiryOptions = view.expiry_days_options.length ? view.expiry_days_options : [7, 30, 90];
  const preferredExpiry = initialPreset === "my_role" && roleAccess ? roleAccess.expiry_days : view.default_expiry_days;
  const [expiryDays, setExpiryDays] = useState<number>(expiryOptions.includes(preferredExpiry) ? preferredExpiry : expiryOptions[0]);
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [pendingDecision, setPendingDecision] = useState<"approve" | "deny" | null>(null);
  const submitting = pendingDecision !== null;
  const [decisionError, setDecisionError] = useState<{ title: string; copy: string } | null>(null);
  const projectsQuery = useQuery({ queryKey: ["oauth", "consent", "projects"], queryFn: () => fetchProjectsList({ page: 1, perPage: 200, search: "" }), enabled: advanced && advancedOpen && available.length > 0 });
  const projectOptions = useMemo(() => (projectsQuery.data?.projects || []).map((project) => ({ value: String(project.id), label: project.name?.trim() || t("react.settings.unnamed_project", "Unnamed project") })), [projectsQuery.data, t]);
  const clientLabel = view.client.client_name || view.client.client_host;
  const canAllow = advanced ? Boolean(preset) : Boolean(roleAccess?.available);
  const who = view.user ? view.user.display_name || view.user.username : "";

  async function decide(decision: "approve" | "deny") {
    if (submitting) return;
    if (decision === "approve" && !canAllow) return;
    setPendingDecision(decision);
    setDecisionError(null);
    try {
      let body: OAuthConsentDecision = { decision: "deny" };
      if (decision === "approve") {
        // "As my role": the decision alone; the server gives the role and the expiry.
        body = advanced && preset ? { decision: "approve", preset, expiry_days: expiryDays, project_ids: projectIds } : { decision: "approve" };
      }
      const result = await decideOAuthConsent(view.request_id, body);
      onRedirect(result.redirect_to);
    } catch (error) {
      setDecisionError(requestErrorNotice(error, t));
      setPendingDecision(null);
    }
  }

  function renderRoleAccess() {
    if (!roleAccess) return null;
    if (!roleAccess.available) {
      return (
        <DsAlert title={t("react.oauth.nothing_grantable_title", "Nothing can be granted")} tone="warning">
          {t("react.oauth.nothing_grantable_copy", "The access this app asks for is more than your account allows. You can only deny the request.")}
        </DsAlert>
      );
    }
    return (
      <>
        <p>{roleAccessSentence(view, clientLabel, t)}</p>
        {roleAccess.permissions.length ? (
          <details className="fs-field" open={roleAccess.narrower}>
            <summary className="fs-field-label">{t("react.oauth.role_access_details", "What it can do")}</summary>
            <ul className="fs-field-helper">
              {roleAccess.permissions.map((item) => <li key={item.key}>{permissionText(item.key, item.description, t)}</li>)}
            </ul>
          </details>
        ) : null}
        <p className="fs-field-helper">
          {t("react.oauth.role_access_expiry", "Works for {days} days, until {date}. Connect again from the app to keep it working after that.", { days: String(roleAccess.expiry_days), date: formatDate(roleAccess.expires_at) })}
        </p>
      </>
    );
  }

  function renderAdvanced() {
    const projectsSummary = projectIds.length
      ? t("react.oauth.project_count", "{value1} projects", { value1: String(projectIds.length) })
      : t("react.agent_access.all_projects", "All projects");
    return (
      <>
        <div className="fs-field">
          <p className="fs-field-helper">{[oauthPresetLabel(preset || "", t), t("react.settings.value1_days", "{value1} days", { value1: String(expiryDays) }), projectsSummary].join(" · ")}</p>
          <div className="fs-footer-actions">
            <button aria-expanded={advancedOpen} className="fs-button ghost sm" disabled={submitting} onClick={() => setAdvancedOpen((open) => !open)} type="button">
              {advancedOpen ? t("react.agent_access.hide_advanced_options", "Hide advanced options") : t("react.agent_access.advanced_options", "Advanced options")}
            </button>
          </div>
        </div>
        {advancedOpen ? (
          <>
            <fieldset className="fs-field">
              <legend className="fs-field-label">{t("react.oauth.access_legend", "Access level")}</legend>
              {view.presets.map((option) => (
                <div className="fs-field" key={option.id}>
                  <DsRadio checked={preset === option.id} disabled={!option.available || submitting} name="oauth-preset" onChange={() => setPreset(option.id)} value={option.id}>
                    <strong>{oauthPresetLabel(option.id, t)}</strong>
                  </DsRadio>
                  <span className="fs-field-helper">{oauthPresetDescription(option.id, t)}</span>
                  {option.subset_of_preset ? <span className="fs-field-helper">{t("react.oauth.preset_subset", "Only the access this app requested:")}</span> : null}
                  <span className="fs-footer-actions">{option.scopes.map((scope) => <DsValueChip key={scope}>{scope}</DsValueChip>)}</span>
                  {option.available ? null : <span className="fs-field-helper error">{reasonText(option, t)}</span>}
                </div>
              ))}
            </fieldset>
            <div className="fs-field">
              <span className="fs-field-label">{t("react.settings.project_restrictions", "Project restrictions")}</span>
              <MultiCombobox ariaLabel={t("react.settings.project_restrictions", "Project restrictions")} disabled={submitting} loading={projectsQuery.isLoading} loadingLabel={t("react.settings.loading_projects", "Loading projects…")} onChange={setProjectIds} options={projectOptions} placeholder={t("react.settings.search_projects", "Search projects")} selected={projectIds} />
              <span className="fs-field-helper">{t("react.oauth.projects_help", "Empty means every project you can access. Choosing projects does not isolate account-wide agents and skills.")}</span>
            </div>
            <fieldset className="fs-field">
              <legend className="fs-field-label fs-required-label">{t("react.oauth.expiry_legend", "Access ends after")}<span aria-hidden="true" className="fs-required">*</span></legend>
              <div className="fs-radio-grid">
                {expiryOptions.map((days) => (
                  <DsRadio checked={expiryDays === days} disabled={submitting} key={days} name="oauth-expiry" onChange={() => setExpiryDays(days)} value={String(days)}>
                    {t("react.settings.value1_days", "{value1} days", { value1: String(days) })}
                  </DsRadio>
                ))}
              </div>
              <span className="fs-field-helper">{t("react.oauth.expiry_help", "Refreshing the connection never extends this date. You can revoke it earlier in Settings → Account → Personal tokens.")}</span>
            </fieldset>
          </>
        ) : null}
      </>
    );
  }

  return (
    <ConsentShell
      description={t("react.oauth.consent_description", "Review what {value1} may do with your Flow Steward account before you allow it.", { value1: clientLabel })}
      title={t("react.oauth.consent_title", "Allow {value1} to access Flow Steward?", { value1: clientLabel })}
    >
      <DsCard title={t("react.oauth.app_title", "App")} description={t("react.oauth.app_description", "Who is asking and where you return after deciding.")}>
        <dl className="fs-field">
          <dt className="fs-field-label">{t("react.oauth.app_name", "Name")}</dt>
          <dd>{clientLabel}</dd>
          {view.client.client_kind === "platform" ? (
            <>
              <dt className="fs-field-label">{t("react.oauth.app_registered_by", "Registered by")}</dt>
              <dd>{t("react.oauth.app_registered_by_admin", "An administrator of this Flow Steward installation")}</dd>
            </>
          ) : (
            <>
              <dt className="fs-field-label">{t("react.oauth.app_published_by", "Identity published by")}</dt>
              <dd><DsValueChip>{view.client.client_host}</DsValueChip></dd>
            </>
          )}
          <dt className="fs-field-label">{t("react.oauth.app_returns_to", "Returns to")}</dt>
          <dd><DsValueChip>{view.client.redirect_uri_host}</DsValueChip></dd>
        </dl>
        {view.client.redirect_uri_loopback ? (
          <DsAlert title={t("react.oauth.loopback_title", "The app returns to this computer")} tone="warning">
            {t("react.oauth.loopback_copy", "After you decide, the browser returns to a program running on this computer. Continue only if you started this connection yourself.")}
          </DsAlert>
        ) : null}
      </DsCard>

      <DsCard title={t("react.oauth.access_title", "Access")} description={t("react.oauth.role_access_description", "The app acts as you and can never do more than your own role allows.")}>
        {who ? (
          <p className="fs-field-helper">
            {view.user?.role
              ? t("react.oauth.signed_in_as_role", "Signed in as {name} · {role}", { name: who, role: roleLabel(view.user.role, t) })
              : t("react.oauth.signed_in_as", "Signed in as {name}", { name: who })}
          </p>
        ) : null}
        {renderRoleAccess()}
        {advanced && available.length ? renderAdvanced() : null}
      </DsCard>

      {decisionError ? <DsAlert title={decisionError.title} tone="danger">{decisionError.copy}</DsAlert> : null}

      <div className="fs-footer-actions">
        <Button busy={pendingDecision === "deny"} busyLabel={t("react.oauth.sending", "Sending…")} onClick={() => ignoreAsyncResult(decide("deny"))} type="button" variant="secondary">{t("react.oauth.deny", "Deny")}</Button>
        {canAllow ? (
          <Button busy={pendingDecision === "approve"} busyLabel={t("react.oauth.sending", "Sending…")} onClick={() => ignoreAsyncResult(decide("approve"))} type="button" variant="primary">
            {t("react.oauth.allow_short", "Allow")}
          </Button>
        ) : null}
      </div>
    </ConsentShell>
  );
}

function ConsentRequest({ onRedirect, requestId }: Readonly<{ onRedirect: RedirectFn; requestId: string }>) {
  const { t } = useI18n();
  const consentQuery = useQuery({
    queryKey: ["oauth", "consent", requestId],
    queryFn: () => fetchOAuthConsent(requestId),
    retry: false,
    refetchOnWindowFocus: false,
  });
  if (consentQuery.isLoading) {
    return <PageLoadingState label={t("react.oauth.loading", "Loading the connection request")} standalone />;
  }
  if (consentQuery.isError || !consentQuery.data) {
    const notice = requestErrorNotice(consentQuery.error, t);
    return (
      <ConsentShell description={t("react.oauth.error_description", "The app could not be connected to Flow Steward.")} title={t("react.oauth.error_title", "Connection stopped")}>
        <DsAlert title={notice.title} tone="warning">{notice.copy}</DsAlert>
      </ConsentShell>
    );
  }
  return <ConsentForm onRedirect={onRedirect} view={consentQuery.data} />;
}

/**
 * The inbound OAuth consent page (ADR 0047, decisions 6 and 15). `?error=` is
 * reached before sign-in and renders without a session; `?request=` needs one.
 */
export function OAuthConsentPage({ onRedirect = defaultRedirect }: Readonly<{ onRedirect?: RedirectFn }> = {}) {
  const [searchParams] = useSearchParams();
  const error = (searchParams.get("error") || "").trim();
  const requestId = (searchParams.get("request") || "").trim();

  useEffect(() => {
    // The consent page carries a one-time request id; keep it out of referrers.
    const meta = document.createElement("meta");
    meta.name = "referrer";
    meta.content = "no-referrer";
    document.head.appendChild(meta);
    return () => meta.remove();
  }, []);

  if (error || !requestId) {
    return <ConsentErrorPage code={error || "invalid_request"} />;
  }
  return (
    <RequireAuth>
      <ConsentRequest onRedirect={onRedirect} requestId={requestId} />
    </RequireAuth>
  );
}
