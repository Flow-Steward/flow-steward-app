import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { interpolate } from "@/lib/i18n/interpolate";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import { JobsListPage } from "@/pages/jobs/list";
import { LOADER_DELAY_MS, LOADER_MIN_SHOWN_MS } from "@/components/shared/use-loader-delay";

vi.mock("@/lib/auth/useAuth", () => ({
  useAuth: vi.fn(),
}));

vi.mock("@/lib/http/client", () => ({
  fetchJson: vi.fn(),
}));

vi.mock("@/lib/time/user-timezone", () => ({
  formatUserDateTime: vi.fn((value: unknown) => (value ? `formatted:${String(value)}` : "—")),
  useUserTimezone: vi.fn(() => "UTC"),
}));

const mockedUseAuth = vi.mocked(useAuth);
const mockedFetchJson = vi.mocked(fetchJson);

/** Opens the published Add filter popover on a field and lists its options. */
async function filterOptionLabels(fieldLabel: string) {
  fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
  fireEvent.click(await screen.findByRole("menuitem", { name: fieldLabel }));
  const menu = await screen.findByRole("menu", { name: "Choose a filter" });
  // A single-value field lists menuitems, a multiple one menuitemcheckboxes.
  return Array.from(menu.querySelectorAll('[role="menuitem"], [role="menuitemcheckbox"]'))
    .map((item) => item.textContent?.trim() || "")
    .filter((label) => label !== "All fields");
}

/** Exactly the eight status groups, in their order. */
const STATUS_GROUP_LABELS = ["In progress", "Waiting", "Waiting for approval", "Stuck", "Interrupted", "Failed", "Completed", "Cancelled"];

function closeFilterMenu() {
  fireEvent.keyDown(screen.getByRole("menu", { name: "Choose a filter" }), { key: "Escape" });
  fireEvent.keyDown(screen.getByRole("menu", { name: "Choose a filter" }), { key: "Escape" });
}

function LocationProbe() {
  return <output data-testid="location-search">{useLocation().search}</output>;
}

const FAILED_ROW = {
  job_id: "job-failed",
  id: "job-failed",
  project_id: "project-1",
  project_name: "Project One",
  task_summary: "Broken import",
  selected_workflow: "workflow-one",
  selected_workflow_display_name: "Inventory Import",
  status: "failed",
};

/** The jobs API answers only the applied statuses; projects and workflows come from their own lists. */
function mockFilteredJobsServer() {
  mockedFetchJson.mockImplementation(async (input) => {
    const url = String(input);
    if (url.startsWith("/api/v2/projects")) {
      return {
        projects: [
          { id: "project-1", name: "Project One" },
          { id: "project-2", name: "Project Two" },
        ],
        page: 1,
        per_page: 200,
        total: 2,
        total_pages: 1,
      } as never;
    }
    if (url.startsWith("/api/workflows")) {
      return [
        { workflow_id: "workflow-one", display_name: "Inventory Import" },
        { workflow_id: "workflow-two", display_name: "Price Sync" },
      ] as never;
    }
    return { jobs: [FAILED_ROW], page: 1, per_page: 25, total: 1, total_pages: 1 } as never;
  });
}

function jobsRequests(): URLSearchParams[] {
  return mockedFetchJson.mock.calls
    .map(([input]) => String(input))
    .filter((url) => url.startsWith("/api/v2/jobs"))
    .map((url) => new URL(url, "https://flow-steward.test").searchParams);
}

function renderJobsListPage(entry = "/jobs", locale: "en" | "lt" = "en", overrides: Record<string, string> = {}, client = new QueryClient()) {
  const messages: Record<string, string> = { ...(locale === "lt" ? {
    "react.jobs.confirm_cancel_selected": "Atšaukti pasirinktas užduotis ({count})?",
    "react.jobs.confirm_delete_selected": "Ištrinti pasirinktas užduotis ({count})?",
    "react.jobs.confirm_cancel_job": "Atšaukti užduotį #{id}?",
    "react.jobs.generated_workflow_run_summary": "Darbo eigos vykdymas: {workflow_id}",
    "react.jobs.generated_workflow_run_label": "Darbo eigos vykdymas",
  } : {}), ...overrides };
  return render(
    <QueryClientProvider client={client}>
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route element={<><JobsListPage /><LocationProbe /></>} path="/jobs" />
        <Route element={<div>job detail page</div>} path="/jobs/:jobId" />
      </Routes>
    </MemoryRouter></I18nContext.Provider></QueryClientProvider>,
  );
}

