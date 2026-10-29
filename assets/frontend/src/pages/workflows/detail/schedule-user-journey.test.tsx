import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider, Route, Routes, useLocation } from "react-router";

import { WorkflowsBuilderPage } from "@/pages/workflows/builder";
import { WorkflowsDetailPage } from "@/pages/workflows/detail";
import { WorkflowsOverviewPage } from "@/pages/workflows/overview";
import { WorkflowsProjectsPage } from "@/pages/workflows/projects";
import type { WorkflowContractField } from "@/pages/projects/detail/api";

// These page journeys take about 6 s when the quality gate shares the machine,
// over vitest's 5 s default. Per-test timeouts set below still take precedence.
vi.setConfig({ testTimeout: 10_000 });

// Authentication and transport are boundaries; pages, context discovery,
// bindings selectors, schedule editor and navigation remain real.
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
const auth = vi.hoisted(() => ({ admin: true }));
vi.mock("@/lib/auth/use-is-admin", () => ({ useIsAdmin: () => auth.admin, useCanReadWorkflowRegistry: () => true }));
vi.mock("@/lib/auth/useAuth", () => ({
  useAuth: () => ({ session: { authenticated: true, roles: ["admin"], user: { roles: ["admin"], timezone: "UTC" } } }),
}));

const workflowId = "schedule-fixture";
const builderPath = `/registry/workflows/${workflowId}/builder`;
const foreignProjectBindingPath = "/api/projects/foreign-project/workflows";
type Write = { path: string; method: string; body: Record<string, unknown> };
const callable = {
  enabled: true, trigger_channels: ["schedule"], input_mode: "fields",
  strict_inputs: true, expected_inputs: [], output_mode: "none",
  expected_outputs: [], returned_files: [],
};

