import { fetchJson } from "@/lib/http/client";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

export type WorkflowRunFile = { input_name: string; filename: string; mime_type: string; content_base64: string };
export type BuilderWorkflowRunRequest = {
  project_id: string; expected_revision: number; acknowledged: true; summary: string;
  inputs: Record<string, unknown>; files: WorkflowRunFile[];
};
export function runBuilderWorkflow(workflowId: string, request: BuilderWorkflowRunRequest, idempotencyKey: string) {
  return fetchJson<{ job_id: string }>(`/api/workflows/${encodeURIComponent(workflowId)}/run`, {
    method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(request),
  });
}

/** Reads a run file; `failureMessage` is the (translated) message a read failure rejects with. */
export function readWorkflowRunFile(inputName: string, file: File, failureMessage: string): Promise<WorkflowRunFile> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(failureMessage, { cause: reader.error }));
    reader.onload = () => resolve({ input_name: inputName, filename: file.name,
      mime_type: file.type || "application/octet-stream", content_base64: stringFromUnknownValue(reader.result).split(",")[1] || "" });
    reader.readAsDataURL(file);
  });
}
