import { agentParticipants } from "./detail-agent-step-fields";
import { httpIssueField } from "./detail-http-request-fields";
import type { HttpFieldFeedback } from "./detail-http-request-intent";
import { stepNumericFieldErrors } from "./detail-step-numeric-fields";
import { transformRequiredFieldErrors } from "./detail-transform-fields";
import { buildNewStepOperations, stepConfigPatch } from "./detail-local-step-creation";
export { buildNewStepOperations, buildNewContainerOperations, stepConfigPatch } from "./detail-local-step-creation";
import { workflowHookPlacementInvalid } from "@/pages/workflows/shared/workflow-hook-placement";
import { workflowIOIssueMessage } from "@/pages/workflows/detail/detail-webhook";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useRef, useState } from "react";

import {
  ifConditionFromDraft,
  jsonObjectFromDraft,
  objectOrEmpty,
  textOrEmpty,
  type IfConditionDraft,
} from "@/pages/workflows/detail/detail-helpers";
import {
  applyWorkflowChanges,
  isWorkflowRevisionConflict,
  outcomeHandleId,
  targetLabel,
  workflowChangeErrorIssues,
  type WorkflowChangeOperation,
} from "@/pages/workflows/builder/api";
import type { WorkflowGraphEdge } from "@/pages/workflows/shared/api";
import {
  edgesDroppedByKindChange,
  switchCaseIds,
  stepPlacement,
  stepAfterKindChange,
  type WorkflowDefinitionModel,
} from "@/pages/workflows/builder/change-set";
import type { WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import { sourcePlacement } from "@/pages/workflows/detail/detail-canvas-handlers";
import { announceCanvasToast, watchGraphIntent } from "@/pages/workflows/builder/canvas-toast-feed";

export type StepFormSaveState = "idle" | "saving" | "saved" | "saved_with_issues" | "error";

/** A kind change waiting for the author's answer in the editor's footer. */
export type PendingKindChange = {
  stepId: string;
  /** The connections the new kind cannot keep, as "source · outcome → target". */
  dropped: string[];
  resolve: (confirmed: boolean) => void;
};

function stepSaveBlockingNotice(ctx: Record<string, any>, t: I18nContextValue["t"] = fallbackContext.t): string {
  if (ctx.stepKindDraft === "http_request" && ctx.httpIntent?.hasRaw()) return ctx.httpIntent.firstRaw() || t("react.workflows.invalid_override_json", "Invalid override JSON.");
  const inputError = Object.values<string>(ctx.stepInputErrors || {}).find(Boolean);
  if (inputError) return inputError;
  if (ctx.stepKindDraft !== "connector") return "";
  if (ctx.connectorConnectionNeedsProjectLink) {
    return t("react.workflows.link_this_workflow_to_a_project_before_choosing_a_connection_for_this_action", "Link this workflow to a project before choosing a connection for this action.");
  }
  if (ctx.missingConnectorConnection && !ctx.staleConnectorOperationId) {
    return t("react.workflows.choose_a_connection_for_this_action_before_saving", "Choose a connection for this action before saving.");
  }
  if (
    ctx.staleConnectorOperationId ||
    ctx.staleConnectorConnectionId ||
    ctx.missingConnectorConnection
  ) {
    return t("react.workflows.choose_an_action_and_connection_available_for_this_installed_extension_release_before_saving", "Choose an action and connection available for this installed extension release before saving.");
  }
  if (ctx.extensionConnectionReadinessBlocked) {
    return (
      ctx.extensionConnectionReadinessMessage ||
      t("react.workflows.resolve_the_selected_project_connection_before_saving_this_step", "Resolve the selected project connection before saving this step.")
    );
  }
  if (!ctx.connectorRequiredInputsMissing) return "";
  const missingNames = Array.isArray(ctx.connectorMissingRequiredInputNames)
    ? ctx.connectorMissingRequiredInputNames
    : [];
  return t("react.workflows.display_required_action_inputs_save", "Complete the required action inputs before saving: {names}.", { names: missingNames.join(", ") });
}

/**
 * The step's own parameters, as the fields a step config patch may carry.
 * Identity, kind and membership are never part of it: those are structure.
 */

function withoutUndefined(value: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => item !== undefined));
}

