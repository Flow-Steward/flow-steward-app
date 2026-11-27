import { useCallback, useMemo, type PropsWithChildren } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { holdsAnyEntitlement } from "@/lib/auth/features";
import {
  AUTH_SESSION_RETRIES,
  AUTH_SESSION_RETRY_DELAY_MS,
  dropAccountScopedQueries,
  purgeAuthenticatedClientState,
  sessionErrorIsTransient,
} from "@/lib/auth/session-state";
import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import { queryKeys } from "@/lib/query/keys";
import {
  firstStringFromUnknownValue,
  isTruthy,
} from "@/lib/utils/unknown-values";

export type AuthUser = {
  id: string;
  email: string;
  username: string;
  display_name: string;
  roles: string[];
  /**
   * ADR 0076: the permission keys the server computed for this person; the interface
   * shows menus and buttons from these. `null` for a principal that is not role-based.
   */
  permissions?: string[] | null;
  /**
   * ADR 0076 §8: for a person limited to chosen projects, the keys they hold in
   * each of them; a project absent from the map is not theirs. `null` or absent:
   * no limit, `permissions` applies in every project.
   */
  project_permissions?: Record<string, string[]> | null;
  /**
   * ADR 0076 §8b: the keys a limited person holds in each visible workspace;
   * `null` or absent without a limit.
   */
  workspace_permissions?: Record<string, string[]> | null;
  /**
   * ADR 0076 §8b: `{ project id: { workflow id: keys } }` of a limited person's
   * workflow grants. A workflow not listed takes its project's keys.
   */
  workflow_permissions?: Record<string, Record<string, string[]>> | null;
  /**
   * ADR 0076 §8b: projects a limited person reaches only through workflow grants,
   * with those workflows; any other workflow there is out of reach.
   */
  project_workflow_ids?: Record<string, string[]> | null;
  account_id: string;
  account_ids: string[];
  account_memberships: Array<Record<string, unknown>>;
  must_change_password?: boolean;
  timezone?: string;
  locale?: string;
  /** Date pattern id from the profile ("D MMM YYYY", "MM/DD/YYYY", ...). */
  date_format?: string;
  /** "24h" or "12h". */
  time_format?: string;
};

export type I18nBootstrap = {
  locale: string;
  fallback_locale: string;
  supported_locales: Array<{
    code: string;
    label: string;
    native_label: string;
  }>;
  messages: Record<string, string>;
};

export type AppEnvironmentBootstrap = {
  edition: string;
  mode?: "development" | "production";
};

/** Files of a real browser run, under the deployment's attachment policy. 0 = no limit. */
export type RunUploadLimits = {
  max_file_bytes: number;
  max_total_bytes: number;
  max_files: number;
};

export type SettingsPageRecord = {
  page_key: string;
  title: string;
  path: string;
  nav_section?: string;
  settings_tab?: boolean;
  sort_order?: number;
  nav_label?: string;
  nav_icon?: string;
  current_pages?: string[];
  [key: string]: unknown;
};

export type RegisteredPageRecord = {
  key: string;
  page_key: string;
  label: string;
  title: string;
  path: string;
  /** Vite-compiled page component; absent when a host renderer draws the page. */
  component_key?: string;
  /** Declared surface; a host-renderer kind renders without a component_key (ADR 0082). */
  control_surface?: Record<string, unknown>;
  nav_section?: string;
  nav_order?: number;
  nav_label?: string;
  nav_icon?: string;
  current_pages?: string[];
  [key: string]: unknown;
};

export type ShellNavigationItem = {
  page_key: string;
  title?: string;
  path: string;
  nav_label?: string;
  nav_icon?: string;
  nav_section?: string;
  current_pages?: string[];
  sort_order?: number;
  [key: string]: unknown;
};

export type ExternalSignInBootstrap = {
  providers_path?: string;
};