describe("JobsListPage", () => {
  it("keeps the current period and selection when a previous-scope deletion finishes", async () => {
    const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(true);
    let rejectDelete: (error: Error) => void = () => undefined;
    const deletion = new Promise<never>((_resolve, reject) => { rejectDelete = reject; });
    mockedFetchJson.mockImplementation(async (url, options) => {
      if (options?.method === "DELETE") return deletion;
      const filtered = String(url).includes("from_date=2026-03-01");
      return { jobs: [{ id: filtered ? "new-scope" : "old-scope", status: "failed" }], total: 1 } as never;
    });
    renderJobsListPage();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select old-scope" }));
    // The bulk bar renders after the selection commits; under a loaded suite that is not synchronous.
    fireEvent.click(await screen.findByRole("button", { name: "Delete Selected" }));
    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledWith("/api/jobs/old-scope", { method: "DELETE" }));
    expect(screen.getByRole("button", { name: "Delete Selected" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: /^Job period:/ }));
    fireEvent.click(await screen.findByRole("radio", { name: "Custom range" }));
    fireEvent.change(screen.getByLabelText("Custom range, first day"), { target: { value: "2026-03-01" } });
    fireEvent.change(screen.getByLabelText("Custom range, last day"), { target: { value: "2026-03-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "Select new-scope" })).not.toBeChecked());
    rejectDelete(new Error("Job is active"));
    expect(await screen.findByText(/Job is active/)).toBeInTheDocument();
    expect(jobsRequests().at(-1)?.get("from_date")).toBe("2026-03-01");
    expect(screen.getByRole("checkbox", { name: "Select new-scope" })).not.toBeChecked();
    expect(screen.queryByRole("button", { name: "Delete Selected" })).not.toBeInTheDocument();
    confirm.mockRestore();
  });
  it("trashes selected jobs and invalidates cached workflow statistics", async () => {
    const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(true);
    const client = new QueryClient();
    client.setQueryData(["workflows", "stats", "workflow-one"], { failed_count: 1 });
    let deleted = false;
    mockedFetchJson.mockImplementation(async (url, options) => {
      if (options?.method === "DELETE") {
        deleted = true;
        return { status: "deleted", job_id: "job-1" } as never;
      }
      return { jobs: deleted ? [] : [{ id: "job-1", selected_workflow: "workflow-one", status: "failed" }], total: deleted ? 0 : 1 } as never;
    });
    renderJobsListPage("/jobs", "en", {}, client);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select job-1" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Selected" }));
    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledWith("/api/jobs/job-1", { method: "DELETE" }));
    await waitFor(() => expect(screen.queryByRole("checkbox", { name: "Select job-1" })).not.toBeInTheDocument());
    expect(client.getQueryState(["workflows", "stats", "workflow-one"])?.isInvalidated).toBe(true);
    confirm.mockRestore();
  });

  it("retains failed deletions selected and shows the server error after partial success", async () => {
    const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(true);
    const rows = [{ id: "done", status: "failed" }, { id: "active", status: "in_progress" }];
    let deleted = false;
    mockedFetchJson.mockImplementation(async (url, options) => {
      if (options?.method === "DELETE") {
        if (String(url).endsWith("/active")) throw new Error("Job is active");
        deleted = true;
        return { status: "deleted" } as never;
      }
      return { jobs: deleted ? rows.slice(1) : rows } as never;
    });
    renderJobsListPage();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select done" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select active" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete Selected" }));
    expect(await screen.findByText(/Job is active/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select active" })).toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "Select done" })).not.toBeInTheDocument();
    confirm.mockRestore();
  });

  it("restores a same-day period from the URL and combines it with workflow and status", async () => {
    renderJobsListPage("/jobs?workflow_id=workflow-one&status=failed&from_date=2026-03-01&to_date=2026-03-01");
    await waitFor(() => expect(jobsRequests().at(-1)?.get("from_date")).toBe("2026-03-01"));
    expect(jobsRequests().at(-1)?.get("to_date")).toBe("2026-03-01");
    expect(jobsRequests().at(-1)?.get("workflow_id")).toBe("workflow-one");
    fireEvent.click(screen.getByRole("button", { name: /^Job period:/ }));
    fireEvent.click(await screen.findByRole("radio", { name: "All runs" }));
    await waitFor(() => expect(jobsRequests().at(-1)?.has("from_date")).toBe(false));
    expect(screen.getByTestId("location-search")).not.toHaveTextContent("from_date");
  });

  it("applies an inclusive custom date range and persists it in the URL", async () => {
    renderJobsListPage();
    fireEvent.click(await screen.findByRole("button", { name: /^Job period:/ }));
    fireEvent.click(await screen.findByRole("radio", { name: "Custom range" }));
    fireEvent.change(screen.getByLabelText("Custom range, first day"), { target: { value: "2026-03-05" } });
    fireEvent.change(screen.getByLabelText("Custom range, last day"), { target: { value: "2026-03-10" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(jobsRequests().at(-1)?.get("to_date")).toBe("2026-03-10"));
    expect(jobsRequests().at(-1)?.get("from_date")).toBe("2026-03-05");
    expect(screen.getByTestId("location-search")).toHaveTextContent("from_date=2026-03-05&to_date=2026-03-10");
  });
  beforeEach(() => {
    mockedUseAuth.mockReset();
    mockedFetchJson.mockReset();

    mockedUseAuth.mockReturnValue({
      session: {
        user: {
          roles: ["admin"],
        },
      },
    } as never);
    mockedFetchJson.mockResolvedValue({
      jobs: [
        {
          job_id: "job-1",
          id: "job-1",
          project_id: "project-1",
          project_name: "Project One",
          task_summary: "Investigate failure",
          selected_workflow: "workflow-one",
          selected_workflow_display_name: "Inventory Import",
          status: "failed",
          current_step_id: "review",
          current_agent: "operator",
          started_at: "2026-09-09T15:05:01Z",
          finished_at: "2026-09-09T15:05:14Z",
        },
      ],
    } as never);
  });

  it.each([
    ["Cancel Selected", "Atšaukti pasirinktas užduotis (2)?"],
    ["Delete Selected", "Ištrinti pasirinktas užduotis (2)?"],
  ])("localizes the %s confirmation count and refuses a declined bulk action", async (action, prompt) => {
    const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(false);
    mockedFetchJson.mockResolvedValue({ jobs: [{ job_id: "owner-job-1", id: "owner-job-1", task_summary: "Owner task one", status: "pending" }, { job_id: "owner-job-2", id: "owner-job-2", task_summary: "Owner task two", status: "pending" }] } as never);
    renderJobsListPage("/jobs", "lt");
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select owner-job-1" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select owner-job-2" }));
    fireEvent.click(screen.getByRole("button", { name: action }));
    expect(confirm).toHaveBeenCalledWith(prompt);
    expect(mockedFetchJson.mock.calls.some(([, init]) => init?.method === "POST" || init?.method === "DELETE")).toBe(false);
    expect(screen.getByRole("checkbox", { name: "Select owner-job-1" })).toBeChecked();
    confirm.mockRestore();
  });

  it("localizes row cancellation confirmation while preserving its ID and declining mutation", async () => {
    const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(false);
    mockedFetchJson.mockResolvedValue({ jobs: [{ job_id: "owner-job", id: "owner-job", task_summary: "Owner task", status: "pending" }] } as never);
    renderJobsListPage("/jobs", "lt");
    fireEvent.click(await screen.findByRole("button", { name: "Actions for job owner-job" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Cancel" }));
    expect(confirm).toHaveBeenCalledWith("Atšaukti užduotį #owner-job?");
    expect(mockedFetchJson.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
    confirm.mockRestore();
  });

  it("localizes explicitly generated workflow summaries but preserves identical unkeyed owner text", async () => {
    const summary = "Workflow run: owner-workflow-id";
    mockedFetchJson.mockResolvedValue({ jobs: [
      { job_id: "generated-job", selected_workflow: "owner-workflow-id", selected_workflow_display_name: "Owner generated flow", display_summary: "Workflow run", task_summary: summary, task_summary_key: "react.jobs.generated_workflow_run_summary", task_summary_params: { workflow_id: "owner-workflow-id" }, status: "pending" },
      { job_id: "authored-job", selected_workflow: "owner-workflow-id", selected_workflow_display_name: "Owner authored flow", display_summary: "Workflow run", task_summary: summary, status: "pending" },
    ] } as never);
    renderJobsListPage("/jobs", "lt");
    await screen.findByText("Owner generated flow");
    const table = screen.getByRole("table", { name: "Jobs" });
    expect(within(table).getByText("Darbo eigos vykdymas")).toHaveAttribute("title", "Darbo eigos vykdymas: owner-workflow-id");
    expect(within(table).getByText("Workflow run")).toHaveAttribute("title", summary);
    expect(within(table).getByText("Owner authored flow")).toHaveAttribute("title", "owner-workflow-id");
    expect(within(table).queryByText("owner-workflow-id")).not.toBeInTheDocument();
  });

  it("prioritizes workflow context and shows when each job started and completed", async () => {
    renderJobsListPage();

    const table = await screen.findByRole("table", { name: "Jobs" });
    const visibleHeaders = within(table)
      .getAllByRole("columnheader")
      .map((header) => header.textContent?.trim() || "")
      .filter(Boolean);

    expect(visibleHeaders).toEqual([
      "Workflow",
      "Project",
      "Status",
      "Started",
      "Completed",
      "Duration",
    ]);
    expect(within(table).queryByRole("columnheader", { name: "#" })).not.toBeInTheDocument();
    expect(within(table).queryByRole("columnheader", { name: "Summary" })).not.toBeInTheDocument();
    expect(within(table).queryByRole("columnheader", { name: "Step" })).not.toBeInTheDocument();
    expect(within(table).queryByRole("columnheader", { name: "Agent" })).not.toBeInTheDocument();
    expect(within(table).getByText("formatted:2026-09-09T15:05:01Z")).toBeInTheDocument();
    expect(within(table).getByText("formatted:2026-09-09T15:05:14Z")).toBeInTheDocument();
  });

  it("shows the workflow name without exposing its technical identifier", async () => {
    renderJobsListPage();

    const table = await screen.findByRole("table", { name: "Jobs" });
    const dataRow = within(table).getAllByRole("row")[1];
    expect(within(dataRow).getByText("Inventory Import")).toBeInTheDocument();
    expect(within(dataRow).getByText("Investigate failure")).toBeInTheDocument();
    expect(within(table).queryByText("workflow-one")).not.toBeInTheDocument();
  });

  it("pages jobs through the server and renders the standard table footer", async () => {
    const allRows = Array.from({ length: 26 }, (_, index) => ({
      job_id: `job-${index + 1}`,
      id: `job-${index + 1}`,
      project_id: "project-1",
      project_name: "Project One",
      task_summary: `Request ${index + 1}`,
      selected_workflow: "workflow-one",
      selected_workflow_display_name: "Inventory Import",
      status: "completed",
      started_at: "2026-09-09T15:05:01Z",
      finished_at: "2026-09-09T15:05:14Z",
    }));
    mockedFetchJson.mockImplementation(async (input) => {
      const url = new URL(String(input), "https://flow-steward.test");
      const page = Number(url.searchParams.get("page") || 1);
      const perPage = Number(url.searchParams.get("per_page") || 25);
      const start = (page - 1) * perPage;
      return {
        jobs: allRows.slice(start, start + perPage),
        page,
        per_page: perPage,
        total: allRows.length,
        total_pages: Math.ceil(allRows.length / perPage),
      } as never;
    });

    renderJobsListPage();

    expect(await screen.findByText("1–25 of 26")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));

    await waitFor(() => {
      expect(mockedFetchJson).toHaveBeenCalledWith(expect.stringContaining("page=2&per_page=25"));
    });
    expect(await screen.findByText("26–26 of 26")).toBeInTheDocument();
    expect(screen.getByText("Request 26")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("combobox", { name: "Rows per page" }));
    fireEvent.click(screen.getByRole("option", { name: "50" }));
    await waitFor(() => {
      expect(mockedFetchJson).toHaveBeenCalledWith(expect.stringContaining("page=1&per_page=50"));
    });
    expect(await screen.findByText("1–26 of 26")).toBeInTheDocument();
  });

  it("seeds the status filter from the URL so dashboard metric cards deep-link into a filtered queue", async () => {
    mockedFetchJson.mockResolvedValue({
      jobs: [
        {
          job_id: "job-running",
          id: "job-running",
          project_id: "project-1",
          project_name: "Project One",
          task_summary: "Running work",
          status: "in_progress",
        },
        {
          job_id: "job-approval",
          id: "job-approval",
          project_id: "project-1",
          project_name: "Project One",
          task_summary: "Needs approval",
          status: "awaiting_approval",
        },
      ],
    } as never);

    renderJobsListPage("/jobs?status=awaiting_approval");

    // The seeded filter shows up as an applied-filter chip, which is how the
    // published filter toolbar reports an active value.
    expect(await screen.findByText("Status · Waiting for approval")).toBeInTheDocument();
  });

  it("leaves the status filter unset when the URL carries no status", async () => {
    renderJobsListPage();

    await screen.findByRole("table", { name: "Jobs" });
    expect(screen.queryByLabelText("Remove Status filter")).not.toBeInTheDocument();
    expect((await filterOptionLabels("Status")).length).toBeGreaterThan(0);
  });

  it("enables row detail navigation from the grid", async () => {
    renderJobsListPage();

    // Clicking the row opens its detail, which is what detail:{navigate:true}
    // used to declare.
    const table = await screen.findByRole("table", { name: "Jobs" });
    const row = await within(table).findByText("Investigate failure");
    fireEvent.click(row);

    expect(await screen.findByText("job detail page")).toBeInTheDocument();
  });

  it("treats resume-pending jobs as visible, highlighted, and cancellable", async () => {
    mockedFetchJson.mockResolvedValue({
      jobs: [
        {
          job_id: "job-resume",
          id: "job-resume",
          project_id: "project-1",
          project_name: "Project One",
          task_summary: "Resume after approval",
          status: "resume_pending",
        },
        {
          job_id: "job-approval",
          id: "job-approval",
          project_id: "project-1",
          project_name: "Project One",
          task_summary: "Needs approval",
          status: "awaiting_approval",
        },
        {
          job_id: "job-pending",
          id: "job-pending",
          project_id: "project-1",
          project_name: "Project One",
          task_summary: "Queued work",
          status: "pending",
        },
      ],
    } as never);

    renderJobsListPage();

    const table = await screen.findByRole("table", { name: "Jobs" });

    // The resume-pending job is listed under its group, painted with the
    // attention tone, not the neutral fallback.
    const statusCells = within(table).getAllByText("In progress");
    expect(statusCells).toHaveLength(2);
    statusCells.forEach((cell) => expect(cell).toHaveClass("fs-badge", "orange"));

    // Cancel is gated by visible_if with an `in` set, so it is offered here...
    fireEvent.click(within(table).getByRole("button", { name: "Actions for job job-resume" }));
    expect(await screen.findByRole("menuitem", { name: "Cancel" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });

    // A durable approval pause can also be cancelled from the row menu.
    fireEvent.click(within(table).getByRole("button", { name: "Actions for job job-approval" }));
    expect(await screen.findByRole("menuitem", { name: "Cancel" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });

    expect(await filterOptionLabels("Status")).toEqual(STATUS_GROUP_LABELS);
  });

  it("selects several statuses in one filter and asks the server for any of them", async () => {
    mockFilteredJobsServer();
    renderJobsListPage();
    await screen.findByText("Broken import");

    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Status" }));
    fireEvent.click(await screen.findByRole("menuitemcheckbox", { name: "Failed" }));
    // The menu stays open for the next status.
    fireEvent.click(await screen.findByRole("menuitemcheckbox", { name: "Stuck" }));

    expect(await screen.findByText("Status · Stuck, Failed")).toBeInTheDocument();
    expect(screen.getByRole("menuitemcheckbox", { name: "Failed" })).toBeChecked();
    expect(screen.getByRole("menuitemcheckbox", { name: "Stuck" })).toBeChecked();
    // A group asks the server for every stored status it holds.
    await waitFor(() => {
      expect(jobsRequests().some((params) => params.getAll("status").join(",") === "stuck,orphaned,failed,max_iterations,triage_needed")).toBe(true);
    });
    expect(screen.getByTestId("location-search")).toHaveTextContent("?status=stuck&status=failed");

    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Failed" }));
    expect(await screen.findByText("Status · Stuck")).toBeInTheDocument();
    expect(screen.getByTestId("location-search")).toHaveTextContent("?status=stuck");
  });

  it("keeps offering every status, project and workflow while a status filter is applied", async () => {
    mockFilteredJobsServer();
    renderJobsListPage("/jobs?status=failed");
    expect(await screen.findByText("Status · Failed")).toBeInTheDocument();
    await waitFor(() => {
      expect(mockedFetchJson.mock.calls.some(([input]) => String(input).startsWith("/api/workflows"))).toBe(true);
    });

    expect(await filterOptionLabels("Status")).toEqual(STATUS_GROUP_LABELS);
    closeFilterMenu();

    expect(await filterOptionLabels("Project")).toEqual(["Project One", "Project Two"]);
    closeFilterMenu();

    expect(await filterOptionLabels("Workflow")).toEqual(["Inventory Import", "Price Sync"]);
  });

  it("restores several statuses from the URL and clears them all with the chip", async () => {
    mockFilteredJobsServer();
    renderJobsListPage("/jobs?status=failed&status=stuck");

    expect(await screen.findByText("Status · Stuck, Failed")).toBeInTheDocument();
    await waitFor(() => {
      expect(jobsRequests().at(-1)?.getAll("status")).toEqual(["stuck", "orphaned", "failed", "max_iterations", "triage_needed"]);
    });

    fireEvent.click(screen.getByRole("button", { name: "Remove Status filter" }));

    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent(/^$/);
    });
    expect(screen.queryByText(/^Status · /)).not.toBeInTheDocument();
    await waitFor(() => {
      expect(jobsRequests().at(-1)?.getAll("status")).toEqual([]);
    });
  });
  it.each(["awaiting_approval", "awaiting_timer", "awaiting_child"])("lets an administrator cancel a job paused in %s from the row menu", async (status) => {
    mockedFetchJson.mockResolvedValue({ jobs: [{ job_id: "waiting", id: "waiting", project_id: "project-1", task_summary: "Paused work", status }] } as never);
    const confirm = vi.spyOn(globalThis, "confirm").mockReturnValue(true);
    renderJobsListPage();
    fireEvent.click(await screen.findByRole("button", { name: "Actions for job waiting" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Cancel" }));
    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledWith("/api/jobs/waiting/cancel", { method: "POST" }));
    confirm.mockRestore();
  });

  it("localizes the Waiting group in both rows and filters without changing the filter code", async () => {
    mockedFetchJson.mockResolvedValue({ jobs: [{ job_id: "waiting", id: "waiting", task_summary: "Paused work", status: "awaiting_timer" }] } as never);
    renderJobsListPage("/jobs?status=awaiting_timer", "lt", { "react.jobs.status_group_waiting": "Laukiama" });
    const table = await screen.findByRole("table", { name: "Jobs" });
    expect(within(table).getByText("Laukiama")).toBeInTheDocument();
    expect(await filterOptionLabels("Status")).toContain("Laukiama");
    // The old stored-status link now names its group.
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("?status=waiting"));
  });

  it("reads links made before the groups as their groups, comma lists included", async () => {
    mockFilteredJobsServer();
    renderJobsListPage("/jobs?status=failed,stuck,awaiting_approval&status=max_iterations&status=no_such_status");

    expect(await screen.findByText("Status · Waiting for approval, Stuck, Failed")).toBeInTheDocument();
    await waitFor(() => {
      expect(jobsRequests().at(-1)?.getAll("status")).toEqual(["awaiting_approval", "stuck", "orphaned", "failed", "max_iterations", "triage_needed"]);
    });
    await waitFor(() => {
      expect(screen.getByTestId("location-search")).toHaveTextContent("?status=awaiting_approval&status=stuck&status=failed");
    });
  });

  it("badges a job by its group and marks an acknowledged run", async () => {
    mockedFetchJson.mockResolvedValue({
      jobs: [
        { job_id: "retried", id: "retried", task_summary: "Retried too often", status: "max_iterations", acknowledged_at: "2026-09-09T16:00:00Z" },
        { job_id: "lost", id: "lost", task_summary: "Lost worker", status: "orphaned" },
        { job_id: "odd", id: "odd", task_summary: "Odd code", status: "legacy_paused" },
      ],
    } as never);
    renderJobsListPage();
    const table = await screen.findByRole("table", { name: "Jobs" });
    const retried = within(table).getByRole("row", { name: /Retried too often/ });
    expect(within(retried).getByText("Failed")).toHaveClass("fs-badge", "danger");
    expect(within(retried).getByText("Acknowledged")).toHaveClass("fs-badge");
    const lost = within(table).getByRole("row", { name: /Lost worker/ });
    expect(within(lost).getByText("Stuck")).toHaveClass("fs-badge", "danger");
    expect(within(lost).queryByText("Acknowledged")).not.toBeInTheDocument();
    // An unknown code is never spelled out in the list.
    const odd = within(table).getByRole("row", { name: /Odd code/ });
    expect(within(odd).getByText("Unknown status")).toBeInTheDocument();
    expect(within(table).queryByText(/legacy_paused/i)).not.toBeInTheDocument();
  });

  it("hides acknowledged runs with a URL-backed toggle, off by default", async () => {
    mockFilteredJobsServer();
    renderJobsListPage();
    const toggle = await screen.findByRole("checkbox", { name: "Hide acknowledged" });
    expect(toggle).not.toBeChecked();
    await waitFor(() => expect(jobsRequests().length).toBeGreaterThan(0));
    expect(jobsRequests().every((params) => !params.has("hide_acknowledged"))).toBe(true);

    fireEvent.click(toggle);
    await waitFor(() => expect(jobsRequests().at(-1)?.get("hide_acknowledged")).toBe("true"));
    expect(screen.getByTestId("location-search")).toHaveTextContent("?hide_acknowledged=1");

    fireEvent.click(toggle);
    await waitFor(() => expect(jobsRequests().at(-1)?.has("hide_acknowledged")).toBe(false));
    expect(screen.getByTestId("location-search")).toHaveTextContent(/^$/);
  });

  it("restores Hide acknowledged from the URL", async () => {
    mockFilteredJobsServer();
    renderJobsListPage("/jobs?status=failed&hide_acknowledged=1");
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "Hide acknowledged" })).toBeChecked());
    await waitFor(() => {
      const request = jobsRequests().at(-1);
      expect(request?.get("hide_acknowledged")).toBe("true");
      expect(request?.getAll("status")).toEqual(["failed", "max_iterations", "triage_needed"]);
    });
  });

  describe("workflow filter names", () => {
  function mockWorkflowCatalogServer() {
    mockedFetchJson.mockImplementation(async (input) => {
      const url = String(input);
      if (url.startsWith("/api/v2/projects")) {
        return { projects: [{ id: "project-1", name: "Support" }, { id: "project-2", name: "Billing" }], page: 1, per_page: 200, total: 2, total_pages: 1 } as never;
      }
      if (url.startsWith("/api/workflows")) {
        return [
          { workflow_id: "extension.install-new.demo_group_fan_out", display_name: "Fan out demo", source_install_id: "install-new" },
          { workflow_id: "classify-support", display_name: "Classify support requests", project_id: "project-1" },
          { workflow_id: "classify-billing", display_name: "Classify support requests", project_id: "project-2" },
          { workflow_id: "extension.install-jev.classify", display_name: "Classify support requests", source_install_id: "install-jev" },
            { workflow_id: "inventory", display_name: "Inventory Import", project_id: "project-1" },
        ] as never;
      }
      if (url.startsWith("/api/extensions")) {
        return { extensions: [{ extension_id: "showcase", display_name: "Showcase", install_id: "install-new" }, { extension_id: "typesafe-jev", display_name: "TypeSafe JEV", install_id: "install-jev" }], total: 2 } as never;
      }
      return {
        jobs: [
          // Historical runs keep their recorded label when the original install is unavailable.
          { job_id: "job-ext", id: "job-ext", project_id: "project-1", project_name: "Support", task_summary: "Demo", selected_workflow: "extension.install-old.demo_group_fan_out", selected_workflow_display_name: "Original fan out demo", status: "completed" },
        ],
        page: 1, per_page: 25, total: 1, total_pages: 1,
      } as never;
    });
  }

  function workflowOptions() {
    const menu = screen.getByRole("menu", { name: "Choose a filter" });
    return within(menu).getAllByRole("menuitem").filter((item) => item.textContent !== "All fields");
  }

  it("retains a historical run name without inferring extension identity from its local id", async () => {
    mockWorkflowCatalogServer();
    renderJobsListPage("/jobs");
    const table = await screen.findByRole("table");
    expect(await within(table).findByText("Original fan out demo")).toBeInTheDocument();
    expect(within(table).queryByText(/extension\.install-old/)).not.toBeInTheDocument();
    await waitFor(() => {
      expect(mockedFetchJson.mock.calls.some(([input]) => String(input).startsWith("/api/extensions"))).toBe(true);
    });

    await filterOptionLabels("Workflow");
    const titles = workflowOptions().map((item) => item.getAttribute("title") || "");
    expect(titles.some((title) => title.includes("extension."))).toBe(false);
    expect(titles).toEqual(expect.arrayContaining(["Fan out demo", "Original fan out demo"]));
    expect(titles.some((title) => title.includes("Previous install"))).toBe(false);
  });

  it("tells same-named workflows apart by project or extension", async () => {
    mockWorkflowCatalogServer();
    renderJobsListPage("/jobs");
    await screen.findByRole("table");
    await waitFor(() => {
      expect(mockedFetchJson.mock.calls.some(([input]) => String(input).startsWith("/api/extensions"))).toBe(true);
    });
    await filterOptionLabels("Workflow");
    await waitFor(() => {
      const titles = workflowOptions().map((item) => item.getAttribute("title"));
      expect(titles).toEqual(expect.arrayContaining([
        "Classify support requests · Billing",
        "Classify support requests · Support",
        "Classify support requests · TypeSafe JEV",
      ]));
    });
    // A name nobody else uses carries no description.
    expect(workflowOptions().map((item) => item.getAttribute("title"))).toContain("Inventory Import");
  });
});
});

