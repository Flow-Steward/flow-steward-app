import { useI18n } from "@/lib/i18n/useI18n";
import { useCallback, useMemo, useRef, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useNavigate, useSearchParams } from "react-router";

import { type DsFilterField, type DsTableSort } from "@/components/shared/design-system";

import {
  checkMcpConnectorHealth,
  createMcpConnector,
  deleteMcpConnector,
  fetchMcpConnectorDeletePlan,
  fetchMcpConnectors,
  syncMcpConnector,
  type McpConnectorDeletePlan,
  type McpConnectorPayload,
  type McpConnectorRecord,
} from "@/pages/registry/mcp/api";

import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

import { ignoreAsyncResult } from "@/lib/utils/async";

import { useHasCapability } from "@/lib/auth/capabilities";


import { mcpFormErrorField } from "@/pages/registry/mcp/form-errors";

import { RESERVED_OAUTH_PARAMETER_IDENTITIES, keyValueObject } from "./key-value-rows";
import { mcpStatusLabel } from "./mcp-status-label";

import {
  type FormSubmitEvent,
  type McpDraft,
  type Outcome,
  EMPTY_DRAFT,
  buildConnectorColumns,
  connectorCredential,
  connectorHealth,
  isValidHttpEndpoint,
  fetchAllActiveProjects,
  projectCanManage,
  mcpTableState,
} from "./mcp-list-page-helpers";

