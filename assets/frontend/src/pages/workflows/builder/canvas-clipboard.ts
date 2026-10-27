import type { CanvasPosition } from "./canvas-core";
import { deriveDeletionClosure } from "./canvas-delete-guard";
import { parseHookNodeId } from "./hook-frames";
import { uniqueLocalId, type WorkflowDefinitionModel } from "./change-set";
import { workflowStepDeclaresHook } from "@/pages/workflows/shared/workflow-hook-placement";
import type { WorkflowGraphEdge, WorkflowNodeRef, WorkflowTargetRef } from "@/pages/workflows/shared/api";

const FORMAT = "flow-steward/builder-selection";
const VERSION = 1;
type NodeKind = WorkflowNodeRef["kind"];
type JsonRecord = Record<string, unknown>;
type Reason = "unsupported_format" | "invalid_fragment" | "unrepresentable_selection" | "id_allocation_failed";
export type CodecResult<T> = { ok: true; value: T } | { ok: false; reason: Reason };
export type BuilderClipboardV1 = {
  format: typeof FORMAT;
  version: typeof VERSION;
  definition: WorkflowDefinitionModel;
  roots: WorkflowNodeRef[];
  relativeWorldPositions: Record<string, CanvasPosition>;
};
type Member = { kind: NodeKind; row: JsonRecord };
type Members = Map<string, Member>;
type Scope = { kind: "group" | "for_each"; id: string; lane: string | null } | null;
const fail = (reason: Reason): { ok: false; reason: Reason } => ({ ok: false, reason });
const own = (row: object, key: PropertyKey) => Object.hasOwn(row, key);
const record = (value: unknown): value is JsonRecord => Boolean(value && typeof value === "object" &&
  (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null));
const id = (value: unknown): value is string => typeof value === "string" && Boolean(value.trim()) && value === value.trim();
const keys = (value: JsonRecord, allowed: readonly string[]) => Object.keys(value).every((key) => allowed.includes(key));
const point = (value: unknown): value is CanvasPosition => record(value) && keys(value, ["x", "y"]) &&
  typeof value.x === "number" && Number.isFinite(value.x) && typeof value.y === "number" && Number.isFinite(value.y);
const positions = (): Record<string, CanvasPosition> => Object.create(null) as Record<string, CanvasPosition>;
const sameScope = (a: Scope, b: Scope) => a === null ? b === null : b !== null && a.kind === b.kind && a.id === b.id && a.lane === b.lane;

/** Reject values JSON would silently drop/change, and cycles; shared subobjects are allowed. */
function jsonValue(value: unknown, ancestors = new Set<object>()): boolean {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true;
  if (typeof value === "number") return Number.isFinite(value);
  if ((!Array.isArray(value) && !record(value)) || ancestors.has(value)) return false;
  ancestors.add(value);
  const valid = Array.isArray(value)
    ? Object.keys(value).length === value.length && Object.keys(value).every((key, index) => key === String(index)) && value.every((item) => jsonValue(item, ancestors))
    : Reflect.ownKeys(value).length === Object.keys(value).length && Object.values(value).every((item) => jsonValue(item, ancestors));
  ancestors.delete(value);
  return valid;
}
function jsonData(value: unknown): boolean {
  try { return jsonValue(value); } catch { return false; }
}
function clone<T>(value: T): T { return structuredClone(value); }

