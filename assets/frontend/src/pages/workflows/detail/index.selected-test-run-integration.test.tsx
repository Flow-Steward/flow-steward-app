import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider } from "react-router";
import { queryKeys } from "@/lib/query/keys";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import { fetchJobDetail, type JobDetail } from "@/pages/jobs/detail/api";
import type { WorkflowTestRunResult } from "@/pages/workflows/shared/simulate-api";

import {
  canvasGraphFixture,
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowTestRunState,
  mockedFindWorkflowLinkedProjectId,
  mockedTestRunWorkflow,
  postedChangeSets,
  workflowDetailFixture,
} from "./index.test-helpers";

// Install the shared helper's API mocks before importing their real consumer.
import { WorkflowsDetailPage } from "@/pages/workflows/detail";

vi.setConfig({ testTimeout: 15_000 });

// Match the shared renderer's admin baseline with the real context consumed by the page.
const authValue: AuthContextValue = {
  error: null, isAuthenticated: true, status: "authenticated",
  logout: async () => undefined, refresh: async () => undefined, switchAccount: async () => undefined,
  session: {
    authenticated: true, local_auth_enabled: true, bootstrap_required: false, external_auth_enabled: false,
    roles: ["admin"], permissions: { can_read: true, can_mutate: true }, account_memberships: [],
    user: { id: "builder-admin", email: "builder@example.test", username: "builder", display_name: "Builder",
      roles: ["admin"], account_id: "account-test", account_ids: ["account-test"], account_memberships: [] },
    i18n: { locale: "en", fallback_locale: "en", supported_locales: [], messages: {} },
    feature_flags: {}, environment: { edition: "community", mode: "production" },
    settings_pages: [], registered_pages: [], shell_navigation: {},
  },
};

function renderAuthenticatedRouter(queryClient: QueryClient, router: ReturnType<typeof createMemoryRouter>) {
  return {
    ...render(<AuthContext.Provider value={authValue}><QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider></AuthContext.Provider>),
    router,
  };
}

function renderPage({
  embedded = true,
  surface = "detail",
  initialEntry = "/registry/workflows/weekly_report/builder",
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
}: {
  embedded?: boolean;
  surface?: "detail" | "builder";
  initialEntry?: string;
  queryClient?: QueryClient;
} = {}) {
  const router = createMemoryRouter([
    { path: "/registry/workflows/:workflowId", element: <WorkflowsDetailPage embedded={embedded} surface={surface} /> },
    { path: "/registry/workflows/:workflowId/builder", element: <WorkflowsDetailPage embedded={embedded} surface={surface} /> },
    { path: "/previous", element: <h1>Previous page</h1> },
  ], { initialEntries: ["/previous", initialEntry] });
  return renderAuthenticatedRouter(queryClient, router);
}


