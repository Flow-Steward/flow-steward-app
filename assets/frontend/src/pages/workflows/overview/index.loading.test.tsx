import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { useCanReadWorkflowRegistry, useIsAdmin } from "@/lib/auth/use-is-admin";
import { WorkflowsOverviewPage } from "@/pages/workflows/overview";
import { WorkflowsProjectsPage } from "@/pages/workflows/projects";
import { fetchWorkflowDag, fetchWorkflowDetail, fetchWorkflowExportPolicy } from "@/pages/workflows/shared/api";

vi.mock("@/lib/auth/use-is-admin", () => ({
  useIsAdmin: vi.fn(),
  useCanReadWorkflowRegistry: vi.fn(),
}));

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflowDag: vi.fn(async () => ({ workflow_id: "wf", nodes: [{ id: "a" }, { id: "b" }], edges: [{ id: "e" }] })),
  fetchWorkflowDetail: vi.fn(),
  fetchWorkflowExportPolicy: vi.fn(async () => ({ workflow_id: "wf" })),
  fetchWorkflowStats: vi.fn(async () => ({ workflow_id: "wf", total_runs: 0 })),
  fetchWorkflowValidation: vi.fn(async () => ({ workflow_id: "wf", validation: { valid: true, errors: [], warnings: [] } })),
}));

vi.mock("@/pages/workflows/shared/project-context", () => ({
  fetchAllProjectsForWorkflowContext: vi.fn(async () => []),
  findWorkflowLinkedProjectId: vi.fn(async () => ""),
}));

vi.mock("@/pages/projects/list/api", () => ({ fetchProjectsList: vi.fn(async () => ({ projects: [], total_pages: 1 })) }));

const mockedDetail = vi.mocked(fetchWorkflowDetail);

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<WorkflowsOverviewPage />} path="/registry/workflows/:workflowId" />
          <Route element={<WorkflowsProjectsPage />} path="/registry/workflows/:workflowId/projects" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe.each([
  ["overview", "/registry/workflows/wf"],
  ["projects", "/registry/workflows/wf/projects"],
])("the workflow %s page before its definition arrives", (_page, path) => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useIsAdmin).mockReturnValue(true);
    vi.mocked(useCanReadWorkflowRegistry).mockReturnValue(true);
  });

  it("waits, and never prints zero placeholders for the header", async () => {
    let resolve: (value: unknown) => void = () => undefined;
    mockedDetail.mockReturnValue(new Promise((done) => { resolve = done; }) as never);
    renderAt(path);

    // B26: "disabled · 0 steps · 0 nodes / 0 edges · Validation 0 errors" flashed here.
    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(screen.queryByText(/0 steps/)).not.toBeInTheDocument();
    expect(screen.queryByText(/0 nodes/)).not.toBeInTheDocument();
    expect(screen.queryByText("disabled")).not.toBeInTheDocument();

    resolve({ workflow_id: "wf", display_name: "Order flow", enabled: true, steps: [{ step_id: "a" }, { step_id: "b" }] });

    expect(await screen.findByText("Order flow")).toBeInTheDocument();
    expect(screen.getByText("2 steps")).toBeInTheDocument();
    expect(vi.mocked(fetchWorkflowDag)).not.toHaveBeenCalled();
    expect(screen.queryByText(/nodes.*edges/)).not.toBeInTheDocument();
  });

  it("uses the current detail portability without a second policy read or graph initialization", async () => {
    mockedDetail.mockResolvedValue({ workflow_id: "wf", display_name: "Protected order flow", enabled: true, steps: [{ step_id: "a" }], portability: { extension_owned: true, protected: true, structure_change_allowed: false, reference_export_allowed: true } } as never);
    renderAt(path);
    expect(await screen.findByText("Protected order flow")).toBeInTheDocument();
    expect(vi.mocked(fetchWorkflowExportPolicy)).not.toHaveBeenCalled();
    expect(vi.mocked(fetchWorkflowDag)).not.toHaveBeenCalled();
    expect(screen.getByText("Structure read-only (extension)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable workflow" })).not.toBeInTheDocument();
  });
});
