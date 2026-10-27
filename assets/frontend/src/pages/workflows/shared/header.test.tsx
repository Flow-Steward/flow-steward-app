import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import type * as ProjectContext from "@/pages/workflows/shared/project-context";
import type * as WorkflowApi from "@/pages/workflows/shared/api";
import type { WorkflowExportPolicy } from "@/pages/workflows/shared/api";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { fetchWorkflowExportPolicy } from "@/pages/workflows/shared/api";
import { findWorkflowLinkedProjectId } from "@/pages/workflows/shared/project-context";
import { WorkflowSummaryHeader } from "@/pages/workflows/shared/header";

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflowExportPolicy: vi.fn(),
}));
vi.mock("@/pages/workflows/shared/project-context", () => ({
  findWorkflowLinkedProjectId: vi.fn(),
}));

const mockedExportPolicy = vi.mocked(fetchWorkflowExportPolicy);
const mockedLinkedProject = vi.mocked(findWorkflowLinkedProjectId);

beforeEach(() => {
  vi.clearAllMocks();
  // Permit clone/delete/export at the policy level so gating is driven by canAuthor.
  mockedExportPolicy.mockResolvedValue({
    clone_allowed: true,
    delete_allowed: true,
    full_export_allowed: true,
    reference_export_allowed: true,
    structure_change_allowed: true,
  } as unknown as Awaited<ReturnType<typeof fetchWorkflowExportPolicy>>);
  mockedLinkedProject.mockResolvedValue("proj-1");
});

