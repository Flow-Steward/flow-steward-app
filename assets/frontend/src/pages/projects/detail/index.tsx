import { useI18n } from "@/lib/i18n/useI18n";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useEffect, useMemo, useState } from "react";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useParams, useSearchParams } from "react-router";

import type { AssignmentSelectorRow } from "@/components/shared/assignment-selector";
import { DsAlert, DsPageHeader, PageContainer } from "@/components/shared/design-system";
import { useHasCapability } from "@/lib/auth/capabilities";
import { useRememberProject } from "@/lib/utils/use-remember-project";
import { queryKeys } from "@/lib/query/keys";
import { fetchProjectDetail, updateProject, type ProjectEditInput } from "@/pages/projects/detail/api";
import { ProjectEditForm } from "@/pages/projects/detail/project-edit-form";
import {
  connectionSearchText,
  listFromUnknown,
  normalizeProjectTab,
  normalizeProviderModels,
  objectOrEmpty,
  textOrEmpty,
  toStringArray,
  tupleProviderOptions,
  workflowAgentIds,
  type AgentCatalogMode,
  type AgentOverrideDraft,
  type DataConnectionStatusFilter,
  type Entity,
  type ProjectEditNotice,
  type ProjectTabKey,
  type WorkflowStatusFilter,
} from "@/pages/projects/detail/project-edit-helpers";
import { fetchProjectWorkflowBindings } from "@/pages/projects/workflows/api";
import { fetchWorkspaces } from "@/pages/registry/workspaces/api";
import { fetchWorkflowDetail, fetchWorkflows } from "@/pages/workflows/shared/api";
import { isTruthy } from "@/lib/utils/unknown-values";

function projectSubmissionError({
  name,
  staleWorkflowIds,
  workspaceId,
}: Readonly<{
  name: string;
  staleWorkflowIds: string[];
  workspaceId: string;
}>, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (!name) return t("react.projects.project_name_is_required", "Project name is required.");
  if (!workspaceId) return t("react.projects.workspace_is_required", "Workspace is required.");
  if (staleWorkflowIds.length) {
    return t("react.projects.this_project_references_missing_workflow_value1_remove_it_before_saving_project_changes", "This project references missing workflow \"{value1}\". Remove it before saving project changes.", { value1: String(staleWorkflowIds[0]) });
  }
  return "";
}

function submittedWorkflowSelection(
  selectedWorkflowIds: string[],
  catalog: Entity[],
  catalogLoaded: boolean,
  selectedDefaultWorkflowId: string,
): { defaultWorkflowId: string; workflowIds: string[] } {
  const catalogWorkflowIds = new Set(
    catalog.map((workflow) => textOrEmpty(workflow.workflow_id)).filter(isTruthy),
  );
  const workflowIds = selectedWorkflowIds.filter(
    (workflowId) => !catalogLoaded || catalogWorkflowIds.has(workflowId),
  );
  return {
    defaultWorkflowId: workflowIds.includes(selectedDefaultWorkflowId)
      ? selectedDefaultWorkflowId
      : "",
    workflowIds,
  };
}

function appendAgentRoutingFields(
  payload: ProjectEditInput & Record<string, unknown>,
  formData: FormData,
) {
  for (const [key, value] of formData.entries()) {
    if (key.startsWith("agent_provider_") || key.startsWith("agent_model_")) {
      payload[key] = textOrEmpty(value);
    }
  }
}

function buildProjectEditPayload({
  dataConnectionIds,
  defaultModel,
  defaultProvider,
  defaultWorkflowId,
  formData,
  name,
  workflowIds,
  workspaceId,
}: Readonly<{
  dataConnectionIds: string[];
  defaultModel: string;
  defaultProvider: string;
  defaultWorkflowId: string;
  formData: FormData;
  name: string;
  workflowIds: string[];
  workspaceId: string;
}>): ProjectEditInput & Record<string, unknown> {
  const payload: ProjectEditInput & Record<string, unknown> = {
    name,
    workspace_id: workspaceId,
    data_connection_ids: dataConnectionIds,
    project_rules: textOrEmpty(formData.get("project_rules")),
    workflow_ids: workflowIds,
    operator_default_workflow_id: defaultWorkflowId,
    default_provider: textOrEmpty(defaultProvider),
    default_model: textOrEmpty(defaultModel),
    description: textOrEmpty(formData.get("description")),
  };
  appendAgentRoutingFields(payload, formData);
  return payload;
}

/**
 * The project page. One level: every section of the project - its settings
 * and its history - is a tab of this one strip, reached in one step.
 *
 * Anyone who can read projects reads every tab; without `mutate_projects` the
 * sections render read-only and offer no Save. The server enforces the same
 * rule on every write.
 */
