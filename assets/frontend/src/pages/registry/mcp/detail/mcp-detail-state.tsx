import { useI18n } from "@/lib/i18n/useI18n";
import { mcpStatusLabel } from "../mcp-status-label";
import { type SubmitEvent, useEffect, useMemo, useRef, useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useParams, useSearchParams } from "react-router";

import { type AssignmentSelectorRow } from "@/components/shared/assignment-selector";

import { DsAlert } from "@/components/shared/design-system";

import {
  checkMcpConnectorHealth,
  disconnectMcpConnectorOAuth,
  fetchMcpConnector,
  fetchMcpConnectorOAuthStatus,
  startMcpConnectorOAuth,
  saveMcpConnectorOAuthCredentials,
  syncMcpConnector,
  type McpConnectorPayload,
  updateMcpConnector,
} from "@/pages/registry/mcp/api";

import { useHasCapability } from "@/lib/auth/capabilities";

import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

import { mcpFormErrorField } from "@/pages/registry/mcp/form-errors";

import { useStableRowKeys } from "@/lib/utils/stable-row-keys";
import {
  type Outcome,
  choose,
  renderWhen,
  anyTrue,
  allTrue,
  errorMessage,
  isNotFoundHttpError,
  projectedToolLabel,
  projectedToolTone,
  formatDateTime,
  apiErrorCode,
  propertyNamesFromSchema,
  type McpSettingsDraft,
  RESERVED_OAUTH_PARAMETER_IDENTITIES,
  keyValueObject,
  settingsDraftFromConnector,
  isValidHttpEndpoint,
} from "./mcp-detail-page-helpers";

