import { applyAgentBlockIntent } from "./detail-agent-step-fields";
import type { WorkflowChangeSetController } from "./detail-change-set";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { emptyGroupDraft } from "./detail-drawer-controller";
import { groupBodyFromDraft, forEachSettingsFromDraft } from "./detail-group-actions";
import type { WorkflowExitRef, WorkflowGroup, WorkflowForEach } from "@/pages/workflows/shared/api";
import type { CreateLocalStepRequest, LocalStepChoice, LocalContainerChoice, CanvasConfirmedCreation } from "@/pages/workflows/builder/canvas-types";
import { groupBodyEntryStepId, groupLanes, placementContainer, placementOwnerFields, portEdge, ROOT_PLACEMENT, stepInsertableBetween, stepPlacement, stepPorts, uniqueLocalId, type WorkflowDefinitionModel, type WorkflowPlacement } from "@/pages/workflows/builder/change-set";
import { nodeRef, sameNodeRef, sameOutcome, type WorkflowChangeOperation, type WorkflowStepPayload } from "@/pages/workflows/builder/api";
import { connectorInputMappingForSave, defaultOverrideForStepKind, overrideForStepSave } from "./detail-step-editing";
import { localizedSTEP_KIND_OPTIONS, objectOrEmpty, textOrEmpty } from "./detail-helpers";
import { errorRoutePolicyOperation, sourcePlacement } from "./detail-canvas-handlers";
import { workflowHookPlacementInvalid } from "@/pages/workflows/shared/workflow-hook-placement";
import { HOOK_END_STEP_ID } from "@/pages/workflows/shared/workflow-hooks";

type StepConfigFields = {
  selectedStep?: { raw?: unknown } | null;
  stepKindDraft?: string;
  stepBlockKey?: string;
  stepDisplayName?: string;
  stepAgentId?: string;
  stepOptional?: boolean;
  agentConfigPatch?: Record<string, unknown>;
};

export function stepConfigPatch(ctx: StepConfigFields, override: Record<string, unknown>) {
  const currentRaw = objectOrEmpty(ctx.selectedStep?.raw);
  const isAgentKind = ctx.stepKindDraft === "agent";
  const isConnectorKind = ctx.stepKindDraft === "connector";
  const connectorConfig = objectOrEmpty(override.connector);
  const invokeConfig = objectOrEmpty(override.invoke_workflow);
  return {
    block_key: isAgentKind ? ctx.stepBlockKey : "",
    display_name: ctx.stepDisplayName,
    agent_id: isAgentKind ? ctx.stepAgentId : "",
    connector_id: isConnectorKind
      ? textOrEmpty(connectorConfig.connector_id ?? currentRaw.connector_id)
      : "",
    connector_install_id: isConnectorKind
      ? textOrEmpty(connectorConfig.connector_install_id ?? currentRaw.connector_install_id)
      : "",
    workflow_id:
      ctx.stepKindDraft === "invoke_workflow"
        ? textOrEmpty(invokeConfig.workflow_id || currentRaw.workflow_id)
        : "",
    // AC17-R65: the stored step contract stops carrying platform plumbing. `connection_ref`
    // is resolved from the chosen connection and injected by the runtime, so persisting an
    // author-supplied copy only preserves a value that is overwritten before use -- and it
    // travels into workflow exports, where an installation-scoped conn_... means nothing.
    input_mapping_jsonb: isConnectorKind
      ? connectorInputMappingForSave(connectorConfig)
      : undefined,
    output_mapping_jsonb: isConnectorKind
      ? objectOrEmpty(connectorConfig.output_mapping)
      : undefined,
    connection_binding_jsonb: isConnectorKind
      ? objectOrEmpty(connectorConfig.connection_binding)
      : undefined,
    optional: ctx.stepOptional,
    override: overrideForStepSave(override, ctx.stepKindDraft ?? ""),
    ...(isAgentKind ? ctx.agentConfigPatch : {}),
  };
}


