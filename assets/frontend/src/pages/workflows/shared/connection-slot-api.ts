import { fetchJson } from "@/lib/http/client";

/** One connection a project may choose for a slot. */
export type WorkflowConnectionSlotOption = {
  connection_id: string;
  display_name: string;
  connection_type?: string;
  extension_install_id?: string;
};

/** One connection an extension workflow needs, and what fills it in the project (ADR 0068). */
export type WorkflowConnectionSlot = {
  slot: string;
  label: string;
  extension_id: string;
  connection_type_ids: string[];
  connection_id: string | null;
  /** `chosen` by the project, `automatic` (the only usable one) or `none`. */
  choice: "chosen" | "automatic" | "none";
  ready: boolean;
  reason: string;
  options: WorkflowConnectionSlotOption[];
};

export type WorkflowConnectionSlots = {
  workflow_id: string;
  project_id: string;
  ready: boolean;
  slots: WorkflowConnectionSlot[];
};

function slotsPath(projectId: string, workflowId: string, suffix = "") {
  return `/api/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}/connection-slots${suffix}`;
}

/** The query key of a workflow's slots in a project; a choice invalidates it. */
export function workflowConnectionSlotsQueryKey(projectId: string, workflowId: string) {
  return ["workflows", "connection-slots", projectId, workflowId] as const;
}

export function fetchWorkflowConnectionSlots(projectId: string, workflowId: string) {
  return fetchJson<WorkflowConnectionSlots>(slotsPath(projectId, workflowId), { cache: "no-store" });
}

/** Store the project's connection for one slot; an empty id clears the choice. */
export function saveWorkflowConnectionSlot(projectId: string, workflowId: string, slot: string, connectionId: string) {
  return fetchJson<WorkflowConnectionSlots>(slotsPath(projectId, workflowId, `/${encodeURIComponent(slot)}`), {
    body: JSON.stringify({ connection_id: connectionId || null }),
    headers: { "Content-Type": "application/json" },
    method: "PUT",
  });
}

/** Whether a workflow definition declares connection slots in `settings.connection_slots`. */
export function declaresConnectionSlots(settings: unknown): boolean {
  if (!settings || typeof settings !== "object" || Array.isArray(settings)) return false;
  const slots = (settings as { connection_slots?: unknown }).connection_slots;
  return Array.isArray(slots) && slots.length > 0;
}
