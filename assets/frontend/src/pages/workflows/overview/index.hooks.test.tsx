import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import type * as WorkflowApi from "@/pages/workflows/shared/api";
import type * as ProjectContext from "@/pages/workflows/shared/project-context";
import { act, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { useCanReadWorkflowRegistry, useIsAdmin } from "@/lib/auth/use-is-admin";
import { WorkflowsOverviewPage } from "@/pages/workflows/overview";
import { fetchWorkflowDetail, fetchWorkflowExportPolicy } from "@/pages/workflows/shared/api";
import type * as hookApi from "@/pages/workflows/shared/hook-api";
import { fetchWorkflowHooks, type WorkflowHook } from "@/pages/workflows/shared/hook-api";
import { fetchAllProjectsForWorkflowContext, findWorkflowLinkedProjectId } from "@/pages/workflows/shared/project-context";

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
  findWorkflowLinkedProjectId: vi.fn(),
}));

const mockedDetail = vi.mocked(fetchWorkflowDetail);
const mockedHooks = vi.mocked(fetchWorkflowHooks);
const mockedProjects = vi.mocked(fetchAllProjectsForWorkflowContext);
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
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route element={<WorkflowsOverviewPage />} path="/registry/workflows/:workflowId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function hooksCard() {
  expect(await screen.findByRole("heading", { name: "Hooks" })).toBeInTheDocument();
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
    expect(await screen.findByText("Empty")).toBeInTheDocument();
    expect(screen.getByText("Adjust the customer name before the order is finished.")).toBeInTheDocument();
    expect(await screen.findByText("Steps of project Orders demo.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Edit the steps of hook Before the order is finished" })).toHaveAttribute(
      "href",
      "/registry/workflows/wf/builder?drawer=step&step_id=before_finish&project_id=p-1",
    );
    expect(mockedHooks).toHaveBeenCalledWith("p-1", "wf", { presentation: "background" });
  });

  it("counts the steps the project keeps in a hook, never naming the workflow that holds them", async () => {
    mockedHooks.mockResolvedValue({ project_id: "p-1", workflow_id: "wf", hooks: [hookRow("my_name_fix")] });
    renderPage();

    expect(await screen.findByText("2 steps")).toBeInTheDocument();
    expect(screen.queryByText(/Fix the customer name/)).not.toBeInTheDocument();
  });

  it("reads the linked project when the URL names none", async () => {
    mockedHooks.mockResolvedValue({ project_id: "p-1", workflow_id: "wf", hooks: [hookRow()] });
    renderPage("/registry/workflows/wf?tab=hooks");

    expect(await screen.findByText("Empty")).toBeInTheDocument();
    expect(mockedLinkedProject).toHaveBeenCalledWith("wf", { presentation: "background" });
  });

  it("asks for a project link when the workflow is in none", async () => {
    mockedLinkedProject.mockResolvedValue("");
    renderPage("/registry/workflows/wf?tab=hooks");

    expect(await screen.findByText("Link this workflow to a project to connect its hooks.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Edit the steps of hook/ })).not.toBeInTheDocument();
    expect(mockedHooks).not.toHaveBeenCalled();
  });

  it("has no Hooks section when the workflow declares no hooks", async () => {
    mockedDetail.mockResolvedValue({ ...author, steps: [author.steps[0]] } as never);
    renderPage();

    expect(await screen.findByText("Hook sample — prepare an order")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Hooks" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Hooks" })).not.toBeInTheDocument();
    expect(mockedHooks).not.toHaveBeenCalled();
    expect(mockedProjects).not.toHaveBeenCalled();
    expect(mockedLinkedProject).not.toHaveBeenCalled();
  });