function sourceExists(preview: WorkflowDefinitionModel, insert: NonNullable<CreateLocalStepRequest["context"]["insert"]>): boolean {
  const { source, outcome } = insert;
  if (source.kind === "step") {
    const step = preview.steps.find((item) => item.step_id === source.id);
    return Boolean(step && stepPorts(step).some((port) => sameOutcome(port.outcome, outcome)));
  }
  const exists = source.kind === "group" ? preview.groups.some((item) => item.group_id === source.id) : preview.forEach.some((item) => item.for_each_id === source.id);
  return exists && ["success", "error"].includes(outcome.name);
}

/** Validate only structural scope. Missing parameters remain honest local drafts. */
export function validateCreationPlacement(preview: WorkflowDefinitionModel, placement: WorkflowPlacement): void {
  if (placement.groupId && placement.forEachId) throw new Error("Choose one Group or For each owner.");
  const owner = placement.groupId ? preview.groups.find((item) => item.group_id === placement.groupId) : placement.forEachId ? preview.forEach.find((item) => item.for_each_id === placement.forEachId) : undefined;
  if ((placement.groupId || placement.forEachId) && !owner) throw new Error("The requested container no longer exists.");
  const lanes = owner ? groupLanes(owner) : [];
  const body = objectOrEmpty(owner?.body);
  if (body.kind === "lanes") {
    if (!placement.laneId || !lanes.some((lane) => lane.lane_id === placement.laneId)) throw new Error("Choose an existing lane in this container.");
  } else if (placement.laneId) throw new Error("A lane belongs to a container with parallel lanes.");
}

/** A group, For each or lane with no entry yet starts with what is added there (Placing, grouping and moving steps). */
function placeLacksEntry(preview: WorkflowDefinitionModel, placement: WorkflowPlacement): boolean {
  const container = placementContainer(preview, placement);
  return Boolean(container) && !groupBodyEntryStepId(container, textOrEmpty(placement.laneId));
}

/** No entry and nothing placed there yet: what is added there is the whole body. */
function placeIsEmpty(preview: WorkflowDefinitionModel, placement: WorkflowPlacement): boolean {
  if (!placeLacksEntry(preview, placement)) return false;
  const inPlace = preview.steps.some((item) => {
    const own = stepPlacement(item);
    return placement.forEachId
      ? own.forEachId === placement.forEachId && own.laneId === placement.laneId
      : !own.forEachId && own.groupId === placement.groupId && own.laneId === placement.laneId;
  });
  const loopInGroup = !placement.forEachId && preview.forEach.some((item) => textOrEmpty(item.group_id) === placement.groupId);
  return !inPlace && !loopInGroup;
}

/** The end of the group, item or lane a placement sits in: where the first step of an empty body goes on to. */
function bodyExitRef(placement: WorkflowPlacement): WorkflowExitRef {
  const laneId = placement.laneId || null;
  return placement.forEachId ? { kind: "for_each_exit", for_each_id: placement.forEachId, lane_id: laneId } : { kind: "group_exit", group_id: placement.groupId, lane_id: laneId };
}

/** Where a hook's own steps end: its hidden end, or the one finishing step of a hook made before the hidden end existed. */
function hookEndRef(preview: WorkflowDefinitionModel, hookBody: boolean) {
  if (preview.steps.some((item) => textOrEmpty(item.step_id) === HOOK_END_STEP_ID)) return nodeRef("step", HOOK_END_STEP_ID);
  if (!hookBody) return null;
  const finishes = preview.steps.filter((item) => !stepPlacement(item).groupId && !stepPlacement(item).forEachId && item.step_kind === "stop_success");
  return finishes.length === 1 ? nodeRef("step", textOrEmpty(finishes[0].step_id)) : null;
}

/**
 * The one connection that ends a body which already has steps: into the group,
 * lane or For each exit, or into the end of a hook. None when the body is
 * empty, has no end yet, or ends on several branches (the author picks one).
 */
