import { fetchJson } from "@/lib/http/client";
import { isHttpError } from "@/lib/http/errors";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import type {
  AccountChoice,
  AccountProfile,
  CreatePersonalTokenInput,
  CreateUserInput,
  EmailChangeStatus,
  PersonalTokenPage,
  ProfileField,
  ProfileSaveResult,
  UpdateUserInput,
  UserFilters,
  UserPage,
} from "@/pages/settings/shared/types";

export type SettingsEntity = Record<string, unknown>;

function objectOrEmpty(value: unknown): SettingsEntity {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as SettingsEntity;
  }
  return {};
}

function listFromPayload(payload: unknown, preferredKey?: string): SettingsEntity[] {
  if (Array.isArray(payload)) {
    return payload.filter((row): row is SettingsEntity => Boolean(row) && typeof row === "object");
  }
  if (payload && typeof payload === "object") {
    const obj = payload as Record<string, unknown>;
    if (preferredKey && Array.isArray(obj[preferredKey])) {
      return obj[preferredKey].filter((row): row is SettingsEntity => Boolean(row) && typeof row === "object");
    }
    for (const value of Object.values(obj)) {
      if (Array.isArray(value)) {
        return value.filter((row): row is SettingsEntity => Boolean(row) && typeof row === "object");
      }
    }
  }
  return [];
}

export async function fetchGeneralSettingsOverview() {
  const [bootstrap, auth, providers] = await Promise.all([
    fetchJson<unknown>("/api/app/bootstrap"),
    fetchJson<unknown>("/api/auth/me"),
    fetchJson<unknown>("/api/model-providers"),
  ]);
  return {
    app_bootstrap: objectOrEmpty(bootstrap),
    auth_session: objectOrEmpty(auth),
    model_providers: listFromPayload(providers, "providers"),
  } satisfies SettingsEntity;
}

export async function fetchCurrentAccountRecord() {
  const payload = await fetchJson<unknown>("/api/auth/accounts/current");
  const account = objectOrEmpty(payload).account;
  return objectOrEmpty(account);
}

export async function saveCurrentAccountRecord(payload: SettingsEntity) {
  const displayName = stringFromUnknownValue(payload.display_name ?? payload.account_id ?? "").trim();
  return fetchJson<unknown>("/api/auth/accounts/current", {
    method: "PATCH",
    body: JSON.stringify({ display_name: displayName }),
  });
}

export async function fetchAccountsCollection(): Promise<AccountChoice[]> {
  const payload = await fetchJson<unknown>("/api/auth/accounts");
  return listFromPayload(payload, "accounts").map((account) => ({
    id: firstStringFromUnknownValue([account.account_id, account.id, ""]).trim(),
    name: firstStringFromUnknownValue([
      account.display_name,
      account.name,
      account.account_id,
      account.id,
      "Unknown account",
    ]).trim(),
  })).filter((account) => Boolean(account.id));
}

export async function fetchAccountProfileRecord(): Promise<{
  profile: AccountProfile;
  email_change_status: EmailChangeStatus;
}> {
  const [authPayload, timezonePayload] = await Promise.all([
    fetchJson<unknown>("/api/auth/me"),
    fetchJson<unknown>("/api/users/me/email-change-status").catch(() => ({})),
  ]);
  const session = objectOrEmpty(authPayload);
  const user = objectOrEmpty(session.user);
  return {
    profile: {
      id: firstStringFromUnknownValue([user.id, ""]),
      email: firstStringFromUnknownValue([user.email, user.username, ""]),
      username: firstStringFromUnknownValue([user.username, user.email, ""]),
      display_name: firstStringFromUnknownValue([user.display_name, user.username, user.email, ""]),
      roles: Array.isArray(user.roles)
        ? user.roles.map((role) => stringFromUnknownValue(role)).filter(Boolean)
        : [],
      account_id: firstStringFromUnknownValue([user.account_id, ""]),
      account_ids: Array.isArray(user.account_ids)
        ? user.account_ids.map((id) => stringFromUnknownValue(id)).filter(Boolean)
        : [],
      timezone: firstStringFromUnknownValue([user.timezone, ""]),
      locale: firstStringFromUnknownValue([user.locale, ""]),
      auth_source: firstStringFromUnknownValue([session.auth_source, ""]),
    },
    email_change_status: objectOrEmpty(timezonePayload) as EmailChangeStatus,
  };
}

