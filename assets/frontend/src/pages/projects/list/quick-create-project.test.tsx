import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes } from "react-router";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { ProjectsListPage } from "@/pages/projects/list";
import { fetchModelSelectionOptions, fetchProjectsList } from "@/pages/projects/list/api";
import { fetchDataConnections } from "@/pages/registry/datasets/api";
import { fetchWorkspaces } from "@/pages/registry/workspaces/api";
import { fetchWorkflows } from "@/pages/workflows/shared/api";

vi.mock("@/pages/projects/list/api", () => ({
  fetchModelSelectionOptions: vi.fn(),
  fetchProjectsList: vi.fn(),
}));
vi.mock("@/pages/registry/workspaces/api", () => ({ fetchWorkspaces: vi.fn() }));
vi.mock("@/pages/registry/datasets/api", () => ({ fetchDataConnections: vi.fn() }));
vi.mock("@/pages/workflows/shared/api", () => ({ fetchWorkflows: vi.fn() }));

const workspace = { id: "ws-1", display_name: "Demo workspace" };
const adminAuth: AuthContextValue = {
  error: null,
  isAuthenticated: true,
  logout: async () => undefined,
  refresh: async () => undefined,
  switchAccount: async () => undefined,
  session: {
    authenticated: true,
    roles: ["admin"],
    user: { roles: ["admin"] },
  } as AuthContextValue["session"],
  status: "authenticated",
};

function renderPage() {
  return render(
    <AuthContext.Provider value={adminAuth}>
    <MemoryRouter initialEntries={["/projects"]}>
      <Routes>
        <Route element={<ProjectsListPage />} path="/projects" />
        <Route element={<><p>Created project detail</p><Link to="/projects">Back to projects</Link></>} path="/projects/project-new" />
      </Routes>
    </MemoryRouter>
    </AuthContext.Provider>,
  );
}

async function openCreate() {
  fireEvent.click(await screen.findByTestId("create-project"));
  return screen.findByRole("dialog", { name: "Create project" });
}

function spyOnCreateRequest() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async () => (
    new Response(JSON.stringify({ id: "project-new" }), {
      status: 201,
      headers: { "content-type": "application/json" },
    })
  ));
}