function samePortSet(left: string[], right: string[]) {
  const a = new Set(left);
  const b = new Set(right);
  return a.size === b.size && [...a].every((item) => b.has(item));
}

/** A switch's case set is its set of ports; changing it is a structural change. */
export function stepPortsChange(
  stepKind: string,
  storedOverride: Record<string, unknown>,
  nextOverride: Record<string, unknown>,
): boolean {
  if (stepKind !== "switch") {
    return false;
  }
  return !samePortSet(
    switchCaseIds(objectOrEmpty(storedOverride.switch)),
    switchCaseIds(objectOrEmpty(nextOverride.switch)),
  );
}

export function useWorkflowStepActions(ctx: Record<string, any>) {
  const { t } = useI18n();
  const {
    changeSet,
    createAsEntry,
    refetchWorkflowSurfaces,
    deletionGuard,
    selectedStep,
    serverRevision,
    setCreateAsEntry,
    setSaveError,
    setSaveNotice,
    setSavingWorkflowSurface,
    setSelectedStepId,
    setStepCreateContext,
    setStepOverrideDraft,
    stepCreateContext,
    stepOverrideDraft,
    workflowId,
  } = ctx as Record<string, any> & { changeSet: WorkflowChangeSetController };
  const [stepFormSaveState, setStepFormSaveState] = useState<StepFormSaveState>("idle");
  const [httpFieldFeedback, setHttpFieldFeedback] = useState<HttpFieldFeedback[]>([]);
  // A kind change asks first, inline in the editor's footer, naming the connections it removes.
  const [pendingKindChange, setPendingKindChange] = useState<PendingKindChange | null>(null);
  const stepSaveFlight = useRef(0);

  const pendingKindChangeRef = useRef<PendingKindChange | null>(null);
  // Every save that lands — sent, or kept with the unapplied structure — bumps this for ✓ Saved / ✓ Updated.
  const [stepSaveSerial, setStepSaveSerial] = useState({ count: 0, staged: false });
  const markStepSaved = (staged: boolean) => setStepSaveSerial((current) => ({ count: current.count + 1, staged }));

  // Another step, or a closed editor, answers a waiting kind change with Cancel.
  const selectedStepRuntimeId = textOrEmpty(selectedStep?.runtimeId);
  useEffect(() => () => pendingKindChangeRef.current?.resolve(false), [selectedStepRuntimeId]);

  function confirmKindChangeInline(stepId: string, dropped: string[]): Promise<boolean> {
    pendingKindChangeRef.current?.resolve(false);
    return new Promise((resolve) => {
      const pending: PendingKindChange = {
        stepId,
        dropped,
        resolve: (confirmed: boolean) => {
          if (pendingKindChangeRef.current === pending) {
            pendingKindChangeRef.current = null;
            setPendingKindChange(null);
          }
          resolve(confirmed);
        },
      };
      pendingKindChangeRef.current = pending;
      setPendingKindChange(pending);
    });
  }

  function edgeDetail(edge: WorkflowGraphEdge): string {
    const target = targetLabel(edge.target, t);
    return `${edge.source.id} · ${outcomeHandleId(edge.outcome)} → ${target}`;
  }

  function hasStagedKindChange(stepId: string): boolean {
    const operations = (changeSet.operations || []) as WorkflowChangeOperation[];
    return operations.some((operation) => operation.op === "change_step_kind" && operation.step_id === stepId);
  }

  /**
   * A saved step's kind is structure: its ports change, so the change joins the
   * local set as change_step_kind, and edges on outcomes the new kind lacks go.
   */
  async function stageKindChange(stepId: string, payload: Record<string, unknown> & { step_kind: string }) {
    const agentToken = ctx.stepKindDraft === "agent" || selectedStep?.stepKind === "agent" ? ctx.agentDrafts?.capture() : null;
    const preview = changeSet.preview as WorkflowDefinitionModel;
    const current = preview.steps.find((step) => step.step_id === stepId);
    if (!current) return;
    const dropped = edgesDroppedByKindChange(preview, stepId, payload).map(edgeDetail);
    const confirmed = await confirmKindChangeInline(stepId, dropped);
    if (!confirmed || (agentToken && !ctx.agentDrafts.isUnedited(agentToken))) {
      return;
    }
    const message = t(
      "react.workflows.change_step_kind_staged",
      "Kind change added to the unapplied structure. Connect the new outcomes, then apply.",
    );
    if (!changeSet.stageGraphIntent([{ op: "change_step_kind", step_id: stepId, step: payload }], message)) return;
    if (payload.step_kind === "agent" && agentToken) ctx.agentDrafts.acknowledgeStaged(agentToken, stepAfterKindChange(current, payload));
    setSaveNotice(message);
    markStepSaved(true);
  }

  function stageNewStep(payload: Record<string, unknown>) {
    const preview = changeSet.preview as WorkflowDefinitionModel;
    const result = buildNewStepOperations({
      preview,
      choice: { catalogKey: `editor:${ctx.stepKindDraft || "agent"}`, stepKind: ctx.stepKindDraft || "agent", displayName: textOrEmpty(ctx.stepDisplayName), override: objectOrEmpty(payload.override) },
      context: stepCreateContext || {},
      configPatch: payload,
      createAsEntry,
      t,
    });
    const message = t("react.workflows.step_added_to_changes", "Step added to the unapplied structure. Connect its outcomes, then apply.");
    const token = changeSet.stageGraphIntent(result.operations, message);
    if (!token) return;
    setStepCreateContext(null);
    setCreateAsEntry(false);
    setSelectedStepId(result.step.step_id);
    ctx.focusCanvasNode?.(result.step.step_id, { place: !stepCreateContext?.insert && !result.placement.groupId && !result.placement.forEachId,
      creation: { target: { kind: "step", id: result.step.step_id }, token, ownerWorkflowId: changeSet.workflowId,
        viewKey: changeSet.placementViewKey, viewEpoch: changeSet.placementViewEpoch } });
    setSaveNotice(message);
  }

  async function saveExistingStep(stepId: string, patch: Record<string, unknown>, override: Record<string, unknown>) {
    const storedOverride = objectOrEmpty(objectOrEmpty(selectedStep?.raw).override);
    if (stepPortsChange(ctx.stepKindDraft, storedOverride, override)) {
      changeSet.stage(
        { op: "update_step_config", step_id: stepId, patch },
        t("react.workflows.step_ports_staged", "The step's outcomes changed, so this save is part of the unapplied structure. Connect the new outcomes, then apply."),
      );
      setSaveNotice(t("react.workflows.step_ports_staged", "The step's outcomes changed, so this save is part of the unapplied structure. Connect the new outcomes, then apply."));
      markStepSaved(true);
      return;
    }
    if (!serverRevision) {
      setSaveError(t("react.workflows.change_set_revision_unknown", "The workflow revision is unknown. Reload the workflow."));
      return;
    }
    setSaveNotice("");
    setSavingWorkflowSurface("step");
    setStepFormSaveState("saving");
    const flight = ++stepSaveFlight.current;
    const httpToken = ctx.stepKindDraft === "http_request" ? ctx.httpIntent?.beginRequest() : undefined;
    const agentToken = ctx.stepKindDraft === "agent" ? ctx.agentDrafts?.beginRequest() : undefined;
    try {
      const result = await applyWorkflowChanges(workflowId, {
        expected_revision: serverRevision,
        operations: [{ op: "update_step_config", step_id: stepId, patch }],
      });
      if ((httpToken && !ctx.httpIntent.isCurrent(httpToken)) || (agentToken && !ctx.agentDrafts.isCurrent(agentToken))) return;
      const acknowledgedHttp = Boolean(httpToken && result?.applied === true && Number.isSafeInteger(result.revision) && result.revision > 0);
      if (acknowledgedHttp) ctx.httpIntent.acknowledgeRequest(httpToken, { workflowId, revision: result.revision });
      if (httpToken) {
        const feedback: HttpFieldFeedback[] = (result?.issues || []).flatMap((issue) => {
          const field = httpIssueField(String(issue.path || ""), stepId);
          return field ? [{ field, message: workflowIOIssueMessage(issue, t), token: httpToken, focus: false }] : [];
        });
        setHttpFieldFeedback((previous) => ctx.httpIntent.isCurrent(httpToken)
          ? feedback.filter((entry) => ctx.httpIntent.isCurrent(entry.token, entry.field)) : previous);
      }
      if (result?.revision) {
        changeSet.rebase(result.revision, serverRevision);
        // The saved parameters are one action of the history: Undo takes them back locally.
        changeSet.recordSavedChange?.([{ op: "update_step_config", step_id: stepId, patch }], result.revision);
      }
      const readback = objectOrEmpty(await refetchWorkflowSurfaces());
      if (acknowledgedHttp && readback.workflow_id === workflowId && readback.revision === result.revision) {
        const rows = (Array.isArray(readback.steps) ? readback.steps : [])
          .map(objectOrEmpty).filter((step) => textOrEmpty(step.step_id) === stepId);
        if (rows.length === 1) ctx.httpIntent.recordAcknowledgedReadback(httpToken, {
          workflowId, revision: result.revision, signature: `${stepId}:${JSON.stringify(rows[0])}`,
        });
      }
      // Query notifications can commit after finally retires the pending token.
      // Accept only this unchanged owner's exact successful readback before that cleanup.
      if (agentToken && result?.applied === true && Number.isSafeInteger(result.revision) && result.revision > 0 &&
        readback.workflow_id === workflowId && readback.revision === result.revision && ctx.agentDrafts.isUnedited(agentToken)) {
        const rows = (Array.isArray(readback.steps) ? readback.steps : [])
          .map(objectOrEmpty).filter((step) => textOrEmpty(step.step_id) === stepId);
        if (rows.length === 1) ctx.agentDrafts.reconcile(rows[0]);
      }
      if ((httpToken && !ctx.httpIntent.isUnedited(httpToken)) || (agentToken && !ctx.agentDrafts.isUnedited(agentToken))) return;
      const savedWithIssues = Boolean(httpToken && (result?.valid === false || (result?.issues || []).length));
      setStepFormSaveState(savedWithIssues ? "saved_with_issues" : "saved");
      markStepSaved(false);
      setSaveNotice(savedWithIssues
        ? t("react.workflows.step_parameters_saved_with_issues", "Step parameters saved. Workflow validation issues remain.")
        : t("react.workflows.step_parameters_saved", "Step parameters saved."));
    } catch (error) {
      if ((httpToken && !ctx.httpIntent.isCurrent(httpToken)) || (agentToken && !ctx.agentDrafts.isUnedited(agentToken))) return;
      const receivedIssues = workflowChangeErrorIssues(error);
      const issues = httpToken ? receivedIssues.filter((issue) => {
        const field = httpIssueField(String(issue.path || ""), stepId);
        return field ? ctx.httpIntent.isCurrent(httpToken, field) : ctx.httpIntent.isUnedited(httpToken);
      }) : receivedIssues;
      if (httpToken && receivedIssues.length && !issues.length) return;
      if (httpToken && !receivedIssues.length && !isWorkflowRevisionConflict(error) && !ctx.httpIntent.isUnedited(httpToken)) return;
      setStepFormSaveState("error");
      if (isWorkflowRevisionConflict(error)) {
        changeSet.reportConflict();
        setSaveError(
          t(
            "react.workflows.step_save_conflict",
            "The workflow changed after it was loaded. Nothing was saved; reload the workflow and save again.",
          ),
        );
        return;
      }
      if (httpToken) {
        const feedback: HttpFieldFeedback[] = issues.flatMap((issue) => {
          const field = httpIssueField(String(issue.path || ""), stepId);
          return field ? [{ field, message: workflowIOIssueMessage(issue, t), token: httpToken }] : [];
        });
        setHttpFieldFeedback((previous) => ctx.httpIntent.isCurrent(httpToken)
          ? feedback.filter((entry) => ctx.httpIntent.isCurrent(entry.token, entry.field)) : previous);
      }
      const issueText = issues.map((issue) => workflowIOIssueMessage(issue, t)).filter(Boolean).join(" ");
      setSaveError(
        issueText ||
          (error instanceof Error ? error.message : t("react.workflows.failed_to_save_step", "Failed to save step.")),
      );
    } finally {
      if (httpToken || agentToken) {
        if (httpToken) ctx.httpIntent.endRequest(httpToken);
        if (agentToken) ctx.agentDrafts.endRequest(agentToken);
        // Editing intent may retire while this flight still owns Step busy.
        // Read the live flight in the updater so queued cleanup cannot release
        // a newer Step save, or another surface's current busy state.
        setSavingWorkflowSurface((current: string) =>
          stepSaveFlight.current === flight && current === "step" ? "" : current);
      } else {
        setSavingWorkflowSurface("");
      }
    }
  }

  async function handleSaveStep(overrideInput?: Record<string, unknown>) {
    if (!textOrEmpty(workflowId)) {
      return;
    }
    const blockingNotice = stepSaveBlockingNotice(ctx, t);
    if (blockingNotice) {
      setSaveError(blockingNotice);
      return;
    }
    let override: Record<string, unknown>;
    try {
      override = overrideInput || jsonObjectFromDraft(stepOverrideDraft, t);
    } catch (error) {
      setSaveError(
        error instanceof Error ? error.message : t("react.workflows.invalid_override_json", "Invalid override JSON."),
      );
      return;
    }
    const numericErrors = stepNumericFieldErrors(ctx.stepKindDraft, objectOrEmpty(override[ctx.stepKindDraft]), t);
    const numericError = Object.values(numericErrors)[0];
    if (numericError) {
      setSaveError(numericError);
      return;
    }
    if (ctx.stepKindDraft === "transform") {
      const requiredErrors = transformRequiredFieldErrors(objectOrEmpty(override.transform), t);
      const requiredError = Object.values(requiredErrors)[0];
      if (requiredError) {
        setSaveError(requiredError);
        return;
      }
    }
    const owner = selectedStep
      ? stepPlacement(selectedStep.raw)
      : stepCreateContext?.insert
        ? sourcePlacement(changeSet.preview, stepCreateContext.insert.source)
        : stepCreateContext || {};
    if (workflowHookPlacementInvalid(ctx.stepKindDraft, override, owner)) {
      setSaveError(t("react.workflows.hook_root_only", "Hooks belong in the workflow root, before or after a Group or For each."));
      return;
    }
    if (overrideInput) {
      setStepOverrideDraft(JSON.stringify(overrideInput, null, 2));
    }
    const agentToken = ctx.stepKindDraft === "agent" ? ctx.agentDrafts?.capture() : null;
    let agentConfigPatch: Record<string, unknown> | undefined;
    if (ctx.stepKindDraft === "agent" && ctx.agentDrafts) {
      try { agentConfigPatch = ctx.agentDrafts.snapshot(ctx.stepAgentId || "", override, agentParticipants(ctx.agentWorkflowRecord)); }
      catch {
        ctx.agentDrafts.focusInvalid();
        setSaveError(t("react.workflows.agent_draft_invalid", "Repair the Agent mapping draft or changed participant binding before saving."));
        return;
      }
    }
    const patch = withoutUndefined(stepConfigPatch({ ...ctx, agentConfigPatch }, override));
    if (!selectedStep) {
      if (!stepCreateContext) {
        setSaveError(t("react.workflows.step_no_longer_available", "This step is no longer available. Close the editor and select an existing step, or explicitly add a new step."));
        return;
      }
      stageNewStep(patch);
      return;
    }
    const stepId = textOrEmpty(selectedStep.runtimeId);
    if (selectedStep.local) {
      const accepted = changeSet.updateStagedStep(stepId, { ...patch, step_kind: ctx.stepKindDraft || "agent" });
      if (!accepted) return;
      if (agentToken) ctx.agentDrafts.acknowledgeStaged(agentToken, accepted);
      const message = t("react.workflows.change_set_step_updated", "The new step's parameters are kept with the unapplied structure.");
      if (ctx.setStagedStepSaveNotice) {
        const noticeOwner = changeSet.captureStagedStepNoticeOwner?.(stepId);
        if (noticeOwner) ctx.setStagedStepSaveNotice(message, noticeOwner);
      } else setSaveNotice(message);
      markStepSaved(true);
      return;
    }
    const nextKind = textOrEmpty(ctx.stepKindDraft) || "agent";
    if (hasStagedKindChange(stepId)) {
      // The kind change already in the set carries this step's whole new configuration.
      const accepted = changeSet.updateStagedStep(stepId, { ...patch, step_kind: nextKind });
      if (!accepted) return;
      if (agentToken) ctx.agentDrafts.acknowledgeStaged(agentToken, accepted);
      setSaveNotice(
        t("react.workflows.change_step_kind_updated", "The kind change in the unapplied structure now carries these settings."),
      );
      markStepSaved(true);
      return;
    }
    if (nextKind !== (textOrEmpty(selectedStep.stepKind) || "agent")) {
      await stageKindChange(stepId, { ...patch, step_kind: nextKind });
      return;
    }
    if (changeSet.touchesStep(stepId)) {
      setSaveError(
        t(
          "react.workflows.step_save_blocked_by_structure",
          "This step is part of the unapplied structure changes. Apply or discard them before saving its parameters.",
        ),
      );
      return;
    }
    await saveExistingStep(stepId, patch, override);
  }

  async function handleApplyIfConditionFix(ifDraft: IfConditionDraft) {
    let override: Record<string, unknown>;
    try {
      override = jsonObjectFromDraft(stepOverrideDraft, t);
    } catch (error) {
      setSaveError(
        error instanceof Error ? error.message : t("react.workflows.invalid_override_json", "Invalid override JSON."),
      );
      return;
    }
    const currentIfConfig = objectOrEmpty(override.if);
    await handleSaveStep({
      ...override,
      if: {
        ...currentIfConfig,
        condition: ifConditionFromDraft(ifDraft),
      },
    });
  }

  async function handleDeleteSelectedStep() {
    const stepId = textOrEmpty(selectedStep?.runtimeId);
    if (!stepId) {
      return;
    }
    // Delete acts at once; the canvas toast names what went and offers Undo (Components › Step actions).
    const name = textOrEmpty(selectedStep?.primaryLabel) || stepId;
    const stage = () => {
      const undo = watchGraphIntent(changeSet);
      changeSet.stageGraphIntent(
        [{ op: "delete_step", step_id: stepId }],
        t("react.workflows.step_delete_staged", "Step deletion added to the unapplied structure."),
      );
      setSelectedStepId("");
      setSaveNotice(t("react.workflows.step_delete_staged", "Step deletion added to the unapplied structure."));
      announceCanvasToast({ label: t("react.workflows.node_deleted", "{name} deleted.", { name }), tone: "success", action: undo(t("react.workflows.undo", "Undo")) });
    };
    if (selectedStep?.local) {
      if (!deletionGuard.currentTarget(stepId)) return;
      stage();
      return;
    }
    await deletionGuard.requestDelete({ kind: "step", id: stepId }, stage);
  }

  return {
    handleApplyIfConditionFix,
    handleDeleteSelectedStep,
    handleSaveStep,
    pendingKindChange,
    stepSaveSerial,
    setStepFormSaveState,
    stepFormSaveState,
    httpFieldFeedback,
  };
}
