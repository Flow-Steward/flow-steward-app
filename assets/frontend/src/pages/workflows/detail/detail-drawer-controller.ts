import { isToolCategory, type ToolCategory } from "./detail-tool-categories";
import { applyAgentBlockIntent } from "./detail-agent-step-fields";
import { ignoreAsyncResult } from "@/lib/utils/async";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any, react-hooks/exhaustive-deps */
import { useCallback, useEffect, useLayoutEffect, useRef } from "react";

import {
  defaultOverrideForStepKind,
  editableOverrideForStep,
  jsonText,
  objectOrEmpty,
  textOrEmpty,
  type WorkflowGroupRow,
  type WorkflowStepRow,
} from "@/pages/workflows/detail/detail-helpers";
import { hookMemberId, parseHookNodeId } from "@/pages/workflows/builder/hook-frames";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { useIsAdmin } from "@/lib/auth/use-is-admin";
import { groupLanes } from "@/pages/workflows/builder/change-set";
import type { WorkflowNodeRef, WorkflowOutcome, WorkflowStep, WorkflowGroup, WorkflowForEach } from "@/pages/workflows/shared/api";
import type { NewContainerDefaults, StepEditorTab } from "@/pages/workflows/builder/canvas-types";

/** Drawer modes that mutate the workflow (authoring) — admin-only. Read-only drawers
 * such as test-run stay available to operators. Used to gate `?drawer=` deep links so
 * a non-admin cannot auto-open an authoring drawer that bypasses the hidden buttons. */
const AUTHORING_DRAWER_PARAMS = new Set([
  "group",
  "step",
  "workflow-settings",
  "settings",
  "schedules",
  "automation",
  "reactions",
]);

/** Where a new step goes. Placement is explicit: root, a Group or For each body, or one lane. */
export type StepCreateContext = {
  groupId: string;
  forEachId?: string;
  laneId: string;
  /**
   * Put the new step after this outcome. On a connected outcome it splits the edge (an
   * ordinary single-outcome step only); `append` marks an outcome that leads nowhere yet,
   * where any kind is created and connected after it.
   */
  insert?: { source: WorkflowNodeRef; outcome: WorkflowOutcome; label: string; append?: boolean } | null;
};

/** The drawer draft of one container: a Group (runs once) or a For each (runs per item). */
export type GroupDraft = {
  containerKind: "group" | "for_each";
  bodyKind: "sequence" | "lanes";
  concurrencyLimit: string;
  description: string;
  displayName: string;
  /** The container id (a Group id or a For each id). */
  groupId: string;
  lanes: string;
  /** For each only: the source list, the item and index names, and the sequence Group it sits in. */
  over: string;
  itemName: string;
  indexName: string;
  parentGroupId: string;
};

export type GroupDraftDefaults = NewContainerDefaults;

export function emptyGroupDraft(defaults: GroupDraftDefaults = {}): GroupDraft {
  const containerKind = defaults.containerKind || "group";
  const bodyKind = defaults.bodyKind || "sequence";
  return {
    containerKind,
    bodyKind,
    concurrencyLimit: "",
    description: "",
    displayName: "",
    groupId: "",
    lanes: bodyKind === "lanes" ? "lane_a, lane_b" : "",
    over: containerKind === "for_each" ? "state.items" : "",
    itemName: "item",
    indexName: "",
    parentGroupId: containerKind === "for_each" ? textOrEmpty(defaults.parentGroupId) : "",
  };
}

export function groupDraftFromRow(row: WorkflowGroupRow): GroupDraft {
  const concurrency = (row.raw.body as Record<string, unknown> | undefined)?.concurrency_limit;
  return {
    containerKind: row.containerKind,
    bodyKind: row.bodyKind === "lanes" ? "lanes" : "sequence",
    concurrencyLimit:
      typeof concurrency === "number" || typeof concurrency === "string" ? String(concurrency) : "",
    description: row.description,
    displayName: row.displayName,
    groupId: row.groupId,
    lanes: row.lanes.join(", "),
    over: row.over,
    itemName: row.itemName || "item",
    indexName: row.indexName,
    parentGroupId: row.parentGroupId,
  };
}

