import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { addDays, todayIn } from "@/lib/time/calendar-days";
import { browserTimezone } from "@/lib/time/user-timezone";
import { EventsAuditTrail } from "@/pages/monitoring/shared/events";
import { fetchEventDetail, fetchEventsList, redriveEvent } from "@/pages/monitoring/shared/events/api";

vi.mock("@/pages/monitoring/shared/events/api", () => ({
  fetchEventDetail: vi.fn(),
  fetchEventsList: vi.fn(),
  redriveEvent: vi.fn(),
}));

const mockedFetchEventsList = vi.mocked(fetchEventsList);
const mockedFetchEventDetail = vi.mocked(fetchEventDetail);
const mockedRedriveEvent = vi.mocked(redriveEvent);

type User = ReturnType<typeof userEvent.setup>;

/** The facet filters are DsSelect: open the combobox, then pick the option. */
async function choose(user: User, combobox: RegExp, option: string | RegExp) {
  await user.click(await screen.findByRole("combobox", { name: combobox }));
  await user.click(await screen.findByRole("option", { name: option }));
}

function renderPage(locale: "en" | "lt" = "en") {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
    },
  });
  const messages: Record<string, string> = locale === "lt"
    ? {
      "react.monitoring.workflow_not_recorded": "Darbo eiga neužfiksuota",
      "react.monitoring.event_detail_event": "Įvykis",
      "events.columns.status": "Būsena",
      "events.columns.severity": "Svarba",
      "events.columns.project": "Projektas",
      "events.columns.job": "Užduotis",
      "events.columns.agent": "Agentas",
      "registry.workflow_detail.step.drawer_title": "Žingsnis",
      "events.columns.group": "Grupė",
      "react.monitoring.event_detail_recorded": "Užfiksuota",
      "react.monitoring.entries_count": "Įrašų: {count}",
    }
    : {};
  return render(
    <I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, fallback = "", params) => interpolate(messages[key] || fallback || key, params) }}>
      <QueryClientProvider client={queryClient}>
        <EventsAuditTrail />
      </QueryClientProvider>
    </I18nContext.Provider>,
  );
}

