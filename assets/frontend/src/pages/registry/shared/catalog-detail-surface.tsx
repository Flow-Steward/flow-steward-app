import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { formatUserDateTime, useUserTimezone } from "@/lib/time/user-timezone";
import { fetchChangelogHistory } from "@/pages/registry/changelog/api";
import {
  deleteMcpProjectedToolConfirmed,
  deleteRegistryEntity,
  fetchMcpProjectedToolDeletePlan,
  fetchRegistryDetail,
  type McpProjectedToolDeletePlan,
  toggleRegistryEntity,
  updateRegistryEntity,
} from "@/pages/registry/shared/api";
import { McpToolDeletePlanModal } from "@/pages/registry/shared/mcp-tool-delete-plan-modal";
import { RegistryEntityChangelogPanel } from "@/pages/registry/shared/entity-changelog-panel";
import { firstStringFromUnknownValue, isTruthy, stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { useHasCapability } from "@/lib/auth/capabilities";

type RegistryEntity = Record<string, unknown>;
type RegistryProfile = "agents" | "tools" | "skills" | "guardrails" | "generic";
type TabSpec = {
  key: string;
  label: string;
};

type RegistryDetailSurfaceProps = {
  endpoint: string;
  idKey: string;
  idParam: string;
  listPath: string;
  profile?: RegistryProfile;
  readOnly?: boolean;
  readOnlyNotice?: string;
  title: string;
};

function objectOrEmpty(value: unknown): RegistryEntity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as RegistryEntity) : {};
}

function normalizePrimitive(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return stringFromUnknownValue(value);
  }
  if (Array.isArray(value)) {
    return `${value.length} items`;
  }
  // AC17-R100: "object" is not a value a reader can act on, and it reads like
  // content rather than like absence. A field whose shape we cannot render is
  // empty as far as the page is concerned, so it gets the same marker as empty.
  return "\u2014";
}

function normalizeStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => firstStringFromUnknownValue([item, ""]).trim())
    .filter(isTruthy);
}

function normalizeRecordList(value: unknown): RegistryEntity[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(
    (item): item is RegistryEntity => Boolean(item) && typeof item === "object" && !Array.isArray(item),
  );
}

function firstRecordArray(entity: RegistryEntity, keys: string[]): RegistryEntity[] {
  for (const key of keys) {
    const rows = normalizeRecordList(entity[key]);
    if (rows.length) {
      return rows;
    }
  }
  return [];
}

function firstStringArray(entity: RegistryEntity, keys: string[]): string[] {
  for (const key of keys) {
    const rows = normalizeStringList(entity[key]);
    if (rows.length) {
      return rows;
    }
  }
  return [];
}

function firstObject(entity: RegistryEntity, keys: string[]): RegistryEntity {
  for (const key of keys) {
    const value = objectOrEmpty(entity[key]);
    if (Object.keys(value).length) {
      return value;
    }
  }
  return {};
}

function historyRowsFromPayload(payload: unknown): RegistryEntity[] {
  if (Array.isArray(payload)) {
    return payload.filter(
      (row): row is RegistryEntity => Boolean(row) && typeof row === "object" && !Array.isArray(row),
    );
  }
  if (payload && typeof payload === "object") {
    for (const value of Object.values(payload as RegistryEntity)) {
      if (Array.isArray(value)) {
        return value.filter(
          (row): row is RegistryEntity => Boolean(row) && typeof row === "object" && !Array.isArray(row),
        );
      }
    }
  }
  return [];
}

function parseCommaSeparated(value: string): string[] {
  return firstStringFromUnknownValue([value, ""])
    .split(",")
    .map((item) => item.trim())
    .filter(isTruthy);
}

