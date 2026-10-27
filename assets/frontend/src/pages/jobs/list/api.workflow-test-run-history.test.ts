import { fetchJson } from "@/lib/http/client";
import { fetchJobsList } from "./api";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));
const read = vi.mocked(fetchJson);
const filters = { page: 1, perPage: 8, status: "", workflowId: "workflow", projectId: "alpha", taskSource: "workflow_test_run" as const };
beforeEach(() => { read.mockReset(); });

it("rejects conflicting present raw history IDs before normalization in jobs and grouped rows", async () => {
  const valid = { id: "run-a", job_id: "run-a", status: "failed", task_source: "workflow_test_run", project_id: "alpha", selected_workflow: "workflow", duration_ms: 0 };
  const conflicting = [
    { ...valid, id: "other-job", job_id: "run-a" },
    { ...valid, id: "", job_id: "run-a" },
    { ...valid, id: "run-a", job_id: null },
  ];
  read.mockResolvedValue({ jobs: [...conflicting, valid], groups: { today: [...conflicting, valid] }, page: 1, per_page: 8, total: 12, total_pages: 2 });
  const result = await fetchJobsList(filters);
  expect(result.jobs).toEqual([valid]);
  expect(result.groups?.today).toEqual([valid]);
  expect(result.total).toBe(12); // Do not relabel server pagination or refill history.
  expect(result.total_pages).toBe(2);
  expect(result.jobs?.[0].duration_ms).toBe(0);
  const requested = new URL(String(read.mock.calls[0][0]), "https://example.test");
  expect(requested.pathname).toBe("/api/v2/jobs");
  expect(requested.searchParams.get("workflow_id")).toBe("workflow");
  expect(requested.searchParams.get("project_id")).toBe("alpha");
  expect(requested.searchParams.get("task_source")).toBe("workflow_test_run");
  expect(requested.searchParams.get("per_page")).toBe("8");
  expect(read).toHaveBeenCalledTimes(1);
});

it("retains canonical-equivalent and single-alias history IDs without losing persisted scalar values", async () => {
  read.mockResolvedValue({ jobs: [
    { id: 42, job_id: " 42 ", project_id: "alpha", selected_workflow: "workflow", task_source: "workflow_test_run", status: "completed", duration_ms: 0 },
    { id: "id-only", project_id: "alpha", selected_workflow: "workflow", task_source: "workflow_test_run", status: "failed" },
    { job_id: "job-only", project_id: "alpha", selected_workflow: "workflow", task_source: "workflow_test_run", status: "in_progress" },
  ] });
  const result = await fetchJobsList(filters);
  expect(result.jobs?.map((row) => [row.id, row.job_id])).toEqual([["42", "42"], ["id-only", "id-only"], ["job-only", "job-only"]]);
  expect(result.jobs?.[0].duration_ms).toBe(0);
  expect(result.jobs?.[1].status).toBe("failed");
  expect(result.jobs?.[2].status).toBe("in_progress");
  expect(read).toHaveBeenCalledTimes(1);
});

it("preserves ordinary Jobs list alias fallback and omitted task-source request behavior", async () => {
  read.mockResolvedValue({ jobs: [{ id: "ordinary-id", job_id: "other-alias", status: "failed" }], groups: { today: [{ job_id: "job-only", status: "completed" }] } });
  const result = await fetchJobsList({ page: 1, perPage: 25, status: "", projectId: null });
  expect(result.jobs?.[0]).toMatchObject({ id: "ordinary-id", job_id: "ordinary-id", status: "failed" });
  expect(result.groups?.today[0]).toMatchObject({ id: "job-only", job_id: "job-only", status: "completed" });
  const requested = new URL(String(read.mock.calls[0][0]), "https://example.test");
  expect(requested.searchParams.has("task_source")).toBe(false);
  expect(requested.searchParams.get("per_page")).toBe("25");
  expect(read).toHaveBeenCalledTimes(1);
});