describe("EventsAuditTrail", () => {
  it("localizes both diagnostic record counts while preserving backend command data", async () => {
    mockedFetchEventDetail.mockResolvedValue({
      event_id: "evt-1", event_type: "workflow.step.completed", event_type_label: "Workflow Step Completed", status: "succeeded", workflow_id: "weekly_report",
      dispatches: [{ id: "dispatch-1", status: "delivered" }],
      commands: [{ id: "command-1", command_type: "fixture.command", command_type_label: "Fixture command" }, { id: "command-2", command_type: "fixture.command", command_type_label: "Fixture command" }],
    });
    renderPage("lt");
    await userEvent.setup().click(await screen.findByRole("button", { name: /workflow step completed/i }));
    const diagnostics = await screen.findByTestId("event-advanced-diagnostics");
    await userEvent.setup().click(within(diagnostics).getByText(/advanced event diagnostics/i));
    expect(within(diagnostics).getByText("Įrašų: 1")).toBeInTheDocument();
    expect(within(diagnostics).getByText("Įrašų: 2")).toBeInTheDocument();
    expect(within(diagnostics).queryByText(/\d+ entries/)).not.toBeInTheDocument();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mockedRedriveEvent.mockResolvedValue({ ok: true });
    mockedFetchEventsList.mockResolvedValue({
      events: [
        {
          event_id: "evt-1",
          event_type: "workflow.step.completed",
          event_type_label: "Workflow Step Completed",
          status: "succeeded",
          workflow_id: "weekly_report",
          project_name: "Demo",
          created_at: "2026-04-25T10:00:00Z",
        },
        {
          event_id: "evt-2",
          event_type: "workflow.step.failed",
          event_type_label: "Workflow Step Failed",
          status: "failed",
          workflow_id: "weekly_report",
          project_name: "Demo",
          created_at: "2026-04-25T11:00:00Z",
        },
      ],
      total: 2,
      page: 1,
      per_page: 25,
      facets: {
        workflows: [{ value: "weekly_report", label: "Weekly Report", count: 2 }],
        event_families: [{ value: "workflow", label: "Workflow", count: 2 }],
        event_categories: [{ value: "step", label: "Step", count: 2 }],
        event_types: [
          { value: "workflow.step.completed", label: "Workflow Step Completed", count: 1 },
          { value: "workflow.step.failed", label: "Workflow Step Failed", count: 1 },
        ],
        statuses: [
          { value: "succeeded", label: "Succeeded", count: 1 },
          { value: "failed", label: "Failed", count: 1 },
        ],
      },
    });
    mockedFetchEventDetail.mockImplementation(async (eventId: string) => {
      if (eventId === "evt-2") {
        return {
          event_id: "evt-2",
          event_type: "workflow.step.failed",
          event_type_label: "Workflow Step Failed",
          status: "failed",
          workflow_id: "weekly_report",
          workflow_display_name: "Weekly Report",
          project_name: "Demo",
          created_at: "2026-04-25T11:00:00Z",
          metadata: {
            summary: "Step failed and needs review.",
          },
        };
      }
      return {
        event_id: "evt-1",
        event_type: "workflow.step.completed",
        event_type_label: "Workflow Step Completed",
        status: "succeeded",
        workflow_id: "weekly_report",
        workflow_display_name: "Weekly Report",
        project_name: "Demo",
        created_at: "2026-04-25T10:00:00Z",
        metadata: {
          summary: "Step completed cleanly.",
        },
      };
    });
  });

  it("renders workflow links in event rows and detail", async () => {
    const user = userEvent.setup();

    renderPage();

    expect((await screen.findAllByText("Workflow: weekly_report")).length).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: /workflow step completed/i }));

    await waitFor(() => {
      expect(mockedFetchEventDetail).toHaveBeenCalledWith("evt-1");
      expect(screen.getAllByTestId("event-workflow-link")).toHaveLength(3);
    });
    expect(screen.getByTestId("event-detail-inspector")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("event-detail-inspector")).getByRole("heading", {
        name: "Workflow Step Completed",
      }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("event-advanced-diagnostics")).not.toHaveAttribute("open");
    expect(screen.getAllByTestId("event-workflow-link")[2]).toHaveAttribute(
      "href",
      "/registry/workflows/weekly_report",
    );
    expect(screen.getAllByTestId("event-workflow-link")[2]).toHaveTextContent("Weekly Report");
  });

  it("shows raw workflow ids when backend workflow labels are missing", async () => {
    const user = userEvent.setup();
    mockedFetchEventsList.mockResolvedValue({
      events: [
        {
          event_id: "evt-workflow-raw",
          event_type: "workflow.step.completed",
          event_type_label: "Workflow Step Completed",
          status: "succeeded",
          workflow_id: "weekly_report",
          project_name: "Demo",
          created_at: "2026-04-25T10:00:00Z",
        },
      ],
      total: 1,
      page: 1,
      per_page: 25,
      facets: {
        workflows: [{ value: "weekly_report", count: 1 }],
      },
    });
    mockedFetchEventDetail.mockResolvedValue({
      event_id: "evt-workflow-raw",
      event_type: "workflow.step.completed",
      event_type_label: "Workflow Step Completed",
      status: "succeeded",
      workflow_id: "weekly_report",
      project_name: "Demo",
    });

    renderPage();

    expect(await screen.findByText("Workflow: weekly_report")).toBeInTheDocument();
    expect(screen.queryByText("Workflow: Weekly Report")).not.toBeInTheDocument();

    await choose(user, /workflow filter/i, "weekly_report (1)");
    expect(screen.getByTestId("events-active-filters")).toHaveTextContent("Workflow: weekly_report");

    await user.click(await screen.findByRole("button", { name: /workflow step completed/i }));

    await waitFor(() => {
      expect(mockedFetchEventDetail).toHaveBeenCalledWith("evt-workflow-raw");
      expect(screen.getAllByTestId("event-workflow-link")).toHaveLength(2);
    });

    expect(screen.getAllByTestId("event-workflow-link")[1]).toHaveTextContent("weekly_report");
    expect(screen.queryByText("Workflow: Weekly Report")).not.toBeInTheDocument();
  });

  it.each([
    { locale: "en" as const, expected: "Workflow not recorded" },
    { locale: "lt" as const, expected: "Darbo eiga neužfiksuota" },
  ])("shows the missing-workflow detail fallback in $locale without creating a workflow link", async ({ locale, expected }) => {
    const user = userEvent.setup();
    mockedFetchEventsList.mockResolvedValueOnce({
      events: [
        {
          event_id: "evt-2",
          event_type: "runtime.message",
          status: "succeeded",
          project_name: "Demo",
        },
      ],
      total: 1,
      page: 1,
      per_page: 25,
      facets: {},
    });
    mockedFetchEventDetail.mockResolvedValueOnce({
      event_id: "evt-2",
      event_type: "runtime.message",
      status: "succeeded",
      project_name: "Demo",
    });

    renderPage(locale);

    await user.click(await screen.findByRole("button", { name: /runtime\.message/i }));

    await waitFor(() => {
      expect(mockedFetchEventDetail).toHaveBeenCalledWith("evt-2");
      const inspector = within(screen.getByTestId("event-detail-inspector"));
      const factLabels = locale === "lt"
        ? ["Įvykis", "Būsena", "Svarba", "Projektas", "Užduotis", "Agentas", "Žingsnis", "Grupė", "Užfiksuota"]
        : ["Event", "Status", "Severity", "Project", "Job", "Agent", "Step", "Group", "Recorded"];
      for (const label of factLabels) expect(inspector.getByText(label)).toBeInTheDocument();
      expect(inspector.getByText(expected)).toBeInTheDocument();
      expect(inspector.queryByTestId("event-workflow-link")).not.toBeInTheDocument();
    });
  });

  it("renders cascading filter selects and refetches with selected workflow hierarchy", async () => {
    const user = userEvent.setup();
    renderPage();

    const [initialCall] = mockedFetchEventsList.mock.calls;
    expect(initialCall?.[0]).toMatchObject({
      workflowId: "",
      eventFamily: "",
      eventCategory: "",
      eventType: "",
      status: "",
    });

    await choose(user, /workflow filter/i, /Weekly Report/i);
    await choose(user, /event family filter/i, "Workflow (2)");
    await choose(user, /event category filter/i, "Step (2)");
    await choose(user, /exact event type filter/i, "Workflow Step Completed (1)");
    await choose(user, /dispatch status filter/i, "Succeeded (1)");

    await waitFor(() => {
      const lastCall = mockedFetchEventsList.mock.calls.at(-1)?.[0];
      expect(lastCall).toMatchObject({
        workflowId: "weekly_report",
        eventFamily: "workflow",
        eventCategory: "step",
        eventType: "workflow.step.completed",
        status: "succeeded",
      });
    });

    expect(screen.getByTestId("events-active-filters")).toHaveTextContent(
      "Workflow: Weekly Report",
    );
    const activeFilters = screen.getByTestId("events-active-filters");
    expect(
      within(activeFilters).getByRole("link", { name: /workflow: weekly report/i }),
    ).toHaveAttribute("href", "/registry/workflows/weekly_report");
    expect(activeFilters).toHaveTextContent("Family: Workflow");
    expect(activeFilters).toHaveTextContent("Subtype: Step");
    expect(activeFilters).toHaveTextContent("Type: Workflow Step Completed");
    expect(activeFilters).toHaveTextContent("Status: Succeeded");

    await user.click(
      screen.getByRole("button", { name: /remove subtype: step filter/i }),
    );

    await waitFor(() => {
      const lastCall = mockedFetchEventsList.mock.calls.at(-1)?.[0];
      expect(lastCall).toMatchObject({
        workflowId: "weekly_report",
        eventFamily: "workflow",
        eventCategory: "",
        eventType: "",
        status: "",
      });
    });

    expect(activeFilters).not.toHaveTextContent("Subtype: Step");
    expect(activeFilters).not.toHaveTextContent("Type: Workflow Step Completed");
    expect(activeFilters).not.toHaveTextContent("Status: Succeeded");
  });

  it("filters by a period preset expanded from today in the user's zone, with a chip to remove it", async () => {
    const user = userEvent.setup();
    renderPage();
    const today = todayIn(browserTimezone());

    await waitFor(() => expect(mockedFetchEventsList).toHaveBeenCalled());
    expect(mockedFetchEventsList.mock.calls[0][0]).toMatchObject({ fromDate: "", toDate: "" });
    // All events is the filter switched off, so it has no chip.
    expect(screen.queryByTestId("events-active-filters")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Event period: All events" }));
    await user.click(screen.getByRole("radio", { name: "Last 7 days" }));

    await waitFor(() => {
      expect(mockedFetchEventsList.mock.calls.at(-1)?.[0]).toMatchObject({ fromDate: addDays(today, -6), toDate: today });
    });
    const activeFilters = screen.getByTestId("events-active-filters");
    expect(activeFilters).toHaveTextContent("Last 7 days");

    await user.click(within(activeFilters).getByRole("button", { name: "Remove Last 7 days filter" }));
    await waitFor(() => {
      expect(mockedFetchEventsList.mock.calls.at(-1)?.[0]).toMatchObject({ fromDate: "", toDate: "" });
    });
    expect(screen.getByRole("button", { name: "Event period: All events" })).toBeInTheDocument();
  });

  it("renders backend-enriched command labels in advanced event diagnostics", async () => {
    const user = userEvent.setup();
    mockedFetchEventDetail.mockResolvedValue({
      event_id: "evt-1",
      event_type: "workflow.step.completed",
      event_type_label: "Workflow Step Completed",
      status: "succeeded",
      workflow_id: "weekly_report",
      workflow_display_name: "Weekly Report",
      commands: [
        {
          command_id: "cmd-1",
          command_type: "resolve_approval",
          command_type_label: "Resolve Approval",
          status: "accepted",
          status_label: "Accepted",
        },
      ],
    });

    renderPage();

    await user.click(await screen.findByRole("button", { name: /workflow step completed/i }));

    await waitFor(() => {
      expect(mockedFetchEventDetail).toHaveBeenCalledWith("evt-1");
      expect(
        within(screen.getByTestId("event-detail-inspector")).getByRole("heading", {
          name: "Workflow Step Completed",
        }),
      ).toBeInTheDocument();
    });

    const diagnostics = await screen.findByTestId("event-advanced-diagnostics");
    await user.click(within(diagnostics).getByText(/advanced event diagnostics/i));

    expect(within(diagnostics).getByText("Resolve Approval")).toBeInTheDocument();
    expect(within(diagnostics).getByText("Accepted")).toBeInTheDocument();
  });

  it("shows raw backend codes in filters when facet labels are missing", async () => {
    const user = userEvent.setup();
    mockedFetchEventsList.mockResolvedValue({
      events: [
        {
          event_id: "evt-2",
          event_type: "runtime.message",
          status: "dead_lettered",
          project_name: "Demo",
        },
      ],
      total: 1,
      page: 1,
      per_page: 25,
      facets: {
        event_families: [{ value: "runtime", count: 1 }],
        event_categories: [{ value: "message", count: 1 }],
        event_types: [{ value: "runtime.message", count: 1 }],
        statuses: [{ value: "dead_lettered", count: 1 }],
      },
    });

    renderPage();

    await choose(user, /event family filter/i, "runtime (1)");
    await choose(user, /event category filter/i, "message (1)");
    await choose(user, /exact event type filter/i, "runtime.message (1)");
    await choose(user, /dispatch status filter/i, "dead_lettered (1)");

    const activeFilters = screen.getByTestId("events-active-filters");
    expect(activeFilters).toHaveTextContent("Family: runtime");
    expect(activeFilters).toHaveTextContent("Subtype: message");
    expect(activeFilters).toHaveTextContent("Type: runtime.message");
    expect(activeFilters).toHaveTextContent("Status: dead_lettered");
    expect(activeFilters).not.toHaveTextContent("Runtime Message");
    expect(activeFilters).not.toHaveTextContent("Dead Lettered");
  });

  it("shows raw backend codes in advanced diagnostics when labels are missing", async () => {
    const user = userEvent.setup();
    mockedFetchEventsList.mockResolvedValueOnce({
      events: [
        {
          event_id: "evt-3",
          event_type: "workflow.step.completed",
          status: "dead_lettered",
          workflow_id: "weekly_report",
          project_name: "Demo",
          created_at: "2026-04-25T10:00:00Z",
        },
      ],
      total: 1,
      page: 1,
      per_page: 25,
      facets: {},
    });
    mockedFetchEventDetail.mockResolvedValue({
      event_id: "evt-3",
      event_type: "workflow.step.completed",
      status: "dead_lettered",
      workflow_id: "weekly_report",
      workflow_display_name: "Weekly Report",
      dispatches: [
        {
          dispatch_id: "dispatch-1",
          status: "dead_lettered",
        },
      ],
      commands: [
        {
          command_id: "cmd-2",
          command_type: "resolve_approval",
          status: "waiting_for_review",
        },
      ],
    });

    renderPage();

    await user.click(await screen.findByRole("button", { name: /workflow\.step\.completed/i }));

    await waitFor(() => {
      expect(mockedFetchEventDetail).toHaveBeenCalledWith("evt-3");
      expect(
        within(screen.getByTestId("event-detail-inspector")).getByRole("heading", {
          name: "workflow.step.completed",
        }),
      ).toBeInTheDocument();
    });

    const diagnostics = await screen.findByTestId("event-advanced-diagnostics");
    await user.click(within(diagnostics).getByText(/advanced event diagnostics/i));

    expect(within(diagnostics).getByText("dead_lettered")).toBeInTheDocument();
    expect(within(diagnostics).getByText("resolve_approval")).toBeInTheDocument();
    expect(within(diagnostics).getByText("waiting_for_review")).toBeInTheDocument();
    expect(within(diagnostics).queryByText("Dead Lettered")).not.toBeInTheDocument();
    expect(within(diagnostics).queryByText("Resolve Approval")).not.toBeInTheDocument();
    expect(within(diagnostics).queryByText("Waiting For Review")).not.toBeInTheDocument();
  });

  it("prefers canonical command type labels and otherwise shows raw command codes before retired labels", async () => {
    const user = userEvent.setup();
    mockedFetchEventsList.mockResolvedValueOnce({
      events: [
        {
          event_id: "evt-4",
          event_type: "workflow.step.completed",
          event_type_label: "Workflow Step Completed",
          status: "succeeded",
          project_name: "Demo",
        },
      ],
      total: 1,
      page: 1,
      per_page: 25,
      facets: {},
    });
    mockedFetchEventDetail.mockResolvedValueOnce({
      event_id: "evt-4",
      event_type: "workflow.step.completed",
      event_type_label: "Workflow Step Completed",
      status: "succeeded",
      commands: [
        {
          command_id: "cmd-retired",
          command_type: "resolve_approval",
          command_label: "Retired Friendly Label",
          status: "accepted",
          status_label: "Accepted",
        },
      ],
    });

    renderPage();

    await user.click(await screen.findByRole("button", { name: /workflow step completed/i }));

    await waitFor(() => {
      expect(mockedFetchEventDetail).toHaveBeenCalledWith("evt-4");
    });

    const diagnostics = await screen.findByTestId("event-advanced-diagnostics");
    await user.click(within(diagnostics).getByText(/advanced event diagnostics/i));

    expect(within(diagnostics).getByText("resolve_approval")).toBeInTheDocument();
    expect(within(diagnostics).queryByText("Retired Friendly Label")).not.toBeInTheDocument();
  });

  it("opens event details in a drawer and closes it without leaving the list layout", async () => {
    const user = userEvent.setup();

    renderPage();

    expect(screen.queryByTestId("event-detail-inspector")).not.toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: /workflow step completed/i }));

    const drawer = await screen.findByTestId("event-detail-inspector");
    expect(drawer).toHaveAttribute("open");
    expect(
      within(drawer).getByRole("heading", {
        name: "Workflow Step Completed",
      }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close event detail" }));

    await waitFor(() => {
      expect(screen.queryByTestId("event-detail-inspector")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("heading", { name: "Events list" })).toBeInTheDocument();
  });

  it("switches between events without closing the drawer first", async () => {
    const user = userEvent.setup();

    renderPage();

    await user.click(await screen.findByRole("button", { name: /workflow step completed/i }));

    const drawer = await screen.findByTestId("event-detail-inspector");
    expect(
      within(drawer).getByRole("heading", {
        name: "Workflow Step Completed",
      }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /workflow step failed/i }));

    await waitFor(() => {
      expect(mockedFetchEventDetail).toHaveBeenCalledWith("evt-2");
      expect(
        within(screen.getByTestId("event-detail-inspector")).getByRole("heading", {
          name: "Workflow Step Failed",
        }),
      ).toBeInTheDocument();
    });
  });
});