describe("Quick create project contract", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.resetAllMocks();
    globalThis.localStorage.clear();
    vi.mocked(fetchProjectsList).mockResolvedValue({ projects: [], total: 0 } as never);
    vi.mocked(fetchWorkspaces).mockResolvedValue([workspace] as never);
    // Keep a real choice in the list catalog: create must not silently assign it.
    vi.mocked(fetchWorkflows).mockResolvedValue([
      { workflow_id: "weekly_report", display_name: "Weekly report", enabled: true },
    ] as never);
    vi.mocked(fetchModelSelectionOptions).mockResolvedValue({
      provider_options: [], provider_models: {},
    });
    vi.mocked(fetchDataConnections).mockResolvedValue([]);
  });

  it.each([
    ["ws-1", "Demo workspace"],
    ["ws-2", "Second workspace"],
  ])("returns to the created project's chosen workspace %s instead of the previous list scope", async (workspaceId, workspaceName) => {
    localStorage.setItem("platform:selected-workspace-id", "ws-2");
    vi.mocked(fetchWorkspaces).mockResolvedValue([workspace, { id: "ws-2", display_name: "Second workspace" }] as never);
    let created = false;
    vi.mocked(fetchProjectsList).mockImplementation(async (options) => ({
      projects: created && options.workspaceId === workspaceId
        ? [{ id: "project-new", name: "Chosen workspace project", workspace_id: workspaceId, job_count: 0 }]
        : [],
      total: created && options.workspaceId === workspaceId ? 1 : 0,
    }) as never);
    const create = vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      created = true;
      return new Response(JSON.stringify({ id: "project-new" }), { status: 201, headers: { "content-type": "application/json" } });
    });
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderPage();
    const dialog = await openCreate();
    const selector = await within(dialog).findByRole("combobox", { name: "Workspace" });
    if (workspaceId !== "ws-2") {
      await user.click(selector);
      await user.click(await screen.findByRole("option", { name: workspaceName }));
    }
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Chosen workspace project" } });
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    await screen.findByText("Created project detail");
    await user.click(screen.getByRole("link", { name: "Back to projects" }));
    expect(await screen.findByText("Chosen workspace project")).toBeInTheDocument();
    expect(localStorage.getItem("platform:selected-workspace-id")).toBe(workspaceId);
    expect(fetchProjectsList).toHaveBeenLastCalledWith(expect.objectContaining({ workspaceId }));
    expect(JSON.parse(String(create.mock.calls[0]?.[1]?.body))).toEqual({ name: "Chosen workspace project", workspace_id: workspaceId, workflow_ids: [] });
  });

  it("preserves the previous workspace when creating in a different chosen workspace fails", async () => {
    localStorage.setItem("platform:selected-workspace-id", "ws-2");
    vi.mocked(fetchWorkspaces).mockResolvedValue([workspace, { id: "ws-2", display_name: "Second workspace" }] as never);
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("Create unavailable"));
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderPage();
    const dialog = await openCreate();
    await user.click(await within(dialog).findByRole("combobox", { name: "Workspace" }));
    await user.click(await screen.findByRole("option", { name: "Demo workspace" }));
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Unsaved project" } });
    await user.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await within(dialog).findByText("Create unavailable")).toBeInTheDocument();
    expect(localStorage.getItem("platform:selected-workspace-id")).toBe("ws-2");
    expect(screen.queryByText("Created project detail")).not.toBeInTheDocument();
  });

  it("offers only Name and Workspace, with one workspace as context rather than a choice", async () => {
    renderPage();
    const dialog = await openCreate();
    expect(await within(dialog).findByText("Demo workspace")).toBeInTheDocument();
    expect(within(dialog).getByRole("textbox", { name: "Name" })).toBeInTheDocument();
    expect(within(dialog).queryAllByRole("combobox")).toHaveLength(0);
    for (const label of ["Description", "Project rules", "Extra tools JSON"]) {
      expect(within(dialog).queryByLabelText(label)).not.toBeInTheDocument();
    }
    expect(within(dialog).queryByRole("group", { name: "Workflows" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("group", { name: "Data connections" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("group", { name: "Model routing" })).not.toBeInTheDocument();
    expect(dialog).not.toHaveTextContent("ws-1");
  });

  it("submits only required context and explicit empty workflow assignment, then enters the project", async () => {
    const fetchSpy = spyOnCreateRequest();
    renderPage();
    const dialog = await openCreate();
    await within(dialog).findByText("Demo workspace");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), {
      target: { value: "  New project  " },
    });
    fireEvent.submit(within(dialog).getByTestId("create-modal"));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalledWith(
      "/api/projects", expect.objectContaining({ method: "POST" }),
    ));
    const request = fetchSpy.mock.calls.find(([url, init]) => url === "/api/projects" && init?.method === "POST");
    expect(request).toBeDefined();
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({
      name: "New project", workspace_id: "ws-1", workflow_ids: [],
    });
    expect(await screen.findByText("Created project detail")).toBeInTheDocument();
  });

  it("does not fetch optional creation catalogs or let their outage block Create", async () => {
    vi.mocked(fetchModelSelectionOptions).mockRejectedValue(new Error("Models offline"));
    vi.mocked(fetchDataConnections).mockRejectedValue(new Error("Connections offline"));
    const fetchSpy = spyOnCreateRequest();
    renderPage();
    const dialog = await openCreate();
    await within(dialog).findByText("Demo workspace");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), {
      target: { value: "Unconfigured project" },
    });
    expect(within(dialog).getByRole("button", { name: "Create" })).toBeEnabled();
    expect(fetchModelSelectionOptions).not.toHaveBeenCalled();
    expect(fetchDataConnections).not.toHaveBeenCalled();
    // The list may still fetch workflows to resolve its existing summary labels.
    fireEvent.click(within(dialog).getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Created project detail")).toBeInTheDocument();
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/projects", expect.objectContaining({ method: "POST" }),
    );
  });

  it("does not send a request for a whitespace-only name", async () => {
    const fetchSpy = spyOnCreateRequest();
    renderPage();
    const dialog = await openCreate();
    await within(dialog).findByText("Demo workspace");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), {
      target: { value: "   " },
    });
    // Submit directly to exercise the application guard, not browser required.
    fireEvent.submit(within(dialog).getByTestId("create-modal"));
    await waitFor(() => expect(within(dialog).getByText("Project name is required.")).toBeInTheDocument());
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(screen.queryByText("Created project detail")).not.toBeInTheDocument();
  });

  it("does not send a request when there is no available workspace", async () => {
    vi.mocked(fetchWorkspaces).mockResolvedValue([]);
    const fetchSpy = spyOnCreateRequest();
    renderPage();
    await waitFor(() => expect(fetchWorkspaces).toHaveBeenCalled());
    const dialog = await openCreate();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), {
      target: { value: "No workspace" },
    });
    fireEvent.submit(within(dialog).getByTestId("create-modal"));
    await waitFor(() => expect(within(dialog).getByText("Workspace is required.")).toBeInTheDocument());
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(screen.queryByText("Created project detail")).not.toBeInTheDocument();
  });

  it("offers a real workspace choice only when multiple workspaces are available", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    vi.mocked(fetchWorkspaces).mockResolvedValue([
      workspace, { id: "ws-2", display_name: "Second workspace" },
    ] as never);
    const fetchSpy = spyOnCreateRequest();
    renderPage();
    const dialog = await openCreate();
    const select = await within(dialog).findByRole("combobox", { name: "Workspace" });
    await user.click(select);
    await user.click(await screen.findByRole("option", { name: "Second workspace" }));
    expect(select).toHaveTextContent("Second workspace");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), {
      target: { value: "Second team project" },
    });
    fireEvent.submit(within(dialog).getByTestId("create-modal"));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    const request = fetchSpy.mock.calls.find(([url, init]) => url === "/api/projects" && init?.method === "POST");
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({
      name: "Second team project", workspace_id: "ws-2", workflow_ids: [],
    });
    expect(await screen.findByText("Created project detail")).toBeInTheDocument();
  });

  it("blocks submission while the required workspace inventory is loading", async () => {
    vi.mocked(fetchWorkspaces).mockImplementation(() => new Promise(() => undefined));
    const fetchSpy = spyOnCreateRequest();
    renderPage();
    const dialog = await openCreate();
    expect(within(dialog).getByText("Loading workspaces...")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Create" })).toBeDisabled();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Pending inventory" } });
    fireEvent.submit(within(dialog).getByTestId("create-modal"));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("blocks submission when the required workspace inventory is unavailable", async () => {
    vi.mocked(fetchWorkspaces).mockRejectedValue(new Error("Workspaces offline"));
    const fetchSpy = spyOnCreateRequest();
    renderPage();
    const dialog = await openCreate();
    await within(dialog).findByText("Unable to load workspaces.");
    expect(within(dialog).getByRole("button", { name: "Create" })).toBeDisabled();
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Unavailable inventory" } });
    fireEvent.submit(within(dialog).getByTestId("create-modal"));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("never submits a stale foreign workspace preference instead of an available workspace", async () => {
    localStorage.setItem("platform:selected-workspace-id", "foreign-workspace");
    const fetchSpy = spyOnCreateRequest();
    renderPage();
    const dialog = await openCreate();
    await within(dialog).findByText("Demo workspace");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Owned project" } });
    fireEvent.submit(within(dialog).getByTestId("create-modal"));
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    const request = fetchSpy.mock.calls.find(([url, init]) => url === "/api/projects" && init?.method === "POST");
    expect(JSON.parse(String(request?.[1]?.body))).toEqual({ name: "Owned project", workspace_id: "ws-1", workflow_ids: [] });
    expect(await screen.findByText("Created project detail")).toBeInTheDocument();
  });
});