function fixture({ linked = ["project-a", "project-b"], failProjects = false, failBinding = "", required = false, fields = [] as WorkflowContractField[], initialInputs = null as Record<string, unknown> | null, projectsReady = Promise.resolve() } = {}) {
  const linkedIds = new Set(linked);
  const writes: Write[] = [];
  const unexpected: string[] = [];
  const historyReads: Array<Record<string, string>> = [];
  const reads: string[] = [];
  const catalogSearches: string[] = [];
  const jobs: Array<Record<string, unknown>> = [];
  if (initialInputs) jobs.push({ id: "stored-1", project_id: "project-a", workflow_id: workflowId, status: "paused", enabled: false, cron_expr: "0 9 * * *", schedule_timezone: "UTC", inputs: structuredClone(initialInputs) });
  const contract = { ...callable, expected_inputs: required ? [{ name: "region", type: "string", required: true }] : fields };
  const projects = [{ id: "project-a", name: "Alpha project" }, { id: "project-b", name: "Beta project" }];
  const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
    status, headers: { "content-type": "application/json" },
  });
  const detail = {
    workflow_id: workflowId, display_name: "Schedule fixture", description: "Inert test workflow",
    enabled: true, revision: 1, settings: { callable: contract }, steps: [], groups: [],
  };
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
    const path = url.pathname;
    const method = init?.method || "GET";
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
    if (method !== "GET") writes.push({ path, method, body });
    else reads.push(path);
    if (method === "GET" && path === "/api/v2/jobs") {
      const scope = Object.fromEntries(url.searchParams);
      historyReads.push(scope);
      const knownProject = ["project-a", "project-b", "foreign-project"].find((projectId) => projectId === scope.project_id);
      const expected = { page: "1", per_page: "8", project_id: knownProject, workflow_id: workflowId, task_source: "workflow_test_run" };
      if (knownProject && JSON.stringify(Array.from(url.searchParams).sort()) === JSON.stringify(Object.entries(expected).sort())) {
        // The schedule URL-hint refusal intentionally probes this one inaccessible project.
        if (knownProject === "foreign-project") return json({ detail: "Selected project is not accessible" }, 403);
        return json({ jobs: [], page: 1, per_page: 8, total: 0, total_pages: 0 });
      }
      unexpected.push(`${method} ${path}?${url.searchParams}`);
      return json({ detail: "Unexpected Test Run history scope" }, 422);
    }
    if (path === "/api/v2/projects") {
      await projectsReady;
      return failProjects ? json({ detail: "Project catalog unavailable" }, 503)
        : json({ projects, page: 1, per_page: 200, total: 2, total_pages: 1 });
    }
    const consumerProject = projects.find((item) => path === `/api/projects/${item.id}/workflows/${workflowId}/where-used`);
    if (method === "GET" && consumerProject) return json({ project_id: consumerProject.id, workflow_id: workflowId, consumers: [] });
    if (method === "GET" && path === foreignProjectBindingPath) {
      return json({ detail: "Selected project is not accessible" }, 403);
    }
    const binding = /^\/api\/projects\/(project-[ab])\/workflows$/.exec(path);
    if (binding) {
      const id = binding[1];
      if (id === failBinding) return json({ detail: "Project binding unavailable" }, 503);
      if (method === "PUT") {
        if ((body.workflow_ids as string[]).includes(workflowId)) linkedIds.add(id);
        else linkedIds.delete(id);
      }
      return json({ project_id: id, workflow_ids: linkedIds.has(id) ? [workflowId] : [],
        operator_default_workflow_id: linkedIds.has(id) ? workflowId : "",
        project_workflows: linkedIds.has(id) ? [{ workflow_id: workflowId, display_name: "Schedule fixture", enabled: true }] : [] });
    }
    const scheduledJob = /^\/api\/projects\/(project-[ab])\/scheduled-jobs\/(stored-1|created-\d+)$/.exec(path);
    if (scheduledJob && method === "PUT") {
      const job = jobs.find((row) => row.project_id === scheduledJob[1] && row.id === scheduledJob[2]);
      if (!job) return json({ detail: "Schedule not found" }, 404);
      Object.assign(job, structuredClone(body));
      return json({ status: "updated", scheduled_job_id: job.id, job });
    }
    const schedules = /^\/api\/projects\/(project-[ab])\/scheduled-jobs$/.exec(path);
    if (schedules) {
      const projectId = schedules[1];
      if (method === "POST") {
        const job = { ...body, id: `created-${jobs.length + 1}`, project_id: projectId, status: "paused" };
        jobs.push(job);
        return json({ project_id: projectId, scheduled_job_id: job.id, status: "created", job }, 201);
      }
      return json({ project_id: projectId, jobs: jobs.filter((job) => job.project_id === projectId), runs: [], timezone_context: { project_schedule_timezone: "UTC" } });
    }
    const project = projects.find((item) => path === `/api/projects/${item.id}`);
    if (project) return json(project);
    const catalog = /^\/api\/projects\/(project-[ab])\/workflow-catalog$/.exec(path);
    if (catalog) {
      catalogSearches.push(url.searchParams.get("enabled_only") || "");
      return json({ project_id: catalog[1], workflows: linkedIds.has(catalog[1]) ? [{ workflow_id: workflowId, display_name: "Schedule fixture", enabled: true }] : [] });
    }
    if (path === `/api/workflows/${workflowId}/stats`) return json({ total_runs: 0, completed_count: 0, failed_count: 0, active_count: 0 });
    if (path === `/api/workflows/${workflowId}`) return json(detail);
    if (path === `/api/workflows/${workflowId}/contract`) return json({ ...detail, interface: contract, definition_issues: [] });
    if (path === `/api/workflows/${workflowId}/dag`) return json({ nodes: [], edges: [] });
    if (path === `/api/workflows/${workflowId}/contract-validation`) return json({ validation: { valid: true, errors: [], warnings: [] } });
    if (path === `/api/workflows/${workflowId}/export-policy`) return json({ workflow_id: workflowId, extension_owned: false, protected: false, full_export_allowed: true, clone_allowed: true, structure_change_allowed: true });
    if (path === `/api/workflows/${workflowId}/canvas-graph`) return json({
      workflow: { id: workflowId, display_name: "Schedule fixture", enabled: true, callable_by_parent: false },
      interface_contract: contract,
      output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
      groups: [], steps: [], edges: [], validation_issues: [], automation_rules: [],
    });
    // The builder reads the steps' kept test outputs (Test output); this workflow keeps none.
    if (path === `/api/workflows/${workflowId}/kept-test-outputs`) return json({ workflow_id: workflowId, kept_outputs: [] });
    if (path === "/api/workflows/meta/callable-workflows") return json({ workflows: [] });
    if (path === "/api/workflows/meta/connector-catalog") return json({ connectors: [] });
    unexpected.push(`${method} ${path}`);
    return json({ detail: `Unexpected fixture request: ${method} ${path}` }, 404);
  });
  return { writes, unexpected, historyReads, reads, jobs, linkedIds, catalogSearches };
}

