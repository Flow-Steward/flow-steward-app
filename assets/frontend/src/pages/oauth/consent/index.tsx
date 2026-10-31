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
import { RequireAuth } from "@/lib/auth/guards";
import { isHttpError } from "@/lib/http/errors";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { fetchProjectsList } from "@/pages/projects/list/api";
import { decideOAuthConsent, fetchOAuthConsent } from "@/pages/settings/shared/api";
import { oauthNotice, oauthPresetDescription, oauthPresetLabel } from "@/pages/settings/shared/oauth-copy";
import type { OAuthConsentPreset, OAuthConsentView, OAuthPresetId } from "@/pages/settings/shared/types";

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

function ConsentForm({ onRedirect, view }: Readonly<{ onRedirect: RedirectFn; view: OAuthConsentView }>) {
  const { t } = useI18n();
  const available = view.presets.filter((preset) => preset.available);
  const initialPreset = available.find((preset) => preset.id === view.default_preset)?.id ?? available[0]?.id ?? null;
  const [preset, setPreset] = useState<OAuthPresetId | null>(initialPreset);
  const expiryOptions = view.expiry_days_options.length ? view.expiry_days_options : [7, 30, 90];
  const [expiryDays, setExpiryDays] = useState<number>(expiryOptions.includes(view.default_expiry_days) ? view.default_expiry_days : expiryOptions[0]);
  const [projectIds, setProjectIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [decisionError, setDecisionError] = useState<{ title: string; copy: string } | null>(null);
  const projectsQuery = useQuery({ queryKey: ["oauth", "consent", "projects"], queryFn: () => fetchProjectsList({ page: 1, perPage: 200, search: "" }), enabled: available.length > 0 });
  const projectOptions = useMemo(() => (projectsQuery.data?.projects || []).map((project) => ({ value: String(project.id), label: project.name?.trim() || t("react.settings.unnamed_project", "Unnamed project") })), [projectsQuery.data, t]);
  const clientLabel = view.client.client_name || view.client.client_host;

  async function decide(decision: "approve" | "deny") {
    if (submitting) return;
    if (decision === "approve" && !preset) return;
    setSubmitting(true);
    setDecisionError(null);
    try {
      const body = decision === "approve" && preset
        ? { decision: "approve" as const, preset, expiry_days: expiryDays, project_ids: projectIds }
        : { decision: "deny" as const };
      const result = await decideOAuthConsent(view.request_id, body);
      onRedirect(result.redirect_to);
    } catch (error) {
      setDecisionError(requestErrorNotice(error, t));
      setSubmitting(false);
    }
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
          <dt className="fs-field-label">{t("react.oauth.app_published_by", "Identity published by")}</dt>
          <dd><DsValueChip>{view.client.client_host}</DsValueChip></dd>
          <dt className="fs-field-label">{t("react.oauth.app_returns_to", "Returns to")}</dt>
          <dd><DsValueChip>{view.client.redirect_uri_host}</DsValueChip></dd>
        </dl>
        {view.client.redirect_uri_loopback ? (
          <DsAlert title={t("react.oauth.loopback_title", "The app returns to this computer")} tone="warning">
            {t("react.oauth.loopback_copy", "After you decide, the browser returns to a program running on this computer. Continue only if you started this connection yourself.")}
          </DsAlert>
        ) : null}
      </DsCard>

      <DsCard title={t("react.oauth.access_title", "Access")} description={t("react.oauth.access_description", "The app gets only the access you choose here, never more than your own role allows.")}>
        {available.length ? null : (
          <DsAlert title={t("react.oauth.nothing_grantable_title", "Nothing can be granted")} tone="warning">
            {t("react.oauth.nothing_grantable_copy", "The access this app asks for is more than your account allows. You can only deny the request.")}
          </DsAlert>
        )}
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
      </DsCard>

      {available.length ? (
        <DsCard title={t("react.oauth.limits_title", "Projects and duration")} description={t("react.oauth.limits_description", "Narrow the access further and choose when it ends.")}>
          <div className="fs-field">
            <span className="fs-field-label">{t("react.settings.project_restrictions", "Project restrictions")}</span>
            <MultiCombobox ariaLabel={t("react.settings.project_restrictions", "Project restrictions")} disabled={projectsQuery.isLoading || submitting} onChange={setProjectIds} options={projectOptions} placeholder={projectsQuery.isLoading ? t("react.settings.loading_projects", "Loading projects…") : t("react.settings.search_projects", "Search projects")} selected={projectIds} />
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
        </DsCard>
      ) : null}

      {decisionError ? <DsAlert title={decisionError.title} tone="danger">{decisionError.copy}</DsAlert> : null}

      <div className="fs-footer-actions">
        <button className="fs-button" disabled={submitting} onClick={() => ignoreAsyncResult(decide("deny"))} type="button">{t("react.oauth.deny", "Deny")}</button>
        {available.length ? (
          <button className="fs-button primary" disabled={submitting || !preset} onClick={() => ignoreAsyncResult(decide("approve"))} type="button">
            {submitting ? t("react.oauth.sending", "Sending…") : t("react.oauth.allow", "Allow access")}
          </button>
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
