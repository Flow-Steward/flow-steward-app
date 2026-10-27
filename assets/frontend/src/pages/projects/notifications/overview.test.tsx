import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { ProjectNotificationOverview } from "@/pages/projects/notifications/overview";
import { fetchProjectNotificationOverview, type NotificationOverviewEntry } from "@/pages/projects/notifications/overview-api";
import type * as OverviewApiModule from "@/pages/projects/notifications/overview-api";

vi.mock("@/pages/projects/notifications/overview-api", async (importOriginal) => ({
  ...(await importOriginal<typeof OverviewApiModule>()),
  fetchProjectNotificationOverview: vi.fn(),
}));

const mockedFetchOverview = vi.mocked(fetchProjectNotificationOverview);

function entry(overrides: Partial<NotificationOverviewEntry> & Pick<NotificationOverviewEntry, "entry_id" | "name">): NotificationOverviewEntry {
  return {
    kind: "automation",
    source: { scope_kind: "workflow", workflow_id: "wf-orders", workflow_name: "Orders", scope_id: "", scope_name: "" },
    event_type: "job.failed",
    conditions: { severity: "info", workflow_id: "", workflow_name: "", fields: [], debounce_seconds: 0 },
    delivery: { method: "channel", provider_key: "acme.chat", provider_name: "acme.chat", channel_id: "ops", recipients: [], subscriptions: [], decided_at_run_time: false },
    enabled: true,
    state: "enabled",
    problems: [],
    settings: { kind: "automation", workflow_id: "wf-orders", scope_kind: "workflow", scope_id: "", rule_key: overrides.name },
    connection: null,
    ...overrides,
  };
}

const ENTRIES: NotificationOverviewEntry[] = [
  entry({
    entry_id: "automation:wf-orders:failed_to_team",
    name: "failed_to_team",
    delivery: {
      method: "matching_subscriptions", provider_key: "", provider_name: "", channel_id: "", recipients: [],
      subscriptions: [{ route_key: "team_failures", name: "Team failures", found: true }], decided_at_run_time: true,
    },
  }),
  entry({
    entry_id: "automation:wf-orders:scope::group::batch::group_done",
    name: "group_done",
    event_type: "job.group_completed",
    source: { scope_kind: "group", workflow_id: "wf-orders", workflow_name: "Orders", scope_id: "batch", scope_name: "Batch import" },
    settings: { kind: "automation", workflow_id: "wf-orders", scope_kind: "group", scope_id: "batch", rule_key: "group_done" },
    delivery: { method: "channel", provider_key: "acme.chat", provider_name: "acme.chat", channel_id: "ops-room", recipients: [], subscriptions: [], decided_at_run_time: false },
  }),
  entry({
    entry_id: "automation:wf-orders:scope::step::send_mail::step_failed",
    name: "step_failed",
    event_type: "job.step_failed",
    source: { scope_kind: "step", workflow_id: "wf-orders", workflow_name: "Orders", scope_id: "send_mail", scope_name: "Send Email" },
    settings: { kind: "automation", workflow_id: "wf-orders", scope_kind: "step", scope_id: "send_mail", rule_key: "step_failed" },
    delivery: { method: "recipients", provider_key: "acme.mailer", provider_name: "acme.mailer", channel_id: "", recipients: ["owner@example.com"], subscriptions: [], decided_at_run_time: false },
    enabled: false,
    state: "disabled",
  }),
  entry({
    entry_id: "subscription:team_failures",
    name: "Team failures",
    kind: "subscription",
    source: { scope_kind: "project", workflow_id: "", workflow_name: "", scope_id: "", scope_name: "" },
    conditions: { severity: "error", workflow_id: "wf-orders", workflow_name: "Orders", fields: [], debounce_seconds: 0 },
    settings: { kind: "subscription", route_key: "team_failures" },
    delivery: { method: "channel", provider_key: "acme.chat", provider_name: "Acme Chat", channel_id: "team", recipients: [], subscriptions: [], decided_at_run_time: false },
    state: "setup_problem",
    problems: ["extension_disabled"],
    connection: { extension_id: "acme.chat" },
  }),
];

function renderOverview(role: "admin" | "viewer" = "admin") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const auth = { session: { authenticated: true, roles: [role], user: { roles: [role] } } } as unknown as AuthContextValue;
  return render(
    <AuthContext.Provider value={auth}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ProjectNotificationOverview projectId="project-1" />
        </MemoryRouter>
      </QueryClientProvider>
    </AuthContext.Provider>,
  );
}

function rowOf(name: string): HTMLElement {
  const row = screen.getAllByTestId("notification-overview-entry").find((item) => within(item).queryByText(name, { selector: "p" }));
  if (!row) throw new Error(`no entry ${name}`);
  return row;
}