export type AuthSession = {
  authenticated: boolean;
  local_auth_enabled: boolean;
  bootstrap_required: boolean;
  external_auth_enabled: boolean;
  /** An email extension is connected, so reset links and verifications can be sent. */
  system_email_available?: boolean;
  auth_source?: string;
  roles: string[];
  permissions: Record<string, boolean>;
  account_memberships: Array<Record<string, unknown>>;
  user?: AuthUser;
  i18n: I18nBootstrap;
  feature_flags: Record<string, boolean>;
  /**
   * Where an entitled feature's API lives (ADR 0082): feature name → base URL.
   * A feature without an endpoint is not offered, whatever its flag says.
   */
  feature_endpoints?: Record<string, string>;
  external_sign_in?: ExternalSignInBootstrap;
  environment: AppEnvironmentBootstrap;
  settings_pages: SettingsPageRecord[];
  registered_pages: RegisteredPageRecord[];
  shell_navigation: Record<string, ShellNavigationItem[]>;
  run_upload_limits?: RunUploadLimits;
  /** Largest file the browser previews (FS_FILE_PREVIEW_MAX_BYTES); larger ones are download-only. */
  file_preview_max_bytes?: number;
};

type AppBootstrapResponse = {
  auth: {
    authenticated: boolean;
    local_auth_enabled: boolean;
    bootstrap_required?: boolean;
    external_auth_enabled: boolean;
    system_email_available?: boolean;
    auth_source?: string;
    user?: AuthUser;
    roles?: string[];
    permissions?: Record<string, boolean>;
    account_memberships?: Array<Record<string, unknown>>;
    external_sign_in?: unknown;
  };
  i18n?: I18nBootstrap;
  feature_flags?: Record<string, boolean>;
  feature_endpoints?: unknown;
  environment?: AppEnvironmentBootstrap;
  limits?: { run_upload?: unknown; file_preview?: unknown };
  settings_pages?: unknown;
  registered_pages?: unknown;
  shell_navigation?: unknown;
};

function normalizeRunUploadLimits(payload: unknown): RunUploadLimits | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  const record = payload as Record<string, unknown>;
  const count = (value: unknown) =>
    typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : 0;
  return {
    max_file_bytes: count(record.max_file_bytes),
    max_total_bytes: count(record.max_total_bytes),
    max_files: count(record.max_files),
  };
}

/** Feature name → base URL; only non-empty same-origin paths are kept. */
function normalizeFeatureEndpoints(payload: unknown): Record<string, string> {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
  const endpoints: Record<string, string> = {};
  for (const [name, value] of Object.entries(payload as Record<string, unknown>)) {
    const path = typeof value === "string" ? value.trim().replace(/\/+$/, "") : "";
    if (name && path.startsWith("/") && !path.startsWith("//")) endpoints[name] = path;
  }
  return endpoints;
}

function normalizeFilePreviewMaxBytes(payload: unknown): number | undefined {
  if (!payload || typeof payload !== "object") return undefined;
  const value = (payload as Record<string, unknown>).max_bytes;
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : undefined;
}

const fallbackI18n: I18nBootstrap = {
  locale: "en",
  fallback_locale: "en",
  supported_locales: [
    { code: "en", label: "English", native_label: "English" },
  ],
  messages: {},
};

const fallbackEnvironment: AppEnvironmentBootstrap = {
  edition: "community",
  mode: "production",
};

const anonymousSession: AuthSession = {
  authenticated: false,
  local_auth_enabled: true,
  bootstrap_required: false,
  external_auth_enabled: false,
  system_email_available: false,
  auth_source: "",
  roles: [],
  permissions: {
    can_read: true,
    can_mutate: false,
  },
  account_memberships: [],
  i18n: fallbackI18n,
  feature_flags: {},
  feature_endpoints: {},
  external_sign_in: {},
  environment: fallbackEnvironment,
  settings_pages: [],
  registered_pages: [],
  shell_navigation: {},
};