const entry = "/registry/workflows/weekly_report/builder?project_id=project_alpha";
const ackKey = "fs.workflow-run-job.weekly_report";
const threadKey = "fs.workflow-test-run.weekly_report";
type Detail = JobDetail;
type Ack = WorkflowTestRunResult;
function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function acknowledgement(jobId = "run-new", projectId = "project_alpha"): Ack {
  return { workflow_id: "weekly_report", project_id: projectId, job_id: jobId, thread_id: `thread-${jobId}`,
    status: "completed", final_status: "completed", needs_user_approval: false };
}
function installFixture() {
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  const edges = [{ source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } }];
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps: [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable", override: { set_variable: { target: "state.plan", value: true } } },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ], edges }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ entry: { kind: "step", id: "plan" }, edges, steps: [
    { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", config_summary: "state.plan", ports: [{ outcome: { name: "success" }, label: "Next" }] },
    { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
  ] }));
  const details = new Map<string, Detail>();
  const revisions = new Map<string, number>();
  const responses = new Map<string, unknown>();
  const rows = (project = "project_alpha") => [...details.values()].filter((detail) => detail.project_id === project).map((detail) => ({
    id: detail.id, job_id: detail.id, selected_workflow: "weekly_report", project_id: project,
    task_source: "workflow_test_run", status: detail.status, duration_ms: detail.duration_ms,
  }));
  const add = (id: string, project = "project_alpha", duration: number | null = 0) => {
    const detail = { id, project_id: project, selected_workflow: "weekly_report", status: "completed", duration_ms: duration,
      job: { id, project_id: project, selected_workflow: "weekly_report", status: "completed", duration_ms: duration }, execution_steps: [] } as Detail;
    details.set(id, detail); return detail;
  };
  add("run-latest"); add("run-older", "project_alpha", null); add("run-beta", "project_beta");
  const detailReads: string[] = [];
  const listReads: URL[] = [];
  const activeReads = new Map<string, number>();
  let maximumConcurrentDetailReads = 0;
  let history: ((url: URL) => Response | Promise<Response>) | undefined;
  let read: ((id: string) => Response | Promise<Response> | undefined) | undefined;
  const fallback = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === "/api/v2/jobs") {
      listReads.push(url);
      if (url.searchParams.get("workflow_id") !== "weekly_report" || url.searchParams.get("task_source") !== "workflow_test_run" ||
        url.searchParams.get("per_page") !== "8" || url.searchParams.get("page") !== "1") return json({ error: "Wrong history scope" }, 422);
      return history ? history(url) : json({ jobs: rows(url.searchParams.get("project_id") || ""), page: 1, per_page: 8, total: 2, total_pages: 1 });
    }
    const match = /^\/api(?:\/v2)?\/jobs\/(run-[^/]+)(?:\/(.*))?$/.exec(url.pathname);
    if (!match) return fallback ? fallback(input, init) : json({});
    const id = match[1], suffix = match[2];
    if (!suffix) detailReads.push(id);
    const detail = details.get(id);
    if (!detail) return json({ error: "Unknown job" }, 404);
    if (!suffix) {
      const next = (activeReads.get(id) || 0) + 1;
      activeReads.set(id, next); maximumConcurrentDetailReads = Math.max(maximumConcurrentDetailReads, next);
      try { return await (read?.(id) || json(detail)); }
      finally { activeReads.set(id, (activeReads.get(id) || 1) - 1); }
    }
    if (responses.has(`${id}/${suffix}`)) return json(responses.get(`${id}/${suffix}`));
    if (suffix === "definition") return json({ job_id: id, available: true, workflow_id: "weekly_report", revision: revisions.get(id) || 5,
      digest: `archive-${id}`, schema_version: 1, steps: [{ step_id: "plan", display_name: "Plan work", step_kind: "set_variable" }], groups: [], for_each: [],
      execution_graph: { schema_version: 1, entry: { kind: "step", id: "plan" }, edges: [] } });
    if (suffix === "execution-steps") return json({ job_id: id, run_terminal: true, child_invocations: [],
      steps: [{ step_id: "plan", step_execution_id: `${id}-plan`, occurrence: 1, attempt: 1, status: "completed", duration_ms: 0 }],
      page: { count: 1, has_more: false, next_cursor: "" } });
    if (suffix === "cost-breakdown") return json({ breakdown: [], total_cost_usd: 0 });
    if (suffix === "approvals") return json({ job_id: id, approvals: [] });
    if (suffix === "decision-overrides") return json({ job_id: id, overrides: [] });
    if (suffix === "cancel" && init?.method === "POST") {
      const cancelled = { ...detail, status: "cancelled", job: { ...detail.job, status: "cancelled" } };
      details.set(id, cancelled);
      return json({ job_id: id, status: "cancelled" });
    }
    return json({ job_id: id });
  });
  return { add, rows, details, detailReads, listReads, revisions, responses, maximumConcurrentReads: () => maximumConcurrentDetailReads,
    read: (handler: NonNullable<typeof read>) => { read = handler; }, history: (handler: NonNullable<typeof history>) => { history = handler; } };
}
function acknowledgeNewRun(fixture: ReturnType<typeof installFixture>) {
  let created = false;
  fixture.history((url) => {
    const rows = fixture.rows(url.searchParams.get("project_id") || "");
    const existing = rows.filter((row) => row.id !== "run-new");
    const fresh = rows.find((row) => row.id === "run-new");
    return json({ jobs: created && fresh ? [fresh, ...existing] : existing, page: 1, per_page: 8, total: existing.length + Number(created && Boolean(fresh)), total_pages: 1 });
  });
  mockedTestRunWorkflow.mockImplementation(async () => { created = true; return acknowledgement(); });
  mockedFetchWorkflowTestRunState.mockResolvedValue(acknowledgement());
}
async function bar(id = "run-latest") {
  return waitFor(() => {
    const region = screen.getByTestId("workflow-run-bar");
    expect(region).toHaveAttribute("data-job-id", id);
    return region;
  });
}
function barJobId() {
  return screen.queryByTestId("workflow-run-bar")?.getAttribute("data-job-id") || "";
}
async function chooseHistory(id: string) {
  // The kept test runs are listed in the Results panel.
  fireEvent.click(await screen.findByTestId(`workflow-test-run-row:${id}`));
}
async function results(id = "run-latest") {
  fireEvent.click(within(await bar(id)).getByRole("button", { name: "Results" }));
  expect(await screen.findByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", id);
  return screen.getByRole("dialog", { name: "Test Run results" });
}
async function launcher() {
  fireEvent.click(within(await screen.findByRole("toolbar", { name: "Canvas tools" })).getByRole("link", { name: "Test Run" }));
  return screen.findByRole("dialog", { name: /^Start a test run$/ });
}
async function chooseInitialHistory() {
  // With no active run, the server's latest finished run is shown by itself once its
  // own detail verifies; earlier runs are chosen in Results › Test runs.
  return bar("run-latest");
}
function noAuthoringWrite() {
  expect(postedChangeSets()).toEqual([]);
  expect(fetchSpy.mock.calls.filter(([, init]) => ["POST", "PATCH", "PUT", "DELETE"].includes(String(init?.method || "GET").toUpperCase()))).toEqual([]);
}

it("keeps the verified row/marks after closing Results and uses the actual bottom controls owner", async () => {
  const fixture = installFixture();
  renderPage({ surface: "builder", initialEntry: entry });
  await chooseInitialHistory();
  await waitFor(() => expect(mockedFetchWorkflowCanvasGraph).toHaveBeenCalledWith("weekly_report"));
  const row = await bar();
  expect(row).toHaveTextContent("Test Run finished");
  const controls = screen.getByTestId("workflow-canvas-controls");
  expect(controls).toContainElement(row);
  const toolbar = within(controls).getByRole("toolbar", { name: "Canvas tools" });
  expect(row.compareDocumentPosition(toolbar) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const drawer = await results();
  expect(controls).toHaveStyle({ right: drawer.style.width });
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument());
  expect(await bar()).toHaveAttribute("data-job-id", "run-latest");
  await waitFor(() => expect(within(screen.getByRole("group", { name: "Open step Plan work" })).getByTestId("workflow-runtime-mark")).toHaveAttribute("data-job-id", "run-latest"));
  await results();
  expect(fixture.maximumConcurrentReads()).toBe(1);
  expect(fixture.listReads.length).toBeGreaterThan(0);
  expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  noAuthoringWrite();
});

it("verifies uncached history while Results remains open, then hands off exactly that archived owner", async () => {
  const fixture = installFixture();
  const pending = deferred<Response>();
  fixture.read((id) => id === "run-older" ? pending.promise.then((response) => response.clone()) : undefined);
  fixture.revisions.set("run-older", 4);
  localStorage.setItem(ackKey, "run-latest");
  const view = renderPage({ surface: "builder", initialEntry: entry });
  await chooseInitialHistory();
  await results();
  const pointers = [localStorage.getItem(ackKey), localStorage.getItem(threadKey)];
  await chooseHistory("run-older");
  await waitFor(() => expect(fixture.detailReads).toContain("run-older"));
  expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
  expect(barJobId()).not.toBe("run-older");
  expect(screen.queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
  await act(async () => { pending.resolve(json(fixture.details.get("run-older"))); await pending.promise; });
  expect(await screen.findByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "run-older");
  expect(await bar("run-older")).toHaveTextContent("Run #1 · Test Run finished");
  expect(screen.queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
  fireEvent.click(within(await bar("run-older")).getByRole("button", { name: "Back to latest" }));
  expect(await bar()).toBeVisible();
  await waitFor(() => expect(screen.getByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "run-latest"));
  expect([localStorage.getItem(ackKey), localStorage.getItem(threadKey)]).toEqual(pointers);
  expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  noAuthoringWrite();
  view.unmount();
});

it("holds ACK navigation until matching detail commits; pending verification never reopens the launcher", async () => {
  const fixture = installFixture();
  const pending = deferred<Response>();
  fixture.add("run-new");
  fixture.read((id) => id === "run-new" ? pending.promise.then((response) => response.clone()) : undefined);
  acknowledgeNewRun(fixture);
  renderPage({ surface: "builder", initialEntry: entry });
  const dialog = await launcher();
  fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));
  await waitFor(() => expect(fixture.detailReads).toContain("run-new"));
  expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "Test Run results" })).not.toBeInTheDocument();
  expect(screen.getByText("Loading Test Run…")).toBeVisible();
  await act(async () => { pending.resolve(json(fixture.details.get("run-new"))); await pending.promise; });
  expect(await screen.findByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "run-new");
  expect(localStorage.getItem(ackKey)).toBe("run-new");
  expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
  noAuthoringWrite();
});