describe("ProjectNotificationOverview", () => {
  beforeEach(() => {
    mockedFetchOverview.mockReset();
  });

  it("is titled Automatic notifications and explains what it shows", async () => {
    mockedFetchOverview.mockResolvedValue([]);
    renderOverview();

    expect(screen.getByText("Automatic notifications")).toBeInTheDocument();
    expect(screen.getByText(/Messages sent by individual steps are configured inside the workflow and are not shown here\./)).toBeInTheDocument();
    expect(await screen.findByText(/No automatic notifications are set up for this project\./)).toBeInTheDocument();
  });

  it("lists automation of all three scopes and the subscriptions, each with its source, event and destination", async () => {
    mockedFetchOverview.mockResolvedValue(ENTRIES);
    renderOverview();

    await screen.findByText("failed_to_team");
    expect(screen.getAllByTestId("notification-overview-entry")).toHaveLength(4);
    expect(within(rowOf("failed_to_team")).getByText("Automation of workflow Orders")).toBeInTheDocument();
    expect(within(rowOf("failed_to_team")).getByText(/Matching project event subscriptions, now: Team failures/)).toHaveTextContent("decided at run time");
    expect(within(rowOf("group_done")).getByText("Automation of group Batch import in workflow Orders")).toBeInTheDocument();
    expect(within(rowOf("group_done")).getByText("job.group_completed")).toBeInTheDocument();
    expect(within(rowOf("group_done")).getByText("acme.chat channel ops-room")).toBeInTheDocument();
    expect(within(rowOf("step_failed")).getByText("Automation of step Send Email in workflow Orders")).toBeInTheDocument();
    expect(within(rowOf("step_failed")).getByText("acme.mailer to owner@example.com")).toBeInTheDocument();
    expect(within(rowOf("Team failures")).getByText("Project event subscription")).toBeInTheDocument();
    expect(within(rowOf("Team failures")).getByText("severity error; only workflow Orders")).toBeInTheDocument();
  });

  it("links each entry to the exact place it is configured", async () => {
    mockedFetchOverview.mockResolvedValue(ENTRIES);
    renderOverview();

    await screen.findByText("failed_to_team");
    expect(screen.getByRole("link", { name: "Open settings of failed_to_team" })).toHaveAttribute(
      "href",
      "/registry/workflows/wf-orders/builder?project_id=project-1&drawer=automation&rule=failed_to_team",
    );
    expect(screen.getByRole("link", { name: "Open settings of group_done" })).toHaveAttribute(
      "href",
      "/registry/workflows/wf-orders/builder?project_id=project-1&drawer=group&group_id=batch&group_tab=automation&rule=group_done",
    );
    expect(screen.getByRole("link", { name: "Open settings of step_failed" })).toHaveAttribute(
      "href",
      "/registry/workflows/wf-orders/builder?project_id=project-1&drawer=step&step_id=send_mail&step_tab=automation&rule=step_failed",
    );
    // A subscription has no editor: it is a read-only row, with no settings link.
    expect(screen.queryByRole("link", { name: "Open settings of Team failures" })).not.toBeInTheDocument();
    expect(within(rowOf("Team failures")).queryByRole("link", { name: /Open settings/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Manage project event subscriptions/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole("link").filter((link) => link.getAttribute("href")?.includes("notification-subscriptions"))).toHaveLength(0);
  });

  it("names disabled rules and setup problems, and links a connection problem to the extension", async () => {
    mockedFetchOverview.mockResolvedValue(ENTRIES);
    renderOverview();

    await screen.findByText("failed_to_team");
    expect(within(rowOf("failed_to_team")).getByText("On")).toBeInTheDocument();
    expect(within(rowOf("step_failed")).getByText("Off")).toBeInTheDocument();
    const broken = rowOf("Team failures");
    expect(within(broken).getByText("Setup problem")).toBeInTheDocument();
    expect(within(broken).getByText("The extension acme.chat is disabled.")).toBeInTheDocument();
    expect(within(broken).getByRole("link", { name: "Open connection settings of Team failures" })).toHaveAttribute(
      "href",
      "/extensions/acme.chat?project_id=project-1",
    );
    expect(within(rowOf("group_done")).queryByRole("link", { name: /Open connection settings/ })).not.toBeInTheDocument();
  });

  it("offers no way to create, edit, switch, delete or save anything here", async () => {
    mockedFetchOverview.mockResolvedValue(ENTRIES);
    renderOverview();

    await screen.findByText("failed_to_team");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows loading without claiming there is nothing", async () => {
    let resolve!: (rows: NotificationOverviewEntry[]) => void;
    mockedFetchOverview.mockReturnValue(new Promise((done) => { resolve = done; }));
    renderOverview();

    expect(screen.getByText("Loading automatic notifications…")).toBeInTheDocument();
    expect(screen.queryByText(/No automatic notifications are set up/)).not.toBeInTheDocument();
    resolve([]);
    expect(await screen.findByText(/No automatic notifications are set up/)).toBeInTheDocument();
    expect(screen.queryByText("Loading automatic notifications…")).not.toBeInTheDocument();
  });

  it("shows a failed load as an error, not as an empty list, and retries", async () => {
    mockedFetchOverview.mockRejectedValueOnce(new Error("Notification overview lookup failed"));
    renderOverview();

    expect(await screen.findByText("Automatic notifications could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("Notification overview lookup failed")).toBeInTheDocument();
    expect(screen.queryByText(/No automatic notifications are set up/)).not.toBeInTheDocument();

    mockedFetchOverview.mockResolvedValueOnce(ENTRIES);
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getAllByTestId("notification-overview-entry")).toHaveLength(4));
    expect(screen.queryByText("Automatic notifications could not be loaded.")).not.toBeInTheDocument();
  });
});

it("gives a project reader no Open settings link: the settings live in the admin-only builder", async () => {
  mockedFetchOverview.mockResolvedValue(ENTRIES);
  renderOverview("viewer");

  await screen.findAllByTestId("notification-overview-entry");
  expect(screen.queryByRole("link", { name: /Open settings of/ })).not.toBeInTheDocument();
});
