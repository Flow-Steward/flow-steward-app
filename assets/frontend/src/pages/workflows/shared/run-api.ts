import { fetchJson } from "@/lib/http/client";

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

export function readWorkflowRunFile(inputName: string, file: File): Promise<WorkflowRunFile> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error());
    reader.onload = () => resolve({ input_name: inputName, filename: file.name,
      mime_type: file.type || "application/octet-stream", content_base64: String(reader.result).split(",")[1] || "" });
    reader.readAsDataURL(file);
  });
}