function LocationReceipt() {
  const location = useLocation();
  return <output aria-label="Current route">{location.pathname}{location.search}</output>;
}
const ownedJourneyRenders = new Set<() => void>();

function renderJourney(path = builderPath, client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })) {
  // The schedule form starts on the signed-in user's timezone; pin it so the run does not follow the host's.
  const router = createMemoryRouter([{ path: "*", element: <><LocationReceipt /><Routes>
    <Route path="/registry/workflows/:workflowId/builder" element={<WorkflowsBuilderPage />} />
    <Route path="/registry/workflows/:workflowId/projects" element={<WorkflowsProjectsPage />} />
    <Route path="/registry/workflows/:workflowId" element={<WorkflowsOverviewPage />} />
    <Route path="/registry/workflows/:workflowId/settings" element={<WorkflowsDetailPage />} />
  </Routes></> }], { initialEntries: [path] });
  const view = render(<AuthContext.Provider value={{ session: { authenticated: true, user: { roles: ["admin"], timezone: "UTC" } } } as unknown as AuthContextValue}><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></AuthContext.Provider>);
  ownedJourneyRenders.add(() => {
    try {
      view.unmount();
    } finally {
      try {
        router.dispose();
      } finally {
        client.clear();
      }
    }
  });
  return client;
}

async function choose(user: ReturnType<typeof userEvent.setup>, label: string, option: string) {
  const selector = await screen.findByRole("combobox", { name: label });
  if (selector.tagName === "SELECT") await user.selectOptions(selector, screen.getByRole("option", { name: option }));
  else {
    await user.click(selector);
    await user.click(await screen.findByRole("option", { name: option }));
  }
}

/** Builder Schedules live in Workflow settings, opened from the workflow name. */
async function openBuilderSchedules(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Schedule fixture" }));
  await user.click(within(await screen.findByRole("dialog", { name: "Workflow settings" })).getByRole("tab", { name: "Schedules" }));
}

async function createPaused(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
}

beforeEach(() => {
  auth.admin = true;
  localStorage.clear();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  vi.spyOn(SVGElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, width: 100, height: 20, top: 0, right: 100, bottom: 20, left: 0, toJSON: () => ({}) });
});
afterEach(() => {
  const disposers = [...ownedJourneyRenders].reverse();
  ownedJourneyRenders.clear();
  const errors: unknown[] = [];
  for (const dispose of disposers) {
    try {
      dispose();
    } catch (error) {
      errors.push(error);
    }
  }
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (errors.length) throw new AggregateError(errors, "Failed to dispose schedule journey test renders");
});