/** Node ids have one namespace, including Step/Group/ForEach; no workflow-id regex. */
function indexRows(value: unknown): Members | null {
  if (!record(value) || !Array.isArray(value.steps) || !Array.isArray(value.groups) || !Array.isArray(value.forEach)) return null;
  const members: Members = new Map();
  for (const [kind, rows, key] of [
    ["step", value.steps, "step_id"], ["group", value.groups, "group_id"], ["for_each", value.forEach, "for_each_id"],
  ] as const) {
    for (const row of rows) {
      if (!record(row) || !own(row, key) || !id(row[key]) || members.has(row[key])) return null;
      members.set(row[key], { kind, row });
    }
  }
  return members;
}
function nodeRef(value: unknown, members: Members): value is WorkflowNodeRef {
  return record(value) && keys(value, ["kind", "id"]) && id(value.id) && members.has(value.id) && members.get(value.id)?.kind === value.kind;
}
function scope(member: Member): Scope {
  const row = member.row;
  if (member.kind === "group") return null;
  const kind = member.kind === "step" && id(row.for_each_id) ? "for_each" : id(row.group_id) ? "group" : null;
  return kind ? { kind, id: String(kind === "for_each" ? row.for_each_id : row.group_id), lane: id(row.lane_id) ? row.lane_id : null } : null;
}
function bodyLanes(member: Member): Map<string, JsonRecord> | null {
  const body = member.row.body;
  if (!record(body)) return null;
  if (body.kind === "sequence") return keys(body, ["kind", "entry"]) ? new Map() : null;
  if (body.kind !== "lanes" || !keys(body, ["kind", "lanes", "concurrency_limit"]) || !Array.isArray(body.lanes) || !body.lanes.length) return null;
  if (own(body, "concurrency_limit") && body.concurrency_limit !== null &&
    (typeof body.concurrency_limit !== "number" || !Number.isSafeInteger(body.concurrency_limit) || body.concurrency_limit < 1)) return null;
  const result = new Map<string, JsonRecord>();
  for (const lane of body.lanes) {
    if (!record(lane) || !keys(lane, ["lane_id", "entry"]) || !id(lane.lane_id) || result.has(lane.lane_id)) return null;
    result.set(lane.lane_id, lane);
  }
  return result;
}
function validOwner(member: Member, members: Members): boolean {
  const row = member.row;
  for (const key of member.kind === "step" ? ["group_id", "for_each_id", "lane_id"] : member.kind === "for_each" ? ["group_id"] : []) {
    if (own(row, key) && row[key] !== null && !id(row[key])) return false;
  }
  if (member.kind === "step" && id(row.group_id) && id(row.for_each_id)) return false;
  const owner = scope(member);
  if (!owner) return member.kind !== "step" || !id(row.lane_id);
  const parent = members.get(owner.id);
  if (parent?.kind !== owner.kind) return false;
  const lanes = bodyLanes(parent);
  if (!lanes) return false;
  if (member.kind === "for_each") return parent.row.body !== null && record(parent.row.body) && parent.row.body.kind === "sequence";
  return lanes.size ? owner.lane !== null && lanes.has(owner.lane) : owner.lane === null;
}
function validEntries(member: Member, members: Members): boolean {
  const body = member.row.body;
  const lanes = bodyLanes(member);
  if (!record(body) || !lanes) return false;
  const ownerId = String(member.kind === "group" ? member.row.group_id : member.row.for_each_id);
  const entry = (value: unknown, lane: string | null) => value === undefined || value === null ||
    (nodeRef(value, members) && value.kind !== "group" && (lane === null && member.kind === "group" || value.kind === "step") &&
      sameScope(scope(members.get(value.id)!), { kind: member.kind as "group" | "for_each", id: ownerId, lane }));
  return body.kind === "sequence" ? entry(body.entry, null) : [...lanes].every(([laneId, lane]) => entry(lane.entry, laneId));
}
function targetRef(value: unknown, members: Members): value is WorkflowTargetRef {
  if (nodeRef(value, members)) return true;
  if (!record(value)) return false;
  const key = value.kind === "group_exit" ? "group_id" : value.kind === "for_each_exit" ? "for_each_id" : null;
  if (!key || !keys(value, ["kind", key, "lane_id"]) || !own(value, "lane_id") || !id(value[key])) return false;
  const member = members.get(value[key]);
  if (member?.kind !== (key === "group_id" ? "group" : "for_each")) return false;
  const lanes = bodyLanes(member);
  return lanes !== null && (lanes.size ? id(value.lane_id) && lanes.has(value.lane_id) : value.lane_id === null);
}
function validEdge(value: unknown, members: Members): value is WorkflowGraphEdge {
  if (!record(value) || !keys(value, ["source", "outcome", "target", "rework", "rework_limit"]) ||
    !nodeRef(value.source, members) || !targetRef(value.target, members) || !record(value.outcome)) return false;
  const outcome = value.outcome;
  if (!keys(outcome, ["name", "case_id"]) || !["success", "true", "false", "case", "default", "approved", "rejected", "error"].includes(String(outcome.name)) ||
    (outcome.name === "case" ? !id(outcome.case_id) : own(outcome, "case_id") && outcome.case_id !== null)) return false;
  if (own(value, "rework") && typeof value.rework !== "boolean") return false;
  if (own(value, "rework_limit") && (value.rework !== true || typeof value.rework_limit !== "number" ||
    !Number.isSafeInteger(value.rework_limit) || value.rework_limit < 1)) return false;
  if (value.rework === true && !own(value, "rework_limit")) return false;
  const sourceScope = scope(members.get(value.source.id)!);
  const target = value.target;
  const targetScope = target.kind === "group_exit" ? { kind: "group" as const, id: target.group_id, lane: target.lane_id }
    : target.kind === "for_each_exit" ? { kind: "for_each" as const, id: target.for_each_id, lane: target.lane_id }
      : scope(members.get(target.id)!);
  return sameScope(sourceScope, targetScope);
}

