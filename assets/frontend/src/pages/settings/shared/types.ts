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
  roles: string[];
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
    | "roles"
    | "account_ids"
    | "primary_account_id"
    | "is_active"
    | "must_change_password"
  >
>;

export type ProfileField = "display_name" | "timezone" | "locale";

export type ProfileSaveResult = {
  succeeded: ProfileField[];
  failed: Partial<Record<ProfileField, string>>;
};
