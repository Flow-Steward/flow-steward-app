
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import {
  Activity,
  Bot,
  Database,
  FileCode2,
  FolderKanban,
  LayoutDashboard,
  Network,
  Settings2,
  ShieldCheck,
  Trash2,
  UserRound,
  Users,
  Waypoints,
  Workflow,
  Wrench,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { ShellNavigationItem } from "@/lib/auth/AuthProvider";
import { fetchJson } from "@/lib/http/client";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export type NavEntry = {
  key: string;
  label: string;
  to: string;
  icon: LucideIcon;
};

export type NavSection = {
  key: string;
  label: string;
  items: NavEntry[];
};

export type AccountMembership = {
  accountId: string;
  accountName: string;
  role: string;
  isActive: boolean;
  isDefault: boolean;
};

export type WorkspaceOption = {
  id: string;
  displayName: string;
  workspaceKey: string;
};

export type GlobalSearchCategory =
  | "all"
  | "jobs"
  | "events"
  | "runtime_messages"
  | "workflows"
  | "agents"
  | "tools"
  | "skills"
  | "mcp_connectors"
  | "projects"
  | "extensions"
  | "guardrails"
  | "data_contracts"
  | "fields"
  | "published_blocks"
  | "datasets"
  | "saved_queries"
  | "data_connections"
  | "model_providers"
  | "model_routes"
  | "users"
  | "workspaces";

export type GlobalSearchItem = {
  id?: string | number;
  group?: string;
  title?: string;
  snippet?: string;
  deep_link?: string | null;
  workflow_id?: string | null;
  job_id?: string | number | null;
  project_id?: string | number | null;
  [key: string]: unknown;
};

export type GlobalSearchGroup = {
  group: string;
  total: number;
  items: GlobalSearchItem[];
  has_more: boolean;
  maybe_more: boolean;
  next_cursor: string | null;
};

export type AwaitingApprovalJob = {
  id?: number | string | null;
  job_id?: number | string | null;
  project_id?: number | string | null;
  project_name?: string | null;
  task_summary?: string | null;
  display_summary?: string | null;
  selected_workflow?: string | null;
  selected_workflow_display_name?: string | null;
  workflow_display_name?: string | null;
  started_at?: string | null;
  thread_id?: string | null;
  status?: string | null;
  status_label?: string | null;
  step_label?: string | null;
  [key: string]: unknown;
};

export type GlobalSearchCategoryOption = {
  value: GlobalSearchCategory;
  label: string;
};

export type PlatformGlobalSearchConfig = {
  categories?: Array<{
    value?: string;
    label?: string;
  }>;
  groupLabels?: Record<string, string>;
  defaultCategory?: string;
};

const ICON_BY_NAME: Record<string, LucideIcon> = {
  account: UserRound,
  activity: Activity,
  agent: Bot,
  dashboard: LayoutDashboard,
  extension: FileCode2,
  file: FileCode2,
  guardrail: ShieldCheck,
  jobs: Network,
  monitoring: Activity,
  process: Wrench,
  project: FolderKanban,
  registry: Database,
  security: ShieldCheck,
  settings: Settings2,
  tools: Wrench,
  trash: Trash2,
  users: Users,
  workflow: Workflow,
  workflows: Workflow,
  changelog: Waypoints,
};



function getGlobalSearchCategoryOptions(t: I18nContextValue["t"] = fallbackContext.t): GlobalSearchCategoryOption[] {
  return [
  { value: "all", label: t("shell.search.category.all", "Everything") },
  { value: "jobs", label: t("jobs.page_title", "Jobs") },
  { value: "events", label: t("monitoring.card.events.title", "Events") },
  { value: "runtime_messages", label: t("shell.search.category.runtime_messages", "Runtime messages") },
  { value: "workflows", label: t("project_edit.tab.workflows", "Workflows") },
  { value: "agents", label: t("registry.agents.title", "Agents") },
  { value: "tools", label: t("registry.agent_detail.tab.tools", "Tools") },
  { value: "skills", label: t("registry.agent_detail.tab.skills", "Skills") },
  { value: "mcp_connectors", label: t("react.shared.mcp_connectors", "MCP connectors") },
  { value: "projects", label: t("monitoring.stat.projects.label", "Projects") },
  { value: "extensions", label: t("settings.extensions.title", "Extensions") },
  { value: "guardrails", label: t("registry.guardrails.title", "Guardrails") },
  { value: "data_contracts", label: t("react.shared.data_contracts", "Data contracts") },
  { value: "fields", label: t("react.shared.fields", "Fields") },
  { value: "published_blocks", label: t("react.shared.published_blocks", "Published blocks") },
  { value: "datasets", label: t("react.shared.datasets", "Datasets") },
  { value: "saved_queries", label: t("react.shared.saved_queries", "Saved queries") },
  { value: "data_connections", label: t("react.shared.data_connections", "Data connections") },
  { value: "model_providers", label: t("react.shared.model_providers", "Model providers") },
  { value: "model_routes", label: t("react.shared.model_routes", "Model routes") },
  { value: "users", label: t("react.shared.users", "Users") },
  { value: "workspaces", label: t("react.shared.workspaces", "Workspaces") },
];
}



function getGlobalSearchGroupLabels(t: I18nContextValue["t"] = fallbackContext.t): Record<string, string> {
  return {
  all: t("shell.search.category.all", "Everything"),
  jobs: t("jobs.page_title", "Jobs"),
  events: t("monitoring.card.events.title", "Events"),
  runtime_messages: t("shell.search.category.runtime_messages", "Runtime messages"),
  workflows: t("project_edit.tab.workflows", "Workflows"),
  agents: t("registry.agents.title", "Agents"),
  tools: t("registry.agent_detail.tab.tools", "Tools"),
  skills: t("registry.agent_detail.tab.skills", "Skills"),
  mcp_connectors: t("react.shared.mcp_connectors", "MCP connectors"),
  projects: t("monitoring.stat.projects.label", "Projects"),
  extensions: t("settings.extensions.title", "Extensions"),
  guardrails: t("registry.guardrails.title", "Guardrails"),
  data_contracts: t("react.shared.data_contracts", "Data contracts"),
  fields: t("react.shared.fields", "Fields"),
  published_blocks: t("react.shared.published_blocks", "Published blocks"),
  datasets: t("react.shared.datasets", "Datasets"),
  saved_queries: t("react.shared.saved_queries", "Saved queries"),
  data_connections: t("react.shared.data_connections", "Data connections"),
  model_providers: t("react.shared.model_providers", "Model providers"),
  model_routes: t("react.shared.model_routes", "Model routes"),
  users: t("react.shared.users", "Users"),
  workspaces: t("react.shared.workspaces", "Workspaces"),
};
}

export function isGlobalSearchCategory(value: unknown): value is GlobalSearchCategory {
  return (
    value === "all" ||
    value === "jobs" ||
    value === "events" ||
    value === "runtime_messages" ||
    value === "workflows" ||
    value === "agents" ||
    value === "tools" ||
    value === "skills" ||
    value === "mcp_connectors" ||
    value === "projects" ||
    value === "extensions" ||
    value === "guardrails" ||
    value === "data_contracts" ||
    value === "fields" ||
    value === "published_blocks" ||
    value === "datasets" ||
    value === "saved_queries" ||
    value === "data_connections" ||
    value === "model_providers" ||
    value === "model_routes" ||
    value === "users" ||
    value === "workspaces"
  );
}

export function readPlatformGlobalSearchConfig(t: I18nContextValue["t"] = fallbackContext.t): {
  categories: GlobalSearchCategoryOption[];
  groupLabels: Record<string, string>;
  defaultCategory: GlobalSearchCategory;
} {
  if (globalThis.window === undefined) {
    return {
      categories: getGlobalSearchCategoryOptions(t),
      groupLabels: getGlobalSearchGroupLabels(t),
      defaultCategory: "all",
    };
  }
  const globalWindow = globalThis as typeof globalThis & {
    __FS_GLOBAL_SEARCH?: PlatformGlobalSearchConfig;
  };
  const raw = globalWindow.__FS_GLOBAL_SEARCH;
  const parsedCategories = Array.isArray(raw?.categories)
    ? raw.categories
        .map((item) => {
          const value = firstStringFromUnknownValue([item?.value, ""])
            .trim()
            .toLowerCase();
          const label = firstStringFromUnknownValue([item?.label, value]).trim();
          if (!isGlobalSearchCategory(value) || !label) {
            return null;
          }
          return {
            value,
            label,
          } satisfies GlobalSearchCategoryOption;
        })
        .filter(isTruthy)
    : [];
  const categories = parsedCategories.length
    ? parsedCategories
    : getGlobalSearchCategoryOptions(t);
  const defaultCategoryRaw = firstStringFromUnknownValue([raw?.defaultCategory, ""])
    .trim()
    .toLowerCase();
  const defaultCategory =
    (isGlobalSearchCategory(defaultCategoryRaw) ? defaultCategoryRaw : null) ||
    categories[0]?.value ||
    "all";
  const groupLabels =
    raw?.groupLabels && typeof raw.groupLabels === "object"
      ? { ...getGlobalSearchGroupLabels(t), ...raw.groupLabels }
      : { ...getGlobalSearchGroupLabels(t) };
  delete groupLabels.kb;
  return {
    categories,
    groupLabels,
    defaultCategory,
  };
}

function iconForNavigation(item: ShellNavigationItem): LucideIcon {
  const raw = firstStringFromUnknownValue([item.nav_icon, item.page_key, ""]).toLowerCase();
  for (const [key, icon] of Object.entries(ICON_BY_NAME)) {
    if (raw.includes(key)) {
      return icon;
    }
  }
  return FileCode2;
}

export function normalizeNavigationEntries(items: ShellNavigationItem[]): NavEntry[] {
  return items
    .map((item) => {
      const to = firstStringFromUnknownValue([item.path, ""]).trim();
      const key = firstStringFromUnknownValue([item.page_key, ""]).trim();
      const label = firstStringFromUnknownValue([item.nav_label, item.title, key, ""]).trim();
      if (!to || !to.startsWith("/") || !key || !label) {
        return null;
      }
      return {
        key,
        label,
        to,
        icon: iconForNavigation(item),
      } satisfies NavEntry;
    })
    .filter(isTruthy);
}

export function dedupeEntries(items: NavEntry[]): NavEntry[] {
  const seenKeys = new Set<string>();
  const seenPaths = new Set<string>();
  const result: NavEntry[] = [];
  items.forEach((entry) => {
    if (seenKeys.has(entry.key) || seenPaths.has(entry.to)) {
      return;
    }
    seenKeys.add(entry.key);
    seenPaths.add(entry.to);
    result.push(entry);
  });
  return result;
}

export function normalizeAccountMemberships(value: unknown): AccountMembership[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const seen = new Set<string>();
  const rows: AccountMembership[] = [];
  value.forEach((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      return;
    }
    const record = item as Record<string, unknown>;
    const accountId = firstStringFromUnknownValue([record.account_id, record.id, ""]).trim();
    if (!accountId || seen.has(accountId)) {
      return;
    }
    seen.add(accountId);
    rows.push({
      accountId,
      accountName: firstStringFromUnknownValue([record.account_name, record.display_name, record.name, accountId]).trim(),
      role: firstStringFromUnknownValue([record.role, record.membership_role, ""]).trim(),
      isActive: record.is_active !== false,
      isDefault: Boolean(record.is_default),
    });
  });
  return rows;
}

