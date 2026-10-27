import type { PropsWithChildren } from "react";

import { Navigate, useLocation } from "react-router";

import { DsAlert } from "@/components/shared/design-system";
import { PageLoadingState } from "@/components/shared/page-loading-state";
import { PagePlaceholder } from "@/components/shared/page-placeholder";
import {
  type FrontendCapability,
  useHasCapability,
} from "@/lib/auth/capabilities";
import { useAuth } from "@/lib/auth/useAuth";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { useI18n } from "@/lib/i18n/useI18n";
import { ignoreAsyncResult } from "@/lib/utils/async";

/**
 * Shown when the session could not be read at all. That is not "signed out":
 * redirecting would drop a signed-in user from the page they asked for.
 */
function SessionUnavailable({ onRetry }: Readonly<{ onRetry: () => Promise<void> }>) {
  const { t } = useI18n();
  return (
    <main className="fs-settings min-h-screen px-6 py-10">
      <DsAlert title={t("react.auth.session_check_failed", "Your session could not be checked")} tone="danger">
        <p>{t("react.auth.session_unavailable_detail", "Flow Steward did not answer. You are still signed in; try again in a moment.")}</p>
        <button className="fs-button" onClick={() => ignoreAsyncResult(onRetry())} type="button">
          {t("react.auth.try_again", "Try again")}
        </button>
      </DsAlert>
    </main>
  );
}

export function RequireAuth({ children }: Readonly<PropsWithChildren>) {
  const auth = useAuth();
  const location = useLocation();
  const { t } = useI18n();

  // A deep link waits on the session like the home route does: with the
  // page loader, not an empty screen under the global request loader.
  if (auth.status === "loading") {
    return <PageLoadingState label={t("react.routing.restoring_session", "Restoring your session")} standalone />;
  }

  if (!auth.session && auth.error) {
    return <SessionUnavailable onRetry={auth.refresh} />;
  }

  if (!auth.isAuthenticated) {
    const nextPath = `${location.pathname}${location.search}`;
    return <Navigate replace to={`/login?next=${encodeURIComponent(nextPath)}`} />;
  }

  return <>{children}</>;
}

/**
 * Gate an authoring-only route (workflow builder, YAML import) to admins. Non-admins
 * are redirected to the read-only surface: the workflow detail view when the path is
 * a `/builder` route, otherwise the workflow catalog. Backend keeps the 403 as the
 * final authority; this only prevents showing authoring UI a non-admin cannot use.
 */
export function RequireAdmin({ children }: Readonly<PropsWithChildren>) {
  const auth = useAuth();
  const location = useLocation();
  const isAdmin = useIsAdmin();

  if (auth.status === "loading") {
    return null;
  }

  if (!auth.session && auth.error) {
    return <SessionUnavailable onRetry={auth.refresh} />;
  }

  if (!auth.isAuthenticated) {
    const nextPath = `${location.pathname}${location.search}`;
    return <Navigate replace to={`/login?next=${encodeURIComponent(nextPath)}`} />;
  }

  if (!isAdmin) {
    const builderSuffix = "/builder";
    const readOnlyPath = location.pathname.endsWith(builderSuffix)
      ? location.pathname.slice(0, -builderSuffix.length)
      : "/registry/workflows";
    return <Navigate replace to={readOnlyPath} />;
  }

  return <>{children}</>;
}

export function RequireCapability({
  capability,
  children,
}: Readonly<PropsWithChildren<{ capability: FrontendCapability }>>) {
  const auth = useAuth();
  const allowed = useHasCapability(capability);
  const { t } = useI18n();

  if (auth.status === "loading") {
    return null;
  }

  if (!allowed) {
    return (
      <PagePlaceholder
        description={t("react.auth.access_denied_detail", "Your current role does not allow access to this area. No privileged inventory was loaded.")}
        eyebrow={t("react.auth.authorization", "Authorization")}
        status={t("react.auth.denied", "Denied")}
        title={t("react.auth.access_denied", "Access denied")}
      />
    );
  }

  return <>{children}</>;
}
