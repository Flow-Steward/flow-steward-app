import { Button } from "@/components/ui/button";
import { useRef } from "react";
import { useI18n } from "@/lib/i18n/useI18n";
import { isToolCategory } from "./detail-tool-categories";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { WorkflowStepStructureSetup } from "@/pages/workflows/detail/detail-step-structure-setup";
import { WorkflowStepKindSmartSetup } from "@/pages/workflows/detail/detail-step-smart-setup";
import { withTabularWriteFormatDefault } from "@/pages/workflows/detail/detail-step-editing";
import { createWorkflowConnectorRenderers } from "@/pages/workflows/detail/detail-connector-renderers";
import { createWorkflowContractRenderers } from "@/pages/workflows/detail/detail-contract-renderers";
import { createWorkflowInterfaceRenderers } from "@/pages/workflows/detail/detail-interface-renderers";
import {
  RETURNED_FILES_FIELD,
  effectiveReturnedFiles,
  stepFileResults,
  type ReturnedFileEntry,
} from "@/pages/workflows/detail/detail-returned-files";
import {
  ifConditionDraftFromUnknown,
  overrideDraftForStepKindChange,
  ifConditionFromDraft,
  jsonText,
  objectFromEntries,
  objectOrEmpty,
  stepOverrideObjectFromDraft,
  textOrEmpty,
  workflowVariableSuggestionPanelId,
  type IfConditionDraft,
} from "@/pages/workflows/detail/detail-helpers";