export function normalizeWorkspaceOptions(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): WorkspaceOption[] {
  const payload =
    value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  let rawRows: unknown[] = [];
  if (Array.isArray(payload.workspaces)) {
    rawRows = payload.workspaces;
  } else if (Array.isArray(value)) {
    rawRows = value;
  }
  return rawRows
    .filter(
      (item): item is Record<string, unknown> =>
        Boolean(item) && typeof item === "object" && !Array.isArray(item),
    )
    .map((row) => ({
      id: firstStringFromUnknownValue([row.id, ""]).trim(),
      displayName: firstStringFromUnknownValue([row.display_name, row.name, row.workspace_key, t("project_mode.workspace", "Workspace")]).trim(),
      workspaceKey: firstStringFromUnknownValue([row.workspace_key, ""]).trim(),
    }))
    .filter((row) => row.id);
}

function normalizeGlobalSearchGroup(raw: unknown): GlobalSearchGroup | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return null;
  }
  const record = raw as Record<string, unknown>;
  const group = firstStringFromUnknownValue([record.group, ""]).trim();
  if (!group) {
    return null;
  }
  const items = Array.isArray(record.items)
    ? record.items.filter(
        (item): item is GlobalSearchItem =>
          Boolean(item) && typeof item === "object" && !Array.isArray(item),
      )
    : [];
  return {
    group,
    total: Number(record.total || items.length || 0),
    items,
    has_more: Boolean(record.has_more),
    maybe_more: Boolean(record.maybe_more),
    next_cursor: firstStringFromUnknownValue([record.next_cursor, ""]).trim() || null,
  };
}