function normalizeSettingsPages(payload: unknown): SettingsPageRecord[] {
  if (!Array.isArray(payload)) {
    return [];
  }
  const rows: SettingsPageRecord[] = [];
  payload.forEach((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return;
    }
    const record = item as Record<string, unknown>;
    const pageKey = firstStringFromUnknownValue([record.page_key, ""]).trim();
    const path = firstStringFromUnknownValue([record.path, ""]).trim();
    if (!pageKey || !path) {
      return;
    }
    rows.push({
      ...record,
      page_key: pageKey,
      path,
      title: firstStringFromUnknownValue([record.title, pageKey]),
      nav_label: firstStringFromUnknownValue([
        record.nav_label,
        record.title,
        pageKey,
      ]),
      nav_section: firstStringFromUnknownValue([record.nav_section, "system"]),
      settings_tab: Boolean(record.settings_tab),
      sort_order: Number(record.sort_order || 100),
      current_pages: Array.isArray(record.current_pages)
        ? record.current_pages
            .map((value) => firstStringFromUnknownValue([value, ""]).trim())
            .filter(isTruthy)
        : [],
    });
  });
  return rows;
}

/** Control-surface kinds drawn by a renderer in this bundle (ADR 0082, section 6). */
const HOST_RENDERER_KINDS = new Set(["resource_admin", "resource_report", "status_document"]);

function normalizeRegisteredPages(payload: unknown): RegisteredPageRecord[] {
  if (!Array.isArray(payload)) {
    return [];
  }
  const rows: RegisteredPageRecord[] = [];
  payload.forEach((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return;
    }
    const record = item as Record<string, unknown>;
    const pageKey = firstStringFromUnknownValue([
      record.page_key,
      record.key,
      "",
    ]).trim();
    const path = firstStringFromUnknownValue([record.path, ""]).trim();
    const componentKey = firstStringFromUnknownValue([
      record.component_key,
      "",
    ]).trim();
    const controlSurface = record.control_surface;
    const hostRendered =
      Boolean(controlSurface)
      && typeof controlSurface === "object"
      && !Array.isArray(controlSurface)
      && HOST_RENDERER_KINDS.has(
        firstStringFromUnknownValue([(controlSurface as Record<string, unknown>).kind, ""]).trim(),
      );
    if (!pageKey || !path.startsWith("/") || !(componentKey || hostRendered)) {
      return;
    }
    const label = firstStringFromUnknownValue([
      record.label,
      record.nav_label,
      record.title,
      pageKey,
    ]).trim();
    rows.push({
      ...record,
      key: firstStringFromUnknownValue([record.key, pageKey]),
      page_key: pageKey,
      label,
      title: firstStringFromUnknownValue([record.title, label, pageKey]),
      path,
      component_key: componentKey,
      nav_label: firstStringFromUnknownValue([
        record.nav_label,
        label,
        pageKey,
      ]),
      nav_section: firstStringFromUnknownValue([record.nav_section, "system"]),
      nav_order: Number(record.nav_order || record.sort_order || 100),
      current_pages: Array.isArray(record.current_pages)
        ? record.current_pages
            .map((value) => firstStringFromUnknownValue([value, ""]).trim())
            .filter(isTruthy)
        : [],
    });
  });
  return rows;
}

function normalizeExternalSignIn(payload: unknown): ExternalSignInBootstrap {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return {};
  }
  const record = payload as Record<string, unknown>;
  const providersPath = firstStringFromUnknownValue([
    record.providers_path,
    "",
  ]).trim();
  return providersPath.startsWith("/") ? { providers_path: providersPath } : {};
}

