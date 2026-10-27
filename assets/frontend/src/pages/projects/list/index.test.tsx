import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";

import { fetchModelSelectionOptions, fetchProjectsList } from "@/pages/projects/list/api";
import { ProjectsListPage } from "@/pages/projects/list";
import { fetchDataConnections } from "@/pages/registry/datasets/api";
import { fetchWorkspaces } from "@/pages/registry/workspaces/api";
import { fetchWorkflows } from "@/pages/workflows/shared/api";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

vi.mock("@/pages/projects/list/api", () => ({
  fetchModelSelectionOptions: vi.fn(),
  fetchProjectsList: vi.fn(),
}));

vi.mock("@/pages/registry/workspaces/api", () => ({
  fetchWorkspaces: vi.fn(),
}));

vi.mock("@/pages/registry/datasets/api", () => ({
  fetchDataConnections: vi.fn(),
}));

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflows: vi.fn(),
}));

const mockedFetchProjectsList = vi.mocked(fetchProjectsList);
const mockedFetchModelSelectionOptions = vi.mocked(fetchModelSelectionOptions);
const mockedFetchDataConnections = vi.mocked(fetchDataConnections);
const mockedFetchWorkspaces = vi.mocked(fetchWorkspaces);
const mockedFetchWorkflows = vi.mocked(fetchWorkflows);

type Role = "admin" | "operator" | "viewer";

function authValue(role: Role): AuthContextValue {
  return {
    error: null,
    isAuthenticated: true,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    session: {
      authenticated: true,
      roles: [role],
      user: { roles: [role] },
    } as AuthContextValue["session"],
    status: "authenticated",
  };
}

function ProjectsListTestView({ messages, role }: { messages: Record<string, string>; role: Role }) {
  return (
    <AuthContext.Provider value={authValue(role)}>
    <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, fallback, params) => interpolate(messages[key] || fallback || key, params) }}>
    <MemoryRouter initialEntries={["/projects"]}>
      <Routes>
        <Route element={<ProjectsListPage />} path="/projects" />
        {/* Landing target, so a row click can be observed rather than mocked. */}
        <Route element={<p>project detail route</p>} path="/projects/:projectId" />
      </Routes>
    </MemoryRouter>
    </I18nContext.Provider>
    </AuthContext.Provider>
  );
}

function renderProjectsListPage(messages: Record<string, string> = {}, role: Role = "admin") {
  const view = render(<ProjectsListTestView messages={messages} role={role} />);
  return { ...view, rerenderRole: (nextRole: Role) => view.rerender(<ProjectsListTestView messages={messages} role={nextRole} />) };
}

/**
 * Select is a button plus a listbox, not a native control: a value is chosen
 * by opening the menu and clicking an option, and the trigger text is what
 * reports the current value.
 */
