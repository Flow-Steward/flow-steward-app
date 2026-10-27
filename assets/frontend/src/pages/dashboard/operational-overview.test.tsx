import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DashboardPage } from "./index";
import { fetchDashboardSummary } from "./api";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { OperationalOverview, type OperationalOverviewProps } from "./operational-overview";
import { ATTENTION_QUEUE_GROUPS } from "./job-links";

vi.mock("./api", () => ({ fetchDashboardSummary: vi.fn() }));
vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: () => true }));

const props: OperationalOverviewProps = {
  range: "24h", onRangeChange: () => {}, needsAttention: 5, failedOrStuck: 2, inProgress: 2, awaitingApproval: 3,
  waiting: 11, completed: 4, queued: 0, longestRunningMs: null, oldestFailingMs: null, oldestWaitingMs: null,
  successRate: "100%", successRateDeltaPp: null, generatedAt: "",
};

function queryOf(link: HTMLElement): URLSearchParams {
  const href = link.getAttribute("href") || "";
  expect(href.startsWith("/jobs?")).toBe(true);
  return new URLSearchParams(href.slice("/jobs?".length));
}

function statusesOf(link: HTMLElement): string[] {
  return queryOf(link).getAll("status");
}

function renderOverview(overrides: Partial<OperationalOverviewProps> = {}) {
  return render(<MemoryRouter><OperationalOverview {...props} {...overrides} /></MemoryRouter>);
}

describe("dashboard status cards", () => {
  it("link each card to the job list filtered by its status groups, under the group labels", () => {
    renderOverview();
    const failedOrStuck = screen.getByRole("link", { name: "Failed / Stuck: 2. Show failed and stuck jobs" });
    expect(statusesOf(failedOrStuck)).toEqual(["failed", "stuck"]);
    // The count leaves acknowledged runs out, so the list it opens does too.
    expect(queryOf(failedOrStuck).get("hide_acknowledged")).toBe("1");
    expect(statusesOf(screen.getByRole("link", { name: /^In progress: 2\./ }))).toEqual(["in_progress"]);
    expect(statusesOf(screen.getByRole("link", { name: "Waiting for approval: 3. Show jobs waiting for approval" }))).toEqual(["awaiting_approval"]);
    expect(statusesOf(screen.getByRole("link", { name: "Waiting: 11. Show waiting jobs" }))).toEqual(["waiting"]);
    expect(statusesOf(screen.getByRole("link", { name: /^Completed: 4\./ }))).toEqual(["completed"]);
    expect(["Failed / Stuck", "In progress", "Waiting for approval", "Waiting", "Completed"].map((label) => screen.getByText(label))).toHaveLength(5);
  });

  it("has no timer or child-workflow card of its own", () => {
    renderOverview();
    expect(screen.queryByText("Waiting for timer")).not.toBeInTheDocument();
    expect(screen.queryByText("Waiting for child workflow")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /timer/i })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link").map((link) => link.getAttribute("href"))).not.toContainEqual(expect.stringContaining("awaiting_timer"));
  });

  it("paints broken runs danger and approvals attention, and neither at zero", () => {
    const view = renderOverview();
    expect(screen.getByRole("link", { name: /^Failed \/ Stuck/ })).toHaveAttribute("data-state", "danger");
    expect(screen.getByRole("link", { name: /^Waiting for approval/ })).toHaveAttribute("data-state", "attention");
    expect(screen.getByRole("link", { name: /^Completed/ })).not.toHaveAttribute("data-state");
    view.unmount();

    renderOverview({ failedOrStuck: 0, awaitingApproval: 0, needsAttention: 0 });
    expect(screen.getByRole("link", { name: /^Failed \/ Stuck/ })).not.toHaveAttribute("data-state");
    expect(screen.getByRole("link", { name: /^Waiting for approval/ })).not.toHaveAttribute("data-state");
  });
});

describe("dashboard attention queue links", () => {
  async function showDashboard(summary: Record<string, unknown>) {
    vi.mocked(fetchDashboardSummary).mockResolvedValue(summary);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(<QueryClientProvider client={client}><MemoryRouter><DashboardPage /></MemoryRouter></QueryClientProvider>);
    await screen.findByRole("link", { name: /^Failed \/ Stuck/ });
    return () => { view.unmount(); client.clear(); };
  }

  it("open the job list with the queue's own groups, without acknowledged runs, from the button and the rail", async () => {
    const teardown = await showDashboard({
      hero_kpis: { needs_attention: { value: 5 }, awaiting_approval: { value: 1 }, in_progress: { value: 4 }, waiting: { value: 11 } },
      groups: {
        needs_attention: [
          { id: "job-1", status: "failed" },
          { id: "job-2", status: "awaiting_approval" },
          { id: "job-3", status: "orphaned" },
          { id: "job-4", status: "max_iterations" },
          { id: "job-5", status: "retryable_interrupted_step" },
        ],
      },
    });
    const button = await screen.findByRole("link", { name: /Resolve attention/ });
    const rail = within(screen.getByRole("complementary", { name: "Attention queue" }));
    const openQueue = rail.getByRole("link", { name: /Open queue/ });
    expect(statusesOf(button)).toEqual([...ATTENTION_QUEUE_GROUPS]);
    expect(queryOf(button).get("hide_acknowledged")).toBe("1");
    expect(openQueue).toHaveAttribute("href", button.getAttribute("href"));
    expect(rail.getAllByRole("link").filter((link) => link.getAttribute("href")?.startsWith("/jobs/"))).toHaveLength(5);
    // Counted against the groups: Failed (failed, max_iterations) and Stuck
    // (orphaned) make 3; the approval and the interrupted run are not red.
    expect(screen.getByRole("link", { name: /^Failed \/ Stuck: 3\./ })).toHaveAttribute("data-state", "danger");
    expect(screen.getByRole("link", { name: /^Waiting for approval: 1\./ })).toHaveAttribute("data-state", "attention");
    expect(screen.getByRole("link", { name: /^In progress: 4\./ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Waiting: 11. Show waiting jobs" })).toBeInTheDocument();
    teardown();
  });

  it("hide the button and keep the rail link filtered when nothing needs attention", async () => {
    const teardown = await showDashboard({ hero_kpis: { needs_attention: { value: 0 } }, groups: { needs_attention: [] } });
    expect(screen.queryByRole("link", { name: /Resolve attention/ })).not.toBeInTheDocument();
    expect(statusesOf(screen.getByRole("link", { name: /Open queue/ }))).toEqual([...ATTENTION_QUEUE_GROUPS]);
    teardown();
  });
});