function bodyEndEdge(preview: WorkflowDefinitionModel, placement: WorkflowPlacement, hookBody: boolean) {
  const inBody = Boolean(placement.groupId || placement.forEachId);
  if (inBody && placeLacksEntry(preview, placement)) return null;
  const hookEnd = inBody ? null : hookEndRef(preview, hookBody);
  if (!inBody && (!hookEnd || sameNodeRef(preview.graph.entry, hookEnd))) return null;
  const end = inBody ? bodyExitRef(placement) : hookEnd;
  const ending = preview.graph.edges.filter((edge) => !edge.rework && sameNodeRef(edge.target, end));
  return ending.length === 1 ? ending[0] : null;
}

export function buildNewStepOperations({ preview, choice, context, expectedEdge, createAsEntry = false, hookBody = false, configPatch, t = fallbackContext.t }: Pick<CreateLocalStepRequest, "context" | "expectedEdge" | "createAsEntry"> & {
  /** The preview is a hook's own steps, which end in its finishing step rather than an exit. */
  hookBody?: boolean;
  choice: LocalStepChoice;
  preview: WorkflowDefinitionModel;
  configPatch?: Record<string, unknown>;
  t?: I18nContextValue["t"];
}): { step: WorkflowStepPayload; operations: WorkflowChangeOperation[]; placement: WorkflowPlacement } {
  if (!localizedSTEP_KIND_OPTIONS(t).some((option) => option.value === choice.stepKind)) throw new Error("This step kind is unavailable.");
  const insert = context.insert;
  if (insert && !sourceExists(preview, insert)) throw new Error("The source or its outcome no longer exists.");
  const placement = insert ? sourcePlacement(preview, insert.source) : { ...ROOT_PLACEMENT, groupId: context.groupId || "", forEachId: context.forEachId || "", laneId: context.laneId || "" };
  if (insert && ["groupId", "forEachId", "laneId"].some((key) => {
    const field = key as keyof WorkflowPlacement;
    return context[field] !== undefined && (context[field] || "") !== placement[field];
  })) throw new Error("The source has moved to a different scope. Reopen Add step.");
  validateCreationPlacement(preview, placement);
  const forEach = preview.forEach.find((item) => item.for_each_id === placement.forEachId);
  if (["collect", "break", "continue"].includes(choice.stepKind) && (!forEach || objectOrEmpty(forEach.body).kind !== "sequence" || placement.laneId)) throw new Error("This control belongs directly in a sequence For each.");
  const chosen = choice.override ?? objectOrEmpty(JSON.parse(defaultOverrideForStepKind(choice.stepKind) || "{}"));
  let supplied = choice.stepKind === "agent" && choice.blockKey ? applyAgentBlockIntent(chosen, choice.blockKey) : chosen;
  if (choice.catalogKey === "hook:declaration") {
    const hook = objectOrEmpty(objectOrEmpty(supplied.invoke_workflow).hook);
    const existingIds = preview.steps.map((item) => textOrEmpty(objectOrEmpty(objectOrEmpty(objectOrEmpty(item.override).invoke_workflow).hook).hook_id));
    const hookId = textOrEmpty(hook.hook_id) || uniqueLocalId("hook", existingIds);
    supplied = { invoke_workflow: { hook: { ...hook, hook_id: hookId } } };
  }
  if (workflowHookPlacementInvalid(choice.stepKind, supplied, placement)) throw new Error("Hooks belong in the workflow root.");
  const override = choice.stepKind === "connector" ? { ...supplied, connector: { ...objectOrEmpty(supplied.connector), input_mapping: connectorInputMappingForSave(objectOrEmpty(supplied.connector)) } } : supplied;
  const patch = configPatch ?? stepConfigPatch({ stepKindDraft: choice.stepKind, stepDisplayName: choice.displayName, stepAgentId: choice.agentId || "", stepBlockKey: choice.blockKey || "", stepOptional: false }, override);
  const stepId = uniqueLocalId(choice.displayName, [...preview.steps.map((item) => textOrEmpty(item.step_id)), ...preview.groups.map((item) => textOrEmpty(item.group_id)), ...preview.forEach.map((item) => textOrEmpty(item.for_each_id))]);
  const step: WorkflowStepPayload = { ...Object.fromEntries(Object.entries(patch).filter(([, value]) => value !== undefined)), ...placementOwnerFields(placement), step_id: stepId, step_kind: choice.stepKind };
  const existing = insert ? portEdge(preview, insert.source, insert.outcome) : null;
  if (expectedEdge && (!insert || !existing || !sameNodeRef(expectedEdge.source, insert.source) || !sameOutcome(expectedEdge.outcome, insert.outcome) || !sameNodeRef(existing.target, expectedEdge.target) || Boolean(existing.rework) !== Boolean(expectedEdge.rework))) throw new Error("The connection changed. Reopen Add step.");
  if (insert?.append && existing) throw new Error("This outcome is now occupied. Reopen Add step.");
  if (existing && !stepInsertableBetween(step)) throw new Error("Only a step with one ordinary Next outcome can be inserted here.");
  const operations: WorkflowChangeOperation[] = [];
  if (insert?.outcome.name === "error" && !existing) operations.push(errorRoutePolicyOperation(preview, insert.source, "route_to_step", t));
  const bodyEnd = insert ? null : bodyEndEdge(preview, placement, hookBody);
  if (insert && existing) operations.push({ op: "insert_step_between", source: insert.source, outcome: insert.outcome, step });
  // Add step at the bottom of a body goes last: between the step that ends it and its end (R3-6).
  else if (bodyEnd && !createAsEntry && stepInsertableBetween(step)) operations.push({ op: "insert_step_between", source: bodyEnd.source, outcome: bodyEnd.outcome, step });
  else {
    operations.push({ op: "create_step", step });
    if (insert) operations.push({ op: "connect", edge: { source: insert.source, outcome: insert.outcome, target: nodeRef("step", stepId) } });
    else if ((placement.groupId || placement.forEachId) && (createAsEntry || placeLacksEntry(preview, placement))) {
      const emptyBody = placeIsEmpty(preview, placement);
      operations.push({ op: "set_entry", target: nodeRef("step", stepId), ...(placement.groupId ? { group_id: placement.groupId } : {}), ...(placement.forEachId ? { for_each_id: placement.forEachId } : {}), ...(placement.laneId ? { lane_id: placement.laneId } : {}) });
      // The only step of a body also ends it, so the new body is complete as it stands.
      if (emptyBody && stepInsertableBetween(step)) operations.push({ op: "connect", edge: { source: nodeRef("step", stepId), outcome: { name: "success" }, target: bodyExitRef(placement) } });
    }
  }
  return { step, operations, placement };
}


