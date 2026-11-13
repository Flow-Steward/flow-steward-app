import { LOADER_MIN_SHOWN_MS } from "@/components/shared/use-loader-delay";
import type * as WorkflowApi from "@/pages/workflows/shared/api";
import type * as ProjectContext from "@/pages/workflows/shared/project-context";
import { act, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createMemoryRouter, RouterProvider } from "react-router";

import { useCanReadWorkflowRegistry, useIsAdmin } from "@/lib/auth/use-is-admin";
import { WorkflowsOverviewPage } from "@/pages/workflows/overview";
import { fetchWorkflowDetail, fetchWorkflowExportPolicy } from "@/pages/workflows/shared/api";
import type * as hookApi from "@/pages/workflows/shared/hook-api";
import { fetchWorkflowHooks, type WorkflowHook } from "@/pages/workflows/shared/hook-api";
import { fetchAllProjectsForWorkflowContext, fetchWorkflowLinkedProjects, findWorkflowLinkedProjectId } from "@/pages/workflows/shared/project-context";

vi.mock("@/lib/auth/use-is-admin", () => ({
  useIsAdmin: vi.fn(),
  useCanReadWorkflowRegistry: vi.fn(),
}));

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflowDag: vi.fn(async () => ({ workflow_id: "wf", nodes: [], edges: [] })),
  fetchWorkflowDetail: vi.fn(),
  fetchWorkflowExportPolicy: vi.fn(async () => ({ workflow_id: "wf" })),
  fetchWorkflowStats: vi.fn(async () => ({ workflow_id: "wf", total_runs: 0 })),
  fetchWorkflowValidation: vi.fn(async () => ({ workflow_id: "wf", validation: { valid: true, errors: [], warnings: [] } })),
}));

vi.mock("@/pages/workflows/shared/hook-api", async (importOriginal) => ({
  ...(await importOriginal<typeof hookApi>()),
  fetchWorkflowHooks: vi.fn(),
}));

vi.mock("@/pages/workflows/shared/project-context", () => ({
  fetchAllProjectsForWorkflowContext: vi.fn(),
  fetchWorkflowLinkedProjects: vi.fn(),
  findWorkflowLinkedProjectId: vi.fn(),
}));

const mockedDetail = vi.mocked(fetchWorkflowDetail);
const mockedHooks = vi.mocked(fetchWorkflowHooks);
const mockedProjects = vi.mocked(fetchAllProjectsForWorkflowContext);
const mockedLinkedProjects = vi.mocked(fetchWorkflowLinkedProjects);
const mockedLinkedProject = vi.mocked(findWorkflowLinkedProjectId);

const author = {
  workflow_id: "wf",
  display_name: "Hook sample — prepare an order",
  enabled: true,
  steps: [
    { step_id: "prepare", step_kind: "set_variable", display_name: "Prepare" },
    {
      step_id: "before_finish",
      step_kind: "invoke_workflow",
      display_name: "Before the order is finished",
      override: {
        invoke_workflow: {
          hook: {
            hook_id: "before_finish",
            display_name: "Before the order is finished",
            description: "Adjust the customer name before the order is finished.",
          },
        },
      },
    },
  ],
};

function hookRow(targetWorkflowId = ""): WorkflowHook {
  return {
    project_id: "p-1",
    workflow_id: "wf",
    hook_id: "before_finish",
    step_id: "before_finish",
    display_name: "Before the order is finished",
    description: "Adjust the customer name before the order is finished.",
    contract_version: 1,
    hook_semantic_digest: "d",
    contract: { hook_id: "before_finish", contract_version: 1, display_name: "Before the order is finished" },
    binding_revision: targetWorkflowId ? 1 : 0,
    binding: targetWorkflowId
      ? { revision_id: "r1", target_workflow_id: targetWorkflowId, input_mapping: {}, output_mapping: {} }
      : null,
  };
}

