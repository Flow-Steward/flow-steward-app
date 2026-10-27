import { effectiveAgentBlockKey } from "./detail-agent-step-fields";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useMemo, useState } from "react";

import { useQuery } from "@tanstack/react-query";
import { useI18n } from "@/lib/i18n/useI18n";
import { distinctLabelOptions, readableMetadata } from "./detail-usage-labels";
import { connectorToolCategory } from "./detail-tool-categories";

import { fetchDatasetQueryTemplates } from "@/pages/registry/datasets/api";
import { fetchRegistryList } from "@/pages/registry/shared/api";
import { fetchWorkflowContract, fetchWorkflowStepAvailableVariables } from "@/pages/workflows/shared/api";
import { fetchWorkflowHook } from "@/pages/workflows/shared/hook-api";
import {
  fetchProjectWorkflowBindings,
  saveProjectWorkflowBindings,
} from "@/pages/projects/workflows/api";
import { createWorkflowVariableSuggestionRenderers } from "@/pages/workflows/detail/detail-variable-suggestions";
import {
  collectOptionsFromUnknown,
  connectorLikeStepKinds,
  contractRows,
  listFromUnknown,
  isSetSourceCopy,
  normalizeAvailableWorkflowVariableSuggestions,
  missingRequiredConnectorInputNames,
  objectOrEmpty,
  saveJson,
  stepOverrideObjectFromDraft,
  stringList,
  textOrEmpty,
  uniqueSelectOptions,
  type SelectOption,
} from "@/pages/workflows/detail/detail-helpers";
import { isTruthy } from "@/lib/utils/unknown-values";
import { UNKNOWN_POSITION, type WorkflowVariablesContext } from "@/pages/workflows/detail/detail-variable-filter";
import { isHttpError } from "@/lib/http/errors";
import {
  extensionConnectionEligibility,
  operationConnectionTypeIds,
} from "@/pages/workflows/detail/detail-extension-connection";

function resolveExtensionInstallAvailability(
  isError: boolean,
  error: unknown,
  installState: unknown,
): string {
  if (isError) {
    return isHttpError(error) && error.kind === "not_found" ? "removed" : "unavailable";
  }
  return textOrEmpty(installState) === "disabled" ? "disabled" : "active";
}

function connectorIsExtension(
  connectorId: string,
  connectorRow: Record<string, unknown>,
  stepConfig: Record<string, unknown>,
): boolean {
  return (
    textOrEmpty(connectorRow.source) === "extension" ||
    connectorId.startsWith("extension.") ||
    Boolean(textOrEmpty(connectorRow.extension_install_id)) ||
    Boolean(textOrEmpty(stepConfig.extension_id))
  );
}

function connectorCatalogOptionLabel(connectorRow: Record<string, unknown>): string {
  return readableMetadata(connectorRow.display_name, [connectorRow.connector_id, connectorRow.extension_id, connectorRow.connector_install_id, connectorRow.extension_install_id]);
}

function extensionInstallIdFor(
  isExtension: boolean,
  connectorRow: Record<string, unknown>,
  stepConfig: Record<string, unknown>,
): string {
  if (!isExtension) return "";
  return textOrEmpty(
    stepConfig.connector_install_id ||
      connectorRow.extension_install_id ||
      connectorRow.connector_install_id ||
      connectorRow.install_id,
  );
}

function connectorOperationIsStale({
  availability,
  installId,
  isExtension,
  isPending,
  isSuccess,
  operationId,
  operationRows,
}: {
  availability: string;
  installId: string;
  isExtension: boolean;
  isPending: boolean;
  isSuccess: boolean;
  operationId: string;
  operationRows: Array<Record<string, unknown>>;
}): boolean {
  if (!isExtension || isPending || !operationId) return false;
  if (availability !== "active") return true;
  if (!isSuccess) return false;
  if (!installId) return true;
  return !operationRows.some(
    (row) =>
      textOrEmpty(row.operation_id || row.id || row.key || row.name) === operationId,
  );
}

function connectorConnectionState({
  availability,
  connectionId,
  connectionRequired,
  connectionRows,
  installId,
  isExtension,
  isPending,
  isSuccess,
  projectId,
}: {
  availability: string;
  connectionId: string;
  connectionRequired: boolean;
  connectionRows: Array<Record<string, unknown>>;
  installId: string;
  isExtension: boolean;
  isPending: boolean;
  isSuccess: boolean;
  projectId: string;
}) {
  const resolvable =
    isExtension &&
    connectionRequired &&
    !isPending &&
    availability === "active" &&
    isSuccess;
  const stale =
    resolvable &&
    Boolean(installId) &&
    Boolean(connectionId) &&
    !connectionRows.some(
      (row) =>
        textOrEmpty(
          row.connection_id || row.connector_install_id || row.extension_install_id,
        ) === connectionId,
    );
  const missing = resolvable && !stale && !connectionId;
  const unavailable = missing && !connectionRows.length;
  return {
    needsProjectLink: unavailable && !textOrEmpty(projectId),
    missing,
    stale,
    unavailable,
  };
}