export function normalizeGlobalSearchGroups(payload: unknown): GlobalSearchGroup[] {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return [];
  }
  const record = payload as Record<string, unknown>;
  const groups = Array.isArray(record.groups)
    ? record.groups
        .map((group) => normalizeGlobalSearchGroup(group))
        .filter(isTruthy)
    : [];
  return groups.filter((group) => group.group !== "kb");
}

export function defaultSearchRoute(item: GlobalSearchItem): string {
  const group = firstStringFromUnknownValue([item.group, ""])
    .trim()
    .toLowerCase();
  if (group === "jobs") {
    const jobId = firstStringFromUnknownValue([item.job_id, item.id, ""]).trim();
    return jobId ? `/jobs/${jobId}` : "/jobs";
  }
  if (group === "events") {
    return "/monitoring?tab=events";
  }
  if (group === "runtime_messages") {
    const jobId = firstStringFromUnknownValue([item.job_id, ""]).trim();
    return jobId ? `/jobs/${jobId}` : "/jobs";
  }
  if (group === "workflows") {
    const workflowId = firstStringFromUnknownValue([item.workflow_id, item.id, ""]).trim();
    return workflowId ? `/registry/workflows/${workflowId}` : "/registry/workflows";
  }
  return "/dashboard";
}

function normalizeAwaitingApprovalJobs(
  payload: unknown,
): AwaitingApprovalJob[] {
  let rows: unknown[] = [];
  if (Array.isArray(payload)) {
    rows = payload;
  } else if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    if (Array.isArray(record.jobs)) {
      rows = record.jobs as unknown[];
    } else if (Array.isArray(record.approvals)) {
      rows = record.approvals as unknown[];
    }
  }
  return rows.filter(
    (row): row is AwaitingApprovalJob =>
      Boolean(row) && typeof row === "object" && !Array.isArray(row),
  );
}

