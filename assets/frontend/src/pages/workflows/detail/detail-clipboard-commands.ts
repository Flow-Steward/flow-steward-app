import type { CanvasPosition } from "@/pages/workflows/builder/canvas-core";
import { decodeBuilderClipboard, encodeBuilderSelection, remapBuilderClipboard, type BuilderClipboardV1 } from "@/pages/workflows/builder/canvas-clipboard";
import { deletionOperation, deriveDeletionClosure, type DeletionClosure, type DeletionScope } from "@/pages/workflows/builder/canvas-delete-guard";
import { deriveDeletionBatch, validDeleteBatchSelection, type DeleteBatchSelection, type DeletionBatch } from "@/pages/workflows/builder/canvas-delete-batch";
import { FOR_EACH_ONLY_STEP_KINDS, groupBodyKind, placementOwnerFields, stepKindOf, type WorkflowDefinitionModel, type WorkflowPlacement } from "@/pages/workflows/builder/change-set";
import { parseHookNodeId } from "@/pages/workflows/builder/hook-frames";
import { CANVAS_GRID } from "@/pages/workflows/builder/layout-constants";
import type { WorkflowChangeOperation, WorkflowStepPayload } from "@/pages/workflows/builder/api";
import type { GraphIntentToken } from "@/pages/workflows/builder/canvas-history";
import type { WorkflowNodeRef } from "@/pages/workflows/shared/api";
import { workflowHookPlacementInvalid } from "@/pages/workflows/shared/workflow-hook-placement";
import type { DeleteGuardBatchCommandContext, DeleteGuardCommandContext, DeleteGuardSnapshot, useWorkflowDeletionGuard } from "./detail-canvas-handlers";
import { validateCreationPlacement } from "./detail-local-step-creation";

export type ClipboardCommand = "copy" | "cut" | "paste" | "duplicate";
export type ClipboardAuthority = DeleteGuardSnapshot & {
  canvasWorkflowId: string;
  viewKey: string;
  generation: number;
  authoringAllowed: boolean;
  targets: readonly { ref: WorkflowNodeRef; canvasId: string }[];
  worldPositions: ReadonlyMap<string, CanvasPosition>;
  placement: WorkflowPlacement;
  /** Captured canvas-world origin; pointer uses it exactly, offset adds grid steps. */
  pasteOrigin: CanvasPosition;
  pasteMode: "pointer" | "offset";
  /** Already below the measured source union, supplied by the actual canvas. */
  duplicateOrigin: CanvasPosition;
};
type Failure = "unavailable" | "busy" | "stale" | "guard_refused" | "invalid_target" | "clipboard_failed" |
  "bulk_cut_unavailable" | "unsupported_format" | "invalid_fragment" | "unrepresentable_selection" | "id_allocation_failed" | "visual_commit_failed";