/** Shape/closure validation only, not step configuration or workflow readiness authority. */
function validatePayload(value: unknown): Reason | null {
  if (!record(value) || value.format !== FORMAT || value.version !== VERSION) return "unsupported_format";
  if (!jsonData(value) || !keys(value, ["format", "version", "definition", "roots", "relativeWorldPositions"])) return "invalid_fragment";
  const definition = value.definition;
  const members = indexRows(definition);
  if (!record(definition) || !keys(definition, ["steps", "groups", "forEach", "graph"]) || !members?.size) return "invalid_fragment";
  if ([...members.keys()].some((key) => parseHookNodeId(key)) || [...members.values()].some((member) => member.kind === "step" && workflowStepDeclaresHook(member.row.step_kind, member.row.override))) return "unrepresentable_selection";
  for (const member of members.values()) {
    if (!validOwner(member, members) || member.kind !== "step" && !validEntries(member, members)) return "invalid_fragment";
    if (member.kind === "for_each" && (typeof member.row.over !== "string" || typeof member.row.item_name !== "string")) return "invalid_fragment";
  }
  if (!Array.isArray(value.roots) || !value.roots.length || !value.roots.every((ref) => nodeRef(ref, members) && scope(members.get(ref.id)!) === null)) return "invalid_fragment";
  const rootIds = new Set(value.roots.map((ref) => String(ref.id)));
  if (rootIds.size !== value.roots.length || [...members].some(([key, member]) => scope(member) === null && !rootIds.has(key))) return "invalid_fragment";
  const graph = definition.graph;
  if (!record(graph) || !keys(graph, ["schema_version", "entry", "edges"]) || graph.schema_version !== 1 || !own(graph, "entry") ||
    graph.entry !== null && (!nodeRef(graph.entry, members) || !rootIds.has(graph.entry.id)) || !Array.isArray(graph.edges)) return "invalid_fragment";
  const ports = new Set<string>();
  for (const edge of graph.edges) {
    if (!validEdge(edge, members)) return "invalid_fragment";
    const port = JSON.stringify([edge.source.kind, edge.source.id, edge.outcome.name, edge.outcome.case_id ?? null]);
    if (ports.has(port)) return "invalid_fragment";
    ports.add(port);
  }
  const geometry = value.relativeWorldPositions;
  if (!record(geometry) || Object.keys(geometry).length !== members.size ||
    [...members.keys()].some((key) => !own(geometry, key) || !point(geometry[key]))) return "invalid_fragment";
  return null;
}

export function decodeBuilderClipboard(text: string): CodecResult<BuilderClipboardV1> {
  let value: unknown;
  try { value = JSON.parse(text); } catch { return fail("unsupported_format"); }
  const reason = validatePayload(value);
  if (reason) return fail(reason);
  const payload = value as BuilderClipboardV1;
  const geometry = positions();
  for (const [key, value] of Object.entries(payload.relativeWorldPositions)) geometry[key] = { ...value };
  return { ok: true, value: { ...payload, relativeWorldPositions: geometry } };
}

