import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";

import { fetchProjectsList } from "@/pages/projects/list/api";
import { fetchProjectWorkflowBindings, saveProjectWorkflowBindings } from "@/pages/projects/workflows/api";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { WorkflowsProjectsPage } from "@/pages/workflows/projects";
import {
  fetchWorkflowDag,
  fetchWorkflowDetail,
  fetchWorkflowValidation,
} from "@/pages/workflows/shared/api";

vi.mock("@/pages/projects/list/api", () => ({
  fetchProjectsList: vi.fn(),
}));

vi.mock("@/pages/projects/workflows/api", () => ({
  fetchProjectWorkflowBindings: vi.fn(),
  saveProjectWorkflowBindings: vi.fn(),
}));

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflowDag: vi.fn(),
  fetchWorkflowDetail: vi.fn(),
  fetchWorkflowValidation: vi.fn(),
}));

const mockedFetchProjectsList = vi.mocked(fetchProjectsList);
const mockedFetchProjectWorkflowBindings = vi.mocked(fetchProjectWorkflowBindings);
const mockedSaveProjectWorkflowBindings = vi.mocked(saveProjectWorkflowBindings);
const mockedFetchWorkflowDag = vi.mocked(fetchWorkflowDag);
const mockedFetchWorkflowDetail = vi.mocked(fetchWorkflowDetail);
const mockedFetchWorkflowValidation = vi.mocked(fetchWorkflowValidation);

function renderPage(locale = "en") {
  const translations: Record<string, string> = {
    "react.workflows.project_workflow_count": "Darbo eigų: {count}",
    "react.shared.toggle_row": "Perjungti {title}",
  };
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, t: (key, text, params) => fallbackContext.t(key, locale === "lt" ? translations[key] || text : text, params) }}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/registry/workflows/wf_demo/projects"]}>
        <Routes>
          <Route element={<WorkflowsProjectsPage />} path="/registry/workflows/:workflowId/projects" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
    </I18nContext.Provider>,
  );
}

