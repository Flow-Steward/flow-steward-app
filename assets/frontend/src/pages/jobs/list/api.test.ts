import { fetchJson } from "@/lib/http/client";
import { fetchJobsList, fetchJobsQueuePosition } from "./api";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));
const mockedFetchJson = vi.mocked(fetchJson);

beforeEach(() => { vi.clearAllMocks(); });

it("requests the latest eight exact workflow Test Runs within the selected project", async () => {
  mockedFetchJson.mockResolvedValue({ jobs: [{ job_id: "run-a", project_id: "project-a", selected_workflow: "workflow-a", task_source: "workflow_test_run", duration_ms: 0, status: "completed" }], page: 1, per_page: 8, total: 12, total_pages: 2 });
  const filters = { page: 1, perPage: 8, status: "", projectId: "project-a", workflowId: "workflow-a", taskSource: "workflow_test_run" as const };
  const result = await fetchJobsList(filters);
  const requested = new URL(String(mockedFetchJson.mock.calls[0][0]), "https://example.test");
  expect(requested.pathname).toBe("/api/v2/jobs");
  expect(requested.searchParams.get("task_source")).toBe("workflow_test_run");
  expect(requested.searchParams.get("project_id")).toBe("project-a");
  expect(requested.searchParams.get("workflow_id")).toBe("workflow-a");
  expect(requested.searchParams.get("per_page")).toBe("8");
  expect(mockedFetchJson).toHaveBeenCalledTimes(1);
  expect(result.jobs?.[0]).toMatchObject({ id: "run-a", task_source: "workflow_test_run", duration_ms: 0, project_id: "project-a", selected_workflow: "workflow-a" });
  expect(result.total).toBe(12);
});

it("preserves the ordinary unfiltered jobs request when task source is omitted", async () => {
  mockedFetchJson.mockResolvedValue({ jobs: [] });
  await fetchJobsList({ page: 1, perPage: 25, status: "", projectId: null });
  const requested = new URL(String(mockedFetchJson.mock.calls[0][0]), "https://example.test");
  expect(requested.searchParams.has("task_source")).toBe(false);
});

it("sends date bounds alongside workflow and status filters", async () => {
  mockedFetchJson.mockResolvedValue({ jobs: [] });
  await fetchJobsList({ page: 1, perPage: 25, status: "failed", projectId: null, workflowId: "wf", fromDate: "2026-03-01", toDate: "2026-03-01" });
  const requested = new URL(String(mockedFetchJson.mock.calls[0][0]), "https://example.test");
  expect(requested.searchParams.get("from_date")).toBe("2026-03-01");
  expect(requested.searchParams.get("to_date")).toBe("2026-03-01");
  expect(requested.searchParams.get("workflow_id")).toBe("wf");
  expect(requested.searchParams.getAll("status")).toEqual(["failed"]);
});

it("asks for a section's operator view with the configured filters as one JSON value (ADR 0075)", async () => {
  mockedFetchJson.mockResolvedValue({ jobs: [] });
  await fetchJobsList({ page: 1, perPage: 25, status: "", projectId: "p1", workflowId: "wf", operatorView: true, operatorFilters: { "output.total": { min: 10 } } });
  const requested = new URL(String(mockedFetchJson.mock.calls[0][0]), "https://example.test");
  expect(requested.searchParams.get("operator_view")).toBe("1");
  expect(requested.searchParams.get("filters")).toBe('{"output.total":{"min":10}}');
  mockedFetchJson.mockClear();
  await fetchJobsList({ page: 1, perPage: 25, status: "", projectId: "p1", workflowId: "wf", operatorView: true, operatorFilters: null });
  const unfiltered = new URL(String(mockedFetchJson.mock.calls[0][0]), "https://example.test");
  expect(unfiltered.searchParams.has("filters")).toBe(false);
});

it("asks for a job's queue position with the same filters as the list, without a page", async () => {
  mockedFetchJson.mockResolvedValue({ job_id: "j2", in_list: true, position: 3, total: 47, previous_job_id: "j1", next_job_id: "j3" });
  const position = await fetchJobsQueuePosition({ status: "awaiting_approval", projectId: "p1", workflowId: "wf", operatorView: true, operatorFilters: { state: ["awaiting_review"] }, search: "acme" }, "j2");
  const requested = new URL(String(mockedFetchJson.mock.calls[0][0]), "https://example.test");
  expect(requested.pathname).toBe("/api/v2/jobs");
  expect(requested.searchParams.get("around")).toBe("j2");
  expect(requested.searchParams.get("operator_view")).toBe("1");
  expect(requested.searchParams.get("filters")).toBe('{"state":["awaiting_review"]}');
  expect(requested.searchParams.get("search")).toBe("acme");
  expect(requested.searchParams.getAll("status")).toEqual(["awaiting_approval"]);
  expect(requested.searchParams.has("page")).toBe(false);
  expect(position.position).toBe(3);
});
