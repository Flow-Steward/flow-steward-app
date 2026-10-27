import { fetchJson, type FetchJsonOptions } from "@/lib/http/client";
import { HttpError } from "@/lib/http/errors";

/** One hook a workflow declares, with its binding in the project (`GET …/hooks/{hook_id}`). */
export type WorkflowHookContract = {
  hook_id: string;
  contract_version: number;
  display_name: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  input_mapping?: Record<string, string>;
  mutable_fields?: string[];
  additional_outputs?: Record<string, { schema?: Record<string, unknown>; default?: unknown; optional?: boolean }>;
  stage_contract?: { summary?: string; effects_before?: string[]; effects_after?: string[] };
};

export type WorkflowHookBinding = {
  revision_id: string;
  target_workflow_id: string;
  input_mapping: Record<string, string>;
  output_mapping: Record<string, string>;
  hook_contract_version?: number;
  hook_semantic_digest?: string;
};

export type WorkflowHook = {
  project_id: string;
  workflow_id: string;
  hook_id: string;
  step_id: string;
  display_name: string;
  description?: string;
  contract_version: number;
  hook_semantic_digest: string;
  contract: WorkflowHookContract;
  binding_revision: number;
  binding: WorkflowHookBinding | null;
};

export type WorkflowHookIssue = { code?: string; message?: string; path?: string };

export type WorkflowHookPreview = {
  valid: boolean;
  issues: WorkflowHookIssue[];
  binding_revision?: number;
  /** With a sample: the hook output a run would publish for it. */
  sample_output?: Record<string, unknown>;
};

/** Hook inputs and a child result to run through the binding check. */
export type WorkflowHookSample = { inputs: Record<string, unknown>; child_outputs: Record<string, unknown> };

export type WorkflowHookBindingResult = {
  project_id: string;
  workflow_id: string;
  hook_id: string;
  binding_revision: number;
  revision_id: string;
  target_workflow_id: string | null;
};

export type WorkflowConsumer = {
  kind: "invoke" | "hook";
  caller_workflow_id: string;
  caller_display_name?: string | null;
  caller_extension_id?: string | null;
  caller_extension_name?: string | null;
  caller_extension_version?: string | null;
  hook_display_name?: string | null;
  step_display_name?: string | null;
  source_install_id?: string | null;
  hook_id?: string | null;
  step_id?: string | null;
  project_id?: string | null;
};

export type WorkflowHookBindingDraft = {
  expected_revision: number;
  target_workflow_id: string;
  input_mapping: Record<string, string>;
  output_mapping: Record<string, string>;
  hook_contract_version?: number;
  hook_semantic_digest?: string;
};

/** A hook request without a project; the builder asks for a project before it can happen. */
export class HookProjectRequiredError extends Error {
  constructor() {
    super("A hook belongs to one project: choose the project first.");
    this.name = "HookProjectRequiredError";
  }
}

function hookPath(projectId: string, workflowId: string, suffix = "") {
  // R2-10: never `/api/projects//workflows/…` (a 404 shown as "Not Found").
  if (!projectId.trim()) throw new HookProjectRequiredError();
  return `/api/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}${suffix}`;
}

function post<T>(url: string, body: unknown) {
  return fetchJson<T>(url, {
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" },
    method: "POST",
  });
}

/** Every hook a workflow declares, with its binding in the project (`GET …/hooks`). */
export function fetchWorkflowHooks(projectId: string, workflowId: string, options?: FetchJsonOptions) {
  return fetchJson<{ project_id: string; workflow_id: string; hooks: WorkflowHook[] }>(hookPath(projectId, workflowId, "/hooks"), {
    cache: "no-store",
  }, options);
}

/** The query key of a workflow's hook list in a project; a binding change invalidates it. */
export function workflowHooksQueryKey(projectId: string, workflowId: string) {
  return ["workflows", "hooks", projectId, workflowId] as const;
}

export function fetchWorkflowHook(projectId: string, workflowId: string, hookId: string) {
  return fetchJson<WorkflowHook>(hookPath(projectId, workflowId, `/hooks/${encodeURIComponent(hookId)}`), {
    cache: "no-store",
  });
}

export function previewWorkflowHookBinding(
  projectId: string,
  workflowId: string,
  hookId: string,
  draft: WorkflowHookBindingDraft,
  sample?: WorkflowHookSample,
) {
  return post<WorkflowHookPreview>(
    hookPath(projectId, workflowId, `/hooks/${encodeURIComponent(hookId)}/preview`),
    sample ? { ...draft, sample } : draft,
  );
}

export function bindWorkflowHook(projectId: string, workflowId: string, hookId: string, draft: WorkflowHookBindingDraft) {
  return post<WorkflowHookBindingResult>(hookPath(projectId, workflowId, `/hooks/${encodeURIComponent(hookId)}/bind`), draft);
}

export function unbindWorkflowHook(projectId: string, workflowId: string, hookId: string, expectedRevision: number) {
  return post<WorkflowHookBindingResult>(hookPath(projectId, workflowId, `/hooks/${encodeURIComponent(hookId)}/unbind`), {
    expected_revision: expectedRevision,
  });
}

export function createAndBindWorkflowHook(
  projectId: string,
  workflowId: string,
  hookId: string,
  body: { expected_revision: number; child_workflow_id: string; display_name?: string },
) {
  return post<WorkflowHookBindingResult>(hookPath(projectId, workflowId, `/hooks/${encodeURIComponent(hookId)}/create-and-bind`), body);
}

export function workflowWhereUsedQueryKey(projectId: string, workflowId: string, locale: string) {
  return ["workflows", "where-used", projectId, workflowId, locale];
}

export function fetchWorkflowWhereUsed(projectId: string, workflowId: string, locale = "") {
  return fetchJson<{ project_id: string; workflow_id: string; workflow_display_name?: string | null; consumers: WorkflowConsumer[] }>(
    hookPath(projectId, workflowId, "/where-used") + (locale ? `?locale=${encodeURIComponent(locale)}` : ""),
    { cache: "no-store" },
  );
}

/** The binding issues a refused bind carries, wherever the error envelope nests them. */
export function hookErrorIssues(error: unknown): WorkflowHookIssue[] {
  if (!(error instanceof HttpError)) return [];
  const queue: unknown[] = [error.payload];
  for (let depth = 0; depth < 5 && queue.length; depth += 1) {
    const next: unknown[] = [];
    for (const value of queue) {
      if (!value || typeof value !== "object" || Array.isArray(value)) continue;
      const record = value as Record<string, unknown>;
      if (Array.isArray(record.issues)) {
        return record.issues.filter(
          (item): item is WorkflowHookIssue => Boolean(item) && typeof item === "object" && !Array.isArray(item),
        );
      }
      next.push(...Object.values(record));
    }
    queue.splice(0, queue.length, ...next);
  }
  return [];
}
