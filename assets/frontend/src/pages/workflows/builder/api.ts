import { fetchJson } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";
import type {
  WorkflowDetail,
  WorkflowForEach,
  WorkflowGraphEdge,
  WorkflowGroup,
  WorkflowNodeRef,
  WorkflowOutcome,
  WorkflowStep,
  WorkflowTargetRef,
} from "@/pages/workflows/shared/api";

export type WorkflowStepPayload = WorkflowStep & { step_id: string; step_kind: string };

/**
 * One operation of a workflow change set, exactly as `POST /changes` accepts it.
 *
 * The builder keeps structural operations locally until the author applies
 * them; a step's own parameters go out as a one-operation set right away.
 */
export type WorkflowChangeOperation =
  | { op: "create_step"; step: WorkflowStepPayload }
  | { op: "create_group"; group: WorkflowGroup }
  | { op: "create_for_each"; for_each: WorkflowForEach }
  | { op: "update_step_config"; step_id: string; patch: Record<string, unknown> }
  /** A structural kind change: the step keeps its id, placement, name, error policy,
   *  boundary contract and optional flag; everything else comes from `step`. */
  | { op: "change_step_kind"; step_id: string; step: Record<string, unknown> & { step_kind: string } }
  | { op: "update_group_config"; group_id: string; patch: Record<string, unknown> }
  | { op: "update_for_each_config"; for_each_id: string; patch: Record<string, unknown> }
  | {
      op: "set_entry";
      target: WorkflowNodeRef;
      group_id?: string;
      for_each_id?: string;
      lane_id?: string;
    }
  | { op: "connect"; edge: WorkflowGraphEdge; replace?: true }
  | { op: "disconnect"; source: WorkflowNodeRef; outcome: WorkflowOutcome }
  | {
      op: "insert_step_between";
      source: WorkflowNodeRef;
      outcome: WorkflowOutcome;
      step: WorkflowStepPayload;
    }
  | {
      op: "move_step";
      step_id: string;
      group_id: string | null;
      for_each_id?: string | null;
      lane_id: string | null;
    }
  /** Move a For each between the root and a sequence Group, with its body. */
  | { op: "move_for_each"; for_each_id: string; group_id: string | null }
  | { op: "delete_step"; step_id: string }
  | { op: "delete_group"; group_id: string; with_contents: true }
  | { op: "delete_for_each"; for_each_id: string; with_contents: true };

export type WorkflowChangeIssue = {
  code?: string;
  path?: string;
  message?: string;
  severity?: string;
  [key: string]: unknown;
};

export type WorkflowChangeResult = {
  workflow_id: string;
  revision: number;
  valid: boolean;
  issues: WorkflowChangeIssue[];
  workflow: WorkflowDetail;
  /** False for a dry run, which writes nothing and keeps the revision. */
  applied?: boolean;
  /** What would make Apply refuse; only an enabled workflow has any. */
  blocking_issues?: WorkflowChangeIssue[];
};

export type GroupExtractionField = { name: string; type: string; required?: boolean };
export type GroupExtractionResult = {
  can_convert: boolean;
  issues: WorkflowChangeIssue[];
  revision: number;
  preview_digest: string;
  child_workflow_id: string;
  display_name: string;
  invocation_step_id: string;
  interface: { expected_inputs?: GroupExtractionField[]; expected_outputs?: GroupExtractionField[] };
  input_mapping: Record<string, string>;
  output_mapping: Record<string, string>;
  step_count: number;
  applied?: boolean;
  child_revision?: number;
};