function tabLabelForProfile(profile: RegistryProfile, t: I18nContextValue["t"] = fallbackContext.t): TabSpec[] {
  if (profile === "agents") {
    return [
      { key: "overview", label: t("project_edit.tab.overview", "Overview") },
      { key: "prompts", label: t("react.registry.prompts", "Prompts") },
      { key: "skills", label: t("react.registry.skills", "Skills") },
      { key: "tools", label: t("react.registry.tools", "Tools") },
      { key: "config", label: t("react.registry.config", "Config") },
      { key: "io", label: "I/O" },
      { key: "preview", label: t("registry.workflow_detail.run_lab.tab.preview", "Preview") },
      { key: "changelog", label: t("react.registry.changelog", "Changelog") },
      { key: "payload", label: t("react.registry.payload", "Payload") },
    ];
  }
  if (profile === "tools") {
    return [
      { key: "overview", label: t("project_edit.tab.overview", "Overview") },
      { key: "config", label: t("react.registry.config", "Config") },
      { key: "prompt", label: t("registry.tool_detail.tab.prompt", "Prompt") },
      { key: "agents", label: t("react.registry.agents_using", "Agents Using") },
      { key: "changelog", label: t("react.registry.changelog", "Changelog") },
      { key: "payload", label: t("react.registry.payload", "Payload") },
    ];
  }
  if (profile === "skills") {
    return [
      { key: "overview", label: t("project_edit.tab.overview", "Overview") },
      { key: "config", label: t("react.registry.config", "Config") },
      { key: "instructions", label: t("registry.skills.field.instructions_short", "Instructions") },
      { key: "io", label: "I/O" },
      { key: "agents", label: t("react.registry.agents_title", "Agents") },
      { key: "changelog", label: t("react.registry.changelog", "Changelog") },
      { key: "payload", label: t("react.registry.payload", "Payload") },
    ];
  }
  if (profile === "guardrails") {
    return [
      { key: "overview", label: t("project_edit.tab.overview", "Overview") },
      { key: "config", label: t("react.registry.config", "Config") },
      { key: "scope", label: t("registry.guardrail_detail.tab.agent_scope", "Agent Scope") },
      { key: "changelog", label: t("react.registry.changelog", "Changelog") },
      { key: "payload", label: t("react.registry.payload", "Payload") },
    ];
  }
  return [
    { key: "overview", label: t("project_edit.tab.overview", "Overview") },
    { key: "history", label: t("react.registry.history", "History") },
    { key: "payload", label: t("react.registry.payload", "Payload") },
  ];
}

function profileFromEndpoint(endpoint: string): RegistryProfile {
  const tail = firstStringFromUnknownValue([endpoint, ""])
    .split("/")
    .findLast(isTruthy);
  if (tail === "agents" || tail === "tools" || tail === "skills" || tail === "guardrails") {
    return tail;
  }
  return "generic";
}

function changelogEntityTypeFromEndpoint(endpoint: string): string {
  const tail = firstStringFromUnknownValue([endpoint, ""])
    .split("/")
    .findLast(isTruthy);
  if (!tail) {
    return "";
  }
  if (tail === "data-contracts") {
    return "data_contract";
  }
  if (tail === "published-blocks") {
    return "published_block";
  }
  if (tail === "workflows") {
    return "workflow";
  }
  if (tail === "fields") {
    return "field";
  }
  if (tail.endsWith("s")) {
    return tail.slice(0, -1);
  }
  return tail;
}

function JsonPanel({ title, value }: Readonly<{ title: string; value: unknown }>) {
  return (
    <Card className="border-white/10 bg-white/5">
      <CardHeader>
        <CardTitle className="text-lg">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <pre className="max-h-[58vh] overflow-auto rounded-xl border border-white/10 bg-slate-950/80 p-3 text-xs text-slate-200">
          {JSON.stringify(value, null, 2)}
        </pre>
      </CardContent>
    </Card>
  );
}

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

function tabPanelClass(activeTab: string, ...tabs: string[]): string {
  return tabs.includes(activeTab) ? "space-y-4" : "hidden";
}

function preferredRows(primary: RegistryEntity[], fallback: RegistryEntity[]) {
  return primary.length ? primary : fallback;
}

