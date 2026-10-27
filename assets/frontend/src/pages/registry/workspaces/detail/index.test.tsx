import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import { fetchDataConnections, fetchDatasets } from "@/pages/registry/datasets/api";
import { fetchLifecyclePlan } from "@/pages/trash/api";
import { RegistryWorkspacesDetailPage } from "@/pages/registry/workspaces/detail";
import {
  executeWorkspaceDeletePlan,
  fetchWorkspace,
  fetchWorkspaceDeletePlan,
  fetchWorkspaceProjects,
  updateWorkspace,
} from "@/pages/registry/workspaces/api";

vi.mock("@/pages/registry/datasets/api", () => ({
  fetchDataConnections: vi.fn(),
  fetchDatasets: vi.fn(),
}));

vi.mock("@/pages/registry/workspaces/api", () => ({
  archiveWorkspace: vi.fn(),
  executeWorkspaceDeletePlan: vi.fn(),
  fetchWorkspace: vi.fn(),
  fetchWorkspaceDeletePlan: vi.fn(),
  fetchWorkspaceProjects: vi.fn(),
  updateWorkspace: vi.fn(),
}));

vi.mock("@/pages/trash/api", () => ({
  executeLifecyclePlan: vi.fn(),
  fetchLifecyclePlan: vi.fn(),
}));

const mockedFetchDataConnections = vi.mocked(fetchDataConnections);
const mockedFetchDatasets = vi.mocked(fetchDatasets);
const mockedExecuteWorkspaceDeletePlan = vi.mocked(executeWorkspaceDeletePlan);
const mockedFetchWorkspace = vi.mocked(fetchWorkspace);
const mockedFetchWorkspaceDeletePlan = vi.mocked(fetchWorkspaceDeletePlan);
const mockedFetchWorkspaceProjects = vi.mocked(fetchWorkspaceProjects);
const mockedUpdateWorkspace = vi.mocked(updateWorkspace);
const mockedFetchLifecyclePlan = vi.mocked(fetchLifecyclePlan);

/** The active tab lives in the URL, so the test has to be able to read it. */
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.pathname + location.search}</span>;
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/registry/workspaces/ws-1"]}>
        <LocationProbe />
        <Routes>
          <Route element={<RegistryWorkspacesDetailPage />} path="/registry/workspaces/:workspaceId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RegistryWorkspacesDetailPage", () => {
  beforeEach(() => {
    mockedFetchDataConnections.mockReset();
    mockedFetchDatasets.mockReset();
    mockedExecuteWorkspaceDeletePlan.mockReset();
    mockedFetchWorkspace.mockReset();
    mockedFetchWorkspaceDeletePlan.mockReset();
    mockedFetchWorkspaceProjects.mockReset();
    mockedUpdateWorkspace.mockReset();
    mockedFetchLifecyclePlan.mockReset();

    mockedFetchWorkspace.mockResolvedValue({
      id: "ws-1",
      workspace_key: "audit-workspace",
      display_name: "Audit Workspace",
      description: "Audit description",
      projects_count: 1,
      jobs_count: 0,
      status: "active",
      metadata_jsonb: {},
      deleted_at: "",
      updated_at: "2026-05-20T06:00:00Z",
    } as never);
    mockedFetchWorkspaceProjects.mockResolvedValue([] as never);
    mockedFetchDataConnections.mockResolvedValue([] as never);
    mockedFetchDatasets.mockResolvedValue([] as never);
    mockedFetchWorkspaceDeletePlan.mockResolvedValue({
      status: "planned",
      action_type: "trash",
      workspace: {
        workspace_id: "ws-1",
        label: "Audit Workspace",
        status: "active",
      },
      projects: [],
      target_workspaces: [],
      warnings: [],
      validation_errors: [],
      summary: {
        active_project_count: 0,
        trashed_project_count: 0,
      },
    } as never);
    mockedExecuteWorkspaceDeletePlan.mockResolvedValue({ status: "trashed" } as never);
    mockedUpdateWorkspace.mockResolvedValue({
      id: "ws-1",
      workspace_key: "audit-workspace",
      display_name: "Audit Workspace",
      description: "Audit description",
      projects_count: 1,
      jobs_count: 0,
      status: "active",
      metadata_jsonb: {},
      deleted_at: "",
      updated_at: "2026-05-20T06:00:00Z",
    } as never);
  });

  it("submits workspace updates without the unsupported status field", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("heading", { name: "Workspace settings" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(mockedUpdateWorkspace).toHaveBeenCalledWith("ws-1", {
        display_name: "Audit Workspace",
        description: "Audit description",
      });
    });
    expect(mockedUpdateWorkspace.mock.calls[0]?.[1]).not.toHaveProperty("status");
    expect(screen.getByDisplayValue("audit-workspace")).toHaveAttribute("readonly");
  });
  it("exposes the workspace views as a real tablist and puts the active one in the URL", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("tab", { name: /^Projects/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getAllByRole("tab")).toHaveLength(3);
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    // The default view writes no query parameter at all.
    expect(screen.getByTestId("location")).toHaveTextContent("/registry/workspaces/ws-1");
    expect(screen.getByTestId("location")).not.toHaveTextContent("tab=");

    await user.click(screen.getByRole("tab", { name: /^Data connections/ }));

    expect(screen.getByRole("tab", { name: /^Data connections/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: /^Projects/ })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    expect(screen.getAllByRole("tabpanel")).toHaveLength(1);
    expect(screen.getByTestId("location")).toHaveTextContent("?tab=data-connections");

    await user.click(screen.getByRole("tab", { name: /^Projects/ }));
    expect(screen.getByTestId("location")).not.toHaveTextContent("tab=");
  });

  it("opens the lifecycle plan as a real dialog and blocks a plan that cannot run", async () => {
    const user = userEvent.setup();
    mockedFetchWorkspace.mockResolvedValue({
      id: "ws-1",
      workspace_key: "audit-workspace",
      display_name: "Audit Workspace",
      description: "Audit description",
      projects_count: 0,
      jobs_count: 0,
      status: "trashed",
      metadata_jsonb: {},
      deleted_at: "2026-06-01T06:00:00Z",
      updated_at: "2026-05-20T06:00:00Z",
    } as never);
    mockedFetchLifecyclePlan.mockResolvedValue({
      action_type: "restore",
      plan_version: "v1",
      root_entity: { entity_type: "workspace", entity_id: "ws-1", label: "Audit Workspace" },
      tree: {
        node_id: "workspace:ws-1",
        entity_type: "workspace",
        entity_id: "ws-1",
        label: "Audit Workspace",
        selectable: true,
        selected: true,
        status: "trashed",
        children: [],
      },
      warnings: [],
      validation_errors: ["A parent account must be restored first."],
    } as never);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Restore" }));

    await screen.findByRole("dialog", { name: "Restore plan" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Close Restore plan" })).toHaveFocus(),
    );
    expect(screen.getByRole("button", { name: "Restore selected" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "A parent account must be restored first.",
    );

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