export function useMcpDetailState() {
  const { t } = useI18n();
  const { connectorId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const projectId = searchParams.get("project_id")?.trim() ?? "";
  const queryClient = useQueryClient();
  const canManageProject = useHasCapability("mutate_projects");
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<Outcome>("neutral");
  const [searchValue, setSearchValue] = useState("");
  const [pendingSelectedToolIds, setPendingSelectedToolIds] = useState<string[] | null>(null);
  const [manualClientId, setManualClientId] = useState("");
  const [manualClientSecret, setManualClientSecret] = useState("");
  const [credentialsOpen, setCredentialsOpen] = useState(false);
  const [resumeAuthorizationAfterCredentials, setResumeAuthorizationAfterCredentials] = useState(false);
  const [credentialsError, setCredentialsError] = useState("");
  const [credentialsErrorField, setCredentialsErrorField] = useState<"client_id" | "client_secret" | "">("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [settingsErrorField, setSettingsErrorField] = useState<
    "name" | "endpoint" | "headers" | "requested_scopes" | "authorization_parameters" | "refresh_token_required" | ""
  >("");
  const [settingsDraft, setSettingsDraft] = useState<McpSettingsDraft | null>(null);
  const requestedScopeKeyState = useStableRowKeys(
    settingsDraft?.requested_scopes.length ?? 0,
    "mcp-requested-scope",
  );
  const requestedScopeRows = (settingsDraft?.requested_scopes ?? []).map(
    (scope, index) => ({ index, key: requestedScopeKeyState.keys[index], scope }),
  );
  const settingsNameRef = useRef<HTMLInputElement>(null);
  const settingsEndpointRef = useRef<HTMLInputElement>(null);
  const settingsHeadersRef = useRef<HTMLFieldSetElement>(null);
  const settingsRequestedScopesRef = useRef<HTMLFieldSetElement>(null);
  const settingsAuthorizationParametersRef = useRef<HTMLFieldSetElement>(null);
  const settingsRefreshTokenRequiredRef = useRef<HTMLFieldSetElement>(null);
  const credentialsClientIdRef = useRef<HTMLInputElement>(null);
  const credentialsClientSecretRef = useRef<HTMLDivElement>(null);
  const handledOAuthCallbackRef = useRef("");

  const connectorQuery = useQuery({
    queryKey: ["registry", "mcp", connectorId, projectId],
    queryFn: () => fetchMcpConnector(connectorId, projectId),
    enabled: allTrue([Boolean(connectorId), Boolean(projectId)]),
  });

  function report(text: string, tone: Outcome) {
    setMessage(text);
    setMessageTone(tone);
  }

  const oauthCallbackOutcome = searchParams.get("oauth")?.trim().toLowerCase() || "";
  const oauthCallbackReason = searchParams.get("reason")?.trim().toLowerCase() || "";
  const oauthCallbackSyncOutcome = searchParams.get("sync")?.trim().toLowerCase() || "";

  const connector = connectorQuery.data;
  const authModeIsOauth = connector?.auth_mode === "oauth2";
  const persistedSelectedToolIds = useMemo(
    () => connector?.allowlisted_tools ?? [],
    [connector?.allowlisted_tools],
  );
  const selectedToolIds = pendingSelectedToolIds ?? persistedSelectedToolIds;
  const publishableSelectedToolIds = useMemo(() => {
    const blocked = new Set(
      (connector?.discovered_tools_jsonb ?? [])
        .filter((tool) => tool.can_publish === false)
        .map((tool) => tool.projected_tool_id),
    );
    return selectedToolIds.filter((toolId) => !blocked.has(toolId));
  }, [connector?.discovered_tools_jsonb, selectedToolIds]);

  const oauthStatusQuery = useQuery({
    queryKey: ["registry", "mcp", connectorId, projectId, "oauth-status"],
    queryFn: () => fetchMcpConnectorOAuthStatus(connectorId, projectId),
    enabled: allTrue([Boolean(connectorId), Boolean(projectId), authModeIsOauth]),
    refetchInterval: choose<number | false>(authModeIsOauth, 20_000, false),
  });

  const oauthStatus = oauthStatusQuery.data ?? connector?.oauth_status ?? {
    status: connector?.auth_status || "not_configured",
    redirect_uri: "",
    issuer: "",
    registration_mode: "",
    resource: "",
    expires_at: "",
    granted_scopes: [],
    connected_identity: "",
    has_access_token: false,
    has_refresh_token: false,
    last_error: "",
  };
  const requestedScopes = connector?.oauth_requested_scopes ?? [];
  const hasStoredClientCredentials = allTrue([
    Boolean(connector?.has_client_id),
    Boolean(connector?.has_client_secret),
  ]);
  const hasAuthorizedAccount = anyTrue([
    Boolean(oauthStatus.has_access_token),
    Boolean(oauthStatus.has_refresh_token),
    Boolean(oauthStatus.connected_identity),
  ]);
  const oauthStatusCode = String(oauthStatus.status || "").trim().toLowerCase();
  const oauthErrorCode = String(oauthStatus.last_error || "").trim().toLowerCase();
  const hasInsufficientScope = allTrue([
    oauthStatusCode === "auth_required",
    oauthErrorCode === "insufficient_scope",
  ]);
  const hasUnrefreshableAuthorization = allTrue([
    oauthStatusCode === "auth_required",
    Boolean(oauthStatus.has_access_token),
    !oauthStatus.has_refresh_token,
  ]);
  const authorizationMustRestart = anyTrue([
    ["refresh_failed", "expired"].includes(oauthStatusCode),
    hasInsufficientScope,
    hasUnrefreshableAuthorization,
  ]);

  async function copyOAuthRedirectUri() {
    try {
      if (!oauthStatus.redirect_uri || !navigator.clipboard?.writeText) {
        throw new Error(t("react.registry.clipboard_api_unavailable", "Clipboard API unavailable"));
      }
      await navigator.clipboard.writeText(oauthStatus.redirect_uri);
      report(t("react.registry.mcp_final_redirect_copied", "OAuth redirect URI copied."), "success");
    } catch {
      report(t("react.registry.mcp_final_redirect_copy_failed", "OAuth redirect URI could not be copied. Select the value and copy it manually."), "danger");
    }
  }

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["registry", "mcp"] }),
      queryClient.invalidateQueries({ queryKey: ["registry", "mcp", connectorId, projectId] }),
      queryClient.invalidateQueries({ queryKey: ["registry", "mcp", connectorId, projectId, "oauth-status"] }),
    ]);
  };

  const syncMutation = useMutation({
    mutationFn: () => syncMcpConnector(connectorId, projectId),
    onSuccess: async (syncedConnector) => {
      queryClient.setQueryData(
        ["registry", "mcp", connectorId, projectId],
        syncedConnector,
      );
      report(t("react.registry.mcp_final_sync_completed", "MCP sync completed."), "success");
      await Promise.all([
        queryClient.invalidateQueries({
          exact: true,
          queryKey: ["registry", "mcp", projectId],
        }),
        queryClient.invalidateQueries({
          exact: true,
          queryKey: ["registry", "mcp", connectorId, projectId, "oauth-status"],
        }),
      ]);
    },
    onError: async (error) => {
      report(errorMessage(error, t("react.registry.mcp_sync_failed", "MCP sync failed.")), "danger");
      await refresh();
    },
  });

  useEffect(() => {
    if (!oauthCallbackOutcome) return;

    const callbackKey = [
      connectorId,
      projectId,
      oauthCallbackOutcome,
      oauthCallbackReason,
      oauthCallbackSyncOutcome,
    ].join(":");
    const cleaned = new URLSearchParams(searchParams);
    cleaned.delete("oauth");
    cleaned.delete("reason");
    cleaned.delete("sync");
    setSearchParams(cleaned, { replace: true });

    if (handledOAuthCallbackRef.current === callbackKey) return;
    handledOAuthCallbackRef.current = callbackKey;

    if (oauthCallbackOutcome === "success") {
      if (oauthCallbackSyncOutcome === "success") {
        report(t("react.registry.mcp_final_oauth_sync_completed", "OAuth authorization completed and remote tools synchronized."), "success");
      } else if (oauthCallbackSyncOutcome === "failed") {
        report(t("react.registry.mcp_final_oauth_sync_failed", "OAuth connected, but tools could not be synced. Use Sync to retry."), "danger");
      } else {
        report(t("react.registry.mcp_final_oauth_completed", "OAuth authorization completed successfully."), "success");
      }
    } else if (oauthCallbackOutcome === "error") {
      const safeReason = oauthCallbackReason === "oauth_failed"
        ? t("react.registry.oauth_authorization_failed_review_the_connector_status_and_try_again", "OAuth authorization failed. Review the connector status and try again.")
        : t("react.registry.oauth_authorization_could_not_be_completed_review_the_connector_status_and_try_again", "OAuth authorization could not be completed. Review the connector status and try again.");
      report(safeReason, "danger");
    }
  }, [
    connectorId,
    oauthCallbackOutcome,
    oauthCallbackReason,
    oauthCallbackSyncOutcome,
    projectId,
    searchParams,
    setSearchParams,
    t,
  ]);

  const healthMutation = useMutation({
    mutationFn: () => checkMcpConnectorHealth(connectorId, projectId),
    onSuccess: async (result) => {
      const latency = choose(Boolean(result.latency_ms), ` (${result.latency_ms}ms)`, "");
      const errorDetail = choose(Boolean(result.error), ` — ${result.error}`, "");
      report(
        t("react.registry.mcp_final_health_result", "Health: {status}{latency}{errorDetail}", { status: mcpStatusLabel(result.status, t), latency, errorDetail }),
        "neutral",
      );
      await refresh();
    },
    onError: (error) => {
      report(errorMessage(error, t("react.registry.health_check_failed", "Health check failed.")), "danger");
    },
  });

  const saveAllowlistMutation = useMutation({
    mutationFn: () =>
      updateMcpConnector(connectorId, {
        allowlisted_tools: [...publishableSelectedToolIds].sort((left, right) => left.localeCompare(right)),
      }, projectId),
    onSuccess: async () => {
      setPendingSelectedToolIds(null);
      report(t("react.registry.mcp_final_allowlist_saved", "Allowlist saved."), "success");
      await refresh();
    },
    onError: (error) => {
      report(errorMessage(error, t("react.registry.failed_to_save_the_allowlist", "Failed to save the allowlist.")), "danger");
    },
  });

  const saveSettingsMutation = useMutation({
    mutationFn: (payload: McpConnectorPayload) => updateMcpConnector(connectorId, payload, projectId),
    onSuccess: async () => {
      setSettingsOpen(false);
      setSettingsDraft(null);
      setSettingsError("");
      setMessage(t("react.registry.connector_settings_saved", "Connector settings saved."));
      await refresh();
    },
    onError: (error) => {
      setSettingsError(errorMessage(error, t("react.registry.failed_to_save_connector_settings", "Failed to save connector settings.")));
      const field = mcpFormErrorField(error);
      setSettingsErrorField(field === "endpoint" || field === "headers" || field === "requested_scopes" || field === "authorization_parameters" || field === "refresh_token_required" || field === "name" ? field : "");
      queueMicrotask(() => {
        if (field === "endpoint") settingsEndpointRef.current?.focus();
        if (field === "headers") settingsHeadersRef.current?.querySelector("input")?.focus();
        if (field === "requested_scopes") (settingsRequestedScopesRef.current?.querySelector<HTMLInputElement>("input") || settingsRequestedScopesRef.current)?.focus();
        if (field === "authorization_parameters") settingsAuthorizationParametersRef.current?.querySelector("input")?.focus();
        if (field === "refresh_token_required") settingsRefreshTokenRequiredRef.current?.focus();
        if (field === "name") settingsNameRef.current?.focus();
      });
    },
  });

  const startOAuthMutation = useMutation({
    mutationFn: () => {
      const projectQuery = choose(
        Boolean(projectId),
        `?project_id=${encodeURIComponent(projectId)}`,
        "",
      );
      return startMcpConnectorOAuth(connectorId, projectId, {
        return_path: `/registry/mcp/${encodeURIComponent(connectorId)}${projectQuery}`,
      });
    },
    onSuccess: (result) => {
      setResumeAuthorizationAfterCredentials(false);
      setManualClientSecret("");
      const authorizationUrl = firstStringFromUnknownValue([result.authorization_url, ""]);
      if (!authorizationUrl) {
        report(t("react.registry.mcp_final_oauth_url_missing", "OAuth authorization URL was not returned."), "danger");
        return;
      }
      report(t("react.registry.mcp_final_oauth_opening", "Opening OAuth authorization."), "neutral");
      globalThis.open(authorizationUrl, "_self", "noopener,noreferrer");
    },
    onError: (error) => {
      if (apiErrorCode(error) === "manual_credentials_required") {
        setResumeAuthorizationAfterCredentials(true);
        setCredentialsOpen(true);
        setCredentialsError("");
        setCredentialsErrorField("");
        queueMicrotask(() => credentialsClientIdRef.current?.focus());
        return;
      }
      setResumeAuthorizationAfterCredentials(false);
      report(errorMessage(error, t("react.registry.failed_to_start_oauth_authorization", "Failed to start OAuth authorization.")), "danger");
    },
  });

  const saveCredentialsMutation = useMutation({
    mutationFn: () => saveMcpConnectorOAuthCredentials(
      connectorId,
      { client_id: manualClientId.trim(), client_secret: manualClientSecret },
      projectId,
    ),
    onSuccess: async () => {
      const shouldResumeAuthorization = resumeAuthorizationAfterCredentials;
      setResumeAuthorizationAfterCredentials(false);
      setManualClientId("");
      setManualClientSecret("");
      setCredentialsOpen(false);
      setCredentialsError("");
      setCredentialsErrorField("");
      report(t("react.registry.mcp_final_oauth_credentials_saved", "OAuth credentials stored."), "success");
      await refresh();
      if (shouldResumeAuthorization) {
        startOAuthMutation.mutate();
      }
    },
    onError: (error) => {
      setCredentialsError(errorMessage(error, t("react.registry.failed_to_store_oauth_credentials", "Failed to store OAuth credentials.")));
      const field = mcpFormErrorField(error);
      setCredentialsErrorField(field === "client_id" || field === "client_secret" ? field : "");
      queueMicrotask(() => {
        if (field === "client_id") credentialsClientIdRef.current?.focus();
        if (field === "client_secret") credentialsClientSecretRef.current?.querySelector("input")?.focus();
      });
    },
  });

  function submitCredentials() {
    if (!manualClientId.trim()) {
      setCredentialsError(t("react.registry.client_id_is_required", "Client ID is required."));
      setCredentialsErrorField("client_id");
      queueMicrotask(() => credentialsClientIdRef.current?.focus());
      return;
    }
    if (manualClientSecret.length === 0) {
      setCredentialsError(t("react.registry.client_secret_is_required", "Client Secret is required."));
      setCredentialsErrorField("client_secret");
      queueMicrotask(() => credentialsClientSecretRef.current?.querySelector("input")?.focus());
      return;
    }
    setCredentialsError("");
    setCredentialsErrorField("");
    saveCredentialsMutation.mutate();
  }

  const disconnectOAuthMutation = useMutation({
    mutationFn: () => disconnectMcpConnectorOAuth(connectorId, projectId),
    onSuccess: async () => {
      setManualClientSecret("");
      report(t("react.registry.mcp_final_oauth_disconnected", "OAuth disconnected."), "success");
      await refresh();
    },
    onError: (error) => {
      report(errorMessage(error, t("react.registry.failed_to_disconnect_oauth", "Failed to disconnect OAuth.")), "danger");
    },
  });

  const projectedToolsById = useMemo(() => {
    return new Map((connector?.projected_tools ?? []).map((tool) => [tool.tool_id, tool]));
  }, [connector?.projected_tools]);

  const selectorRows = useMemo<AssignmentSelectorRow[]>(() => {
    return (connector?.discovered_tools_jsonb ?? []).map((tool) => {
      const projectedTool = projectedToolsById.get(tool.projected_tool_id);
      const isInTrash = tool.lifecycle_state === "in_trash";
      const inputNames = propertyNamesFromSchema(tool.input_schema);
      const outputNames = propertyNamesFromSchema(tool.output_schema);
      return {
        id: tool.projected_tool_id,
        title: tool.display_name || tool.remote_tool_name,
        subtitle: tool.remote_tool_name,
        selected: !isInTrash && selectedToolIds.includes(tool.projected_tool_id),
        disabled: tool.can_publish === false,
        actionHref: isInTrash
          ? `/trash?entity_type=registry_tool&entity_id=${encodeURIComponent(tool.projected_tool_id)}`
          : undefined,
        actionLabel: isInTrash ? t("react.registry.mcp_final_view_in_trash", "View in Trash") : undefined,
        searchText: [
          tool.display_name,
          tool.remote_tool_name,
          tool.projected_tool_id,
          tool.description,
          ...inputNames,
          ...outputNames,
        ].map((value) => stringFromUnknownValue(value)).join(" ")
          .toLowerCase(),
        // The description is prose and goes to its own clamped line; `details`
        // is for short facts, which is why it wraps them inline.
        description: stringFromUnknownValue(tool.description),
        details: [
          t("react.registry.mcp_final_projected_id", "Projected ID: {id}", { id: tool.projected_tool_id }),
          inputNames.length ? t("react.registry.mcp_final_inputs", "Inputs: {names}", { names: inputNames.join(", ") }) : t("react.registry.mcp_final_no_inputs", "Inputs: none"),
          outputNames.length ? t("react.registry.mcp_final_outputs", "Outputs: {names}", { names: outputNames.join(", ") }) : t("react.registry.mcp_final_no_outputs", "Outputs: none"),
          ...(tool.availability_reason_text ? [tool.availability_reason_text] : []),
          ...(isInTrash && tool.deleted_at
            ? [tool.deleted_by
              ? t("react.registry.mcp_final_trashed_by", "Moved to Trash on {date} by {actor}", { date: tool.deleted_at, actor: tool.deleted_by })
              : t("react.registry.mcp_final_trashed", "Moved to Trash on {date}", { date: tool.deleted_at })]
            : []),
        ],
        badges: [
          {
            label: projectedToolLabel(isInTrash, projectedTool, t),
            tone: projectedToolTone(isInTrash, projectedTool),
          },
          {
            label: t("react.registry.value0_inputs", "{value0} inputs", { value0: inputNames.length }),
            tone: "info",
          },
        ],
        rightMeta: connector?.allowlisted_tools.includes(tool.projected_tool_id) ? t("react.registry.mcp_final_allowlisted", "allowlisted") : "",
      };
    });
  }, [
    connector?.allowlisted_tools,
    connector?.discovered_tools_jsonb,
    projectedToolsById,
    selectedToolIds, t
  ]);

  function toggleSelectedTool(toolId: string) {
    setPendingSelectedToolIds((current) => {
      const base = current ?? persistedSelectedToolIds;
      return base.includes(toolId)
        ? base.filter((candidate) => candidate !== toolId)
        : [...base, toolId];
    });
  }

  function openSettings() {
    if (!connector) {
      return;
    }
    setSettingsDraft(settingsDraftFromConnector(connector));
    setSettingsError("");
    setSettingsErrorField("");
    setSettingsOpen(true);
  }

  function setSettingsValidationError(
    field: "name" | "endpoint" | "headers" | "requested_scopes" | "authorization_parameters" | "refresh_token_required",
    error: string,
  ) {
    setSettingsError(error);
    setSettingsErrorField(field);
    queueMicrotask(() => {
      const target = {
        name: settingsNameRef.current,
        endpoint: settingsEndpointRef.current,
        headers: settingsHeadersRef.current?.querySelector<HTMLInputElement>("[aria-invalid='true']") || settingsHeadersRef.current,
        requested_scopes: settingsRequestedScopesRef.current?.querySelector<HTMLInputElement>("[aria-invalid='true']") || settingsRequestedScopesRef.current,
        authorization_parameters: settingsAuthorizationParametersRef.current?.querySelector<HTMLInputElement>("[aria-invalid='true']") || settingsAuthorizationParametersRef.current,
        refresh_token_required: settingsRefreshTokenRequiredRef.current,
      }[field];
      target?.focus();
    });
  }

  function submitSettings(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settingsDraft) {
      return;
    }
    const displayName = settingsDraft.display_name.trim();
    const baseUrl = settingsDraft.base_url.trim();
    if (!displayName) {
      setSettingsValidationError("name", t("react.registry.name_is_required", "Name is required."));
      return;
    }
    if (!baseUrl) {
      setSettingsValidationError("endpoint", t("react.registry.endpoint_is_required", "Endpoint is required."));
      return;
    }
    if (!isValidHttpEndpoint(baseUrl)) {
      setSettingsValidationError(
        "endpoint",
        t("react.registry.enter_a_valid_http_s_endpoint_url", "Enter a valid HTTP(S) endpoint URL."),
      );
      return;
    }
    let replacementHeaders: Record<string, string> | undefined;
    try {
      replacementHeaders = keyValueObject(settingsDraft.replacement_headers, undefined, undefined, t);
    } catch (error) {
      setSettingsValidationError(
        "headers",
        error instanceof Error ? error.message : t("react.registry.invalid_replacement_header_row", "Invalid replacement header row."),
      );
      return;
    }
    if (settingsDraft.headers_mode === "replace" && !replacementHeaders) {
      setSettingsValidationError(
        "headers",
        t("react.registry.add_at_least_one_replacement_header_or_choose_keep_existing_or_clear", "Add at least one replacement header, or choose Keep existing or Clear."),
      );
      return;
    }
    let authorizationParameters: Record<string, string> | undefined;
    try {
      authorizationParameters = keyValueObject(
        settingsDraft.authorization_parameters,
        t("react.registry.mcp_final_authorization_parameter", "Authorization parameter"),
        RESERVED_OAUTH_PARAMETER_IDENTITIES,
        t,
      );
    } catch (error) {
      setSettingsValidationError(
        "authorization_parameters",
        error instanceof Error ? error.message : t("react.registry.invalid_authorization_parameter_row", "Invalid authorization parameter row."),
      );
      return;
    }
    setSettingsError("");
    setSettingsErrorField("");
    const payload: McpConnectorPayload = {
      display_name: displayName,
      base_url: baseUrl,
      transport: settingsDraft.transport,
      auth_mode: settingsDraft.auth_mode,
      namespace_prefix: settingsDraft.namespace_prefix.trim(),
      description: settingsDraft.description.trim(),
      enabled: settingsDraft.enabled,
      oauth_config: {
        requested_scopes: settingsDraft.requested_scopes.map((scope) => scope.trim()).filter(Boolean),
        authorization_parameters: authorizationParameters || {},
        refresh_token_required: settingsDraft.refresh_token_required,
      },
      ...(settingsDraft.headers_mode === "replace" && replacementHeaders
        ? { headers_mode: "replace" as const, headers: replacementHeaders }
        : {}),
      ...(settingsDraft.headers_mode === "clear"
        ? { headers_mode: "clear" as const }
        : {}),
    };
    saveSettingsMutation.mutate(payload);
  }

  // Rebuilt in scope before it reaches AssignmentSelector: the status reads as
  // ordinary copy and the sync failure is an alert, not red text.
  const syncFailed = connector?.last_sync_status?.trim().toLowerCase() === "failed";
  const remoteToolsMessage = (
    <>
      <span className="fs-ink-primary">
        {t("react.registry.sync_status", "Sync status:")}{" "}{mcpStatusLabel(connector?.last_sync_status || "never", t)}
        {choose(Boolean(connector?.last_synced_at), ` · ${formatDateTime(connector?.last_synced_at ?? "", t)}`, "")}
      </span>
      {renderWhen(Boolean(connector?.last_sync_error), (
        <DsAlert
          title={choose(syncFailed, t("react.registry.mcp_final_last_sync_failed", "Last sync failed"), t("react.registry.mcp_final_sync_warnings", "Sync completed with warnings"))}
          tone={choose(syncFailed, "danger" as const, "warning" as const)}
        >
          {connector?.last_sync_error}
        </DsAlert>
      ))}
    </>
  );

  /* A purged connector is not the same as an unreachable API: one is a dead
     link, the other is worth retrying. Both name their region. */
  const connectorMissing = anyTrue([
    isNotFoundHttpError(connectorQuery.error),
    allTrue([!connectorQuery.isLoading, !connectorQuery.isError, !connector]),
  ]);
  return {
    projectId,
    connectorMissing,
    connectorQuery,
    refresh,
    canManageProject,
    syncMutation,
    healthMutation,
    connector,
    openSettings,
    connectorId,
    message,
    messageTone,
    settingsOpen,
    settingsDraft,
    setSettingsOpen,
    saveSettingsMutation,
    settingsNameRef,
    submitSettings,
    settingsErrorField,
    setSettingsDraft,
    settingsEndpointRef,
    settingsRequestedScopesRef,
    requestedScopeRows,
    requestedScopeKeyState,
    settingsAuthorizationParametersRef,
    settingsRefreshTokenRequiredRef,
    settingsHeadersRef,
    settingsError,
    authModeIsOauth,
    oauthStatus,
    copyOAuthRedirectUri,
    requestedScopes,
    hasInsufficientScope,
    credentialsOpen,
    resumeAuthorizationAfterCredentials,
    credentialsErrorField,
    setManualClientId,
    setCredentialsErrorField,
    credentialsClientIdRef,
    manualClientId,
    credentialsClientSecretRef,
    setManualClientSecret,
    manualClientSecret,
    credentialsError,
    saveCredentialsMutation,
    submitCredentials,
    hasAuthorizedAccount,
    authorizationMustRestart,
    startOAuthMutation,
    oauthStatusQuery,
    disconnectOAuthMutation,
    hasStoredClientCredentials,
    setResumeAuthorizationAfterCredentials,
    setCredentialsError,
    setCredentialsOpen,
    remoteToolsMessage,
    saveAllowlistMutation,
    setSearchValue,
    toggleSelectedTool,
    selectorRows,
    searchValue,
  };
}
