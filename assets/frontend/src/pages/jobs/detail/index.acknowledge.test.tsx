import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { jobPayload, mockedFetchJobDetail, mockedUseAuth, primeApi, renderPage } from "./index.test-helpers";
import { acknowledgeJob, unacknowledgeJob } from "@/pages/jobs/detail/api";
import { DashboardPage } from "@/pages/dashboard";
import { fetchDashboardSummary } from "@/pages/dashboard/api";

vi.mock("@/pages/dashboard/api", () => ({ fetchDashboardSummary: vi.fn() }));
vi.mock("@/lib/auth/capabilities", () => ({ useHasCapability: () => true }));

const mockedAcknowledgeJob = vi.mocked(acknowledgeJob);
const mockedUnacknowledgeJob = vi.mocked(unacknowledgeJob);

function terminalJob(status: string, overrides: Record<string, unknown> = {}) {
  return jobPayload({
    status,
    finished_at: "2026-04-25T10:05:00Z",
    lifecycle: { status: { code: status, label: "", terminal: true } },
    ...overrides,
  });
}

describe("JobsDetailPage acknowledge", () => {
  beforeEach(() => {
    mockedAcknowledgeJob.mockReset().mockResolvedValue({ status: "acknowledged", job_id: "42" });
    mockedUnacknowledgeJob.mockReset().mockResolvedValue({ status: "unacknowledged", job_id: "42" });
  });

  it.each(["failed", "max_iterations", "stuck", "orphaned"])("offers Acknowledge on a %s run and calls the acknowledge route", async (status) => {
    primeApi(terminalJob(status));
    renderPage();

    const button = await screen.findByTestId("job-acknowledge-button");
    expect(button).toHaveTextContent("Acknowledge");
    fireEvent.click(button);
    await waitFor(() => expect(mockedAcknowledgeJob).toHaveBeenCalledWith("42"));
    expect(mockedUnacknowledgeJob).not.toHaveBeenCalled();
  });

  it("offers Unacknowledge on an acknowledged run, shows the badge and calls the unacknowledge route", async () => {
    primeApi(terminalJob("failed", { job: { id: 42, status: "failed", acknowledged_at: "2026-04-25T11:00:00Z" } }));
    renderPage();

    expect(await screen.findByTestId("job-acknowledged-badge")).toHaveTextContent("Acknowledged");
    const button = screen.getByTestId("job-acknowledge-button");
    expect(button).toHaveTextContent("Unacknowledge");
    fireEvent.click(button);
    await waitFor(() => expect(mockedUnacknowledgeJob).toHaveBeenCalledWith("42"));
    expect(mockedAcknowledgeJob).not.toHaveBeenCalled();
  });

  it.each([false, true])("keeps a failed acknowledgment change visible and retryable (already acknowledged: %s)", async (acknowledged) => {
    primeApi(terminalJob("failed", {
      job: { id: 42, status: "failed", acknowledged_at: acknowledged ? "2026-04-25T11:00:00Z" : null },
    }));
    const mutation = acknowledged ? mockedUnacknowledgeJob : mockedAcknowledgeJob;
    mutation.mockRejectedValue(new Error("The acknowledgment could not be saved"));
    renderPage();

    fireEvent.click(await screen.findByTestId("job-acknowledge-button"));
    expect(await screen.findByText("The acknowledgment could not be saved")).toBeVisible();
    expect(screen.getByTestId("job-acknowledge-button")).toBeEnabled();
    expect(screen.getByTestId("job-acknowledge-button")).toHaveTextContent(acknowledged ? "Unacknowledge" : "Acknowledge");
    expect(Boolean(screen.queryByTestId("job-acknowledged-badge"))).toBe(acknowledged);
  });

  it.each([false, true])("does not offer acknowledgment mutations to a viewer (already acknowledged: %s)", async (acknowledged) => {
    const auth = mockedUseAuth.getMockImplementation()?.();
    mockedUseAuth.mockReturnValue({
      ...auth,
      session: { authenticated: true, roles: ["viewer"], user: { roles: ["viewer"] } },
    } as never);
    primeApi(terminalJob("failed", {
      job: { id: 42, status: "failed", acknowledged_at: acknowledged ? "2026-04-25T11:00:00Z" : null },
    }));
    renderPage();

    await screen.findByTestId("job-run-overview");
    expect(screen.queryByTestId("job-acknowledge-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("job-restart-button")).not.toBeInTheDocument();
    expect(Boolean(screen.queryByTestId("job-acknowledged-badge"))).toBe(acknowledged);
  });

  it.each([false, true])("refreshes the dashboard and detail after acknowledgment changes (already acknowledged: %s)", async (initiallyAcknowledged) => {
    let acknowledged = initiallyAcknowledged;
    primeApi(terminalJob("failed"));
    mockedFetchJobDetail.mockImplementation(async () => terminalJob("failed", {
      job: { id: 42, status: "failed", acknowledged_at: acknowledged ? "2026-04-25T11:00:00Z" : null },
    }) as never);
    const mutation = initiallyAcknowledged ? mockedUnacknowledgeJob : mockedAcknowledgeJob;
    mutation.mockImplementation(async () => {
      acknowledged = !initiallyAcknowledged;
      return { status: acknowledged ? "acknowledged" : "unacknowledged", job_id: "42" };
    });
    vi.mocked(fetchDashboardSummary).mockImplementation(async () => ({
      hero_kpis: { needs_attention: { value: acknowledged ? 0 : 1 } },
      groups: { needs_attention: acknowledged ? [] : [{ id: "42", status: "failed" }] },
    }));
    const view = renderPage();
    render(
      <QueryClientProvider client={view.queryClient}>
        <MemoryRouter><DashboardPage /></MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByRole("link", { name: `Failed / Stuck: ${initiallyAcknowledged ? 0 : 1}. Show failed and stuck jobs` });

    fireEvent.click(await screen.findByTestId("job-acknowledge-button"));

    expect(await screen.findByRole("link", { name: `Failed / Stuck: ${initiallyAcknowledged ? 1 : 0}. Show failed and stuck jobs` })).toBeInTheDocument();
    await waitFor(() => expect(Boolean(screen.queryByTestId("job-acknowledged-badge"))).toBe(!initiallyAcknowledged));
  });

  it.each(["completed", "cancelled", "awaiting_approval", "retryable_interrupted_step", "in_progress"])("offers no Acknowledge on a %s run", async (status) => {
    primeApi(terminalJob(status));
    renderPage();

    await waitFor(() => expect(mockedFetchJobDetail).toHaveBeenCalled());
    expect(await screen.findByTestId("job-detail-page")).toBeInTheDocument();
    expect(screen.queryByTestId("job-acknowledge-button")).not.toBeInTheDocument();
  });

  it("leads with the status group and keeps the detailed label as secondary text", async () => {
    primeApi(terminalJob("max_iterations", { lifecycle: { status: { code: "max_iterations", label: "Stopped after repeated retries", terminal: true } } }));
    renderPage();

    expect(await screen.findByText("Status: Failed · Stopped after repeated retries")).toBeInTheDocument();
  });

  it("spells out a code outside every group, raw", async () => {
    primeApi(terminalJob("legacy_paused", { lifecycle: { status: { code: "legacy_paused", label: "Legacy Paused", terminal: true } } }));
    renderPage();

    expect(await screen.findByText("Status: legacy_paused")).toBeInTheDocument();
  });
});