export function ProjectsDetailPage() {
  const { t } = useI18n();
  const { projectId = "" } = useParams();
  const canMutate = useHasCapability("mutate_projects");
  /* Cancel used to leave for the overview, which threw the drafts away. The
     overview is this page now, so Cancel discards them in place: bumping the
     revision re-seeds every draft from the server data and remounts the form's
     uncontrolled fields. */
  const [draftRevision, setDraftRevision] = useState(0);
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<ProjectEditNotice | null>(null);
  const [activeTab, setActiveTab] = useState<ProjectTabKey>(() => normalizeProjectTab(searchParams.get("tab")));

  function reportSuccess(text: string) {
    setNotice({ text, tone: "success" });
  }

  function reportFailure(text: string) {
    setNotice({ text, tone: "danger" });
  }
  const [selectedWorkflowIds, setSelectedWorkflowIds] = useState<string[]>([]);
  const [defaultWorkflowId, setDefaultWorkflowId] = useState("");
  const [workflowSearch, setWorkflowSearch] = useState("");
  const [workflowStatusFilter, setWorkflowStatusFilter] = useState<WorkflowStatusFilter>("all");
  const [selectedDataConnectionIds, setSelectedDataConnectionIds] = useState<string[]>([]);
  const [dataConnectionSearch, setDataConnectionSearch] = useState("");
  const [dataConnectionStatusFilter, setDataConnectionStatusFilter] =
    useState<DataConnectionStatusFilter>("all");
  const [selectedOverrideAgentIds, setSelectedOverrideAgentIds] = useState<string[]>([]);
  const [agentOverrideDrafts, setAgentOverrideDrafts] = useState<Record<string, AgentOverrideDraft>>({});
  const [agentCatalogOpen, setAgentCatalogOpen] = useState(false);
  const [agentCatalogSearch, setAgentCatalogSearch] = useState("");
  const [agentCatalogMode, setAgentCatalogMode] = useState<AgentCatalogMode>("project-relevant");
  const [agentProviderFilter, setAgentProviderFilter] = useState("all");
  const [agentModelFilter, setAgentModelFilter] = useState("all");
  const [workspaceId, setWorkspaceId] = useState("");
  const [defaultProviderDraft, setDefaultProviderDraft] = useState("");
  const [defaultModelDraft, setDefaultModelDraft] = useState("");

  const detailQuery = useQuery({
    queryKey: queryKeys.projectDetail(projectId),
    queryFn: () => fetchProjectDetail(projectId),
    enabled: Boolean(projectId),
  });

  const workflowsQuery = useQuery({
    queryKey: ["projects", projectId, "workflows", "edit-workspace"],
    queryFn: () => fetchProjectWorkflowBindings(projectId),
    enabled: Boolean(projectId),
  });

  const workflowCatalogQuery = useQuery({
    queryKey: ["workflows", "catalog", "project-edit"],
    queryFn: () => fetchWorkflows({ enabledOnly: false, search: "" }),
  });

  const workspacesQuery = useQuery({
    queryKey: ["registry", "workspaces", "project-edit"],
    queryFn: () => fetchWorkspaces(),
  });
  const projectDataReady =
    detailQuery.isSuccess && workflowsQuery.isSuccess && workflowCatalogQuery.isSuccess;
  const workflowCatalogReady = workflowCatalogQuery.isSuccess;
  const workflowCatalogIdSet = useMemo(
    () =>
      new Set(
        (workflowCatalogQuery.data || [])
          .map((workflow) => textOrEmpty(workflow.workflow_id))
          .filter(isTruthy),
      ),
    [workflowCatalogQuery.data],
  );
  // Only request workflow details for ids the catalog actually knows about.
  // Stale bindings (e.g. a deleted workflow id) must never hit /api/workflows/{id} or
  // they 404; they surface as explicit stale rows instead.
  const detailSafeWorkflowIds = useMemo(
    () => selectedWorkflowIds.filter((workflowId) => workflowCatalogIdSet.has(workflowId)),
    [selectedWorkflowIds, workflowCatalogIdSet],
  );
  const workflowDetailQueries = useQueries({
    queries: detailSafeWorkflowIds.map((workflowId) => ({
      queryKey: ["workflows", "detail", workflowId, "project-edit"],
      queryFn: () => fetchWorkflowDetail(workflowId),
      enabled: Boolean(workflowId),
      staleTime: 60_000,
    })),
  });

  const mutation = useMutation({
    mutationFn: (payload: ProjectEditInput) => updateProject(projectId, payload),
    onSuccess: async () => {
      reportSuccess(t("react.projects.project_update_success", "Project updated successfully."));
      await queryClient.invalidateQueries({ queryKey: queryKeys.projectDetail(projectId) });
      await queryClient.invalidateQueries({
        queryKey: ["projects", projectId, "workflows", "edit-workspace"],
      });
    },
    onError: (error) => {
      reportFailure(error instanceof Error ? error.message : t("react.projects.project_update_failed", "Failed to update project."));
    },
  });

  const project = objectOrEmpty(detailQuery.data);
  useRememberProject(projectId || null);
  const workflowPayload = objectOrEmpty(workflowsQuery.data);
  const projectWorkflowRows = useMemo(
    () => listFromUnknown(workflowPayload.project_workflows),
    [workflowPayload.project_workflows],
  );
  const availableWorkflowRows = useMemo(() => {
    const byWorkflowId = new Map<
      string,
      {
        defaultCandidate: boolean;
        displayName: string;
        enabled: boolean;
        linked: boolean;
        stale: boolean;
        workflowId: string;
      }
    >();
    projectWorkflowRows.forEach((workflow) => {
      const workflowId = textOrEmpty(workflow.workflow_id);
      if (!workflowId) {
        return;
      }
      byWorkflowId.set(workflowId, {
        defaultCandidate: Boolean(workflow.is_default),
        displayName: textOrEmpty(
          workflow.display_name ||
            workflow.workflow_display_name ||
            workflow.name ||
            workflow.workflow_id,
        ),
        enabled: workflow.enabled !== false,
        linked: workflow.enabled !== false,
        stale: false,
        workflowId,
      });
    });
    (workflowCatalogQuery.data || []).forEach((workflow) => {
      const workflowId = textOrEmpty(workflow.workflow_id);
      if (!workflowId) {
        return;
      }
      const existing = byWorkflowId.get(workflowId);
      byWorkflowId.set(workflowId, {
        defaultCandidate: existing?.defaultCandidate || false,
        displayName: textOrEmpty(workflow.display_name || workflow.name || workflow.workflow_id),
        enabled: workflow.enabled !== false,
        linked: existing?.linked || false,
        stale: false,
        workflowId,
      });
    });
    // A binding is stale when the catalog has loaded yet still has no entry for it
    // (the workflow was deleted/renamed after the project linked it).
    if (workflowCatalogReady) {
      byWorkflowId.forEach((row, workflowId) => {
        if (!workflowCatalogIdSet.has(workflowId)) {
          row.stale = true;
        }
      });
    }
    return Array.from(byWorkflowId.values()).sort((left, right) => {
      if (left.linked !== right.linked) {
        return left.linked ? -1 : 1;
      }
      return left.displayName.localeCompare(right.displayName);
    });
  }, [projectWorkflowRows, workflowCatalogIdSet, workflowCatalogQuery.data, workflowCatalogReady]);
  const selectedWorkflowIdSet = useMemo(() => new Set(selectedWorkflowIds), [selectedWorkflowIds]);
  // Selected ids the catalog no longer knows about. While any remain selected the
  // project save is blocked (Option B): the operator must explicitly remove them.
  const staleSelectedWorkflowIds = useMemo(
    () =>
      workflowCatalogReady
        ? selectedWorkflowIds.filter((workflowId) => !workflowCatalogIdSet.has(workflowId))
        : [],
    [selectedWorkflowIds, workflowCatalogIdSet, workflowCatalogReady],
  );
  const staleSelectedWorkflowIdSet = useMemo(
    () => new Set(staleSelectedWorkflowIds),
    [staleSelectedWorkflowIds],
  );
  const selectedDefaultWorkflowId =
    selectedWorkflowIdSet.has(defaultWorkflowId) && !staleSelectedWorkflowIdSet.has(defaultWorkflowId)
      ? defaultWorkflowId
      : "";
  const linkedWorkflowCount = selectedWorkflowIds.length;
  const disabledWorkflowCount = availableWorkflowRows.filter((workflow) => !workflow.enabled).length;
  const availableUnlinkedWorkflowCount = availableWorkflowRows.filter(
    (workflow) => !selectedWorkflowIdSet.has(workflow.workflowId),
  ).length;
  const workflowSelectorRows = useMemo<AssignmentSelectorRow[]>(() => {
    return availableWorkflowRows.map((workflow) => {
      const selected = selectedWorkflowIdSet.has(workflow.workflowId);
      const isDefault = selectedDefaultWorkflowId === workflow.workflowId;
      return {
        id: workflow.workflowId,
        title: workflow.displayName || workflow.workflowId,
        subtitle: workflow.workflowId,
        badges: [
          selected
            ? { label: "linked", tone: "linked" as const }
            : { label: "available", tone: "available" as const },
          workflow.stale ? { label: "missing", tone: "disabled" as const } : null,
          !workflow.enabled && !workflow.stale
            ? { label: "disabled", tone: "disabled" as const }
            : null,
          isDefault ? { label: t("react.projects.operator_default_2", "operator default"), tone: "default" as const } : null,
        ].filter(isTruthy) as AssignmentSelectorRow["badges"],
        selected,
        filterKeys: [
          "all",
          selected ? "linked" : "available",
          workflow.stale ? "missing" : "",
          !workflow.enabled ? "disabled" : "",
        ].filter(isTruthy),
        searchText: `${workflow.displayName} ${workflow.workflowId}`.toLowerCase(),
      };
    });
  }, [availableWorkflowRows, selectedDefaultWorkflowId, selectedWorkflowIdSet, t]);
  const availableDataConnectionRows = useMemo(
    () => listFromUnknown(project.available_data_connections),
    [project.available_data_connections],
  );
  const assignedDataConnectionRows = useMemo(
    () => listFromUnknown(project.assigned_data_connections),
    [project.assigned_data_connections],
  );
  const configuredAgentOverrides = useMemo(() => {
    const rows =
      project.agent_model_overrides &&
      typeof project.agent_model_overrides === "object" &&
      !Array.isArray(project.agent_model_overrides)
        ? (project.agent_model_overrides as Entity)
        : {};
    return rows;
  }, [project.agent_model_overrides]);
  const providerOptions = useMemo(() => {
    const routableOptions = tupleProviderOptions(project.available_providers);
    const routableProviderIds = new Set(
      routableOptions.map((provider) => provider.value),
    );
    const persistedProviderIds = new Set<string>();
    const defaultProvider = textOrEmpty(project.default_provider);
    if (defaultProvider) {
      persistedProviderIds.add(defaultProvider);
    }
    Object.values(configuredAgentOverrides).forEach((override) => {
      const provider = textOrEmpty(objectOrEmpty(override).provider);
      if (provider) {
        persistedProviderIds.add(provider);
      }
    });
    const unavailableOptions = Array.from(persistedProviderIds)
      .filter((providerId) => !routableProviderIds.has(providerId))
      .sort((left, right) => left.localeCompare(right))
      .map((providerId) => ({
        value: providerId,
        label: t("react.projects.value1_unavailable", "{value1} (unavailable)", { value1: String(providerId) }),
      }));
    return [
      ...routableOptions,
      ...unavailableOptions,
    ];
  }, [configuredAgentOverrides, project.available_providers, project.default_provider, t]);
  const providerModels = useMemo(
    () => normalizeProviderModels(project.providers_with_models),
    [project.providers_with_models],
  );
  const defaultAgentModels = useMemo(
    () => objectOrEmpty(project.default_agent_models),
    [project.default_agent_models],
  );
  const linkedWorkflowDetails = useMemo(
    () =>
      workflowDetailQueries
        .map((query) => (query.data && typeof query.data === "object" ? query.data : null))
        .filter(isTruthy),
    [workflowDetailQueries],
  );
  const projectRelevantAgentIds = useMemo(() => {
    const ids = new Set<string>();
    linkedWorkflowDetails.forEach((workflow) => {
      workflowAgentIds(workflow).forEach((agentId) => ids.add(agentId));
    });
    Object.keys(configuredAgentOverrides).forEach((agentId) => {
      const normalizedAgentId = textOrEmpty(agentId);
      if (normalizedAgentId) {
        ids.add(normalizedAgentId);
      }
    });
    return Array.from(ids).sort((left, right) => left.localeCompare(right));
  }, [configuredAgentOverrides, linkedWorkflowDetails]);
  const allAgentCatalogRows = useMemo(() => {
    const names = new Set<string>();
    Object.keys(defaultAgentModels).forEach((agentName) => {
      const normalizedAgentName = textOrEmpty(agentName);
      if (normalizedAgentName) {
        names.add(normalizedAgentName);
      }
    });
    Object.keys(configuredAgentOverrides).forEach((agentName) => {
      const normalizedAgentName = textOrEmpty(agentName);
      if (normalizedAgentName) {
        names.add(normalizedAgentName);
      }
    });
    projectRelevantAgentIds.forEach((agentName) => {
      if (agentName) {
        names.add(agentName);
      }
    });
    return Array.from(names)
      .sort((left, right) => left.localeCompare(right))
      .map((agentName) => {
        const defaults = objectOrEmpty(defaultAgentModels[agentName]);
        const override = objectOrEmpty(configuredAgentOverrides[agentName]);
        const defaultProvider = textOrEmpty(defaults.provider);
        const defaultModel = textOrEmpty(defaults.model);
        const overrideProvider = textOrEmpty(override.provider);
        const overrideModel = textOrEmpty(override.model);
        return {
          agentName,
          provider: overrideProvider,
          model: overrideModel,
          defaultProvider,
          defaultModel,
          relevant: projectRelevantAgentIds.includes(agentName),
          overridden: Boolean(overrideProvider || overrideModel),
        };
      });
  }, [configuredAgentOverrides, defaultAgentModels, projectRelevantAgentIds]);
  const selectedDataConnectionIdSet = useMemo(
    () => new Set(selectedDataConnectionIds),
    [selectedDataConnectionIds],
  );
  const dataConnectionSelectorRows = useMemo<AssignmentSelectorRow[]>(() => {
    return availableDataConnectionRows.map((connection) => {
      const connectionId = textOrEmpty(connection.connection_id);
      const selected = selectedDataConnectionIdSet.has(connectionId);
      const enabled = connection.enabled !== false;
      const status = textOrEmpty(connection.status || connection.health_status) || "unknown";
      const unhealthy = !enabled || !["healthy", "ok", "success", "unknown"].includes(status);
      const details = [
        textOrEmpty(connection.engine_hint),
        textOrEmpty(connection.workspace_display_name),
      ].filter(isTruthy);
      return {
        id: connectionId,
        title: textOrEmpty(connection.display_name) || connectionId,
        subtitle: connectionId,
        details,
        badges: [
          selected
            ? { label: "linked", tone: "linked" as const }
            : { label: "available", tone: "available" as const },
          !enabled ? { label: "disabled", tone: "disabled" as const } : null,
          unhealthy ? { label: status, tone: "info" as const } : null,
        ].filter(isTruthy) as AssignmentSelectorRow["badges"],
        selected,
        filterKeys: [
          "all",
          selected ? "linked" : "available",
          !enabled ? "disabled" : "",
          unhealthy ? "unhealthy" : "",
        ].filter(isTruthy),
        searchText: connectionSearchText(connection),
      };
    });
  }, [availableDataConnectionRows, selectedDataConnectionIdSet]);
  const selectedOverrideAgentIdSet = useMemo(
    () => new Set(selectedOverrideAgentIds),
    [selectedOverrideAgentIds],
  );
  const filteredAgentCatalogRows = useMemo(() => {
    return allAgentCatalogRows.filter((row) => {
      const providerKey = row.provider || row.defaultProvider || "default";
      const modelKey = row.model || row.defaultModel || "default";
      if (agentProviderFilter !== "all" && providerKey !== agentProviderFilter) {
        return false;
      }
      if (agentModelFilter !== "all" && modelKey !== agentModelFilter) {
        return false;
      }
      return true;
    });
  }, [agentModelFilter, agentProviderFilter, allAgentCatalogRows]);
  const agentProviderOptions = useMemo(() => {
    return Array.from(
      new Set(
        allAgentCatalogRows
          .map((row) => row.provider || row.defaultProvider)
          .filter((value) => Boolean(textOrEmpty(value))),
      ),
    ).sort((left, right) => left.localeCompare(right));
  }, [allAgentCatalogRows]);
  const agentModelOptions = useMemo(() => {
    return Array.from(
      new Set(
        allAgentCatalogRows
          .map((row) => row.model || row.defaultModel)
          .filter((value) => Boolean(textOrEmpty(value))),
      ),
    ).sort((left, right) => left.localeCompare(right));
  }, [allAgentCatalogRows]);
  const agentSelectorRows = useMemo<AssignmentSelectorRow[]>(() => {
    return filteredAgentCatalogRows.map((row) => ({
      id: row.agentName,
      title: row.agentName,
      subtitle:
        [row.defaultProvider, row.defaultModel].filter(isTruthy).join(" / ") || t("react.projects.uses_platform_defaults", "Uses platform defaults"),
      details: row.relevant ? [t("react.projects.referenced_by_linked_workflows", "Referenced by linked workflows")] : [],
      badges: [
        row.overridden ? { label: "overridden", tone: "linked" as const } : null,
        row.relevant ? { label: "project-relevant", tone: "default" as const } : null,
      ].filter(isTruthy) as AssignmentSelectorRow["badges"],
      selected: selectedOverrideAgentIdSet.has(row.agentName),
      filterKeys: [
        "all",
        row.overridden ? "overridden" : "",
        row.relevant ? "project-relevant" : "",
      ].filter(isTruthy),
      searchText: [
        row.agentName,
        row.defaultProvider,
        row.defaultModel,
        row.provider,
        row.model,
      ]
        .filter(isTruthy)
        .join(" ")
        .toLowerCase(),
    }));
  }, [filteredAgentCatalogRows, selectedOverrideAgentIdSet, t]);
  const visibleOverrideEditors = useMemo(() => {
    return selectedOverrideAgentIds
      .map((agentName) => agentOverrideDrafts[agentName] || allAgentCatalogRows.find((row) => row.agentName === agentName))
      .filter(isTruthy)
      .sort((left, right) => {
        if (left.relevant !== right.relevant) {
          return left.relevant ? -1 : 1;
        }
        return left.agentName.localeCompare(right.agentName);
      });
  }, [agentOverrideDrafts, allAgentCatalogRows, selectedOverrideAgentIds]);
  const configuredOverrideSnapshot = useMemo(
    () =>
      JSON.stringify(
        allAgentCatalogRows
          .filter((row) => row.overridden)
          .map((row) => ({
            agentName: row.agentName,
            defaultModel: row.defaultModel,
            defaultProvider: row.defaultProvider,
            model: row.model,
            provider: row.provider,
            relevant: row.relevant,
          })),
      ),
    [allAgentCatalogRows],
  );
  const configuredOverrideRows = useMemo(() => {
    try {
      const parsed = JSON.parse(configuredOverrideSnapshot) as AgentOverrideDraft[];
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  }, [configuredOverrideSnapshot]);
  const agentCatalogFilterItems: Array<{ key: AgentCatalogMode; label: string; count: number }> = [
    { key: "project-relevant", label: t("react.projects.project_relevant", "Project-relevant"), count: allAgentCatalogRows.filter((row) => row.relevant).length },
    { key: "overridden", label: t("react.projects.overrides", "Overrides"), count: allAgentCatalogRows.filter((row) => row.overridden).length },
    { key: "all", label: t("project_detail.jobs.all", "All"), count: allAgentCatalogRows.length },
  ];
  const agentCatalogMessage =
    agentCatalogMode === "project-relevant" && !projectRelevantAgentIds.length
      ? t("react.projects.no_linked_workflows_are_contributing_project_relevant_agents_yet_switch_to_all_to_browse_the_full_catalog", "No linked workflows are contributing project-relevant agents yet. Switch to All to browse the full catalog.")
      : "";

  const requestedTab = normalizeProjectTab(searchParams.get("tab"));

  useEffect(() => {
    const workflowIdsFromPayload = toStringArray(workflowPayload.workflow_ids);
    const nextSelectedWorkflowIds = workflowIdsFromPayload.length
      ? workflowIdsFromPayload
      : projectWorkflowRows
          .filter((workflow) => workflow.enabled !== false)
          .map((workflow) => textOrEmpty(workflow.workflow_id))
          .filter(isTruthy);
    setSelectedWorkflowIds(nextSelectedWorkflowIds);
    const nextSelectedWorkflowIdSet = new Set(nextSelectedWorkflowIds);
    const configuredDefault = textOrEmpty(workflowPayload.operator_default_workflow_id);
    if (configuredDefault && nextSelectedWorkflowIdSet.has(configuredDefault)) {
      setDefaultWorkflowId(configuredDefault);
    } else {
      const fallbackDefault =
        nextSelectedWorkflowIds.find((workflowId) =>
          availableWorkflowRows.find((workflow) => workflow.workflowId === workflowId)?.defaultCandidate,
        ) || "";
      setDefaultWorkflowId(fallbackDefault);
    }
    // draftRevision: Cancel re-seeds the drafts from the server data.
  }, [availableWorkflowRows, draftRevision, projectWorkflowRows, workflowPayload.operator_default_workflow_id, workflowPayload.workflow_ids]);

  useEffect(() => {
    if (!defaultWorkflowId) {
      return;
    }
    if (selectedWorkflowIdSet.has(defaultWorkflowId) && !staleSelectedWorkflowIdSet.has(defaultWorkflowId)) {
      return;
    }
    setDefaultWorkflowId("");
  }, [defaultWorkflowId, selectedWorkflowIdSet, staleSelectedWorkflowIdSet]);

  useEffect(() => {
    const currentWorkspaceId = textOrEmpty(project.workspace_id);
    if (currentWorkspaceId) {
      setWorkspaceId(currentWorkspaceId);
    } else if (!workspaceId && (workspacesQuery.data || []).length === 1) {
      setWorkspaceId((workspacesQuery.data || [])[0].id);
    }
  }, [project.workspace_id, workspaceId, workspacesQuery.data]);

  useEffect(() => {
    const nextIds = toStringArray(project.data_connection_ids);
    setSelectedDataConnectionIds(nextIds);
  }, [draftRevision, project.data_connection_ids]);

  useEffect(() => {
    if (!detailQuery.isSuccess) {
      return;
    }
    setDefaultProviderDraft(textOrEmpty(project.default_provider));
    setDefaultModelDraft(textOrEmpty(project.default_model));
  }, [detailQuery.isSuccess, draftRevision, project.default_model, project.default_provider]);

  useEffect(() => {
    const nextDrafts = configuredOverrideRows.reduce<Record<string, AgentOverrideDraft>>((acc, row) => {
        acc[row.agentName] = {
          agentName: row.agentName,
          provider: row.provider,
          model: row.model,
          defaultProvider: row.defaultProvider,
          defaultModel: row.defaultModel,
          relevant: row.relevant,
        };
        return acc;
      }, {});
    setAgentOverrideDrafts(nextDrafts);
    setSelectedOverrideAgentIds(
      Object.keys(nextDrafts).sort((left, right) => left.localeCompare(right)),
    );
    setAgentCatalogOpen(false);
  }, [configuredOverrideRows, draftRevision]);

  useEffect(() => {
    setActiveTab(requestedTab);
  }, [requestedTab]);

  function modelOptionsForProvider(provider: string, currentValue = "") {
    const normalizedProvider = textOrEmpty(provider);
    if (!normalizedProvider) {
      return [];
    }
    const providerSpecific = providerModels[normalizedProvider] || [];
    return currentValue && !providerSpecific.some((option) => option.modelId === currentValue)
      ? [{ modelId: currentValue, label: currentValue }, ...providerSpecific]
      : providerSpecific;
  }

  function isModelRouteUnavailable(provider: string, model: string): boolean {
    const normalizedProvider = textOrEmpty(provider);
    const normalizedModel = textOrEmpty(model);
    if (!normalizedProvider && !normalizedModel) {
      return false;
    }
    if (!normalizedProvider || !normalizedModel) {
      return true;
    }
    return !(providerModels[normalizedProvider] || []).some(
      (option) => option.modelId === normalizedModel,
    );
  }

  function nextModelForProvider(provider: string, currentModel: string): string {
    const normalizedProvider = textOrEmpty(provider);
    const normalizedModel = textOrEmpty(currentModel);
    if (!normalizedProvider || !normalizedModel) {
      return "";
    }
    return modelOptionsForProvider(normalizedProvider).some(
      (option) => option.modelId === normalizedModel,
    ) ? normalizedModel : "";
  }

  const defaultModelOptions = modelOptionsForProvider(defaultProviderDraft, defaultModelDraft);
  const modelRouteUnavailable =
    isModelRouteUnavailable(defaultProviderDraft, defaultModelDraft)
    || visibleOverrideEditors.some((row) => isModelRouteUnavailable(row.provider, row.model));

  function addAgentOverride(agentName: string) {
    const normalizedAgentName = textOrEmpty(agentName);
    if (!normalizedAgentName) {
      return;
    }
    const catalogRow = allAgentCatalogRows.find((row) => row.agentName === normalizedAgentName);
    setAgentOverrideDrafts((current) => ({
      ...current,
      [normalizedAgentName]:
        current[normalizedAgentName] || {
          agentName: normalizedAgentName,
          provider: "",
          model: "",
          defaultProvider: catalogRow?.defaultProvider || "",
          defaultModel: catalogRow?.defaultModel || "",
          relevant: Boolean(catalogRow?.relevant),
        },
    }));
    setSelectedOverrideAgentIds((current) =>
      current.includes(normalizedAgentName)
        ? current
        : [...current, normalizedAgentName].sort((left, right) => left.localeCompare(right)),
    );
  }

  function removeAgentOverride(agentName: string) {
    const normalizedAgentName = textOrEmpty(agentName);
    setSelectedOverrideAgentIds((current) => current.filter((item) => item !== normalizedAgentName));
    setAgentOverrideDrafts((current) => {
      const next = { ...current };
      delete next[normalizedAgentName];
      return next;
    });
  }

  function updateAgentOverride(agentName: string, patch: Partial<AgentOverrideDraft>) {
    const normalizedAgentName = textOrEmpty(agentName);
    setAgentOverrideDrafts((current) => ({
      ...current,
      [normalizedAgentName]: {
        ...(current[normalizedAgentName] || {
          agentName: normalizedAgentName,
          provider: "",
          model: "",
          defaultProvider: "",
          defaultModel: "",
          relevant: false,
        }),
        ...patch,
      },
    }));
  }

  function selectProjectTab(tabKey: ProjectTabKey) {
    setActiveTab(tabKey);
    const nextParams = new URLSearchParams(searchParams);
    if (tabKey === "overview") {
      nextParams.delete("tab");
    } else {
      nextParams.set("tab", tabKey);
    }
    setSearchParams(nextParams, { replace: true });
  }

  function discardDrafts() {
    setNotice(null);
    setDraftRevision((current) => current + 1);
  }

  function toggleWorkflow(workflowId: string) {
    setSelectedWorkflowIds((current) => {
      if (current.includes(workflowId)) {
        const next = current.filter((item) => item !== workflowId);
        if (workflowId === defaultWorkflowId) {
          setDefaultWorkflowId("");
        }
        return next;
      }
      return [...current, workflowId];
    });
  }

  function toggleDataConnection(connectionId: string) {
    setSelectedDataConnectionIds((current) => {
      if (current.includes(connectionId)) {
        return current.filter((item) => item !== connectionId);
      }
      return [...current, connectionId].sort((left, right) => left.localeCompare(right));
    });
  }

  function onSubmit(event: React.SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canMutate || !projectDataReady) {
      return;
    }
    const formData = new FormData(event.currentTarget);
    const name = textOrEmpty(formData.get("name"));
    const submissionError = projectSubmissionError({
      name,
      staleWorkflowIds: staleSelectedWorkflowIds,
      workspaceId,
    }, t);
    if (submissionError) {
      reportFailure(submissionError);
      return;
    }
    const workflowSelection = submittedWorkflowSelection(
      selectedWorkflowIds,
      workflowCatalogQuery.data || [],
      workflowCatalogQuery.isSuccess,
      selectedDefaultWorkflowId,
    );
    const payload = buildProjectEditPayload({
      dataConnectionIds: selectedDataConnectionIds,
      defaultModel: defaultModelDraft,
      defaultProvider: defaultProviderDraft,
      defaultWorkflowId: workflowSelection.defaultWorkflowId,
      formData,
      name,
      workflowIds: workflowSelection.workflowIds,
      workspaceId,
    });
    mutation.mutate(payload);
  }

  const tabItems: Array<{ key: ProjectTabKey; label: string }> = [
    { key: "overview", label: t("react.projects.overview", "Overview") },
    { key: "workflows", label: t("project_edit.tab.workflows", "Workflows") },
    // The main menu's own name for the concept, so the two never drift apart.
    { key: "data-connections", label: t("shell.nav.data_connections", "Data Connections") },
    { key: "policy", label: t("project_detail.project_settings.policy", "Policy") },
    { key: "integrations", label: t("react.projects.models", "Models") },
    { key: "notifications", label: t("settings.tab.notifications", "Notifications") },
    { key: "connections", label: t("react.projects.runtime_connections", "Runtime Connections") },
    { key: "datasets", label: t("react.projects.datasets", "Datasets") },
    { key: "history", label: t("react.projects.history", "History") },
  ];
  const workflowFilterItems: Array<{ key: WorkflowStatusFilter; label: string; count: number }> = [
    { key: "all", label: t("project_detail.jobs.all", "All"), count: availableWorkflowRows.length },
    { key: "linked", label: t("react.projects.linked", "Linked"), count: linkedWorkflowCount },
    { key: "available", label: t("react.projects.available", "Available"), count: availableUnlinkedWorkflowCount },
    { key: "disabled", label: t("account_settings.status.disabled", "Disabled"), count: disabledWorkflowCount },
  ];
  const dataConnectionFilterItems: Array<{
    key: DataConnectionStatusFilter;
    label: string;
    count: number;
  }> = [
    { key: "all", label: t("project_detail.jobs.all", "All"), count: dataConnectionSelectorRows.length },
    {
      key: "linked",
      label: t("react.projects.linked", "Linked"),
      count: dataConnectionSelectorRows.filter((row) => row.selected).length,
    },
    {
      key: "available",
      label: t("react.projects.available", "Available"),
      count: dataConnectionSelectorRows.filter((row) => !row.selected).length,
    },
    {
      key: "disabled",
      label: t("account_settings.status.disabled", "Disabled"),
      count: dataConnectionSelectorRows.filter((row) => (row.filterKeys || []).includes("disabled"))
        .length,
    },
    {
      key: "unhealthy",
      label: t("monitoring.health.needs_attention", "Needs attention"),
      count: dataConnectionSelectorRows.filter((row) => (row.filterKeys || []).includes("unhealthy"))
        .length,
    },
  ];

  /* Canon composition item 1: one compact page header with an eyebrow, a title,
     a single sentence and no page-level action. Both states render the same
     header, so the page does not change shape when the data arrives. */
  const pageTitle = textOrEmpty(project.name) || t("react.projects.project_value1", "Project #{value1}", { value1: String(projectId) });
  const pageDescription =
    t("react.projects.project_details_workflow_bindings_data_connections_policies_model_routing_and_history_in_one_place", "Project details, workflow bindings, data connections, policies, model routing and history in one place.");

  const pageBody = detailQuery.isLoading ? (
    <section className="fs-project-header">
      <DsPageHeader description={pageDescription} eyebrow={t("monitoring.stat.projects.label", "Projects")} title={pageTitle} />
      <article className="fs-card">
        <div className="fs-card-body inline-flex items-center gap-2 text-sm fs-ink-secondary">
          <Loader2 className="h-4 w-4 animate-spin" />
          {t("react.projects.loading_project_settings", "Loading project settings…")} </div>
      </article>
    </section>
  ) : (
    <section className="fs-project-header">
      <DsPageHeader description={pageDescription} eyebrow={t("monitoring.stat.projects.label", "Projects")} title={pageTitle} />

      <ProjectEditForm
        activeTab={activeTab}
        addAgentOverride={addAgentOverride}
        agentCatalogFilterItems={agentCatalogFilterItems}
        agentCatalogMessage={agentCatalogMessage}
        agentCatalogMode={agentCatalogMode}
        agentCatalogOpen={agentCatalogOpen}
        agentCatalogSearch={agentCatalogSearch}
        agentModelFilter={agentModelFilter}
        agentModelOptions={agentModelOptions}
        agentProviderFilter={agentProviderFilter}
        agentProviderOptions={agentProviderOptions}
        agentSelectorRows={agentSelectorRows}
        assignedDataConnectionRows={assignedDataConnectionRows}
        availableWorkflowRows={availableWorkflowRows}
        dataConnectionFilterItems={dataConnectionFilterItems}
        dataConnectionSearch={dataConnectionSearch}
        dataConnectionSelectorRows={dataConnectionSelectorRows}
        dataConnectionStatusFilter={dataConnectionStatusFilter}
        dataConnectionsLoading={detailQuery.isLoading}
        defaultModelDraft={defaultModelDraft}
        defaultModelOptions={defaultModelOptions}
        defaultProviderDraft={defaultProviderDraft}
        draftRevision={draftRevision}
        isModelRouteUnavailable={isModelRouteUnavailable}
        linkedWorkflowCount={linkedWorkflowCount}
        modelOptionsForProvider={modelOptionsForProvider}
        mutationPending={mutation.isPending}
        modelRouteUnavailable={modelRouteUnavailable}
        nextModelForProvider={nextModelForProvider}
        notice={notice}
        onDiscard={discardDrafts}
        onSubmit={onSubmit}
        project={project}
        projectDataReady={projectDataReady}
        projectId={projectId}
        projectRelevantAgentIds={projectRelevantAgentIds}
        providerOptions={providerOptions}
        readOnly={!canMutate}
        removeAgentOverride={removeAgentOverride}
        selectProjectTab={selectProjectTab}
        selectedDataConnectionIds={selectedDataConnectionIds}
        selectedDefaultWorkflowId={selectedDefaultWorkflowId}
        selectedOverrideAgentIdSet={selectedOverrideAgentIdSet}
        selectedOverrideAgentIds={selectedOverrideAgentIds}
        selectedWorkflowIds={selectedWorkflowIds}
        staleSelectedWorkflowIds={staleSelectedWorkflowIds}
        setAgentCatalogMode={setAgentCatalogMode}
        setAgentCatalogOpen={setAgentCatalogOpen}
        setAgentCatalogSearch={setAgentCatalogSearch}
        setAgentModelFilter={setAgentModelFilter}
        setAgentProviderFilter={setAgentProviderFilter}
        setDataConnectionSearch={setDataConnectionSearch}
        setDataConnectionStatusFilter={setDataConnectionStatusFilter}
        setDefaultModelDraft={setDefaultModelDraft}
        setDefaultProviderDraft={setDefaultProviderDraft}
        setDefaultWorkflowId={setDefaultWorkflowId}
        setWorkflowSearch={setWorkflowSearch}
        setWorkflowStatusFilter={setWorkflowStatusFilter}
        tabItems={tabItems}
        toggleDataConnection={toggleDataConnection}
        toggleWorkflow={toggleWorkflow}
        updateAgentOverride={updateAgentOverride}
        visibleOverrideEditors={visibleOverrideEditors}
        workflowAssignmentsLoading={workflowsQuery.isLoading || workflowCatalogQuery.isLoading}
        workflowFilterItems={workflowFilterItems}
        workflowSearch={workflowSearch}
        workflowSelectorRows={workflowSelectorRows}
        workflowStatusFilter={workflowStatusFilter}
        workspaceId={workspaceId}
        workspaces={workspacesQuery.data || []}
      />

      {(detailQuery.isError || workflowsQuery.isError || workflowCatalogQuery.isError) ? (
        <DsAlert tone="danger" title={t("react.projects.part_of_the_project_settings_could_not_be_loaded", "Part of the project settings could not be loaded")}>
          {[
            detailQuery.error instanceof Error ? detailQuery.error.message : "",
            workflowsQuery.error instanceof Error ? workflowsQuery.error.message : "",
            workflowCatalogQuery.error instanceof Error ? workflowCatalogQuery.error.message : "",
          ].filter(isTruthy).join(" · ") || t("react.projects.failed_to_load_part_of_the_project_settings", "Failed to load part of the project settings.")}
        </DsAlert>
      ) : null}
    </section>
  );

  return <PageContainer width="standard">{pageBody}</PageContainer>;
}