function renderHeader(
  canAuthor: boolean,
  onCloneWorkflow: (projectId: string) => void = () => {},
  options: { nodeCount?: number; edgeCount?: number; projectId?: string; activePage?: "overview" | "builder"; portability?: WorkflowExportPolicy; onDeleteWorkflow?: () => void; onWorkflowTestRun?: () => void; onWorkflowSettings?: () => void } = {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = (currentOptions: typeof options) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WorkflowSummaryHeader
          activePage="detail"
          canAuthor={canAuthor}
          description="d"
          edgeCount={0}
          enabled
          errorCount={0}
          isValid
          nodeCount={0}
          onCloneWorkflow={onCloneWorkflow}
          onDeleteWorkflow={() => {}}
          onToggleEnabled={() => {}}
          onWorkflowSettings={() => {}}
          stepCount={1}
          title="WF"
          warningCount={0}
          workflowId="wf-1"
          {...currentOptions}
        />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(content(options));
  return { ...view, queryClient: client, rerenderHeader: (next: typeof options) => view.rerender(content(next)) };
}

describe("WorkflowSummaryHeader authoring RBAC", () => {
  it("renders authoring controls for admins (canAuthor)", async () => {
    renderHeader(true);
    // The secondary commands live in the published action menu rather than as a
    // row of eight buttons, so the menu has to be opened to see them. The enable
    // toggle and the builder stay visible: one is the only command on this
    // surface, the other the destination the screen exists to reach.
    expect(await screen.findByRole("button", { name: "Disable workflow" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open builder/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
    const menu = await screen.findByRole("menu", { name: "Workflow actions" });
    expect(within(menu).getByRole("menuitem", { name: /clone workflow/i })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: /build with ai/i })).not.toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: /workflow settings/i })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: /test run/i })).toHaveAttribute("href", expect.stringContaining("drawer=workflow-test-run"));
    // Destructive, and carried as danger text inside the menu rather than as a
    // red surface in the header — the dropdown contract is explicit about that.
    const remove = within(menu).getByRole("menuitem", { name: /delete workflow/i });
    expect(remove).toHaveClass("danger");
  });

  it("passes the resolved project context into clone", async () => {
    const onCloneWorkflow = vi.fn();
    renderHeader(true, onCloneWorkflow);

    await waitFor(() => expect(mockedLinkedProject).toHaveBeenCalledWith("wf-1", { presentation: "background" }));
    fireEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
    fireEvent.click(
      within(await screen.findByRole("menu", { name: "Workflow actions" })).getByRole(
        "menuitem",
        { name: /clone workflow/i },
      ),
    );

    expect(onCloneWorkflow).toHaveBeenCalledWith("proj-1");
  });

  it("hides authoring controls for non-admins", async () => {
    renderHeader(false);
    await waitFor(() => expect(mockedExportPolicy).toHaveBeenCalled());
    // A non-admin still gets the menu for the reads it is allowed (export), so
    // open it: an authoring command must not be inside it either.
    fireEvent.click(await screen.findByRole("button", { name: "Workflow actions" }));
    await screen.findByRole("menu", { name: "Workflow actions" });
    expect(screen.getByRole("menuitem", { name: "Export YAML" })).toBeInTheDocument();
    // Exporting with the child workflows it calls is a read too.
    expect(
      screen.getByRole("menuitem", { name: "Export YAML with child workflows" }),
    ).toHaveAttribute("href", expect.stringContaining("include_children=true"));
    expect(screen.queryByRole("button", { name: /build with ai/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/build with ai/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /clone workflow/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /delete workflow/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /workflow settings/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /test run/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
    // No authoring affordance leaks via a link fallback either.
    expect(screen.queryByText(/workflow settings/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/build with ai/i)).not.toBeInTheDocument();
  });
});

describe("WorkflowSummaryHeader for a protected extension workflow", () => {
  it("keeps licensed export, clone and lifecycle controls but hides source delete and editing", async () => {
    mockedExportPolicy.mockResolvedValue({
      clone_allowed: true,
      extension_owned: true,
      full_export_allowed: true,
      protected: false,
      reference_export_allowed: true,
      structure_change_allowed: false,
      step_test_run_allowed: true,
    } as unknown as Awaited<ReturnType<typeof fetchWorkflowExportPolicy>>);
    renderHeader(true);

    expect(await screen.findByRole("link", { name: "View workflow (read-only)" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disable workflow" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
    const menu = await screen.findByRole("menu", { name: "Workflow actions" });
    expect(within(menu).getByRole("menuitem", { name: /clone workflow/i })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Export YAML" })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: /delete workflow/i })).not.toBeInTheDocument();
  });

  it("says the structure is read-only, not hidden, since the builder shows it", async () => {
    mockedExportPolicy.mockResolvedValue({
      clone_allowed: false,
      delete_allowed: false,
      extension_owned: true,
      full_export_allowed: false,
      protected: true,
      reference_export_allowed: true,
      structure_change_allowed: false,
    } as unknown as Awaited<ReturnType<typeof fetchWorkflowExportPolicy>>);
    renderHeader(true);

    expect(await screen.findByText("Structure read-only (extension)")).toBeInTheDocument();
    expect(screen.queryByText(/hidden/i)).not.toBeInTheDocument();
  });

  function protectedPolicy() {
    mockedExportPolicy.mockResolvedValue({
      clone_allowed: false,
      extension_owned: true,
      full_export_allowed: false,
      protected: true,
      reference_export_allowed: true,
      structure_change_allowed: false,
    } as unknown as Awaited<ReturnType<typeof fetchWorkflowExportPolicy>>);
  }

  it("offers View workflow, opening the builder read-only, in place of Open builder", async () => {
    protectedPolicy();
    renderHeader(true);

    // B15: the header had only the action menu, so the one way into the read-only
    // view was a quick link below the fold of the overview.
    const view = await screen.findByRole("link", { name: "View workflow (read-only)" });
    await waitFor(() => expect(view).toHaveAttribute("href", "/registry/workflows/wf-1/builder?project_id=proj-1"));
    // The header slot and style of an ordinary workflow's Open builder.
    expect(view).toHaveClass("fs-button");
    expect(view).not.toHaveClass("primary");
    expect(view).toHaveAttribute("data-workflow-header-action", "view");
    expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
  });

  it("does not offer View workflow to a non-admin, who cannot open the builder", async () => {
    protectedPolicy();
    renderHeader(false);

    await screen.findByText("Structure read-only (extension)");
    expect(screen.queryByRole("link", { name: /View workflow/ })).not.toBeInTheDocument();
  });

  it("names the extension by its display name and version, linking to its page", async () => {
    mockedExportPolicy.mockResolvedValue({
      extension_owned: true,
      protected: true,
      reference_export_allowed: true,
      source_extension_id: "acme.hook-sample",
      source_extension_name: "Hook Sample",
      source_extension_version: "1.0.1",
      source_install_id: "4f054cc7-e9c4-44f4-9e44-6d90fc58f255",
      source_release_id: "pr_f576e7757790789ebbc24ba5",
      structure_change_allowed: false,
    } as unknown as Awaited<ReturnType<typeof fetchWorkflowExportPolicy>>);
    renderHeader(true);

    // B13: the badge read "Provided by extension: <install UUID> / <release id>".
    const link = await screen.findByRole("link", { name: "Hook Sample 1.0.1" });
    expect(link).toHaveAttribute("href", "/extensions/acme.hook-sample");
    expect(screen.getByText(/Provided by extension/)).toBeInTheDocument();
    expect(screen.queryByText(/4f054cc7/)).not.toBeInTheDocument();
    expect(screen.queryByText(/pr_f576e7757790789ebbc24ba5/)).not.toBeInTheDocument();
  });

  it("falls back to the extension id, and never to the install id", async () => {
    mockedExportPolicy.mockResolvedValue({
      extension_owned: true,
      protected: true,
      source_extension_id: "acme.hook-sample",
      source_install_id: "4f054cc7-e9c4-44f4-9e44-6d90fc58f255",
      structure_change_allowed: false,
    } as unknown as Awaited<ReturnType<typeof fetchWorkflowExportPolicy>>);
    renderHeader(true);

    expect(await screen.findByRole("link", { name: "acme.hook-sample" })).toHaveAttribute(
      "href",
      "/extensions/acme.hook-sample",
    );
    expect(screen.queryByText(/4f054cc7/)).not.toBeInTheDocument();
  });

  it("says only that an extension provides it when the install cannot be read", async () => {
    mockedExportPolicy.mockResolvedValue({
      extension_owned: true,
      protected: true,
      source_install_id: "4f054cc7-e9c4-44f4-9e44-6d90fc58f255",
      structure_change_allowed: false,
    } as unknown as Awaited<ReturnType<typeof fetchWorkflowExportPolicy>>);
    renderHeader(true);

    expect(await screen.findByText("Provided by extension")).toBeInTheDocument();
    expect(screen.queryByText(/4f054cc7/)).not.toBeInTheDocument();
  });

  it("keeps Open builder, and no View workflow, on an ordinary workflow", async () => {
    renderHeader(true);

    expect(await screen.findByRole("link", { name: /open builder/i })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /View workflow/ })).not.toBeInTheDocument();
  });
});

describe("summary graph counts and metadata isolation", () => {
  it("omits unknown summary graph counts while retaining genuine builder zero counts", async () => {
    const view = renderHeader(true, undefined, { activePage: "overview", nodeCount: undefined, edgeCount: undefined });
    await screen.findByRole("button", { name: "Disable workflow" });
    expect(screen.queryByText(/nodes.*edges/)).not.toBeInTheDocument();
    view.unmount();
    renderHeader(true, undefined, { activePage: "builder", nodeCount: 0, edgeCount: 0 });
    expect(await screen.findByText("0 nodes / 0 edges")).toBeInTheDocument();
  });

  it("prefers the supplied current protected policy over a permissive independent policy read", async () => {
    renderHeader(true, undefined, { projectId: "p-explicit", portability: { extension_owned: true, protected: true, structure_change_allowed: false, reference_export_allowed: true } as WorkflowExportPolicy });
    expect(await screen.findByText("Structure read-only (extension)")).toBeInTheDocument();
    expect(mockedExportPolicy).not.toHaveBeenCalled();
    expect(mockedLinkedProject).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Disable workflow" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View workflow (read-only)" })).toHaveAttribute("href", expect.stringContaining("project_id=p-explicit"));
  });

  it("uses refreshed supplied protection instead of an old permissive policy cache", async () => {
    const ordinary = { clone_allowed: true, full_export_allowed: true, structure_change_allowed: true } as WorkflowExportPolicy;
    const view = renderHeader(true, undefined, { projectId: "p-explicit", portability: ordinary });
    expect(await screen.findByRole("button", { name: "Disable workflow" })).toBeEnabled();
    act(() => { view.queryClient.setQueryData(["workflows", "export-policy", "wf-1"], ordinary); });
    view.rerenderHeader({ projectId: "p-explicit", portability: { ...ordinary, extension_owned: true, protected: true, clone_allowed: false, full_export_allowed: false, reference_export_allowed: true, structure_change_allowed: false } });
    expect(await screen.findByText("Structure read-only (extension)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable workflow" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View workflow (read-only)" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
    const menu = await screen.findByRole("menu", { name: "Workflow actions" });
    expect(within(menu).queryByRole("menuitem", { name: "Delete workflow" })).not.toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "Test Run" })).not.toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Workflow settings" })).toBeInTheDocument();
    expect(mockedExportPolicy).not.toHaveBeenCalled();
  });

  it("keeps fallback policy metadata nonblocking and sensitive commands fail-closed through denial", async () => {
    const actual = await vi.importActual<typeof WorkflowApi>("@/pages/workflows/shared/api");
    mockedExportPolicy.mockImplementation(actual.fetchWorkflowExportPolicy);
    let resolve!: (response: Response) => void;
    let resolveRetry!: (response: Response) => void;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockReturnValueOnce(new Promise((finish) => { resolve = finish; })).mockReturnValue(new Promise((finish) => { resolveRetry = finish; }));
    vi.useFakeTimers();
    const remove = vi.fn(); const testRun = vi.fn();
    const view = renderHeader(true, undefined, { projectId: "p-explicit", onDeleteWorkflow: remove, onWorkflowTestRun: testRun });
    const { unmount: unmountIndicator } = render(<RequestLoadingIndicator />);
    try {
      function assertCommandsBlocked() {
        if (!screen.queryByRole("menu", { name: "Workflow actions" })) fireEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
        const menu = screen.getByRole("menu", { name: "Workflow actions" });
        for (const label of ["Delete workflow", "Test Run"]) {
          const item = within(menu).queryByRole("menuitem", { name: label });
          if (item) { expect(item).toBeDisabled(); fireEvent.click(item); }
        }
        expect(remove).not.toHaveBeenCalled(); expect(testRun).not.toHaveBeenCalled();
      }
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(fetchMock.mock.calls[0][0]).toBe("/api/workflows/wf-1/export-policy");
      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Disable workflow" })).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
      assertCommandsBlocked();
      await act(async () => { resolve(new Response(JSON.stringify({ error: "Policy read denied" }), { status: 403, headers: { "content-type": "application/json" } })); await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByText("Policy read denied")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Disable workflow" })).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
      assertCommandsBlocked();
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Disable workflow" })).not.toBeInTheDocument();
      await act(async () => { resolveRetry(new Response(JSON.stringify({ clone_allowed: true, full_export_allowed: true, structure_change_allowed: true }), { headers: { "content-type": "application/json" } })); await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByRole("button", { name: "Disable workflow" })).toBeEnabled();
      expect(screen.getByRole("link", { name: /open builder/i })).toHaveAttribute("href", expect.stringContaining("project_id=p-explicit"));
      expect(mockedLinkedProject).not.toHaveBeenCalled();
    } finally {
      view.unmount(); unmountIndicator(); resolve(new Response("{}")); resolveRetry(new Response("{}"));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      vi.clearAllTimers(); vi.useRealTimers(); fetchMock.mockRestore();
    }
  });

  it("shows a context discovery failure and never clones with an invented empty fallback", async () => {
    mockedLinkedProject.mockRejectedValue(new Error("Project links unavailable"));
    const clone = vi.fn(); renderHeader(true, clone);
    expect(await screen.findByText("Project links unavailable")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
    const item = within(await screen.findByRole("menu", { name: "Workflow actions" })).queryByRole("menuitem", { name: /clone workflow/i });
    if (item) { expect(item).toBeDisabled(); fireEvent.click(item); }
    expect(clone).not.toHaveBeenCalled();
  });

  it("does not clone with an empty project while legitimate first-linked discovery is pending", async () => {
    let resolve!: (project: string) => void;
    mockedLinkedProject.mockReturnValue(new Promise((finish) => { resolve = finish; }));
    const clone = vi.fn(); renderHeader(true, clone);
    fireEvent.click(await screen.findByRole("button", { name: "Workflow actions" }));
    const pendingClone = within(await screen.findByRole("menu", { name: "Workflow actions" })).queryByRole("menuitem", { name: /clone workflow/i });
    if (pendingClone) { expect(pendingClone).toBeDisabled(); fireEvent.click(pendingClone); }
    expect(clone).not.toHaveBeenCalled();
    await act(async () => { resolve("p-first"); });
    // Reopen if the menu was hidden while discovery was pending.
    if (!screen.queryByRole("menu", { name: "Workflow actions" })) fireEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
    fireEvent.click(await within(screen.getByRole("menu", { name: "Workflow actions" })).findByRole("menuitem", { name: /clone workflow/i }));
    expect(clone).toHaveBeenCalledWith("p-first");
  });
  it.each(["pending", "rejected"])("does not navigate context-dependent commands with an unresolved %s project", async (state) => {
    let resolve!: (projectId: string) => void;
    if (state === "pending") mockedLinkedProject.mockReturnValue(new Promise((finish) => { resolve = finish; }));
    else mockedLinkedProject.mockRejectedValue(new Error("Project context unavailable"));
    const view = renderHeader(true, undefined, { onWorkflowSettings: undefined, onWorkflowTestRun: undefined });
    if (state === "pending") expect(await screen.findByRole("status")).toHaveTextContent("Loading workflow data");
    else expect(await screen.findByText("Project context unavailable")).toBeInTheDocument();
    await screen.findByRole("button", { name: "Disable workflow" });
    expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
    const menu = await screen.findByRole("menu", { name: "Workflow actions" });
    for (const label of ["Workflow settings", "Test Run"]) {
      const item = within(menu).getByRole("menuitem", { name: label });
      expect(item).toBeDisabled(); expect(item).not.toHaveAttribute("href");
      fireEvent.click(item);
    }
    if (state === "pending") {
      await act(async () => { resolve("p-first"); });
      expect(await screen.findByRole("link", { name: /open builder/i })).toHaveAttribute("href", "/registry/workflows/wf-1/builder?project_id=p-first");
      for (const label of ["Workflow settings", "Test Run"]) expect(within(menu).getByRole("menuitem", { name: label })).toHaveAttribute("href", expect.stringContaining("project_id=p-first"));
    }
    view.unmount();
  });

  it("retains existing in-place Settings and Test Run callbacks while project discovery is pending", async () => {
    mockedLinkedProject.mockReturnValue(new Promise(() => {}));
    const settings = vi.fn(); const testRun = vi.fn();
    renderHeader(true, undefined, { onWorkflowSettings: settings, onWorkflowTestRun: testRun });
    await screen.findByRole("button", { name: "Disable workflow" });
    for (const label of ["Workflow settings", "Test Run"]) {
      fireEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
      fireEvent.click(within(await screen.findByRole("menu", { name: "Workflow actions" })).getByRole("menuitem", { name: label }));
    }
    expect(settings).toHaveBeenCalledOnce(); expect(testRun).toHaveBeenCalledOnce();
  });

  it("retries only failed fallback project discovery through the same nonmodal real reads", async () => {
    const actualContext = await vi.importActual<typeof ProjectContext>("@/pages/workflows/shared/project-context");
    mockedLinkedProject.mockImplementation(actualContext.findWorkflowLinkedProjectId);
    let resolveRetry!: (response: Response) => void; let projectReads = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const path = new URL(String(input), "http://localhost").pathname;
      if (path === "/api/v2/projects") {
        projectReads += 1;
        if (projectReads === 1) return new Response(JSON.stringify({ error: "Project discovery unavailable" }), { status: 503, headers: { "content-type": "application/json" } });
        return new Promise<Response>((resolve) => { resolveRetry = resolve; });
      }
      if (path === "/api/projects/p-first/workflows") return new Response(JSON.stringify({ workflow_ids: ["wf-1"] }), { headers: { "content-type": "application/json" } });
      throw new Error(`Unexpected request ${path}`);
    });
    vi.useFakeTimers();
    const view = renderHeader(true, undefined, { portability: { clone_allowed: true, structure_change_allowed: true } as WorkflowExportPolicy, onWorkflowSettings: undefined });
    const { unmount: unmountIndicator } = render(<RequestLoadingIndicator />);
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByText("Project discovery unavailable")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(projectReads).toBe(2);
      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      expect(screen.queryByRole("link", { name: /open builder/i })).not.toBeInTheDocument();
      await act(async () => { resolveRetry(new Response(JSON.stringify({ projects: [{ id: "p-first" }], total_pages: 1 }), { headers: { "content-type": "application/json" } })); await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByRole("link", { name: /open builder/i })).toHaveAttribute("href", "/registry/workflows/wf-1/builder?project_id=p-first");
      expect(mockedExportPolicy).not.toHaveBeenCalled();
      expect(mockedLinkedProject).toHaveBeenLastCalledWith("wf-1", { presentation: "background" });
    } finally {
      view.unmount(); unmountIndicator(); resolveRetry?.(new Response("{}"));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      vi.clearAllTimers(); vi.useRealTimers(); fetchMock.mockRestore();
    }
  });

});

describe("WorkflowSummaryHeader availability and badge text", () => {
  const POSTGRES_ID = "acme.data-connector-postgres";
  const BACKEND_SENTENCE =
    `Needs the extension ${POSTGRES_ID}, which this account does not have active. Install it from Extensions > Catalog, or turn it on if it is installed. This workflow executes the projected dataset through MCP Toolbox for PostgreSQL.`;

  function unavailablePolicy(overrides: Record<string, unknown> = {}) {
    return {
      extension_owned: true,
      protected: true,
      structure_change_allowed: false,
      source_extension_id: "acme.example-showcase",
      source_extension_name: "Example Showcase Extension",
      availability_status: "unavailable",
      availability_reason_code: "required_extension_uninstalled",
      availability_reason_text: BACKEND_SENTENCE,
      new_runs_blocked: true,
      missing_extension_details: [
        { extension_id: POSTGRES_ID, display_name: "PostgreSQL (Google MCP Toolbox)", state: "not_installed" },
      ],
      viewer_can_manage_extensions: true,
      ...overrides,
    } as unknown as Awaited<ReturnType<typeof fetchWorkflowExportPolicy>>;
  }

  it("says once, in a wrapping notice and not a badge, what the workflow needs and offers Install", async () => {
    mockedExportPolicy.mockResolvedValue(unavailablePolicy());
    renderHeader(true);

    const notice = await screen.findByTestId("workflow-missing-extension-notice");
    expect(within(notice).getByText("This workflow can't run yet: it needs PostgreSQL (Google MCP Toolbox).")).toBeInTheDocument();
    expect(notice).toContainHTML('class="fs-alert warning"');
    expect(within(notice).getByRole("link", { name: "Install" })).toHaveAttribute(
      "href",
      `/extensions?find=${encodeURIComponent(POSTGRES_ID)}#marketplace`,
    );
    // The backend sentence and the raw id live only under Details, once.
    const details = within(notice).getByRole("group");
    expect(details).not.toHaveAttribute("open");
    expect(within(details).getByText("Details")).toBeInTheDocument();
    expect(within(details).getByText(POSTGRES_ID)).toBeInTheDocument();
    expect(within(details).getByText(BACKEND_SENTENCE)).toBeInTheDocument();
    expect(screen.getAllByText(BACKEND_SENTENCE)).toHaveLength(1);
    expect(screen.queryByText(/can't run yet/)).toBe(within(notice).getByText(/can't run yet/));
  });

  it("tells a viewer who cannot install extensions to ask an administrator", async () => {
    mockedExportPolicy.mockResolvedValue(unavailablePolicy({ viewer_can_manage_extensions: false }));
    renderHeader(false);

    expect(await screen.findByText("Ask your administrator to turn it on.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Install" })).not.toBeInTheDocument();
  });

  it("offers Turn on, on the installed extension's page, for one that is installed but off", async () => {
    mockedExportPolicy.mockResolvedValue(unavailablePolicy({
      missing_extension_details: [
        { extension_id: POSTGRES_ID, display_name: "PostgreSQL (Google MCP Toolbox)", state: "installed_disabled" },
      ],
    }));
    renderHeader(true);

    expect(await screen.findByRole("link", { name: "Turn on" })).toHaveAttribute(
      "href",
      `/extensions/${encodeURIComponent(POSTGRES_ID)}`,
    );
  });

  it("keeps the space after the colon in Provided by extension", async () => {
    mockedExportPolicy.mockResolvedValue(unavailablePolicy());
    renderHeader(true);

    await screen.findByRole("link", { name: "Example Showcase Extension" });
    // The label is inline text inside one wrapper, not a flex item of the badge
    // on its own, where the space after the colon collapsed.
    const label = screen.getByText("Provided by extension:");
    expect(label).not.toHaveClass("fs-badge");
    expect(label).toHaveTextContent(/^Provided by extension: Example Showcase Extension$/);
  });
});