export function useWorkflowStepOptionsController(ctx: Record<string, any>) {
  const { t } = useI18n();
  const {
    availableProjectsQuery,
    definitionRows,
    drawerMode,
    linkedProjectQuery,
    selectedProjectId,
    selectedStep,
    setSearchParams,
    stepAgentId,
    stepKindDraft,
    stepToolCategoryDraft,
    stepOverrideDraft,
    workflow,
    workflowId,
  } = ctx;
  const variablesContext: WorkflowVariablesContext = ctx.variablesContext || UNKNOWN_POSITION;
  const stepRows = useMemo(
    () => (definitionRows?.stepRows || []) as Array<Record<string, any>>,
    [definitionRows],
  );

  const [projectLinkDraft, setProjectLinkDraft] = useState("");
  const [projectLinkMessage, setProjectLinkMessage] = useState("");
  const [projectLinkPending, setProjectLinkPending] = useState(false);
  const [activeWorkflowSuggestionPanelId, setActiveWorkflowSuggestionPanelId] =
    useState("");
  const [workflowSuggestionHighlight, setWorkflowSuggestionHighlight] = useState(0);
  // Typed in the open field since it opened: only then does its own text filter the list.
  const [workflowSuggestionTyped, setWorkflowSuggestionTyped] = useState(false);
  const [expandedWorkflowSuggestionKeys, setExpandedWorkflowSuggestionKeys] =
    useState<string[]>([
      "group:inputs",
      "group:workflow",
      "group:previous-steps",
    ]);

  useEffect(() => {
    setProjectLinkDraft((current) => current || selectedProjectId || "");
  }, [selectedProjectId]);

  const callableWorkflowsQuery = useQuery({
    queryKey: ["workflows", "meta", "callable-workflows", workflowId],
    queryFn: () =>
      saveJson("/api/workflows/meta/callable-workflows", { method: "GET" }),
    enabled: drawerMode === "step" && stepKindDraft === "invoke_workflow",
  });
  // Variables are what runs before a saved step. A step that exists only in the
  // unapplied change set borrows the place it will run at (see workflowVariablesContext);
  // with no place known yet it still gets every saved output and the global variables.
  const availableWorkflowVariablesQuery = useQuery({
    queryKey: [
      "workflows",
      "meta",
      "available-variables",
      workflowId,
      variablesContext.stepId,
      variablesContext.position,
      variablesContext.forEachId,
    ],
    queryFn: () =>
      fetchWorkflowStepAvailableVariables(workflowId, variablesContext.stepId, {
        position: variablesContext.position === "after" ? "after" : "before",
        forEachId: variablesContext.forEachId,
      }),
    // The group drawer too: a loop source is a path from before the group.
    enabled: (drawerMode === "step" || drawerMode === "group") && Boolean(workflowId),
    retry: false,
  });
  // Step authoring can target a Hook child while the Interface drawer still
  // edits its parent. Reuse the applied contract query for the actual step target.
  const scopedContractQuery = useQuery({
    queryKey: ["workflows", "contract", workflowId],
    queryFn: () => fetchWorkflowContract(workflowId),
    enabled: (drawerMode === "step" || drawerMode === "group") && Boolean(workflowId),
    retry: false,
  });
  // A child step may name the parent Hook only when the saved declaration and
  // this project's current binding still match the selected child frame.
  const hookPresentation = ctx.hookPresentation as {
    parentWorkflowId: string; projectId: string; hookId: string; stepId: string;
    bindingRevision: number; childWorkflowId: string; declaration: Record<string, unknown>;
  } | null;
  const hookQuery = useQuery({
    queryKey: ["workflows", "hook", hookPresentation?.projectId, hookPresentation?.parentWorkflowId, hookPresentation?.hookId],
    queryFn: () => fetchWorkflowHook(hookPresentation!.projectId, hookPresentation!.parentWorkflowId, hookPresentation!.hookId),
    enabled: Boolean(hookPresentation?.projectId && hookPresentation.parentWorkflowId && hookPresentation.hookId),
    retry: false,
  });
  const verifiedHook = useMemo(() => {
    const hook = hookQuery.data;
    const binding = hook?.binding;
    const owner = hookPresentation;
    if (!owner || hookQuery.isFetching || !hookQuery.isSuccess || !hook || !binding ||
      hook.project_id !== owner.projectId || hook.workflow_id !== owner.parentWorkflowId ||
      hook.hook_id !== owner.hookId || hook.step_id !== owner.stepId ||
      hook.binding_revision !== owner.bindingRevision || !binding.revision_id ||
      binding.target_workflow_id !== owner.childWorkflowId || owner.childWorkflowId !== workflowId ||
      hook.contract.hook_id !== owner.hookId || hook.contract_version !== Number(owner.declaration.contract_version) ||
      (binding.hook_contract_version !== undefined && binding.hook_contract_version !== hook.contract_version) ||
      (binding.hook_semantic_digest !== undefined && binding.hook_semantic_digest !== hook.hook_semantic_digest)) return null;
    return hook;
  }, [hookPresentation, hookQuery.data, hookQuery.isFetching, hookQuery.isSuccess, workflowId]);
  const availableWorkflowVariablesPayload = useMemo(() => {
    const payload = objectOrEmpty(availableWorkflowVariablesQuery.data);
    // A container's own source is read before its items exist: no item names there.
    const loop = drawerMode === "group"
      ? []
      : [...listFromUnknown(payload.loop), ...variablesContext.localLoop];
    return availableWorkflowVariablesQuery.data || loop.length ? { ...payload, loop } : undefined;
  }, [availableWorkflowVariablesQuery.data, drawerMode, variablesContext.localLoop]);
  const workflowVariablePositionUnknown =
    drawerMode === "step" && textOrEmpty(objectOrEmpty(availableWorkflowVariablesQuery.data).position) === "unknown";
  // Extension source and operation names come back in the user's language.
  const { locale } = useI18n();
  const connectorCatalogQuery = useQuery({
    queryKey: ["workflows", "meta", "connector-catalog", selectedProjectId, locale],
    queryFn: () => {
      const params = new URLSearchParams({ locale });
      if (selectedProjectId) params.set("project_id", selectedProjectId);
      return saveJson(`/api/workflows/meta/connector-catalog?${params.toString()}`, {
        method: "GET",
      });
    },
    enabled: drawerMode === "step" && connectorLikeStepKinds.has(stepKindDraft),
  });
  // The Agent selector used to be built only from fields already present on the
  // workflow record (participants, agent_catalog, ...). A workflow with no
  // participants yet therefore showed an empty selector even when the registry
  // was full, and saving then failed with "agent step requires agent_id or
  // participant_key" (AUDIT-033). Load the registry catalog too.
  const agentCatalogQuery = useQuery({
    queryKey: ["workflows", "meta", "agent-catalog"],
    queryFn: () => fetchRegistryList("/api/agents", { presentation: "background" }),
    enabled: drawerMode === "step" && stepKindDraft === "agent",
    retry: false,
    refetchInterval: 60_000,
  });
  const datasetQueryTemplatesQuery = useQuery({
    queryKey: [
      "workflows",
      "meta",
      "dataset-query-templates",
      selectedProjectId,
    ],
    queryFn: () =>
      fetchDatasetQueryTemplates(
        selectedProjectId ? { projectId: selectedProjectId } : undefined,
        { presentation: "background" },
      ),
    // Deliberately NOT gated on a selected project. `fetchDatasetQueryTemplates`
    // treats projectId as optional and scopes the result when one is present,
    // and the sibling connector-catalog query above already works this way.
    // Requiring a project meant the editor reported "No saved queries are
    // available" whenever the builder had no project in context — including
    // right after the author had created and successfully previewed one
    // (AUDIT-033).
    enabled: drawerMode === "step" && stepKindDraft === "dataset_query",
    refetchInterval: 60_000,
  });

  const agentBlockCatalogQuery = useQuery({
    queryKey: ["workflows", "meta", "agent-blocks", workflowId],
    queryFn: () => fetchRegistryList("/api/published-blocks?source_kind=agent"),
    enabled: drawerMode === "step" && stepKindDraft === "agent",
  });

  const stepAgentOptions = useMemo(() => {
    const options: SelectOption[] = [];
    const workflowRecord = objectOrEmpty(workflow);
    const agentSources = [
      workflowRecord.agents,
      workflowRecord.all_agents,
      workflowRecord.registry_agents,
      workflowRecord.agent_catalog,
      workflowRecord.participants,
      // The registry catalog, so a workflow with no participants yet still
      // offers every agent that exists.
      agentCatalogQuery.data,
    ];
    agentSources.forEach((source) => {
      options.push(
        ...collectOptionsFromUnknown(
          source,
          ["agent_id", "id", "key", "participant_key", "agent"],
          [
            "display_name",
            "agent_display_name",
            "label",
            "name",
            "agent_id",
            "participant_key",
          ],
        ),
      );
    });
    stepRows.forEach((step) => {
      const value = textOrEmpty(step.agentId);
      if (!value) {
        return;
      }
      options.push({ value, label: textOrEmpty(step.agentLabel) || value });
    });
    if (stepAgentId) {
      options.push({ value: stepAgentId, label: stepAgentId });
    }
    return uniqueSelectOptions(options);
  }, [agentCatalogQuery.data, stepRows, stepAgentId, workflow]);

  const stepSkillOptions = useMemo(() => {
    const options: SelectOption[] = [];
    const workflowRecord = objectOrEmpty(workflow);
    const skillSources = [
      workflowRecord.skills,
      workflowRecord.all_skills,
      workflowRecord.registry_skills,
      workflowRecord.skill_catalog,
      workflowRecord.capabilities,
    ];
    skillSources.forEach((source) => {
      options.push(
        ...collectOptionsFromUnknown(
          source,
          ["skill_id", "id", "key", "skill"],
          ["display_name", "skill_display_name", "label", "name", "skill_id"],
        ),
      );
    });
    stepRows.forEach((step) => {
      const value = textOrEmpty(step.skillId);
      if (!value) {
        return;
      }
      options.push({ value, label: textOrEmpty(step.skillLabel) || value });
    });
    return uniqueSelectOptions(options);
  }, [stepRows, workflow]);

  const stepOverrideObject = useMemo(
    () => stepOverrideObjectFromDraft(stepOverrideDraft, t),
    [stepOverrideDraft, t],
  );
  const stepKindConfig = useMemo(
    () => objectOrEmpty(stepOverrideObject[stepKindDraft]),
    [stepKindDraft, stepOverrideObject],
  );

  const agentBlockOptions = useMemo(() => {
    const options = (agentBlockCatalogQuery.data || []).flatMap((row) => {
      const key = textOrEmpty(row.block_key);
      return key ? [{ value: key, label: textOrEmpty(row.display_name) || key, disabled: row.enabled === false }] : [];
    });
    const key = effectiveAgentBlockKey(stepOverrideObject);
    if (key && !options.some((row) => row.value === key)) options.push({ value: key, label: key, disabled: true });
    return options;
  }, [agentBlockCatalogQuery.data, stepOverrideObject]);

  const connectorRows = useMemo(
    () => listFromUnknown(connectorCatalogQuery.data?.connectors),
    [connectorCatalogQuery.data],
  );
  const selectedRuntimeConnectorId =
    textOrEmpty(stepKindConfig.connector_id ?? objectOrEmpty(selectedStep?.raw).connector_id);
  const selectedConnectorRow = useMemo(
    () => {
      const directMatch = connectorRows.find(
        (row) => textOrEmpty(row.connector_id) === selectedRuntimeConnectorId,
      );
      if (directMatch || !selectedRuntimeConnectorId) return directMatch || {};
      return connectorRows.find((row) =>
        listFromUnknown(row.operations).some(
          (operation) =>
            textOrEmpty(operation.runtime_connector_id) === selectedRuntimeConnectorId,
        ),
      ) || {};
    },
    [connectorRows, selectedRuntimeConnectorId],
  );
  const selectedConnectorId =
    textOrEmpty(selectedConnectorRow.connector_id) || selectedRuntimeConnectorId;
  const stepToolCategory = selectedRuntimeConnectorId
    ? connectorToolCategory(selectedConnectorRow, { ...stepKindConfig, connector_id: selectedRuntimeConnectorId })
    : stepToolCategoryDraft || connectorToolCategory(selectedConnectorRow, stepKindConfig);
  const connectorOptions = useMemo(
    () =>
      distinctLabelOptions(uniqueSelectOptions([
        ...connectorRows.filter((row) => connectorToolCategory(row) === stepToolCategory).map((row) => ({
          value: textOrEmpty(row.connector_id),
          // In the catalog, so available: only a name is missing, and an id is never shown.
          label: connectorCatalogOptionLabel(row) || (connectorToolCategory(row) === "extension_tools"
            ? t("react.workflows.unnamed_extension", "Unnamed extension")
            : t("react.workflows.unnamed_tool", "Unnamed tool")),
        })),
        // Only a saved source the catalog no longer has is unavailable.
        {
          value: selectedConnectorId,
          label: stepToolCategory === "extension_tools"
            ? t("react.workflows.unavailable_extension", "Unavailable extension")
            : t("react.workflows.unavailable_action", "Unavailable action"),
        },
      ])).sort((left, right) => left.label.localeCompare(right.label, locale)),
    [connectorRows, selectedConnectorId, stepToolCategory, locale, t],
  );
  const selectedConnectorOperationId = textOrEmpty(
    stepKindConfig.operation_id ||
      stepKindConfig.function_name ||
      stepKindConfig.action_id,
  );
  const isExtensionConnector = connectorIsExtension(
    selectedRuntimeConnectorId,
    selectedConnectorRow,
    stepKindConfig,
  );
  const selectedExtensionInstallId = extensionInstallIdFor(
    isExtensionConnector,
    selectedConnectorRow,
    stepKindConfig,
  );
  const extensionInstallContractQuery = useQuery({
    queryKey: [
      "workflows",
      "meta",
      "extension-install-contract",
      selectedExtensionInstallId,
      selectedProjectId,
      locale,
    ],
    queryFn: () => {
      const projectQuery = selectedProjectId
        ? `&project_id=${encodeURIComponent(selectedProjectId)}`
        : "";
      return saveJson(
        `/api/workflows/meta/extension-install-contract?install_id=${encodeURIComponent(selectedExtensionInstallId)}${projectQuery}&locale=${encodeURIComponent(locale)}`,
        { method: "GET" },
      );
    },
    enabled:
      drawerMode === "step" &&
      connectorLikeStepKinds.has(stepKindDraft) &&
      Boolean(selectedExtensionInstallId),
    retry: false,
  });
  const extensionInstallAvailability = resolveExtensionInstallAvailability(
    extensionInstallContractQuery.isError,
    extensionInstallContractQuery.error,
    objectOrEmpty(extensionInstallContractQuery.data).install_state,
  );
  const setupExtensionId = textOrEmpty(objectOrEmpty(extensionInstallContractQuery.data).extension_id);
  const connectorConnectionSetupHref = selectedProjectId && setupExtensionId
    && extensionInstallContractQuery.isSuccess && extensionInstallAvailability === "active"
    ? `/extensions/${encodeURIComponent(setupExtensionId)}?project_id=${encodeURIComponent(selectedProjectId)}#installed`
    : "";
  const selectedExtensionReleaseId = textOrEmpty(
    objectOrEmpty(extensionInstallContractQuery.data).release_id,
  );
  const selectedExtensionDescriptorSha256 = textOrEmpty(
    objectOrEmpty(extensionInstallContractQuery.data).descriptor_sha256,
  );
  const preserveSelectedInstallOptions =
    !selectedExtensionInstallId || extensionInstallContractQuery.isPending;
  const connectorOperationRows = useMemo(
    () =>
      selectedExtensionInstallId
        ? listFromUnknown(
            objectOrEmpty(extensionInstallContractQuery.data).operations,
          )
        : listFromUnknown(selectedConnectorRow.operations),
    [
      extensionInstallContractQuery.data,
      selectedConnectorRow.operations,
      selectedExtensionInstallId,
    ],
  );
  const rawConnectorConnectionRows = useMemo(
    () =>
      selectedExtensionInstallId
        ? listFromUnknown(
            objectOrEmpty(extensionInstallContractQuery.data).connections,
          )
        : listFromUnknown(selectedConnectorRow.connections),
    [
      extensionInstallContractQuery.data,
      selectedConnectorRow.connections,
      selectedExtensionInstallId,
    ],
  );
  const selectedConnectorOperation = useMemo(
    () =>
      connectorOperationRows.find(
        (row) =>
          textOrEmpty(row.operation_id || row.id || row.key || row.name) ===
          selectedConnectorOperationId,
      ) || {},
    [connectorOperationRows, selectedConnectorOperationId],
  );
  // Every type the operation declares. Reading only the singular field hid a
  // project's valid SFTP connection from an operation that accepts FTP or SFTP.
  const selectedOperationConnectionTypeIds = operationConnectionTypeIds(
    selectedConnectorOperation,
  );
  const selectedOperationConnectionTypeId = selectedOperationConnectionTypeIds[0] || "";
  // Scopes are strings; a reader that keeps only objects returned none, so a
  // connection missing the action's access looked usable.
  const selectedOperationRequiredScopes = stringList(
    selectedConnectorOperation.required_oauth_scopes,
  );
  const selectedConnectionTypeLabel = textOrEmpty(
    selectedConnectorOperation.connection_label,
  ) || t("react.workflows.project_connection", "project connection");
  const selectedConnectionId = textOrEmpty(
    objectOrEmpty(stepKindConfig.connection_binding).connection_id,
  );
  const connectorConnectionRequired =
    isExtensionConnector &&
    (selectedConnectorOperation.connection_required === true || Boolean(selectedConnectionId));
  const connectorConnectionRows = useMemo(
    () =>
      selectedOperationConnectionTypeIds.length
        ? rawConnectorConnectionRows.filter((row) =>
            selectedOperationConnectionTypeIds.includes(
              textOrEmpty(row.connection_type || row.connection_type_id),
            ),
          )
        : rawConnectorConnectionRows,
    [rawConnectorConnectionRows, selectedOperationConnectionTypeIds],
  );
  const selectedConnectorConnection = useMemo(
    () =>
      connectorConnectionRows.find(
        (row) =>
          textOrEmpty(
            row.connection_id || row.connector_install_id || row.extension_install_id,
          ) === selectedConnectionId,
      ),
    [connectorConnectionRows, selectedConnectionId],
  );
  const selectedConnectionEligibility =
    extensionConnectionEligibility(selectedConnectorConnection, {
      connectionTypeIds: selectedOperationConnectionTypeIds,
      requiredScopes: selectedOperationRequiredScopes,
    }, t);
  const extensionConnectionReadinessBlocked =
    selectedConnectorOperation.connection_required === true
    && Boolean(selectedConnectorConnection)
    && selectedConnectionEligibility.applicable
    && !selectedConnectionEligibility.allowed;
  const extensionConnectionReadinessMessage = extensionConnectionReadinessBlocked
    ? selectedConnectionEligibility.guidance
    : "";
  // "Stale" means an action that was chosen and has since disappeared from the
  // install contract. An empty operation id on a new step means the author has
  // not chosen one yet — a neutral state, not a loss — and reporting it as a
  // vanished saved action sends someone looking for a problem that is not there.
  const staleConnectorOperationId = connectorOperationIsStale({
    availability: extensionInstallAvailability,
    installId: selectedExtensionInstallId,
    isExtension: isExtensionConnector,
    isPending: extensionInstallContractQuery.isPending,
    isSuccess: extensionInstallContractQuery.isSuccess,
    operationId: selectedConnectorOperationId,
    operationRows: connectorOperationRows,
  });
  const connectionState = connectorConnectionState({
    availability: extensionInstallAvailability,
    connectionId: selectedConnectionId,
    connectionRequired: connectorConnectionRequired,
    connectionRows: connectorConnectionRows,
    installId: selectedExtensionInstallId,
    isExtension: isExtensionConnector,
    isPending: extensionInstallContractQuery.isPending,
    isSuccess: extensionInstallContractQuery.isSuccess,
    projectId: selectedProjectId,
  });
  const staleConnectorConnectionId = connectionState.stale;
  const missingConnectorConnection = connectionState.missing;
  const connectorConnectionUnavailable = connectionState.unavailable;
  const connectorConnectionNeedsProjectLink = connectionState.needsProjectLink;
  const extensionFormSchemaQuery = useQuery({
    queryKey: [
      "workflows",
      "meta",
      "extension-form-schema",
      selectedExtensionInstallId,
      selectedConnectorOperationId,
      locale,
    ],
    queryFn: () =>
      saveJson(
        `/api/workflows/meta/extension-form-schema?install_id=${encodeURIComponent(selectedExtensionInstallId)}&operation_id=${encodeURIComponent(selectedConnectorOperationId)}&locale=${encodeURIComponent(locale)}`,
        { method: "GET" },
      ),
    enabled:
      drawerMode === "step" &&
      connectorLikeStepKinds.has(stepKindDraft) &&
      Boolean(selectedExtensionInstallId) &&
      Boolean(selectedConnectorOperationId),
    retry: false,
  });
  const extensionFormSections = useMemo(
    () => listFromUnknown(objectOrEmpty(extensionFormSchemaQuery.data).sections),
    [extensionFormSchemaQuery.data],
  );
  const extensionFormFields = useMemo(
    () => extensionFormSections.flatMap((section) => listFromUnknown(section.fields)),
    [extensionFormSections],
  );
  const connectorParameterRows = useMemo(
    () =>
      extensionFormSections.length
        ? contractRows(extensionFormFields)
        : contractRows(selectedConnectorOperation.parameters),
    [extensionFormFields, extensionFormSections, selectedConnectorOperation],
  );
  const connectorMissingRequiredInputNames = useMemo(
    () =>
      missingRequiredConnectorInputNames(
        stepKindConfig.input_mapping,
        connectorParameterRows,
      ),
    [connectorParameterRows, stepKindConfig.input_mapping],
  );
  const connectorRequiredInputsMissing =
    connectorLikeStepKinds.has(stepKindDraft)
    && connectorMissingRequiredInputNames.length > 0;
  const connectorOutputRows = useMemo(
    () =>
      contractRows(
        selectedConnectorOperation.returns || selectedConnectorOperation.outputs,
      ),
    [selectedConnectorOperation],
  );
  const connectorOperationOptions = useMemo(
    () => distinctLabelOptions(uniqueSelectOptions([
      ...connectorOperationRows.map((row) => {
        const value = textOrEmpty(row.operation_id || row.id || row.key);
        return {
          value,
          label: readableMetadata(row.display_name, [row.operation_id, row.id, row.key])
            || readableMetadata(row.label, [row.operation_id, row.id, row.key])
            // Listed, so it exists: only its name is missing (unless its extension is off).
            || (staleConnectorOperationId && value === selectedConnectorOperationId
              ? t("react.workflows.unavailable_action", "Unavailable action")
              : t("react.workflows.unnamed_action", "Unnamed action")),
        };
      }),
      // Only a saved action the catalog no longer has is unavailable.
      { value: selectedConnectorOperationId, label: t("react.workflows.unavailable_action", "Unavailable action") },
    ])),
    [connectorOperationRows, selectedConnectorOperationId, staleConnectorOperationId, t],
  );
  const connectorConnectionOptions = useMemo(
    () =>
      uniqueSelectOptions([
        ...connectorConnectionRows.map((row) => {
          const eligibility = extensionConnectionEligibility(row, {
            connectionTypeIds: selectedOperationConnectionTypeIds,
            requiredScopes: selectedOperationRequiredScopes,
          }, t);
          return {
            value: textOrEmpty(
              row.connection_id ||
                row.connector_install_id ||
                row.extension_install_id,
            ),
            disabled: eligibility.applicable && !eligibility.allowed,
            label: (() => {
            const displayName =
              textOrEmpty(
                row.display_name ||
                  row.connection_id ||
                  row.connector_install_id ||
                  row.extension_install_id,
              ) || t("react.workflows.display_project_connection", "Project connection");
            const connectedEmail = textOrEmpty(row.connected_email);
            const accountName = connectedEmail && connectedEmail !== displayName
              ? `${displayName} (${connectedEmail})`
              : displayName;
            return eligibility.optionSuffix
              ? `${accountName} — ${eligibility.optionSuffix}`
              : accountName;
          })(),
          };
        }),
        ...(preserveSelectedInstallOptions
          ? [
              {
                value: selectedConnectionId,
                label: selectedConnectionId,
              },
            ]
          : []),
      ]),
    [
      connectorConnectionRows,
      preserveSelectedInstallOptions,
      selectedConnectionId,
      selectedOperationRequiredScopes,
      t,
      selectedOperationConnectionTypeIds,
    ],
  );
  const datasetQueryTemplateOptions = useMemo(
    () =>
      uniqueSelectOptions(
        listFromUnknown(datasetQueryTemplatesQuery.data).map((row) => ({
          value: textOrEmpty(row.query_template_id),
          label:
            textOrEmpty(row.display_name) ||
            textOrEmpty(row.query_template_id) ||
            t("react.workflows.saved_query", "Saved query"),
        })),
      ),
    [datasetQueryTemplatesQuery.data, t],
  );
  const selectedDatasetQueryTemplate = useMemo(
    () =>
      listFromUnknown(datasetQueryTemplatesQuery.data).find(
        (row) =>
          textOrEmpty(row.query_template_id) ===
          textOrEmpty(stepKindConfig.query_template_id),
      ) || {},
    [datasetQueryTemplatesQuery.data, stepKindConfig.query_template_id],
  );
  const callableWorkflowOptions = useMemo(
    () =>
      uniqueSelectOptions(
        listFromUnknown(callableWorkflowsQuery.data?.workflows).map((row) => ({
          value: textOrEmpty(row.workflow_id || row.id),
          label: textOrEmpty(row.display_name || row.workflow_id || row.id),
        })),
      ),
    [callableWorkflowsQuery.data],
  );
  const selectedCallableWorkflow = useMemo(
    () =>
      listFromUnknown(callableWorkflowsQuery.data?.workflows).find(
        (row) =>
          textOrEmpty(row.workflow_id || row.id) ===
          textOrEmpty(stepKindConfig.workflow_id),
      ) || {},
    [callableWorkflowsQuery.data, stepKindConfig.workflow_id],
  );
  const availableWorkflowVariableSuggestions = useMemo(() => {
    const contractData = scopedContractQuery.data;
    const scopedContract = contractData && contractData.workflow_id === workflowId
      ? objectOrEmpty(contractData.interface) : {};
    const producerLabels: Record<string, string> = {};
    const producerMembers: Record<string, Array<Record<string, unknown>>> = Object.create(null);
    for (const row of [...listFromUnknown(objectOrEmpty(workflow).steps), ...stepRows]) {
      const id = textOrEmpty(row.step_id || row.id);
      if (!id) continue;
      producerLabels[id] = textOrEmpty(row.display_name) || id;
      const override = objectOrEmpty(row.override);
      if (textOrEmpty(row.step_kind) === "transform") {
        const transform = objectOrEmpty(override.transform);
        const target = textOrEmpty(transform.result_target).trim().replace(/^state\./, "");
        if (!target) continue;
        producerMembers[`${id}:${target}`] = listFromUnknown(transform.fields).flatMap((rawField) => {
          const field = objectOrEmpty(rawField);
          const name = textOrEmpty(field.to);
          return name ? [{ name, type: field.type, description: field.description }] : [];
        });
      } else if (textOrEmpty(row.step_kind) === "set_variable") {
        const setVariable = objectOrEmpty(override.set_variable);
        const assignments = Object.keys(objectOrEmpty(setVariable.assignments)).length
          ? objectOrEmpty(setVariable.assignments)
          : textOrEmpty(setVariable.target) ? { [textOrEmpty(setVariable.target)]: setVariable.value } : {};
        for (const [rawTarget, value] of Object.entries(assignments)) {
          const target = rawTarget.trim().replace(/^state\./, "");
          if (!target || !value || typeof value !== "object" || Array.isArray(value)) continue;
          const literal = value as Record<string, unknown>;
          const copySource = isSetSourceCopy(value) ? value.source.trim() : "";
          if (copySource.startsWith("state.inputs.") && textOrEmpty(scopedContract.input_mode) === "json_schema") {
            const inputName = copySource.slice("state.inputs.".length);
            const schema = objectOrEmpty(objectOrEmpty(
              objectOrEmpty(scopedContract.input_schema).properties,
            )[inputName]);
            if (textOrEmpty(schema.type) !== "object" ||
                ["$ref", "anyOf", "oneOf", "allOf", "if", "then", "else", "patternProperties"].some((key) => key in schema)) continue;
            producerMembers[`${id}:${target}`] = Object.entries(objectOrEmpty(schema.properties)).flatMap(
              ([name, rawMember]) => {
                const member = objectOrEmpty(rawMember);
                return textOrEmpty(member.type)
                  ? [{ name, label: member.title, type: member.type, description: member.description }] : [];
              },
            );
          } else if (!copySource) {
            producerMembers[`${id}:${target}`] = Object.entries(literal).map(([name, member]) => ({
              name, type: Array.isArray(member) ? "array" : member === null ? "null" : typeof member,
            }));
          }
        }
      }
    }
    const groups = normalizeAvailableWorkflowVariableSuggestions(availableWorkflowVariablesPayload, t,
      { interfaceContract: scopedContract, producerLabels, producerMembers });
    if (!verifiedHook?.binding) return groups;
    const hookInputs = objectOrEmpty(verifiedHook.contract.input_schema?.properties);
    return groups.map((group) => group.key !== "inputs" ? group : {
      ...group,
      options: group.options.map((option) => {
        const childInput = option.value.startsWith("state.inputs.") ? option.value.slice("state.inputs.".length).split(".")[0] : "";
        const hookInput = textOrEmpty(verifiedHook.binding?.input_mapping?.[childInput]);
        if (!hookInput || !Object.hasOwn(hookInputs, hookInput)) return option;
        const source = t("react.workflows.hook_input_source", "Hook input {input}", { input: hookInput });
        return { ...option, description: [option.description, source].filter(Boolean).join(" · ") };
      }),
    });
  }, [availableWorkflowVariablesPayload, scopedContractQuery.data, stepRows, t, verifiedHook, workflow, workflowId]);
  const workflowDestinationOptions = useMemo(() => {
    const contractData = scopedContractQuery.data;
    const contract = contractData && contractData.workflow_id === workflowId
      ? objectOrEmpty(contractData.interface) : {};
    const seen = new Set<string>();
    return (Array.isArray(contract.expected_outputs) ? contract.expected_outputs : []).flatMap((raw: unknown) => {
      const row = objectOrEmpty(raw);
      const name = textOrEmpty(row.name);
      if (!name || seen.has(name)) return [];
      seen.add(name);
      const label = textOrEmpty(row.label || row.title || row.display_name) ||
        name.replaceAll(/[_-]+/g, " ").replaceAll(/\b\w/g, (letter) => letter.toUpperCase());
      const mapping = textOrEmpty(verifiedHook?.binding?.output_mapping?.[name]);
      const writable = Boolean(mapping && (verifiedHook?.contract.mutable_fields?.includes(mapping) ||
        Object.hasOwn(verifiedHook?.contract.additional_outputs || {}, mapping)));
      const mappingDetail = verifiedHook
        ? writable
          ? t("react.workflows.hook_return_mapping", "Returned to Hook {target}: {path}", {
              target: mapping, path: `state.hooks.${verifiedHook.hook_id}.output.${mapping}`,
            })
          : t("react.workflows.hook_child_result_only", "Child result only; no parent Hook mapping")
        : "";
      return [{ value: `state.outputs.${name}`, label,
        description: [textOrEmpty(row.description), mappingDetail].filter(Boolean).join(" · "),
        meta: textOrEmpty(row.type), stepId: "", variant: "workflow" as const }];
    });
  }, [scopedContractQuery.data, t, verifiedHook, workflowId]);
  const availableWorkflowVariableSuggestionCount = useMemo(
    () =>
      availableWorkflowVariableSuggestions.reduce(
        (total, group) => total + group.options.length,
        0,
      ),
    [availableWorkflowVariableSuggestions],
  );
  const workflowSuggestionStructure = useMemo(() => {
    const inputsGroup = availableWorkflowVariableSuggestions.find((group) => group.key === "inputs") || null;
    const globalGroup =
      availableWorkflowVariableSuggestions.find(
        (group) => group.key === "global",
      ) || null;
    const workflowGroup =
      availableWorkflowVariableSuggestions.find(
        (group) => group.key === "workflow",
      ) || null;
    const stateAliasOptions =
      availableWorkflowVariableSuggestions.find((group) => group.key === "step")
        ?.options || [];
    const stepArtifactGroups = availableWorkflowVariableSuggestions.filter(
      (group) => group.key.startsWith("step:"),
    );
    const stepIds = Array.from(
      new Set([
        ...stateAliasOptions.map((option) => option.stepId).filter(isTruthy),
        ...stepArtifactGroups.map((group) => group.key.replace(/^step:/, "")),
      ]),
    ).sort((left, right) => left.localeCompare(right));
    const steps = stepIds.map((stepId) => ({
      aliasOptions: stateAliasOptions.filter(
        (option) => option.stepId === stepId,
      ),
      artifactOptions:
        stepArtifactGroups.find((group) => group.key === `step:${stepId}`)
          ?.options || [],
      stepId,
    }));
    return { inputsGroup, globalGroup, steps, workflowGroup };
  }, [availableWorkflowVariableSuggestions]);

  async function handleLinkProjectForBuilder() {
    const normalizedWorkflowId = textOrEmpty(workflowId);
    const normalizedProjectId = textOrEmpty(projectLinkDraft);
    if (!normalizedWorkflowId || !normalizedProjectId) {
      setProjectLinkMessage(t("react.workflows.choose_a_project_before_linking_this_workflow", "Choose a project before linking this workflow."));
      return false;
    }
    setProjectLinkPending(true);
    setProjectLinkMessage("");
    try {
      const currentBindings =
        await fetchProjectWorkflowBindings(normalizedProjectId);
      const currentWorkflowIds = Array.isArray(currentBindings.workflow_ids)
        ? currentBindings.workflow_ids
            .map((value: unknown) => textOrEmpty(value))
            .filter(isTruthy)
        : [];
      const nextWorkflowIds = Array.from(
        new Set([...currentWorkflowIds, normalizedWorkflowId]),
      );
      // Linking for a Test Run adds the workflow and nothing else: the
      // project's default is a separate decision, and making this workflow the
      // default required it to be enabled, so a draft could not be linked.
      await saveProjectWorkflowBindings(normalizedProjectId, {
        workflow_ids: nextWorkflowIds,
      });
      setSearchParams((current: URLSearchParams) => {
        const next = new URLSearchParams(current);
        next.set("project_id", normalizedProjectId);
        return next;
      });
      await linkedProjectQuery.refetch();
      await availableProjectsQuery.refetch();
      setProjectLinkMessage(t("react.workflows.project_linked_test_run_is_ready", "Project linked. Test Run is ready."));
      return true;
    } catch (error) {
      setProjectLinkMessage(
        error instanceof Error ? error.message : t("react.workflows.failed_to_link_project", "Failed to link project."),
      );
      return false;
    } finally {
      setProjectLinkPending(false);
    }
  }

  const {
    renderProjectLinkControl,
    renderWorkflowVariableInput,
    renderWorkflowDestinationInput,
  } = createWorkflowVariableSuggestionRenderers({
    activeWorkflowSuggestionPanelId,
    availableProjectsQuery,
    availableWorkflowVariableSuggestionCount,
    expandedWorkflowSuggestionKeys,
    handleLinkProjectForBuilder,
    projectLinkDraft,
    projectLinkMessage,
    projectLinkPending,
    selectedProjectId,
    setActiveWorkflowSuggestionPanelId,
    setExpandedWorkflowSuggestionKeys,
    setProjectLinkDraft,
    setWorkflowSuggestionHighlight,
    setWorkflowSuggestionTyped,
    workflowSuggestionHighlight,
    workflowSuggestionTyped,
    workflowVariablePositionUnknown,
    workflowSuggestionStructure,
    workflowDestinationOptions,
  }, t);

  return {
    stepToolCategory,
    activeWorkflowSuggestionPanelId,
    availableWorkflowVariableSuggestions,
    workflowVariablePositionUnknown,
    callableWorkflowOptions,
    callableWorkflows: listFromUnknown(callableWorkflowsQuery.data?.workflows),
    connectorConnectionRequired,
    connectorConnectionSetupHref,
    connectorConnectionsRefreshing: extensionInstallContractQuery.isFetching,
    refreshConnectorConnections: () => extensionInstallContractQuery.refetch(),
    connectorConnectionOptions,
    connectorConnectionRows,
    extensionConnectionReadinessBlocked,
    extensionConnectionReadinessMessage,
    selectedConnectionTypeLabel,
    extensionInstallAvailability,
    connectorOperationOptions,
    connectorOperationRows,
    connectorOptions,
    connectorRows,
    connectorOutputRows,
    connectorParameterRows,
    connectorMissingRequiredInputNames,
    connectorRequiredInputsMissing,
    agentCatalogQuery,
    agentBlockCatalogQuery,
    agentBlockOptions,
    datasetQueryTemplateOptions,
    datasetQueryTemplatesQuery,
    renderProjectLinkControl,
    renderWorkflowVariableInput,
    renderWorkflowDestinationInput,
    selectedCallableWorkflow,
    selectedDatasetQueryTemplate,
    selectedConnectorId,
    selectedExtensionInstallId,
    selectedExtensionReleaseId,
    selectedExtensionDescriptorSha256,
    selectedConnectorOperation,
    selectedConnectorOperationId,
    selectedOperationConnectionTypeId,
    selectedConnectorRow,
    selectedConnectorDisplayName: connectorCatalogOptionLabel(selectedConnectorRow),
    staleConnectorConnectionId,
    missingConnectorConnection,
    connectorConnectionUnavailable,
    connectorConnectionNeedsProjectLink,
    staleConnectorOperationId,
    stepAgentOptions,
    stepKindConfig,
    stepSkillOptions,
  };
}