export type ClipboardCommandResult = {
  ok: boolean;
  command: ClipboardCommand;
  reason?: Failure;
  staged: boolean;
  clipboardWritten: boolean;
  authority?: { ownerKey: string; workflowId: string; projectId: string; canvasWorkflowId: string; viewKey: string; generation: number };
};
export type ClipboardCommit = {
  command: "cut" | "paste" | "duplicate";
  authority: ClipboardAuthority;
  intentToken: GraphIntentToken;
  removed?: DeletionClosure;
  roots: WorkflowNodeRef[];
  rootLabels: readonly string[];
  /** Absolute display canvas-world coordinates; the bridge converts to parent-local once. */
  worldPositions: ReadonlyMap<string, CanvasPosition>;
  placement: WorkflowPlacement;
};
type Dependencies = {
  /** Committed reader: applies current authoring/Development/Hook policies, including Copy fallback. */
  readCurrent: (command: ClipboardCommand) => ClipboardAuthority | null;
  clipboard: Pick<Clipboard, "readText" | "writeText"> | null;
  guard: Pick<ReturnType<typeof useWorkflowDeletionGuard>, "requestDelete"> &
    Partial<Pick<ReturnType<typeof useWorkflowDeletionGuard>, "requestDeleteBatch">>;
  openProtectedScope: (authority: ClipboardAuthority, scope: DeletionScope) => Promise<boolean>;
  /** Synchronous visual setters only; no extra graph/placement history entry. */
  onCommitted: (commit: ClipboardCommit) => undefined;
  copyLabel: (label: string, ordinal: number) => string;
};
const finitePoint = (point: CanvasPosition) => Number.isFinite(point.x) && Number.isFinite(point.y);
const samePlacement = (a: WorkflowPlacement, b: WorkflowPlacement) => a.groupId === b.groupId && a.forEachId === b.forEachId && a.laneId === b.laneId;
function sameAuthority(expected: ClipboardAuthority, current: ClipboardAuthority | null): current is ClipboardAuthority {
  return Boolean(current && current.authoringAllowed && !current.owner.structureLocked &&
    current.canvasWorkflowId === expected.canvasWorkflowId && current.viewKey === expected.viewKey && current.generation === expected.generation &&
    current.owner.ownerKey === expected.owner.ownerKey && current.owner.workflowId === expected.owner.workflowId &&
    current.owner.projectId === expected.owner.projectId && current.owner.selectionId === expected.owner.selectionId &&
    current.owner.serverRevision === expected.owner.serverRevision && current.changeSet.baseRevision === expected.changeSet.baseRevision &&
    current.changeSet.operations === expected.changeSet.operations && current.changeSet.preview === expected.changeSet.preview &&
    current.changeSet.status === expected.changeSet.status && current.changeSet.status !== "applying" && current.changeSet.status !== "conflict" &&
    samePlacement(current.placement, expected.placement) && current.targets.length === expected.targets.length &&
    current.targets.every((target, index) => target.canvasId === expected.targets[index].canvasId &&
      target.ref.kind === expected.targets[index].ref.kind && target.ref.id === expected.targets[index].ref.id));
}
/** The journal publication barrier belongs to a graph, not its visual selection. */
type GraphPublicationProof = {
  workflowId: string;
  projectId: string;
  changeSetWorkflowId: string;
  operations: ClipboardAuthority["changeSet"]["operations"];
  preview: ClipboardAuthority["changeSet"]["preview"];
  baseRevision: ClipboardAuthority["changeSet"]["baseRevision"];
};
function graphPublicationProof(authority: ClipboardAuthority): GraphPublicationProof {
  return { workflowId: authority.owner.workflowId, projectId: authority.owner.projectId,
    changeSetWorkflowId: authority.changeSet.workflowId, operations: authority.changeSet.operations,
    preview: authority.changeSet.preview, baseRevision: authority.changeSet.baseRevision };
}
function sameGraphPublication(expected: GraphPublicationProof, current: ClipboardAuthority): boolean {
  return expected.workflowId === current.owner.workflowId && expected.projectId === current.owner.projectId &&
    expected.changeSetWorkflowId === current.changeSet.workflowId && expected.operations === current.changeSet.operations &&
    expected.preview === current.changeSet.preview && expected.baseRevision === current.changeSet.baseRevision;
}
/** Refuse mixed projected owners; this is not a grant of Hook edit permission. */
function wellScopedTargets(value: ClipboardAuthority): boolean {
  const owners = new Set<string>();
  for (const target of value.targets) {
    const member = parseHookNodeId(target.canvasId);
    if (member) {
      if (!member.memberStepId || member.hookId !== value.viewKey || member.memberStepId !== target.ref.id) return false;
      owners.add("hook:" + member.hookId);
    } else owners.add("author");
  }
  return owners.size <= 1;
}
function eligible(value: ClipboardAuthority | null, command: ClipboardCommand): value is ClipboardAuthority {
  const revision = value?.changeSet.baseRevision ?? value?.owner.serverRevision;
  return Boolean(value && value.authoringAllowed && !value.owner.structureLocked && value.owner.workflowId && value.owner.ownerKey && value.changeSet.workflowId === value.owner.workflowId &&
    Number.isSafeInteger(value.generation) && value.generation >= 0 && value.canvasWorkflowId &&
    Number.isInteger(revision) && Number(revision) > 0 && value.changeSet.status !== "applying" && value.changeSet.status !== "conflict" &&
    wellScopedTargets(value) && (command === "paste" || value.targets.length));
}
/** Capture scalar proof fields and geometry; keep immutable operation/preview identities. */
function captureAuthority(value: ClipboardAuthority): ClipboardAuthority {
  return { ...value, owner: { ...value.owner }, changeSet: { ...value.changeSet },
    targets: value.targets.map((target) => ({ ...target, ref: { ...target.ref } })),
    placement: { ...value.placement }, pasteOrigin: { ...value.pasteOrigin }, duplicateOrigin: { ...value.duplicateOrigin },
    worldPositions: new Map([...value.worldPositions].map(([key, point]) => [key, { ...point }])),
  };
}
function sourceText(authority: ClipboardAuthority) {
  return encodeBuilderSelection({ definition: authority.changeSet.preview as WorkflowDefinitionModel,
    targets: authority.targets.map((target) => target.ref), worldPositions: authority.worldPositions });
}