export type LocalContainerBuildResult = {
  nodeId: string;
  operations: WorkflowChangeOperation[];
  placement: WorkflowPlacement;
} & ({ nodeKind: "group"; container: WorkflowGroup } | { nodeKind: "for_each"; container: WorkflowForEach });

/** Genuine container operations share the form's canonical body/settings policies. */
export function buildNewContainerOperations({ preview, choice, context, expectedEdge, createAsEntry = false, t = fallbackContext.t }: Pick<CreateLocalStepRequest, "context" | "expectedEdge" | "createAsEntry"> & { preview: WorkflowDefinitionModel; choice: LocalContainerChoice; t?: I18nContextValue["t"] }): LocalContainerBuildResult {
  const insert = context.insert;
  if (insert && !sourceExists(preview, insert)) throw new Error("The source or its outcome no longer exists.");
  const placement = insert ? sourcePlacement(preview, insert.source) : { ...ROOT_PLACEMENT, groupId: context.groupId || "", forEachId: context.forEachId || "", laneId: context.laneId || "" };
  if (insert && (["groupId", "forEachId", "laneId"] as const).some((key) => context[key] !== undefined && (context[key] || "") !== placement[key])) throw new Error("The source has moved to a different scope. Reopen Add step.");
  validateCreationPlacement(preview, placement);
  if (expectedEdge || (insert && portEdge(preview, insert.source, insert.outcome))) throw new Error("Containers cannot be inserted into an occupied connection.");
  if (placement.forEachId || placement.laneId || (choice.nodeKind === "group" && placement.groupId)) throw new Error("This container is unavailable in the selected scope.");
  const parent = placement.groupId ? preview.groups.find((item) => item.group_id === placement.groupId) : undefined;
  if (parent && objectOrEmpty(parent.body).kind !== "sequence") throw new Error("A For each belongs directly in a sequence Group.");
  if (choice.containerDefaults?.parentGroupId && choice.containerDefaults.parentGroupId !== placement.groupId) throw new Error("The selected parent has changed. Reopen Add step.");
  if (choice.containerDefaults?.containerKind && choice.containerDefaults.containerKind !== choice.nodeKind) throw new Error("Choose the requested container kind.");
  const draft = emptyGroupDraft({ ...choice.containerDefaults, containerKind: choice.nodeKind, parentGroupId: placement.groupId });
  // No invented selector makes this incomplete local draft appear ready to apply.
  draft.over = "";
  const nodeId = uniqueLocalId(choice.displayName, [...preview.steps.map((item) => textOrEmpty(item.step_id)), ...preview.groups.map((item) => textOrEmpty(item.group_id)), ...preview.forEach.map((item) => textOrEmpty(item.for_each_id))], choice.nodeKind);
  const shared = { display_name: choice.displayName.trim() || nodeId, body: groupBodyFromDraft(draft) };
  const operations: WorkflowChangeOperation[] = [];
  if (insert?.outcome.name === "error") operations.push(errorRoutePolicyOperation(preview, insert.source, "route_to_step", t));
  const result: LocalContainerBuildResult = choice.nodeKind === "group"
    ? { nodeKind: "group", nodeId, container: { ...shared, group_id: nodeId }, operations, placement }
    : { nodeKind: "for_each", nodeId, container: { ...shared, for_each_id: nodeId, group_id: placement.groupId || null, ...(forEachSettingsFromDraft(draft) as Pick<WorkflowForEach, "over" | "item_name">) }, operations, placement };
  if (result.nodeKind === "group") operations.push({ op: "create_group", group: result.container });
  else operations.push({ op: "create_for_each", for_each: result.container });
  if (insert) operations.push({ op: "connect", edge: { source: insert.source, outcome: insert.outcome, target: nodeRef(result.nodeKind, nodeId) } });
  else if (createAsEntry || (placement.groupId && placeLacksEntry(preview, placement))) {
    const emptyBody = Boolean(placement.groupId) && placeIsEmpty(preview, placement);
    operations.push({ op: "set_entry", ...(placement.groupId ? { group_id: placement.groupId } : {}), target: nodeRef(result.nodeKind, nodeId) });
    if (emptyBody) operations.push({ op: "connect", edge: { source: nodeRef(result.nodeKind, nodeId), outcome: { name: "success" }, target: bodyExitRef(placement) } });
  }
  return result;
}

/** A committed lifecycle capability; presentation composites are never owners. */
export type LocalCreationOwner = {
  workflowId: string;
  hookId: string;
  revision: number;
  generation: number;
  preview: WorkflowDefinitionModel;
  stage: WorkflowChangeSetController["stage"];
  stageGraphIntent: WorkflowChangeSetController["stageGraphIntent"];
  viewEpoch: number;
  toCanvasId: (id: string) => string;
  focus?: (canvasId: string, options: { place: boolean; creation?: Omit<CanvasConfirmedCreation, "nodeId" | "nonce"> }) => void;
};