export function encodeBuilderSelection({ definition, targets, worldPositions }: {
  definition: WorkflowDefinitionModel;
  targets: readonly WorkflowNodeRef[];
  worldPositions: ReadonlyMap<string, CanvasPosition>;
}): CodecResult<string> {
  const source = indexRows(definition);
  if (!source || !targets.length || targets.some((ref) => !nodeRef(ref, source))) return fail("invalid_fragment");
  if (targets.some((ref) => parseHookNodeId(ref.id))) return fail("unrepresentable_selection");
  const selected = new Set<string>();
  for (const target of targets) {
    const closure = deriveDeletionClosure(definition, target);
    if (!closure) return fail("unrepresentable_selection");
    for (const key of [...closure.steps, ...closure.groups, ...closure.forEach]) selected.add(key);
  }
  const rows = [...selected].map((key) => source.get(key)!);
  if (rows.some((member) => !jsonData(member.row))) return fail("invalid_fragment");
  const fragment: WorkflowDefinitionModel = {
    steps: clone(definition.steps.filter((row) => selected.has(String(row.step_id)))),
    groups: clone(definition.groups.filter((row) => selected.has(row.group_id))),
    forEach: clone(definition.forEach.filter((row) => selected.has(row.for_each_id))),
    graph: { schema_version: 1, entry: null, edges: [] },
  };
  for (const step of fragment.steps) {
    if (step.group_id && !selected.has(step.group_id)) { step.group_id = null; step.lane_id = null; }
    if (step.for_each_id && !selected.has(step.for_each_id)) { step.for_each_id = null; step.lane_id = null; }
  }
  for (const item of fragment.forEach) if (item.group_id && !selected.has(item.group_id)) item.group_id = null;
  const members = indexRows(fragment)!;
  const roots = [...members].filter(([, member]) => scope(member) === null).map(([key, member]) => ({ kind: member.kind, id: key }));
  if (!roots.length) return fail("invalid_fragment");
  const rootPoints = roots.map((ref) => worldPositions.get(ref.id));
  if (!rootPoints.every(point)) return fail("invalid_fragment");
  const origin = rootPoints.reduce<CanvasPosition>((result, value) => ({ x: Math.min(result.x, value.x), y: Math.min(result.y, value.y) }), { x: Infinity, y: Infinity });
  const geometry = positions();
  for (const key of selected) {
    const value = worldPositions.get(key);
    if (!point(value)) return fail("invalid_fragment");
    geometry[key] = { x: value.x - origin.x, y: value.y - origin.y };
  }
  if (!record(definition.graph) || definition.graph.schema_version !== 1 || !Array.isArray(definition.graph.edges)) return fail("invalid_fragment");
  for (const edge of definition.graph.edges) {
    if (!record(edge) || !record(edge.source) || !record(edge.target)) return fail("invalid_fragment");
    const targetId = edge.target.kind === "group_exit" ? edge.target.group_id : edge.target.kind === "for_each_exit" ? edge.target.for_each_id : edge.target.id;
    if (selected.has(String(edge.source.id)) && selected.has(String(targetId))) {
      if (!jsonData(edge)) return fail("invalid_fragment");
      fragment.graph.edges.push(clone(edge));
    }
  }
  if (nodeRef(definition.graph.entry, members) && roots.some((ref) => ref.id === definition.graph.entry?.id)) fragment.graph.entry = clone(definition.graph.entry);
  const payload: BuilderClipboardV1 = { format: FORMAT, version: VERSION, definition: fragment, roots, relativeWorldPositions: geometry };
  const reason = validatePayload(payload);
  return reason ? fail(reason) : { ok: true, value: JSON.stringify(payload) };
}

/** A damaged destination draft may still name a missing node. Reserve those
 * structural ids too: pasting must never repair/attach an old dangling ref.
 * Opaque row configuration and business strings are deliberately not traversed. */