it("settles failed ACK detail verification with a scoped notice and no Results or launch retry", async () => {
  const fixture = installFixture();
  fixture.add("run-new");
  fixture.read((id) => id === "run-new" ? json({ message: "Detail temporarily unavailable" }, 503) : undefined);
  acknowledgeNewRun(fixture);
  renderPage({ surface: "builder", initialEntry: entry });
  const dialog = await launcher();
  fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));
  expect(await screen.findByText("The Test Run was started, but its details could not be verified. Select a run from history to inspect it.")).toBeVisible();
  expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Results" })).not.toBeInTheDocument();
  expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
  noAuthoringWrite();
});

it("does not label a new owner's launcher as failed after the old admitted request rejects", async () => {
  installFixture();
  localStorage.setItem(ackKey, "run-latest");
  const pending = deferred<Ack>();
  mockedTestRunWorkflow.mockImplementationOnce(() => pending.promise);
  const view = renderPage({ surface: "builder", initialEntry: entry });
  const first = await launcher();
  fireEvent.click(within(first).getByRole("button", { name: "Run workflow test run" }));
  await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1));
  await act(async () => { await view.router.navigate(entry.replace("project_alpha", "project_beta")); });
  await act(async () => { pending.reject(new Error("OLD_OWNER_START_FAILURE")); await pending.promise.catch(() => undefined); });
  const dialog = screen.queryByRole("dialog", { name: /^Start a test run$/ }) || await launcher();
  await waitFor(() => expect(within(dialog).getByRole("button", { name: "Run workflow test run" })).not.toHaveTextContent("Starting"));
  expect(within(dialog).queryByText("OLD_OWNER_START_FAILURE")).not.toBeInTheDocument();
  expect(within(dialog).queryByText("View previous test run")).not.toBeInTheDocument();
  expect(screen.queryByText("Previous test run — the latest start request failed.")).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(dialog).not.toBeInTheDocument());
  // The new owner's latest finished run is shown by itself; its Results stay one click away.
  expect(within(await bar("run-beta")).getByRole("button", { name: "Results" })).toBeVisible();
  expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
  expect(localStorage.getItem(ackKey)).toBe("run-latest");
  noAuthoringWrite();
});

it("retires a late ACK through project A→B→A without storing or opening its job", async () => {
  const fixture = installFixture();
  const pending = deferred<Ack>();
  mockedTestRunWorkflow.mockImplementationOnce(() => pending.promise);
  const view = renderPage({ surface: "builder", initialEntry: entry });
  const dialog = await launcher();
  fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));
  await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1));
  await act(async () => { await view.router.navigate(entry.replace("project_alpha", "project_beta")); });
  await act(async () => { await view.router.navigate(entry); });
  await act(async () => { fixture.add("run-stale"); pending.resolve(acknowledgement("run-stale")); await pending.promise; });
  await waitFor(() => expect(screen.getByRole("button", { name: "Run workflow test run" })).not.toHaveTextContent("Starting"));
  expect(localStorage.getItem(ackKey)).toBeNull();
  expect(fixture.detailReads).not.toContain("run-stale");
  expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
  expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
  noAuthoringWrite();
});

