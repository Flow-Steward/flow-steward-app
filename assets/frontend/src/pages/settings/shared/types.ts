export type AccountProfile = {
  id: string;
  email: string;
  username: string;
  display_name: string;
  roles: string[];
  account_id: string;
  account_ids: string[];
  timezone: string;
  locale: string;
  /** Date pattern id ("D MMM YYYY", "MM/DD/YYYY", ...). */
  date_format: string;
  /** "24h" or "12h". */
  time_format: string;
  auth_source: string;
};

export type EmailChangeStatus = {
  new_email?: string;
  expires_at?: string;
  status?: string;
};

export type AccountChoice = {
  id: string;
  name: string;
};

export const TOKEN_SCOPES = [
  "jobs.read",
  "jobs.write",
  "projects.read",
  "projects.write",
  "registry.read",
  "registry.write",
  "workflows.read",
  "workflows.write",
  "reports.read",
  "reports.write",
  "notify.read",
  "notify:dispatch",
] as const;

export type TokenScope = (typeof TOKEN_SCOPES)[number];

export type PersonalToken = {
  id: string;
  label: string;
  scopes: string[];
  allowed_project_ids: string[];
  allowed_workflow_ids: string[];
  created_at?: string;
  expires_at?: string;
  last_used_at?: string;
  revoked_at?: string;
  is_active?: boolean;
};

