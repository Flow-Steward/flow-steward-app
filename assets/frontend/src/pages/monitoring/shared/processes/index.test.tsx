import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { ProcessesControlPanel } from "@/pages/monitoring/shared/processes";
import { cancelProcessJob, fetchProcessesPayload } from "@/pages/monitoring/shared/processes/api";

vi.mock("@/pages/monitoring/shared/processes/api", () => ({
  cancelProcessJob: vi.fn(),
  fetchProcessesPayload: vi.fn(),
  killWorkerProcess: vi.fn(),
}));

const mockedFetchProcessesPayload = vi.mocked(fetchProcessesPayload);
const mockedCancelProcessJob = vi.mocked(cancelProcessJob);

const THREE_JOBS = {
  jobs: [
    { id: 11, project_name: "Alpha", display_summary: "Index", status: "in_progress" },
    { id: 12, project_name: "Beta", display_summary: "Review", status: "in_progress" },
    { id: 13, project_name: "Gamma", display_summary: "Sync", status: "pending" },
  ],
  workers: [],
  subprocesses: [],
  worker_processes: [],
};

class TestPlatformGrid extends HTMLElement {
  config?: Record<string, unknown>;
  dataPayload?: { items: Record<string, unknown>[]; total: number };

  setData(payload: { items: Record<string, unknown>[]; total: number }) {
    this.dataPayload = payload;
  }
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProcessesControlPanel />
    </QueryClientProvider>,
  );
}