export function useWorkflowStepRendererController(ctx: Record<string, any>) {
  const { t } = useI18n();
  const {
    availableWorkflowVariableSuggestions,
    callableWorkflowOptions,
    connectorConnectionRequired,
    connectorConnectionSetupHref,
    connectorConnectionsRefreshing,
    refreshConnectorConnections,
    connectorConnectionOptions,
    connectorConnectionRows,
    extensionConnectionReadinessMessage,
    extensionInstallAvailability,
    connectorOperationOptions,
    // The catalog rows the source and action handlers search. Without them the
    // handlers found nothing and wrote the install identity as empty strings,
    // so a clean-install save was refused for an identity the catalog had.
    connectorOperationRows,
    connectorOptions,
    connectorOutputRows,
    connectorParameterRows,
    connectorRows,
    agentCatalogQuery,
    datasetQueryTemplateOptions,
    datasetQueryTemplatesQuery,
    handleApplyIfConditionFix,
    renderWorkflowVariableInput,
    renderWorkflowDestinationInput,
    selectedCallableWorkflow,
    selectedDatasetQueryTemplate,
    selectedCanvasIssue,
    selectedConnectorId,
    selectedExtensionInstallId,
    selectedExtensionReleaseId,
    selectedExtensionDescriptorSha256,
    selectedConnectorOperation,
    selectedConnectorOperationId,
    selectedOperationConnectionTypeId,
    selectedConnectionTypeLabel,
    selectedConnectorRow,
    selectedIssueWorkflowContractSection,
    selectedProjectId,
    staleConnectorConnectionId,
    missingConnectorConnection,
    connectorConnectionUnavailable,
    connectorConnectionNeedsProjectLink,
    staleConnectorOperationId,
    selectedStep,
    setStepAgentId,
    setStepKindDraft,
    setStepToolCategoryDraft,
    stepToolCategory,
    setStepOverrideDraft,
    setWorkflowContractSectionsExpanded,
    stepAgentId,
    stepAgentLocked,
    stepAgentOptions,
    stepKindConfig,
    stepKindDraft,
    stepOverrideDraft,
    stepStructureContext,
    transformActionsQuery,
    workflowId,
    workflowContractSectionsExpanded,
    workflowInterfaceControlIdPrefix,
    workflowInterfaceNameValidationAttempted,
    workflowInterfaceOutputsDraft,
    workflowInterfaceReturnedFiles,
  } = ctx;

  const kindAdmission = useRef(0);
  async function handleStepKindChange(nextKind: string) {
    const attempt = ++kindAdmission.current;
    const intent = ctx.httpIntent;
    const token = intent?.capture();
    const agentToken = stepKindDraft === "agent" ? ctx.agentDrafts?.capture() : undefined;
    if ((stepKindDraft === "http_request" && intent?.hasRaw()) || (stepKindDraft === "agent" && ctx.agentDrafts?.invalid)) {
      if (!(await ctx.confirmDiscardInvalidInput?.(true))) return;
    }
    if (attempt !== kindAdmission.current || (token && !intent.isUnedited(token)) || (agentToken && !ctx.agentDrafts.isUnedited(agentToken))) return;
    intent?.beginEditorSession();
    ctx.agentDrafts?.reset();
    const toolCategory = isToolCategory(nextKind) ? nextKind : "";
    const normalized = toolCategory ? "connector" : textOrEmpty(nextKind) || "agent";
    setStepToolCategoryDraft(toolCategory);
    setStepKindDraft(normalized);
    const override = stepOverrideObjectFromDraft(stepOverrideDraft, t);
    if (toolCategory && (stepKindDraft !== "connector" || toolCategory !== stepToolCategory)) {
      // Empty values override the selected saved row as well as the draft.
      override.connector = { connector_id: "", operation_id: "", extension_id: "", connector_install_id: "", input_mapping: {}, output_mapping: {}, connection_binding: {} };
    }
    setStepOverrideDraft(overrideDraftForStepKindChange(jsonText(override), normalized));
  }

  function updateStepKindConfig(
    stepKind: string,
    patch: Record<string, unknown>,
  ) {
    setStepOverrideDraft((currentDraft: string) => {
      const currentOverride = stepOverrideObjectFromDraft(currentDraft, t);
      const currentConfig = objectOrEmpty(currentOverride[stepKind]);
      const nextConfig = { ...currentConfig, ...patch };
      return jsonText({
        ...currentOverride,
        [stepKind]: stepKind === "tabular_feed"
          ? withTabularWriteFormatDefault(nextConfig)
          : nextConfig,
      });
    });
  }

  function replaceStepKindConfigField(
    stepKind: string,
    field: string,
    value: unknown,
  ) {
    updateStepKindConfig(stepKind, { [field]: value });
  }

  /** Save "Return from workflow" for this step's file results. */
  function updateStepReturnedFiles(entries: ReturnedFileEntry[]) {
    setStepOverrideDraft((currentDraft: string) => jsonText({
      ...stepOverrideObjectFromDraft(currentDraft, t),
      [RETURNED_FILES_FIELD]: entries,
    }));
  }

  const stepOverrideObject = stepOverrideObjectFromDraft(stepOverrideDraft, t);
  const stepAuthoredId = textOrEmpty(objectOrEmpty(selectedStep?.raw).step_id) || textOrEmpty(selectedStep?.runtimeId);
  const contractReturnedFiles = Array.isArray(workflowInterfaceReturnedFiles) ? workflowInterfaceReturnedFiles : [];
  const stepReturnedFiles = effectiveReturnedFiles(stepOverrideObject, stepAuthoredId, contractReturnedFiles);
  // A new key may not repeat another output's name, in this step or elsewhere.
  const returnedFileTakenKeys = [
    ...contractReturnedFiles.map((row: Record<string, unknown>) => textOrEmpty(row.name)),
    ...(Array.isArray(workflowInterfaceOutputsDraft) ? workflowInterfaceOutputsDraft : []).map((row: Record<string, unknown>) => textOrEmpty(row.name)),
    ...stepReturnedFiles.map((entry) => entry.key),
  ].filter(Boolean);

  function updateConfigMapField(
    stepKind: string,
    field: string,
    rows: Array<{ key: string; value: string }>,
  ) {
    replaceStepKindConfigField(stepKind, field, objectFromEntries(rows));
  }

  function updateIfConditionDraft(patch: Partial<IfConditionDraft>) {
    const draft = ifConditionDraftFromUnknown(stepKindConfig.condition);
    replaceStepKindConfigField(
      "if",
      "condition",
      ifConditionFromDraft({ ...draft, ...patch }),
    );
  }

  function unsupportedConditionDisplayReason(reason: string | undefined): string {
    switch (reason) {
      case "This state comparison can be stored as a structured condition without changing its value.":
        return t("react.workflows.display_if_comparison_replacement", "This state comparison can be stored as a structured condition without changing its value.");
      case "This raw IF condition uses a constant value. Apply the replacement below to store it as a structured constant condition.":
        return t("react.workflows.display_if_constant_replacement", "This raw IF condition uses a constant value. Apply the replacement below to store it as a structured constant condition.");
      case "This raw IF condition cannot be edited safely as a structured selector.":
        return t("react.workflows.display_if_unsafe_selector", "This raw IF condition cannot be edited safely as a structured selector.");
      case "This IF condition is missing the selector field needed by the editor.":
        return t("react.workflows.display_if_missing_selector", "This IF condition is missing the selector field needed by the editor.");
      case "This IF condition is missing the left-side selector needed by the editor.":
        return t("react.workflows.display_if_missing_left_selector", "This IF condition is missing the left-side selector needed by the editor.");
      case "This raw IF condition shape is not supported by the structured editor.":
        return t("react.workflows.display_if_unsupported_shape", "This raw IF condition shape is not supported by the structured editor.");
      case "This raw IF condition is not a structured selector condition.":
        return t("react.workflows.display_if_not_structured", "This raw IF condition is not a structured selector condition.");
      default: {
        const unsupportedOperator = /^The IF operator "(.*)" is not supported by the structured editor\.$/.exec(reason || "");
        return unsupportedOperator
          ? t("react.workflows.display_if_unsupported_operator", 'The IF operator "{operator}" is not supported by the structured editor.', { operator: unsupportedOperator[1] })
          : reason || "";
      }
    }
  }

  function renderUnsupportedIfConditionNotice(
    ifDraft: IfConditionDraft,
    onApply?: () => void,
    applyLabel = t("react.workflows.display_apply_fix", "Apply fix"),
  ) {
    if (!ifDraft.invalidSource && !ifDraft.invalidReason) {
      return null;
    }
    return (
      <div className="rounded-lg border border-danger fs-surface-danger px-3 py-2 text-xs fs-ink-danger">
        <p className="font-medium uppercase tracking-[0.12em] fs-ink-danger">
          {t("react.workflows.unsupported_raw_if_condition", "Unsupported raw IF condition")}</p>
        <p className="mt-1 fs-ink-danger">
          {unsupportedConditionDisplayReason(ifDraft.invalidReason) ||
            t("react.workflows.this_saved_condition_could_not_be_converted_safely", "This saved condition could not be converted safely.")}{" "}
          {t("react.workflows.edit_the_structured_fields_below_to_replace_it", "Edit the structured fields below to replace it.")}</p>
        {ifDraft.invalidSource ? (
          <p className="mt-2 truncate rounded border border-danger fs-surface-inset px-2 py-1 font-mono text-[11px] fs-ink-danger">
            {ifDraft.invalidSource}
          </p>
        ) : null}
        {onApply && ifDraft.canApplyFix ? (
          <Button
            variant="secondary" className="mt-2"
            onClick={onApply}
            type="button"
          >
            {applyLabel}
          </Button>
        ) : null}
      </div>
    );
  }

  const {
    renderConfigMapEditor,
    renderSwitchCasesEditor,
    renderWorkflowContractMappingEditor,
    renderWorkflowContractSection,
  } = createWorkflowContractRenderers({
    renderWorkflowVariableInput,
    replaceStepKindConfigField,
    selectedCanvasIssue,
    selectedIssueWorkflowContractSection,
    setWorkflowContractSectionsExpanded,
    stepKindConfig,
    updateConfigMapField,
    workflowContractSectionsExpanded,
  }, t);

  const {
    renderContractChips,
    renderWorkflowInterfaceFieldEditor,
    renderWorkflowReturnedFiles,
  } = createWorkflowInterfaceRenderers({
    workflowInterfaceControlIdPrefix,
    workflowInterfaceNameValidationAttempted,
    workflowInterfaceReturnedFiles,
  }, t);
  const {
    renderConnectorInputMappingEditor,
    renderConnectorOutputMappingEditor,
  } = createWorkflowConnectorRenderers({
    connectorConnectionRows,
    connectorOutputRows,
    connectorParameterRows,
    renderWorkflowVariableInput,
    replaceStepKindConfigField,
    selectedConnectorId,
    selectedExtensionInstallId,
    selectedExtensionReleaseId,
    selectedExtensionDescriptorSha256,
    selectedProjectId,
    selectedStep,
    stepKindConfig,
  }, t);

  const datasetResultTargetListId = workflowVariableSuggestionPanelId(
    "dataset-query-result-target",
  );
  const httpResponseTargetListId = workflowVariableSuggestionPanelId(
    "http-request-response-target",
  );
  const rootIfSelectorListId = workflowVariableSuggestionPanelId(
    "root-if-selector",
  );
  const rootSwitchSelectorListId = workflowVariableSuggestionPanelId(
    "root-switch-selector",
  );
  const collectTargetListId = workflowVariableSuggestionPanelId("collect-target");
  const collectSourceListId = workflowVariableSuggestionPanelId("collect-source");
  const collectKeyListId = workflowVariableSuggestionPanelId("collect-key");
  const batchSourcePathListId = workflowVariableSuggestionPanelId(
    "batch-source-path",
  );
  const batchResultTargetListId = workflowVariableSuggestionPanelId(
    "batch-result-target",
  );

  const stepSmartSetupContext = {
    stepContractPreviewQuery: ctx.stepContractPreviewQuery,
    agentDrafts: ctx.agentDrafts, agentWorkflowRecord: ctx.agentWorkflowRecord,
    agentBlockCatalogQuery: ctx.agentBlockCatalogQuery, agentBlockOptions: ctx.agentBlockOptions,
    httpIntent: ctx.httpIntent,
    httpFieldFeedback: ctx.httpFieldFeedback,
    stepToolCategory,
    onStepInputValidationChange: ctx.onStepInputValidationChange,
    hookDeclarationEditorKey: JSON.stringify([textOrEmpty(workflowId), textOrEmpty(selectedProjectId), textOrEmpty(selectedStep?.runtimeId)]),
    hookDeclarationIdentityLocked: ctx.hookDeclarationIdentityLocked,
    hookDeclarationReadOnly: ctx.hookDeclarationReadOnly,
    hookDeclarationIssues: ctx.hookDeclarationIssues,
    availableWorkflowVariableSuggestions,
    batchResultTargetListId,
    batchSourcePathListId,
    callableWorkflowOptions,
    connectorConnectionRequired,
    connectorConnectionSetupHref,
    connectorConnectionsRefreshing,
    refreshConnectorConnections,
    selectedExtensionInstallId,
    connectorConnectionOptions,
    connectorConnectionRows,
    extensionConnectionReadinessMessage,
    extensionInstallAvailability,
    connectorOperationOptions,
    connectorOperationRows,
    connectorOptions,
    connectorOutputRows,
    connectorParameterRows,
    connectorRows,
    agentCatalogQuery,
    datasetQueryTemplateOptions,
    datasetQueryTemplatesQuery,
    datasetResultTargetListId,
    collectKeyListId,
    collectSourceListId,
    collectTargetListId,
    handleApplyIfConditionFix,
    httpResponseTargetListId,
    renderConfigMapEditor,
    renderConnectorInputMappingEditor,
    renderConnectorOutputMappingEditor,
    renderContractChips,
    renderSwitchCasesEditor,
    renderUnsupportedIfConditionNotice,
    renderWorkflowContractMappingEditor,
    renderWorkflowVariableInput,
    renderWorkflowDestinationInput,
    replaceStepKindConfigField,
    rootIfSelectorListId,
    rootSwitchSelectorListId,
    selectedCallableWorkflow,
    selectedDatasetQueryTemplate,
    selectedConnectorId,
    selectedConnectorOperation,
    selectedConnectorOperationId,
    selectedOperationConnectionTypeId,
    selectedConnectionTypeLabel,
    selectedConnectorRow,
    selectedProjectId,
    staleConnectorConnectionId,
    missingConnectorConnection,
    connectorConnectionUnavailable,
    connectorConnectionNeedsProjectLink,
    staleConnectorOperationId,
    setStepAgentId,
    stepAgentId,
    stepAgentLocked,
    stepAgentOptions,
    stepKindConfig,
    stepKindDraft,
    transformActionsQuery,
    updateIfConditionDraft,
    updateStepKindConfig,
    updateStepReturnedFiles,
    returnedFileTakenKeys,
    stepFileResultRows: stepFileResults(stepKindDraft, stepOverrideObject, connectorOutputRows),
    stepReturnedFiles,
    workflowId,
  };

  function renderStepKindSmartSetup() {
    const runtimeId = textOrEmpty(selectedStep?.runtimeId);
    const key = stepKindDraft === "set_variable" || stepKindDraft === "approval"
      ? JSON.stringify([selectedProjectId, workflowId, runtimeId, stepKindDraft])
      : `${runtimeId}:${stepKindDraft}`;
    return <WorkflowStepKindSmartSetup ctx={stepSmartSetupContext} key={key} />;
  }

  function renderStepStructureSetup() {
    return <WorkflowStepStructureSetup ctx={stepStructureContext} />;
  }


  return {
    handleStepKindChange,
    renderStepStructureSetup,
    renderStepKindSmartSetup,
    renderWorkflowContractSection,
    renderWorkflowInterfaceFieldEditor,
    renderWorkflowReturnedFiles,
  };
}