function preferredStrings(primary: string[], fallback: string[]) {
  return primary.length ? primary : fallback;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function RegistryDetailSurface({
  endpoint,
  idKey,
  idParam,
  listPath,
  profile,
  readOnly = false,
  readOnlyNotice,
  title,
}: Readonly<RegistryDetailSurfaceProps>) {
  const { t } = useI18n();
  const canAdministerPlatform = useHasCapability("administer_platform");
  const effectiveReadOnly = readOnly || !canAdministerPlatform;
  const params = useParams();
  const navigate = useNavigate();
  const userTimezone = useUserTimezone();
  const entityId = firstStringFromUnknownValue([params[idParam], ""]).trim();
  const profileKey = profile || profileFromEndpoint(endpoint);
  const tabs = tabLabelForProfile(profileKey, t);
  const [activeTab, setActiveTab] = useState<string>(tabs[0]?.key || "overview");
  const [saveStatus, setSaveStatus] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [mcpDeletePlan, setMcpDeletePlan] = useState<McpProjectedToolDeletePlan | null>(null);
  const [mcpDeleteError, setMcpDeleteError] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [promptDraft, setPromptDraft] = useState("");
  const [instructionsDraft, setInstructionsDraft] = useState("");
  const [scopeDraft, setScopeDraft] = useState("");

  const detailQuery = useQuery({
    queryKey: [endpoint, "detail", entityId],
    queryFn: () => fetchRegistryDetail(endpoint, entityId),
    enabled: Boolean(entityId),
  });

  const historyQuery = useQuery({
    queryKey: ["registry", "changelog", "history", changelogEntityTypeFromEndpoint(endpoint), entityId],
    queryFn: async () => {
      const payload = await fetchChangelogHistory(changelogEntityTypeFromEndpoint(endpoint), entityId);
      return historyRowsFromPayload(payload);
    },
    enabled: Boolean(entityId),
    retry: false,
  });

  const entity = objectOrEmpty(detailQuery.data);
  const resolvedEntityId = firstStringFromUnknownValue([entity[idKey], entity.id, entityId]).trim();
  const entityName = firstStringFromUnknownValue([entity.display_name, entity.name, entity[`${idKey}`], resolvedEntityId, title]).trim();

  useEffect(() => {
    if (!detailQuery.data) {
      setPromptDraft("");
      setInstructionsDraft("");
      setScopeDraft("");
      return;
    }
    const current = objectOrEmpty(detailQuery.data);
    setPromptDraft(firstStringFromUnknownValue([current.prompt, firstObject(current, ["prompts"]).system, ""]).trim());
    setInstructionsDraft(normalizeStringList(current.instructions).join("\n"));
    setScopeDraft(firstStringArray(current, ["_agents", "agent_ids", "agents"]).join(", "));
  }, [detailQuery.data]);

  const topFacts = useMemo(
    () =>
      [
        ["Identifier", entity[idKey] || entity.id || entityId],
        ["Enabled", entity.enabled],
        ["Version", entity.version],
        ["Category", entity.category],
        ["Updated", formatUserDateTime(entity.updated_at || entity.last_updated_at, userTimezone)],
      ] as const,
    [entity, entityId, idKey, userTimezone],
  );

  const agentPrompts = firstObject(entity, ["prompts"]);
  const agentSkills = firstRecordArray(entity, ["skills", "_skills", "agent_skills"]);
  const agentTools = firstRecordArray(entity, ["tools", "_tools", "agent_tools", "effective_tools"]);
  const agentInputs = firstRecordArray(entity, ["expected_inputs", "_inputs", "inputs"]);
  const agentOutputs = firstRecordArray(entity, ["required_outputs", "expected_outputs", "_outputs", "outputs"]);

  const toolImplementation = firstObject(entity, ["implementation"]);
  const toolAgents = firstStringArray(entity, ["using_agents", "_agents", "agents"]);
  const toolOperations = firstRecordArray(entity, ["operations", "contract", "contract_operations"]);
  const toolPrompt = firstStringFromUnknownValue([entity.prompt, ""]).trim();

  const skillInstructions = normalizeStringList(entity.instructions);
  const skillInputs = firstRecordArray(entity, ["inputs", "expected_inputs"]);
  const skillOutputs = firstRecordArray(entity, ["outputs", "expected_outputs"]);
  const skillAgents = firstStringArray(entity, ["using_agents", "_agents", "agents"]);

  const guardrailScope = firstStringArray(entity, ["_agents", "agent_ids", "agents"]);
  const guardrailConfig = firstObject(entity, ["config"]);

  async function saveEntityPatch(patch: RegistryEntity) {
    if (!resolvedEntityId) {
      setSaveStatus(t("react.registry.display_select_valid_entity", "Select a valid entity before saving."));
      return;
    }
    setSaveStatus("");
    setSaving(true);
    try {
      const payload = { ...entity, ...patch };
      await updateRegistryEntity(endpoint, resolvedEntityId, payload);
      setSaveStatus(t("react.registry.display_saved_period", "Saved."));
      await detailQuery.refetch();
      await historyQuery.refetch();
    } catch (error) {
      setSaveStatus(error instanceof Error ? error.message : t("react.registry.display_failed_save_changes", "Failed to save changes."));
    } finally {
      setSaving(false);
    }
  }

  async function toggleEntityEnabled() {
    if (!resolvedEntityId) {
      setSaveStatus(t("react.registry.display_entity_id_missing", "Entity id is missing."));
      return;
    }
    setSaveStatus("");
    setSaving(true);
    try {
      await toggleRegistryEntity(endpoint, resolvedEntityId);
      setSaveStatus(t("react.registry.display_enabled_state_updated", "Enabled state updated."));
      await detailQuery.refetch();
      await historyQuery.refetch();
    } catch (error) {
      setSaveStatus(error instanceof Error ? error.message : t("react.registry.failed_to_toggle_entity", "Failed to toggle entity."));
    } finally {
      setSaving(false);
    }
  }

  async function deleteEntity() {
    if (!resolvedEntityId) {
      setSaveStatus(t("react.registry.display_entity_id_missing", "Entity id is missing."));
      return;
    }
    setSaveStatus("");
    setDeleting(true);
    try {
      await deleteRegistryEntity(endpoint, resolvedEntityId);
      setDeleteConfirmOpen(false);
      ignoreAsyncResult(navigate(listPath));
    } catch (error) {
      setSaveStatus(error instanceof Error ? error.message : t("react.registry.failed_to_delete_entity", "Failed to delete entity."));
    } finally {
      setDeleting(false);
    }
  }

  async function openDeleteFlow() {
    const implementation = objectOrEmpty(entity.implementation);
    const kind = firstStringFromUnknownValue([implementation.type, implementation.kind, ""]).trim();
    if (endpoint === "/api/tools" && kind === "mcp") {
      setDeleting(true);
      setMcpDeleteError("");
      try {
        setMcpDeletePlan(await fetchMcpProjectedToolDeletePlan(resolvedEntityId));
      } catch (error) {
        setSaveStatus(error instanceof Error ? error.message : t("react.registry.failed_to_load_mcp_tool_impact", "Failed to load MCP tool impact."));
      } finally {
        setDeleting(false);
      }
      return;
    }
    setDeleteConfirmOpen(true);
  }

  async function deleteMcpTool() {
    if (!mcpDeletePlan) return;
    setDeleting(true);
    setMcpDeleteError("");
    try {
      await deleteMcpProjectedToolConfirmed(mcpDeletePlan.tool_id, mcpDeletePlan.revision);
      setMcpDeletePlan(null);
      ignoreAsyncResult(navigate(listPath));
    } catch (error) {
      setMcpDeleteError(error instanceof Error ? error.message : t("react.registry.failed_to_move_mcp_tool_to_trash", "Failed to move MCP tool to Trash."));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <section className="fs-panel-stack">
      <Card className="border-white/10 bg-white/5">
        <CardHeader>
          <p className="text-xs font-medium uppercase tracking-[0.3em] text-cyan-200">{t("trash.category.registry", "Registry")}</p>
          <CardTitle>{entityName || t("react.registry.value0_detail", "{value0} detail", { value0: title })}</CardTitle>
          <CardDescription>
            {effectiveReadOnly
              ? readOnlyNotice || t("react.registry.display_read_only_projection", "This registry projection is read-only and cannot be mutated directly.")
              : t("react.registry.dedicated_detail_route_for_value0_records", "Dedicated detail route for {value0} records.", { value0: title.toLowerCase() })}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Link
            className="rounded-full border border-white/10 px-3 py-1.5 text-xs text-slate-200 hover:border-cyan-300/40"
            to={listPath}
          >
            {t("react.registry.back_to", "Back to")}{" "}{title.toLowerCase()} {" "}{t("react.registry.catalog", "catalog")}</Link>
          <span className="rounded-full border border-white/10 px-3 py-1.5 text-xs text-slate-300">
            {idKey}: {resolvedEntityId || "—"}
          </span>
          {renderWhen(!effectiveReadOnly, (
            <>
              <button
                className="rounded-full border border-white/10 px-3 py-1.5 text-xs text-slate-200 hover:border-cyan-300/40 disabled:opacity-60"
                disabled={saving || deleting}
                onClick={() => {
                  ignoreAsyncResult(toggleEntityEnabled());
                }}
                type="button"
              >
                {entity.enabled ? t("extensions.detail.disable", "Disable") : t("extensions.detail.enable", "Enable")}
              </button>
              <button
                className="rounded-full border border-rose-300/35 bg-rose-500/10 px-3 py-1.5 text-xs font-medium text-rose-100 hover:border-rose-300/55 disabled:opacity-60"
                disabled={saving || deleting}
                onClick={() => ignoreAsyncResult(openDeleteFlow())}
                type="button"
              >
                {t("settings.model_providers.delete", "Delete")}</button>
            </>
          ))}
          {renderWhen(Boolean(saveStatus), (
            <span className="rounded-full border border-cyan-300/30 bg-cyan-500/10 px-3 py-1.5 text-xs text-cyan-100">
              {saveStatus}
            </span>
          ))}
        </CardContent>
      </Card>

      <div className="border-b border-white/10">
        <nav className="-mb-px flex gap-1 overflow-x-auto">
          {tabs.map((tab) => (
            <button
              className={[
                "rounded-t-xl border-b-2 px-4 py-2.5 text-sm font-medium transition",
                activeTab === tab.key
                  ? "border-cyan-300 text-cyan-100"
                  : "border-transparent text-slate-400 hover:text-slate-200",
              ].map((value) => stringFromUnknownValue(value)).join(" ")}
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              type="button"
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      <div className={tabPanelClass(activeTab, "overview")}>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {topFacts.map(([label, value]) => (
            <Card className="border-white/10 bg-white/5" key={label}>
              <CardHeader>
                <CardTitle className="text-sm uppercase tracking-[0.15em] text-slate-300">
                  {label}
                </CardTitle>
              </CardHeader>
              <CardContent className="text-lg font-medium text-white">
                {normalizePrimitive(value)}
              </CardContent>
            </Card>
          ))}
        </div>

        <Card className="border-white/10 bg-white/5">
          <CardHeader>
            <CardTitle className="text-lg">{t("registry.workflows.simulation.description", "Description")}</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-slate-200">
            {normalizePrimitive(entity.description)}
          </CardContent>
        </Card>
      </div>

      <div className={tabPanelClass(activeTab, "config")}>
        <Card className="border-white/10 bg-white/5">
            <CardHeader>
              <CardTitle className="text-lg">{t("registry.tool_detail.configuration", "Configuration")}</CardTitle>
              <CardDescription>
                {t("react.registry.update_the_saved_settings_for_this", "Update the saved settings for this")}{" "}{title.toLowerCase()} {" "}{t("react.registry.record", "record.")}</CardDescription>
            </CardHeader>
          <CardContent className="grid gap-3">
            <label className="grid gap-1 text-xs text-slate-300">
              <span className="uppercase tracking-[0.15em] text-slate-400">{t("users.modal.display_name", "Display Name")}</span>
              <input
                className="rounded-xl border border-white/10 bg-slate-900/70 px-3 py-2 text-sm text-white outline-none focus:border-cyan-300/45"
                defaultValue={firstStringFromUnknownValue([entity.display_name, entity.name, ""])}
                key={`display-name:${resolvedEntityId}`}
                onBlur={(event) => {
                  const value = firstStringFromUnknownValue([event.target.value, ""]).trim();
                  if (!value || value === firstStringFromUnknownValue([entity.display_name, entity.name, ""]).trim()) {
                    return;
                  }
                  ignoreAsyncResult(saveEntityPatch({ display_name: value, name: value }));
                }}
                placeholder={t("react.registry.display_name", "Display name")}
                type="text"
              />
            </label>
            <label className="grid gap-1 text-xs text-slate-300">
              <span className="uppercase tracking-[0.15em] text-slate-400">{t("registry.workflows.simulation.description", "Description")}</span>
              <textarea
                className="min-h-24 rounded-xl border border-white/10 bg-slate-900/70 px-3 py-2 text-sm text-white outline-none focus:border-cyan-300/45"
                defaultValue={firstStringFromUnknownValue([entity.description, ""])}
                key={`description:${resolvedEntityId}`}
                onBlur={(event) => {
                  const value = firstStringFromUnknownValue([event.target.value, ""]).trim();
                  if (value === firstStringFromUnknownValue([entity.description, ""]).trim()) {
                    return;
                  }
                  ignoreAsyncResult(saveEntityPatch({ description: value }));
                }}
                placeholder={t("registry.workflows.simulation.description", "Description")}
              />
            </label>
            {renderWhen(profileKey === "tools", (
              <JsonPanel title={t("react.registry.implementation_contract", "Implementation contract")} value={toolImplementation} />
            ))}
            {renderWhen(profileKey === "guardrails", (
              <JsonPanel title={t("react.registry.rule_configuration", "Rule configuration")} value={guardrailConfig} />
            ))}
          </CardContent>
        </Card>
      </div>

      <div className={tabPanelClass(activeTab, "prompts")}>
        <Card className="border-white/10 bg-white/5">
          <CardHeader>
            <CardTitle className="text-lg">{t("react.registry.prompt_editor", "Prompt editor")}</CardTitle>
            <CardDescription>
              {t("react.registry.system_and_role_prompts_for_this_agent_save_writes_directly_to_the_entity_prompt_set", "System and role prompts for this agent. Save writes directly to the entity prompt set.")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <textarea
              className="min-h-44 w-full rounded-xl border border-white/10 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-300/45"
              onChange={(event) => setPromptDraft(event.target.value)}
              placeholder={t("react.registry.system_prompt", "System prompt...")}
              value={promptDraft}
            />
            <div className="flex flex-wrap gap-2">
              <button
                className="rounded-xl border border-cyan-300/35 bg-cyan-500/10 px-3 py-2 text-xs text-cyan-100 disabled:opacity-60"
                disabled={saving}
                onClick={() => {
                  const prompts = { ...agentPrompts, system: promptDraft.trim() };
                  ignoreAsyncResult(saveEntityPatch({ prompts }));
                }}
                type="button"
              >
                {t("react.registry.save_prompts", "Save prompts")}</button>
              <button
                className="rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-200 hover:border-cyan-300/35"
                onClick={() =>
                  setPromptDraft(firstStringFromUnknownValue([agentPrompts.system, entity.prompt, ""]).trim())
                }
                type="button"
              >
                {t("trash.reset", "Reset")}</button>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className={tabPanelClass(activeTab, "skills")}>
        <Card className="border-white/10 bg-white/5">
          <CardHeader>
            <CardTitle className="text-lg">{t("react.registry.assigned_skills", "Assigned skills")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {agentSkills.map((row, index) => (
              <div
                className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-xs text-slate-200"
                key={`${firstStringFromUnknownValue([row.skill_id, row.id, index])}`}
              >
                <p className="font-medium text-cyan-100">
                  {firstStringFromUnknownValue([row.skill_id, row.id, row.name, `skill-${index + 1}`])}
                </p>
                <p className="mt-1 text-slate-400">
                  {firstStringFromUnknownValue([row.display_name, row.description, t("react.registry.display_no_description_period", "No description.")])}
                </p>
              </div>
            ))}
            {renderWhen(!agentSkills.length, (
              <div className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-300">
                {t("react.registry.no_assigned_skills", "No assigned skills.")}</div>
            ))}
          </CardContent>
        </Card>
      </div>

      <div className={tabPanelClass(activeTab, "tools")}>
        <Card className="border-white/10 bg-white/5">
          <CardHeader>
            <CardTitle className="text-lg">{t("react.registry.tool_access_map", "Tool access map")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {agentTools.map((row, index) => (
              <div
                className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-xs text-slate-200"
                key={`${firstStringFromUnknownValue([row.tool_id, row.id, index])}`}
              >
                <p className="font-medium text-cyan-100">
                  {firstStringFromUnknownValue([row.tool_id, row.id, row.name, `tool-${index + 1}`])}
                </p>
                <p className="mt-1 text-slate-400">
                  {firstStringFromUnknownValue([row.display_name, row.description, t("react.registry.display_no_description_period", "No description.")])}
                </p>
              </div>
            ))}
            {renderWhen(!agentTools.length, (
              <div className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-300">
                {t("react.registry.no_tools_attached", "No tools attached.")}</div>
            ))}
          </CardContent>
        </Card>
      </div>

      <div className={tabPanelClass(activeTab, "io")}>
        <div className="grid gap-4 lg:grid-cols-2">
          <JsonPanel title={t("react.registry.expected_inputs", "Expected inputs")} value={preferredRows(agentInputs, skillInputs)} />
          <JsonPanel title={t("react.registry.expected_outputs", "Expected outputs")} value={preferredRows(agentOutputs, skillOutputs)} />
        </div>
      </div>

      <div className={tabPanelClass(activeTab, "prompt")}>
        <Card className="border-white/10 bg-white/5">
          <CardHeader>
            <CardTitle className="text-lg">{t("react.registry.tool_prompt_variant_0d0b37e6", "Tool prompt")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <textarea
              className="min-h-44 w-full rounded-xl border border-white/10 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-300/45"
              onChange={(event) => setPromptDraft(event.target.value)}
              placeholder={t("react.registry.tool_prompt", "Tool prompt...")}
              value={promptDraft}
            />
            <div className="flex flex-wrap gap-2">
              <button
                className="rounded-xl border border-cyan-300/35 bg-cyan-500/10 px-3 py-2 text-xs text-cyan-100 disabled:opacity-60"
                disabled={saving}
                onClick={() => {
                  ignoreAsyncResult(saveEntityPatch({ prompt: promptDraft.trim() }));
                }}
                type="button"
              >
                {t("react.registry.save_prompt", "Save prompt")}</button>
              <button
                className="rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-200 hover:border-cyan-300/35"
                onClick={() => setPromptDraft(toolPrompt)}
                type="button"
              >
                {t("trash.reset", "Reset")}</button>
            </div>
            <JsonPanel title={t("registry.tool_detail.operations.title", "Operations")} value={toolOperations} />
          </CardContent>
        </Card>
      </div>

      <div className={tabPanelClass(activeTab, "agents")}>
        <Card className="border-white/10 bg-white/5">
          <CardHeader>
            <CardTitle className="text-lg">{t("react.registry.agent_relationships", "Agent relationships")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {preferredStrings(toolAgents, skillAgents).map((agentId) => (
              <div
                className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-xs text-slate-200"
                key={agentId}
              >
                {agentId}
              </div>
            ))}
            {renderWhen(!toolAgents.length && !skillAgents.length, (
              <div className="rounded-xl border border-white/10 bg-slate-900/40 px-3 py-2 text-sm text-slate-300">
                {t("react.registry.no_linked_agents_found", "No linked agents found.")}</div>
            ))}
          </CardContent>
        </Card>
      </div>

      <div className={tabPanelClass(activeTab, "instructions")}>
        <Card className="border-white/10 bg-white/5">
          <CardHeader>
            <CardTitle className="text-lg">{t("react.registry.instruction_set", "Instruction set")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <textarea
              className="min-h-44 w-full rounded-xl border border-white/10 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-300/45"
              onChange={(event) => setInstructionsDraft(event.target.value)}
              placeholder={t("react.registry.one_instruction_per_line", "One instruction per line...")}
              value={instructionsDraft}
            />
            <div className="flex flex-wrap gap-2">
              <button
                className="rounded-xl border border-cyan-300/35 bg-cyan-500/10 px-3 py-2 text-xs text-cyan-100 disabled:opacity-60"
                disabled={saving}
                onClick={() => {
                  ignoreAsyncResult(saveEntityPatch({
                    instructions: instructionsDraft
                      .split("\n")
                      .map((line) => line.trim())
                      .filter(isTruthy),
                  }));
                }}
                type="button"
              >
                {t("react.registry.save_instructions", "Save instructions")}</button>
              <button
                className="rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-200 hover:border-cyan-300/35"
                onClick={() => setInstructionsDraft(skillInstructions.join("\n"))}
                type="button"
              >
                {t("trash.reset", "Reset")}</button>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className={tabPanelClass(activeTab, "scope")}>
        <Card className="border-white/10 bg-white/5">
          <CardHeader>
            <CardTitle className="text-lg">{t("react.registry.agent_scope", "Agent scope")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-xs text-slate-300">
              {t("react.registry.comma_separated_agent_ids_leave_empty_to_apply_this_guardrail_to_all_agents", "Comma-separated agent ids. Leave empty to apply this guardrail to all agents.")}</p>
            <textarea
              className="min-h-24 w-full rounded-xl border border-white/10 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 outline-none focus:border-cyan-300/45"
              onChange={(event) => setScopeDraft(event.target.value)}
              placeholder="researcher, writer, reviewer"
              value={scopeDraft}
            />
            <div className="flex flex-wrap gap-2">
              <button
                className="rounded-xl border border-cyan-300/35 bg-cyan-500/10 px-3 py-2 text-xs text-cyan-100 disabled:opacity-60"
                disabled={saving}
                onClick={() => {
                  ignoreAsyncResult(saveEntityPatch({ _agents: parseCommaSeparated(scopeDraft) }));
                }}
                type="button"
              >
                {t("react.registry.save_scope", "Save scope")}</button>
              <button
                className="rounded-xl border border-white/10 px-3 py-2 text-xs text-slate-200 hover:border-cyan-300/35"
                onClick={() => setScopeDraft(guardrailScope.join(", "))}
                type="button"
              >
                {t("trash.reset", "Reset")}</button>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className={tabPanelClass(activeTab, "preview")}>
        <Card className="border-white/10 bg-white/5">
          <CardHeader>
            <CardTitle className="text-lg">{t("react.registry.agent_preview", "Agent preview")}</CardTitle>
            <CardDescription>
              {t("react.registry.preview_quick_context_and_prompt_contract_for_this_agent_before_runtime_execution", "Preview quick context and prompt contract for this agent before runtime execution.")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="grid gap-2 md:grid-cols-2">
              <div className="rounded-xl border border-white/10 bg-slate-900/60 px-3 py-2 text-xs text-slate-200">
                <p className="uppercase tracking-[0.14em] text-slate-400">{t("registry.workflow_detail.step.system_prompt", "System prompt")}</p>
                <p className="mt-1 whitespace-pre-wrap text-slate-100">
                  {firstStringFromUnknownValue([agentPrompts.system, "(empty)"])}
                </p>
              </div>
              <div className="rounded-xl border border-white/10 bg-slate-900/60 px-3 py-2 text-xs text-slate-200">
                <p className="uppercase tracking-[0.14em] text-slate-400">{t("react.registry.runtime_dependencies", "Runtime dependencies")}</p>
                <p className="mt-1 text-slate-100">
                  {agentSkills.length} {" "}{t("react.registry.skills_variant_cca63e6c", "skills,")}{" "}{agentTools.length} {" "}{t("registry.agents.tools_label", "tools")}</p>
              </div>
            </div>
            <p className="text-xs text-slate-300">
              {t("react.registry.use_the_dedicated_workflow_surfaces_to_run_full_preview_execution_this_tab_keeps_the_detail_route_workflow_awa", "Use the dedicated workflow surfaces to run full preview execution. This tab keeps the detail route workflow-aware while retaining explicit prompt and dependency context.")}</p>
          </CardContent>
        </Card>
      </div>

      <div className={tabPanelClass(activeTab, "payload")}>
        <JsonPanel title={t("react.registry.entity_payload", "Entity payload")} value={entity} />
      </div>

      <div className={tabPanelClass(activeTab, "changelog", "history")}>
        <RegistryEntityChangelogPanel
          entityId={resolvedEntityId || entityId}
          entityType={changelogEntityTypeFromEndpoint(endpoint)}
          loading={historyQuery.isLoading}
          onRestored={async () => {
            await historyQuery.refetch();
            await detailQuery.refetch();
          }}
          rows={historyQuery.data || []}
        />
      </div>

      {renderWhen(detailQuery.isError, (
        <Card className="border-rose-300/30 bg-rose-500/10">
          <CardContent className="text-sm text-rose-100">
            {errorMessage(detailQuery.error, t("react.registry.failed_to_load_registry_detail", "Failed to load registry detail."))}
          </CardContent>
        </Card>
      ))}

      {renderWhen(!effectiveReadOnly && deleteConfirmOpen, (
        <dialog
          aria-modal="true"
          className="fixed inset-0 z-50 m-0 flex h-full w-full max-h-none max-w-none items-center justify-center border-0 bg-black/45 px-4"
          open
        >
          <div className="w-full max-w-md rounded-2xl border border-white/10 bg-slate-950 p-4 shadow-2xl">
            <h2 className="text-lg font-medium text-white">{t("settings.model_providers.delete", "Delete")}{" "}{title.toLowerCase()}</h2>
            <p className="mt-2 text-sm text-slate-300">
              {t("settings.model_providers.delete", "Delete")}{" "}{entityName || resolvedEntityId}{t("react.registry.this_moves_the_record_out_of_the_active_catalog_when_the_backend_supports_soft_delete", "? This moves the record out of the active catalog when the backend supports soft delete.")}</p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                className="rounded-xl border border-white/10 px-3 py-2 text-sm text-slate-200 hover:border-cyan-300/35"
                disabled={deleting}
                onClick={() => setDeleteConfirmOpen(false)}
                type="button"
              >
                {t("extensions.repeater.cancel", "Cancel")}</button>
              <button
                className="rounded-xl border border-rose-300/35 bg-rose-500/10 px-3 py-2 text-sm font-medium text-rose-100 hover:border-rose-300/55 disabled:opacity-60"
                disabled={deleting}
                onClick={() => {
                  ignoreAsyncResult(deleteEntity());
                }}
                type="button"
              >
                {deleting ? t("react.registry.deleting", "Deleting...") : t("settings.model_providers.delete", "Delete")}
              </button>
            </div>
          </div>
        </dialog>
      ))}
      {renderWhen(Boolean(mcpDeletePlan), (
        <McpToolDeletePlanModal
          busy={deleting}
          error={mcpDeleteError}
          onClose={() => {
            setMcpDeletePlan(null);
            setMcpDeleteError("");
          }}
          onConfirm={() => ignoreAsyncResult(deleteMcpTool())}
          plan={mcpDeletePlan as McpProjectedToolDeletePlan}
        />
      ))}
    </section>
  );
}