export function approvalJobId(row: AwaitingApprovalJob): string {
  return firstStringFromUnknownValue([row.id, row.job_id, ""]).trim();
}

export function approvalJobTitle(row: AwaitingApprovalJob, t: I18nContextValue["t"] = fallbackContext.t): string {
  const jobId = approvalJobId(row);
  const summary = firstStringFromUnknownValue([row.display_summary, row.task_summary, ""]).trim();
  if (summary) {
    return summary;
  }
  if (jobId) {
    return t("react.shared.job_value1", "Job {value1}", { value1: String(jobId) });
  }
  return summary || "Awaiting approval";
}

export function shellHumanizeLabel(value: unknown): string {
  const text = firstStringFromUnknownValue([value, ""]).trim();
  if (!text) {
    return "";
  }
  const special: Record<string, string> = {
    id: "ID",
    ui: "UI",
    ux: "UX",
  };
  return text
    .replaceAll(/[_-]+/g, " ")
    .split(" ")
    .filter(isTruthy)
    .map(
      (part) =>
        special[part.toLowerCase()] ||
        part.charAt(0).toUpperCase() + part.slice(1),
    )
    .join(" ");
}

function approvalStatusLabel(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  const raw = firstStringFromUnknownValue([value, ""]).trim();
  if (!raw) {
    return "";
  }
  if (raw === "awaiting_approval") {
    return t("react.shared.waiting_for_approval", "Waiting for approval");
  }
  if (raw === "resume_pending") {
    return t("react.shared.resume_queued", "Resume queued");
  }
  return shellHumanizeLabel(raw);
}

