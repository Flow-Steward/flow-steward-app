import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { RegistryWorkspacesPage } from "@/pages/registry/workspaces";
import {
  createWorkspace,
  executeWorkspaceDeletePlan,
  fetchWorkspaceDeletePlan,
  fetchWorkspaces,
} from "@/pages/registry/workspaces/api";
import {
  WORKSPACE_STORAGE_KEY,
  WORKSPACES_CHANGED_EVENT,
} from "@/lib/utils/workspace-selection";
import { useHasCapability } from "@/lib/auth/capabilities";

vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: vi.fn() }));

vi.mock("@/pages/registry/workspaces/api", () => ({
  archiveWorkspace: vi.fn(),
  createWorkspace: vi.fn(),
  executeWorkspaceDeletePlan: vi.fn(),
  fetchWorkspaceDeletePlan: vi.fn(),
  fetchWorkspaces: vi.fn(),
}));

vi.mock("@/lib/time/user-timezone", () => ({
  formatUserDateTime: vi.fn((value: unknown) => (value ? "May 20, 2026, 6:00 AM" : "—")),
  useUserTimezone: vi.fn(() => "UTC"),
}));

const mockedCreateWorkspace = vi.mocked(createWorkspace);
const mockedExecuteWorkspaceDeletePlan = vi.mocked(executeWorkspaceDeletePlan);
const mockedFetchWorkspaceDeletePlan = vi.mocked(fetchWorkspaceDeletePlan);
const mockedFetchWorkspaces = vi.mocked(fetchWorkspaces);
const mockedUseHasCapability = vi.mocked(useHasCapability);

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/registry/workspaces"]}>
        <Routes>
          <Route element={<RegistryWorkspacesPage />} path="/registry/workspaces" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RegistryWorkspacesPage", () => {
  beforeEach(() => {
    mockedUseHasCapability.mockReturnValue(true);
    globalThis.localStorage.clear();
    mockedCreateWorkspace.mockReset();
    mockedExecuteWorkspaceDeletePlan.mockReset();
    mockedFetchWorkspaceDeletePlan.mockReset();
    mockedFetchWorkspaces.mockReset();

    mockedFetchWorkspaces.mockResolvedValue([
      {
        id: "ws-1",
        workspace_key: "default",
        display_name: "Default workspace",
        description: "",
        projects_count: 0,
        jobs_count: 0,
        status: "active",
        metadata_jsonb: {},
        deleted_at: "",
        updated_at: "2026-05-20T06:00:00Z",
      },
    ] as never);
    mockedCreateWorkspace.mockResolvedValue({
      id: "ws-2",
      workspace_key: "audit-workspace",
      display_name: "Audit Workspace",
      description: "Audit description",
      projects_count: 0,
      jobs_count: 0,
      status: "active",
      metadata_jsonb: {},
      deleted_at: "",
      updated_at: "2026-05-20T06:00:00Z",
    } as never);
    mockedFetchWorkspaceDeletePlan.mockResolvedValue({
      status: "planned",
      action_type: "trash",
      workspace: {
        workspace_id: "ws-1",
        label: "Default workspace",
        status: "active",
      },
      projects: [
        {
          project_id: "project-1",
          label: "Project One",
          status: "active",
          can_reassign: false,
          current_workspace_id: "ws-1",
          current_workspace_label: "Default workspace",
        },
      ],
      datasets: [
        { dataset_id: "dataset-1", label: "Workspace Dataset", status: "active" },
      ],
      data_connections: [
        {
          connection_id: "connection-1",
          label: "Workspace Connection",
          status: "active",
          linked_dataset_count: 1,
          allowed_actions: ["block", "reassign", "delete"],
        },
      ],
      target_workspaces: [],
      warnings: [],
      validation_errors: [],
      summary: {
        active_project_count: 1,
        trashed_project_count: 0,
      },
    } as never);
  });

  it("keeps an operator workspace catalog free of mutation controls", async () => {
    mockedUseHasCapability.mockReturnValue(false);
    renderPage();

    expect(await screen.findByText("Read only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New workspace" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Move to trash" })).not.toBeInTheDocument();
  });

  it("names the page once, in its header, with no title on the table card", async () => {
    renderPage();

    expect(await screen.findByRole("table", { name: "Workspace registry" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Workspace registry" })).not.toBeInTheDocument();
  });

  it("marks the required workspace name while native validation keeps Description optional", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "New workspace" }));
    const name = screen.getByRole("textbox", { name: "Workspace name" }) as HTMLInputElement;
    const description = screen.getByRole("textbox", { name: "Description" }) as HTMLTextAreaElement;
    expect(name).toBeRequired();
    expect(description).not.toBeRequired();
    expect(name.validity.valueMissing).toBe(true);
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(mockedCreateWorkspace).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "New workspace" })).toBeVisible();
    const nameLabel = name.labels?.[0];
    expect(nameLabel).toBeDefined();
    expect(within(nameLabel as HTMLLabelElement).getByText("*")).toBeVisible();
    expect(within(description.labels![0]).queryByText("*")).not.toBeInTheDocument();
    await user.type(name, "Name without description");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(mockedCreateWorkspace).toHaveBeenCalled());
    expect(mockedCreateWorkspace.mock.calls[0]?.[0]).toEqual({
      workspace_key: "", display_name: "Name without description", description: "",
    });
    expect(await screen.findByText("Workspace created.")).toBeVisible();
  });

  it("submits workspace creation without the unsupported status field", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("table", { name: "Workspace registry" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New workspace" }));
    await user.type(screen.getByRole("textbox", { name: "Workspace name" }), "Audit Workspace");
    await user.type(screen.getByRole("textbox", { name: "Description" }), "Audit description");
    await user.click(screen.getByRole("button", { name: /^Create$/ }));

    await waitFor(() => {
      expect(mockedCreateWorkspace).toHaveBeenCalled();
    });
    expect(mockedCreateWorkspace.mock.calls[0]?.[0]).toEqual({
      workspace_key: "",
      display_name: "Audit Workspace",
      description: "Audit description",
    });
    expect(mockedCreateWorkspace.mock.calls[0]?.[0]).not.toHaveProperty("status");
  });

  it("maps a duplicate workspace conflict to the workspace-name field", async () => {
    const user = userEvent.setup();
    mockedCreateWorkspace.mockRejectedValue(
      new Error("workspace_key 'release' already exists"),
    );
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New workspace" }));
    const nameInput = screen.getByRole("textbox", { name: "Workspace name" });
    await user.type(nameInput, "Release");
    await user.click(screen.getByRole("button", { name: /^Create$/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "workspace_key 'release' already exists",
    );
    expect(nameInput).toHaveAttribute("aria-invalid", "true");
    expect(nameInput).toHaveAttribute("aria-describedby", "workspace-name-error");
  });

  it("renders friendly updated timestamps instead of raw ISO strings", async () => {
    renderPage();

    expect(await screen.findByText("May 20, 2026, 6:00 AM")).toBeInTheDocument();
    expect(screen.queryByText("2026-05-20T06:00:00Z")).not.toBeInTheDocument();
  });

  it("lists only the workspaces that exist: no status filter, no status column", async () => {
    // The owner's decision: a trashed workspace lives in Trash, and is restored or
    // deleted there. The registry asks for the search alone, and the endpoint's
    // default is the active workspaces.
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("table", { name: "Workspace registry" })).toBeInTheDocument();
    await waitFor(() => {
      expect(mockedFetchWorkspaces).toHaveBeenLastCalledWith({ search: "" });
    });
    expect(screen.queryByRole("button", { name: "Add filter" })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Status" })).not.toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Search workspaces" }), "rel");
    await waitFor(() => {
      expect(mockedFetchWorkspaces).toHaveBeenLastCalledWith({ search: "rel" });
    });
  });

  it("opens a workspace delete plan before moving a workspace to trash", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("table", { name: "Workspace registry" })).toBeInTheDocument();
    expect(await screen.findByText("Default workspace")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Actions for Default workspace" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to trash" }));

    await waitFor(() => {
      expect(mockedFetchWorkspaceDeletePlan).toHaveBeenCalledWith("ws-1");
    });
    expect(await screen.findByRole("heading", { name: "Workspace delete plan" })).toBeInTheDocument();
    expect(screen.getByText("Project One")).toBeInTheDocument();
    expect(screen.getByText("Workspace Dataset")).toBeInTheDocument();
    expect(screen.getByText("Workspace Connection")).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Data connection action for Workspace Connection" }),
    ).toHaveTextContent("Choose action");
    expect(screen.getByRole("button", { name: "Move workspace to trash" })).toBeDisabled();
  });

  it("switches the active workspace selection off a deleted workspace and notifies the shell", async () => {
    const user = userEvent.setup();
    globalThis.localStorage.setItem(WORKSPACE_STORAGE_KEY, "ws-audit");
    const auditWorkspace = {
      id: "ws-audit",
      workspace_key: "audit",
      display_name: "Audit Workspace",
      description: "",
      projects_count: 1,
      jobs_count: 0,
      status: "active",
      metadata_jsonb: {},
      deleted_at: "",
      updated_at: "2026-06-30T06:00:00Z",
    };
    const defaultWorkspace = {
      id: "ws-default",
      workspace_key: "default",
      display_name: "Default workspace",
      description: "",
      projects_count: 0,
      jobs_count: 0,
      status: "active",
      metadata_jsonb: {},
      deleted_at: "",
      updated_at: "2026-06-30T06:00:00Z",
    };
    // The registry page list is filtered and hides the Default workspace; only the
    // unfiltered fetch (no params) sees it. The fallback must still pick Default.
    mockedFetchWorkspaces.mockImplementation((params?: { search?: string; status?: string }) =>
      Promise.resolve(
        (params === undefined ? [auditWorkspace, defaultWorkspace] : [auditWorkspace]) as never,
      ),
    );
    mockedFetchWorkspaceDeletePlan.mockResolvedValue({
      status: "planned",
      action_type: "trash",
      workspace: { workspace_id: "ws-audit", label: "Audit Workspace", status: "active" },
      projects: [
        {
          project_id: "project-1",
          label: "Project One",
          status: "active",
          can_reassign: true,
          current_workspace_id: "ws-audit",
          current_workspace_label: "Audit Workspace",
        },
      ],
      target_workspaces: [{ workspace_id: "ws-default", label: "Default workspace" }],
      warnings: [],
      validation_errors: [],
      summary: { active_project_count: 1, trashed_project_count: 0 },
    } as never);
    mockedExecuteWorkspaceDeletePlan.mockResolvedValue({ status: "ok" } as never);

    const workspacesChangedSpy = vi.fn();
    globalThis.addEventListener(WORKSPACES_CHANGED_EVENT, workspacesChangedSpy);

    try {
      renderPage();

      expect(await screen.findByText("Audit Workspace")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Actions for Audit Workspace" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to trash" }));

      await waitFor(() => {
        expect(mockedFetchWorkspaceDeletePlan).toHaveBeenCalledWith("ws-audit");
      });
      expect(await screen.findByRole("heading", { name: "Workspace delete plan" })).toBeInTheDocument();

      await user.click(screen.getByRole("combobox", { name: "Delete action" }));
      await user.click(await screen.findByRole("option", { name: "Keep project by moving" }));
      await user.click(screen.getByRole("combobox", { name: "Target workspace" }));
      await user.click(await screen.findByRole("option", { name: "Default workspace" }));
      await user.click(screen.getByRole("button", { name: "Move workspace to trash" }));

      await waitFor(() => {
        expect(mockedExecuteWorkspaceDeletePlan).toHaveBeenCalledWith(
          "ws-audit",
          expect.arrayContaining([
            expect.objectContaining({
              project_id: "project-1",
              action: "reassign",
              target_workspace_id: "ws-default",
            }),
          ]),
          [],
        );
      });
      // Selection moves to the Default workspace, and the shell is told to reload.
      await waitFor(() => {
        expect(globalThis.localStorage.getItem(WORKSPACE_STORAGE_KEY)).toBe("ws-default");
      });
      expect(workspacesChangedSpy).toHaveBeenCalled();
    } finally {
      globalThis.removeEventListener(WORKSPACES_CHANGED_EVENT, workspacesChangedSpy);
    }
  });
  it("announces plan warnings and the blocking validation from the alert's own tone", async () => {
    const user = userEvent.setup();
    mockedFetchWorkspaceDeletePlan.mockResolvedValue({
      status: "planned",
      action_type: "trash",
      workspace: { workspace_id: "ws-1", label: "Default workspace", status: "active" },
      projects: [
        {
          project_id: "project-1",
          label: "Project One",
          status: "active",
          can_reassign: false,
          current_workspace_id: "ws-1",
          current_workspace_label: "Default workspace",
        },
        {
          project_id: "project-2",
          label: "Retired Project",
          status: "trashed",
          can_reassign: false,
          current_workspace_id: "ws-1",
          current_workspace_label: "Default workspace",
        },
      ],
      datasets: [],
      data_connections: [
        {
          connection_id: "connection-1",
          label: "Workspace Connection",
          status: "active",
          linked_dataset_count: 1,
          allowed_actions: ["block", "reassign", "delete"],
        },
      ],
      target_workspaces: [],
      warnings: ["This workspace still holds scheduled jobs."],
      validation_errors: [],
      summary: { active_project_count: 1, trashed_project_count: 1 },
    } as never);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Actions for Default workspace" }));
    await user.click(await screen.findByRole("menuitem", { name: "Move to trash" }));
    expect(await screen.findByRole("heading", { name: "Workspace delete plan" })).toBeInTheDocument();

    // Neither role is written by hand: `warning` resolves to status and `danger`
    // to alert, from DsAlert's own tone mapping.
    expect(screen.getByRole("status")).toHaveAccessibleDescription(
      "This workspace still holds scheduled jobs.",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Choose an explicit action for every live Data Connection.",
    );
    expect(screen.getByRole("button", { name: "Move workspace to trash" })).toBeDisabled();

    // The already-trashed children are collapsed, so they cannot push the
    // decisions and the confirm button off the screen.
    expect(screen.getByText("Already trashed (1)")).toBeInTheDocument();
    // The row is rendered — the count above it would be a lie otherwise — and it
    // sits inside the collapsed disclosure rather than in the decision list.
    // jsdom does not lay `<details>` out, so its collapsed state is a browser
    // measurement, not a unit-test one.
    expect(screen.getByText("Retired Project")).toBeInTheDocument();
  });
  it("blames the search when a search matches nothing", async () => {
    const user = userEvent.setup();
    // The server filters, so an empty result under a search is indistinguishable
    // from an empty registry unless the copy takes the search into account.
    mockedFetchWorkspaces.mockImplementation((params?: { search?: string }) =>
      Promise.resolve((params?.search ? [] : [
        {
          id: "ws-release",
          workspace_key: "release",
          display_name: "Release workspace",
          description: "",
          projects_count: 0,
          jobs_count: 0,
          status: "active",
          metadata_jsonb: {},
          deleted_at: "",
          updated_at: "2026-06-30T06:00:00Z",
        },
      ]) as never),
    );
    renderPage();

    expect(await screen.findByText("Release workspace")).toBeInTheDocument();

    await user.type(screen.getByRole("searchbox", { name: "Search workspaces" }), "zzz");

    expect(await screen.findByText("No workspaces match this search")).toBeInTheDocument();
    expect(screen.getByText("Change or clear the search.")).toBeInTheDocument();
  });
});
