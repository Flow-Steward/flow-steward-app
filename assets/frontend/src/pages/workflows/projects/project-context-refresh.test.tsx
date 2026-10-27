import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider, Route, Routes, useLocation } from "react-router";
import { WorkflowsProjectsPage } from "@/pages/workflows/projects";
import { WorkflowsOverviewPage } from "@/pages/workflows/overview";
import { WorkflowsBuilderPage } from "@/pages/workflows/builder";

vi.mock("@/lib/auth/use-is-admin", () => ({ useIsAdmin: () => true, useCanReadWorkflowRegistry: () => true }));
vi.mock("@/lib/auth/useAuth", () => ({ useAuth: () => ({ session: { authenticated: true, roles: ["admin"], user: { roles: ["admin"], timezone: "UTC" } } }) }));

const workflowId = "context-refresh-fixture";
const rootPath = `/registry/workflows/${workflowId}`;
const contextKey = ["workflows", workflowId, "linked-project-id"];
const bindingKey = (id: string) => ["projects", "workflow-bindings", id];
const callable = { enabled: true, trigger_channels: ["schedule"], input_mode: "fields", strict_inputs: true, expected_inputs: [], output_mode: "none", expected_outputs: [], returned_files: [] };

function fixture(failSecond = false) {
  const linked = new Set<string>();
  const writes: Array<{ path: string; method: string; body: Record<string, unknown> }> = [];
  const unexpected: string[] = [];
  const historyReads: Array<Record<string, string>> = [];
  const projects = [{ id: "project-a", name: "Alpha project" }, { id: "project-b", name: "Beta project" }];
  const binding = (id: string) => ({ project_id: id, workflow_ids: linked.has(id) ? [workflowId] : [], operator_default_workflow_id: "", project_workflows: [] });
  const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
  const detail = { workflow_id: workflowId, display_name: "Context fixture", enabled: true, revision: 1, settings: { callable }, steps: [], groups: [] };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
    const path = url.pathname;
    const method = init?.method || "GET";
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
    if (method !== "GET") writes.push({ path, method, body });
    if (method === "GET" && path === "/api/v2/jobs") {
      const scope = Object.fromEntries(url.searchParams);
      historyReads.push(scope);
      const knownProject = ["project-a", "project-b"].find((projectId) => projectId === scope.project_id);
      const expected = { page: "1", per_page: "8", project_id: knownProject, workflow_id: workflowId, task_source: "workflow_test_run" };
      if (knownProject && JSON.stringify(Array.from(url.searchParams).sort()) === JSON.stringify(Object.entries(expected).sort())) {
        return json({ jobs: [], page: 1, per_page: 8, total: 0, total_pages: 0 });
      }
      unexpected.push(`${method} ${path}?${url.searchParams}`);
      return json({ detail: "Unexpected Test Run history scope" }, 422);
    }
    if (path === "/api/v2/projects") return json({ projects, page: 1, per_page: 200, total: 2, total_pages: 1 });
    const consumerProject = projects.find((item) => path === `/api/projects/${item.id}/workflows/${workflowId}/where-used`);
    if (method === "GET" && consumerProject) return json({ project_id: consumerProject.id, workflow_id: workflowId, consumers: [] });
    const match = /^\/api\/projects\/(project-[ab])\/workflows$/.exec(path);
    if (match) {
      const id = match[1];
      if (method === "PUT") {
        if (failSecond && id === "project-b") return json({ detail: "Owned fixture rejects Beta binding" }, 503);
        if ((body.workflow_ids as string[]).includes(workflowId)) linked.add(id);
        else linked.delete(id);
      }
      return json(binding(id));
    }
    if (path === `/api/workflows/${workflowId}`) return json(detail);
    if (path === `/api/workflows/${workflowId}/dag`) return json({ nodes: [], edges: [] });
    if (path === `/api/workflows/${workflowId}/contract-validation`) return json({ validation: { valid: true, errors: [], warnings: [] } });
    if (path === `/api/workflows/${workflowId}/contract`) return json({ ...detail, interface: callable, definition_issues: [] });
    if (path === `/api/workflows/${workflowId}/export-policy`) return json({ workflow_id: workflowId, extension_owned: false, protected: false, full_export_allowed: true, clone_allowed: true, structure_change_allowed: true });
    if (path === `/api/workflows/${workflowId}/stats`) return json({ workflow_id: workflowId, total_runs: 0, completed_count: 0, failed_count: 0, active_count: 0, cancelled_count: 0, success_rate: null, total_cost_usd: 0, avg_cost_per_run_usd: 0, jobs_with_cost: 0, last_run_at: null, avg_duration_seconds: null });
    if (path === `/api/workflows/${workflowId}/canvas-graph`) return json({ workflow: { id: workflowId, display_name: "Context fixture", enabled: true, callable_by_parent: false }, interface_contract: callable, output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] }, groups: [], steps: [], edges: [], validation_issues: [], automation_rules: [] });
    // The Builder reads kept test outputs (workflow-level, outside the definition) on open.
    if (method === "GET" && path === `/api/workflows/${workflowId}/kept-test-outputs`) return json({ workflow_id: workflowId, kept_outputs: [] });
    if (path === "/api/workflows/meta/callable-workflows") return json({ workflows: [] });
    if (path === "/api/workflows/meta/connector-catalog") return json({ connectors: [] });
    unexpected.push(`${method} ${path}`);
    return json({ detail: `Unexpected fixture request ${method} ${path}` }, 404);
  });
  return { linked, writes, unexpected, historyReads, binding };
}

