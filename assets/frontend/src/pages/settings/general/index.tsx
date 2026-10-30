import { useI18n } from "@/lib/i18n/useI18n";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { DsAlert, DsBadge, DsCard } from "@/components/shared/design-system";
import { fetchGeneralSettingsOverview } from "@/pages/settings/shared/api";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

type Entity = Record<string, unknown>;

function objectOrEmpty(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

function listFromUnknown(value: unknown): Entity[] {
  return Array.isArray(value)
    ? value.filter((item): item is Entity => Boolean(item) && typeof item === "object")
    : [];
}

export function SettingsGeneralPage() {
  const { t } = useI18n();
  const overviewQuery = useQuery({
    queryKey: ["settings", "general", "overview"],
    queryFn: fetchGeneralSettingsOverview,
  });

  const overview = objectOrEmpty(overviewQuery.data);
  const appBootstrap = objectOrEmpty(overview.app_bootstrap);
  const bootstrapAuth = objectOrEmpty(appBootstrap.auth);
  const externalSignIn = objectOrEmpty(bootstrapAuth.external_sign_in);
  const authSession = objectOrEmpty(overview.auth_session);
  const providers = listFromUnknown(overview.model_providers);

  const localAuthEnabled = Boolean(bootstrapAuth.local_auth_enabled ?? authSession.local_auth_enabled);
  const externalAuthEnabled = Boolean(bootstrapAuth.external_auth_enabled ?? authSession.external_auth_enabled);
  const bootstrapRequired = Boolean(bootstrapAuth.bootstrap_required);
  const externalSignInEnabled = Boolean(externalSignIn.providers_path);
  const providerCount = providers.length;
  const providerLabels = providers.slice(0, 6).map((provider, index) => {
    const id = firstStringFromUnknownValue([provider.provider_id, provider.id, `provider-${index}`]);
    const name = firstStringFromUnknownValue([provider.name, provider.display_name, provider.label], t("react.settings.unnamed_provider", "Unnamed provider"));
    return { id, name };
  });

  return (
    <>
      {overviewQuery.isError ? (
        <DsAlert title={t("react.settings.failed_to_load_general_settings", "Failed to load general settings")} tone="danger">
          {overviewQuery.error instanceof Error ? overviewQuery.error.message : t("react.settings.unknown_error", "Unknown error")}
        </DsAlert>
      ) : null}

      {overviewQuery.isLoading ? <p className="fs-empty-copy">{t("react.settings.loading_general_settings_snapshot", "Loading general settings snapshot...")}</p> : null}

      <DsCard description={t("react.settings.current_login_capabilities_for_this_deployment", "Current login capabilities for this deployment.")} title={t("monitoring.flags.auth_mode", "Auth mode")}>
        <div className="fs-detail-rows" data-columns="2">
          {[
            [t("react.settings.local_password_auth", "Local password auth"), localAuthEnabled],
            [t("react.settings.external_auth", "External auth"), externalAuthEnabled],
            [t("react.settings.external_sign_in", "External sign-in"), externalSignInEnabled],
            [t("react.settings.bootstrap_required", "Bootstrap required"), bootstrapRequired],
          ].map(([label, enabled]) => (
            <div className="fs-detail-row" key={String(label)}>
              <span>{label}</span>
              <span className="fs-detail-value">
                <DsBadge tone={enabled ? "green" : "neutral"}>{enabled ? t("react.settings.enabled", "Enabled") : t("react.settings.disabled", "Disabled")}</DsBadge>
              </span>
            </div>
          ))}
        </div>
      </DsCard>

      <DsCard
        description={t("react.settings.value1_configured_providers_discovered_via_api_model_providers", "{value1} configured providers discovered via /api/model-providers.", { value1: String(providerCount) })}
        title={t("react.settings.model_providers", "Model providers")}
      >
        {overviewQuery.isLoading ? (
          <p className="fs-empty-copy">{t("settings.loading.model_providers", "Loading model providers...")}</p>
        ) : overviewQuery.isError ? (
          <p className="fs-empty-copy">{t("react.settings.model_providers_unavailable", "Model providers unavailable.")}</p>
        ) : providerLabels.length ? (
          <div className="fs-detail-rows">
            {providerLabels.map((provider) => (
              <div className="fs-detail-row" key={provider.id}>
                <span>{provider.name}</span>
              </div>
            ))}
          </div>
        ) : (
          <p className="fs-empty-copy">{t("react.settings.no_model_providers_configured_yet", "No model providers configured yet.")}</p>
        )}
      </DsCard>

      <DsCard description={t("react.settings.use_the_dedicated_settings_areas_for_deeper_configuration", "Use the dedicated settings areas for deeper configuration.")} title={t("react.settings.next_actions", "Next actions")}>
        <div className="fs-footer-actions">
          {[
            { href: "/settings/users", label: t("react.settings.user_management", "User management") },
            { href: "/settings/providers", label: t("react.settings.model_providers", "Model providers") },
            { href: "/settings/security", label: t("react.settings.security_policy", "Security policy") },
            { href: "/settings/service-accounts", label: t("react.settings.service_accounts_2", "Service accounts") },
            { href: "/settings/notifications", label: t("react.settings.notification_channels", "Notification channels") },
          ].map((item) => (
            <Link className="fs-button" key={item.href} to={item.href}>
              {item.label}
            </Link>
          ))}
        </div>
      </DsCard>
    </>
  );
}
