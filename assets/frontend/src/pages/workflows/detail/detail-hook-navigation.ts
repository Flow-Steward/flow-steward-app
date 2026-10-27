import { nodeIdForRef } from "@/pages/workflows/builder/layout";
import { buildNewStepOperations, buildNewContainerOperations } from "./detail-local-step-creation";
import type { CreateLocalStepRequest, CreateLocalStepResult, LocalContainerChoice, StepEditorTab } from "@/pages/workflows/builder/canvas-types";
import { ROOT_PLACEMENT } from "@/pages/workflows/builder/change-set";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { SetURLSearchParams } from "react-router";
import { useI18n } from "@/lib/i18n/useI18n";
import { authorRef, childPlacement, childRef, hookGroupId, parseHookNodeId } from "@/pages/workflows/builder/hook-frames";
import type { WorkflowTargetRef } from "@/pages/workflows/shared/api";
import type { WorkflowGroupRow } from "./detail-helpers";
import type { WorkflowHookScope } from "./detail-hook-scope";
import type { StepCreateContext, useWorkflowDrawerController } from "./detail-drawer-controller";

type DrawerNavigation = Pick<ReturnType<typeof useWorkflowDrawerController>, "openGroupDrawer" | "openStepDrawer" | "openNewGroupDrawer" | "openNewStepDrawer" | "openCreatedStep" | "openCreatedContainer">;
type NavigationOptions = {
  hookScope: WorkflowHookScope;
  /** Parent workflow/project owner; optional for legacy callers. */
  ownerKey?: string;
  authorGroupRows: WorkflowGroupRow[];
  authorStructureLocked: boolean;
  allowHookDeclaration?: boolean;
  drawerMode: string;
  selectedGroupId: string;
  selectedStepId: string;
  confirmDiscardParameters: () => Promise<boolean>;
  searchParams: URLSearchParams;
  setSearchParams: SetURLSearchParams;
  setSaveError: (message: string) => void;
};

function isContainerChoice(choice: CreateLocalStepRequest["choice"]): choice is LocalContainerChoice {
  return choice.nodeKind === "group" || choice.nodeKind === "for_each";
}

/** Read all placement owners before a visual Hook root can disappear. */
function creationPlacementHook(scope: Pick<CreateLocalStepRequest["context"], "groupId" | "forEachId">): string | null | undefined {
  const groupId = scope.groupId || "";
  const forEachId = scope.forEachId || "";
  const group = parseHookNodeId(groupId);
  const forEach = parseHookNodeId(forEachId);
  if ((group && !group.hookId) || (forEach && (!forEach.hookId || !forEach.memberStepId))) return null;
  const actualGroup = Boolean(groupId && (!group || group.memberStepId));
  if (actualGroup && forEachId) return null;
  const owners: string[] = [];
  if (groupId) owners.push(group?.hookId || "");
  if (forEachId) owners.push(forEach?.hookId || "");
  return owners.every((owner) => owner === owners[0]) ? owners[0] : null;
}

/** The outer frame is author-owned; its canonical exit is child-owned only as a target. */
function creationReferenceHook(ref: WorkflowTargetRef, target = false): string | null {
  const id = ref.kind === "group_exit" ? ref.group_id : ref.kind === "for_each_exit" ? ref.for_each_id : ref.id;
  if (!id) return null;
  const parsed = parseHookNodeId(id);
  if (!parsed) return "";
  if (!parsed.hookId) return null;
  if (parsed.memberStepId) return parsed.hookId;
  if (target && ref.kind === "group_exit" && ref.group_id === hookGroupId(parsed.hookId) && ref.lane_id === null) return parsed.hookId;
  return ref.kind === "group" ? "" : null;
}

/** Pure identity preflight: no normalization, activation, async work or local writes. */
function creationRequestHook(request: CreateLocalStepRequest): string | null {
  const placementHook = creationPlacementHook(request.context);
  const source = request.context.insert?.source;
  const sourceHook = source ? creationReferenceHook(source) : undefined;
  if (placementHook === null || sourceHook === null) return null;
  const hookId = placementHook ?? sourceHook ?? "";
  if (sourceHook !== undefined && sourceHook !== hookId) return null;
  if (request.expectedEdge && (!source || creationReferenceHook(request.expectedEdge.source) !== hookId || creationReferenceHook(request.expectedEdge.target, true) !== hookId)) return null;
  if (request.intent) {
    const intentHook = creationPlacementHook(request.intent.scope);
    if (intentHook === null || (intentHook !== undefined && intentHook !== hookId) || creationReferenceHook(request.intent.source) !== hookId) return null;
  }
  return hookId;
}