/** The page batch reader uses this same displayed owner/selection identity. */
function cutBatchSelection(authority: ClipboardAuthority): DeleteBatchSelection | null {
  const canvasIds = authority.targets.map((target) => target.canvasId);
  const selection = {
    canvasIds, generation: authority.generation,
    ownerScopeKey: JSON.stringify([authority.canvasWorkflowId, authority.owner.projectId,
      authority.owner.workflowId, authority.viewKey]),
  };
  return authority.owner.selectionId === JSON.stringify(canvasIds) && validDeleteBatchSelection(selection)
    ? selection : null;
}

/** Target topology uses the current creation policies. Readiness remains a server concern. */
function creationBatch(payload: BuilderClipboardV1, authority: ClipboardAuthority, command: "paste" | "duplicate", copyLabel: Dependencies["copyLabel"]) {
  const preview = authority.changeSet.preview as WorkflowDefinitionModel;
  validateCreationPlacement(preview, authority.placement);
  const mapped = remapBuilderClipboard(payload, preview);
  if (!mapped.ok) return mapped;
  const fragment = mapped.value.definition;
  const rootIds = new Set(mapped.value.roots.map((ref) => ref.id));
  const placement = authority.placement;
  const forEach = preview.forEach.find((row) => row.for_each_id === placement.forEachId);
  const parent = preview.groups.find((row) => row.group_id === placement.groupId);
  for (const root of mapped.value.roots) {
    if (root.kind === "group" && (placement.groupId || placement.forEachId || placement.laneId)) return { ok: false as const, reason: "invalid_target" as const };
    if (root.kind === "for_each" && (placement.forEachId || placement.laneId || parent && groupBodyKind(parent) !== "sequence")) return { ok: false as const, reason: "invalid_target" as const };
  }
  for (const step of fragment.steps) if (rootIds.has(step.step_id!)) {
    if (FOR_EACH_ONLY_STEP_KINDS.has(stepKindOf(step)) && (!forEach || groupBodyKind(forEach) !== "sequence" || placement.laneId) ||
      workflowHookPlacementInvalid(step.step_kind, step.override, placement)) return { ok: false as const, reason: "invalid_target" as const };
    Object.assign(step, placementOwnerFields(placement));
  }
  for (const row of fragment.forEach) if (rootIds.has(row.for_each_id)) row.group_id = placement.groupId || null;
  if (command === "duplicate") {
    const used = new Set([...preview.steps, ...preview.groups, ...preview.forEach].map((row) => String(row.display_name || "")));
    const labelled = [...fragment.steps.map((row) => ({ row, nodeId: row.step_id })),
      ...fragment.groups.map((row) => ({ row, nodeId: row.group_id })),
      ...fragment.forEach.map((row) => ({ row, nodeId: row.for_each_id }))];
    for (const { row, nodeId } of labelled) {
      if (!rootIds.has(String(nodeId))) continue;
      const base = String(row.display_name || nodeId);
      let label = "";
      for (let ordinal = 1; ordinal <= used.size + 1; ordinal += 1) {
        const candidate = copyLabel(base, ordinal);
        if (candidate.trim() && !used.has(candidate)) { label = candidate; break; }
      }
      if (!label) return { ok: false as const, reason: "invalid_target" as const };
      row.display_name = label;
      used.add(label);
    }
  }
  const operations: WorkflowChangeOperation[] = [
    ...fragment.groups.map((group) => ({ op: "create_group" as const, group })),
    ...fragment.forEach.map((for_each) => ({ op: "create_for_each" as const, for_each })),
    ...fragment.steps.map((step) => ({ op: "create_step" as const, step: { ...step, step_id: step.step_id!, step_kind: stepKindOf(step) } as WorkflowStepPayload })),
    ...fragment.graph.edges.map((edge) => ({ op: "connect" as const, edge })),
  ];
  return { ok: true as const, value: { operations, roots: mapped.value.roots, relativeWorldPositions: mapped.value.relativeWorldPositions } };
}

/** OS permissions and shared Retry/notice rendering are supplied by future owner wiring.
 * Multi-root Cut is deliberately unavailable until an exact combined guard exists. */