function renderPage(entry = "/registry/workflows/wf?project_id=p-1&tab=hooks") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter([{ path: "/registry/workflows/:workflowId", element: <WorkflowsOverviewPage /> }], { initialEntries: [entry] });
  return { ...render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>), router, queryClient: client };
}

async function hooksCard() {
  await waitFor(() => expect(screen.getByRole("heading", { name: "Hooks" })).toBeInTheDocument());
  expect(within(screen.getByRole("table", { name: "Hooks" })).getByText("Before the order is finished")).toBeInTheDocument();
}

describe("Workflow overview Hooks section", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(fetchWorkflowExportPolicy).mockResolvedValue({ workflow_id: "wf" } as never);
    vi.mocked(useIsAdmin).mockReturnValue(true);
    vi.mocked(useCanReadWorkflowRegistry).mockReturnValue(true);
    mockedProjects.mockResolvedValue([{ id: "p-1", name: "Orders demo" }] as never);
    mockedLinkedProject.mockResolvedValue("p-1");
    mockedLinkedProjects.mockResolvedValue([{ id: "p-1", name: "Orders demo" }] as never);
    mockedDetail.mockImplementation(async (workflowId: string) => (
      workflowId === "my_name_fix" ? { workflow_id: "my_name_fix", display_name: "Fix the customer name", steps: [{ step_id: "fix_name" }, { step_id: "tidy" }, { step_id: "hook_end" }] } : author
    ) as never);
  });

  it("lists each hook as empty, with Edit steps opening its step in the builder", async () => {
    mockedHooks.mockResolvedValue({ project_id: "p-1", workflow_id: "wf", hooks: [hookRow()] });
    renderPage();

    await hooksCard();
    // Hooks is a tab of its own after Projects, not a card of the Overview.
    expect(screen.getByRole("link", { name: "Hooks" })).toHaveAttribute("aria-current", "page");
    expect(within(screen.getByRole("navigation", { name: "Workflow views" })).getAllByRole("link").map((link) => link.textContent))
      .toEqual(["Overview", "Projects", "Hooks", "Smart setup", "Automation", "History"]);
    expect(screen.queryByRole("heading", { name: "Run statistics" })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Empty")).toBeInTheDocument());
    expect(screen.getByText("Adjust the customer name before the order is finished.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Steps of project Orders demo.")).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Edit the steps of hook Before the order is finished" })).toHaveAttribute(
      "href",
      "/registry/workflows/wf/builder?drawer=step&step_id=before_finish&project_id=p-1",
    );
    expect(mockedHooks).toHaveBeenCalledWith("p-1", "wf");
  });

  it("counts the steps the project keeps in a hook, never naming the workflow that holds them", async () => {
    mockedHooks.mockResolvedValue({ project_id: "p-1", workflow_id: "wf", hooks: [hookRow("my_name_fix")] });
    renderPage();

    await waitFor(() => expect(screen.getByText("2 steps")).toBeInTheDocument());
    expect(screen.queryByText(/Fix the customer name/)).not.toBeInTheDocument();
  });

  it.each([1, 2])("requires an explicit choice with %i linked projects instead of reading the first Hook", async (count) => {
    mockedLinkedProjects.mockResolvedValue([{ id: "p-1", name: "Orders demo" }, { id: "p-2", name: "Second project" }].slice(0, count) as never);
    renderPage("/registry/workflows/wf?tab=hooks");
    const select = await screen.findByRole("combobox", { name: "Project for hooks" });
    expect(select).toHaveTextContent("Choose a linked project");
    expect(screen.queryByRole("link", { name: /Edit the steps of hook/ })).not.toBeInTheDocument();
    expect(mockedHooks).not.toHaveBeenCalled();
    expect(mockedLinkedProject).not.toHaveBeenCalled();
  });

  it("offers linking when no authorized project is linked without reading Hooks", async () => {
    mockedLinkedProject.mockResolvedValue("");
    mockedLinkedProjects.mockResolvedValue([]);
    renderPage("/registry/workflows/wf?tab=hooks");
    await waitFor(() => expect(screen.getByRole("link", { name: "Link a project" })).toHaveAttribute("href", "/registry/workflows/wf/projects"));
    expect(screen.queryByRole("link", { name: /Edit the steps of hook/ })).not.toBeInTheDocument();
    expect(mockedHooks).not.toHaveBeenCalled();
  });

  it.each(["deleted-project", "not-linked"])("rejects stale URL context %s without reading a different project's Hooks", async (projectId) => {
    mockedLinkedProjects.mockResolvedValue([{ id: "p-1", name: "Orders demo" }] as never);
    renderPage(`/registry/workflows/wf?tab=hooks&project_id=${projectId}`);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Project for hooks" })).toHaveTextContent("Choose a linked project"));
    expect(screen.queryByRole("link", { name: /Edit the steps of hook/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
    expect(mockedHooks).not.toHaveBeenCalled();
    expect(mockedLinkedProject).not.toHaveBeenCalled();
  });

  it.each([403, 404, 503])("retains a real %i binding read failure without reading Hooks or inventing a project", async (status) => {
    const actualContext = await vi.importActual<typeof ProjectContext>("@/pages/workflows/shared/project-context");
    mockedLinkedProjects.mockImplementation(actualContext.fetchWorkflowLinkedProjects);
    const requests: string[] = [];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = new URL(String(input), "http://localhost").pathname;
      requests.push(path);
      if (path === "/api/workflows/wf/linked-projects") return new Response(JSON.stringify({ detail: "Project bindings unavailable" }), { status, headers: { "content-type": "application/json" } });
      throw new Error(`Unexpected request ${path}`);
    });
    const view = renderPage("/registry/workflows/wf?tab=hooks&project_id=p-1");
    try {
      await waitFor(() => expect(screen.getByText("Linked projects could not be loaded")).toBeInTheDocument());
      expect(requests).toContain("/api/workflows/wf/linked-projects");
      expect(requests.filter((path) => /^\/api\/projects\/[^/]+\/workflows$/.test(path))).toEqual([]);
      expect(mockedHooks).not.toHaveBeenCalled();
      expect(mockedLinkedProject).not.toHaveBeenCalled();
      expect(screen.queryByRole("link", { name: /Edit the steps of hook/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
    } finally { view.unmount(); fetchMock.mockRestore(); }
  });

  it("withdraws cached project authority when a linked-project refresh fails", async () => {
    mockedHooks.mockResolvedValue({ project_id: "p-1", workflow_id: "wf", hooks: [hookRow()] });
    const view = renderPage();
    await screen.findByRole("link", { name: /Edit the steps of hook/ });
    const reads = mockedHooks.mock.calls.length;
    mockedLinkedProjects.mockRejectedValue(new Error("Project links unavailable"));
    await act(async () => { await view.queryClient.refetchQueries({ queryKey: ["workflows", "wf", "hook-projects"], type: "active" }); });
    await waitFor(() => expect(screen.getByText("Linked projects could not be loaded")).toBeInTheDocument());
    expect(screen.queryByRole("link", { name: /Edit the steps of hook/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
    expect(mockedHooks).toHaveBeenCalledTimes(reads);
  });

  it("shows declared Hooks to a non-author without reading project bindings or offering authoring", async () => {
    vi.mocked(useIsAdmin).mockReturnValue(false);
    renderPage();
    await screen.findByRole("heading", { name: "Hooks" });
    expect(screen.queryByRole("combobox", { name: "Project for hooks" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Edit the steps of hook/ })).not.toBeInTheDocument();
    expect(mockedLinkedProjects).not.toHaveBeenCalled();
    expect(mockedHooks).not.toHaveBeenCalled();
  });

  it("has no Hooks section when the workflow declares no hooks", async () => {
    mockedDetail.mockResolvedValue({ ...author, steps: [author.steps[0]] } as never);
    renderPage();

    await waitFor(() => expect(screen.getByText("Hook sample — prepare an order")).toBeInTheDocument());
    expect(screen.queryByRole("heading", { name: "Hooks" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Hooks" })).not.toBeInTheDocument();
    expect(mockedHooks).not.toHaveBeenCalled();
    expect(mockedProjects).not.toHaveBeenCalled();
    expect(mockedLinkedProject).not.toHaveBeenCalled();
  });

it("keeps explicit project B through real nonmodal reads, Edit steps, URL and a fresh reload", async () => {
  const actualApi = await vi.importActual<typeof WorkflowApi>("@/pages/workflows/shared/api");
  const actualContext = await vi.importActual<typeof ProjectContext>("@/pages/workflows/shared/project-context");
  const actualHooks = await vi.importActual<typeof hookApi>("@/pages/workflows/shared/hook-api");
  mockedDetail.mockImplementation(actualApi.fetchWorkflowDetail);
  vi.mocked(fetchWorkflowExportPolicy).mockImplementation(actualApi.fetchWorkflowExportPolicy);
  mockedProjects.mockImplementation(actualContext.fetchAllProjectsForWorkflowContext);
  mockedLinkedProject.mockImplementation(actualContext.findWorkflowLinkedProjectId);
  mockedLinkedProjects.mockImplementation(actualContext.fetchWorkflowLinkedProjects);
  mockedHooks.mockImplementation(actualHooks.fetchWorkflowHooks);
  const waiting = new Map<string, (response: Response) => void>();
  const requested: string[] = [];
  const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
  const bothProjects = [{ id: "p-1", name: "Orders demo" }, { id: "p-2", name: "Second project" }];
  // Every link read waits on one gate, so the page stays usable while it is open.
  let releaseLinks!: () => void;
  const linksGate = new Promise<void>((resolve) => { releaseLinks = resolve; });
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = new URL(String(input), "http://localhost").pathname; requested.push(path);
    if (path === "/api/workflows/wf") return response({ ...author, portability: { clone_allowed: true, full_export_allowed: true, structure_change_allowed: true } });
    if (path === "/api/workflows/wf/export-policy") return response({ clone_allowed: true, structure_change_allowed: true });
    if (path === "/api/v2/projects") return response({ projects: bothProjects, total_pages: 1 });
    if (path === "/api/workflows/wf/linked-projects") return linksGate.then(() => response({ workflow_id: "wf", projects: bothProjects }));
    if (["/api/projects/p-2/workflows/wf/hooks", "/api/workflows/my_name_fix"].includes(path)) {
      return new Promise<Response>((resolve) => { waiting.set(path, resolve); });
    }
    throw new Error(`Unexpected request ${path}`);
  });
  vi.useFakeTimers();
  const view = renderPage("/registry/workflows/wf?tab=hooks&account_id=account-7&from=overview");
  try {
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText(author.display_name)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Projects" }); link.focus();
    async function assertUsable(keepFocus = true) {
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(screen.getByRole("link", { name: "Projects" })).toBe(link);
      if (keepFocus) expect(link).toHaveFocus();
    }
    await assertUsable(); expect(requested).toContain("/api/workflows/wf/linked-projects");
    // Finding both linked projects must not select either without a user choice.
    await act(async () => { releaseLinks(); await vi.advanceTimersByTimeAsync(0); });
    // The linked-projects line loader was painted, so it holds its minimum time before the picker replaces it.
    await act(async () => { await vi.advanceTimersByTimeAsync(LOADER_MIN_SHOWN_MS); });
    await assertUsable();
    expect(requested.filter((path) => /^\/api\/projects\/[^/]+\/workflows$/.test(path))).toEqual([]);
    expect(requested.filter((path) => path.endsWith("/hooks"))).toEqual([]);
    const select = screen.getByRole("combobox", { name: "Project for hooks" });
    expect(select).toHaveTextContent("Choose a linked project");
    fireEvent.click(select);
    fireEvent.click(screen.getByRole("option", { name: "Second project" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(waiting.has("/api/projects/p-2/workflows/wf/hooks")).toBe(true);
    expect(view.router.state.location.search).toContain("project_id=p-2");
    expect(view.router.state.location.search).toContain("account_id=account-7");
    expect(view.router.state.location.search).toContain("from=overview");
    await act(async () => { waiting.get("/api/projects/p-2/workflows/wf/hooks")?.(response({ project_id: "p-2", workflow_id: "wf", hooks: [{ ...hookRow("my_name_fix"), project_id: "p-2" }] })); waiting.delete("/api/projects/p-2/workflows/wf/hooks"); await vi.advanceTimersByTimeAsync(0); });
    await assertUsable(false); expect(waiting.has("/api/workflows/my_name_fix")).toBe(true);
    await act(async () => { waiting.get("/api/workflows/my_name_fix")?.(response({ workflow_id: "my_name_fix", display_name: "Fix the customer name", steps: [] })); waiting.delete("/api/workflows/my_name_fix"); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("Empty")).toBeInTheDocument();
    await assertUsable(false);
    expect(screen.getByRole("link", { name: "Edit the steps of hook Before the order is finished" })).toHaveAttribute("href", "/registry/workflows/wf/builder?drawer=step&step_id=before_finish&project_id=p-2");
    const reloadEntry = `${view.router.state.location.pathname}${view.router.state.location.search}`;
    view.unmount();
    const reloadedRequests: string[] = [];
    fetchMock.mockImplementation(async (input) => {
      const path = new URL(String(input), "http://localhost").pathname;
      reloadedRequests.push(path);
      if (path === "/api/workflows/wf") return response({ ...author, portability: { clone_allowed: true, full_export_allowed: true, structure_change_allowed: true } });
      if (path === "/api/workflows/wf/export-policy") return response({ clone_allowed: true, structure_change_allowed: true });
      if (path === "/api/v2/projects") return response({ projects: bothProjects, total_pages: 1 });
      if (path === "/api/workflows/wf/linked-projects") return response({ workflow_id: "wf", projects: bothProjects });
      if (path === "/api/projects/p-2/workflows/wf/hooks") return response({ project_id: "p-2", workflow_id: "wf", hooks: [{ ...hookRow(), project_id: "p-2" }] });
      throw new Error(`Unexpected reload request ${path}`);
    });
    // Reload starts a fresh project → binding request chain. Await its visible
    // completion rather than assuming one fake-clock tick delivers every query notification.
    view.queryClient.clear();
    vi.useRealTimers();
    const { unmount: unmountReload } = renderPage(reloadEntry);
    try {
      await waitFor(() => expect(screen.getByRole("combobox", { name: "Project for hooks" })).toHaveTextContent("Second project"));
      await screen.findByRole("link", { name: "Edit the steps of hook Before the order is finished" });
      expect(reloadedRequests).toContain("/api/projects/p-2/workflows/wf/hooks");
      expect(reloadedRequests).not.toContain("/api/projects/p-1/workflows/wf/hooks");
      expect(screen.getByRole("link", { name: "Edit the steps of hook Before the order is finished" })).toHaveAttribute("href", "/registry/workflows/wf/builder?drawer=step&step_id=before_finish&project_id=p-2");
    } finally { unmountReload(); }
    expect(requested.some((path) => /export-policy|\/dag|canvas/.test(path))).toBe(false);
  } finally {
    view.unmount(); releaseLinks(); waiting.forEach((resolve) => resolve(response({}))); waiting.clear();
    if (vi.isFakeTimers()) {
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      vi.clearAllTimers();
    }
    vi.useRealTimers(); fetchMock.mockRestore();
  }
});

it("retains a real hook read error instead of presenting an unconnected successful state", async () => {
  mockedHooks.mockRejectedValue(new Error("Hook connection read denied"));
  renderPage();
  await waitFor(() => expect(screen.getByText("Hook connection read denied")).toBeInTheDocument());
  expect(screen.queryByText("Empty")).not.toBeInTheDocument();
});

});
