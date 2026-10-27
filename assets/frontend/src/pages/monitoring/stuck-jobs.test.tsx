import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";

import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import { WORKSPACE_STORAGE_KEY } from "@/lib/utils/workspace-selection";
import { JobsListPage } from "@/pages/jobs/list";
import { MonitoringPage } from "@/pages/monitoring";
import { fetchMonitoringStorage, fetchMonitoringSummary } from "@/pages/monitoring/api";

vi.mock("@/lib/auth/useAuth", () => ({ useAuth: vi.fn() }));
vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));
vi.mock("@/pages/monitoring/api", () => ({
  cleanupMonitoringStorageOrphan: vi.fn(),
  fetchMonitoringStorage: vi.fn(),
  fetchMonitoringSummary: vi.fn(),
}));

function renderMonitoring() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/monitoring?tab=services"]}>
        <Routes>
          <Route path="/monitoring" element={<MonitoringPage />} />
          <Route path="/jobs" element={<JobsListPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Monitoring stuck job navigation", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    localStorage.setItem(WORKSPACE_STORAGE_KEY, "workspace-own");
    vi.mocked(useAuth).mockReturnValue({ session: { user: { roles: ["admin"] } } } as never);
    vi.mocked(fetchMonitoringStorage).mockResolvedValue({});
    vi.mocked(fetchMonitoringSummary).mockResolvedValue({
      ready: false,
      summary: { checks_total: 2, checks_ok: 1, checks_failed: 1 },
      checks: {
        postgres: { ok: true },
        stuck_jobs: {
          ok: false,
          persisted_count: 26,
          detection_count: 1,
          detail: "26 persisted stuck job(s); 1 additional watchdog detection(s).",
        },
      },
    });
  });

  afterEach(() => {
    localStorage.removeItem(WORKSPACE_STORAGE_KEY);
  });

  it("opens the real Jobs page with persisted-stuck status and keeps workspace scope during pagination", async () => {
    vi.mocked(fetchJson).mockImplementation(async (input) => {
      const url = new URL(String(input), "https://flow-steward.test");
      const page = Number(url.searchParams.get("page") || 1);
      return {
        jobs: [{ id: `stuck-${page}`, job_id: `stuck-${page}`, project_id: "project-own",
          project_name: "Own project", task_summary: `Persisted stuck job ${page}`, status: "stuck" }],
        total: 26, page, per_page: 25, total_pages: 2,
      } as never;
    });
    renderMonitoring();

    const link = await screen.findByRole("link", { name: "View persisted stuck jobs" });
    expect(link).toHaveAttribute("href", "/jobs?status=stuck");
    expect(screen.getByText("Failed checks: 1")).toBeInTheDocument();
    expect(screen.getByText(/26 persisted stuck job/)).toBeInTheDocument();
    fireEvent.click(link);

    expect(await screen.findByText("Status · Stuck")).toBeInTheDocument();
    expect(await screen.findByText("Persisted stuck job 1")).toBeInTheDocument();
    // The link opens the Stuck status group, which holds stuck and orphaned runs.
    await waitFor(() => expect(vi.mocked(fetchJson)).toHaveBeenCalledWith(
      "/api/v2/jobs?page=1&per_page=25&status=stuck&status=orphaned&workspace_id=workspace-own",
    ));
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("Persisted stuck job 2")).toBeInTheDocument();
    expect(screen.queryByText("Persisted stuck job 1")).not.toBeInTheDocument();
    expect(screen.getByText("Status · Stuck")).toBeInTheDocument();
    expect(vi.mocked(fetchJson)).toHaveBeenCalledWith(
      "/api/v2/jobs?page=2&per_page=25&status=stuck&status=orphaned&workspace_id=workspace-own",
    );
  });

  it("keeps the persisted-stuck filter on an empty queue instead of treating watchdog detections as persisted jobs", async () => {
    vi.mocked(fetchMonitoringSummary).mockResolvedValue({
      ready: false,
      summary: { checks_total: 1, checks_ok: 0, checks_failed: 1 },
      checks: { stuck_jobs: { ok: false, persisted_count: 0, detection_count: 2,
        detail: "0 persisted stuck job(s); 2 additional watchdog detection(s)." } },
    });
    vi.mocked(fetchJson).mockResolvedValue({ jobs: [], total: 0, page: 1, per_page: 25, total_pages: 1 } as never);
    renderMonitoring();

    fireEvent.click(await screen.findByRole("link", { name: "View persisted stuck jobs" }));
    expect(await screen.findByText("Status · Stuck")).toBeInTheDocument();
    expect(await screen.findByRole("table", { name: "Jobs" })).toBeInTheDocument();
    expect(vi.mocked(fetchJson)).toHaveBeenCalledWith(
      "/api/v2/jobs?page=1&per_page=25&status=stuck&status=orphaned&workspace_id=workspace-own",
    );
  });
});
