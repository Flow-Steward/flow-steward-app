import type { LocalCreationOwner } from "./detail-local-step-creation";
/* eslint-disable @typescript-eslint/no-explicit-any */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";

import { useI18n } from "@/lib/i18n/useI18n";
import { queryKeys } from "@/lib/query/keys";
import {
  canvasGraphWithDefinition,
  definitionFromWorkflow,
  ROOT_PLACEMENT,
  type WorkflowPlacement,
} from "@/pages/workflows/builder/change-set";
import {
  authorRef,
  canvasPlacement,
  childPlacement,
  canvasOperation,
  childRef,
  clearHookOperations,
  hookEndCompletion,
  hookFrame,
  hookMemberId,
  parseHookNodeId,
  useHookFrames,
  type HookFrame,
} from "@/pages/workflows/builder/hook-frames";
import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import { useWorkflowCanvasHandlers } from "@/pages/workflows/detail/detail-canvas-handlers";
import { useWorkflowChangeSet, type WorkflowChangeSetController } from "@/pages/workflows/detail/detail-change-set";
import { useWorkflowDefinitionRows } from "@/pages/workflows/detail/detail-data-controller";
import type { StepCreateContext } from "@/pages/workflows/detail/detail-drawer-controller";
import {
  fetchWorkflowCanvasGraph,
  fetchWorkflowDetail,
  type WorkflowGraphEdge,
  type WorkflowNodeRef,
  type WorkflowOutcome,
  type WorkflowTargetRef,
} from "@/pages/workflows/shared/api";
import { createAndBindWorkflowHook, hookErrorIssues, workflowHooksQueryKey } from "@/pages/workflows/shared/hook-api";

const NO_ISSUES: Array<Record<string, unknown>> = [];

/** Only the immediately committed binding can retain its existing capability. */
function sameCreationBinding(previous: readonly unknown[] | null, next: readonly unknown[]): boolean {
  return previous !== null && previous.length === next.length && previous.every((value, index) => Object.is(value, next[index]));
}

function hookOfRef(ref: WorkflowTargetRef): string {
  if (!("kind" in ref)) return "";
  const id = ref.kind === "group_exit" ? ref.group_id : ref.kind === "for_each_exit" ? ref.for_each_id : ref.id;
  const parsed = parseHookNodeId(id);
  // The frame's own exit is the end of the hook: it belongs to that hook.
  if (parsed && !parsed.memberStepId && ref.kind === "group_exit") return parsed.hookId;
  return parsed?.memberStepId ? parsed.hookId : "";
}

/** A new-step context from the child's handlers, in canvas terms: inside the frame. */
function frameCreateContext(hookId: string, context: Partial<StepCreateContext>): Partial<StepCreateContext> {
  const insert = context.insert;
  return {
    ...context,
    ...canvasPlacement(hookId, { ...ROOT_PLACEMENT, ...context }),
    insert: insert ? { ...insert, source: { ...insert.source, id: hookMemberId(hookId, insert.source.id) } } : null,
  };
}

/** A create context inside a frame, in the child's own terms: its root. */
function childCreateContext(context: StepCreateContext | null): StepCreateContext | null {
  if (!context) return null;
  const insert = context.insert;
  return {
    ...context,
    ...childPlacement({ ...ROOT_PLACEMENT, ...context }),
    insert: insert ? { ...insert, source: childRef(insert.source) } : null,
  };
}

/** A unique, valid id for the workflow "Add step" creates for an empty hook. */
function newChildWorkflowId(workflowId: string, hookId: string, attempt: number): string {
  const base = `${workflowId.split(".").pop() || "workflow"}_${hookId}`
    .toLowerCase()
    .replaceAll(/[^a-z0-9_]/g, "_")
    .replace(/^[^a-z]+/, "")
    .slice(0, 56) || "hook_actions";
  return attempt ? `${base}_${attempt + 1}` : base;
}

/**
 * The hook a builder edits in place (B14): one hook at a time, the "active"
 * one, whose connected client workflow gets its own definition, revision and
 * change set. Steps of that workflow are drawn inside the hook's frame with
 * ids `hook:<hook_id>:<step_id>`; while one of them (or a new step in the
 * frame) is in the drawer, the step-level controllers run against the child.
 * The author's own change set and structure are untouched.
 */