/** One owner transition for canvas commands, drawers and projected deep links. */
export function useWorkflowHookNavigation({
  hookScope, ownerKey = hookScope.authorWorkflowId, authorGroupRows, authorStructureLocked, drawerMode, selectedGroupId, selectedStepId,
  confirmDiscardParameters, searchParams, setSearchParams, setSaveError, allowHookDeclaration = false,
}: NavigationOptions) {
  const { t } = useI18n();
  const navigationSerial = useRef({ value: 0 });
  const [renderOwner, setRenderOwner] = useState({ key: ownerKey, epoch: 0 });
  if (renderOwner.key !== ownerKey) setRenderOwner({ key: ownerKey, epoch: renderOwner.epoch + 1 });
  const committedOwner = useRef<typeof renderOwner | null>(null);
  useLayoutEffect(() => {
    const serial = navigationSerial.current;
    committedOwner.current = renderOwner;
    return () => {
      if (committedOwner.current === renderOwner) committedOwner.current = null;
      ++serial.value;
    };
  }, [renderOwner]);
  const activeCreation = useRef<AbortController | null>(null);
  const creationFlights = useRef(new Map<string, Promise<CreateLocalStepResult>>());
  const currentCreationPolicy = useRef({ hookScope, authorStructureLocked, allowHookDeclaration });
  useLayoutEffect(() => { currentCreationPolicy.current = { hookScope, authorStructureLocked, allowHookDeclaration }; }, [hookScope, authorStructureLocked, allowHookDeclaration]);
  useEffect(() => {
    const serial = navigationSerial.current;
    return () => { ++serial.value; activeCreation.current?.abort(); };
  }, []);
  const { frames: navigationFrames, canActivateHook, activateHook } = hookScope;
  const needsHookSelectionGuard = Boolean(navigationFrames.size || hookScope.effectiveHookId ||
    hookScope.childChangeSet.hasChanges || hookScope.childChangeSet.status === "applying");
  const prepareSelection = useCallback(async (mode: string, id: string, hookOverride?: string, isCurrentRequest: () => boolean = () => true) => {
    if (!isCurrentRequest()) return false;
    activeCreation.current?.abort(); activeCreation.current = null;
    const serial = ++navigationSerial.current.value;
    const parsed = parseHookNodeId(id);
    const frame = [...navigationFrames.values()].find((item) => item.stepId === id);
    const hookId = hookOverride ?? parsed?.hookId ?? frame?.hookId ?? "";
    if (hookId && !canActivateHook(hookId)) return false;
    const alreadyOpen = mode === drawerMode && (mode === "step" ? id === selectedStepId : mode === "group" && id === selectedGroupId);
    if (!alreadyOpen && !(await confirmDiscardParameters())) return false;
    if (serial !== navigationSerial.current.value || !isCurrentRequest()) return false;
    if (hookId && !activateHook(hookId)) return false;
    return true;
  }, [confirmDiscardParameters, drawerMode, navigationFrames, canActivateHook, activateHook, selectedGroupId, selectedStepId]);
  const pendingDeepLink = useRef("");
  useEffect(() => {
    const mode = searchParams.get("drawer") || "";
    const id = mode === "group" ? searchParams.get("group_id") || "" : mode === "step" ? searchParams.get("step_id") || "" : "";
    const parsed = parseHookNodeId(id);
    if (!parsed?.memberStepId || parsed.hookId === hookScope.effectiveHookId) {
      pendingDeepLink.current = "";
      return;
    }
    const key = `${mode}:${id}`;
    if (pendingDeepLink.current === key) return;
    pendingDeepLink.current = key;
    void Promise.resolve().then(async () => {
      const allowed = await prepareSelection(mode, id);
      if (allowed) return;
      setSearchParams((current) => {
        if ((current.get(mode === "group" ? "group_id" : "step_id") || "") !== id) return current;
        const next = new URLSearchParams(current);
        ["drawer", "step_id", "step_tab", "group_id", "group_tab"].forEach((name) => next.delete(name));
        return next;
      }, { replace: true });
    });
  }, [hookScope.effectiveHookId, prepareSelection, searchParams, setSearchParams]);
  const resolveGroupRow = useCallback((id: string): WorkflowGroupRow | null => {
    const parsed = parseHookNodeId(id);
    return parsed?.memberStepId
      ? hookScope.childRows.definitionRows.groupRows.find((row) => parsed.hookId === hookScope.effectiveHookId && row.groupId === parsed.memberStepId) || null
      : authorGroupRows.find((row) => row.groupId === id) || null;
  }, [authorGroupRows, hookScope.childRows.definitionRows.groupRows, hookScope.effectiveHookId]);

  const canvasNavigation = (drawer: DrawerNavigation) => ({
    createStep: (request: CreateLocalStepRequest): Promise<CreateLocalStepResult> => {
      const prior = creationFlights.current.get(request.requestId);
      if (prior) return prior;
      const command = async (): Promise<CreateLocalStepResult> => {
        if (request.signal?.aborted) return { status: "cancelled" };
        const hookId = creationRequestHook(request);
        if (hookId === null) return { status: "refused" };
        const invoke = !isContainerChoice(request.choice) && request.choice.stepKind === "invoke_workflow" ? request.choice.override?.invoke_workflow : undefined;
        const declaring = Boolean(invoke && typeof invoke === "object" && Object.hasOwn(invoke, "hook"));
        const declarationAllowed = () => !declaring || Boolean(currentCreationPolicy.current.allowHookDeclaration && !hookId && !request.context.groupId && !request.context.forEachId && !request.context.laneId);
        if (!declarationAllowed()) return { status: "refused" };
        if (!hookId && currentCreationPolicy.current.authorStructureLocked) return { status: "refused" };
        if (hookId && !currentCreationPolicy.current.hookScope.canActivateHook(hookId)) return { status: "refused" };
        // R2-10: without a project a hook has no place for steps; say so before anything changes.
        if (hookId && currentCreationPolicy.current.hookScope.framesByHook.get(hookId)?.needsProject) {
          const message = t("react.workflows.hook_choose_project_first", "Choose a project in the header first: each project keeps its own steps in a hook.");
          setSaveError(message);
          return { status: "refused", message };
        }
        if (!(await prepareSelection("new-step", "", hookId || undefined))) return { status: "cancelled" };
        const serial = navigationSerial.current.value;
        if (request.signal?.aborted) return { status: "cancelled" };
        if (!declarationAllowed()) return { status: "refused" };
        const controller = new AbortController(); activeCreation.current = controller;
        const abort = () => controller.abort(); request.signal?.addEventListener("abort", abort, { once: true });
        const current = () => !controller.signal.aborted && serial === navigationSerial.current.value;
        try {
          let policy = currentCreationPolicy.current;
          let workflowId = policy.hookScope.authorWorkflowId;
          if (hookId) {
            const frame = policy.hookScope.framesByHook.get(hookId);
            if (!frame || !policy.hookScope.canActivateHook(hookId) || !policy.hookScope.activateHook(hookId)) return { status: "refused" };
            workflowId = await policy.hookScope.ensureHookWorkflow(frame);
            if (!current()) return { status: "cancelled" };
            // The real reason (no project, a refused binding), not "resolve the current edit".
            if (!workflowId) return { status: "failed", message: policy.hookScope.creationFailure() || undefined };
          }
          policy = currentCreationPolicy.current;
          if (!current()) return { status: "cancelled" };
          if ((!hookId && policy.authorStructureLocked) || (hookId && !policy.hookScope.canActivateHook(hookId))) return { status: "refused" };
          if (!declarationAllowed()) return { status: "refused" };
          const owner = await policy.hookScope.acquireCreationOwner(hookId, workflowId, controller.signal);
          policy = currentCreationPolicy.current;
          if (!current() || owner?.hookId !== hookId || owner.workflowId !== workflowId || !policy.hookScope.isCreationOwnerCurrent(owner)) return { status: "cancelled" };
          if ((!hookId && policy.authorStructureLocked) || (hookId && !policy.hookScope.canActivateHook(hookId))) return { status: "refused" };
          if (!declarationAllowed()) return { status: "refused" };
          const mapRef = <T extends WorkflowTargetRef>(ref: T): T => hookId ? childRef(ref) : authorRef(ref, policy.hookScope.frames);
          const context = { ...request.context, ...(hookId ? childPlacement({ ...ROOT_PLACEMENT, ...request.context }) : {}), insert: request.context.insert ? { ...request.context.insert, source: mapRef(request.context.insert.source) } : null };
          const expectedEdge = request.expectedEdge ? { ...request.expectedEdge, source: mapRef(request.expectedEdge.source), target: mapRef(request.expectedEdge.target) } : undefined;
          if (isContainerChoice(request.choice)) {
            const result = buildNewContainerOperations({ preview: owner.preview, choice: request.choice, context, expectedEdge, createAsEntry: request.createAsEntry, t });
            if (!current() || !currentCreationPolicy.current.hookScope.isCreationOwnerCurrent(owner)) return { status: "cancelled" };
            const notice = result.nodeKind === "group"
              ? t("react.workflows.group_added_to_changes", "Group added to the unapplied structure. Add its steps, then apply.")
              : t("react.workflows.for_each_added_to_changes", "For each added to the unapplied structure. Add its steps, then apply.");
            const token = owner.stageGraphIntent(result.operations, notice);
            if (!token) return { status: "cancelled" };
            const projectedId = owner.toCanvasId(result.nodeId);
            const canvasNodeId = nodeIdForRef({ kind: result.nodeKind, id: projectedId });
            drawer.openCreatedContainer(result.container, projectedId, result.nodeKind);
            owner.focus?.(canvasNodeId, { place: !context.insert && !result.placement.groupId && !result.placement.forEachId,
              creation: { target: { kind: result.nodeKind, id: result.nodeId }, token, ownerWorkflowId: owner.workflowId,
                viewKey: owner.hookId, viewEpoch: owner.viewEpoch, intent: request.intent,
                point: request.intent?.point ?? request.at?.point, selectionGeneration: request.intent?.selectionGeneration } });
            return { status: "created", nodeKind: result.nodeKind, nodeId: result.nodeId, canvasNodeId };
          }
          const result = buildNewStepOperations({ preview: owner.preview, choice: request.choice, context, expectedEdge, createAsEntry: request.createAsEntry, hookBody: Boolean(hookId), t });
          if (!current() || !currentCreationPolicy.current.hookScope.isCreationOwnerCurrent(owner)) return { status: "cancelled" };
          if (!declarationAllowed()) return { status: "refused" };
          // No await between current-owner validation, one atomic stage, and
          // the prepared editor handoff. Missing inputs remain unapplied.
          const token = owner.stageGraphIntent(result.operations, t("react.workflows.step_added_to_changes", "Step added to the unapplied structure. Connect its outcomes, then apply."));
          if (!token) return { status: "cancelled" };
          const canvasStepId = owner.toCanvasId(result.step.step_id);
          if (request.choice.toolCategory) drawer.openCreatedStep(result.step, canvasStepId, request.choice.toolCategory);
          else drawer.openCreatedStep(result.step, canvasStepId);
          const declaration = result.step.step_kind === "invoke_workflow" ? result.step.override?.invoke_workflow : undefined;
          const declaredHookId = declaration && typeof declaration === "object" && "hook" in declaration ? (declaration.hook as { hook_id?: string })?.hook_id : "";
          const canvasNodeId = declaredHookId ? nodeIdForRef({ kind: "group", id: hookGroupId(declaredHookId) }) : canvasStepId;
          owner.focus?.(canvasNodeId, { place: !context.insert && !result.placement.groupId && !result.placement.forEachId,
            ...(!declaredHookId ? { creation: { target: { kind: "step" as const, id: result.step.step_id }, token,
              ownerWorkflowId: owner.workflowId, viewKey: owner.hookId, viewEpoch: owner.viewEpoch,
              intent: request.intent, point: request.intent?.point ?? request.at?.point,
              selectionGeneration: request.intent?.selectionGeneration } } : {}) });
          return { status: "created", stepId: result.step.step_id, canvasStepId, ...(declaredHookId ? { canvasNodeId } : {}) };
        } catch (error) {
          if (!current()) return { status: "cancelled" };
          const message = error instanceof Error ? error.message : t("react.workflows.palette_creation_unavailable", "Step creation is unavailable. Reopen the workflow and try again.");
          setSaveError(message); return { status: "failed", message };
        } finally {
          request.signal?.removeEventListener("abort", abort);
          if (activeCreation.current === controller) activeCreation.current = null;
        }
      };
      const promise = command(); creationFlights.current.set(request.requestId, promise);
      const clearFlight = () => { if (creationFlights.current.get(request.requestId) === promise) creationFlights.current.delete(request.requestId); };
      void promise.then(clearFlight, clearFlight);
      return promise;
    },
    openGroup: async (groupId: string, defaultTab = "settings") => {
      const parsed = parseHookNodeId(groupId);
      if (!parsed) return drawer.openGroupDrawer(groupId, defaultTab);
      if (!(await prepareSelection("group", groupId))) return;
      const frame = hookScope.framesByHook.get(parsed.hookId);
      if (!frame) return;
      if (!parsed.memberStepId) return drawer.openStepDrawer(frame.stepId, "", true);
      setSearchParams((current) => {
        const next = new URLSearchParams(current);
        next.set("drawer", "group"); next.set("group_id", groupId); next.set("group_tab", defaultTab);
        next.delete("step_id"); next.delete("step_tab");
        return next;
      }, { replace: true });
    },
    openNewStep: async (context: Partial<StepCreateContext> = {}, blockKeyOverride = "", defaults: Record<string, unknown> = {}) => {
      const parsed = parseHookNodeId(context.forEachId || context.groupId || "");
      if (!parsed) {
        if (authorStructureLocked) { setSaveError(t("react.workflows.structure_locked_hint", "This workflow is protected by its extension. You can read it and connect your own workflows to its hooks.")); return; }
        return drawer.openNewStepDrawer(context, blockKeyOverride, defaults);
      }
      if (!(await prepareSelection("new-step", "", parsed.hookId))) return;
      const frame = hookScope.framesByHook.get(parsed.hookId);
      if (!frame) return;
      const serial = navigationSerial.current.value;
      const childId = await hookScope.ensureHookWorkflow(frame);
      if (!childId || serial !== navigationSerial.current.value) return;
      return drawer.openNewStepDrawer(context, blockKeyOverride, defaults, true);
    },
    openNewGroup: async (defaults: Parameters<typeof drawer.openNewGroupDrawer>[0] = {}) => {
      const hookId = defaults.hookId || "";
      if (!hookId && authorStructureLocked) { setSaveError(t("react.workflows.structure_locked_hint", "This workflow is protected by its extension. You can read it and connect your own workflows to its hooks.")); return; }
      if (!hookId && !needsHookSelectionGuard) return drawer.openNewGroupDrawer(defaults);
      if (!(await prepareSelection("new-group", "", hookId))) return;
      const frame = hookId ? hookScope.framesByHook.get(hookId) : undefined;
      const serial = navigationSerial.current.value;
      if (frame && !(await hookScope.ensureHookWorkflow(frame))) return;
      if (serial !== navigationSerial.current.value) return;
      hookScope.setContainerCreationHookId(hookId);
      return drawer.openNewGroupDrawer({
        ...defaults,
        parentGroupId: defaults.parentGroupId ? parseHookNodeId(defaults.parentGroupId)?.memberStepId || defaults.parentGroupId : "",
      }, true);
    },
    openStep: async (stepId: string, blockKeyOverride = "", requestedTab: StepEditorTab = "smart-setup", isCurrentRequest: () => boolean = () => true) => {
      const owner = renderOwner;
      const isCurrent = () => committedOwner.current === owner && isCurrentRequest();
      if (!isCurrent()) return false;
      const parsed = parseHookNodeId(stepId);
      // Current-owner navigation uses the same awaited drawer guard. In particular,
      // cancelling invalid same-step input must not change the router first.
      if (!parsed?.memberStepId || (parsed.hookId === hookScope.effectiveHookId && hookScope.childRows.definitionRows.stepRowsById.has(parsed.memberStepId))) {
        const opened = await drawer.openStepDrawer(stepId, blockKeyOverride, false, requestedTab, isCurrent);
        if (opened && isCurrent() && searchParams.get("drawer") === "step") {
          const pendingId = searchParams.get("step_id");
          setSearchParams((current) => {
            if (!isCurrent() || current.get("drawer") !== "step" || current.get("step_id") !== pendingId) return current;
            const next = new URLSearchParams(current);
            ["drawer", "step_id", "step_tab"].forEach((name) => next.delete(name));
            return next;
          }, { replace: true });
        }
        return Boolean(opened && isCurrent());
      }
      if (!(await prepareSelection("step", stepId, undefined, isCurrent)) || !isCurrent()) return false;
      setSearchParams((current) => {
        if (!isCurrent()) return current;
        const next = new URLSearchParams(current);
        next.set("drawer", "step"); next.set("step_id", stepId);
        next.set("step_tab", requestedTab === "smart-setup" ? "smart-setup" : requestedTab);
        next.delete("group_id"); next.delete("group_tab");
        return next;
      }, { replace: true });
    },
  });
  return { prepareSelection, resolveGroupRow, canvasNavigation, needsHookSelectionGuard };
}