describe("WorkflowsProjectsPage", () => {
  beforeEach(() => {
    mockedFetchProjectsList.mockReset();
    mockedFetchProjectWorkflowBindings.mockReset();
    mockedSaveProjectWorkflowBindings.mockReset();
    mockedFetchWorkflowDetail.mockReset();
    mockedFetchWorkflowDag.mockReset();
    mockedFetchWorkflowValidation.mockReset();

    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "wf_demo",
      display_name: "Workflow Demo",
      description: "Demo workflow",
      enabled: true,
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({
      workflow_id: "wf_demo",
      nodes: [{ id: "analysis", step_id: "analysis" }],
      edges: [],
    } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      workflow_id: "wf_demo",
      validation: {
        valid: true,
        errors: [],
        warnings: [],
      },
    } as never);
    mockedFetchProjectsList.mockResolvedValue({
      projects: [
        { id: 7, name: "Project One" },
        { id: 8, name: "Project Two" },
      ],
      page: 1,
      per_page: 200,
      total: 2,
      total_pages: 1,
    } as never);
    mockedFetchProjectWorkflowBindings.mockImplementation(async (projectId) => ({
      project_id: projectId,
      operator_default_workflow_id: projectId === "7" ? "wf_demo" : "",
      workflow_ids: projectId === "7" ? ["wf_demo"] : [],
      project_workflows: [],
    }));
  });

  it("keeps a vanished failed project named without counting or retrying its hidden selection", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    let removed = false;
    const linked = new Set<string>();
    const projects = [{ id: 7, name: "Project One" }, { id: 8, name: "Project Two" }];
    mockedFetchProjectsList.mockImplementation(async () => ({
      projects: removed ? [projects[0]] : projects,
      page: 1, per_page: 200, total: removed ? 1 : 2, total_pages: 1,
    }) as never);
    mockedFetchProjectWorkflowBindings.mockImplementation(async (id) => ({
      project_id: id, workflow_ids: linked.has(id) ? ["wf_demo"] : [], operator_default_workflow_id: "", project_workflows: [],
    }));
    mockedSaveProjectWorkflowBindings.mockImplementation(async (id) => {
      if (id === "8") {
        removed = true;
        throw new Error("Project not found");
      }
      linked.add(id);
      return {} as never;
    });
    renderPage();
    await user.click(await screen.findByRole("checkbox", { name: "Toggle Project One" }));
    await user.click(screen.getByRole("checkbox", { name: "Toggle Project Two" }));
    await user.click(screen.getByRole("button", { name: "Save project bindings" }));
    expect(await screen.findByText(/Project Two: Project not found/)).toBeInTheDocument();
    expect(screen.getByText("1 linked")).toBeInTheDocument();
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    expect(screen.getAllByText("1 total")).toHaveLength(2);
    expect(screen.getByRole("checkbox", { name: "Toggle Project One" })).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "Toggle Project Two" })).not.toBeInTheDocument();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save project bindings" })).toBeDisabled();
    expect(screen.queryByText("Project workflow bindings saved.")).not.toBeInTheDocument();
    expect(mockedSaveProjectWorkflowBindings.mock.calls.map(([id]) => id)).toEqual(["7", "8"]);
  });

  it.each([
    { name: "disabled current workflow with no default", enabled: false, initialIds: [], initialDefault: "", nextIds: ["wf_demo"], nextDefault: "" },
    { name: "another link while the old default is disabled", enabled: true, initialIds: ["wf_old_disabled"], initialDefault: "wf_old_disabled", nextIds: ["wf_old_disabled", "wf_demo"], nextDefault: "wf_old_disabled" },
    { name: "unlinking the default without promoting a remaining disabled workflow", enabled: true, initialIds: ["wf_demo", "wf_remaining_disabled"], initialDefault: "wf_demo", nextIds: ["wf_remaining_disabled"], nextDefault: "" },
  ])("saves only links for $name", async ({ enabled, initialIds, initialDefault, nextIds, nextDefault }) => {
    mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "wf_demo", display_name: "Workflow Demo", enabled, steps: [], groups: [] } as never);
    mockedFetchProjectsList.mockResolvedValue({ projects: [{ id: 8, name: "Link-only Project" }], page: 1, per_page: 200, total: 1, total_pages: 1 } as never);
    let ids = initialIds;
    let operatorDefault = initialDefault;
    mockedFetchProjectWorkflowBindings.mockImplementation(async () => ({ project_id: "8", workflow_ids: ids, operator_default_workflow_id: operatorDefault, project_workflows: [] }));
    mockedSaveProjectWorkflowBindings.mockImplementation(async (_id, payload) => {
      if (payload.operator_default_workflow_id !== undefined) throw new Error("An explicit disabled default cannot be selected");
      ids = payload.workflow_ids;
      if (!ids.includes(operatorDefault)) operatorDefault = "";
      return { project_id: "8", workflow_ids: ids, operator_default_workflow_id: operatorDefault, project_workflows: [] };
    });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderPage();
    await user.click(await screen.findByRole("checkbox", { name: "Toggle Link-only Project" }));
    await user.click(screen.getByRole("button", { name: "Save project bindings" }));
    await waitFor(() => expect(mockedSaveProjectWorkflowBindings).toHaveBeenCalledWith("8", { workflow_ids: nextIds }));
    expect(await screen.findByText("Project workflow bindings saved.")).toBeInTheDocument();
    expect(ids).toEqual(nextIds);
    expect(operatorDefault).toBe(nextDefault);
    const savedToggle = screen.getByRole("checkbox", { name: "Toggle Link-only Project" });
    if (nextIds.includes("wf_demo")) expect(savedToggle).toBeChecked();
    else expect(savedToggle).not.toBeChecked();
  });

  it("keeps linked/default badges and counts persisted while selection changes remain unsaved", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderPage();
    await user.click(await screen.findByRole("checkbox", { name: "Toggle Project Two" }));
    const unsavedRow = screen.getByRole("row", { name: /Project Two/ });
    expect(within(unsavedRow).getByRole("checkbox")).toBeChecked();
    expect(within(unsavedRow).getByText("available")).toBeInTheDocument();
    expect(within(unsavedRow).getByText("Unsaved changes")).toBeInTheDocument();
    expect(within(unsavedRow).queryByText("linked")).not.toBeInTheDocument();
    expect(within(unsavedRow).queryByText("default")).not.toBeInTheDocument();
    expect(screen.getByText("1 linked")).toBeInTheDocument();
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    expect(mockedSaveProjectWorkflowBindings).not.toHaveBeenCalled();
  });

  it("names failed projects and retries only their unsaved selection after a partial save", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const linked = new Set<string>();
    let rejectSecond = true;
    mockedFetchProjectWorkflowBindings.mockImplementation(async (id) => ({ project_id: id, workflow_ids: linked.has(id) ? ["wf_demo"] : [], operator_default_workflow_id: "", project_workflows: [] }));
    mockedSaveProjectWorkflowBindings.mockImplementation(async (id) => {
      if (id === "8" && rejectSecond) throw new Error("Binding denied");
      linked.add(id);
      return {} as never;
    });
    renderPage();
    await user.click(await screen.findByRole("checkbox", { name: "Toggle Project One" }));
    await user.click(screen.getByRole("checkbox", { name: "Toggle Project Two" }));
    await user.click(screen.getByRole("button", { name: "Save project bindings" }));
    expect(await screen.findByText(/Project Two: Binding denied/)).toBeInTheDocument();
    expect(screen.queryByText("Project workflow bindings saved.")).not.toBeInTheDocument();
    const savedRow = screen.getByRole("row", { name: /Project One/ });
    const failedRow = screen.getByRole("row", { name: /Project Two/ });
    expect(within(savedRow).getByText("linked")).toBeInTheDocument();
    expect(within(savedRow).queryByText("Unsaved changes")).not.toBeInTheDocument();
    expect(within(failedRow).getByText("available")).toBeInTheDocument();
    expect(within(failedRow).getByText("Unsaved changes")).toBeInTheDocument();
    expect(within(failedRow).getByRole("checkbox")).toBeChecked();
    expect(screen.getByText("1 linked")).toBeInTheDocument();
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    rejectSecond = false;
    await user.click(screen.getByRole("button", { name: "Save project bindings" }));
    expect(await screen.findByText("Project workflow bindings saved.")).toBeInTheDocument();
    expect(mockedSaveProjectWorkflowBindings.mock.calls.map(([id]) => id)).toEqual(["7", "8", "8"]);
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it.each([0, 1, 3].flatMap((count) => ["lt", "en"].map((locale) => ({ count, locale }))))("localizes project workflow count $count in $locale while preserving authored names and IDs", async ({ count, locale }) => {
    const name = "Toggle Ąžuolas workflows";
    mockedFetchProjectsList.mockResolvedValue({ projects: [{ id: 7, name }], page: 1, per_page: 200, total: 1, total_pages: 1 } as never);
    mockedFetchProjectWorkflowBindings.mockResolvedValue({ project_id: "7", operator_default_workflow_id: "", workflow_ids: ["wf_demo", "wf_other", "wf_third"].slice(0, count), project_workflows: [] });
    renderPage(locale);
    expect(await screen.findByText(name)).toBeInTheDocument();
    expect(screen.getByText(locale === "lt" ? `Darbo eigų: ${count}` : `${count} workflows`)).toBeInTheDocument();
    const toggle = screen.getByRole("checkbox", { name: `${locale === "lt" ? "Perjungti" : "Toggle"} ${name}` });
    if (count > 0) expect(toggle).toBeChecked();
    else expect(toggle).not.toBeChecked();
    expect(screen.getByText("#7")).toBeInTheDocument();
    expect(mockedFetchProjectWorkflowBindings).toHaveBeenCalledWith("7");
  });

  it("renders project bindings in the workflow projects tab", async () => {
    renderPage();

    expect(await screen.findByText("Workflow Demo")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Projects" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(await screen.findByRole("button", { name: "Save project bindings" })).toBeInTheDocument();
    expect(await screen.findByText("Project One")).toBeInTheDocument();
    expect(screen.getByText("Project Two")).toBeInTheDocument();

    await waitFor(() => expect(mockedFetchProjectWorkflowBindings).toHaveBeenCalled());
    expect(mockedFetchProjectWorkflowBindings).toHaveBeenCalledWith("7");
    expect(mockedFetchProjectWorkflowBindings).toHaveBeenCalledWith("8");
    expect(mockedFetchProjectsList).toHaveBeenCalledWith({
      page: 1,
      perPage: 200,
      search: "",
      includeAllWorkspaces: true,
    });
  });

  it("round-trips an initially empty project through link and unlink", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    let workflowIds: string[] = [];
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: 8, name: "Empty Project" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    } as never);
    mockedFetchProjectWorkflowBindings.mockImplementation(async () => ({
      project_id: "8",
      operator_default_workflow_id: "",
      workflow_ids: workflowIds,
      project_workflows: [],
    }));
    mockedSaveProjectWorkflowBindings.mockImplementation(async (_projectId, payload) => {
      workflowIds = Array.isArray(payload.workflow_ids)
        ? payload.workflow_ids.map((workflowId) => String(workflowId))
        : [];
      return {} as never;
    });
    renderPage();

    const toggle = await screen.findByRole("checkbox", { name: "Toggle Empty Project" });
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "Save project bindings" }));
    await waitFor(() => {
      expect(mockedSaveProjectWorkflowBindings).toHaveBeenLastCalledWith("8", {
        workflow_ids: ["wf_demo"],
      });
    });

    await waitFor(() => expect(toggle).toBeChecked());
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "Save project bindings" }));
    await waitFor(() => {
      expect(mockedSaveProjectWorkflowBindings).toHaveBeenLastCalledWith("8", {
        workflow_ids: [],
      });
    });
    expect(screen.queryByText(/must keep at least one workflow/i)).not.toBeInTheDocument();
  });

  it("uses canonical workflow_ids even when stale metadata still contains a workflow", async () => {
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: 7, name: "Stale Project" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    } as never);
    mockedFetchProjectWorkflowBindings.mockResolvedValue({
      project_id: "7",
      workflow_ids: [],
      operator_default_workflow_id: "",
      project_workflows: [
        { workflow_id: "wf_demo", enabled: true, deleted_at: null },
      ],
    });

    renderPage();

    expect(await screen.findByRole("checkbox", { name: "Toggle Stale Project" })).not.toBeChecked();
    expect(screen.getByText("0 linked")).toBeInTheDocument();
    expect(screen.getByText("0 selected")).toBeInTheDocument();
  });

  it("retains the draft and clearly marks it unsaved after a failed PUT", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: 8, name: "Rejected Project" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    } as never);
    mockedFetchProjectWorkflowBindings.mockResolvedValue({
      project_id: "8",
      workflow_ids: [],
      operator_default_workflow_id: "",
      project_workflows: [],
    });
    mockedSaveProjectWorkflowBindings.mockRejectedValue(new Error("Unknown workflow id"));
    renderPage();

    const toggle = await screen.findByRole("checkbox", { name: "Toggle Rejected Project" });
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "Save project bindings" }));

    expect(await screen.findByText(/^Changes were not fully saved\..*unsaved changes remain\./i)).toBeInTheDocument();
    expect(screen.getByText(/unknown workflow id/i)).toBeInTheDocument();
    expect(toggle).toBeChecked();
    expect(screen.queryByText("Project workflow bindings saved.")).not.toBeInTheDocument();
  });

  it("claims success only after refetch confirms the persisted binding", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    let resolveRefetch: (() => void) | undefined;
    const refetchGate = new Promise<void>((resolve) => {
      resolveRefetch = resolve;
    });
    let fetchCount = 0;
    mockedFetchProjectsList.mockResolvedValue({
      projects: [{ id: 8, name: "Confirmed Project" }],
      page: 1,
      per_page: 200,
      total: 1,
      total_pages: 1,
    } as never);
    mockedFetchProjectWorkflowBindings.mockImplementation(async () => {
      fetchCount += 1;
      if (fetchCount > 1) {
        await refetchGate;
      }
      return {
        project_id: "8",
        workflow_ids: fetchCount > 1 ? ["wf_demo"] : [],
        operator_default_workflow_id: fetchCount > 1 ? "wf_demo" : "",
        project_workflows: [],
      };
    });
    mockedSaveProjectWorkflowBindings.mockResolvedValue({} as never);
    renderPage();

    const toggle = await screen.findByRole("checkbox", { name: "Toggle Confirmed Project" });
    await user.click(toggle);
    await user.click(screen.getByRole("button", { name: "Save project bindings" }));
    await waitFor(() => expect(mockedSaveProjectWorkflowBindings).toHaveBeenCalledOnce());
    expect(screen.queryByText("Project workflow bindings saved.")).not.toBeInTheDocument();

    resolveRefetch?.();
    expect(await screen.findByText("Project workflow bindings saved.")).toBeInTheDocument();
    expect(toggle).toBeChecked();
  });
});
