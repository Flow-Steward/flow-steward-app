import { workflowHookPlacementInvalid } from "@/pages/workflows/shared/workflow-hook-placement";
import { useI18n } from "@/lib/i18n/useI18n";
/* eslint-disable @typescript-eslint/no-explicit-any, react-hooks/exhaustive-deps, react-hooks/preserve-manual-memoization */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type {
  WorkflowCanvasIssue,
  WorkflowGraphEdge,
  WorkflowNodeRef,
  WorkflowOutcome,
  WorkflowTargetRef,
} from "@/pages/workflows/shared/api";
import {
  applyWorkflowChanges,
  outcomeHandleId,
  sameNodeRef,
  sameOutcome,
  targetLabel,
  type WorkflowChangeOperation,
} from "@/pages/workflows/builder/api";
import {
  edgesTouchingStep,
  errorPolicyToken,
  groupPorts,
  moveStepOperations,
  placementOwnerFields,
  portEdge,
  ROOT_PLACEMENT,
  stepPlacement,
  stepPorts,
  type WorkflowDefinitionModel,
  type WorkflowPlacement,
} from "@/pages/workflows/builder/change-set";
import { announceCanvasNotice } from "./detail-canvas-notice";
import { deriveDeletionClosure, deletionIssuesUnderstood, deletionOperation, deletionScopeIssue, type DeletionScope, type DeletionTarget } from "@/pages/workflows/builder/canvas-delete-guard";
import { deriveDeletionBatch, sameDeleteBatchSelection, validDeleteBatchSelection, type DeleteBatchSelection, type DeletionBatch } from "@/pages/workflows/builder/canvas-delete-batch";
import { fetchWorkflowReactions } from "@/pages/workflows/shared/reactions-api";
import type { WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";

/** Use the existing definition transaction for both policy and edge; never
 * silently replace a saved retry/continue/recovery policy with a new route. */
export function errorRoutePolicyOperation(preview: WorkflowDefinitionModel, source: WorkflowNodeRef, onError: "route_to_step" | "fail", t: ReturnType<typeof useI18n>["t"]): WorkflowChangeOperation {
  const step = source.kind === "step" ? preview.steps.find((item) => item.step_id === source.id) : undefined;
  // A policy is inherited step → For each → Group; each container is found from its own owner.
  const forEach = preview.forEach.find((item) => item.for_each_id === (source.kind === "for_each" ? source.id : step?.for_each_id));
  const groupId = source.kind === "group" ? source.id : source.kind === "for_each" ? forEach?.group_id : step?.group_id || forEach?.group_id;
  const group = preview.groups.find((item) => item.group_id === groupId);
  let owner: { error_policy?: Record<string, unknown> | null } | undefined = step;
  if (source.kind === "group") owner = group;
  if (source.kind === "for_each") owner = forEach;
  if (!owner || (step && ["stop_success", "stop_fail"].includes(step.step_kind || ""))) throw new Error(t("react.workflows.error_route_terminal", "Only an executable nonterminal step or group can have an error route."));
  const inherited = source.kind === "group" ? "" : errorPolicyToken(source.kind === "step" ? forEach?.error_policy : null) || errorPolicyToken(group?.error_policy);
  const policy = errorPolicyToken(owner.error_policy) || inherited || "fail";
  if (!["fail", "route_to_step"].includes(policy)) throw new Error(t("react.workflows.error_route_conflict", "Cannot change this error route: the existing {policy} policy must be resolved explicitly first.", { policy }));
  const patch = { error_policy: { ...owner.error_policy, on_error: onError } };
  if (source.kind === "step") return { op: "update_step_config", step_id: source.id, patch };
  if (source.kind === "for_each") return { op: "update_for_each_config", for_each_id: source.id, patch };
  return { op: "update_group_config", group_id: source.id, patch };
}

function nodeDisplayName(preview: WorkflowDefinitionModel, ref: WorkflowNodeRef): string {
  if (ref.kind === "step") return preview.steps.find((item) => item.step_id === ref.id)?.display_name || ref.id;
  if (ref.kind === "group") return preview.groups.find((item) => item.group_id === ref.id)?.display_name || ref.id;
  return preview.forEach.find((item) => item.for_each_id === ref.id)?.display_name || ref.id;
}

/** A connection in words, by names: "Decode invoice · Next → Classify request". */
export function edgeNameLabel(preview: WorkflowDefinitionModel, edge: WorkflowGraphEdge, t: ReturnType<typeof useI18n>["t"]): string {
  const source = edge.source;
  let ports: Array<{ outcome: WorkflowOutcome; label: string }> = [];
  if (source.kind === "step") {
    const step = preview.steps.find((item) => item.step_id === source.id);
    ports = step ? stepPorts(step, t) : [];
  } else {
    const container = source.kind === "group"
      ? preview.groups.find((item) => item.group_id === source.id)
      : preview.forEach.find((item) => item.for_each_id === source.id);
    ports = container ? groupPorts(container, t) : [];
  }
  const outcome = ports.find((port) => sameOutcome(port.outcome, edge.outcome))?.label || outcomeHandleId(edge.outcome);
  const target = edge.target.kind === "step" || edge.target.kind === "group" || edge.target.kind === "for_each"
    ? nodeDisplayName(preview, edge.target)
    : targetLabel(edge.target, t);
  return `${nodeDisplayName(preview, source)} · ${outcome} → ${target}`;
}

/**
 * Where a step that follows *source* runs: the source step's own body or lane;
 * after a For each, the Group it sits in; after a Group, the root.
 */
export function sourcePlacement(preview: WorkflowDefinitionModel, source: WorkflowNodeRef): WorkflowPlacement {
  if (source.kind === "step") {
    return stepPlacement(preview.steps.find((item) => item.step_id === source.id));
  }
  if (source.kind === "for_each") {
    const forEach = preview.forEach.find((item) => item.for_each_id === source.id);
    return { ...ROOT_PLACEMENT, groupId: typeof forEach?.group_id === "string" ? forEach.group_id : "" };
  }
  return { ...ROOT_PLACEMENT };
}

export type DeleteGuardNotice = {
  state: "checking" | "protected" | "unknown";
  message: string;
  canOpenAutomation: boolean;
  scope?: DeletionScope;
};

export type DeleteOwner = {
  workflowId: string;
  projectId: string;
  ownerKey: string;
  selectionId: string;
  serverRevision: number | null;
  structureLocked: boolean;
};

export type DeleteGuardSnapshot = { changeSet: WorkflowChangeSetController; owner: DeleteOwner };

/** A canvas command supplies its own selected-node generation and live owner.
 * The editor uses the hook's existing drawer-bound snapshot instead. */
export type DeleteGuardCommandContext = {
  captured: DeleteGuardSnapshot;
  current: () => DeleteGuardSnapshot | null;
  openProtectedScope?: (scope: DeletionScope) => Promise<boolean>;
};

/** Batch UI readers provide one generation for the exact sorted selected-ID set.
 * `ownerScopeKey` distinguishes the author from each Hook instance. */
export type DeleteGuardBatchCommandContext = DeleteGuardCommandContext & {
  selection: DeleteBatchSelection;
  currentSelection: () => DeleteBatchSelection | null;
};

/** One owner-local adapter for the editor's destructive graph intent. */
export function useWorkflowDeletionGuard({ changeSet, openProtectedScope, owner }: {
  changeSet: WorkflowChangeSetController;
  openProtectedScope: (scope: DeletionScope) => Promise<boolean>;
  owner: DeleteOwner;
}) {
  const { t } = useI18n();
  const [notice, setNotice] = useState<DeleteGuardNotice | null>(null);
  const latest = useRef({ changeSet, owner });
  const sequence = useRef(0);
  const retryRef = useRef<(() => Promise<boolean>) | null>(null);
  const protectedOpenRef = useRef(openProtectedScope);
  // A command may own a Hook child's change set while the closed drawer still
  // owns the author. Its live proof cannot be inferred from this hook's props.
  const activeProofRef = useRef<(() => boolean) | null>(null);
  useLayoutEffect(() => { latest.current = { changeSet, owner }; });
  useEffect(() => {
    // The canvas command's current() reads the page's selection/child-owner
    // layout ref. Run after every committed layout effect has refreshed it.
    // The proof compares the captured owner, revision, lock, operation, preview,
    // base-revision and status fields. Controller wrapper identity is irrelevant.
    if (activeProofRef.current && !activeProofRef.current()) {
      sequence.current += 1; // An old fetch may not publish after this render.
      activeProofRef.current = null;
      retryRef.current = null;
      protectedOpenRef.current = openProtectedScope;
      setNotice(null);
    }
  });

  const currentTarget = useCallback((targetId: string) => {
    const current = latest.current;
    return current.owner.ownerKey === owner.ownerKey && current.owner.selectionId === targetId &&
      current.owner.projectId === owner.projectId && current.owner.workflowId === owner.workflowId &&
      current.owner.serverRevision === owner.serverRevision &&
      current.changeSet.operations === changeSet.operations && current.changeSet.preview === changeSet.preview &&
      current.changeSet.baseRevision === changeSet.baseRevision && current.changeSet.status === changeSet.status &&
      changeSet.status !== "applying" && changeSet.status !== "conflict" &&
      !owner.structureLocked && !current.owner.structureLocked;
  }, [changeSet, owner]);

  const requestDelete = useCallback(async (target: DeletionTarget, onAccepted: () => void, command?: DeleteGuardCommandContext): Promise<boolean> => {
    const captured = command?.captured ?? { changeSet, owner };
    const sequenceAtStart = ++sequence.current;
    const stillCurrent = () => {
      const current = command ? command.current() : latest.current;
      if (!current) return false;
      return sequence.current === sequenceAtStart &&
        current.owner.ownerKey === captured.owner.ownerKey &&
        current.owner.selectionId === target.id &&
        current.owner.projectId === captured.owner.projectId &&
        current.owner.workflowId === captured.owner.workflowId &&
        current.owner.serverRevision === captured.owner.serverRevision &&
        !current.owner.structureLocked &&
        current.changeSet.operations === captured.changeSet.operations &&
        current.changeSet.preview === captured.changeSet.preview &&
        current.changeSet.baseRevision === captured.changeSet.baseRevision &&
        current.changeSet.status === captured.changeSet.status;
    };
    if (!stillCurrent() || captured.owner.structureLocked || !captured.owner.workflowId ||
      captured.changeSet.status === "conflict" || captured.changeSet.status === "applying") return false;
    activeProofRef.current = stillCurrent;
    retryRef.current = () => requestDelete(target, onAccepted, command);
    protectedOpenRef.current = command?.openProtectedScope ?? openProtectedScope;
    const revision = captured.changeSet.baseRevision ?? captured.owner.serverRevision;
    const closure = deriveDeletionClosure(captured.changeSet.preview as WorkflowDefinitionModel, target);
    if (!closure || !Number.isInteger(revision) || !revision || revision <= 0) {
      setNotice({ state: "unknown", message: t("react.workflows.delete_guard_unknown", "Could not verify saved Automation rules. Retry."), canOpenAutomation: false });
      return false;
    }
    const candidate = captured.changeSet.candidateOperations(deletionOperation(target));
    setNotice({ state: "checking", message: t("react.workflows.delete_guard_checking", "Checking saved Automation rules…"), canOpenAutomation: false });
    try {
      const ownRuleNames: string[] = [];
      let firstOwnedScope: DeletionScope | undefined;
      for (const scope of closure.scopes) {
        const response = await fetchWorkflowReactions({
          workflowId: captured.owner.workflowId, projectId: captured.owner.projectId,
          scopeKind: scope.kind, scopeId: scope.id,
        });
        if (!stillCurrent()) return false;
        if (response?.workflow_id !== captured.owner.workflowId || (captured.owner.projectId ? response.project_id !== captured.owner.projectId : response.project_id !== null && response.project_id !== "") ||
          response.scope_kind !== scope.kind || response.scope_id !== scope.id || !Array.isArray(response.reactions) ||
          response.reactions.some((rule) => !rule || typeof rule !== "object" || Array.isArray(rule) ||
            rule.scope_kind !== scope.kind || rule.scope_id !== scope.id)) throw new Error("invalid reactions response");
        for (const rule of response.reactions) {
          firstOwnedScope ||= scope;
          ownRuleNames.push(typeof rule.display_name === "string" && rule.display_name.trim() ? rule.display_name : scope.label);
        }
      }
      if (ownRuleNames.length) {
        setNotice({ state: "protected", message: t("react.workflows.delete_guard_named", "Saved Automation rule: {names}.", { names: [...new Set(ownRuleNames)].join(", ") }), canOpenAutomation: true, scope: firstOwnedScope });
        return false;
      }
      const result = await applyWorkflowChanges(captured.owner.workflowId, {
        expected_revision: revision,
        operations: candidate,
        dry_run: true,
      });
      if (!stillCurrent()) return false;
      if (result?.workflow_id !== captured.owner.workflowId || result.revision !== revision || result.applied !== false ||
        !Array.isArray(result.issues) || !Array.isArray(result.blocking_issues)) throw new Error("invalid deletion dry run");
      if (deletionScopeIssue(result.blocking_issues)) {
        setNotice({ state: "protected", message: t("react.workflows.delete_guard_foreign", "A saved Automation rule protects part of the pending structure."), canOpenAutomation: false });
        return false;
      }
      if (!deletionIssuesUnderstood(result.blocking_issues)) throw new Error("unknown deletion dry-run issue");
      if (!stillCurrent()) return false;
      onAccepted();
      activeProofRef.current = null;
      retryRef.current = null;
      setNotice(null);
      return true;
    } catch {
      if (!stillCurrent()) return false;
      setNotice({ state: "unknown", message: t("react.workflows.delete_guard_unknown", "Could not verify saved Automation rules. Retry."), canOpenAutomation: false });
      return false;
    }
  }, [changeSet, owner, t]);

  /** One candidate and one all-account dry-run for the whole selected set.
   * Callers stage one graph intent synchronously only in onAccepted. */
  const requestDeleteBatch = useCallback(async (
    targets: readonly DeletionTarget[],
    onAccepted: (batch: DeletionBatch) => void,
    command: DeleteGuardBatchCommandContext,
  ): Promise<boolean> => {
    const captured = command.captured;
    const selectedTargets = targets.map((target) => ({ ...target }));
    const selected: DeleteBatchSelection = { ...command.selection, canvasIds: [...command.selection.canvasIds] };
    const sequenceAtStart = ++sequence.current;
    const stillCurrent = () => {
      const current = command.current();
      if (!current) return false;
      return sequence.current === sequenceAtStart &&
        sameDeleteBatchSelection(selected, command.currentSelection()) &&
        current.owner.ownerKey === captured.owner.ownerKey &&
        current.owner.selectionId === captured.owner.selectionId &&
        current.owner.projectId === captured.owner.projectId &&
        current.owner.workflowId === captured.owner.workflowId &&
        current.owner.serverRevision === captured.owner.serverRevision &&
        !current.owner.structureLocked &&
        current.changeSet.operations === captured.changeSet.operations &&
        current.changeSet.preview === captured.changeSet.preview &&
        current.changeSet.baseRevision === captured.changeSet.baseRevision &&
        current.changeSet.status === captured.changeSet.status &&
        current.changeSet.status !== "applying" && current.changeSet.status !== "conflict";
    };
    if (!validDeleteBatchSelection(selected) || selectedTargets.length !== selected.canvasIds.length ||
      !stillCurrent() || captured.owner.structureLocked || !captured.owner.workflowId ||
      captured.changeSet.status === "conflict" || captured.changeSet.status === "applying") return false;
    const revision = captured.changeSet.baseRevision ?? captured.owner.serverRevision;
    const batch = deriveDeletionBatch(captured.changeSet.preview as WorkflowDefinitionModel, selectedTargets);
    if (!batch) return false;
    activeProofRef.current = stillCurrent;
    retryRef.current = () => requestDeleteBatch(selectedTargets, onAccepted, command);
    protectedOpenRef.current = command.openProtectedScope ?? openProtectedScope;
    if (!Number.isInteger(revision) || !revision || revision <= 0) {
      setNotice({ state: "unknown", message: t("react.workflows.delete_guard_unknown", "Could not verify saved Automation rules. Retry."), canOpenAutomation: false });
      return false;
    }
    const candidate = captured.changeSet.candidateOperations(batch.operations);
    setNotice({ state: "checking", message: t("react.workflows.delete_guard_checking", "Checking saved Automation rules…"), canOpenAutomation: false });
    try {
      const ownRuleNames: string[] = [];
      let firstOwnedScope: DeletionScope | undefined;
      for (const scope of batch.closure.scopes) {
        const response = await fetchWorkflowReactions({
          workflowId: captured.owner.workflowId, projectId: captured.owner.projectId,
          scopeKind: scope.kind, scopeId: scope.id,
        });
        if (!stillCurrent()) return false;
        if (response?.workflow_id !== captured.owner.workflowId ||
          (captured.owner.projectId ? response.project_id !== captured.owner.projectId : response.project_id !== null && response.project_id !== "") ||
          response.scope_kind !== scope.kind || response.scope_id !== scope.id || !Array.isArray(response.reactions) ||
          response.reactions.some((rule) => !rule || typeof rule !== "object" || Array.isArray(rule) ||
            rule.scope_kind !== scope.kind || rule.scope_id !== scope.id)) throw new Error("invalid reactions response");
        for (const rule of response.reactions) {
          firstOwnedScope ||= scope;
          ownRuleNames.push(typeof rule.display_name === "string" && rule.display_name.trim() ? rule.display_name : scope.label);
        }
      }
      if (ownRuleNames.length) {
        setNotice({ state: "protected", message: t("react.workflows.delete_guard_named", "Saved Automation rule: {names}.", { names: [...new Set(ownRuleNames)].join(", ") }), canOpenAutomation: true, scope: firstOwnedScope });
        return false;
      }
      const result = await applyWorkflowChanges(captured.owner.workflowId, {
        expected_revision: revision, operations: candidate, dry_run: true,
      });
      if (!stillCurrent()) return false;
      if (result?.workflow_id !== captured.owner.workflowId || result.revision !== revision || result.applied !== false ||
        !Array.isArray(result.issues) || !Array.isArray(result.blocking_issues)) throw new Error("invalid deletion dry run");
      if (deletionScopeIssue(result.blocking_issues)) {
        setNotice({ state: "protected", message: t("react.workflows.delete_guard_foreign", "A saved Automation rule protects part of the pending structure."), canOpenAutomation: false });
        return false;
      }
      if (!deletionIssuesUnderstood(result.blocking_issues)) throw new Error("unknown deletion dry-run issue");
      if (!stillCurrent()) return false;
      onAccepted(batch);
      activeProofRef.current = null;
      retryRef.current = null;
      setNotice(null);
      return true;
    } catch {
      if (!stillCurrent()) return false;
      setNotice({ state: "unknown", message: t("react.workflows.delete_guard_unknown", "Could not verify saved Automation rules. Retry."), canOpenAutomation: false });
      return false;
    }
  }, [openProtectedScope, t]);

  const retry = useCallback(() => { void retryRef.current?.(); }, []);
  const cancelCommand = useCallback(() => {
    sequence.current += 1;
    activeProofRef.current = null;
    retryRef.current = null;
    setNotice(null);
  }, []);
  const openAutomation = useCallback(() => {
    if (notice?.canOpenAutomation && notice.scope) void protectedOpenRef.current(notice.scope);
  }, [notice]);
  return { notice, currentTarget, requestDelete, requestDeleteBatch, retry, cancelCommand, openAutomation };
}

/**
 * Structural graph edits from the canvas and the drawers. Each one is staged
 * in the local change set; nothing is written until the author applies it.
 */
export function useWorkflowCanvasHandlers(ctx: Record<string, any>) {
  const { t } = useI18n();
  const {
    changeSet,
    openGroupDrawer,
    openNewStepDrawer,
    openStepDrawer,
    openWorkflowSettingsDrawer,
    requestConfirmation,
    setAutomationScopeKind,
    setDrawerMode,
    setDrawerTab,
    setSaveNotice,
    setSaveError,
    setSelectedCanvasIssue,
  } = ctx as Record<string, any> & { changeSet: WorkflowChangeSetController };

  function errorPolicyOperations(preview: WorkflowDefinitionModel, source: WorkflowNodeRef, outcome: WorkflowOutcome, policy: "route_to_step" | "fail") {
    if (outcome.name !== "error") return [];
    try { return [errorRoutePolicyOperation(preview, source, policy, t)]; }
    catch (error) {
      setSaveError?.(error instanceof Error ? error.message : t("react.workflows.error_route_failed", "The error route could not be changed."));
      throw error;
    }
  }

  const handleCanvasValidationIssue = useCallback(
    (issue: WorkflowCanvasIssue) => {
      setSelectedCanvasIssue(issue);
      if (issue.scope === "step" && (issue.step_id || issue.scope_id)) {
        openStepDrawer(issue.step_id || issue.scope_id);
        return;
      }
      if (issue.scope === "group" && (issue.group_id || issue.scope_id)) {
        openGroupDrawer(issue.group_id || issue.scope_id, "settings");
        return;
      }
      if (issue.scope === "workflow" && issue.code === "workflow.terminal_fail_intent_missing" && openWorkflowSettingsDrawer) {
        void openWorkflowSettingsDrawer("smart-setup");
        return;
      }
      setDrawerMode("workflow-settings");
      setDrawerTab("smart-setup");
      setAutomationScopeKind("workflow");
    },
    [openGroupDrawer, openStepDrawer, openWorkflowSettingsDrawer, setSelectedCanvasIssue],
  );

  /**
   * Route one named port to a target. A port that already leads somewhere is
   * replaced only after the author confirms it (`replace: true`).
   */
  const connectPort = useCallback(
    async (source: WorkflowNodeRef, outcome: WorkflowOutcome, target: WorkflowTargetRef, rework = false) => {
      const preview = changeSet.preview as WorkflowDefinitionModel;
      const existing = portEdge(preview, source, outcome);
      if (existing && sameNodeRef(existing.target, target) && Boolean(existing.rework) === rework) {
        return false;
      }
      const edge: WorkflowGraphEdge = { source, outcome, target, ...(rework ? { rework: true } : {}) };
      const policyOperations = errorPolicyOperations(preview, source, outcome, "route_to_step");
      if (existing) {
        const confirmed = await requestConfirmation({
          title: t("react.workflows.replace_connection_title", "Replace this connection?"),
          body: t(
            "react.workflows.replace_connection_body",
            "Outcome \"{outcome}\" of {source} already leads to {current}. It will lead to {next} instead.",
            {
              current: targetLabel(existing.target, t),
              next: targetLabel(target, t),
              outcome: outcomeHandleId(outcome),
              source: source.id,
            },
          ),
          confirmLabel: t("react.workflows.replace_connection", "Replace connection"),
        });
        if (!confirmed) {
          return false;
        }
        changeSet.stage([...policyOperations, { op: "connect", edge, replace: true }]);
      } else {
        changeSet.stage([...policyOperations, { op: "connect", edge }]);
      }
      setSaveNotice(t("react.workflows.connection_staged", "Connection added to the unapplied structure."));
      return true;
    },
    [changeSet, requestConfirmation, t],
  );

  const disconnectPort = useCallback(
    (source: WorkflowNodeRef, outcome: WorkflowOutcome) => {
      const preview = changeSet.preview as WorkflowDefinitionModel;
      if (!portEdge(preview, source, outcome)) {
        return;
      }
      // One graph intent, so the canvas toast's Undo and ⌘Z bring the connection back.
      changeSet.stageGraphIntent(
        [
          ...errorPolicyOperations(preview, source, outcome, "fail"),
          { op: "disconnect", source, outcome },
        ],
        t("react.workflows.disconnect_staged", "Connection removal added to the unapplied structure."),
      );
      setSaveNotice(t("react.workflows.disconnect_staged", "Connection removal added to the unapplied structure."));
    },
    [changeSet, t],
  );

  /** Set the root entry, or the entry of a container body or lane (the placement's owner). */
  const setEntry = useCallback(
    (target: WorkflowNodeRef, placement: Partial<WorkflowPlacement> = {}) => {
      const owner = placementOwnerFields(placement);
      changeSet.stage(
        {
          op: "set_entry",
          target,
          ...(owner.group_id ? { group_id: owner.group_id } : {}),
          ...(owner.for_each_id ? { for_each_id: owner.for_each_id } : {}),
          ...(owner.lane_id ? { lane_id: owner.lane_id } : {}),
        },
        t("react.workflows.entry_staged", "Entry change added to the unapplied structure."),
      );
      setSaveNotice(t("react.workflows.entry_staged", "Entry change added to the unapplied structure."));
    },
    [changeSet, t],
  );

  /**
   * Move a step into a container body, a lane or the root. Its old connections
   * cannot cross scopes and are dropped; a place with no entry yet starts with
   * it. The editor's Placement asks first, inline, and passes `confirmed`;
   * any other caller still asks here. The move is one graph intent, so the
   * toast's Undo (and ⌘Z) takes it back whole.
   */
  const moveStep = useCallback(
    async (stepId: string, placement: WorkflowPlacement, options: { confirmed?: boolean; placeLabel?: string } = {}) => {
      const preview = changeSet.preview as WorkflowDefinitionModel;
      const step = preview.steps.find((item) => item.step_id === stepId);
      if (workflowHookPlacementInvalid(step?.step_kind, step?.override, placement)) {
        setSaveError?.(t("react.workflows.hook_root_only", "Hooks belong in the workflow root, before or after a Group or For each."));
        return false;
      }
      if (!options.confirmed) {
        const dropped = edgesTouchingStep(preview, stepId).map((edge) => edgeNameLabel(preview, edge, t));
        const confirmed = await requestConfirmation({
          title: t("react.workflows.move_step_title", "Move this step?"),
          body: t(
            "react.workflows.move_step_body",
            "Its connections are removed by the move. Connect it again in its new place before applying.",
          ),
          details: dropped,
          confirmLabel: t("react.workflows.move_step_confirm", "Move step"),
        });
        if (!confirmed) {
          return false;
        }
      }
      const message = t("react.workflows.move_step_staged", "Move added to the unapplied structure. Reconnect the step, then apply.");
      const token = changeSet.stageGraphIntent(moveStepOperations(preview, stepId, placement), message);
      if (!token) return false;
      const name = step?.display_name || stepId;
      announceCanvasNotice({
        label: options.placeLabel
          ? t("react.workflows.move_step_done", "{step} moved to {place}.", { step: name, place: options.placeLabel })
          : message,
        tone: "success",
        action: {
          label: t("react.workflows.undo", "Undo"),
          onClick: () => {
            if (changeSet.hasGraphIntent(token)) changeSet.undoGraphIntent();
          },
        },
      });
      return true;
    },
    [changeSet, requestConfirmation, t],
  );

  const insertStepOnEdge = useCallback(
    (edge: WorkflowGraphEdge) => {
      openNewStepDrawer({
        ...sourcePlacement(changeSet.preview as WorkflowDefinitionModel, edge.source),
        insert: {
          label: `${edge.source.id} · ${outcomeHandleId(edge.outcome)} → ${targetLabel(edge.target, t)}`,
          outcome: edge.outcome,
          source: edge.source,
        },
      });
    },
    [changeSet, openNewStepDrawer, t],
  );

  /** The "+" on a free outcome: a new step, created already connected after it, in its scope. */
  const appendStepAfterOutcome = useCallback(
    (source: WorkflowNodeRef, outcome: WorkflowOutcome) => {
      openNewStepDrawer({
        ...sourcePlacement(changeSet.preview as WorkflowDefinitionModel, source),
        insert: { append: true, label: `${source.id} · ${outcomeHandleId(outcome)}`, outcome, source },
      });
    },
    [changeSet, openNewStepDrawer],
  );

  return {
    appendStepAfterOutcome,
    connectPort,
    disconnectPort,
    handleCanvasValidationIssue,
    insertStepOnEdge,
    moveStep,
    setEntry,
  };
}