describe("ProcessesControlPanel", () => {
  beforeAll(() => {
    if (!globalThis.customElements.get("platform-grid")) {
      globalThis.customElements.define("platform-grid", TestPlatformGrid);
    }
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("counts the worker processes, not the subprocesses of a running job", async () => {
    // AC17-R69: on the owner's Compact server two workers were alive as PIDs 30 and 31 and
    // the page said "Worker processes: 0". The chip counted `subprocesses`, which are the
    // children of an *active job* and are empty on a healthy idle install, so the number the
    // installer asks for — FS_WORKER_PROCESSES — could not be confirmed to have taken effect.
    mockedFetchProcessesPayload.mockResolvedValue({
      jobs: [],
      workers: [],
      subprocesses: [],
      worker_processes: [
        { pid: 30, cmdline: "python -m core.entrypoints.cli.worker_process", rss_mb: 120 },
        { pid: 31, cmdline: "python -m core.entrypoints.cli.worker_process", rss_mb: 118 },
      ],
    });

    renderPanel();

    // the chip renders immediately with 0 while the query is in flight, so wait on content
    await waitFor(() =>
      expect(screen.getByTestId("proc-worker-process-count")).toHaveTextContent(
        "Worker processes: 2",
      ),
    );
    // and the empty subprocess table must say idle, not absent
    expect(await screen.findByText(/2 worker processes are running and idle/)).toBeInTheDocument();
  });

  it("keeps saying nothing is there when no worker process is visible", async () => {
    // Scalable: the workers are in another container, so reporting none is correct and the
    // idle wording must not appear.
    mockedFetchProcessesPayload.mockResolvedValue({
      jobs: [], workers: [], subprocesses: [], worker_processes: [],
    });

    renderPanel();

    await screen.findByRole("table", { name: "Worker subprocesses" });
    expect(screen.getByTestId("proc-worker-process-count")).toHaveTextContent(
      "Worker processes: 0",
    );
    expect(screen.queryByText(/running and idle/)).not.toBeInTheDocument();
  });

  it("keeps worker summary rows without pid out of the kill-process grid", async () => {
    mockedFetchProcessesPayload.mockResolvedValue({
      jobs: [],
      workers: [{ worker_id: "worker-a", status: "running" }],
      subprocesses: [{ pid: 4321, cmdline: "python worker.py", rss_mb: 128 }],
    });

    renderPanel();

    // Read the rendered rows: a worker summary with no pid is not a killable
    // process and must not appear as one.
    const table = await screen.findByRole("table", { name: "Worker subprocesses" });
    await waitFor(() => {
      expect(within(table).getAllByRole("row")).toHaveLength(2);
    });
    expect(within(table).getByText("4321")).toBeInTheDocument();
    expect(within(table).getByText("python worker.py")).toBeInTheDocument();
    expect(within(table).queryByText("worker-a")).not.toBeInTheDocument();
  });

  it("does not expose kill rows when only worker summaries are present", async () => {
    mockedFetchProcessesPayload.mockResolvedValue({
      jobs: [],
      workers: [{ worker_id: "worker-a", status: "running" }],
      subprocesses: [],
    });

    renderPanel();

    // No killable process at all: the empty state stands in, and no bulk bar
    // appears because nothing can be selected.
    expect(await screen.findByTestId("proc-workers-grid")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Worker subprocesses" });
    expect(within(table).getAllByRole("row")).toHaveLength(1);
    expect(screen.queryByRole("region", { name: "Bulk actions" })).not.toBeInTheDocument();
  });

  it("asks before cancelling a job and names it as an irreversible action", async () => {
    mockedFetchProcessesPayload.mockResolvedValue(THREE_JOBS);
    mockedCancelProcessJob.mockResolvedValue({ status: "cancelled" } as never);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Actions for job 12" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Cancel" }));

    expect(mockedCancelProcessJob).not.toHaveBeenCalled();
    const confirmation = screen.getByRole("alertdialog", { name: "Cancel job #12?" });
    expect(confirmation).toHaveTextContent("The run stops where it is, and its progress is lost.");
    expect(within(confirmation).getByText("This cannot be undone.")).toBeInTheDocument();
    fireEvent.click(within(confirmation).getByRole("button", { name: "Cancel job" }));

    await waitFor(() => expect(mockedCancelProcessJob).toHaveBeenCalledWith("12"));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(await screen.findByText("Job #12 cancelled.")).toBeInTheDocument();
  });

  it("keeps the dialog open with the reason when the only job fails to cancel", async () => {
    mockedFetchProcessesPayload.mockResolvedValue(THREE_JOBS);
    mockedCancelProcessJob.mockRejectedValue(new Error("Job already finished."));
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Actions for job 11" }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Cancel" }));
    const confirmation = screen.getByRole("alertdialog", { name: "Cancel job #11?" });
    fireEvent.click(within(confirmation).getByRole("button", { name: "Cancel job" }));

    expect(await within(confirmation).findByRole("alert")).toHaveTextContent("Job already finished.");
    expect(within(confirmation).getByRole("button", { name: "Cancel job" })).toBeEnabled();
  });

  it("reports a partial bulk cancel and keeps the failed job selected for a retry", async () => {
    mockedFetchProcessesPayload.mockResolvedValue(THREE_JOBS);
    mockedCancelProcessJob.mockImplementation(async (id) => {
      if (id === "12") throw new Error("Job already finished.");
      return { status: "cancelled" } as never;
    });
    renderPanel();

    const table = await screen.findByRole("table", { name: "Active jobs" });
    fireEvent.click(await within(table).findByRole("checkbox", { name: "Select visible rows" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel Selected" }));

    const confirmation = screen.getByRole("alertdialog", { name: "Cancel 3 jobs?" });
    expect(within(confirmation).getByRole("list", { name: "Affected objects" })).toHaveTextContent("#12 · Beta");
    fireEvent.click(within(confirmation).getByRole("button", { name: "Cancel 3 jobs" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(await screen.findByText("2 of 3 jobs cancelled.")).toBeInTheDocument();
    expect(screen.getByText(/#12: Job already finished\./)).toBeInTheDocument();
    expect(within(table).getByRole("checkbox", { name: /12/ })).toBeChecked();
    expect(within(table).getByRole("checkbox", { name: /11/ })).not.toBeChecked();
  });
});