export function approvalJobSubtitle(row: AwaitingApprovalJob, t: I18nContextValue["t"] = fallbackContext.t): string {
  const workflowLabel =
    firstStringFromUnknownValue([row.selected_workflow_display_name, row.workflow_display_name, ""]).trim() || shellHumanizeLabel(row.selected_workflow);
  const statusLabel =
    firstStringFromUnknownValue([row.status_label, row.step_label, ""]).trim() ||
    approvalStatusLabel(row.status, t);
  return [row.project_name, workflowLabel, statusLabel]
    .map((value) => firstStringFromUnknownValue([value, ""]).trim())
    .filter(isTruthy)
    .join(" · ");
}

export async function requestAwaitingApprovalJobs(): Promise<AwaitingApprovalJob[]> {
  if (globalThis.window === undefined) {
    return [];
  }
  return normalizeAwaitingApprovalJobs(
    await fetchJson<unknown>("/api/jobs/awaiting-approval", {}, { presentation: "background" }),
  );
}

function eventRecordFromPayload(payload: unknown): Record<string, unknown> {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return {};
  }
  const record = payload as Record<string, unknown>;
  if (
    record.data &&
    typeof record.data === "object" &&
    !Array.isArray(record.data)
  ) {
    return record.data as Record<string, unknown>;
  }
  return record;
}

export function shouldRefreshApprovalsFromEvent(payload: unknown): boolean {
  const record = eventRecordFromPayload(payload);
  if (!Object.keys(record).length) {
    return false;
  }

  const jobId = record.job_id || record.jobId || record.job;
  const eventText = [record.event_type, record.type, record.kind, record.title]
    .map((value) =>
      firstStringFromUnknownValue([value, ""])
        .trim()
        .toLowerCase(),
    )
    .filter(isTruthy)
    .join(" ");
  const statusText = [
    record.status,
    record.job_status,
    record.state,
    record.previous_status,
    record.new_status,
    record.from_status,
    record.to_status,
  ]
    .map((value) =>
      firstStringFromUnknownValue([value, ""])
        .trim()
        .toLowerCase(),
    )
    .filter(isTruthy)
    .join(" ");
  const detailText = [record.summary, record.detail, record.message]
    .map((value) =>
      firstStringFromUnknownValue([value, ""])
        .trim()
        .toLowerCase(),
    )
    .filter(isTruthy)
    .join(" ");
  const combinedText = [eventText, statusText, detailText]
    .filter(isTruthy)
    .join(" ");

  if (
    combinedText.includes("approval") ||
    combinedText.includes("resume_pending") ||
    combinedText.includes("resume pending") ||
    combinedText.includes("awaiting_approval") ||
    combinedText.includes("awaiting approval")
  ) {
    return true;
  }

  const isStatusTransition =
    /(job[._:-]?status|status[._:-]?(?:changed|change|transition|updated)|state[._:-]?(?:changed|change|transition|updated))/i.test(
      eventText,
    ) ||
    Boolean(
      record.previous_status ||
      record.new_status ||
      record.from_status ||
      record.to_status,
    );
  return Boolean(
    jobId &&
    isStatusTransition &&
    /(awaiting_approval|resume_pending|waiting|completed|failed|cancelled|canceled|running|in_progress|done)/.test(
      statusText,
    ),
  );
}

const PRIMARY_THEME_STORAGE_KEY = "platform.ui_theme";
export const APPROVALS_REFRESH_EVENT = "platform:approvals-refresh";

export function normalizeThemeMode(value: unknown): "dark" | "light" | null {
  const rawValue = firstStringFromUnknownValue([value, ""])
    .trim()
    .toLowerCase();
  return rawValue === "light" || rawValue === "dark" ? rawValue : null;
}

export function readStoredThemeMode(): "dark" | "light" | null {
  if (globalThis.window === undefined) {
    return null;
  }
  const storage = globalThis.localStorage as Partial<Storage> | undefined;
  if (!storage || typeof storage.getItem !== "function") {
    return null;
  }
  try {
    return normalizeThemeMode(storage.getItem(PRIMARY_THEME_STORAGE_KEY));
  } catch {
    return null;
  }
}

export function persistThemeMode(value: "dark" | "light"): void {
  if (globalThis.window === undefined) {
    return;
  }
  const storage = globalThis.localStorage as Partial<Storage> | undefined;
  if (!storage || typeof storage.setItem !== "function") {
    return;
  }
  try {
    storage.setItem(PRIMARY_THEME_STORAGE_KEY, value);
  } catch {
    // Ignore localStorage availability errors in restricted browser contexts.
  }
}