export async function saveAccountProfileRecord(
  changes: Partial<Pick<AccountProfile, ProfileField>>,
  t: I18nContextValue["t"] = fallbackContext.t,
): Promise<ProfileSaveResult> {
  const operations: Array<{ field: ProfileField; request: Promise<unknown> }> = [];
  if (changes.display_name !== undefined) {
    operations.push({
      field: "display_name",
      request: fetchJson<unknown>("/api/users/me/profile", {
        method: "PATCH",
        body: JSON.stringify({ display_name: changes.display_name.trim() }),
      }),
    });
  }
  if (changes.timezone !== undefined) {
    operations.push({
      field: "timezone",
      request: fetchJson<unknown>("/api/users/me/timezone", {
        method: "PATCH",
        body: JSON.stringify({ timezone: changes.timezone.trim() }),
      }),
    });
  }
  if (changes.locale !== undefined) {
    operations.push({
      field: "locale",
      request: fetchJson<unknown>("/api/users/me/locale", {
        method: "PATCH",
        body: JSON.stringify({ locale: changes.locale.trim() }),
      }).catch((error) => {
        if (isHttpError(error) && error.status === 404) {
          throw new Error(t("react.settings.locale_save_endpoint_unavailable", "Interface language save endpoint is not available. Restart the backend and try again."));
        }
        throw error;
      }),
    });
  }

  const settled = await Promise.allSettled(operations.map(({ request }) => request));
  const result: ProfileSaveResult = { succeeded: [], failed: {} };
  settled.forEach((entry, index) => {
    const field = operations[index]?.field;
    if (!field) return;
    if (entry.status === "fulfilled") {
      result.succeeded.push(field);
      return;
    }
    result.failed[field] = entry.reason instanceof Error ? entry.reason.message : t("react.shared.save_failed", "Save failed.");
  });
  return result;
}

