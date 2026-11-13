import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DashboardPage } from "./index";
import { fetchDashboardSummary } from "./api";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { OperationalOverview, type OperationalOverviewProps } from "./operational-overview";
import { ATTENTION_QUEUE_GROUPS } from "./job-links";
import { LOADER_DELAY_MS, LOADER_MIN_SHOWN_MS } from "@/components/shared/use-loader-delay";

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
    expect(screen.getByRole("link", { name: /^Failed \/ Stuck/ })).toHaveAttribute("data-state", "clear");
    expect(screen.getByRole("link", { name: /^Waiting for approval/ })).toHaveAttribute("data-state", "clear");
  });

  it("keeps zeros calm and draws the wave only on a card whose figure is explained", () => {
    renderOverview({ inProgress: 0, waiting: 0, awaitingApproval: 0, failedOrStuck: 16, oldestFailingMs: 3 * 86_400_000, completed: 5, successRate: "38.5%" });
    const card = (name: RegExp) => screen.getByRole("link", { name });
    for (const name of [/^In progress/, /^Waiting for approval/, /^Waiting:/]) {
      expect(card(name)).toHaveAttribute("data-state", "clear");
      expect(card(name)).not.toContainHTML("fs-metric-wave");
    }
    expect(card(/^Failed \/ Stuck/)).toHaveAttribute("data-state", "danger");
    expect(card(/^Failed \/ Stuck/)).toContainHTML("fs-metric-wave");
    expect(card(/^Completed/)).not.toHaveAttribute("data-state");
    expect(card(/^Completed/)).toContainHTML("fs-metric-wave");
    // The count is a live snapshot, whatever range is selected.
    expect(screen.getByText("Right now · oldest failing 3d")).toBeInTheDocument();
  });
});