it("keeps actual late project→binding→hook→target HTTP reads nonmodal and preserves first-linked context", async () => {
  const actualApi = await vi.importActual<typeof WorkflowApi>("@/pages/workflows/shared/api");
  const actualContext = await vi.importActual<typeof ProjectContext>("@/pages/workflows/shared/project-context");
  const actualHooks = await vi.importActual<typeof hookApi>("@/pages/workflows/shared/hook-api");
  mockedDetail.mockImplementation(actualApi.fetchWorkflowDetail);
  vi.mocked(fetchWorkflowExportPolicy).mockImplementation(actualApi.fetchWorkflowExportPolicy);
  mockedProjects.mockImplementation(actualContext.fetchAllProjectsForWorkflowContext);
  mockedLinkedProject.mockImplementation(actualContext.findWorkflowLinkedProjectId);
  mockedHooks.mockImplementation(actualHooks.fetchWorkflowHooks);
  const waiting = new Map<string, (response: Response) => void>();
  const requested: string[] = [];
  const response = (value: unknown) => new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
  let projectLists = 0;
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const path = new URL(String(input), "http://localhost").pathname; requested.push(path);
    if (path === "/api/workflows/wf") return response({ ...author, portability: { clone_allowed: true, full_export_allowed: true, structure_change_allowed: true } });
    if (path === "/api/workflows/wf/export-policy") return response({ clone_allowed: true, structure_change_allowed: true });
    if (path === "/api/v2/projects") {
      projectLists += 1;
      if (projectLists > 1) return response({ projects: [{ id: "p-1", name: "Orders demo" }, { id: "p-2", name: "Second project" }], total_pages: 1 });
    }
    if (["/api/v2/projects", "/api/projects/p-1/workflows", "/api/projects/p-2/workflows", "/api/projects/p-1/workflows/wf/hooks", "/api/workflows/my_name_fix"].includes(path)) {
      return new Promise<Response>((resolve) => { waiting.set(path, resolve); });
    }
    throw new Error(`Unexpected request ${path}`);
  });
  vi.useFakeTimers();
  const view = renderPage("/registry/workflows/wf?tab=hooks");
  const { unmount: unmountIndicator } = render(<RequestLoadingIndicator />);
  try {
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText(author.display_name)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Projects" }); link.focus();
    async function assertUsable() {
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Projects" })).toBe(link); expect(link).toHaveFocus();
    }
    await assertUsable(); expect(waiting.has("/api/v2/projects")).toBe(true);
    await act(async () => { waiting.get("/api/v2/projects")?.(response({ projects: [{ id: "p-1", name: "Orders demo" }, { id: "p-2", name: "Second project" }], total_pages: 1 })); waiting.delete("/api/v2/projects"); await vi.advanceTimersByTimeAsync(0); });
    await assertUsable();
    // Completing the second binding first must not change the existing first-linked choice.
    await act(async () => { waiting.get("/api/projects/p-2/workflows")?.(response({ project_id: "p-2", workflow_ids: ["wf"] })); waiting.delete("/api/projects/p-2/workflows"); await vi.advanceTimersByTimeAsync(0); });
    await assertUsable();
    await act(async () => { waiting.get("/api/projects/p-1/workflows")?.(response({ project_id: "p-1", workflow_ids: ["wf"] })); waiting.delete("/api/projects/p-1/workflows"); await vi.advanceTimersByTimeAsync(0); });
    await assertUsable(); expect(waiting.has("/api/projects/p-1/workflows/wf/hooks")).toBe(true);
    const headerBuilderLinks = screen.getAllByRole("link", { name: /open builder/i }).filter((link) => link.getAttribute("data-workflow-header-action") === "builder");
    expect(headerBuilderLinks).toHaveLength(1);
    expect(headerBuilderLinks[0]).toHaveAttribute("href", "/registry/workflows/wf/builder?project_id=p-1");
    await act(async () => { waiting.get("/api/projects/p-1/workflows/wf/hooks")?.(response({ project_id: "p-1", workflow_id: "wf", hooks: [hookRow("my_name_fix")] })); waiting.delete("/api/projects/p-1/workflows/wf/hooks"); await vi.advanceTimersByTimeAsync(0); });
    await assertUsable(); expect(waiting.has("/api/workflows/my_name_fix")).toBe(true);
    await act(async () => { waiting.get("/api/workflows/my_name_fix")?.(response({ workflow_id: "my_name_fix", display_name: "Fix the customer name", steps: [] })); waiting.delete("/api/workflows/my_name_fix"); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("Empty")).toBeInTheDocument();
    await assertUsable();
    expect(requested.some((path) => /export-policy|\/dag|canvas/.test(path))).toBe(false);
  } finally {
    view.unmount(); unmountIndicator(); waiting.forEach((resolve) => resolve(response({}))); waiting.clear();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    vi.clearAllTimers(); vi.useRealTimers(); fetchMock.mockRestore();
  }
});

it("retains a real hook read error instead of presenting an unconnected successful state", async () => {
  mockedHooks.mockRejectedValue(new Error("Hook connection read denied"));
  renderPage();
  expect(await screen.findByText("Hook connection read denied")).toBeInTheDocument();
  expect(screen.queryByText("Empty")).not.toBeInTheDocument();
});

});
