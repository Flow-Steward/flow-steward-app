import type * as WorkflowApi from "@/pages/workflows/shared/api";
import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";
import { act, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";

import { useCanReadWorkflowRegistry, useIsAdmin } from "@/lib/auth/use-is-admin";
import { HttpError } from "@/lib/http/errors";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { WorkflowsOverviewPage } from "@/pages/workflows/overview";
import {
  fetchWorkflowDag,
  fetchWorkflowDetail,
  fetchWorkflowExportPolicy,
  fetchWorkflowStats,
  fetchWorkflowValidation,
} from "@/pages/workflows/shared/api";

vi.mock("@/lib/auth/use-is-admin", () => ({
  useIsAdmin: vi.fn(),
  useCanReadWorkflowRegistry: vi.fn(),
}));

vi.mock("@/pages/workflows/shared/api", () => ({
  fetchWorkflowDag: vi.fn(),
  fetchWorkflowDetail: vi.fn(),
  fetchWorkflowExportPolicy: vi.fn(),
  fetchWorkflowStats: vi.fn(),
  fetchWorkflowValidation: vi.fn(),
}));

const mockedUseIsAdmin = vi.mocked(useIsAdmin);
const mockedUseCanReadWorkflowRegistry = vi.mocked(useCanReadWorkflowRegistry);
const mockedFetchWorkflowDag = vi.mocked(fetchWorkflowDag);
const mockedFetchWorkflowDetail = vi.mocked(fetchWorkflowDetail);
const mockedFetchWorkflowExportPolicy = vi.mocked(fetchWorkflowExportPolicy);
const mockedFetchWorkflowStats = vi.mocked(fetchWorkflowStats);
const mockedFetchWorkflowValidation = vi.mocked(fetchWorkflowValidation);

function renderPage(initialEntry = "/registry/workflows/wf_missing", locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  const messages: Record<string, string> = locale === "lt" ? {
    "react.workflows.overview_description": "Šios darbo eigos statistika ir naujausia veikla.",
    "react.workflows.completed_failed_counts": "Baigta: {completed} / nepavyko: {failed}",
    "react.workflows.no_cost_data_recorded": "Išlaidų duomenų neužfiksuota",
    "react.workflows.finished_runs_only": "Tik baigti paleidimai",
    "react.workflows.active_statuses_hint": "Laukia, vykdoma arba laukia patvirtinimo",
    "react.workflows.active_now_count": "Dabar aktyvu: {count}",
    "react.workflows.jobs_with_cost_data_count": "Užduočių su išlaidų duomenimis: {count}",
    "react.workflows.across_runs_count": "Per {count} paleidimus",
  } : {};
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route element={<WorkflowsOverviewPage />} path="/registry/workflows/:workflowId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
    </I18nContext.Provider>,
  );
}