export type PersonalTokenPage = {
  tokens: PersonalToken[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
};

export type CreatePersonalTokenInput = {
  label: string;
  expires_in_days?: number;
  scopes: (TokenScope | "admin:all")[];
  allowed_project_ids: string[];
  allowed_workflow_ids: string[];
};

export type AccountMembership = {
  account_id?: string;
  id?: string;
  display_name?: string;
  name?: string;
};

export type UserRecord = {
  id: string;
  email: string;
  username?: string;
  display_name: string;
  roles: string[];
  account_id?: string;
  account_ids: string[];
  account_memberships?: AccountMembership[];
  is_active: boolean;
  must_change_password: boolean;
};

export type UserPage = {
  users: UserRecord[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
};

export type UserFilters = {
  search: string;
  role: string;
  status: "" | "active" | "inactive";
  page: number;
  perPage: number;
};

export type CreateUserInput = {
  display_name: string;
  email: string;
  password: string;
  /** One system role (ADR 0076 §7). */
  role: string;
  account_ids?: string[];
  primary_account_id?: string;
  is_active: boolean;
  must_change_password: boolean;
};

export type UpdateUserInput = Partial<
  Pick<
    CreateUserInput,
    | "display_name"
    | "password"
    | "role"
    | "account_ids"
    | "primary_account_id"
    | "is_active"
    | "must_change_password"
  >
>;

export type ProfileField = "display_name" | "timezone" | "locale" | "date_format" | "time_format";

export type ProfileSaveResult = {
  succeeded: ProfileField[];
  failed: Partial<Record<ProfileField, string>>;
};

/** Readiness of the inbound OAuth server for cloud AI apps (ADR 0047, decision 2). */
export type OAuthReadiness = {
  ready: boolean;
  blockers: string[];
};

/** `my_role`: the person's role, intersected with what the app asked for (ADR 0076 §5). */
export type OAuthPresetId = "my_role" | "operational" | "full_authoring";

export type OAuthConsentPreset = {
  id: OAuthPresetId;
  /** The scopes this preset would actually grant: the preset ∩ the request. */
  scopes: string[];
  available: boolean;
  reason: string;
  /** The client asked for fewer scopes than the preset holds. */
  subset_of_preset: boolean;
};

export type OAuthConsentClient = {
  client_id: string;
  client_name: string;
  client_host: string;
  /** `platform`: registered by an administrator of this installation (ADR 0063). */
  client_kind?: "platform" | "cimd";
  redirect_uri: string;
  redirect_uri_host: string;
  redirect_uri_loopback: boolean;
};

/** One permission of the catalogue, in its plain description. */
export type PermissionDescription = { key: string; description: string };

/**
 * ADR 0076 §5: what the app gets "as my role" on the consent screen. The server gives
 * the dates; the page only formats them.
 */
export type OAuthConsentRoleAccess = {
  preset: "my_role";
  role: "admin" | "operator" | "viewer";
  scopes: string[];
  /** What the app will be able to do, from the permission catalogue. */
  permissions: PermissionDescription[];
  /** The app asked for less than the person's role allows. */
  narrower: boolean;
  available: boolean;
  expiry_days: number;
  expires_at: string;
};

export type OAuthConsentView = {
  request_id: string;
  expires_at: string;
  client: OAuthConsentClient;
  user?: { username: string; display_name: string; role: string };
  requested_scopes: string[];
  role_access?: OAuthConsentRoleAccess;
  /** Only an admin chooses a preset, an expiry and projects. */
  advanced_options?: boolean;
  presets: OAuthConsentPreset[];
  default_preset: OAuthPresetId | null;
  expiry_days_options: number[];
  default_expiry_days: number;
};

/** An operator or a viewer sends only the decision: their role, the server's expiry. */
export type OAuthConsentDecision =
  | { decision: "approve"; preset?: OAuthPresetId; expiry_days?: number; project_ids?: string[] }
  | { decision: "deny" };

export type OAuthGrantStatus = "active" | "expired" | "revoked";

export type OAuthGrant = {
  id: string;
  client_id: string;
  client_name: string;
  client_host: string;
  client_kind?: "platform" | "cimd";
  preset: string;
  scopes: string[];
  allowed_project_ids: string[];
  created_at: string;
  expires_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  revoked_reason: string | null;
  status: OAuthGrantStatus;
};

export type OAuthGrantsPage = {
  grants: OAuthGrant[];
  readiness: OAuthReadiness;
};

export type McpOAuthSetting = {
  enabled: boolean;
  readiness: OAuthReadiness;
};

/** Settings → Account → AI agents (task 4): one setup step the wizard renders. */
export type AgentSetupStep = {
  id: string;
  kind: "link" | "command" | "file" | "value" | "notice" | "package" | "platform";
  title_key: string;
  title: string;
  description_key: string;
  description: string;
  /** A note under the step's block; a command in it sits between backticks. */
  hint_key?: string;
  hint?: string;
  values: Record<string, string>;
  /** Filled by the server except the `{token}` placeholder, which only the browser fills. */
  content: string;
  uses_token: boolean;
  uses_mcp_url: boolean;
  file_name: string;
  /** `package` steps: how the browser lays out the archive it builds. */
  archive_layout: string;
  manifest_dir: string;
  /** `platform` steps: the platform OAuth client preset an administrator registers. */
  platform_preset?: string;
  url: string;
};

export type AgentClientPackage = {
  package_id: string;
  offered: boolean;
  available: boolean;
  auth: "personal_token" | "oauth" | "none";
  verified: boolean;
  message_key: string;
  message: string;
  steps: AgentSetupStep[];
};

export type AgentClient = {
  id: string;
  label: string;
  connection: "local" | "cloud";
  description_key: string;
  description: string;
  mcp_blockers: string[];
  packages: AgentClientPackage[];
};

export type AgentSkillPackage = {
  id: string;
  display_name: string;
  short_description: string;
  version: string;
  needs_mcp: boolean;
  description_key: string;
  description: string;
};

/**
 * ADR 0076 §5: the access an agent gets from a person who does not choose one. An
 * operator or a viewer connects with their role's permissions and the server's
 * expiry; only an admin (`advanced_options`) chooses a preset, an expiry or projects.
 */
export type AgentRoleAccess = {
  preset: "my_role";
  scopes: string[];
  expiry_days: number;
  advanced_options: boolean;
};

export type AgentAccessSetup = {
  mcp_url: string;
  mcp_blocker: string;
  cloud_readiness: OAuthReadiness;
  core_version: string;
  packages: AgentSkillPackage[];
  presets: OAuthConsentPreset[];
  expiry_days_options: number[];
  /** The end date of each expiry choice, from the server. */
  expiry_dates?: Record<string, string>;
  default_expiry_days: number;
  role_access: AgentRoleAccess;
  first_request: AgentSetupStep;
  clients: AgentClient[];
};

export type AgentAccessToken = {
  id: string;
  label?: string;
  scopes?: string[];
  allowed_project_ids?: string[];
  expires_at?: string | null;
  created_at?: string | null;
  last_used_at?: string | null;
  revoked?: boolean;
  expired?: boolean;
};

/**
 * Without `preset`, `expiry_days` and `project_ids` the token gets the person's role
 * and the server's expiry; only an admin may send them (ADR 0076 §5).
 */
export type AgentAccessTokenRequest = {
  client_id: string;
  package_id: string;
  preset?: OAuthPresetId;
  expiry_days?: number;
  project_ids?: string[];
};

/**
 * ADR 0076 §5: one AI agent connection of the person, as the AI agents page and the
 * app shell notice show it. Every date and `ending_soon` come from the server.
 */
export type AgentConnection = {
  id: string;
  kind: "personal_token" | "oauth_grant";
  /** The wizard client a token was made for; empty for a cloud client's grant. */
  client_id: string;
  client_label: string;
  created_at: string | null;
  ends_at: string | null;
  last_used_at: string | null;
  days_left: number | null;
  ending_soon: boolean;
  /** `connect_again`: a fresh token from this page; `reconnect_from_client`: OAuth again. */
  renew: "connect_again" | "reconnect_from_client";
};

export type AgentConnectionsPage = {
  connections: AgentConnection[];
  reminder_days: number;
};

export type AgentAccessIssueResult = {
  status: "created" | "replayed";
  token: AgentAccessToken;
  /** The one-time secret; empty on a replay of the same attempt. */
  secret: string;
  secret_already_shown: boolean;
};

/** An admin-registered platform OAuth client (ADR 0063). */
export type PlatformClient = {
  client_id: string;
  preset: string;
  name: string;
  created_at: string | null;
  created_by: string;
  revoked_at: string | null;
};

/** What the platform's own OAuth registration asks for (Teams Developer Portal for Microsoft 365). */
export type PlatformPortalValues = {
  base_url: string;
  authorization_endpoint: string;
  token_endpoint: string;
  refresh_endpoint: string;
  scope: string;
  pkce: boolean;
  client_secret: string;
};

export type PlatformClientsView = {
  presets: Array<{ id: string; name: string; redirect_uris: string[] }>;
  clients: PlatformClient[];
  portal: PlatformPortalValues | null;
  readiness: { ready: boolean; blockers: string[] };
};