export function convertWorkflowGroup(
  workflowId: string,
  groupId: string,
  payload: {
    child_workflow_id: string;
    display_name: string;
    expected_revision: number;
    dry_run: boolean;
    preview_digest?: string;
  },
) {
  return fetchJson<GroupExtractionResult>(
    `/api/workflows/${encodeURIComponent(workflowId)}/groups/${encodeURIComponent(groupId)}/convert-to-child`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

/**
 * Apply a change set, or with `dry_run` judge it exactly as Apply would
 * (authorization, validation of the result, readiness) without writing.
 */
export async function applyWorkflowChanges(
  workflowId: string,
  payload: { expected_revision: number; operations: WorkflowChangeOperation[]; dry_run?: boolean },
) {
  return fetchJson<WorkflowChangeResult>(
    `/api/workflows/${encodeURIComponent(workflowId)}/changes`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

function errorDetail(error: unknown): Record<string, unknown> {
  if (!(error instanceof HttpError)) {
    return {};
  }
  const payload = error.payload;
  if (!payload || typeof payload !== "object") {
    return {};
  }
  const detail = (payload as Record<string, unknown>).detail;
  return detail && typeof detail === "object" && !Array.isArray(detail)
    ? (detail as Record<string, unknown>)
    : {};
}

/** True for the 409 the server answers when the workflow moved past the caller's revision. */
export function isWorkflowRevisionConflict(error: unknown): boolean {
  return (
    error instanceof HttpError &&
    error.status === 409 &&
    errorDetail(error).code === "workflow_revision_conflict"
  );
}

/** The exact-path issues of a refused change set (400), if the server sent any. */
export function workflowChangeErrorIssues(error: unknown): WorkflowChangeIssue[] {
  if (!(error instanceof HttpError) || error.status !== 400) {
    return [];
  }
  const detail = errorDetail(error);
  const raw = detail.issues ?? detail.errors;
  return Array.isArray(raw)
    ? raw.filter(
        (item): item is WorkflowChangeIssue =>
          Boolean(item) && typeof item === "object" && !Array.isArray(item),
      )
    : [];
}

export function nodeRef(kind: WorkflowNodeRef["kind"], id: string): WorkflowNodeRef {
  return { kind, id };
}

export function sameNodeRef(left: unknown, right: unknown): boolean {
  if (!left || !right || typeof left !== "object" || typeof right !== "object") {
    return false;
  }
  const a = left as Record<string, unknown>;
  const b = right as Record<string, unknown>;
  if (a.kind === "group_exit" || b.kind === "group_exit") {
    return (
      a.kind === b.kind &&
      a.group_id === b.group_id &&
      (a.lane_id ?? null) === (b.lane_id ?? null)
    );
  }
  if (a.kind === "for_each_exit" || b.kind === "for_each_exit") {
    return (
      a.kind === b.kind &&
      a.for_each_id === b.for_each_id &&
      (a.lane_id ?? null) === (b.lane_id ?? null)
    );
  }
  return a.kind === b.kind && a.id === b.id;
}

export function sameOutcome(left: WorkflowOutcome | undefined, right: WorkflowOutcome | undefined) {
  if (!left || !right) {
    return false;
  }
  return left.name === right.name && (left.case_id || "") === (right.case_id || "");
}

/** A stable string for one source port, used as a React key and a canvas handle id. */
export function outcomeHandleId(outcome: WorkflowOutcome): string {
  return outcome.name === "case" ? `case:${outcome.case_id || ""}` : outcome.name;
}

const SIMPLE_OUTCOMES = new Set(["success", "true", "false", "default", "approved", "rejected", "error"]);

export function outcomeFromHandleId(handleId: string): WorkflowOutcome | null {
  const value = handleId.trim();
  if (value.startsWith("case:")) {
    const caseId = value.slice("case:".length);
    return caseId ? { name: "case", case_id: caseId } : null;
  }
  return SIMPLE_OUTCOMES.has(value) ? ({ name: value } as WorkflowOutcome) : null;
}

export function targetKey(target: WorkflowTargetRef): string {
  if (target.kind === "group_exit") {
    return `group_exit:${target.group_id}:${target.lane_id || ""}`;
  }
  if (target.kind === "for_each_exit") {
    return `for_each_exit:${target.for_each_id}:${target.lane_id || ""}`;
  }
  return `${target.kind}:${target.id}`;
}

type LabelTranslate = (key: string, fallback: string, values?: Record<string, string>) => string;

/** A readable name for an edge target: a node id, or the exit of its container. */
export function targetLabel(target: WorkflowTargetRef, t: LabelTranslate): string {
  if (target.kind === "group_exit") {
    return target.lane_id
      ? t("react.workflows.group_lane_exit_label", "exit of {group} / {lane}", { group: target.group_id, lane: target.lane_id })
      : t("react.workflows.group_exit_label", "exit of {group}", { group: target.group_id });
  }
  if (target.kind === "for_each_exit") {
    return t("react.workflows.for_each_exit_label", "end of item in {for_each}", { for_each: target.for_each_id });
  }
  return target.id;
}

export function edgeKey(edge: Pick<WorkflowGraphEdge, "source" | "outcome">): string {
  return `${edge.source.kind}:${edge.source.id}:${outcomeHandleId(edge.outcome)}`;
}