describe("WorkflowsOverviewPage", () => {
  it("refreshes actual overview statistics after30s without taking focus or replacing the loaded page", async () => {
    const actual = await vi.importActual<typeof WorkflowApi>("@/pages/workflows/shared/api");
    mockedFetchWorkflowStats.mockImplementation(actual.fetchWorkflowStats);
    mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "wf_missing", display_name: "Fixture workflow", enabled: true } as never);
    const stats = { workflow_id: "wf_missing", total_runs: 1, completed_count: 1, failed_count: 0, active_count: 0, cancelled_count: 0, success_rate: 1, total_cost_usd: 0, jobs_with_cost: 0, avg_cost_per_run_usd: 0, avg_duration_seconds: null, last_run_at: null };
    let pending: ((response: Response) => void) | undefined;
    let calls = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      expect(String(input)).toBe("/api/workflows/wf_missing/stats");
      calls += 1;
      if (calls > 1) return new Promise<Response>((resolve) => { pending = resolve; });
      return new Response(JSON.stringify(stats), { headers: { "content-type": "application/json" } });
    });
    vi.useFakeTimers();
    const view = renderPage();
    const { unmount: unmountIndicator } = render(<RequestLoadingIndicator />);
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      const link = screen.getByRole("link", { name: "Linked projects" });
      link.focus();
      expect(screen.getByText("1 completed / 0 failed")).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(pending).toBeDefined();
      await act(async () => { await vi.advanceTimersByTimeAsync(400); });
      expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Linked projects" })).toBe(link);
      expect(link).toHaveFocus();
      expect(screen.getByText("1 completed / 0 failed")).toBeInTheDocument();
      await act(async () => { pending?.(new Response(JSON.stringify({ ...stats, total_runs: 3, completed_count: 3 }), { headers: { "content-type": "application/json" } })); await vi.advanceTimersByTimeAsync(0); });
      expect(screen.getByText("3 completed / 0 failed")).toBeInTheDocument();
      expect(link).toHaveFocus();
    } finally {
      view.unmount(); unmountIndicator(); pending?.(new Response("{}"));
      await act(async () => { await vi.advanceTimersByTimeAsync(0); });
      vi.clearAllTimers(); vi.useRealTimers(); fetchMock.mockRestore();
    }
  });

  it("localizes empty workflow overview captions found in the browser", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "wf_missing", display_name: "Fixture workflow", description: "", enabled: false } as never);
    mockedFetchWorkflowStats.mockResolvedValue({ workflow_id: "wf_missing", total_runs: 0, completed_count: 0, failed_count: 0, active_count: 0, cancelled_count: 0, success_rate: null, total_cost_usd: 0, jobs_with_cost: 0, avg_cost_per_run_usd: 0, avg_duration_seconds: null, last_run_at: null });
    renderPage("/registry/workflows/wf_missing", "lt");
    expect(await screen.findByText("Šios darbo eigos statistika ir naujausia veikla.")).toBeInTheDocument();
    expect(await screen.findByText("Baigta: 0 / nepavyko: 0")).toBeInTheDocument();
    expect(screen.getByText("Išlaidų duomenų neužfiksuota")).toBeInTheDocument();
    expect(screen.getByText("Tik baigti paleidimai")).toBeInTheDocument();
    expect(screen.getByText("Laukia, vykdoma arba laukia patvirtinimo")).toBeInTheDocument();
  });

  it("localizes populated workflow run and cost captions without changing workflow data", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "wf_missing", display_name: "Fixture workflow", description: "Owner-authored workflow description", enabled: true } as never);
    mockedFetchWorkflowStats.mockResolvedValue({ workflow_id: "wf_missing", total_runs: 4, completed_count: 2, failed_count: 1, active_count: 1, cancelled_count: 0, success_rate: 0.5, total_cost_usd: 2, jobs_with_cost: 3, avg_cost_per_run_usd: 0.5, avg_duration_seconds: 60, last_run_at: null });
    renderPage("/registry/workflows/wf_missing", "lt");
    expect(await screen.findByText("Owner-authored workflow description")).toBeInTheDocument();
    expect(await screen.findByText("Dabar aktyvu: 1")).toBeInTheDocument();
    expect(screen.getByText("Užduočių su išlaidų duomenimis: 3")).toBeInTheDocument();
    expect(screen.getByText("Per 4 paleidimus")).toBeInTheDocument();
    expect(screen.getByText("Baigta: 2 / nepavyko: 1")).toBeInTheDocument();
  });
  beforeEach(() => {
    mockedUseIsAdmin.mockReset();
    mockedUseCanReadWorkflowRegistry.mockReset();
    mockedUseIsAdmin.mockReturnValue(false);
    // Default to an operator-level session that may read the global registry so the
    // existing summary/not-found cases exercise the loaded queries.
    mockedUseCanReadWorkflowRegistry.mockReturnValue(true);
    mockedFetchWorkflowDag.mockReset();
    mockedFetchWorkflowDetail.mockReset();
    mockedFetchWorkflowExportPolicy.mockReset();
    mockedFetchWorkflowExportPolicy.mockResolvedValue({ structure_change_allowed: true, full_export_allowed: true } as never);
    mockedFetchWorkflowStats.mockReset();
    mockedFetchWorkflowValidation.mockReset();

    mockedFetchWorkflowDag.mockResolvedValue({
      workflow_id: "wf_missing",
      nodes: [],
      edges: [],
    } as never);
    mockedFetchWorkflowStats.mockResolvedValue({
      workflow_id: "wf_missing",
      total_runs: 0,
      completed_runs: 0,
      failed_runs: 0,
      success_rate: null,
      total_cost_usd: 0,
      average_cost_usd: 0,
      average_duration_seconds: null,
      last_run_at: null,
      active_runs: 0,
      cancelled_runs: 0,
    } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      workflow_id: "wf_missing",
      validation: {
        valid: false,
        errors: [{ code: "workflow.not_found", message: "Workflow is missing." }],
        warnings: [],
      },
    } as never);
  });

  it.each([true, false])("offers Quick links Test Run only when admin=%s while retaining read links", async (admin) => {
    mockedUseIsAdmin.mockReturnValue(admin);
    mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "wf_missing", display_name: "Fixture workflow", enabled: true } as never);
    renderPage("/registry/workflows/wf_missing?project_id=proj-1");
    await screen.findByRole("heading", { name: "Fixture workflow" });
    expect(screen.getByRole("link", { name: /Jobs filtered by this workflow/i })).toHaveAttribute("href", expect.stringContaining("workflow_id=wf_missing"));
    expect(screen.getByRole("link", { name: "Linked projects" })).toHaveAttribute("href", "/registry/workflows/wf_missing/projects?project_id=proj-1");
    if (admin) {
      expect(screen.getByRole("link", { name: "Test Run" })).toHaveAttribute("href", "/registry/workflows/wf_missing/builder?project_id=proj-1&drawer=workflow-test-run");
    } else {
      expect(screen.queryByRole("link", { name: "Test Run" })).not.toBeInTheDocument();
    }
  });

  it("renders a not-found state instead of a placeholder workflow shell", async () => {
    mockedFetchWorkflowDetail.mockRejectedValue(
      new HttpError({
        status: 404,
        url: "/api/workflows/wf_missing",
        payload: { detail: "Workflow 'wf_missing' not found" },
      }),
    );

    renderPage();

    const notFoundTitle = await screen.findByText("Workflow not found");
    expect(notFoundTitle).toBeInTheDocument();
    // The load-error branch is a branch assigned to pageBody that sits above
    // the main tree; it must still land inside the page's PageContainer
    // rather than escaping it.
    expect(
      within(screen.getByTestId("page-container")).getByText("Workflow not found"),
    ).toBe(notFoundTitle);
    expect(
      screen.getByText("This workflow was deleted or is no longer available in this workspace."),
    ).toBeInTheDocument();
    expect(screen.getByText("Workflow 'wf_missing' not found")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Back to workflow catalog/i }),
    ).toHaveAttribute("href", "/registry/workflows");
    expect(screen.queryByText("Stats and recent activity for this workflow.")).not.toBeInTheDocument();
  });

  it("shows a restricted state for viewers without firing registry requests", async () => {
    mockedUseCanReadWorkflowRegistry.mockReturnValue(false);

    renderPage();

    const restrictedTitle = await screen.findByText("Workflow catalog is restricted");
    expect(restrictedTitle).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Test Run" })).not.toBeInTheDocument();
    // Same requirement as the load-error branch: the access-denied branch is
    // the first of the three branches assigned to pageBody and must not
    // bypass the container either.
    expect(
      within(screen.getByTestId("page-container")).getByText("Workflow catalog is restricted"),
    ).toBe(restrictedTitle);
    expect(mockedFetchWorkflowDetail).not.toHaveBeenCalled();
    expect(mockedFetchWorkflowDag).not.toHaveBeenCalled();
    expect(mockedFetchWorkflowStats).not.toHaveBeenCalled();
    expect(mockedFetchWorkflowValidation).not.toHaveBeenCalled();
  });

  // The alert wrapped its children in a <p>, and this surface passes two
  // paragraphs. A <p> cannot contain a <p>: the browser closes the outer one,
  // React hydrates a tree it did not render, and the console fills with
  // hydration warnings on an error screen — where the console is exactly where
  // someone is looking.
  it("renders the not-found alert without nesting a paragraph in a paragraph", async () => {
    mockedFetchWorkflowDetail.mockRejectedValue(
      new HttpError({
        status: 404,
        url: "/api/workflows/wf_missing",
        payload: { detail: "Workflow 'wf_missing' not found" },
      }),
    );

    renderPage();

    const detail = await screen.findByText(
      "This workflow was deleted or is no longer available in this workspace.",
    );

    // Invalid nesting is a structural property with no accessible-role
    // expression, so there is no user-facing query that can see it.
    expect(detail.parentElement?.tagName.toLowerCase()).not.toBe("p");
  });
});