function LocationReceipt() {
  const location = useLocation();
  return <output aria-label="Current route">{location.pathname}{location.search}</output>;
}
function renderJourney(path: string, client: QueryClient) {
  render(<QueryClientProvider client={client}><RouterProvider router={createMemoryRouter([{ path: "*", element: <><LocationReceipt /><Routes>
    <Route path="/registry/workflows/:workflowId/projects" element={<WorkflowsProjectsPage />} />
    <Route path="/registry/workflows/:workflowId" element={<WorkflowsOverviewPage />} />
    <Route path="/registry/workflows/:workflowId/builder" element={<WorkflowsBuilderPage />} />
  </Routes></> }], { initialEntries: [path] })} /></QueryClientProvider>);
}
function queryClient() { return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }); }

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(SVGElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, width: 100, height: 20, top: 0, right: 100, bottom: 20, left: 0, toJSON: () => ({}) });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each([false, true])("refreshes fresh empty context after binding Save (partial failure=%s)", async (partialFailure) => {
  const state = fixture(partialFailure);
  const client = queryClient();
  const freshAt = Date.now();
  client.setQueryData(contextKey, "", { updatedAt: freshAt });
  client.setQueryData(bindingKey("project-a"), state.binding("project-a"), { updatedAt: freshAt });
  client.setQueryData(bindingKey("project-b"), state.binding("project-b"), { updatedAt: freshAt });
  // The published checkbox hides its native input behind the visible mark.
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  renderJourney(`${rootPath}/projects`, client);
  await user.click(await screen.findByRole("checkbox", { name: "Toggle Alpha project" }));
  if (partialFailure) await user.click(screen.getByRole("checkbox", { name: "Toggle Beta project" }));
  expect(client.getQueryData(contextKey)).toBe("");
  await user.click(screen.getByRole("button", { name: "Save project bindings" }));
  await screen.findByText(partialFailure ? /Changes were not fully saved/ : "Project workflow bindings saved.");
  expect(state.linked).toEqual(new Set(["project-a"]));
  expect(state.writes).toEqual((partialFailure ? ["project-a", "project-b"] : ["project-a"]).map((id) => ({ path: `/api/projects/${id}/workflows`, method: "PUT", body: { workflow_ids: [workflowId] } })));
  await waitFor(() => expect(client.getQueryData(contextKey)).toBe("project-a"));
  expect(Date.now() - freshAt).toBeLessThan(30_000);
  expect(client.getQueryState(bindingKey("project-a"))?.isInvalidated).toBe(true);
  if (partialFailure) expect(client.getQueryData(bindingKey("project-b"))).toEqual({ project_id: "project-b", workflow_ids: [], operator_default_workflow_id: "", project_workflows: [] });
  const builder = screen.getByRole("link", { name: "Open builder" });
  expect(builder).toHaveAttribute("href", `${rootPath}/builder?project_id=project-a`);
  await user.click(builder);
  expect(screen.getByLabelText("Current route")).toHaveTextContent(`${rootPath}/builder?project_id=project-a`);
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^project-[ab]$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("preserves explicit Project context on all Overview Quick links and ordinary Builder navigation", async () => {
  const state = fixture();
  state.linked.add("project-a"); state.linked.add("project-b");
  const user = userEvent.setup();
  renderJourney(`${rootPath}?project_id=project-b`, queryClient());
  // The existing card has no landmark label; scope by its visible heading.
  // eslint-disable-next-line testing-library/no-node-access
  const card = (await screen.findByRole("heading", { name: "Quick links" })).closest("article");
  if (!card) throw new Error("Quick links article absent");
  const links = within(card);
  const builder = links.getByRole("link", { name: "Open builder" });
  for (const name of ["Open builder", "Linked projects", "Test Run"]) {
    const href = links.getByRole("link", { name }).getAttribute("href");
    expect(new URL(href || "", "http://localhost").searchParams.get("project_id")).toBe("project-b");
  }
  expect(new URL(links.getByRole("link", { name: "Test Run" }).getAttribute("href") || "", "http://localhost").searchParams.get("drawer")).toBe("workflow-test-run");
  await user.click(builder);
  expect(screen.getByLabelText("Current route")).toHaveTextContent("project_id=project-b");
  expect(state.writes).toEqual([]);
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^project-[ab]$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});