function normalizeShellNavigation(
  payload: unknown,
): Record<string, ShellNavigationItem[]> {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return {};
  }
  const sections: Record<string, ShellNavigationItem[]> = {};
  Object.entries(payload as Record<string, unknown>).forEach(
    ([section, items]) => {
      if (!Array.isArray(items)) {
        return;
      }
      const rows: ShellNavigationItem[] = [];
      items.forEach((item) => {
        if (!item || typeof item !== "object" || Array.isArray(item)) {
          return;
        }
        const record = item as Record<string, unknown>;
        const pageKey = firstStringFromUnknownValue([
          record.page_key,
          "",
        ]).trim();
        const path = firstStringFromUnknownValue([record.path, ""]).trim();
        if (!pageKey || !path) {
          return;
        }
        rows.push({
          ...record,
          page_key: pageKey,
          path,
          title: firstStringFromUnknownValue([record.title, pageKey]),
          nav_label: firstStringFromUnknownValue([
            record.nav_label,
            record.title,
            pageKey,
          ]),
          nav_icon: firstStringFromUnknownValue([record.nav_icon, "file"]),
          nav_section: firstStringFromUnknownValue([
            record.nav_section,
            section,
            "system",
          ]),
          sort_order: Number(record.sort_order || 100),
          current_pages: Array.isArray(record.current_pages)
            ? record.current_pages
                .map((value) => firstStringFromUnknownValue([value, ""]).trim())
                .filter(isTruthy)
            : [],
        });
      });
      sections[firstStringFromUnknownValue([section, "system"])] = rows;
    },
  );
  return sections;
}

/** Keeps the pages whose `required_entitlement` (one feature or any of a list) the account holds. */
function entitledPages<T extends Record<string, unknown>>(
  pages: T[],
  flags: Record<string, boolean>,
): T[] {
  return pages.filter((page) => holdsAnyEntitlement(flags, page.required_entitlement));
}

function entitledNavigation(
  sections: Record<string, ShellNavigationItem[]>,
  flags: Record<string, boolean>,
): Record<string, ShellNavigationItem[]> {
  return Object.fromEntries(
    Object.entries(sections).map(([section, items]) => [section, entitledPages(items, flags)]),
  );
}

function toAuthSession(payload: AppBootstrapResponse): AuthSession {
  const featureFlags = payload.feature_flags || {};
  return {
    authenticated: Boolean(payload.auth?.authenticated),
    local_auth_enabled: Boolean(payload.auth?.local_auth_enabled),
    bootstrap_required: Boolean(payload.auth?.bootstrap_required),
    external_auth_enabled: Boolean(payload.auth?.external_auth_enabled),
    system_email_available: Boolean(payload.auth?.system_email_available),
    auth_source: firstStringFromUnknownValue([payload.auth?.auth_source, ""]),
    user: payload.auth?.user,
    roles: Array.isArray(payload.auth?.roles) ? payload.auth.roles : [],
    permissions:
      payload.auth?.permissions && typeof payload.auth.permissions === "object"
        ? payload.auth.permissions
        : anonymousSession.permissions,
    account_memberships: Array.isArray(payload.auth?.account_memberships)
      ? payload.auth.account_memberships
      : [],
    i18n: payload.i18n || fallbackI18n,
    feature_flags: featureFlags,
    feature_endpoints: normalizeFeatureEndpoints(payload.feature_endpoints),
    external_sign_in: normalizeExternalSignIn(payload.auth?.external_sign_in),
    environment: {
      ...(payload.environment || fallbackEnvironment),
      mode: payload.environment?.mode === "development" ? "development" : "production",
    },
    settings_pages: entitledPages(normalizeSettingsPages(payload.settings_pages), featureFlags),
    registered_pages: entitledPages(normalizeRegisteredPages(payload.registered_pages), featureFlags),
    shell_navigation: entitledNavigation(normalizeShellNavigation(payload.shell_navigation), featureFlags),
    run_upload_limits: normalizeRunUploadLimits(payload.limits?.run_upload),
    file_preview_max_bytes: normalizeFilePreviewMaxBytes(payload.limits?.file_preview),
  };
}