function reserveDestinationReferences(definition: WorkflowDefinitionModel, reserved: Set<string>): boolean {
  const reserve = (value: unknown) => {
    if (!id(value)) return false;
    reserved.add(value);
    return true;
  };
  const ref = (value: unknown, allowExit = false): boolean => {
    if (!record(value)) return false;
    if (["step", "group", "for_each"].includes(String(value.kind))) return reserve(value.id);
    if (allowExit && value.kind === "group_exit") return reserve(value.group_id);
    if (allowExit && value.kind === "for_each_exit") return reserve(value.for_each_id);
    return false;
  };
  const optionalOwner = (value: unknown) => value === undefined || value === null || reserve(value);
  const body = (value: unknown): boolean => {
    if (value === undefined || value === null) return true;
    if (!record(value) || !["sequence", "lanes"].includes(String(value.kind))) return false;
    if (own(value, "entry") && value.entry !== null && !ref(value.entry)) return false;
    if (own(value, "lanes")) {
      if (!Array.isArray(value.lanes)) return false;
      for (const lane of value.lanes) {
        if (!record(lane) || own(lane, "entry") && lane.entry !== null && !ref(lane.entry)) return false;
      }
    }
    return true;
  };
  for (const step of definition.steps) {
    if (!optionalOwner(step.group_id) || !optionalOwner(step.for_each_id)) return false;
  }
  for (const group of definition.groups) if (!body(group.body)) return false;
  for (const item of definition.forEach) if (!optionalOwner(item.group_id) || !body(item.body)) return false;
  if (!record(definition.graph) || !Array.isArray(definition.graph.edges)) return false;
  if (definition.graph.entry !== null && !ref(definition.graph.entry)) return false;
  for (const edge of definition.graph.edges) {
    if (!record(edge) || !ref(edge.source) || !ref(edge.target, true)) return false;
  }
  return true;
}

export function remapBuilderClipboard(payload: BuilderClipboardV1, destinationDefinition: WorkflowDefinitionModel): CodecResult<{
  definition: WorkflowDefinitionModel;
  roots: WorkflowNodeRef[];
  relativeWorldPositions: Record<string, CanvasPosition>;
  idMap: ReadonlyMap<string, string>;
}> {
  const reason = validatePayload(payload);
  if (reason) return fail(reason);
  const destination = indexRows(destinationDefinition);
  if (!destination) return fail("invalid_fragment");
  const source = indexRows(payload.definition)!;
  const reserved = new Set([...destination.keys(), ...source.keys()]);
  if (!reserveDestinationReferences(destinationDefinition, reserved)) return fail("invalid_fragment");
  const mapping = new Map<string, string>();
  for (const [key, member] of source) {
    const next = uniqueLocalId(key, reserved, member.kind);
    if (!id(next) || reserved.has(next) || parseHookNodeId(next)) return fail("id_allocation_failed");
    mapping.set(key, next);
    reserved.add(next);
  }
  const mapNode = (ref: WorkflowNodeRef): WorkflowNodeRef => ({ ...ref, id: mapping.get(ref.id)! });
  const mapBody = (body: unknown) => {
    if (!record(body)) return body;
    if (own(body, "entry") && body.entry !== null) body.entry = mapNode(body.entry as WorkflowNodeRef);
    if (Array.isArray(body.lanes)) for (const lane of body.lanes) {
      if (own(lane, "entry") && lane.entry !== null) lane.entry = mapNode(lane.entry as WorkflowNodeRef);
    }
    return body;
  };
  const definition = clone(payload.definition);
  for (const step of definition.steps) {
    step.step_id = mapping.get(step.step_id!)!;
    if (step.group_id) step.group_id = mapping.get(step.group_id)!;
    if (step.for_each_id) step.for_each_id = mapping.get(step.for_each_id)!;
  }
  for (const group of definition.groups) { group.group_id = mapping.get(group.group_id)!; mapBody(group.body); }
  for (const item of definition.forEach) {
    item.for_each_id = mapping.get(item.for_each_id)!;
    if (item.group_id) item.group_id = mapping.get(item.group_id)!;
    mapBody(item.body);
  }
  if (definition.graph.entry) definition.graph.entry = mapNode(definition.graph.entry);
  for (const edge of definition.graph.edges) {
    edge.source = mapNode(edge.source);
    edge.target = edge.target.kind === "group_exit" ? { ...edge.target, group_id: mapping.get(edge.target.group_id)! }
      : edge.target.kind === "for_each_exit" ? { ...edge.target, for_each_id: mapping.get(edge.target.for_each_id)! } : mapNode(edge.target);
  }
  const geometry = positions();
  for (const [key, value] of Object.entries(payload.relativeWorldPositions)) geometry[mapping.get(key)!] = { ...value };
  return { ok: true, value: { definition, roots: payload.roots.map(mapNode), relativeWorldPositions: geometry, idMap: mapping } };
}