export function useWorkflowHookScope(ctx: Record<string, any>) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const {
    authorChangeSet,
    enabled,
    focusCanvasNode,
    openNewStepDrawerRef,
    openStepDrawerRef,
    openGroupDrawerRef,
    projectId,
    requestConfirmation,
    selectedStepId,
    selectedGroupId,
    drawerMode,
    groupCreateMode,
    setSelectedGroupId,
    structureLocked,
    setAutomationScopeKind,
    setDrawerMode,
    setDrawerTab,
    setSaveError,
    setSaveNotice,
    setSelectedCanvasIssue,
    setSelectedStepId,
    stepCreateContext,
    workflow,
    workflowId,
  } = ctx as Record<string, any> & { authorChangeSet: WorkflowChangeSetController };

  const previewFrames = useHookFrames({ enabled, projectId, steps: authorChangeSet.preview.steps, workflowId });
  const serverFrames = useMemo(() => {
    const saved = Array.isArray(workflow?.steps) ? workflow.steps : [];
    return new Map([...previewFrames].map(([key, frame]) => {
      const persisted = saved.some((step: Record<string, unknown>) => step.step_id === frame.stepId && step.step_kind === "invoke_workflow" && textOf((step.override as { invoke_workflow?: { hook?: { hook_id?: string } } })?.invoke_workflow?.hook?.hook_id) === frame.hookId);
      return [key, persisted ? frame : { ...frame, declarationPending: true, connected: false, targetWorkflowId: "", targetName: "", bindingRevision: 0, members: [], memberGroups: [], memberForEach: [], memberEdges: [], memberEntry: null }];
    }));
  }, [previewFrames, workflow]);
  const framesByHook = useMemo(
    () => new Map([...serverFrames.values()].map((frame) => [frame.hookId, frame])),
    [serverFrames],
  );

  const selectedMember = parseHookNodeId(selectedStepId || "");
  const selectedContainer = parseHookNodeId(selectedGroupId || "");
  const createHook = parseHookNodeId(textOf(stepCreateContext?.forEachId) || textOf(stepCreateContext?.groupId));
  const [containerCreationHookId, setContainerCreationHookId] = useState("");
  const requestedHookId = (selectedMember?.memberStepId ? selectedMember.hookId : "")
    || createHook?.hookId
    || (selectedContainer?.memberStepId ? selectedContainer.hookId : "")
    || (groupCreateMode ? containerCreationHookId : "")
    || "";
  const [activeHookId, setActiveHookId] = useState("");
  const [activeOwner, setActiveOwner] = useState<{ hookId: string; workflowId: string } | null>(null);
  const singleConnected = [...framesByHook.values()].filter((frame) => frame.targetWorkflowId);
  // The hook the selection points at wins (a deep link, the DOM bridge); the
  // canvas handlers switch it explicitly, refusing while the current one holds
  // unapplied changes.
  const effectiveHookId = (requestedHookId && !requestedBlocked(requestedHookId, activeHookId) ? requestedHookId : "")
    || activeHookId
    || (singleConnected.length === 1 ? singleConnected[0].hookId : "");
  const activeServerFrame = effectiveHookId ? framesByHook.get(effectiveHookId) : undefined;
  const frameTargetId = activeServerFrame?.targetWorkflowId || "";
  const childWorkflowId = activeServerFrame?.declarationPending ? "" : activeOwner?.hookId === effectiveHookId && activeOwner.workflowId
    ? activeOwner.workflowId : frameTargetId;

  const childDetailQuery = useQuery({
    queryKey: queryKeys.workflowDetail(childWorkflowId),
    queryFn: () => fetchWorkflowDetail(childWorkflowId),
    enabled: enabled && Boolean(childWorkflowId),
    retry: false,
  });
  const childCanvasQuery = useQuery({
    queryKey: queryKeys.workflowCanvasGraph(childWorkflowId),
    queryFn: () => fetchWorkflowCanvasGraph(childWorkflowId),
    enabled: enabled && Boolean(childWorkflowId),
    retry: false,
  });
  const childWorkflow = childWorkflowId ? childDetailQuery.data : undefined;
  const childDefinition = useMemo(() => definitionFromWorkflow(childWorkflow), [childWorkflow]);
  const childRevision = Number((childWorkflow as Record<string, unknown> | undefined)?.revision || 0) || null;
  const refetchChild = useCallback(async () => {
    const [detail] = await Promise.all([
      childDetailQuery.refetch(),
      childCanvasQuery.refetch(),
      queryClient.invalidateQueries({ queryKey: ["workflows", "where-used", projectId] }),
    ]);
    return detail.data ?? null;
  }, [childCanvasQuery, childDetailQuery, projectId, queryClient]);
  const rawChildChangeSet = useWorkflowChangeSet({
    refetchWorkflowSurfaces: refetchChild,
    serverDefinition: childDefinition,
    serverRevision: childRevision,
    workflowId: childWorkflowId || workflowId,
    // Two Hook frames may share one child workflow but have different canvas ids.
    placementViewKey: effectiveHookId,
  });
  // R2-11: every staged edit keeps the hook whole around its hidden end (its
  // first node starts it and ends there; with nothing left the end starts it).
  // The completion joins the same command, so one Undo reverses both.
  const pendingOperations = rawChildChangeSet.operations;
  const completed = useCallback((next: WorkflowChangeOperation | WorkflowChangeOperation[]) => {
    const list = Array.isArray(next) ? next : [next];
    return [...list, ...hookEndCompletion(childDefinition, pendingOperations, list)];
  }, [childDefinition, pendingOperations]);
  const rawStage = rawChildChangeSet.stage;
  const rawStageGraphIntent = rawChildChangeSet.stageGraphIntent;
  const stage = useCallback((next: WorkflowChangeOperation | WorkflowChangeOperation[], message = "") => rawStage(completed(next), message), [completed, rawStage]);
  const stageGraphIntent = useCallback((next: WorkflowChangeOperation[], message = "") => rawStageGraphIntent(completed(next), message), [completed, rawStageGraphIntent]);
  const childChangeSet = useMemo(
    () => ({ ...rawChildChangeSet, stage, stageGraphIntent }) as WorkflowChangeSetController,
    [rawChildChangeSet, stage, stageGraphIntent],
  );
  // A hook whose binding changed elsewhere follows it at once instead of drawing
  // the old workflow's steps; unapplied edits keep their owner until applied or discarded.
  const childIdle = !rawChildChangeSet.hasChanges && rawChildChangeSet.status !== "applying";
  const [seenTarget, setSeenTarget] = useState({ hookId: effectiveHookId, target: frameTargetId });
  if (seenTarget.hookId !== effectiveHookId || seenTarget.target !== frameTargetId) {
    setSeenTarget({ hookId: effectiveHookId, target: frameTargetId });
    if (seenTarget.hookId === effectiveHookId && childIdle && !activeServerFrame?.declarationPending
      && activeOwner?.hookId === effectiveHookId && activeOwner.workflowId && activeOwner.workflowId !== frameTargetId) {
      setActiveOwner({ hookId: effectiveHookId, workflowId: frameTargetId });
    }
  }
  const memberStepId = selectedMember?.memberStepId && selectedMember.hookId === effectiveHookId ? selectedMember.memberStepId : "";
  const childRows = useWorkflowDefinitionRows({
    operations: childChangeSet.operations,
    preview: childChangeSet.preview,
    selectedGroupId: selectedContainer?.hookId === effectiveHookId ? selectedContainer.memberStepId : "",
    selectedStepId: memberStepId,
    validationErrors: NO_ISSUES,
    validationWarnings: NO_ISSUES,
  });

  const childLoaded = Boolean(childWorkflowId && childDetailQuery.isSuccess);
  const scopeActive = childLoaded && (Boolean(memberStepId) || createHook?.hookId === effectiveHookId
    || Boolean(selectedContainer?.memberStepId && selectedContainer.hookId === effectiveHookId)
    || Boolean(groupCreateMode && containerCreationHookId === effectiveHookId));
  /** A member is selected but its workflow is still loading: nothing is closed meanwhile. */
  const scopePending = Boolean(selectedMember?.memberStepId || selectedContainer?.memberStepId) && !scopeActive;

  // The frames, the active one drawn from its unapplied preview.
  const frames = useMemo(() => {
    if (!activeServerFrame || activeServerFrame.declarationPending || !childCanvasQuery.data) return serverFrames;
    const previewGraph = canvasGraphWithDefinition(childCanvasQuery.data, childChangeSet.preview, t);
    const next = new Map(serverFrames);
    const frame = hookFrame({ ...activeServerFrame, targetWorkflowId: childWorkflowId, targetName: childWorkflow?.display_name || activeServerFrame.targetName }, previewGraph, activeServerFrame.usedIn);
    // An unapplied preview is not the archived definition of an earlier run.
    next.set(activeServerFrame.stepId, childChangeSet.hasChanges ? { ...frame, targetRevision: null } : frame);
    return next;
  }, [activeServerFrame, childCanvasQuery.data, childChangeSet.hasChanges, childChangeSet.preview, childWorkflow?.display_name, childWorkflowId, serverFrames, t]);

  const childHandlers = useWorkflowCanvasHandlers({
    changeSet: childChangeSet,
    openGroupDrawer: (groupId: string, tab?: string) => openGroupDrawerRef.current?.(hookMemberId(effectiveHookId, groupId), tab),
    openNewStepDrawer: (context: Partial<StepCreateContext>) => openNewStepDrawerRef.current?.(frameCreateContext(effectiveHookId, context)),
    openStepDrawer: (stepId: string) => openStepDrawerRef.current?.(hookMemberId(effectiveHookId, stepId)),
    requestConfirmation,
    setAutomationScopeKind,
    setDrawerMode,
    setDrawerTab,
    setSaveError,
    setSaveNotice,
    setSelectedCanvasIssue,
  });

  /** Whether an edit that touches hook members can go to the active child; says why not. */
  const memberEditTarget = useCallback((...refs: WorkflowTargetRef[]): "author" | "child" | "refused" => {
    const hooks = refs.map(hookOfRef);
    if (hooks.every((hookId) => !hookId)) {
      if (structureLocked) {
        setSaveError(t("react.workflows.structure_locked_hint", "This workflow is protected by its extension. You can read it and connect your own workflows to its hooks."));
        return "refused";
      }
      return "author";
    }
    if (hooks.some((hookId) => !hookId) || new Set(hooks).size > 1) {
      setSaveError(t("react.workflows.hook_connect_scope", "A connection inside a hook stays between that hook's steps."));
      return "refused";
    }
    if (hooks[0] !== effectiveHookId || !childLoaded) {
      setSaveError(t("react.workflows.hook_select_first", "Open a step of this hook first, then edit its connections."));
      return "refused";
    }
    setActiveOwner((current) => current?.hookId === effectiveHookId && current.workflowId === childWorkflowId
      ? current : { hookId: effectiveHookId, workflowId: childWorkflowId });
    return "child";
  }, [childLoaded, childWorkflowId, effectiveHookId, setSaveError, structureLocked, t]);

  /** The author's canvas handlers, routed: hook members go to the child's change set. */
  const route = useCallback((author: Record<string, any>) => ({
    ...author,
    connectPort: (source: WorkflowNodeRef, outcome: WorkflowOutcome, target: WorkflowTargetRef, rework = false) => {
      // The frame's exit is the end of the hook: childRef makes it the hidden end step.
      const where = memberEditTarget(source, target);
      if (where === "refused") return false;
      if (where === "child") return childHandlers.connectPort(childRef(source), outcome, childRef(target), rework);
      return author.connectPort(authorRef(source, frames), outcome, authorRef(target, frames), rework);
    },
    disconnectPort: (source: WorkflowNodeRef, outcome: WorkflowOutcome) => {
      const where = memberEditTarget(source);
      if (where === "refused") return;
      if (where === "child") return childHandlers.disconnectPort(childRef(source), outcome);
      return author.disconnectPort(authorRef(source, frames), outcome);
    },
    insertStepOnEdge: (edge: WorkflowGraphEdge) => {
      const where = memberEditTarget(edge.source, edge.target);
      if (where === "refused") return;
      if (where === "child") return childHandlers.insertStepOnEdge({ ...edge, source: childRef(edge.source), target: childRef(edge.target) });
      return author.insertStepOnEdge({ ...edge, source: authorRef(edge.source, frames), target: authorRef(edge.target, frames) });
    },
    appendStepAfterOutcome: (source: WorkflowNodeRef, outcome: WorkflowOutcome) => {
      const where = memberEditTarget(source);
      if (where === "refused") return;
      if (where === "child") return childHandlers.appendStepAfterOutcome(childRef(source), outcome);
      return author.appendStepAfterOutcome(authorRef(source, frames), outcome);
    },
    setEntry: (target: WorkflowNodeRef, placement: Partial<WorkflowPlacement> = {}) => {
      const where = memberEditTarget(target);
      if (where === "refused") return;
      if (where === "child") return childHandlers.setEntry(childRef(target), childPlacement({ ...ROOT_PLACEMENT, ...placement }));
      return author.setEntry(authorRef(target, frames), placement);
    },
    // The editor's inline confirmation travels with the move, so it is never asked twice.
    moveStep: (stepId: string, placement: WorkflowPlacement, options?: { confirmed?: boolean; placeLabel?: string }) => {
      const member = parseHookNodeId(stepId);
      if (member?.memberStepId) {
        const placementHook = parseHookNodeId(placement.forEachId || placement.groupId);
        if (memberEditTarget({ kind: "step", id: stepId }) !== "child" || placementHook?.hookId !== member.hookId) {
          setSaveError(t("react.workflows.hook_move_refused", "A step inside a hook stays in that hook."));
          return Promise.resolve(false);
        }
        return childHandlers.moveStep(member.memberStepId, childPlacement(placement), options);
      }
      if (memberEditTarget({ kind: "step", id: stepId }) === "refused") return Promise.resolve(false);
      return author.moveStep(stepId, placement, options);
    },
  }), [childHandlers, frames, memberEditTarget, setSaveError, t]);

  // Discard may remove a local selected row. Capture its committed child/editor
  // owner here; the general missing-step cleanup deliberately leaves Hook rows
  // alone while their child query and journal are becoming ready.
  const discardEditor = useMemo(() => ({
    projectId,
    workflowId,
    hookId: effectiveHookId,
    childWorkflowId,
    selectedStepId,
    journal: childChangeSet,
    retire: drawerMode === "step" && !stepCreateContext && childLoaded
      && childChangeSet.ownerReady && childChangeSet.workflowId === childWorkflowId
      && childWorkflow?.workflow_id === childWorkflowId
      && selectedMember?.hookId === effectiveHookId && Boolean(memberStepId)
      && childRows.selectedStep?.local === true && childRows.selectedStep.runtimeId === memberStepId,
  }), [childChangeSet, childLoaded, childRows.selectedStep, childWorkflow?.workflow_id, childWorkflowId, drawerMode, effectiveHookId, memberStepId, projectId, selectedMember?.hookId, selectedStepId, stepCreateContext, workflowId]);
  const committedDiscardEditor = useRef<typeof discardEditor | null>(null);
  useLayoutEffect(() => {
    committedDiscardEditor.current = discardEditor;
    return () => {
      if (committedDiscardEditor.current === discardEditor) committedDiscardEditor.current = null;
    };
  }, [discardEditor]);

  /**
   * The canvas's one change-set controller. Apply applies the hook's workflow
   * first, then the author's; Discard drops both. The draft marks show the
   * child's operations in canvas terms.
   */
  const canvasChangeSet = useMemo((): WorkflowChangeSetController => {
    const childBusy = childChangeSet.hasChanges || childChangeSet.status === "applying" || childChangeSet.status === "conflict" || childChangeSet.status === "error";
    if (!childBusy) return authorChangeSet;
    const both = childChangeSet.hasChanges && authorChangeSet.hasChanges;
    return {
      ...childChangeSet,
      hasChanges: true,
      operations: [
        ...authorChangeSet.operations,
        ...childChangeSet.operations.map((operation) => canvasOperation(effectiveHookId, operation)),
      ],
      preview: authorChangeSet.preview,
      notice: both
        ? t("react.workflows.hook_apply_order", "The hook's workflow is applied first, then this workflow.")
        : childChangeSet.notice,
      apply: async () => {
        const applied = await childChangeSet.apply();
        if (applied && authorChangeSet.hasChanges) return authorChangeSet.apply();
        return applied;
      },
      discard: () => {
        // An old toolbar callback cannot discard or retire a newer owner,
        // including leaving this Hook and returning to the same canvas id.
        if (committedDiscardEditor.current !== discardEditor) return;
        childChangeSet.discard();
        if (authorChangeSet.hasChanges) authorChangeSet.discard();
        if (discardEditor.retire && committedDiscardEditor.current === discardEditor) {
          setSelectedStepId((current: string) => committedDiscardEditor.current === discardEditor && current === discardEditor.selectedStepId ? "" : current);
          setDrawerMode((current: string) => committedDiscardEditor.current === discardEditor && current === "step" ? "" : current);
        }
      },
    } as WorkflowChangeSetController;
  }, [authorChangeSet, childChangeSet, discardEditor, effectiveHookId, setDrawerMode, setSelectedStepId, t]);

  /** Why the last first-step preparation of a hook failed, for the palette that asked. */
  const creationFailure = useRef("");
  const failCreation = useCallback((message: string) => {
    creationFailure.current = message;
    setSaveError(message);
    return "";
  }, [setSaveError]);
  /**
   * The first Add on an empty hook prepares the place its steps are kept: an
   * empty per-project workflow (only its hidden end) bound to the hook. Nothing
   * is prefilled and no workflow is ever named; it needs a chosen project.
   */
  const ensureHookWorkflow = useCallback(async (frame: HookFrame) => {
    creationFailure.current = "";
    if (frame.declarationPending) {
      return failCreation(t("react.workflows.hook_apply_before_steps", "Apply the declaration before adding its steps."));
    }
    if (frame.targetWorkflowId) return frame.targetWorkflowId;
    // R2-10: never ask a project API with an empty project id.
    if (!projectId) {
      return failCreation(t("react.workflows.hook_choose_project_first", "Choose a project in the header first: each project keeps its own steps in a hook."));
    }
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const childId = newChildWorkflowId(workflowId, frame.hookId, attempt);
      try {
        const result = await createAndBindWorkflowHook(projectId, workflowId, frame.hookId, {
          expected_revision: frame.bindingRevision,
          child_workflow_id: childId,
          display_name: frame.displayName,
        });
        await queryClient.invalidateQueries({ queryKey: workflowHooksQueryKey(projectId, workflowId) });
        await queryClient.invalidateQueries({ queryKey: ["workflows", "meta", "callable-workflows"] });
        return result.target_workflow_id || childId;
      } catch (error) {
        const issues = hookErrorIssues(error);
        if (JSON.stringify((error as { payload?: unknown })?.payload || "").includes("workflow.hook.child_id_taken")) continue;
        const reason = issues.map((issue) => issue.message || issue.code).filter(Boolean).join(" ")
          || (error instanceof Error ? error.message : String(error));
        return failCreation(t("react.workflows.hook_steps_unavailable", "The hook could not take steps: {reason}", { reason }));
      }
    }
    return failCreation(t("react.workflows.hook_steps_unavailable", "The hook could not take steps: {reason}", { reason: "workflow.hook.child_id_taken" }));
  }, [failCreation, projectId, queryClient, t, workflowId]);

  /**
   * "Clear the hook": removes every step of the active hook as one unapplied
   * change (Undo or Discard brings them back; Apply makes the hook empty again).
   */
  const clearHook = useCallback((hookId: string) => {
    const current = childChangeSet;
    const frame = framesByHook.get(hookId);
    if (!frame?.targetWorkflowId) return false;
    if (hookId !== effectiveHookId || current.workflowId !== frame.targetWorkflowId || !current.ownerReady) {
      setSaveError(t("react.workflows.hook_steps_loading", "The hook's steps are still loading. Try again in a moment."));
      return false;
    }
    const operations = clearHookOperations(current.preview);
    if (!operations.length) {
      setSaveError(t("react.workflows.hook_already_empty", "This hook has no steps to clear."));
      return false;
    }
    return Boolean(current.stageGraphIntent(operations, t("react.workflows.hook_cleared", "The hook's steps are removed from the unapplied structure. Apply to keep the hook empty, or Undo.")));
  }, [childChangeSet, effectiveHookId, framesByHook, setSaveError, t]);

  const canActivateHook = useCallback((hookId: string) => {
    if (hookId !== effectiveHookId && (childChangeSet.hasChanges || childChangeSet.status === "applying")) {
      setSaveError(t("react.workflows.hook_switch_blocked", "Apply or discard the changes in hook {hook} first.", {
        hook: activeServerFrame?.displayName || effectiveHookId,
      }));
      return false;
    }
    return !hookId || framesByHook.has(hookId);
  }, [activeServerFrame?.displayName, childChangeSet.hasChanges, childChangeSet.status, effectiveHookId, framesByHook, setSaveError, t]);
  const activateHook = useCallback((hookId: string) => {
    if (!canActivateHook(hookId)) return false;
    setActiveHookId(hookId);
    if (!childChangeSet.hasChanges && childChangeSet.status !== "applying") {
      setActiveOwner({ hookId, workflowId: framesByHook.get(hookId)?.targetWorkflowId || "" });
    }
    return true;
  }, [canActivateHook, childChangeSet.hasChanges, childChangeSet.status, framesByHook]);
  const creationRequests = useRef(new Map<string, Promise<string>>());
  const ensureWorkflowOnce = useCallback((frame: HookFrame) => {
    if (frame.targetWorkflowId) return Promise.resolve(frame.targetWorkflowId);
    const key = `${projectId}:${workflowId}:${frame.hookId}:${frame.bindingRevision}`;
    const pending = creationRequests.current.get(key);
    if (pending) return pending;
    const promise = ensureHookWorkflow(frame).finally(() => creationRequests.current.delete(key));
    creationRequests.current.set(key, promise);
    return promise;
  }, [ensureHookWorkflow, projectId, workflowId]);

  // Publish current controllers only after React commits their exact owner,
  // revision and preview. This effect never creates or stages anything.
  const creationGeneration = useRef(0);
  const creationRegistry = useRef<{
    alive: boolean;
    author: LocalCreationOwner | null;
    child: LocalCreationOwner | null;
    authorBinding: readonly unknown[] | null;
    childBinding: readonly unknown[] | null;
    childId: string;
    childBlocked: boolean;
    waiters: Set<{ hookId: string; workflowId: string; settle: (owner: LocalCreationOwner | null) => void }>;
  }>({ alive: false, author: null, child: null, authorBinding: null, childBinding: null, childId: "", childBlocked: false, waiters: new Set() });
  useLayoutEffect(() => {
    const registry = creationRegistry.current;
    registry.alive = true;
    const generation = ++creationGeneration.current;
    const authorRevision = Number(workflow?.revision || 0);
    const authorReady = authorChangeSet.ownerReady && authorChangeSet.workflowId === workflowId && textOf(workflow?.workflow_id) === workflowId && authorRevision > 0 && !["applying", "conflict"].includes(authorChangeSet.status);
    const authorBinding = authorReady ? [
      projectId, workflowId, authorRevision, enabled, structureLocked,
      authorChangeSet.workflowId, authorChangeSet.ownerReady, authorChangeSet.preview, authorChangeSet.stage, authorChangeSet.stageGraphIntent, authorChangeSet.operations, authorChangeSet.status,
      focusCanvasNode,
    ] : null;
    if (!authorBinding) registry.author = null;
    else if (!registry.author || !sameCreationBinding(registry.authorBinding, authorBinding)) {
      registry.author = { workflowId, hookId: "", revision: authorRevision, generation, preview: authorChangeSet.preview, stage: authorChangeSet.stage, stageGraphIntent: authorChangeSet.stageGraphIntent, viewEpoch: authorChangeSet.placementViewEpoch, toCanvasId: (id) => id, focus: (id, options) => focusCanvasNode?.(id, options) };
    }
    registry.authorBinding = authorBinding;
    const childReady = childChangeSet.ownerReady && childChangeSet.workflowId === childWorkflowId && enabled && childLoaded && textOf(childWorkflow?.workflow_id) === childWorkflowId && Boolean(childRevision) && !["applying", "conflict"].includes(childChangeSet.status);
    const childBinding = childReady ? [
      projectId, workflowId, authorRevision, enabled, structureLocked, authorChangeSet.ownerReady, authorChangeSet.preview,
      effectiveHookId, childWorkflowId, childRevision, activeServerFrame?.stepId, activeServerFrame?.targetWorkflowId, activeServerFrame?.bindingRevision, Boolean(activeServerFrame?.declarationPending),
      childChangeSet.workflowId, childChangeSet.ownerReady, childChangeSet.preview, childChangeSet.stage, childChangeSet.stageGraphIntent, childChangeSet.operations, childChangeSet.status,
      focusCanvasNode,
    ] : null;
    if (!childBinding) registry.child = null;
    else if (!registry.child || !sameCreationBinding(registry.childBinding, childBinding)) {
      registry.child = { workflowId: childWorkflowId, hookId: effectiveHookId, revision: childRevision!, generation, preview: childChangeSet.preview, stage: childChangeSet.stage, stageGraphIntent: childChangeSet.stageGraphIntent, viewEpoch: childChangeSet.placementViewEpoch, toCanvasId: (id) => hookMemberId(effectiveHookId, id), focus: (id, options) => focusCanvasNode?.(id, options) };
    }
    registry.childBinding = childBinding;
    registry.childId = childWorkflowId;
    registry.childBlocked = childDetailQuery.isError || ["applying", "conflict"].includes(childChangeSet.status) || (childLoaded && (!childRevision || textOf(childWorkflow?.workflow_id) !== childWorkflowId));
    // Settlement removes waiters; iterate a snapshot of this committed owner generation.
    const waiters = Array.from(registry.waiters);
    for (const waiter of waiters) {
      const owner = waiter.hookId ? registry.child : registry.author;
      if (owner?.hookId === waiter.hookId && owner.workflowId === waiter.workflowId) waiter.settle(owner);
      else if (!waiter.hookId || (registry.childId === waiter.workflowId && registry.childBlocked) || (registry.child && registry.child.hookId === waiter.hookId && registry.child.workflowId !== waiter.workflowId)) waiter.settle(null);
    }
  }, [activeServerFrame, authorChangeSet, childChangeSet, childDetailQuery.isError, childLoaded, childRevision, childWorkflow, childWorkflowId, effectiveHookId, enabled, focusCanvasNode, projectId, structureLocked, workflow, workflowId]);
  useLayoutEffect(() => () => {
    const registry = creationRegistry.current;
    registry.alive = false; registry.author = null; registry.child = null;
    registry.authorBinding = null; registry.childBinding = null;
    const waiters = Array.from(registry.waiters);
    for (const waiter of waiters) waiter.settle(null);
  }, []);
  const acquireCreationOwner = useCallback((hookId: string, expectedWorkflowId: string, signal?: AbortSignal): Promise<LocalCreationOwner | null> => {
    const registry = creationRegistry.current;
    if (!registry.alive || signal?.aborted) return Promise.resolve(null);
    const owner = hookId ? registry.child : registry.author;
    if (owner?.hookId === hookId && owner.workflowId === expectedWorkflowId) return Promise.resolve(owner);
    if (!hookId || (registry.childId === expectedWorkflowId && registry.childBlocked)) return Promise.resolve(null);
    return new Promise((resolve) => {
      const waiter = { hookId, workflowId: expectedWorkflowId, settle: (ready: LocalCreationOwner | null) => { registry.waiters.delete(waiter); signal?.removeEventListener("abort", cancel); resolve(ready); } };
      const cancel = () => waiter.settle(null);
      registry.waiters.add(waiter);
      signal?.addEventListener("abort", cancel, { once: true });
      if (signal?.aborted) cancel();
    });
  }, []);
  const isCreationOwnerCurrent = useCallback((owner: LocalCreationOwner) => {
    const registry = creationRegistry.current;
    return registry.alive && (owner.hookId ? registry.child : registry.author) === owner;
  }, []);

  return {
    authorWorkflowId: workflowId,
    acquireCreationOwner,
    isCreationOwnerCurrent,
    canvasChangeSet,
    childChangeSet,
    /** The child's own structural edits, in its own ids: the step drawer of a member uses them. */
    childHandlers,
    childRevision,
    childRows,
    childWorkflow,
    childWorkflowId,
    effectiveHookId,
    ensureHookWorkflow: ensureWorkflowOnce,
    /** Why the last ensureHookWorkflow returned no workflow, said in the palette that asked. */
    creationFailure: () => creationFailure.current,
    clearHook,
    canActivateHook,
    activateHook,
    containerCreationHookId,
    setContainerCreationHookId,
    frameCreateContext: (context: Partial<StepCreateContext>) => frameCreateContext(effectiveHookId, context),
    frames,
    framesByHook,
    memberStepId,
    refetchChild,
    route,
    scopeActive,
    scopePending,
    scopedCreateContext: scopeActive ? childCreateContext(stepCreateContext) : stepCreateContext,
    /** Selection and focus in canvas terms, from the child's bare step ids. */
    selectChildGroup: (groupId: string) => {
      setSelectedGroupId(groupId ? hookMemberId(effectiveHookId, groupId) : "");
      if (!groupId && drawerMode === "group") setDrawerMode("");
    },
    selectChildStep: (stepId: string) => setSelectedStepId(stepId ? hookMemberId(effectiveHookId, stepId) : ""),
    focusChildNode: (stepId: string, options?: { place?: boolean }) => focusCanvasNode?.(hookMemberId(effectiveHookId, stepId), options),
  };
}

/** A selection in another hook than the active one, once the active one is chosen, waits for a switch. */
function requestedBlocked(requestedHookId: string, activeHookId: string): boolean {
  return Boolean(activeHookId) && requestedHookId !== activeHookId;
}

function textOf(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export type WorkflowHookScope = ReturnType<typeof useWorkflowHookScope>;