describe("ProjectsListPage", () => {
  beforeEach(() => {
    globalThis.localStorage.clear();
    mockedFetchProjectsList.mockReset();
    mockedFetchDataConnections.mockReset();
    mockedFetchWorkspaces.mockReset();
    mockedFetchWorkflows.mockReset();
    mockedFetchModelSelectionOptions.mockReset();
    vi.restoreAllMocks();

    mockedFetchProjectsList.mockResolvedValue({
      projects: [
        {
          id: "project-1",
          name: "Audit Project",
          workspace_id: "ws-1",
          workspace_name: "Test",
          operator_default_workflow_id: "weekly_report",
          updated_at: "2026-05-16T09:00:00Z",
          job_count: 0,
        },
      ],
      total: 1,
    } as never);
    mockedFetchWorkspaces.mockResolvedValue([
      {
        id: "ws-1",
        display_name: "Test",
      },
    ] as never);
    mockedFetchWorkflows.mockResolvedValue([
      {
        workflow_id: "weekly_report",
        display_name: "Weekly report",
        enabled: true,
      },
      {
        workflow_id: "release_report",
        display_name: "Release report",
        enabled: true,
      },
    ] as never);
    mockedFetchDataConnections.mockResolvedValue([
      {
        connection_id: "warehouse_main",
        display_name: "Warehouse main",
        workspace_id: "ws-1",
        source_kind: "postgres",
        environment: "prod",
        enabled: true,
        health_status: "healthy",
        linked_dataset_count: 1,
      },
      {
        connection_id: "analytics_readonly",
        display_name: "Analytics readonly",
        workspace_id: "ws-1",
        source_kind: "postgres",
        environment: "prod",
        enabled: true,
        health_status: "healthy",
        linked_dataset_count: 0,
      },
    ] as never);
    mockedFetchModelSelectionOptions.mockResolvedValue({
      provider_options: [{ provider_id: "openai", label: "OpenAI Direct" }],
      provider_models: {
        openai: [{ model_id: "gpt-5", label: "GPT-5 Verified" }],
      },
    });
  });

  it.each([
    ["admin", true, true],
    ["operator", false, true],
    ["viewer", false, false],
  ] as const)("offers creation only to admin while preserving %s project access", async (role, canCreate, canDelete) => {
    localStorage.setItem("platform:selected-workspace-id", "ws-1");
    if (!canCreate) mockedFetchWorkspaces.mockRejectedValue(new Error("Forbidden"));
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectsListPage({}, role);
    const table = await screen.findByRole("table", { name: "Projects inventory" });
    await within(table).findByText("Audit Project");
    expect(Boolean(screen.queryByRole("button", { name: "New project" }))).toBe(canCreate);
    expect(screen.queryByRole("dialog", { name: "Create project" })).not.toBeInTheDocument();
    if (canCreate) expect(mockedFetchWorkspaces).toHaveBeenCalled();
    else expect(mockedFetchWorkspaces).not.toHaveBeenCalled();
    expect(localStorage.getItem("platform:selected-workspace-id")).toBe("ws-1");
    expect(mockedFetchProjectsList).toHaveBeenLastCalledWith(expect.objectContaining({ workspaceId: "ws-1" }));
    await user.click(within(table).getByRole("button", { name: /^Actions for / }));
    expect(await screen.findByRole("menuitem", { name: "View" })).toBeInTheDocument();
    expect(Boolean(screen.queryByRole("menuitem", { name: "Delete" }))).toBe(canDelete);
  });

  it.each(["operator", "viewer"] as const)("closes and discards an admin creation draft on downgrade to %s", async (role) => {
    const request = vi.spyOn(globalThis, "fetch");
    const view = renderProjectsListPage();
    await screen.findByText("Audit Project");
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    const dialog = await screen.findByRole("dialog", { name: "Create project" });
    await within(dialog).findByText("Test");
    fireEvent.change(within(dialog).getByRole("textbox", { name: "Name" }), { target: { value: "Admin draft" } });
    view.rerenderRole(role);
    expect(screen.queryByRole("dialog", { name: "Create project" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New project" })).not.toBeInTheDocument();
    expect(request).not.toHaveBeenCalled();
    view.rerenderRole("admin");
    expect(screen.queryByRole("dialog", { name: "Create project" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    expect(await screen.findByRole("textbox", { name: "Name" })).toHaveValue("");
  });

  it("ignores the admin workspace response after role loss without clearing persisted project scope", async () => {
    localStorage.setItem("platform:selected-workspace-id", "ws-1");
    let resolveWorkspaces!: (rows: Awaited<ReturnType<typeof fetchWorkspaces>>) => void;
    mockedFetchWorkspaces.mockReturnValue(new Promise((resolve) => { resolveWorkspaces = resolve; }));
    const view = renderProjectsListPage();
    await screen.findByText("Audit Project");
    expect(mockedFetchWorkspaces).toHaveBeenCalledTimes(1);
    view.rerenderRole("operator");
    await act(async () => { resolveWorkspaces([{ id: "foreign-workspace", display_name: "Other" }] as never); });
    expect(await screen.findByText("Audit Project")).toBeInTheDocument();
    expect(localStorage.getItem("platform:selected-workspace-id")).toBe("ws-1");
    expect(mockedFetchProjectsList).toHaveBeenLastCalledWith(expect.objectContaining({ workspaceId: "ws-1" }));
    expect(mockedFetchWorkspaces).toHaveBeenCalledTimes(1);
  });

  it("offers authorized soft deletion for a project with terminal jobs without deleting those jobs", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    let trashed = false;
    mockedFetchProjectsList.mockImplementation(async () => ({
      projects: trashed ? [] : [{ id: "project-1", name: "Audit Project", workspace_id: "ws-1", job_count: 2 }],
      total: trashed ? 0 : 1,
    }) as never);
    const request = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (url !== "/api/projects/project-1" || init?.method !== "DELETE") throw new Error("Unexpected mutation");
      trashed = true;
      return new Response(JSON.stringify({ status: "ok" }), { status: 200, headers: { "content-type": "application/json" } });
    });
    renderProjectsListPage();
    await screen.findByText("Audit Project");
    await user.click(screen.getByRole("button", { name: /^Actions for / }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("alertdialog", { name: /Delete Audit Project\?/ });
    expect(within(dialog).getByText(/restore this from Trash/i)).toBeInTheDocument();
    expect(within(dialog).queryByText(/only offered while a project has no jobs/i)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Delete project" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(await screen.findByText("No projects found")).toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith("/api/projects/project-1", expect.objectContaining({ method: "DELETE" }));
    // The only request is project soft deletion; there is no job-delete request.
  });

  it("localizes the live project count announcement", async () => {
    renderProjectsListPage({ "monitoring.badge.projects": "Projektų: {count}" });
    expect(await screen.findByText("Projektų: 1")).toBeInTheDocument();
    expect(screen.queryByText("1 projects")).not.toBeInTheDocument();
  });

  it.each([0, 2])("hides project mutation controls from a viewer with %s jobs", async (jobCount) => {
    mockedFetchProjectsList.mockResolvedValue({ projects: [{ id: "project-1", name: "Audit Project", workspace_id: "ws-1", job_count: jobCount }], total: 1 } as never);
    // Upstream asserted this through the retired grid's row_actions config; the
    // behaviour is the same, the surface is the published table and row menu.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectsListPage({}, "viewer");

    const table = await screen.findByRole("table", { name: "Projects inventory" });
    expect(screen.queryByRole("button", { name: "New project" })).not.toBeInTheDocument();

    const row = within(table).getAllByRole("row")[1];
    await user.click(within(row).getByRole("button", { name: /^Actions for / }));
    expect(await screen.findByRole("menuitem", { name: "View" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("does not refetch workspaces in a grid bootstrap loop", async () => {
    renderProjectsListPage();

    await waitFor(() => {
      expect(mockedFetchProjectsList).toHaveBeenCalled();
    });
    await waitFor(() => {
      expect(mockedFetchWorkspaces).toHaveBeenCalledTimes(2);
    });

    await new Promise((resolve) => globalThis.setTimeout(resolve, 25));

    expect(mockedFetchWorkspaces).toHaveBeenCalledTimes(2);
    expect(mockedFetchProjectsList.mock.calls.length).toBeLessThanOrEqual(3);
  });

  it("renders the project inventory in a semantic table", async () => {
    renderProjectsListPage();

    const table = await screen.findByRole("table", { name: "Projects inventory" });
    await waitFor(() => {
      expect(within(table).getAllByRole("row").length).toBeGreaterThan(1);
    });

    // The columns the list endpoint feeds, read off the rendered row rather
    // than off a payload handed to a web component.
    const row = within(table).getAllByRole("row")[1];
    // The operator default resolves to the workflow's display name.
    expect(within(row).getByText("Weekly report")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Name" })).toHaveAttribute("aria-sort", "none");
    // A single workspace is resolved as the scope, so repeating it on every row
    // carries nothing.
    expect(screen.queryByRole("columnheader", { name: "Workspace" })).not.toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Jobs" })).toHaveAttribute("data-numeric", "true");
  });

  it("never offers a workspace column, because a session is always scoped to one", async () => {
    // The column existed only for the "all workspaces" scope. That scope is
    // gone: the shell always points the session at a concrete workspace, so a
    // per-row workspace name would repeat the same value on every row.
    mockedFetchWorkspaces.mockResolvedValue([
      { id: "ws-1", display_name: "Test" },
      { id: "ws-2", display_name: "Second workspace" },
    ] as never);
    renderProjectsListPage();

    await screen.findByRole("table", { name: "Projects inventory" });
    expect(
      screen.queryByRole("columnheader", { name: "Workspace" }),
    ).not.toBeInTheDocument();
  });

  it("marks the rows as their own click target", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectsListPage();

    const table = await screen.findByRole("table", { name: "Projects inventory" });
    const row = within(table).getAllByRole("row")[1];
    // The pointer cursor hangs off this attribute, and the row really does
    // navigate, so the affordance is not a decoration.
    expect(row).toHaveAttribute("data-clickable");

    await user.click(within(row).getByText("Audit Project"));
    expect(await screen.findByText("project detail route")).toBeInTheDocument();
  });

  it("sends the has-jobs filter to the endpoint", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectsListPage();

    await screen.findByRole("table", { name: "Projects inventory" });
    await user.click(screen.getByRole("button", { name: /add filter/i }));
    await user.click(await screen.findByRole("menuitem", { name: "Jobs" }));
    await user.click(await screen.findByRole("menuitem", { name: "With jobs" }));

    await waitFor(() => {
      expect(mockedFetchProjectsList).toHaveBeenCalledWith(
        expect.objectContaining({ hasJobs: "yes", page: 1 }),
      );
    });
  });

  it("asks the endpoint to sort instead of reordering the loaded page", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderProjectsListPage();

    await screen.findByRole("table", { name: "Projects inventory" });
    await user.click(screen.getByRole("button", { name: "Jobs" }));

    // Sorting 25 loaded rows would leave every other page in name order.
    await waitFor(() => {
      expect(mockedFetchProjectsList).toHaveBeenCalledWith(
        expect.objectContaining({ sort: "job_count", direction: "asc" }),
      );
    });

    await user.click(screen.getByRole("button", { name: "Jobs" }));
    await waitFor(() => {
      expect(mockedFetchProjectsList).toHaveBeenCalledWith(
        expect.objectContaining({ sort: "job_count", direction: "desc" }),
      );
    });
  });

  it("confirms project deletion in a dialog instead of a native confirm", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const confirmSpy = vi.spyOn(globalThis, "confirm");
    renderProjectsListPage();

    const table = await screen.findByRole("table", { name: "Projects inventory" });
    await waitFor(() => {
      expect(within(table).getAllByRole("row").length).toBeGreaterThan(1);
    });

    await user.click(screen.getByRole("button", { name: /^Actions for / }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    // A destructive confirmation is an alertdialog, not a plain dialog, and it
    // carries an accessible name - the hand-rolled modals carried neither.
    const dialog = await screen.findByRole("alertdialog", { name: /Delete Audit Project\?/ });
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Delete project" })).toBeInTheDocument();
    expect(confirmSpy).not.toHaveBeenCalled();

    // UI-001: deleting sets deleted_at, so the project goes to Trash and can be
    // restored. Telling the operator it cannot be undone was untrue, and it
    // reserved for a reversible action the words that belong to a purge.
    expect(within(dialog).getByText(/restore this from Trash/i)).toBeInTheDocument();
    expect(within(dialog).queryByText(/cannot be undone/i)).not.toBeInTheDocument();
  });

  it("resets the create project name when the modal is cancelled", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });

    renderProjectsListPage();

    await waitFor(() => {
      expect(mockedFetchProjectsList).toHaveBeenCalled();
      expect(mockedFetchWorkspaces).toHaveBeenCalled();
    });

    await user.click(await screen.findByTestId("create-project"));
    await waitFor(() => {
      expect(mockedFetchWorkflows).toHaveBeenCalled();
    });
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Draft Project" },
    });
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    });

    await user.click(screen.getByTestId("create-project"));

    expect(screen.getByLabelText("Name")).toHaveValue("");
    expect(mockedFetchDataConnections).not.toHaveBeenCalled();
    expect(mockedFetchModelSelectionOptions).not.toHaveBeenCalled();
  });
});