it("authors object array items as JSON through normal Schedule POST and fresh reopen", async () => {
  const fields = [{ name: "source_rows", label: "Source feed rows", type: "array", required: true }];
  const state = fixture({ linked: ["project-a"], fields });
  const user = userEvent.setup();
  renderJourney(`${builderPath}?project_id=project-a&drawer=schedules`);
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  const editor = within(screen.getByRole("form", { name: "Schedule editor" }));
  await user.click(editor.getByRole("button", { name: "Add item" }));
  fireEvent.change(editor.getByRole("textbox", { name: "Source feed rows item 1" }), {
    target: { value: '{"sku":"A","price":10,"title":"Alpha"}' },
  });
  await choose(user, "Source feed rows item 1 Type", "JSON");
  await user.click(editor.getByRole("button", { name: "Add item" }));
  fireEvent.change(editor.getByRole("textbox", { name: "Source feed rows item 2" }), {
    target: { value: '{"sku":"B","price":20,"title":"Beta"}' },
  });
  await choose(user, "Source feed rows item 2 Type", "JSON");
  const scheduleReadsBeforePost = state.reads.filter((path) => path === "/api/projects/project-a/scheduled-jobs").length;
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  expect(state.writes).toEqual([{ path: "/api/projects/project-a/scheduled-jobs", method: "POST", body: {
    workflow_id: workflowId, cron_expr: "0 9 * * *", schedule_timezone: "UTC", enabled: false,
    inputs: { source_rows: [{ sku: "A", price: 10, title: "Alpha" }, { sku: "B", price: 20, title: "Beta" }] },
  } }]);
  expect(state.reads.filter((path) => path === "/api/projects/project-a/scheduled-jobs").length).toBeGreaterThan(scheduleReadsBeforePost);
  await user.click(screen.getByRole("button", { name: /^Edit schedule: / }));
  const reopenedEditor = within(screen.getByRole("form", { name: "Schedule editor" }));
  expect(reopenedEditor.getByRole("textbox", { name: "Source feed rows item 1" })).toHaveValue('{"sku":"A","price":10,"title":"Alpha"}');
  expect(reopenedEditor.getByRole("textbox", { name: "Source feed rows item 2" })).toHaveValue('{"sku":"B","price":20,"title":"Beta"}');
  expect(reopenedEditor.getByRole("combobox", { name: "Source feed rows item 1 Type" })).toHaveTextContent("JSON");
  expect(reopenedEditor.getByRole("combobox", { name: "Source feed rows item 2 Type" })).toHaveTextContent("JSON");
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("blocks invalid new JSON array drafts instead of submitting the last valid Schedule inputs", async () => {
  const fields = [{ name: "source_rows", label: "Source feed rows", type: "array", required: true }];
  const state = fixture({ linked: ["project-a"], fields });
  const user = userEvent.setup();
  renderJourney(`${builderPath}?project_id=project-a&drawer=schedules`);
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  await user.click(screen.getByRole("button", { name: "Add item" }));
  const item = screen.getByRole("textbox", { name: "Source feed rows item 1" });
  fireEvent.change(item, { target: { value: '{"sku":"A","price":10}' } });
  await choose(user, "Source feed rows item 1 Type", "JSON");
  fireEvent.change(item, { target: { value: '{"sku":' } });
  expect(item).toHaveValue('{"sku":');
  expect(screen.getByRole("alert")).toHaveTextContent("Source feed rows items must be valid JSON.");
  expect(screen.getByRole("button", { name: "Create schedule" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  expect(state.writes).toEqual([]);
  expect(state.jobs).toEqual([]);
  fireEvent.change(item, { target: { value: '{"sku":"B","price":21}' } });
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(state.writes).toHaveLength(1));
  expect(state.writes[0]).toMatchObject({ method: "POST", body: { inputs: { source_rows: [{ sku: "B", price: 21 }] } } });
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("defaults new array items to Text and keeps literal true and false strings in Schedule POST", async () => {
  const fields = [{ name: "source_rows", label: "Source feed rows", type: "array", required: true }];
  const state = fixture({ linked: ["project-a"], fields });
  const user = userEvent.setup();
  const utils = renderJourney(`${builderPath}?project_id=project-a&drawer=schedules`);
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  await user.click(screen.getByRole("button", { name: "Add item" }));
  expect(screen.getByRole("combobox", { name: "Source feed rows item 1 Type" })).toHaveTextContent("Text");
  fireEvent.change(screen.getByRole("textbox", { name: "Source feed rows item 1" }), { target: { value: "true" } });
  await user.click(screen.getByRole("button", { name: "Add item" }));
  expect(screen.getByRole("combobox", { name: "Source feed rows item 2 Type" })).toHaveTextContent("Text");
  fireEvent.change(screen.getByRole("textbox", { name: "Source feed rows item 2" }), { target: { value: "false" } });
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(state.writes).toHaveLength(1));
  expect(state.writes[0]).toMatchObject({ method: "POST", body: { inputs: { source_rows: ["true", "false"] } } });
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  await utils.invalidateQueries({ queryKey: ["projects", "scheduled-jobs", "project-a"] });
  await user.click(screen.getByRole("button", { name: /^Edit schedule: / }));
  expect(screen.getByRole("textbox", { name: "Source feed rows item 1" })).toHaveValue("true");
  expect(screen.getByRole("textbox", { name: "Source feed rows item 2" })).toHaveValue("false");
  expect(screen.getByRole("combobox", { name: "Source feed rows item 1 Type" })).toHaveTextContent("Text");
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("shows the project name in Test Run while preserving the project context", async () => {
  const state = fixture();
  renderJourney(`${builderPath}?project_id=project-b&drawer=workflow-test-run`);
  const project = await screen.findByRole("textbox", { name: "Project" });
  await waitFor(() => expect(project).toHaveValue("Beta project"));
  expect(project).toHaveAttribute("readonly");
  expect(screen.getByLabelText("Current route")).toHaveTextContent("project_id=project-b");
  expect(state.writes).toEqual([]);
});

it("does not show a raw project ID while Test Run project names are loading", async () => {
  let resolveProjects!: () => void;
  const projectsReady = new Promise<void>((resolve) => { resolveProjects = resolve; });
  const state = fixture({ projectsReady });
  renderJourney(`${builderPath}?project_id=project-b&drawer=workflow-test-run`);
  const project = await screen.findByRole("textbox", { name: "Project" });
  expect(project).toHaveValue("Loading project...");
  resolveProjects();
  await waitFor(() => expect(project).toHaveValue("Beta project"));
  expect(state.writes).toEqual([]);
});

it("shows an unavailable project name rather than an ID when the project read fails", async () => {
  const state = fixture({ failProjects: true });
  renderJourney(`${builderPath}?project_id=project-b&drawer=workflow-test-run`);
  const project = await screen.findByRole("textbox", { name: "Project" });
  await waitFor(() => expect(project).toHaveValue("Project unavailable"));
  expect(state.writes).toEqual([]);
});

it("opens Builder Schedules without a URL hint and explicitly schedules the second linked project", async () => {
  const state = fixture();
  const user = userEvent.setup();
  renderJourney();
  await openBuilderSchedules(user);
  expect(screen.queryByRole("button", { name: "New schedule" })).not.toBeInTheDocument();
  await choose(user, "Schedule project", "Beta project");
  await waitFor(() => expect(state.reads).toContain(`/api/projects/project-b/workflows/${workflowId}/where-used`));
  expect(state.writes).toEqual([]);
  await createPaused(user);
  expect(state.writes).toEqual([{ path: "/api/projects/project-b/scheduled-jobs", method: "POST", body: expect.objectContaining({ workflow_id: workflowId, enabled: false, cron_expr: "0 9 * * *", inputs: {} }) }]);
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it.each([
  { repeat: "Daily", cron: "0 9 * * *" },
  { repeat: "Weekly", cron: "0 9 * * 1" },
  { repeat: "Monthly", cron: "0 9 1 * *" },
])("creates a normal $repeat schedule without a single-option catchup selector", async ({ repeat, cron }) => {
  const state = fixture({ linked: ["project-a"] });
  const user = userEvent.setup();
  renderJourney(`${builderPath}?project_id=project-a&drawer=schedules`);
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  const editor = screen.getByRole("form", { name: "Schedule editor" });
  expect(within(editor).queryByRole("combobox", { name: "Catchup policy" })).not.toBeInTheDocument();
  expect(within(editor).queryByRole("combobox", { name: "Concurrency scope" })).not.toBeInTheDocument();
  expect(within(editor).queryByRole("textbox", { name: "Concurrency key" })).not.toBeInTheDocument();
  expect(within(editor).queryByRole("textbox", { name: "Schedule instructions" })).not.toBeInTheDocument();
  if (repeat !== "Daily") await user.click(within(screen.getByRole("group", { name: "Repeat" })).getByRole("button", { name: repeat }));
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  expect(state.writes).toEqual([{ path: "/api/projects/project-a/scheduled-jobs", method: "POST", body: {
    workflow_id: workflowId, cron_expr: cron,
    schedule_timezone: "UTC", enabled: false, inputs: {},
  } }]);
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("creates caller input values while leaving fixed workflow defaults to execution", async () => {
  const fields = [
    { name: "fixed_text", label: "Fixed text", type: "string", required: true, default: "", default_configured: true },
    { name: "fixed_flag", label: "Fixed flag", type: "boolean", required: true, default: false, default_configured: true },
    { name: "fixed_record", label: "Fixed record", type: "object", required: true, default: { enabled: true }, default_configured: true },
    { name: "count", label: "Count", type: "number", required: true, default_configured: false },
    { name: "enabled", label: "Caller flag", type: "boolean", default_configured: false },
  ];
  const state = fixture({ linked: ["project-a"], fields });
  const user = userEvent.setup();
  renderJourney(`${builderPath}?project_id=project-a&drawer=schedules`);
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  const editor = screen.getByRole("form", { name: "Schedule editor" });
  expect(within(editor).queryByRole("textbox", { name: /^Fixed text/ })).not.toBeInTheDocument();
  expect(within(editor).queryByRole("checkbox", { name: /^Fixed flag/ })).not.toBeInTheDocument();
  expect(within(editor).queryByRole("textbox", { name: /^Fixed record key/ })).not.toBeInTheDocument();
  await user.type(screen.getByLabelText(/^Count/), "12");
  await user.click(screen.getByText("Caller flag", { exact: true }));
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  expect(state.writes).toEqual([{ path: "/api/projects/project-a/scheduled-jobs", method: "POST", body: {
    workflow_id: workflowId, cron_expr: "0 9 * * *",
    schedule_timezone: "UTC", enabled: false, inputs: { count: 12, enabled: true },
  } }]);
  await user.click(screen.getByRole("button", { name: /^Edit schedule: / }));
  expect(screen.getByLabelText(/^Count/)).toHaveValue(12);
  expect(screen.getByRole("checkbox", { name: "Caller flag" })).toBeChecked();
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("removes stale saved values for configured defaults while preserving caller types and undeclared inputs", async () => {
  const fields = [
    { name: "fixed_flag", label: "Fixed flag", type: "boolean", default: false, default_configured: true },
    { name: "fixed_record", label: "Fixed record", type: "object", default: { configured: "current" }, default_configured: true },
    { name: "count", label: "Count", type: "number", required: true, default_configured: false },
    { name: "record", label: "Caller record", type: "object", default_configured: false },
  ];
  const state = fixture({ linked: ["project-a"], fields, initialInputs: {
    fixed_flag: true, fixed_record: { obsolete: "override" }, count: 5,
    record: { enabled: false, nested: [12, null] }, undeclared: { preserve: ["opaque"] },
  } });
  const user = userEvent.setup();
  const utils = renderJourney(`${builderPath}?project_id=project-a&drawer=schedules`);
  await user.click(await screen.findByRole("button", { name: /^Edit schedule: / }));
  await user.clear(screen.getByLabelText(/^Count/));
  await user.type(screen.getByLabelText(/^Count/), "7");
  await user.click(screen.getByRole("button", { name: "Save schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  expect(state.writes).toEqual([{ path: "/api/projects/project-a/scheduled-jobs/stored-1", method: "PUT", body: {
    workflow_id: workflowId, cron_expr: "0 9 * * *",
    schedule_timezone: "UTC", enabled: false,
    inputs: { count: 7, record: { enabled: false, nested: [12, null] }, undeclared: { preserve: ["opaque"] } },
  } }]);
  await utils.invalidateQueries({ queryKey: ["projects", "scheduled-jobs", "project-a"] });
  await user.click(screen.getByRole("button", { name: /^Edit schedule: / }));
  expect(screen.getByLabelText(/^Count/)).toHaveValue(7);
  expect(screen.getByRole("textbox", { name: "Caller record value 1" })).toHaveValue("false");
  expect(screen.getByRole("textbox", { name: "Caller record value 2" })).toHaveValue("[12,null]");
  expect(state.jobs[0]?.inputs).toEqual({ count: 7, record: { enabled: false, nested: [12, null] }, undeclared: { preserve: ["opaque"] } });
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("opens Schedules from the Overview's own tab", async () => {
  const state = fixture({ linked: ["project-a"] });
  const user = userEvent.setup();
  renderJourney(`/registry/workflows/${workflowId}`);
  // One click from the workflow page, no builder and no drawer in between.
  await user.click(await screen.findByRole("link", { name: "Schedules" }));
  await createPaused(user);
  expect(state.writes).toEqual([{ path: "/api/projects/project-a/scheduled-jobs", method: "POST", body: expect.objectContaining({ workflow_id: workflowId, enabled: false }) }]);
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it.each([
  { linked: [], message: /Link this workflow to a project before creating/ },
  { failProjects: true, message: /The project list is incomplete/ },
  { failBinding: "project-b", message: /The project list is incomplete/ },
])("never falls back to a project when link discovery is empty or incomplete ($message)", async ({ message, ...options }) => {
  const state = fixture(options);
  const user = userEvent.setup();
  renderJourney();
  await openBuilderSchedules(user);
  expect(await screen.findByText(message)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New schedule" })).not.toBeInTheDocument();
  expect(state.reads.filter((path) => path.includes("scheduled-jobs"))).toEqual([]);
  expect(state.writes).toEqual([]);
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("rejects an unlinked URL hint until a named linked project is selected", async () => {
  const state = fixture();
  const user = userEvent.setup();
  renderJourney(`${builderPath}?project_id=foreign-project`);
  await openBuilderSchedules(user);
  expect(await screen.findByText(/The requested project is not linked or accessible/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New schedule" })).not.toBeInTheDocument();
  expect(state.reads.some((path) => path.includes("scheduled-jobs"))).toBe(false);
  expect(state.reads.filter((path) => path.includes("/foreign-project/"))).toEqual([foreignProjectBindingPath]);
  await choose(user, "Schedule project", "Beta project");
  await screen.findByRole("button", { name: "New schedule" });
  // The project page has no schedules tab any more, so nothing links out to one.
  expect(screen.queryByRole("link", { name: "Open project schedules" })).not.toBeInTheDocument();
  expect(state.writes).toEqual([]);
  expect(state.reads.filter((path) => path.includes("/foreign-project/"))).toEqual([foreignProjectBindingPath]);
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("discards an unsaved editor on project switch and Cancel never writes", async () => {
  const state = fixture();
  const user = userEvent.setup();
  renderJourney();
  await openBuilderSchedules(user);
  await choose(user, "Schedule project", "Alpha project");
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  fireEvent.change(screen.getByLabelText("Time"), { target: { value: "12:34" } });
  await choose(user, "Schedule project", "Beta project");
  expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument();
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  expect(screen.getByLabelText("Time")).toHaveValue("09:00");
  await user.click(within(screen.getByTestId("workflow-schedule-editor-footer")).getByRole("button", { name: "Cancel" }));
  expect(state.writes).toEqual([]);
  await createPaused(user);
  expect(state.writes).toEqual([{ path: "/api/projects/project-b/scheduled-jobs", method: "POST", body: expect.objectContaining({ enabled: false }) }]);
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("shows loading instead of assuming a project and enforces required saved inputs", async () => {
  let release = () => {};
  const projectsReady = new Promise<void>((resolve) => { release = resolve; });
  const state = fixture({ linked: ["project-a"], required: true, projectsReady });
  const user = userEvent.setup();
  renderJourney();
  await openBuilderSchedules(user);
  expect(await screen.findByText("Loading linked projects…")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New schedule" })).not.toBeInTheDocument();
  release();
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  expect(state.writes).toEqual([]);
  await user.type(screen.getByLabelText(/^region/), "eu");
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(state.writes).toHaveLength(1));
  expect(state.writes[0]).toMatchObject({ path: "/api/projects/project-a/scheduled-jobs", method: "POST", body: { enabled: false, inputs: { region: "eu" } } });
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("creates and reopens a paused schedule with contract-derived structured inputs and a working Add row", async () => {
  const fields = [{ name: "record", label: "Record", type: "object" }, { name: "items", label: "Items", type: "array" }];
  const state = fixture({ linked: ["project-a"], fields });
  const user = userEvent.setup();
  const utils = renderJourney(`${builderPath}?project_id=project-a&drawer=schedules`);
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Record key 1" }), { target: { value: "code" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Record value 1" }), { target: { value: "001" } });
  await user.click(screen.getByRole("button", { name: "Add row" }));
  expect(screen.getByRole("textbox", { name: "Record key 2" })).toHaveValue("");
  fireEvent.change(screen.getByRole("textbox", { name: "Record key 2" }), { target: { value: "literal" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Record value 2" }), { target: { value: "true" } });
  await user.click(screen.getByRole("button", { name: "Add item" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Items item 1" }), { target: { value: "false" } });
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  expect(state.writes).toHaveLength(1);
  expect(state.jobs[0]).toMatchObject({ enabled: false, inputs: { record: { code: "001", literal: "true" }, items: ["false"] } });
  const previousReads = state.reads.filter((path) => path.endsWith("/scheduled-jobs")).length;
  await utils.invalidateQueries({ queryKey: ["projects", "scheduled-jobs", "project-a"] });
  await user.click(screen.getByRole("button", { name: /^Edit schedule: / }));
  expect(screen.getByRole("textbox", { name: "Record value 1" })).toHaveValue("001");
  expect(screen.getByRole("textbox", { name: "Record value 2" })).toHaveValue("true");
  expect(screen.getByRole("textbox", { name: "Items item 1" })).toHaveValue("false");
  expect(state.reads.filter((path) => path.endsWith("/scheduled-jobs")).length).toBeGreaterThan(previousReads);
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("blocks invalid structured edits and preserves nested types and undeclared values through save and fresh reopen", async () => {
  const initialInputs = { record: { enabled: true, count: 12, nested: { flags: [false, null] }, literal: "001" }, items: [{ sku: "A1" }, 5, false, null, "true"], legacy: { preserve: ["opaque"] } };
  const fields = [{ name: "record", label: "Record", type: "object" }, { name: "items", label: "Items", type: "array" }];
  const state = fixture({ linked: ["project-a"], fields, initialInputs });
  const user = userEvent.setup();
  const utils = renderJourney(`${builderPath}?project_id=project-a&drawer=schedules`);
  await user.click(await screen.findByRole("button", { name: /^Edit schedule: / }));
  const nested = screen.getByRole("textbox", { name: "Record value 3" });
  fireEvent.change(nested, { target: { value: '{"flags":' } });
  expect(nested).toHaveValue('{"flags":');
  await user.click(screen.getByRole("button", { name: "Save schedule" }));
  expect(state.writes).toEqual([]);
  expect(state.jobs[0]?.inputs).toEqual(initialInputs);
  fireEvent.change(nested, { target: { value: JSON.stringify(initialInputs.record.nested) } });
  const item = screen.getByRole("textbox", { name: "Items item 1" });
  fireEvent.change(item, { target: { value: '{"sku":' } });
  await user.click(screen.getByRole("button", { name: "Save schedule" }));
  expect(state.writes).toEqual([]);
  expect(item).toHaveValue('{"sku":');
  fireEvent.change(item, { target: { value: JSON.stringify(initialInputs.items[0]) } });
  await user.click(screen.getByRole("button", { name: "Save schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0]).toMatchObject({ method: "PUT", path: "/api/projects/project-a/scheduled-jobs/stored-1", body: { enabled: false, inputs: initialInputs } });
  expect(state.jobs[0]?.inputs).toEqual(initialInputs);
  const previousReads = state.reads.filter((path) => path.endsWith("/scheduled-jobs")).length;
  await utils.invalidateQueries({ queryKey: ["projects", "scheduled-jobs", "project-a"] });
  await user.click(screen.getByRole("button", { name: /^Edit schedule: / }));
  expect(state.reads.filter((path) => path.endsWith("/scheduled-jobs")).length).toBeGreaterThan(previousReads);
  expect(screen.getByRole("textbox", { name: "Record value 1" })).toHaveValue("true");
  expect(screen.getByRole("textbox", { name: "Record value 2" })).toHaveValue("12");
  expect(screen.getByRole("textbox", { name: "Record value 3" })).toHaveValue(JSON.stringify(initialInputs.record.nested));
  expect(screen.getByRole("textbox", { name: "Items item 1" })).toHaveValue(JSON.stringify(initialInputs.items[0]));
  expect(screen.getByRole("textbox", { name: "Items item 2" })).toHaveValue("5");
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});

it("drops a non-admin schedules deep link without loading an authoring context", async () => {
  const state = fixture();
  auth.admin = false;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderJourney(`/registry/workflows/${workflowId}/settings?drawer=schedules`, client);
  await waitFor(() => expect(screen.getByLabelText("Current route")).not.toHaveTextContent("drawer=schedules"));
  expect(client.getQueryState(["workflows", workflowId, "schedule-projects"])).toBeUndefined();
  expect(screen.queryByRole("button", { name: "New schedule" })).not.toBeInTheDocument();
  expect(state.writes).toEqual([]);
  for (const scope of state.historyReads) expect(scope).toEqual({ page: "1", per_page: "8", project_id: expect.stringMatching(/^(?:project-[ab]|foreign-project)$/), workflow_id: workflowId, task_source: "workflow_test_run" });
  expect(state.unexpected).toEqual([]);
});