export async function requestAccountEmailChange(email: string) {
  return fetchJson<unknown>("/api/users/me/email-change/request", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export async function changeAccountPassword(payload: {
  current_password: string;
  new_password: string;
  revoke_other_sessions?: boolean;
}) {
  return fetchJson<unknown>("/api/auth/change-password", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function fetchUsersCollection(filters: UserFilters): Promise<UserPage> {
  const query = new URLSearchParams({
    page: String(filters.page),
    per_page: String(filters.perPage),
  });
  if (filters.search.trim()) query.set("search", filters.search.trim());
  if (filters.role) query.set("role", filters.role);
  if (filters.status) query.set("active", filters.status === "active" ? "true" : "false");
  return fetchJson<UserPage>(`/api/auth/users?${query.toString()}`);
}

export async function createUser(payload: CreateUserInput) {
  return fetchJson<{ status?: string; user?: SettingsEntity; warnings?: string[] }>("/api/auth/users", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateUser(id: string, payload: UpdateUserInput) {
  return fetchJson<{ user?: SettingsEntity }>(`/api/auth/users/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function deleteUser(id: string) {
  return fetchJson<{ status: string; user_id: string; revoked_sessions: number }>(
    `/api/auth/users/${encodeURIComponent(id)}`,
    { method: "DELETE" },
  );
}

export async function forceResetUser(id: string) {
  return fetchJson<unknown>(`/api/auth/users/${encodeURIComponent(id)}/force-password-reset`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function unlockUser(id: string) {
  return fetchJson<unknown>(`/api/auth/users/${encodeURIComponent(id)}/unlock`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function setTemporaryUserPassword(
  id: string,
  password: string,
  mustChangePassword = true,
) {
  return updateUser(id, { password, must_change_password: mustChangePassword });
}

export async function fetchModelProvidersCollection() {
  const payload = await fetchJson<unknown>("/api/model-providers");
  return listFromPayload(payload, "providers");
}

export async function fetchModelCatalogSettings(): Promise<SettingsEntity> {
  return fetchJson<SettingsEntity>("/api/settings/model-catalog");
}

export async function createModelProvider(payload: SettingsEntity) {
  return fetchJson<unknown>("/api/model-providers", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateModelProvider(id: string, payload: SettingsEntity) {
  return fetchJson<unknown>(`/api/model-providers/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export async function reverifyModelProvider(id: string) {
  return fetchJson<unknown>(`/api/model-providers/${encodeURIComponent(id)}/verify`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export async function deleteModelProvider(id: string) {
  return fetchJson<unknown>(`/api/model-providers/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function fetchServiceAccountsCollection() {
  const payload = await fetchJson<unknown>("/api/auth/service-accounts");
  return listFromPayload(payload, "service_accounts");
}

export async function createServiceAccount(payload: SettingsEntity) {
  return fetchJson<unknown>("/api/auth/service-accounts", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function updateServiceAccount(serviceAccountKey: string, payload: SettingsEntity) {
  return fetchJson<unknown>(
    `/api/auth/service-accounts/${encodeURIComponent(serviceAccountKey)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

export async function deleteServiceAccount(serviceAccountKey: string) {
  return fetchJson<unknown>(
    `/api/auth/service-accounts/${encodeURIComponent(serviceAccountKey)}`,
    {
      method: "DELETE",
    },
  );
}

export async function fetchServiceAccountTokensCollection(serviceAccountKey: string) {
  const payload = await fetchJson<unknown>(
    `/api/auth/service-accounts/${encodeURIComponent(serviceAccountKey)}/tokens`,
  );
  return listFromPayload(payload, "tokens");
}

export async function createServiceAccountToken(
  serviceAccountKey: string,
  payload: SettingsEntity,
) {
  return fetchJson<unknown>(
    `/api/auth/service-accounts/${encodeURIComponent(serviceAccountKey)}/tokens`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export async function updateServiceAccountToken(
  serviceAccountKey: string,
  tokenId: string,
  payload: SettingsEntity,
) {
  return fetchJson<unknown>(
    `/api/auth/service-accounts/${encodeURIComponent(serviceAccountKey)}/tokens/${encodeURIComponent(tokenId)}`,
    {
      method: "PATCH",
      body: JSON.stringify(payload),
    },
  );
}

export async function revokeServiceAccountToken(
  serviceAccountKey: string,
  tokenId: string,
) {
  return fetchJson<unknown>(
    `/api/auth/service-accounts/${encodeURIComponent(serviceAccountKey)}/tokens/${encodeURIComponent(tokenId)}/revoke`,
    {
      method: "POST",
      body: JSON.stringify({}),
    },
  );
}

export async function deleteServiceAccountToken(
  serviceAccountKey: string,
  tokenId: string,
) {
  return fetchJson<unknown>(
    `/api/auth/service-accounts/${encodeURIComponent(serviceAccountKey)}/tokens/${encodeURIComponent(tokenId)}`,
    {
      method: "DELETE",
    },
  );
}

export async function fetchExtensionsOverview() {
  const [installed, marketplace] = await Promise.all([
    fetchJson<unknown>("/api/extensions"),
    fetchJson<unknown>("/api/extensions/marketplace"),
  ]);
  return {
    installed: objectOrEmpty(installed),
    marketplace: objectOrEmpty(marketplace),
  } satisfies SettingsEntity;
}

function securityProjectPolicyPath(projectId: string) {
  return `/api/security/projects/${encodeURIComponent(projectId)}/policy`;
}

export async function fetchSecurityPolicyRecord(projectId?: string) {
  if (projectId) {
    const record = await fetchJson<unknown>(
      `${securityProjectPolicyPath(projectId)}?include_sources=1`,
    );
    const entity = objectOrEmpty(record);
    return {
      policy: objectOrEmpty(entity.effective_policy),
      posture: objectOrEmpty(entity.source_map),
      project_override: objectOrEmpty(entity.project_override),
      global_policy: objectOrEmpty(entity.global_policy),
    } satisfies SettingsEntity;
  }

  const [policy, posture] = await Promise.all([
    fetchJson<unknown>("/api/security/policy"),
    fetchJson<unknown>("/api/security/posture"),
  ]);
  return {
    policy: objectOrEmpty(policy),
    posture: objectOrEmpty(posture),
  } satisfies SettingsEntity;
}

export async function saveSecurityPolicyRecord(payload: SettingsEntity, projectId?: string) {
  const body =
    payload.policy && typeof payload.policy === "object" && !Array.isArray(payload.policy)
      ? (payload.policy as SettingsEntity)
      : payload;
  if (projectId) {
    return fetchJson<unknown>(securityProjectPolicyPath(projectId), {
      method: "PUT",
      body: JSON.stringify(body),
    });
  }
  return fetchJson<unknown>("/api/security/policy", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export async function fetchRetentionSettingsRecord() {
  try {
    const payload = await fetchJson<unknown>("/api/settings/retention");
    return objectOrEmpty(payload);
  } catch (error) {
    if (
      isHttpError(error) &&
      error.status === 503 &&
      error.payload &&
      typeof error.payload === "object"
    ) {
      return objectOrEmpty(error.payload);
    }
    throw error;
  }
}

export async function saveRetentionSettingsRecord(payload: SettingsEntity) {
  const settings =
    payload.settings && typeof payload.settings === "object" && !Array.isArray(payload.settings)
      ? objectOrEmpty(payload.settings)
      : payload;

  const body = {
    action_logs_days: Number(settings.action_logs_days || 0),
    job_logs_days: Number(settings.job_logs_days || 0),
    trash_days: Number(settings.trash_days || 0),
    terminal_job_checkpoints_days: Number(settings.terminal_job_checkpoints_days || 0),
    revisions_keep: Number(settings.revisions_keep || 0),
  };

  return fetchJson<unknown>("/api/settings/retention", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function fetchPersonalTokensCollection(
  page = 1,
  perPage = 5,
): Promise<PersonalTokenPage> {
  const query = new URLSearchParams({ page: String(page), per_page: String(perPage) });
  return fetchJson<PersonalTokenPage>(`/api/auth/tokens?${query.toString()}`);
}

export async function createPersonalToken(payload: CreatePersonalTokenInput) {
  return fetchJson<unknown>("/api/auth/tokens", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function revokePersonalToken(tokenId: string) {
  return fetchJson<unknown>(`/api/auth/tokens/${encodeURIComponent(tokenId)}/revoke`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}