export function createWorkflowClipboardCommands(deps: Dependencies) {
  let sequence = 0;
  let active: { token: number; command: ClipboardCommand; authority: ClipboardAuthority } | null = null;
  let retryAuthority: ClipboardAuthority | null = null;
  let awaitingPublication: GraphPublicationProof | null = null;
  let pasteCount = 0;
  let pasteNamespace = "";
  const invalidate = () => { sequence += 1; active = null; retryAuthority = null; pasteCount = 0; pasteNamespace = ""; };
  const result = (command: ClipboardCommand, reason?: Failure, clipboardWritten = false, staged = false, authority?: ClipboardAuthority): ClipboardCommandResult =>
    ({ ok: reason === undefined, command, reason, clipboardWritten, staged, authority: authority ? {
      ownerKey: authority.owner.ownerKey, workflowId: authority.owner.workflowId, projectId: authority.owner.projectId,
      canvasWorkflowId: authority.canvasWorkflowId, viewKey: authority.viewKey, generation: authority.generation,
    } : undefined });
  const commit = (authority: ClipboardAuthority, event: Omit<ClipboardCommit, "intentToken" | "rootLabels">, operations: WorkflowChangeOperation[], written: boolean) => {
    const intentToken = authority.changeSet.stageGraphIntent(operations);
    if (!intentToken) return result(event.command, "stale", written, false, authority);
    awaitingPublication = graphPublicationProof(authority);
    const rootLabels = event.roots.map((root) => {
      const operation = root.kind === "step"
        ? operations.find((item) => item.op === "create_step" && item.step.step_id === root.id)
        : root.kind === "group"
          ? operations.find((item) => item.op === "create_group" && item.group.group_id === root.id)
          : operations.find((item) => item.op === "create_for_each" && item.for_each.for_each_id === root.id);
      const label = operation?.op === "create_step" ? operation.step.display_name
        : operation?.op === "create_group" ? operation.group.display_name
          : operation?.op === "create_for_each" ? operation.for_each.display_name : root.id;
      return String(label || root.id).trim() || root.id;
    });
    try { deps.onCommitted({ ...event, rootLabels, intentToken }); } catch { return result(event.command, "visual_commit_failed", written, true, authority); }
    return result(event.command, undefined, written, true, authority);
  };
  const run = async (command: ClipboardCommand, expectedRetry?: ClipboardAuthority): Promise<ClipboardCommandResult> => {
    // A new owner command must not be stuck behind an old owner's pending promise.
    if (active && !sameAuthority(active.authority, deps.readCurrent(active.command))) invalidate();
    if (active) return result(command, "busy", false, false, active.authority);
    const live = deps.readCurrent(command);
    if (!eligible(live, command)) return result(command, "unavailable");
    const captured = captureAuthority(live);
    const complete = (reason?: Failure, written = false) => result(command, reason, written, false, captured);
    // Selection/view changes and invalidate cannot publish a staged journal.
    // Retire this barrier only when the owning graph proof actually changes.
    if (awaitingPublication && sameGraphPublication(awaitingPublication, captured)) return complete("busy");
    awaitingPublication = null;
    if (expectedRetry && !sameAuthority(expectedRetry, captured)) return complete("stale");
    const namespace = JSON.stringify([captured.owner.projectId, captured.owner.workflowId, captured.canvasWorkflowId, captured.viewKey, captured.placement]);
    if (namespace !== pasteNamespace) { pasteNamespace = namespace; pasteCount = 0; }
    const token = ++sequence;
    active = { token, command, authority: captured };
    retryAuthority = null;
    const currentAuthority = () => {
      // A refused guard keeps this proof for its notice and Open Automation action.
      // The busy slot ends in finally; a newer command or invalidate retires proof.
      if (sequence !== token) return null;
      const latest = deps.readCurrent(command);
      return sameAuthority(captured, latest) ? latest : null;
    };
    const current = () => currentAuthority() !== null;
    try {
      if (command === "copy" || command === "cut") {
        if (!deps.clipboard || typeof deps.clipboard.writeText !== "function") return complete("unavailable");
        if (command === "cut" && captured.targets.length === 1 && captured.owner.selectionId !== captured.targets[0].ref.id) return complete("unavailable");
        const encoded = sourceText(captured);
        if (!encoded.ok) return complete(encoded.reason);
        let removed: DeletionClosure | undefined;
        let deleteOperations: WorkflowChangeOperation[] = [];
        if (command === "cut") {
          if (captured.targets.length === 1) {
            const target = captured.targets[0].ref;
            removed = deriveDeletionClosure(captured.changeSet.preview as WorkflowDefinitionModel, target) ?? undefined;
            if (!removed) return complete("unrepresentable_selection");
            let authorized = false;
            const context: DeleteGuardCommandContext = { captured,
              current: currentAuthority,
              openProtectedScope: (scope) => current() ? deps.openProtectedScope(captured, scope) : Promise.resolve(false),
            };
            let allowed = false;
            try { allowed = await deps.guard.requestDelete(target, () => { authorized = true; }, context); } catch { /* Fail closed. */ }
            if (!current()) return complete("stale");
            if (!allowed || !authorized) { retryAuthority = captured; return complete("guard_refused"); }
            deleteOperations = [deletionOperation(target)];
          } else {
            const requestBatch = deps.guard.requestDeleteBatch;
            if (!requestBatch) return complete("bulk_cut_unavailable");
            const selection = cutBatchSelection(captured);
            const canonical = deriveDeletionBatch(captured.changeSet.preview as WorkflowDefinitionModel,
              captured.targets.map((target) => target.ref));
            if (!selection || !canonical) return complete("unrepresentable_selection");
            const context: DeleteGuardBatchCommandContext = {
              captured, selection, current: currentAuthority,
              currentSelection: () => {
                const latest = currentAuthority();
                return latest ? cutBatchSelection(latest) : null;
              },
              openProtectedScope: (scope) => current() ? deps.openProtectedScope(captured, scope) : Promise.resolve(false),
            };
            let accepted: DeletionBatch | null = null;
            let allowed = false;
            try { allowed = await requestBatch(captured.targets.map((target) => target.ref),
              (batch) => { accepted = batch; }, context); } catch { /* Fail closed. */ }
            if (!current()) return complete("stale");
            if (!allowed || !accepted || JSON.stringify(accepted) !== JSON.stringify(canonical)) {
              retryAuthority = captured;
              return complete("guard_refused");
            }
            removed = canonical.closure;
            deleteOperations = canonical.operations;
          }
        }
        if (!current()) return complete("stale");
        try { await deps.clipboard.writeText(encoded.value); } catch {
          if (!current()) return complete("stale");
          if (command === "cut") retryAuthority = captured;
          return complete("clipboard_failed");
        }
        if (!current()) return complete("stale", true);
        if (command === "copy") return complete(undefined, true);
        return commit(captured, { command, authority: captured, removed, roots: [], worldPositions: new Map(), placement: captured.placement },
          deleteOperations, true);
      }
      let text: string;
      if (command === "paste") {
        if (!deps.clipboard || typeof deps.clipboard.readText !== "function") return complete("unavailable");
        try { text = await deps.clipboard.readText(); } catch { return complete(current() ? "clipboard_failed" : "stale"); }
        if (!current()) return complete("stale");
      } else {
        const encoded = sourceText(captured);
        if (!encoded.ok) return complete(encoded.reason);
        text = encoded.value;
      }
      const decoded = decodeBuilderClipboard(text);
      if (!decoded.ok) return complete(decoded.reason);
      let batch: ReturnType<typeof creationBatch>;
      try { batch = creationBatch(decoded.value, captured, command, deps.copyLabel); } catch { return complete("invalid_target"); }
      if (!batch.ok) return complete(batch.reason);
      const base = command === "duplicate" ? captured.duplicateOrigin : captured.pasteOrigin;
      if (!finitePoint(base)) return complete("invalid_target");
      const offset = command === "paste" && captured.pasteMode === "offset" ? CANVAS_GRID * (pasteCount + 1) : 0;
      const world = new Map<string, CanvasPosition>();
      for (const [key, point] of Object.entries(batch.value.relativeWorldPositions)) {
        const position = { x: base.x + point.x + offset, y: base.y + point.y + offset };
        if (!finitePoint(position)) return complete("invalid_target");
        world.set(key, position);
      }
      if (!current()) return complete("stale");
      const accepted = commit(captured, { command, authority: captured, roots: batch.value.roots, worldPositions: world, placement: captured.placement }, batch.value.operations, false);
      if (accepted.staged && command === "paste" && captured.pasteMode === "offset") pasteCount += 1;
      return accepted;
    } finally {
      if (active?.token === token) active = null;
    }
  };
  return {
    copy: () => run("copy"), cut: () => run("cut"), paste: () => run("paste"), duplicate: () => run("duplicate"),
    retryCut: () => retryAuthority ? run("cut", retryAuthority) : Promise.resolve(result("cut", "unavailable")),
    invalidate,
  };
}