export function useMcpListState() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  // The grid's filters live in the URL: project, health, credentials. An empty
  // project shows every project the caller may see.
  const projectId = searchParams.get("project_id")?.trim() || "";
  const healthFilter = searchParams.get("health")?.trim() || "";
  const credentialsFilter = searchParams.get("credentials")?.trim() || "";
  const canMutateProjects = useHasCapability("mutate_projects");
  const [createOpen, setCreateOpen] = useState(false);
  const [draft, setDraft] = useState<McpDraft>(EMPTY_DRAFT);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<Outcome>("neutral");
  const [modalError, setModalError] = useState("");
  const [modalErrorField, setModalErrorField] = useState<
    "project" | "name" | "endpoint" | "headers" | "requested_scopes" | "authorization_parameters" | "refresh_token_required" | ""
  >("");
  const [search, setSearch] = useState("");
  const [busyConnectorId, setBusyConnectorId] = useState("");
  const [rowMenu, setRowMenu] = useState<string | null>(null);
  /* Deleting needs the revision the plan reports, so a connector that changed
     under the operator cannot be removed on stale information. */
  const [deletePlan, setDeletePlan] = useState<McpConnectorDeletePlan | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const deleteTriggerLabelRef = useRef("");
  /* Validation focus targets for the create form. */
  const createNameRef = useRef<HTMLInputElement>(null);
  const createProjectRef = useRef<HTMLDivElement>(null);
  const createEndpointRef = useRef<HTMLInputElement>(null);
  const createHeadersRef = useRef<HTMLDivElement>(null);
  const createRequestedScopesRef = useRef<HTMLFieldSetElement>(null);
  const createAuthorizationParametersRef = useRef<HTMLDivElement>(null);
  const createRefreshTokenRequiredRef = useRef<HTMLFieldSetElement>(null);
  const [sort, setSort] = useState<DsTableSort | null>(null);
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(25);

  const projectsQuery = useQuery({
    queryKey: ["registry", "mcp", "projects"],
    queryFn: fetchAllActiveProjects,
    staleTime: 60_000,
  });
  const projects = useMemo(
    () => (Array.isArray(projectsQuery.data) ? projectsQuery.data : []),
    [projectsQuery.data],
  );
  const canManageProject = projectCanManage(projectsQuery.isLoading, projects, canMutateProjects);
  const projectNames = useMemo(
    () => new Map(projects.map((project) => [project.id, project.name || project.id])),
    [projects],
  );
  const projectName = useCallback((id: string) => projectNames.get(id) ?? id, [projectNames]);

  // Every project's connectors in one request; the Project filter narrows them here.
  const connectorsQuery = useQuery({
    queryKey: ["registry", "mcp", "connectors"],
    queryFn: () => fetchMcpConnectors(),
  });
  const connectorProject = useCallback(
    (connectorId: string) => connectorsQuery.data?.find((row) => row.connector_id === connectorId)?.project_id ?? "",
    [connectorsQuery.data],
  );

  const setFilter = useCallback((key: "project_id" | "health" | "credentials", value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next);
    setPage(1);
  }, [searchParams, setSearchParams]);
  const clearFilters = useCallback(() => {
    const next = new URLSearchParams(searchParams);
    for (const key of ["project_id", "health", "credentials"]) next.delete(key);
    setSearchParams(next);
    setPage(1);
  }, [searchParams, setSearchParams]);

  const report = useCallback((text: string, tone: Outcome) => {
    setMessage(text);
    setMessageTone(tone);
  }, []);

  const allConnectors = useMemo(() => connectorsQuery.data || [], [connectorsQuery.data]);
  const filterFields = useMemo<DsFilterField[]>(() => {
    const distinct = (values: string[]) => [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b));
    return [
      {
        key: "project_id",
        label: t("events.columns.project", "Project"),
        options: projects.map((project) => ({ value: project.id, label: project.name || project.id })),
      },
      {
        key: "health",
        label: t("settings.mcp.health", "Health"),
        options: distinct(allConnectors.map(connectorHealth)).map((value) => ({ value, label: mcpStatusLabel(value, t) })),
      },
      {
        key: "credentials",
        label: t("react.registry.credentials", "Credentials"),
        options: distinct(allConnectors.map(connectorCredential)).map((value) => ({ value, label: mcpStatusLabel(value, t) })),
      },
    ];
  }, [allConnectors, projects, t]);

  const connectors = useMemo(() => {
    const rows = allConnectors.filter((connector) =>
      (!projectId || connector.project_id === projectId)
      && (!healthFilter || connectorHealth(connector) === healthFilter)
      && (!credentialsFilter || connectorCredential(connector) === credentialsFilter),
    );
    const normalizedSearch = search.trim().toLowerCase();
    if (!normalizedSearch) {
      return rows;
    }
    return rows.filter((connector) =>
      [
        connector.display_name,
        connector.base_url,
        connector.transport,
        connector.auth_mode,
        connector.auth_status,
        connector.oauth_status.status,
        connector.namespace_prefix,
      ]
        .map((value) => stringFromUnknownValue(value))
        .join(" ")
        .toLowerCase()
        .includes(normalizedSearch),
    );
  }, [allConnectors, projectId, healthFilter, credentialsFilter, search]);

  // `/api/mcp` returns the whole list in one response, so sorting and paging
  // both work over the loaded array and leave the request untouched.
  const sortedConnectors = useMemo(() => {
    if (!sort) {
      return connectors;
    }
    const factor = sort.direction === "asc" ? 1 : -1;
    return [...connectors].sort((left, right) => {
      if (sort.key === "allowlisted_tools") {
        return (left.allowlisted_tools.length - right.allowlisted_tools.length) * factor;
      }
      const a = stringFromUnknownValue(left[sort.key as keyof McpConnectorRecord]);
      const b = stringFromUnknownValue(right[sort.key as keyof McpConnectorRecord]);
      return a.localeCompare(b) * factor;
    });
  }, [connectors, sort]);

  const total = sortedConnectors.length;
  const pagedConnectors = useMemo(
    () => sortedConnectors.slice((page - 1) * perPage, page * perPage),
    [page, perPage, sortedConnectors],
  );

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ["registry", "mcp"] });
  };

  const createMutation = useMutation({
    mutationFn: (request: { payload: McpConnectorPayload; projectId: string }) =>
      createMcpConnector(request.payload, request.projectId),
    onSuccess: async () => {
      setDraft(EMPTY_DRAFT);
      setCreateOpen(false);
      setModalError("");
      report(t("react.registry.mcp_final_connector_created", "MCP connector created."), "success");
      await refresh();
    },
    onError: (error) => {
      const field = mcpFormErrorField(error);
      setModalErrorField(field === "endpoint" || field === "headers" || field === "requested_scopes" || field === "authorization_parameters" || field === "refresh_token_required" || field === "name" ? field : "");
      setModalError(
        error instanceof Error
          ? error.message
          : t("react.registry.failed_to_create_mcp_connector", "Failed to create MCP connector."),
      );
      queueMicrotask(() => {
        if (field === "endpoint") createEndpointRef.current?.focus();
        if (field === "headers") createHeadersRef.current?.querySelector("input")?.focus();
        if (field === "requested_scopes") (createRequestedScopesRef.current?.querySelector<HTMLInputElement>("input") || createRequestedScopesRef.current)?.focus();
        if (field === "authorization_parameters") createAuthorizationParametersRef.current?.querySelector("input")?.focus();
        if (field === "refresh_token_required") createRefreshTokenRequiredRef.current?.focus();
        if (field === "name") createNameRef.current?.focus();
      });
    },
  });

  const syncMutation = useMutation({
    mutationFn: async (connectorId: string) => {
      setBusyConnectorId(connectorId);
      return syncMcpConnector(connectorId, connectorProject(connectorId));
    },
    onSuccess: async () => {
      report(t("react.registry.mcp_final_sync_completed", "MCP sync completed."), "success");
      await refresh();
    },
    onError: async (error) => {
      report(error instanceof Error ? error.message : t("react.registry.mcp_final_sync_failed", "MCP sync failed."), "danger");
      await refresh();
    },
    onSettled: () => {
      setBusyConnectorId("");
    },
  });

  const healthMutation = useMutation({
    mutationFn: async (connectorId: string) => {
      setBusyConnectorId(connectorId);
      return checkMcpConnectorHealth(connectorId, connectorProject(connectorId));
    },
    onSuccess: async (result) => {
      const latency = result.latency_ms ? ` (${result.latency_ms}ms)` : "";
      const errorDetail = result.error ? ` — ${result.error}` : "";
      report(
        t("react.registry.mcp_final_health_result", "Health: {status}{latency}{errorDetail}", { status: mcpStatusLabel(result.status, t), latency, errorDetail }),
        "neutral",
      );
      await refresh();
    },
    onError: (error) => {
      report(error instanceof Error ? error.message : t("react.registry.mcp_final_health_check_failed", "Health check failed."), "danger");
    },
    onSettled: () => {
      setBusyConnectorId("");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (request: { connectorId: string; revision: string }) => {
      setBusyConnectorId(request.connectorId);
      return deleteMcpConnector(request.connectorId, request.revision, connectorProject(request.connectorId));
    },
    onSuccess: async () => {
      setDeletePlan(null);
      setDeleteError("");
      report(t("react.registry.mcp_final_connector_deleted", "MCP connector deleted."), "success");
      await refresh();
    },
    onError: (error) => {
      setDeleteError(error instanceof Error ? error.message : t("react.registry.failed_to_delete_mcp_connector", "Failed to delete MCP connector."));
    },
    onSettled: () => {
      setBusyConnectorId("");
    },
  });

  function openCreate() {
    // The filtered project, or the only one, is the likely place for it.
    const defaultProject = projectId || (projects.length === 1 ? projects[0].id : "");
    setDraft({ ...EMPTY_DRAFT, project_id: defaultProject });
    setModalError("");
    setModalErrorField("");
    setCreateOpen(true);
  }

  function setCreateValidationError(
    field: "project" | "name" | "endpoint" | "headers" | "requested_scopes" | "authorization_parameters" | "refresh_token_required",
    error: string,
  ) {
    setModalError(error);
    setModalErrorField(field);
    queueMicrotask(() => {
      const target = {
        project: createProjectRef.current,
        name: createNameRef.current,
        endpoint: createEndpointRef.current,
        headers: createHeadersRef.current?.querySelector<HTMLInputElement>("[aria-invalid='true']") || null,
        requested_scopes: createRequestedScopesRef.current?.querySelector<HTMLInputElement>("[aria-invalid='true']") || createRequestedScopesRef.current,
        authorization_parameters: createAuthorizationParametersRef.current?.querySelector<HTMLInputElement>("[aria-invalid='true']") || null,
        refresh_token_required: createRefreshTokenRequiredRef.current,
      }[field];
      target?.focus();
    });
  }

  function submit(event: FormSubmitEvent) {
    event.preventDefault();
    const displayName = draft.display_name.trim();
    const baseUrl = draft.base_url.trim();
    if (!draft.project_id) {
      setCreateValidationError("project", t("react.registry.choose_a_project", "Choose a project."));
      return;
    }
    if (!displayName) {
      setCreateValidationError("name", t("react.registry.name_is_required", "Name is required."));
      return;
    }
    if (!baseUrl) {
      setCreateValidationError("endpoint", t("react.registry.endpoint_is_required", "Endpoint is required."));
      return;
    }
    if (!isValidHttpEndpoint(baseUrl)) {
      setCreateValidationError(
        "endpoint",
        t("react.registry.enter_a_valid_http_s_endpoint_url", "Enter a valid HTTP(S) endpoint URL."),
      );
      return;
    }
    let headers: Record<string, string> = {};
    const oauthConfig: Record<string, unknown> = {};
    try {
      headers = keyValueObject(draft.headers, t("react.registry.mcp_final_header", "Header"), undefined, t);
    } catch (error) {
      setCreateValidationError(
        "headers",
        error instanceof Error ? error.message : t("react.registry.invalid_header_row", "Invalid header row."),
      );
      return;
    }
    let authorizationParameters: Record<string, string> = {};
    try {
      authorizationParameters = keyValueObject(
        draft.authorization_parameters,
        t("react.registry.mcp_final_authorization_parameter", "Authorization parameter"),
        RESERVED_OAUTH_PARAMETER_IDENTITIES,
        t,
      );
      const requestedScopes = draft.requested_scopes
        .map((row) => row.value.trim())
        .filter(Boolean);
      if (requestedScopes.length) oauthConfig.requested_scopes = requestedScopes;
      if (Object.keys(authorizationParameters).length) {
        oauthConfig.authorization_parameters = authorizationParameters;
      }
      if (draft.refresh_token_required) oauthConfig.refresh_token_required = true;
    } catch (error) {
      setCreateValidationError(
        "authorization_parameters",
        error instanceof Error ? error.message : t("react.registry.invalid_authorization_parameter_row", "Invalid authorization parameter row."),
      );
      return;
    }
    setModalError("");
    setModalErrorField("");
    const payload: McpConnectorPayload = {
      display_name: displayName,
      base_url: baseUrl,
      transport: draft.transport,
      auth_mode: draft.auth_mode,
      headers,
      namespace_prefix: draft.namespace_prefix.trim(),
      description: draft.description.trim(),
      enabled: draft.enabled,
    };
    if (Object.keys(oauthConfig).length > 0) payload.oauth_config = oauthConfig;
    createMutation.mutate({ payload, projectId: draft.project_id });
  }

  let loadError = "";
  if (connectorsQuery.isError) {
    loadError = connectorsQuery.error instanceof Error
      ? connectorsQuery.error.message
      : t("react.registry.mcp_final_connectors_load_failed", "Failed to load MCP connectors.");
  }

  const openDeletePlan = useCallback(async (connectorId: string, rowLabel: string) => {
    deleteTriggerLabelRef.current = t("react.registry.actions_for_value0", "Actions for {value0}", { value0: rowLabel });
    setBusyConnectorId(connectorId);
    setDeleteError("");
    try {
      setDeletePlan(await fetchMcpConnectorDeletePlan(connectorId, connectorProject(connectorId)));
    } catch (error) {
      report(
        error instanceof Error ? error.message : t("react.registry.mcp_final_delete_impact_load_failed", "Failed to load MCP connector delete impact."),
        "danger",
      );
    } finally {
      setBusyConnectorId("");
    }
  }, [connectorProject, report, t]);

  const connectorColumns = buildConnectorColumns({
    busyConnectorId,
    canManageProject,
    projectName,
    onDelete: (connectorId, displayName) => {
      ignoreAsyncResult(openDeletePlan(connectorId, displayName));
    },
    onHealth: (connectorId) => healthMutation.mutate(connectorId),
    onRowMenuChange: setRowMenu,
    onSync: (connectorId) => syncMutation.mutate(connectorId),
    onView: (connectorId) => {
      ignoreAsyncResult(navigate(
        `/registry/mcp/${encodeURIComponent(connectorId)}?project_id=${encodeURIComponent(connectorProject(connectorId))}`,
      ));
    },
    rowMenu,
  }, t);

  const tableState = mcpTableState({
    loading: connectorsQuery.isLoading,
    loadError,
    total,
    filtered: Boolean(search.trim() || projectId || healthFilter || credentialsFilter),
    onRetry: () => ignoreAsyncResult(refresh()),
  }, t);
  return {
    refresh,
    canManageProject,
    projectId,
    healthFilter,
    credentialsFilter,
    filterFields,
    setFilter,
    clearFilters,
    connectorProject,
    openCreate,
    message,
    messageTone,
    searchParams,
    setSearchParams,
    projects,
    projectsQuery,
    setSearch,
    setPage,
    search,
    total,
    connectors,
    connectorsQuery,
    connectorColumns,
    navigate,
    setSort,
    pagedConnectors,
    sort,
    tableState,
    setPerPage,
    page,
    perPage,
    createMutation,
    setCreateOpen,
    createOpen,
    createNameRef,
    createProjectRef,
    submit,
    setDraft,
    draft,
    modalErrorField,
    createEndpointRef,
    createHeadersRef,
    createRequestedScopesRef,
    createAuthorizationParametersRef,
    createRefreshTokenRequiredRef,
    modalError,
    deletePlan,
    deleteMutation,
    deleteError,
    setDeletePlan,
    setDeleteError,
    deleteTriggerLabelRef,
  };
}
