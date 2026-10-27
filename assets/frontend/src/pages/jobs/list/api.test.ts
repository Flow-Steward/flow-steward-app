import { fetchJson } from "@/lib/http/client";
import { fetchJobsList } from "./api";

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
