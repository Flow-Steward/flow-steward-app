import { fetchJson } from "@/lib/http/client";

export type ExtensionOAuthScope = {
  projectId: string;
  installId: string;
  providerId: string;
};

export type OAuthConfigurationFieldView = {
  field_id: string;
  value?: string;
  secret_present?: boolean;
};

export type OAuthConfigurationView = {
  configuration_id: string;
  display_name: string;
  status: "incomplete" | "ready_for_authorization" | "verified" | "needs_attention";
  fields: OAuthConfigurationFieldView[];
  revision: number;
  authorization_revision: number;
};

export type OAuthConfigurationMutation = {
  configuration_id?: string;
  display_name: string;
  values: Record<string, string>;
  secret_replacements: Record<string, string>;
  expected_revision?: number;
  confirmation_token?: string;
};

export type OAuthConfigurationImpact = {
  configuration_id?: string;
  expected_revision?: number;
  changed_fields: string[];
  change_policies: string[];
  connections: Array<{
    connection_id: string;
    display_name?: string;
    connection_type?: string;
  }>;
  requires_confirmation: boolean;
  confirmation_token?: string;
  /** Workflow steps bound to the connections a delete would remove. */
  workflow_steps?: Array<{
    workflow_id: string;
    workflow_name: string;
    group_name: string;
    step_name: string;
    connection_id: string;
  }>;
};

export type OAuthAuthorizationStart = {
  configuration_id: string;
  connection_type: string;
  intent_id: string;
  display_name: string;
  return_page_id: string;
  intent_inputs: Record<string, string>;
  existing_connection_id?: string;
};

export type OAuthAuthorizationStartResult = {
  authorization_url: string;
  expires_at: string;
  redirect_uri: string;
};

export type OAuthPostConnectRetryResult = {
  outcome:
    | "connected"
    | "needs_attention"
    | "authorization_required"
    | "authorization_denied"
    | "restart_required"
    | "provider_unavailable";
  connection_id?: string;
  help_target?: {
    page_id: string;
    anchor?: string;
  };
};

function prefix(scope: ExtensionOAuthScope): string {
  return `/api/projects/${encodeURIComponent(scope.projectId)}/extensions/${encodeURIComponent(scope.installId)}/oauth/${encodeURIComponent(scope.providerId)}`;
}

export function fetchExtensionOAuthConfigurations(scope: ExtensionOAuthScope) {
  return fetchJson<OAuthConfigurationView[]>(`${prefix(scope)}/configurations`, {
    cache: "no-store",
  });
}

export function ensureExtensionOAuthRedirectUri(scope: ExtensionOAuthScope) {
  return fetchJson<{ redirect_uri: string }>(`${prefix(scope)}/callback-registration`, {
    method: "PUT",
  });
}

export function fetchExtensionOAuthSetupStatus(scope: ExtensionOAuthScope) {
  return fetchJson<{ callback_registration_ready: boolean }>(`${prefix(scope)}/setup-status`, {
    cache: "no-store",
  });
}

export function previewExtensionOAuthConfiguration(
  scope: ExtensionOAuthScope,
  mutation: OAuthConfigurationMutation,
) {
  return fetchJson<OAuthConfigurationImpact>(`${prefix(scope)}/configurations/impact`, {
    method: "POST",
    body: JSON.stringify(mutation),
  });
}

export function saveExtensionOAuthConfiguration(
  scope: ExtensionOAuthScope,
  mutation: OAuthConfigurationMutation,
) {
  return fetchJson<OAuthConfigurationView>(`${prefix(scope)}/configurations`, {
    method: "PUT",
    body: JSON.stringify(mutation),
  });
}

export function startExtensionOAuthAuthorization(
  scope: ExtensionOAuthScope,
  command: OAuthAuthorizationStart,
) {
  return fetchJson<OAuthAuthorizationStartResult>(`${prefix(scope)}/authorization`, {
    method: "POST",
    body: JSON.stringify(command),
  });
}

export function retryExtensionOAuthPostConnect(
  scope: ExtensionOAuthScope,
  connectionId: string,
  command: { configuration_id: string; connection_type: string },
) {
  return fetchJson<OAuthPostConnectRetryResult>(
    `${prefix(scope)}/connections/${encodeURIComponent(connectionId)}/post-connect/retry`,
    { method: "POST", body: JSON.stringify(command) },
  );
}

export function disconnectExtensionOAuthConnection(
  scope: ExtensionOAuthScope,
  configurationId: string,
  connectionId: string,
) {
  return fetchJson<Record<string, unknown>>(
    `${prefix(scope)}/configurations/${encodeURIComponent(configurationId)}/connections/${encodeURIComponent(connectionId)}/disconnect`,
    { method: "POST" },
  );
}

/** Move a setup left on an earlier extension release to the installed one. */
export function reconcileExtensionOAuthConfigurationRelease(
  scope: ExtensionOAuthScope,
  configurationId: string,
) {
  return fetchJson<OAuthConfigurationView>(
    `${prefix(scope)}/configurations/${encodeURIComponent(configurationId)}/reconcile-release`,
    { method: "POST" },
  );
}

export function previewExtensionOAuthConfigurationDelete(
  scope: ExtensionOAuthScope,
  configurationId: string,
  expectedRevision: number,
) {
  return fetchJson<OAuthConfigurationImpact>(
    `${prefix(scope)}/configurations/${encodeURIComponent(configurationId)}/delete-impact`,
    { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) },
  );
}

export function deleteExtensionOAuthConfiguration(
  scope: ExtensionOAuthScope,
  configurationId: string,
  expectedRevision: number,
  confirmationToken?: string,
) {
  return fetchJson<{ deleted: boolean }>(
    `${prefix(scope)}/configurations/${encodeURIComponent(configurationId)}`,
    {
      method: "DELETE",
      body: JSON.stringify({
        expected_revision: expectedRevision,
        ...(confirmationToken ? { confirmation_token: confirmationToken } : {}),
      }),
    },
  );
}