export function useWorkflowDrawerController(ctx: Record<string, any>) {
  const { t } = useI18n();
  const canAuthorWorkflow = useIsAdmin();
  const {
    advancedMode,
    definitionRows,
    drawerMode,
    drawerTab,
    groupCreateMode,
    groupDraft,
    groupFormDirty,
    savingWorkflowSurface,
    searchParams,
    selectedGroupId,
    selectedStepId,
    confirmDiscardParameters,
    confirmDiscardInvalidInput,
    hasInvalidStepInput,
    protectedWorkflowStructureRedacted,
    parameterSwitchNeedsConfirmation,
    selectedIssueWorkflowContractSection,
    setAdvancedMode,
    setAutomationScopeKind,
    setDrawerMode,
    setDrawerTab,
    setGroupCreateMode,
    setGroupDraft,
    setReactionFormVisible,
    setSaveNotice,
    setSearchParams,
    setSelectedCanvasIssue,
    setSelectedGroupId,
    setSelectedStepId,
    setStepAgentId,
    setStepBlockKey,
    setStepCreateContext,
    setStepDisplayName,
    setStepKindDraft,
    setStepToolCategoryDraft,
    setStepOptional,
    setStepOverrideDraft,
    setWorkflowTestRunLaunchOpen,
  } = ctx;
  const drawerRequestKey: string | URLSearchParams = ctx.drawerRequestKey ?? searchParams;
  const stepDrawerRequest = useRef<{ key: string | URLSearchParams; consumed: boolean } | null>(null);
  const stepRowsById = definitionRows.stepRowsById as Map<string, WorkflowStepRow>;
  const groupRows = definitionRows.groupRows as WorkflowGroupRow[];
  // Presentation belongs to one unapplied creation, never to the workflow payload.
  const localToolCategories = useRef(new Map<string, { category: ToolCategory; owner?: { isCurrent: () => boolean } }>());
  const localToolScope = useRef(ctx.localToolDraftOwnerKey);
  const localToolContext = useRef(ctx);
  useLayoutEffect(() => {
    localToolContext.current = ctx;
    if (localToolScope.current !== ctx.localToolDraftOwnerKey) {
      localToolCategories.current.clear();
      localToolScope.current = ctx.localToolDraftOwnerKey;
    }
    for (const [id, draft] of localToolCategories.current) {
      draft.owner ??= ctx.captureLocalToolDraftOwner?.(id) ?? undefined;
      if (!draft.owner?.isCurrent()) localToolCategories.current.delete(id);
    }
    const selected = stepRowsById.get(selectedStepId);
    const draft = localToolCategories.current.get(selectedStepId);
    if (draft && selected?.local && !configuredConnectorId(selected) && isToolCategory(ctx.stepToolCategoryDraft)) {
      draft.category = ctx.stepToolCategoryDraft;
    }
  });
  function configuredConnectorId(row: WorkflowStepRow) {
    return textOrEmpty(row.raw.connector_id) || textOrEmpty(objectOrEmpty(objectOrEmpty(row.raw.override).connector).connector_id);
  }
  const localToolCategory = useCallback((row: WorkflowStepRow | null, canvasId: string) => {
    const draft = localToolCategories.current.get(canvasId);
    return row?.local && row.stepKind === "connector" && !configuredConnectorId(row) &&
      localToolScope.current === localToolContext.current.localToolDraftOwnerKey && draft?.owner?.isCurrent()
      ? draft.category : "";
  }, []);

  const groupRowFor = (id: string) => ctx.resolveGroupRow ? ctx.resolveGroupRow(id) as WorkflowGroupRow | null : groupRows.find((row) => row.groupId === id) || null;

  const drawerTitle = (() => {
    if (drawerMode === "workflow-interface") return t("react.workflows.workflow_interface", "Workflow interface");
    if (drawerMode === "workflow-test-run") {
      return t("react.workflows.test_run_results", "Test Run results");
    }
    if (drawerMode === "workflow-settings") return t("react.workflows.manual_workflow_settings", "Workflow settings");
    if (drawerMode === "group") {
      return groupDraft.containerKind === "for_each"
        ? t("react.workflows.for_each_editor", "For each editor")
        : t("react.workflows.manual_group_editor", "Group editor");
    }
    if (drawerMode === "library") return t("react.workflows.manual_step_library", "Step library");
    if (drawerMode === "step") return t("react.workflows.manual_step_editor", "Step editor");
    return t("react.workflows.manual_workflow_settings", "Workflow settings");
  })();

  const loadStepDrafts = useCallback((row: WorkflowStepRow | null, blockKeyOverride = "", canvasId = row?.runtimeId || "") => {
    ctx.httpIntent?.beginEditorSession();
    setStepDisplayName(textOrEmpty(row?.primaryLabel) || textOrEmpty(row?.runtimeId));
    setStepKindDraft(textOrEmpty(row?.stepKind) || "agent");
    setStepToolCategoryDraft(localToolCategory(row, canvasId));
    setStepOverrideDraft(row ? jsonText(editableOverrideForStep(row)) : "");
    setStepAgentId(textOrEmpty(row?.agentId));
    setStepOptional(Boolean(row?.optional));
    setStepBlockKey(textOrEmpty(blockKeyOverride) || textOrEmpty(row?.blockKey));
  }, []);

  const openStepDrawer = useCallback(
    async (stepId: string, blockKeyOverride = "", confirmed = false, requestedTab: StepEditorTab = "smart-setup", isCurrent?: () => boolean) => {
      if (isCurrent && !isCurrent()) return false;
      if (stepId === selectedStepId && requestedTab !== drawerTab && hasInvalidStepInput && !(await confirmDiscardInvalidInput())) return false;
      if (isCurrent && !isCurrent()) return false;
      if (!confirmed && ctx.prepareSelection && !(await ctx.prepareSelection("step", stepId))) return false;
      if (!confirmed && !ctx.prepareSelection && stepId !== selectedStepId && parameterSwitchNeedsConfirmation && !(await confirmDiscardParameters())) return false;
      if (isCurrent && !isCurrent()) return false;
      const row = stepRowsById.get(stepId) || null;
      setStepCreateContext(null);
      const hook = parseHookNodeId(stepId);
      const owner = row?.forEachId || row?.groupId || "";
      setSelectedGroupId(hook?.memberStepId && owner ? hookMemberId(hook.hookId, owner) : owner);
      setSelectedStepId(stepId);
      if (stepId !== selectedStepId) loadStepDrafts(row, blockKeyOverride, stepId);
      else setStepToolCategoryDraft(localToolCategory(row, stepId));
      setDrawerMode("step");
      setDrawerTab(requestedTab);
      setAutomationScopeKind("step");
      setReactionFormVisible(false);
      return true;
    },
    [confirmDiscardParameters, confirmDiscardInvalidInput, drawerTab, hasInvalidStepInput, parameterSwitchNeedsConfirmation, loadStepDrafts, selectedStepId, stepRowsById, ctx.prepareSelection],
  );

  /** Explicit handoff after a guarded local transaction, before row derivation. */
  const openCreatedStep = useCallback((step: WorkflowStep, canvasStepId: string, toolCategory?: ToolCategory) => {
    const stepId = textOrEmpty(step.step_id);
    const groupId = textOrEmpty(step.group_id);
    const forEachId = textOrEmpty(step.for_each_id);
    const row: WorkflowStepRow = {
      agentId: textOrEmpty(step.agent_id), agentLabel: "", blockKey: textOrEmpty(step.block_key),
      groupId, forEachId, laneId: textOrEmpty(step.lane_id), local: true,
      optional: Boolean(step.optional), ordinal: 0,
      primaryLabel: textOrEmpty(step.display_name) || stepId, raw: step,
      runtimeId: stepId, secondaryLabel: "", skillLabel: "", skillId: "",
      stepKind: textOrEmpty(step.step_kind), stepKey: stepId,
    };
    const hook = parseHookNodeId(canvasStepId);
    const owner = forEachId || groupId;
    setGroupCreateMode(false);
    setStepCreateContext(null);
    ctx.setCreateAsEntry?.(false);
    setSelectedGroupId(hook?.memberStepId && owner ? hookMemberId(hook.hookId, owner) : owner);
    setSelectedStepId(canvasStepId);
    if (step.step_kind === "connector" && toolCategory) {
      localToolCategories.current.set(canvasStepId, { category: toolCategory });
    }
    loadStepDrafts(row, "", canvasStepId);
    if (step.step_kind === "connector" && toolCategory) setStepToolCategoryDraft(toolCategory);
    setDrawerMode("step"); setDrawerTab("smart-setup");
    setAutomationScopeKind("step"); setReactionFormVisible(false);
  }, [loadStepDrafts]);

  /** Prepared container fields arrive in the same guarded transaction as its stage. */
  const openCreatedContainer = useCallback((container: WorkflowGroup | WorkflowForEach, canvasId: string, nodeKind: "group" | "for_each") => {
    const raw = objectOrEmpty(container);
    const body = objectOrEmpty(raw.body);
    const id = textOrEmpty(nodeKind === "group" ? raw.group_id : raw.for_each_id);
    setGroupCreateMode(false); setStepCreateContext(null); ctx.setCreateAsEntry?.(false);
    setSelectedGroupId(canvasId); setSelectedStepId("");
    setGroupDraft({
      ...emptyGroupDraft({ containerKind: nodeKind }), groupId: id,
      displayName: textOrEmpty(raw.display_name) || id, description: textOrEmpty(raw.description),
      bodyKind: body.kind === "lanes" ? "lanes" : "sequence",
      lanes: groupLanes(container).map((lane) => lane.lane_id).join(", "),
      concurrencyLimit: typeof body.concurrency_limit === "number" || typeof body.concurrency_limit === "string" ? String(body.concurrency_limit) : "",
      parentGroupId: nodeKind === "for_each" ? textOrEmpty(raw.group_id) : "",
      over: textOrEmpty(raw.over), itemName: textOrEmpty(raw.item_name) || "item", indexName: textOrEmpty(raw.index_name),
    });
    setDrawerMode("group"); setDrawerTab("settings"); setAutomationScopeKind("group"); setReactionFormVisible(false);
  }, []);

  const openNewStepDrawer = useCallback(
    async (
      context: Partial<StepCreateContext> = {},
      blockKeyOverride = "",
      defaults: Record<string, any> = {},
      confirmed = false,
    ) => {
      if (!confirmed && ctx.prepareSelection && !(await ctx.prepareSelection("new-step", "", parseHookNodeId(context.forEachId || context.groupId || "")?.hookId))) return;
      if (!confirmed && !ctx.prepareSelection && parameterSwitchNeedsConfirmation && !(await confirmDiscardParameters())) return;
      ctx.httpIntent?.beginEditorSession();
      const nextStepKind = textOrEmpty(defaults.stepKind) || "agent";
      setStepCreateContext({
        groupId: textOrEmpty(context.groupId),
        forEachId: textOrEmpty(context.forEachId),
        insert: context.insert || null,
        laneId: textOrEmpty(context.laneId),
      });
      setSelectedGroupId(textOrEmpty(context.forEachId) || textOrEmpty(context.groupId));
      setSelectedStepId("");
      setStepDisplayName(textOrEmpty(defaults.displayName) || t("react.workflows.new_step_default_name", "New step"));
      setStepKindDraft(nextStepKind);
      setStepToolCategoryDraft("");
      const overrideDraft = defaults.override ?? defaultOverrideForStepKind(nextStepKind);
      setStepOverrideDraft(nextStepKind === "agent" && blockKeyOverride
        ? jsonText(applyAgentBlockIntent(objectOrEmpty(JSON.parse(overrideDraft || "{}")), blockKeyOverride)) : overrideDraft);
      setStepAgentId(textOrEmpty(defaults.agentId));
      setStepOptional(false);
      setStepBlockKey(blockKeyOverride);
      setDrawerMode("step");
      setDrawerTab("smart-setup");
      setAutomationScopeKind("step");
      setReactionFormVisible(false);
    },
    [confirmDiscardParameters, parameterSwitchNeedsConfirmation, t, ctx.prepareSelection],
  );

  const openGroupDrawer = useCallback(
    async (groupId: string, defaultTab = "settings", confirmed = false) => {
      if (!confirmed && ctx.prepareSelection && !(await ctx.prepareSelection("group", groupId))) return;
      if (!confirmed && !ctx.prepareSelection && (groupId !== selectedGroupId || selectedStepId) && parameterSwitchNeedsConfirmation && !(await confirmDiscardParameters())) return;
      const row = groupRowFor(groupId);
      setGroupCreateMode(false);
      setStepCreateContext(null);
      setSelectedGroupId(groupId);
      setSelectedStepId("");
      if (row && groupId !== groupDraft.groupId) {
        setGroupDraft(groupDraftFromRow(row));
      }
      setDrawerMode("group");
      setDrawerTab(defaultTab);
      setAutomationScopeKind("group");
      setReactionFormVisible(false);
    },
    [confirmDiscardParameters, parameterSwitchNeedsConfirmation, groupDraft.groupId, groupRows, selectedGroupId, selectedStepId, ctx.prepareSelection, ctx.resolveGroupRow],
  );

  const openNewGroupDrawer = useCallback(async (defaults: GroupDraftDefaults = {}, confirmed = false) => {
    if (!confirmed && ctx.prepareSelection && !(await ctx.prepareSelection("new-group", "", defaults.hookId))) return;
    if (!confirmed && !ctx.prepareSelection && parameterSwitchNeedsConfirmation && !(await confirmDiscardParameters())) return;
    setGroupCreateMode(true);
    setStepCreateContext(null);
    setSelectedGroupId("");
    setSelectedStepId("");
    setGroupDraft(emptyGroupDraft(defaults));
    setDrawerMode("group");
    setDrawerTab("settings");
    setAutomationScopeKind("group");
    setReactionFormVisible(false);
  }, [confirmDiscardParameters, parameterSwitchNeedsConfirmation, ctx.prepareSelection]);

  const openWorkflowTestRunDrawer = useCallback(async (isCurrent?: () => boolean) => {
    const stillCurrent = typeof isCurrent === "function" ? isCurrent : undefined;
    if (stillCurrent && !stillCurrent()) return false;
    if (hasInvalidStepInput && !(await confirmDiscardInvalidInput())) return;
    if (stillCurrent && !stillCurrent()) return false;
    setWorkflowTestRunLaunchOpen(true);
    return true;
  }, [confirmDiscardInvalidInput, hasInvalidStepInput, setWorkflowTestRunLaunchOpen]);

  const openWorkflowTestRunResultsDrawer = useCallback(async (isCurrent?: () => boolean) => {
    if (isCurrent && !isCurrent()) return false;
    if (hasInvalidStepInput && !(await confirmDiscardInvalidInput())) return false;
    if (isCurrent && !isCurrent()) return false;
    setDrawerMode("workflow-test-run");
    setDrawerTab("test-run");
    setAutomationScopeKind("workflow");
    setReactionFormVisible(false);
    return true;
  }, [confirmDiscardInvalidInput, hasInvalidStepInput]);

  function openValidationIssueGroup(group: any) {
    if (!group.targetGroup) {
      return;
    }
    ignoreAsyncResult(openGroupDrawer(group.targetGroup.groupId, "settings"));
    setSaveNotice(
      t("react.workflows.opened_affected_group_value0", "Opened affected group: {value0}.", {
        value0: group.targetGroup.displayName || group.targetGroup.groupId,
      }),
    );
  }

  function openValidationIssueStep(group: any) {
    if (!group.targetStep) {
      return;
    }
    ignoreAsyncResult(openStepDrawer(group.targetStep.runtimeId, group.targetStep.blockKey));
    setSaveNotice(
      t("react.workflows.opened_affected_step_value0", "Opened affected step: {value0}.", {
        value0: group.targetStep.primaryLabel || group.targetStep.runtimeId,
      }),
    );
  }

  function openValidationIssueSourceStep(group: any) {
    const sourceStep = group.sourceStepId ? stepRowsById.get(group.sourceStepId) : null;
    if (!sourceStep) {
      return;
    }
    ignoreAsyncResult(openStepDrawer(sourceStep.runtimeId, sourceStep.blockKey));
    setSaveNotice(
      t("react.workflows.opened_source_step_value0", "Opened source step: {value0}.", {
        value0: sourceStep.primaryLabel || sourceStep.runtimeId,
      }),
    );
  }

  const closeDrawer = useCallback(async () => {
    if (hasInvalidStepInput && !(await confirmDiscardInvalidInput())) return;
    setDrawerMode("");
    setDrawerTab("smart-setup");
    setSelectedCanvasIssue(null);
  }, [confirmDiscardInvalidInput, hasInvalidStepInput, setSelectedCanvasIssue]);

  const changeDrawerTab = useCallback(async (tab: string) => {
    if (tab !== drawerTab && hasInvalidStepInput && !(await confirmDiscardInvalidInput())) return;
    setDrawerTab(tab);
  }, [confirmDiscardInvalidInput, drawerTab, hasInvalidStepInput]);

  const openWorkflowSettingsDrawer = useCallback(async (tab = "smart-setup") => {
    if (hasInvalidStepInput && !(await confirmDiscardInvalidInput())) return;
    setDrawerMode("workflow-settings");
    setDrawerTab(tab);
    setAutomationScopeKind("workflow");
    setReactionFormVisible(false);
  }, [confirmDiscardInvalidInput, hasInvalidStepInput]);

  const openWorkflowAutomationDrawer = useCallback(() => openWorkflowSettingsDrawer("automation"), [openWorkflowSettingsDrawer]);

  const openWorkflowInterfaceDrawer = useCallback(async (tab = "inputs") => {
    if (protectedWorkflowStructureRedacted || (hasInvalidStepInput && !(await confirmDiscardInvalidInput()))) return;
    setSelectedCanvasIssue(null);
    setDrawerMode("workflow-interface");
    setDrawerTab(["inputs", "outputs"].includes(tab) ? tab : "inputs");
    setAutomationScopeKind("workflow");
    setReactionFormVisible(false);
  }, [protectedWorkflowStructureRedacted, confirmDiscardInvalidInput, hasInvalidStepInput, setSelectedCanvasIssue]);

  const toggleBuilderAdvancedMode = useCallback(async () => {
    if (hasInvalidStepInput && !(await confirmDiscardInvalidInput())) return;
    const nextAdvancedMode = !advancedMode;
    setAdvancedMode(nextAdvancedMode);
    if (nextAdvancedMode) {
      setDrawerMode("workflow-settings");
      setDrawerTab("advanced");
      setAutomationScopeKind("workflow");
      setReactionFormVisible(false);
      return;
    }
    setDrawerTab((current: string) =>
      current === "advanced"
        ? "smart-setup"
        : current,
    );
  }, [advancedMode, confirmDiscardInvalidInput, hasInvalidStepInput]);

  const openGroupAutomationDrawer = useCallback(
    (groupId: string) => {
      ignoreAsyncResult(openGroupDrawer(groupId, "automation"));
    },
    [openGroupDrawer],
  );

  const openStepAutomationDrawer = useCallback(
    async (stepId: string) => {
      if (stepId === selectedStepId && hasInvalidStepInput && !(await confirmDiscardInvalidInput())) return;
      if (!(await openStepDrawer(stepId))) return;
      setDrawerTab("automation");
      setAutomationScopeKind("step");
    },
    [confirmDiscardInvalidInput, hasInvalidStepInput, openStepDrawer, selectedStepId],
  );

  // The group editor follows its group: it loads once the definition arrives and
  // again when the group itself changes, never on unrelated re-renders.
  const selectedGroupRow = groupRowFor(selectedGroupId);
  const selectedGroupSignature = selectedGroupRow
    ? `${selectedGroupRow.groupId}:${JSON.stringify(selectedGroupRow.raw)}`
    : "";
  useEffect(() => {
    if (drawerMode !== "group" || groupCreateMode || !selectedGroupRow ||
      (groupFormDirty && savingWorkflowSurface !== "group")) {
      return;
    }
    setGroupDraft(groupDraftFromRow(selectedGroupRow));
  }, [groupCreateMode, selectedGroupSignature]);

  useEffect(() => {
    if (
      !["workflow-settings", "workflow-interface"].includes(drawerMode) ||
      !selectedIssueWorkflowContractSection
    ) {
      return;
    }
    setDrawerMode("workflow-interface");
    setDrawerTab(selectedIssueWorkflowContractSection);
    globalThis.requestAnimationFrame(() => {
      document
        .getElementById(
          `workflow-contract-section-${selectedIssueWorkflowContractSection}`,
        )
        ?.scrollIntoView({ block: "start", behavior: "smooth" });
    });
  }, [drawerMode, selectedIssueWorkflowContractSection]);

  useEffect(() => {
    const request = stepDrawerRequest.current?.key === drawerRequestKey
      ? stepDrawerRequest.current : { key: drawerRequestKey, consumed: false };
    stepDrawerRequest.current = request;
    const drawerParam = firstStringFromUnknownValue([searchParams.get("drawer"), ""]).trim();
    if (!drawerParam) {
      return;
    }
    const normalizedDrawer = drawerParam.toLowerCase();
    const groupIdParam = firstStringFromUnknownValue([searchParams.get("group_id"), ""]).trim();
    const stepIdParam = firstStringFromUnknownValue([searchParams.get("step_id"), ""]).trim();
    const groupTabParam = firstStringFromUnknownValue([searchParams.get("group_tab"), ""]).trim();
    const stepTabRequest = searchParams.get("step_tab");
    const stepTabParam: StepEditorTab = stepTabRequest === "test-run" || stepTabRequest === "automation" ? stepTabRequest : "smart-setup";
    const nextParams = new URLSearchParams(searchParams);
    const clearDrawerParams = () => {
      nextParams.delete("drawer");
      nextParams.delete("group_id");
      nextParams.delete("step_id");
      nextParams.delete("step_tab");
      nextParams.delete("group_tab");
      setSearchParams(nextParams, { replace: true });
    };
    // Never auto-open an authoring drawer for non-admins via a `?drawer=` deep link;
    // drop the param so it cannot bypass the hidden authoring buttons.
    if (AUTHORING_DRAWER_PARAMS.has(normalizedDrawer) && !canAuthorWorkflow) {
      clearDrawerParams();
      return;
    }
    if (
      normalizedDrawer === "workflow-settings" ||
      normalizedDrawer === "settings" ||
      normalizedDrawer === "schedules"
    ) {
      setDrawerMode("workflow-settings");
      setDrawerTab(normalizedDrawer === "schedules" ? "schedules" : "smart-setup");
      setAutomationScopeKind("workflow");
      setReactionFormVisible(false);
      clearDrawerParams();
      return;
    }
    if (normalizedDrawer === "interface" || normalizedDrawer === "workflow-interface") {
      ignoreAsyncResult(openWorkflowInterfaceDrawer());
      clearDrawerParams();
      return;
    }
    if (
      normalizedDrawer === "workflow-test-run" ||
      normalizedDrawer === "test-run"
    ) {
      ignoreAsyncResult(openWorkflowTestRunDrawer());
      clearDrawerParams();
      return;
    }
    if (
      normalizedDrawer === "automation" ||
      normalizedDrawer === "reactions"
    ) {
      setDrawerMode("workflow-settings");
      setDrawerTab("automation");
      setAutomationScopeKind("workflow");
      setReactionFormVisible(false);
      clearDrawerParams();
      return;
    }
    if (normalizedDrawer === "group" && groupIdParam) {
      if (!groupRowFor(groupIdParam)) {
        return;
      }
      ignoreAsyncResult(openGroupDrawer(groupIdParam, groupTabParam || "settings"));
      clearDrawerParams();
      return;
    }
    if (normalizedDrawer === "step" && stepIdParam) {
      if (!stepRowsById.has(stepIdParam)) {
        return;
      }
      // One location owns one routed request, including its awaited guards.
      // Callback/row readiness commits must not replay its initial Play tab.
      if (request.consumed) return;
      request.consumed = true;
      ignoreAsyncResult(openStepDrawer(stepIdParam, "", false, stepTabParam));
      clearDrawerParams();
      return;
    }
    clearDrawerParams();
  }, [
    canAuthorWorkflow,
    drawerRequestKey,
    groupRows,
    openGroupDrawer,
    openStepDrawer,
    openWorkflowTestRunDrawer,
    searchParams,
    setSearchParams,
    stepRowsById,
  ]);

  return {
    closeDrawer,
    changeDrawerTab,
    openWorkflowSettingsDrawer,
    openWorkflowInterfaceDrawer,
    drawerTitle,
    loadStepDrafts,
    openGroupAutomationDrawer,
    openGroupDrawer,
    openNewGroupDrawer,
    openNewStepDrawer,
    openCreatedStep,
    openCreatedContainer,
    openStepAutomationDrawer,
    openStepDrawer,
    openValidationIssueGroup,
    openValidationIssueSourceStep,
    openValidationIssueStep,
    openWorkflowAutomationDrawer,
    openWorkflowTestRunDrawer,
    openWorkflowTestRunResultsDrawer,
    toggleBuilderAdvancedMode,
  };
}