describe("JobsListPage first load", () => {
  let resolveJobs: (payload: unknown) => void = () => undefined;

  beforeEach(() => {
    vi.useFakeTimers();
    mockedUseAuth.mockReset();
    mockedFetchJson.mockReset();
    mockedUseAuth.mockReturnValue({ session: { user: { roles: ["admin"] } } } as never);
    const jobsAnswer = new Promise((resolve) => {
      resolveJobs = resolve;
    });
    mockedFetchJson.mockImplementation(async (input) => {
      if (String(input).startsWith("/api/v2/jobs")) return jobsAnswer as never;
      return [] as never;
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function skeletonRows(): Element[] {
    // eslint-disable-next-line testing-library/no-node-access
    return Array.from(screen.getByRole("table", { name: "Jobs" }).querySelectorAll("tbody tr.fs-table-skeleton-row"));
  }

  async function advance(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("renders the table chrome at once and keeps the body empty, not 'No jobs', for the loader delay", async () => {
    renderJobsListPage();
    await advance(LOADER_DELAY_MS - 1);

    expect(screen.getByRole("columnheader", { name: "Workflow" })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search jobs" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Jobs" })).toHaveAttribute("aria-busy", "true");
    expect(skeletonRows()).toHaveLength(0);
    expect(screen.queryByText("No jobs found")).not.toBeInTheDocument();
  });

  it("holds a fixed run of skeleton rows sized per column, then swaps in the real rows", async () => {
    renderJobsListPage();
    await advance(LOADER_DELAY_MS);

    const rows = skeletonRows();
    expect(rows).toHaveLength(5);
    // eslint-disable-next-line testing-library/no-node-access
    const lines = Array.from(rows[0].querySelectorAll<HTMLElement>(".fs-skeleton.fs-skeleton-line"));
    expect(lines.length).toBeGreaterThan(1);
    expect(new Set(lines.map((line) => line.style.width)).size).toBeGreaterThan(1);
    expect(screen.queryByText("No jobs found")).not.toBeInTheDocument();

    await act(async () => {
      resolveJobs({ jobs: [FAILED_ROW], page: 1, per_page: 25, total: 1, total_pages: 1 });
      await vi.advanceTimersByTimeAsync(0);
    });
    // Once shown, the skeleton stays for its minimum time.
    expect(skeletonRows()).toHaveLength(5);
    expect(screen.queryByText("Inventory Import")).not.toBeInTheDocument();

    await advance(LOADER_MIN_SHOWN_MS);
    expect(skeletonRows()).toHaveLength(0);
    expect(screen.getByText("Inventory Import")).toBeInTheDocument();
    expect(screen.queryByText("No jobs found")).not.toBeInTheDocument();
  });

  it("shows the empty state only when the answer is empty, without a skeleton first", async () => {
    renderJobsListPage();
    await advance(LOADER_DELAY_MS - 100);
    await act(async () => {
      resolveJobs({ jobs: [], page: 1, per_page: 25, total: 0, total_pages: 1 });
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.getByText("No jobs found")).toBeInTheDocument();
    await advance(LOADER_DELAY_MS + LOADER_MIN_SHOWN_MS);
    expect(skeletonRows()).toHaveLength(0);
    expect(screen.getByText("No jobs found")).toBeInTheDocument();
  });
});