async function fetchAuthSession(): Promise<AuthSession> {
  try {
    const appBootstrap =
      await fetchJson<AppBootstrapResponse>("/api/app/bootstrap");
    if (appBootstrap?.auth && typeof appBootstrap.auth === "object") {
      return toAuthSession(appBootstrap);
    }
  } catch (error) {
    if (!(error instanceof HttpError) || error.status !== 404) {
      if (error instanceof HttpError && error.status === 401) {
        return anonymousSession;
      }
      throw error;
    }
  }

  try {
    const authMe = await fetchJson<{
      authenticated: boolean;
      local_auth_enabled: boolean;
      external_auth_enabled: boolean;
      auth_source?: string;
      user?: AuthUser;
    }>("/api/auth/me");

    return {
      ...anonymousSession,
      authenticated: Boolean(authMe.authenticated),
      local_auth_enabled: Boolean(authMe.local_auth_enabled),
      bootstrap_required: false,
      external_auth_enabled: Boolean(authMe.external_auth_enabled),
      auth_source: firstStringFromUnknownValue([authMe.auth_source, ""]),
      user: authMe.user,
      roles: Array.isArray(authMe.user?.roles) ? authMe.user.roles : [],
      account_memberships: Array.isArray(authMe.user?.account_memberships)
        ? authMe.user.account_memberships
        : [],
    };
  } catch (error) {
    if (error instanceof HttpError && error.status === 401) {
      return anonymousSession;
    }
    throw error;
  }
}

export function AuthProvider({ children }: Readonly<PropsWithChildren>) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.authSession,
    queryFn: fetchAuthSession,
    // A signed-out answer is a 401, which `fetchAuthSession` turns into the
    // anonymous session. Anything else — a 5xx while the runtime restarts, a
    // dropped connection — says nothing about the session, so a cold load
    // tries again before any guard reads it as "signed out" and leaves the
    // page it was asked for.
    retry: (failureCount, error) => failureCount < AUTH_SESSION_RETRIES && sessionErrorIsTransient(error),
    retryDelay: (attempt) => AUTH_SESSION_RETRY_DELAY_MS * (attempt + 1),
  });

  const refresh = useCallback(async () => {
    await queryClient.refetchQueries({ queryKey: queryKeys.authSession });
  }, [queryClient]);

  const logout = useCallback(async () => {
    if (query.data?.authenticated) {
      await fetchJson<{ status: string }>("/api/auth/logout", {
        method: "POST",
      });
    }
    await purgeAuthenticatedClientState(
      queryClient,
      globalThis.window === undefined ? null : globalThis.localStorage,
    );
    await refresh();
  }, [query.data?.authenticated, queryClient, refresh]);

  const switchAccount = useCallback(
    async (accountId: string) => {
      const normalizedAccountId = firstStringFromUnknownValue([
        accountId,
        "",
      ]).trim();
      if (!normalizedAccountId) {
        return;
      }
      await fetchJson<{
        status: string;
        account_id: string;
        account_ids: string[];
        account_memberships: Array<Record<string, unknown>>;
      }>("/api/auth/switch-account", {
        method: "POST",
        body: JSON.stringify({ account_id: normalizedAccountId }),
      });
      await dropAccountScopedQueries(queryClient);
      await refresh();
    },
    [queryClient, refresh],
  );

  const session = query.data ?? null;
  let status: AuthContextValue["status"] = "anonymous";
  // Pending, not isLoading: a retry waiting for the tab to regain focus (or
  // the network to return) is paused, so isLoading is false while nothing is
  // known yet, and the guards read that as "signed out".
  if (query.isPending) {
    status = "loading";
  } else if (session?.authenticated) {
    status = "authenticated";
  }
  const contextValue = useMemo(
    () => ({
      error: query.error instanceof Error ? query.error.message : null,
      isAuthenticated: Boolean(session?.authenticated),
      logout,
      refresh,
      switchAccount,
      session,
      status,
    }),
    [logout, query.error, refresh, session, status, switchAccount],
  );

  return (
    <AuthContext.Provider value={contextValue}>{children}</AuthContext.Provider>
  );
}