it("rechecks the selected owner after the actual drawer confirmation before any Results setters", async () => {
  const fixture = installFixture();
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps: [{ step_id: "rows", display_name: "Feed rows", step_kind: "tabular_feed",
    override: { tabular_feed: { operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.dataset" } } }], edges: [] }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ entry: { kind: "step", id: "rows" }, steps: [{ id: "rows", step_id: "rows", kind: "tabular_feed", display_name: "Feed rows", config_summary: "Create dataset", ports: [] }], edges: [] }));
  const view = renderPage({ surface: "builder", initialEntry: entry });
  await chooseInitialHistory();
  await bar();
  await waitFor(() => expect(window.editStep).toBeTypeOf("function"));
  act(() => window.editStep?.("rows"));
  fireEvent.change(await screen.findByRole("textbox", { name: "Rows" }), { target: { value: "[{" } });
  fireEvent.click(within(await bar()).getByRole("button", { name: "Results" }));
  const confirmation = await screen.findByRole("dialog", { name: "Discard invalid input?" });
  // Same-path project changes are allowed by the real leave guard. No modal
  // background click or private drawer setter is fabricated here.
  await act(async () => { await view.router.navigate(entry.replace("project_alpha", "project_beta")); });
  await waitFor(() => expect(fixture.listReads.some((url) => url.searchParams.get("project_id") === "project_beta")).toBe(true));
  fireEvent.click(within(confirmation).getByRole("button", { name: "Discard input" }));
  // The old owner's confirmed Results never open; the new project shows its own latest run.
  await bar("run-beta");
  expect(screen.queryByRole("dialog", { name: "Test Run results" })).not.toBeInTheDocument();
  expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
  expect(await screen.findByRole("textbox", { name: "Rows" })).toHaveValue("[{");
  expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  noAuthoringWrite();
});

// Earlier runs are chosen in Results › Test runs, which a step editor with invalid input
// cannot share the drawer with; the hook test covers a cancelled history choice.
it.each(["Results", "Back to latest"])("keeps invalid input/selection on Cancel from %s with one public confirmation", async (action) => {
  installFixture();
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps: [{ step_id: "rows", display_name: "Feed rows", step_kind: "tabular_feed",
    override: { tabular_feed: { operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.dataset" } } }], edges: [] }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ entry: { kind: "step", id: "rows" }, steps: [{ id: "rows", step_id: "rows", kind: "tabular_feed", display_name: "Feed rows", config_summary: "Create dataset", ports: [] }], edges: [] }));
  renderPage({ surface: "builder", initialEntry: entry });
  await chooseInitialHistory();
  await results(); await chooseHistory("run-older"); await bar("run-older");
  fireEvent.click(within(screen.getByRole("dialog", { name: "Test Run results" })).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument());
  await waitFor(() => expect(window.editStep).toBeTypeOf("function"));
  act(() => window.editStep?.("rows"));
  const rows = await screen.findByRole("textbox", { name: "Rows" });
  fireEvent.change(rows, { target: { value: "[{" } });
  expect(rows).toHaveAttribute("aria-invalid", "true");
  const initiator = within(await bar("run-older")).getByRole("button", { name: action });
  initiator.focus();
  fireEvent.click(initiator);
  const confirmation = await screen.findByRole("dialog", { name: "Discard invalid input?" });
  expect(screen.getAllByRole("dialog", { name: "Discard invalid input?" })).toHaveLength(1);
  fireEvent.click(within(confirmation).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(initiator).toHaveFocus());
  expect(await screen.findByRole("textbox", { name: "Rows" })).toHaveValue("[{");
  expect(await bar("run-older")).toBeVisible();
  expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
  expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  noAuthoringWrite();
});


function renderWithJobCache(queryClient: QueryClient) {
  // Same actual public page/provider/router boundary as renderPage, with an
  // explicit real query cache solely so a previous HTTP outcome can be seeded.
  const router = createMemoryRouter([
    { path: "/registry/workflows/:workflowId/builder", element: <WorkflowsDetailPage embedded surface="builder" /> },
  ], { initialEntries: [entry] });
  return renderAuthenticatedRouter(queryClient, router);
}

it.each(["error", "foreign detail"] as const)("keeps a cached %s pending through the Results-open ACK handoff until the current HTTP verification settles", async (cached) => {
  const fixture = installFixture();
  const real = fixture.add("run-new");
  acknowledgeNewRun(fixture);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const fresh = deferred<Response>();
  let unmount: (() => void) | undefined;
  try {
    const key = queryKeys.jobDetail("run-new");
    fixture.read((id) => id !== "run-new" ? undefined : cached === "error"
      ? json({ message: "PRIOR_CACHED_VERIFICATION_ERROR" }, 503)
      : json({ ...real, project_id: "project_foreign", task_summary: "CACHED_FOREIGN_DETAIL",
        job: { ...real.job, project_id: "project_foreign" } }));
    // Seed through actual fetchJson HTTP and the actual query client, rather than
    // mocking the selected hook or injecting a fabricated query result object.
    if (cached === "error") {
      await expect(queryClient.fetchQuery({ queryKey: key, queryFn: () => fetchJobDetail("run-new"), retry: false })).rejects.toBeInstanceOf(Error);
      expect(queryClient.getQueryState(key)?.status).toBe("error");
    } else {
      await queryClient.fetchQuery({ queryKey: key, queryFn: () => fetchJobDetail("run-new"), retry: false });
      expect(queryClient.getQueryData<Detail>(key)?.project_id).toBe("project_foreign");
    }
    const priorReads = fixture.detailReads.filter((id) => id === "run-new").length;
    expect(priorReads).toBe(1);
    fixture.read((id) => id === "run-new" ? fresh.promise.then((response) => response.clone()) : undefined);
    const view = renderWithJobCache(queryClient);
    unmount = view.unmount;
    await chooseInitialHistory();
    await results();
    const dialog = await launcher();
    fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));
    // This GET is the resumed selected verification after the OLD full-proof
    // Results handoff is retired; its real response remains held below.
    await waitFor(() => expect(fixture.detailReads.filter((id) => id === "run-new").length).toBeGreaterThan(priorReads));
    const loading = screen.getByRole("region", { name: "Loading Test Run…" });
    expect(loading).toHaveTextContent("Loading Test Run…");
    expect(loading).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("The Test Run was started, but its details could not be verified. Select a run from history to inspect it.")).not.toBeInTheDocument();
    expect(screen.queryByText("Test Run could not be verified")).not.toBeInTheDocument();
    expect(screen.queryByText("CACHED_FOREIGN_DETAIL")).not.toBeInTheDocument();
    expect(screen.queryByText("PRIOR_CACHED_VERIFICATION_ERROR")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Results" })).not.toBeInTheDocument();
    await act(async () => { fresh.resolve(json(real)); await fresh.promise; });
    expect(await screen.findByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "run-new");
    expect(await bar("run-new")).toHaveTextContent("Test Run finished");
    expect(screen.queryByText("The Test Run was started, but its details could not be verified. Select a run from history to inspect it.")).not.toBeInTheDocument();
    expect(localStorage.getItem(ackKey)).toBe("run-new");
    expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
    noAuthoringWrite();
  } finally {
    // Resolve even on an exceptional assertion/render path, then dispose only
    // this test's actual mounted page and query cache.
    await act(async () => { fresh.resolve(json(real)); await fresh.promise; });
    unmount?.();
    queryClient.clear();
  }
});

it("clears a terminal run through the real lower control while retaining history and local ACK storage", async () => {
  installFixture();
  localStorage.setItem(ackKey, "run-latest");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = renderWithJobCache(client);
  try {
    await chooseInitialHistory();
    const row = await bar();
    await waitFor(() => expect(screen.getByTestId("workflow-runtime-mark")).toHaveAttribute("data-job-id", "run-latest"));
    fireEvent.click(within(row).getByRole("button", { name: "Clear marks" }));
    await waitFor(() => expect(row).not.toBeInTheDocument());
    expect(screen.queryByText("No Test Run selected")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
    expect(localStorage.getItem(ackKey)).toBe("run-latest");
    await act(async () => { await client.invalidateQueries({ queryKey: ["jobs", "workflow-test-run-history"] }); });
    // Cleared stays cleared: the refreshed history does not bring the run back by itself.
    expect(barJobId()).toBe("");
    expect(screen.queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
    const dialog = await launcher();
    expect(within(dialog).queryByRole("combobox", { name: "Test Run history" })).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(localStorage.getItem(ackKey)).toBe("run-latest");
    noAuthoringWrite();
  } finally { view.unmount(); client.clear(); }
});

it("reaches the latest finished run after a reload, and earlier runs through its Results", async () => {
  installFixture();
  localStorage.setItem(ackKey, "run-latest");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = renderWithJobCache(client);
  try {
    expect(await bar()).toHaveTextContent(/^Test Run finished/);
    await waitFor(() => expect(screen.getByTestId("workflow-runtime-mark")).toHaveAttribute("data-job-id", "run-latest"));
    const drawer = await results();
    expect(within(drawer).getByRole("button", { name: /^Run #2 · Finished/ })).toHaveAttribute("aria-current", "true");
    expect(within(drawer).queryByRole("button", { name: /run-beta/ })).not.toBeInTheDocument();
    await chooseHistory("run-older");
    // Results stay open and show the chosen run once its own detail verifies.
    await waitFor(() => expect(screen.getByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "run-older"));
    expect(screen.getByRole("dialog", { name: "Test Run results" })).toBeVisible();
    expect(await bar("run-older")).toHaveTextContent("Run #1 · Test Run finished");
    expect(within(await bar("run-older")).getByRole("button", { name: "Back to latest" })).toBeVisible();
    expect(localStorage.getItem(ackKey)).toBe("run-latest");
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    noAuthoringWrite();
  } finally { view.unmount(); client.clear(); }
});

it("stops the verified active selected job only after the actual confirmation and reads its terminal state", async () => {
  const fixture = installFixture();
  const running = fixture.details.get("run-latest");
  if (!running) throw new Error("Run missing");
  fixture.details.set("run-latest", { ...running, status: "in_progress", job: { ...running.job, status: "in_progress" } });
  renderPage({ surface: "builder", initialEntry: entry });
  const row = await bar();
  expect(within(row).queryByRole("button", { name: "Clear marks" })).not.toBeInTheDocument();
  fireEvent.click(within(row).getByRole("button", { name: "Stop" }));
  const confirmation = await screen.findByRole("dialog", { name: "Stop Test Run?" });
  expect(fetchSpy.mock.calls.filter(([url]) => String(url).endsWith("/cancel"))).toEqual([]);
  fireEvent.click(within(confirmation).getByRole("button", { name: "Cancel" }));
  expect(fetchSpy.mock.calls.filter(([url]) => String(url).endsWith("/cancel"))).toEqual([]);
  fireEvent.click(within(await bar()).getByRole("button", { name: "Stop" }));
  const second = await screen.findByRole("dialog", { name: "Stop Test Run?" });
  fireEvent.click(within(second).getByRole("button", { name: "Stop" }));
  await waitFor(() => expect(fetchSpy.mock.calls.filter(([url, init]) => String(url).endsWith("/api/jobs/run-latest/cancel") && init?.method === "POST")).toHaveLength(1));
  await waitFor(async () => expect(within(await bar()).getByRole("button", { name: "Clear marks" })).toBeVisible());
  expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument();
  expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
});

it("opens the existing form for historical Run again without accepted input evidence or a direct request", async () => {
  installFixture();
  renderPage({ surface: "builder", initialEntry: entry });
  await bar();
  const first = await launcher();
  expect(within(first).queryByRole("combobox", { name: "Test Run history" })).not.toBeInTheDocument();
  fireEvent.change(within(first).getByLabelText("Test Run Summary"), { target: { value: "Separate new launch draft" } });
  fireEvent.click(within(first).getByRole("button", { name: "Cancel" }));
  fireEvent.click(within(await bar()).getByRole("button", { name: "Run again" }));
  const form = await screen.findByRole("dialog", { name: /^Start a test run$/ });
  expect(within(form).getByLabelText("Test Run Summary")).toHaveValue("Separate new launch draft");
  expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  expect(fetchSpy.mock.calls.filter(([url, init]) => String(url).endsWith("/restart") && init?.method === "POST")).toEqual([]);
});

it("uses the active lower Running control to open the selected Results without reopening or submitting the form", async () => {
  const fixture = installFixture();
  const running = fixture.details.get("run-latest");
  if (!running) throw new Error("Run missing");
  fixture.details.set("run-latest", { ...running, status: "in_progress", job: { ...running.job, status: "in_progress" } });
  renderPage({ surface: "builder", initialEntry: entry });
  await bar();
  const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
  fireEvent.click(within(toolbar).getByRole("button", { name: "Running" }));
  expect(await screen.findByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "run-latest");
  expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument();
  expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  expect(fixture.maximumConcurrentReads()).toBe(1);
});

it("opens the selected approval workspace from Review request without submitting a decision", async () => {
  const fixture = installFixture();
  const detail = fixture.details.get("run-latest")!;
  const gate = { approval_id: "gate-selected", job_id: "run-latest", gate_kind: "approval", step_id: "plan", step_execution_id: "run-latest-plan",
    thread_id: "thread-run-latest", message: "Approve this selected item?", content: "Selected published context" };
  fixture.details.set("run-latest", { ...detail, status: "awaiting_approval", job: { ...detail.job, status: "awaiting_approval" },
    lifecycle: { status: { code: "awaiting_approval", terminal: false }, approval: { state: "awaiting_approval", gate } } });
  fixture.responses.set("run-latest/approval", { job_id: "run-latest", approval_id: "gate-selected", payload: gate });
  renderPage({ surface: "builder", initialEntry: entry });
  // The Approval step's Test Run tab opens by itself with the request in view and focused.
  const decision = await screen.findByTestId("workflow-approval-decision");
  await waitFor(() => expect(within(decision).getByText("Approve this selected item?")).toHaveFocus());
  // The step declares no Outputs shown to the reviewer: the panel says so instead of guessing.
  expect(within(decision).getByText("This step shows nothing with its question.")).toBeVisible();
  expect(await bar()).toHaveTextContent("Decide in the panel");
  expect(within(await bar()).queryByRole("button", { name: "Review request" })).not.toBeInTheDocument();
  // With the panel closed, the run bar carries the request and Review request brings it back.
  fireEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByTestId("workflow-approval-decision")).not.toBeInTheDocument());
  fireEvent.click(within(await bar()).getByRole("button", { name: "Review request" }));
  const reopened = await screen.findByTestId("workflow-approval-decision");
  await waitFor(() => expect(within(reopened).getByText("Approve this selected item?")).toHaveFocus());
  expect(fetchSpy.mock.calls.filter(([url]) => String(url).includes("/api/approvals/"))).toEqual([]);
  noAuthoringWrite();
});

it.each([4, 5])("opens a failed occurrence in the archive for rev %s unless the live editor matches", async (revision) => {
  const fixture = installFixture();
  const detail = fixture.details.get("run-latest")!;
  fixture.details.set("run-latest", { ...detail, status: "failed", job: { ...detail.job, status: "failed" } });
  fixture.revisions.set("run-latest", revision);
  fixture.responses.set("run-latest/execution-steps", { job_id: "run-latest", run_terminal: true,
    run_error: { code: "transform.invalid", message: "Selected transform failed", step_id: "plan", step_execution_id: "failed-pass" },
    steps: [{ step_id: "plan", step_execution_id: "old-pass", occurrence: 1, status: "completed", output: { evidence: "Wrong earlier pass" } },
      { step_id: "plan", step_execution_id: "failed-pass", occurrence: 2, status: "failed", error: "Selected transform failed", output: { evidence: "Exact failed pass" } }],
    page: { has_more: false, next_cursor: "" }, child_invocations: [] });
  const originalRect = HTMLElement.prototype.getBoundingClientRect;
  const measure = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (!this.matches("dialog.workflow-builder-drawer")) return originalRect.call(this);
    // JSDOM has no layout: measure only the real drawer at its configured width.
    const width = Number.parseFloat(this.style.width);
    return new DOMRect(window.innerWidth - width, 64, width, window.innerHeight - 64);
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let view: ReturnType<typeof renderPage> | undefined;
  try {
    view = renderPage({ surface: "builder", initialEntry: entry, queryClient });
    await chooseInitialHistory();
    const row = await bar();
    await waitFor(() => expect(within(row).getByRole("button", { name: "Open step" })).toBeVisible());
    fireEvent.click(within(row).getByRole("button", { name: "Open step" }));
    if (revision === 5) {
      expect(await screen.findByRole("dialog", { name: "Step editor" })).toBeVisible();
      expect(screen.queryByTestId("job-step-inspector-drawer")).not.toBeInTheDocument();
    } else {
      // The archived failure opens in Results at exactly its own pass, not an earlier one.
      const resultsPanel = await screen.findByTestId("workflow-test-run-results");
      await waitFor(() => expect(within(resultsPanel).getAllByRole("button", { expanded: true })).toHaveLength(1));
      const [pass] = screen.getAllByTestId("workflow-run-pass").filter((row) => within(row).queryByRole("button", { expanded: true }));
      expect(pass).toHaveTextContent("Exact failed pass");
      expect(pass).not.toHaveTextContent("Wrong earlier pass");
      expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument();
    }
    noAuthoringWrite();
  } finally {
    view?.unmount();
    queryClient.clear();
    measure.mockRestore();
  }
});

it("opens a pass in place, then Results Escape returns focus to the same Results opener", async () => {
  const user = userEvent.setup();
  const fixture = installFixture();
  const originalRect = HTMLElement.prototype.getBoundingClientRect;
  const measure = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    // JSDOM has no layout; publish the real outer drawer's local inspector host.
    return this.classList.contains("workflow-builder-drawer")
      ? new DOMRect(window.innerWidth - 540, 64, 540, window.innerHeight - 64)
      : originalRect.call(this);
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = renderPage({ surface: "builder", initialEntry: entry, queryClient });
  await chooseInitialHistory();
  try {
    const opener = within(await bar()).getByRole("button", { name: "Results" });
    const pointers = [localStorage.getItem(ackKey), localStorage.getItem(threadKey)];
    await user.click(opener);
    const resultsDrawer = await screen.findByRole("dialog", { name: "Test Run results" });
    expect(await screen.findByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "run-latest");
    expect(within(await bar()).getByRole("button", { name: "Results" })).toBe(opener);
    const [pass] = await within(resultsDrawer).findAllByTestId("workflow-run-pass");
    const selectedRow = within(pass).getByRole("button", { name: /Plan work/ });
    await user.click(selectedRow);
    // The pass opens in place inside the same Results drawer; no nested layer opens.
    expect(selectedRow).toHaveAttribute("aria-expanded", "true");
    expect(selectedRow).toHaveFocus();
    expect(within(pass).getByRole("button", { name: "Open step" })).toBeVisible();
    expect(screen.getByRole("dialog", { name: "Test Run results" })).toBe(resultsDrawer);

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Test Run results" })).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
    expect(within(await bar()).getByRole("button", { name: "Results" })).toBe(opener);
    expect([localStorage.getItem(ackKey), localStorage.getItem(threadKey)]).toEqual(pointers);
    expect(fixture.maximumConcurrentReads()).toBe(1);
    noAuthoringWrite();
  } finally {
    view.unmount();
    queryClient.clear();
    measure.mockRestore();
  }
});


it("reads the run once when Results open, closes without handing observation off, and returns focus to its opener", async () => {
  const user = userEvent.setup();
  const fixture = installFixture();
  const originalRect = HTMLElement.prototype.getBoundingClientRect;
  const measure = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("workflow-builder-drawer")
      ? new DOMRect(window.innerWidth - 540, 64, 540, window.innerHeight - 64)
      : originalRect.call(this);
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = renderPage({ surface: "builder", initialEntry: entry, queryClient });
  await chooseInitialHistory();
  const { unmount: unmountLoading } = render(<RequestLoadingIndicator />);
  try {
    const opener = within(await bar()).getByRole("button", { name: "Results" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument());
    const pointers = [localStorage.getItem(ackKey), localStorage.getItem(threadKey)];
    const readsBeforeOpen = fixture.detailReads.length;
    await user.click(opener);
    const resultsDrawer = await screen.findByRole("dialog", { name: "Test Run results" });
    const [pass] = await within(resultsDrawer).findAllByTestId("workflow-run-pass");
    await user.click(within(pass).getByRole("button", { name: /Plan work/ }));
    // Opening Results reads the selected run's detail once, fresh; the run keeps one observer.
    await waitFor(() => expect(fixture.detailReads.slice(readsBeforeOpen)).toEqual(["run-latest"]));
    const readsBeforeClose = fixture.detailReads.length;
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Test Run results" })).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
    // Closing hands nothing off: no further detail read.
    expect(fixture.detailReads.slice(readsBeforeClose)).toEqual([]);
    expect(within(await bar()).getByRole("button", { name: "Results" })).toBe(opener);
    expect(opener).toBeEnabled();
    expect([localStorage.getItem(ackKey), localStorage.getItem(threadKey)]).toEqual(pointers);
    expect(fixture.maximumConcurrentReads()).toBe(1);
    noAuthoringWrite();
  } finally {
    view.unmount();
    unmountLoading();
    queryClient.clear();
    measure.mockRestore();
  }
});


it("keeps committed For each progress on the main Builder through Results and retires it with the selected run", async () => {
  const fixture = installFixture();
  const loop = { for_each_id: "pick-lines", display_name: "Walk the pick lines", over: "state.pick_lines", item_name: "line",
    body: { kind: "sequence", entry: { kind: "step", id: "plan" } } };
  const edges = [
    { source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "pick-lines", lane_id: null } },
    { source: { kind: "for_each", id: "pick-lines" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
  ];
  const steps = [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable", for_each_id: "pick-lines",
      override: { set_variable: { target: "state.plan", value: true } } },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ];
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, entry: { kind: "for_each", id: "pick-lines" },
    steps, for_each: [loop], edges }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    workflow: { id: "weekly_report", display_name: "Weekly Report", enabled: true, callable_by_parent: false, revision: 5 },
    entry: { kind: "for_each", id: "pick-lines" }, for_each: [{ ...loop, id: "pick-lines" }], edges,
    steps: [
      { id: "plan", step_id: "plan", kind: "set_variable", for_each_id: "pick-lines", display_name: "Plan work", config_summary: "state.plan",
        ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
    ],
  }));
  fixture.responses.set("run-latest/definition", { job_id: "run-latest", available: true, workflow_id: "weekly_report", revision: 5,
    snapshot_id: "pick-archive", digest: "archive-run-latest", schema_version: 1, steps, groups: [], for_each: [loop],
    execution_graph: { schema_version: 1, entry: { kind: "for_each", id: "pick-lines" }, edges } });
  fixture.responses.set("run-latest/execution-steps", { job_id: "run-latest", run_terminal: true, child_invocations: [],
    steps: [{ step_id: "plan", step_execution_id: "run-latest-plan", occurrence: 1, attempt: 1, status: "completed", duration_ms: 0 }],
    page: { count: 1, has_more: false, next_cursor: "" },
    container_progress: { schema_version: 1, job_id: "run-latest", project_id: "project_alpha", workflow_id: "weekly_report",
      snapshot_id: "pick-archive", revision: 5, digest: "archive-run-latest", covered_from_start: true, truncated: false,
      checkpoint_terminal: true, last_sequence: 8, passes: [{ container: { kind: "for_each", id: "pick-lines" },
        execution_id: "pick-pass", parent_frames: [], started_sequence: 1, updated_sequence: 8, finished_sequence: 8,
        current: false, outcome: "success", for_each: { item_index: 3, source_count: 5, selected_count: 5, processed_count: 4,
          applied_limit: 100, truncated: false, broke_at: 3 } }] },
  });
  const originalRect = HTMLElement.prototype.getBoundingClientRect;
  const measure = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (!this.matches("dialog.workflow-builder-drawer")) return originalRect.call(this);
    // JSDOM has no layout: measure only the real Results drawer at its configured width.
    const width = Number.parseFloat(this.style.width);
    return new DOMRect(window.innerWidth - width, 64, width, window.innerHeight - 64);
  });
  let view: ReturnType<typeof renderPage> | undefined;
  try {
    view = renderPage({ surface: "builder", initialEntry: entry });
    await chooseInitialHistory();
    await bar();
    await waitFor(() => expect(within(screen.getByRole("group", { name: "Open step Plan work" })).getByTestId("workflow-runtime-mark"))
      .toHaveAttribute("data-job-id", "run-latest"));
    const mainMark = await screen.findByTestId("workflow-container-runtime:pick-lines");
    expect(mainMark).toHaveTextContent("4 items done");
    expect(mainMark).toHaveAttribute("data-job-id", "run-latest");
    expect(mainMark).toHaveAttribute("data-execution-id", "pick-pass");
    expect(mainMark).not.toHaveTextContent(/5 of 5|Duration/);

    const drawer = await results();
    // The compact Test Run results: one row per pass, opening in place to what it read and wrote.
    expect(within(drawer).queryByTestId("job-definition-canvas")).not.toBeInTheDocument();
    const [pass] = await within(drawer).findAllByTestId("workflow-run-pass");
    fireEvent.click(within(pass).getByRole("button", { name: /Plan work/ }));
    expect(within(pass).getByText("No inputs were recorded.")).toBeVisible();
    expect(within(pass).getByRole("button", { name: "Open step" })).toBeVisible();
    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument());
    expect(screen.getByTestId("workflow-container-runtime:pick-lines")).toHaveTextContent("4 items done");
    await results();
    await chooseHistory("run-older");
    await bar("run-older");
    await waitFor(() => expect(screen.queryByTestId("workflow-container-runtime:pick-lines")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: /^For each item\s*Walk the pick lines/ })).toBeInTheDocument();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    noAuthoringWrite();
  } finally {
    view?.unmount();
    measure.mockRestore();
  }
});


it("keeps an idle canvas clear while the existing toolbar opens Test Run without starting a run", async () => {
  const fixture = installFixture();
  const pending = deferred<Response>();
  fixture.history(() => pending.promise);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = renderPage({ surface: "builder", initialEntry: entry, queryClient: client });
  try {
    await screen.findByRole("toolbar", { name: "Canvas tools" });
    await waitFor(() => expect(fixture.listReads.length).toBeGreaterThan(0));
    expect(screen.queryByText("No Test Run selected")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Test Run history" })).not.toBeInTheDocument();
    await act(async () => {
      pending.resolve(json({ jobs: [], page: 1, per_page: 8, total: 0, total_pages: 1 }));
      await pending.promise;
    });
    expect(screen.queryByRole("region", { name: /^Test Run/ })).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
    const dialog = await launcher();
    expect(within(dialog).queryByRole("combobox", { name: "Test Run history" })).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument());
    expect(within(screen.getByRole("toolbar", { name: "Canvas tools" })).getByRole("link", { name: "Test Run" })).toBeVisible();
    expect(screen.queryByText("No Test Run selected")).not.toBeInTheDocument();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    noAuthoringWrite();
  } finally {
    pending.resolve(json({ jobs: [], page: 1, per_page: 8, total: 0, total_pages: 1 }));
    view.unmount(); client.clear();
  }
});
