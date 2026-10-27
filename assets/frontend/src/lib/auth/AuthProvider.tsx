import { useCallback, useMemo, type PropsWithChildren } from "react";

import { useQuery, useQueryClient } from "@tanstack/react-query";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import {
  AUTH_SESSION_RETRIES,
  AUTH_SESSION_RETRY_DELAY_MS,
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
  account_id: string;
  account_ids: string[];
  account_memberships: Array<Record<string, unknown>>;
  must_change_password?: boolean;
  timezone?: string;
  locale?: string;
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
  component_key: string;
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
  external_sign_in?: ExternalSignInBootstrap;
  environment: AppEnvironmentBootstrap;
  settings_pages: SettingsPageRecord[];
  registered_pages: RegisteredPageRecord[];
  shell_navigation: Record<string, ShellNavigationItem[]>;
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
  environment?: AppEnvironmentBootstrap;
  settings_pages?: unknown;
  registered_pages?: unknown;
  shell_navigation?: unknown;
};

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
    if (!pageKey || !path.startsWith("/") || !componentKey) {
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

function toAuthSession(payload: AppBootstrapResponse): AuthSession {
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
    feature_flags: payload.feature_flags || {},
    external_sign_in: normalizeExternalSignIn(payload.auth?.external_sign_in),
    environment: {
      ...(payload.environment || fallbackEnvironment),
      mode: payload.environment?.mode === "development" ? "development" : "production",
    },
    settings_pages: normalizeSettingsPages(payload.settings_pages),
    registered_pages: normalizeRegisteredPages(payload.registered_pages),
    shell_navigation: normalizeShellNavigation(payload.shell_navigation),
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
      await refresh();
    },
    [refresh],
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