describe("dashboard attention queue links", () => {
  async function showDashboard(summary: Record<string, unknown>) {
    vi.mocked(fetchDashboardSummary).mockResolvedValue(summary);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(<QueryClientProvider client={client}><MemoryRouter><DashboardPage /></MemoryRouter></QueryClientProvider>);
    // While loading the card is named by its label alone; the count arrives with the answer.
    // The loader rules hold a shown Loading state for 500 ms after a 400 ms delay, so
    // the answer can land after the default 1 s wait when the suite is busy.
    await screen.findByRole("link", { name: /^Failed \/ Stuck: / });
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
    const rail = within(screen.getByRole("region", { name: "Attention queue" }));
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

  it("name each row by its workflow and list them oldest first behind one Open queue link", async () => {
    const generatedAt = "2026-11-09T12:00:00Z";
    const hoursAgo = (hours: number) => new Date(Date.parse(generatedAt) - hours * 3_600_000).toISOString();
    const failedRun = (id: string, hours: number, extra: Record<string, unknown> = {}) => ({
      id, status: "failed", status_label: "Failed", task_source: "workflow_test_run",
      selected_workflow_display_name: "Shopify wine v2", task_summary: "Run this workflow from the builder test surface.",
      project_name: "Wine shop", finished_at: hoursAgo(hours), ...extra,
    });
    const teardown = await showDashboard({
      generated_at: generatedAt,
      groups: {
        needs_attention: [
          failedRun("job-new", 3),
          failedRun("job-live", 4, { task_source: "api", selected_workflow_display_name: "Nightly sync" }),
          failedRun("job-old", 72),
          failedRun("job-mid", 5),
          failedRun("job-a", 3.5),
          failedRun("job-b", 3.2),
          failedRun("job-c", 3.1),
        ],
      },
    });
    // The count link waits for the summary, like the cards.
    await screen.findByRole("link", { name: /Open queue · 2 more/ });
    const queue = within(screen.getByRole("region", { name: "Attention queue" }));
    const rows = queue.getAllByRole("link").filter((link) => link.getAttribute("href")?.startsWith("/jobs/"));
    expect(rows.map((row) => row.getAttribute("href"))).toEqual(["/jobs/job-old", "/jobs/job-mid", "/jobs/job-live", "/jobs/job-a", "/jobs/job-b"]);
    expect(rows[0]).toHaveTextContent("Shopify wine v2");
    expect(rows[0]).toHaveTextContent("Test run from the builder · 3d ago");
    expect(rows[0]).not.toHaveTextContent("builder test surface");
    expect(rows[2]).toHaveTextContent("Nightly sync");
    expect(rows[2]).toHaveTextContent("Wine shop · 4h ago");
    expect(queue.getByRole("link", { name: /Open queue · 2 more/ })).toBeInTheDocument();
    expect(screen.queryByText(/more in the queue/)).not.toBeInTheDocument();
    expect(screen.getByText("Right now · oldest failing 3d")).toBeInTheDocument();
    teardown();
  });

  it("hide the button and keep the rail link filtered when nothing needs attention", async () => {
    const teardown = await showDashboard({ hero_kpis: { needs_attention: { value: 0 } }, groups: { needs_attention: [] } });
    expect(screen.queryByRole("link", { name: /Resolve attention/ })).not.toBeInTheDocument();
    expect(statusesOf(screen.getByRole("link", { name: /Open queue/ }))).toEqual([...ATTENTION_QUEUE_GROUPS]);
    teardown();
  });
});

/** The figure and description of every card, as text: empty while loading. */
function cardFigures(): string[] {
  return Array.from(document.querySelectorAll(".fs-metric-figure")).map((node) => node.textContent || "");
}

function paintedSkeletons(container: ParentNode = document): number {
  return container.querySelectorAll(".fs-skeleton").length;
}

describe("dashboard status cards while loading", () => {
  it("keep the real labels and stand the figure and description on the Skeleton, with no state or wave", () => {
    renderOverview({ loading: "skeleton", needsAttention: 0, failedOrStuck: 0 });
    for (const label of ["Failed / Stuck", "In progress", "Waiting for approval", "Waiting", "Completed"]) {
      const card = screen.getByRole("link", { name: label });
      expect(card).toHaveAttribute("aria-busy", "true");
      expect(card).not.toHaveAttribute("data-state");
      expect(card).not.toContainHTML("fs-metric-wave");
      expect(paintedSkeletons(card)).toBe(2);
    }
    expect(cardFigures()).toEqual(["", "", "", "", ""]);
    expect(screen.queryByText("None right now")).not.toBeInTheDocument();
    // The all-clear is an answer, not a wait.
    expect(screen.queryByText("Everything is running clean")).not.toBeInTheDocument();
  });

  it("hold the same structure unpainted before the loader delay", () => {
    renderOverview({ loading: "reserved" });
    expect(screen.getByRole("link", { name: "Completed" })).toBeInTheDocument();
    expect(cardFigures()).toEqual(["", "", "", "", ""]);
    expect(paintedSkeletons()).toBe(0);
  });
});

describe("dashboard loading timing", () => {
  type Deferred = { promise: Promise<Record<string, unknown>>; resolve: (value: Record<string, unknown>) => void };
  function deferred(): Deferred {
    let resolve: Deferred["resolve"] = () => undefined;
    const promise = new Promise<Record<string, unknown>>((done) => { resolve = done; });
    return { promise, resolve };
  }
  const SUMMARY_24H = { hero_kpis: { needs_attention: { value: 1 }, in_progress: { value: 7 }, waiting: { value: 4 } }, groups: { needs_attention: [{ id: "job-1", status: "failed" }] } };
  const SUMMARY_7D = { hero_kpis: { needs_attention: { value: 1 }, in_progress: { value: 9 }, waiting: { value: 6 } }, groups: { needs_attention: [{ id: "job-1", status: "failed" }] } };

  let answers: Record<string, Deferred>;

  beforeEach(() => {
    vi.useFakeTimers();
    answers = { "24h": deferred(), "7d": deferred() };
    vi.mocked(fetchDashboardSummary).mockReset();
    vi.mocked(fetchDashboardSummary).mockImplementation((range: string) => answers[range].promise as never);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  async function advance(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  function showDashboardPage() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(<QueryClientProvider client={client}><MemoryRouter><DashboardPage /></MemoryRouter></QueryClientProvider>);
    return () => { view.unmount(); client.clear(); };
  }

  function queueSkeletonRows(): number {
    // eslint-disable-next-line testing-library/no-node-access
    return screen.getByRole("region", { name: "Attention queue" }).querySelectorAll(".fs-cc__queue-row--skeleton").length;
  }

  it("paints nothing for the first 400ms and never a zero, then the cards and three queue rows on the Skeleton", async () => {
    const teardown = showDashboardPage();
    await advance(LOADER_DELAY_MS - 1);
    expect(cardFigures()).toEqual(["", "", "", "", ""]);
    expect(paintedSkeletons()).toBe(0);
    expect(screen.queryByText("Nothing needs you")).not.toBeInTheDocument();
    expect(screen.getByText("Failed, blocked or interrupted jobs, oldest first.")).toBeInTheDocument();
    // The quick links are static: no loading state at all.
    expect(screen.getByRole("link", { name: /Create a project/ })).toBeInTheDocument();

    await advance(1);
    expect(paintedSkeletons(screen.getByRole("link", { name: "In progress" }))).toBe(2);
    expect(queueSkeletonRows()).toBe(3);
    expect(cardFigures()).toEqual(["", "", "", "", ""]);

    await act(async () => {
      answers["24h"].resolve(SUMMARY_24H);
      await vi.advanceTimersByTimeAsync(0);
    });
    // Held for the minimum time once shown.
    expect(cardFigures()).toEqual(["", "", "", "", ""]);
    await advance(LOADER_MIN_SHOWN_MS);
    expect(screen.getByRole("link", { name: /^In progress: 7\./ })).toBeInTheDocument();
    expect(queueSkeletonRows()).toBe(0);
    expect(paintedSkeletons()).toBe(0);
    teardown();
  });

  it("put the cards back in Loading on a range switch and never show the previous range's numbers", async () => {
    const teardown = showDashboardPage();
    await act(async () => {
      answers["24h"].resolve(SUMMARY_24H);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByRole("link", { name: /^In progress: 7\./ })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await advance(0);
    expect(screen.queryByRole("link", { name: /^In progress: 7\./ })).not.toBeInTheDocument();
    expect(cardFigures()).toEqual(["", "", "", "", ""]);
    // The queue is a live snapshot, not range-scoped: it keeps its rows.
    expect(queueSkeletonRows()).toBe(0);
    expect(screen.getAllByRole("link").some((link) => link.getAttribute("href") === "/jobs/job-1")).toBe(true);

    await advance(LOADER_DELAY_MS);
    expect(paintedSkeletons(screen.getByRole("link", { name: "In progress" }))).toBe(2);

    await act(async () => {
      answers["7d"].resolve(SUMMARY_7D);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.queryByRole("link", { name: /^In progress: 9\./ })).not.toBeInTheDocument();
    await advance(LOADER_MIN_SHOWN_MS);
    expect(screen.getByRole("link", { name: /^In progress: 9\./ })).toBeInTheDocument();
    expect(cardFigures()).not.toContain("");
    teardown();
  });
});
