import { useState } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

import { WorkflowSettingsSmartSetup } from "./detail-workflow-settings-smart-setup";
import { WorkflowSchedules } from "./detail-workflow-schedules";
import { fetchWorkflowContract } from "@/pages/projects/detail/api";
import { fetchProjectWorkflowBindings } from "@/pages/projects/workflows/api";
import { fetchProjectScheduledJobs, createProjectScheduledJob, deleteProjectScheduledJob, toggleProjectScheduledJob, updateProjectScheduledJob, previewProjectCron } from "@/pages/projects/scheduled-jobs/api";

vi.mock("@/pages/projects/detail/api", () => ({ fetchWorkflowContract: vi.fn() }));
vi.mock("@/pages/projects/workflows/api", () => ({ fetchProjectWorkflowBindings: vi.fn() }));
vi.mock("@/pages/projects/scheduled-jobs/api", () => ({
  fetchProjectScheduledJobs: vi.fn(), createProjectScheduledJob: vi.fn(),
  updateProjectScheduledJob: vi.fn(), previewProjectCron: vi.fn(),
  deleteProjectScheduledJob: vi.fn(), toggleProjectScheduledJob: vi.fn(),
}));

const EDIT_SCHEDULE = /^Edit schedule: /;

const saved = vi.fn();

it("creates a workflow schedule without a separate task summary", async () => {
  const user = userEvent.setup();
  vi.mocked(createProjectScheduledJob).mockResolvedValue({ scheduled_job_id: "new" });
  renderSettings();
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  expect(screen.queryByLabelText("Task summary")).not.toBeInTheDocument();
  await user.type(screen.getByLabelText(/^region/), "eu");
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(createProjectScheduledJob).toHaveBeenCalledOnce());
  const input = vi.mocked(createProjectScheduledJob).mock.calls[0][1];
  expect(input).toMatchObject({ workflow_id: "wf-1", inputs: { region: "eu" }, enabled: false });
  expect(input).not.toHaveProperty("task_summary");
});

function mockObjectContract() {
  vi.mocked(fetchWorkflowContract).mockResolvedValue({ workflow_id: "wf-1", enabled: true,
    interface: { enabled: true, trigger_channels: ["schedule"], expected_inputs: [{ name: "payload", type: "object", required: true }], output_mode: "none" }, definition_issues: [],
  } as never);
}

it.each(["create", "update"])("blocks %s when object draft keys collide instead of submitting old serialized inputs", async (operation) => {
  mockObjectContract();
  const row = { id: "object-schedule", workflow_id: "wf-1", enabled: false,
    cron_expr: "0 9 * * *", schedule_timezone: "UTC", inputs: { payload: { first: "keep", second: "new" } } };
  vi.mocked(fetchProjectScheduledJobs).mockResolvedValue({ project_id: "project-1", timezone_context: {}, jobs: operation === "update" ? [row] : [], runs: [] });
  const user = userEvent.setup();
  renderSettings();
  await user.click(await screen.findByRole("button", { name: operation === "update" ? EDIT_SCHEDULE : "New schedule" }));
  if (operation === "create") {
    await user.type(screen.getByLabelText("payload key 1"), "first");
    await user.type(screen.getByLabelText("payload value 1"), "keep");
    await user.click(screen.getByRole("button", { name: "Add row" }));
    await user.type(screen.getByLabelText("payload key 2"), "second");
    await user.type(screen.getByLabelText("payload value 2"), "new");
  }
  await user.clear(screen.getByLabelText("payload key 2"));
  await user.type(screen.getByLabelText("payload key 2"), "first");
  expect(screen.getByLabelText("payload value 1")).toHaveValue("keep");
  expect(screen.getByLabelText("payload value 2")).toHaveValue("new");
  expect(screen.getByRole("alert")).toHaveTextContent(/duplicate.*key/i);
  const submit = screen.getByRole("button", { name: operation === "update" ? "Save schedule" : "Create schedule" });
  expect(submit).toBeDisabled();
  await user.click(submit);
  fireEvent.submit(screen.getByRole("form", { name: "Schedule editor" }));
  expect(createProjectScheduledJob).not.toHaveBeenCalled();
  expect(updateProjectScheduledJob).not.toHaveBeenCalled();
  await user.clear(screen.getByLabelText("payload key 2"));
  expect(submit).toBeDisabled();
  await user.type(screen.getByLabelText("payload key 2"), "second");
  expect(submit).toBeEnabled();
  expect(screen.getByLabelText("payload value 1")).toHaveValue("keep");
  expect(screen.getByLabelText("payload value 2")).toHaveValue("new");
});

it("creates and updates a paused two-key object schedule and reopens both edited values", async () => {
  mockObjectContract();
  const jobs: Array<Record<string, unknown>> = [];
  vi.mocked(fetchProjectScheduledJobs).mockImplementation(async () => ({ project_id: "project-1", timezone_context: {}, jobs, runs: [] }) as never);
  vi.mocked(createProjectScheduledJob).mockImplementation(async (_project, input) => {
    jobs.push({ ...input, id: "object-schedule" });
    return { scheduled_job_id: "object-schedule" };
  });
  vi.mocked(updateProjectScheduledJob).mockImplementation(async (_project, _id, input) => {
    Object.assign(jobs[0], input);
    return { job: jobs[0] } as never;
  });
  const user = userEvent.setup();
  renderSettings();
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  await user.type(screen.getByLabelText("payload key 1"), "first");
  await user.type(screen.getByLabelText("payload value 1"), "keep");
  await user.click(screen.getByRole("button", { name: "Add row" }));
  await user.type(screen.getByLabelText("payload key 2"), "second");
  await user.type(screen.getByLabelText("payload value 2"), "new");
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  expect(createProjectScheduledJob).toHaveBeenCalledWith("project-1", expect.objectContaining({
    enabled: false, inputs: { payload: { first: "keep", second: "new" } },
  }));
  await user.click(screen.getByRole("button", { name: EDIT_SCHEDULE }));
  expect(screen.getByLabelText("payload value 1")).toHaveValue("keep");
  expect(screen.getByLabelText("payload value 2")).toHaveValue("new");
  await user.clear(screen.getByLabelText("payload key 1"));
  await user.type(screen.getByLabelText("payload key 1"), "renamed");
  await user.clear(screen.getByLabelText("payload value 2"));
  await user.type(screen.getByLabelText("payload value 2"), "updated");
  await user.click(screen.getByRole("button", { name: "Save schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  expect(updateProjectScheduledJob).toHaveBeenCalledWith("project-1", "object-schedule", expect.objectContaining({
    enabled: false, inputs: { payload: { renamed: "keep", second: "updated" } },
  }));
  await user.click(screen.getByRole("button", { name: EDIT_SCHEDULE }));
  expect(screen.getByLabelText("payload key 1")).toHaveValue("renamed");
  expect(screen.getByLabelText("payload value 1")).toHaveValue("keep");
  expect(screen.getByLabelText("payload key 2")).toHaveValue("second");
  expect(screen.getByLabelText("payload value 2")).toHaveValue("updated");
  expect(screen.getByRole("switch", { name: "Enabled" })).not.toBeChecked();
});
function Settings({ projectId = "project-1", initialSchedule = true, unitEditor = true }) {
  const [channels, setChannels] = useState(initialSchedule ? ["schedule"] : []);
  // Editor unit coverage is separate from the real route/navigation journey.
  if (unitEditor) return <WorkflowSchedules projectId={projectId} workflowId="wf-1" />;
  // Smart setup renders in the Workflow settings drawer for an author; Save rests until a setting differs from the saved one.
  return <AuthContext.Provider value={{ session: { user: { roles: ["admin"] } } } as unknown as AuthContextValue}><WorkflowSettingsSmartSetup ctx={{
    drawerMode: "workflow-settings", setDrawerTab: () => undefined,
    workflowFormDirty: channels.join(",") !== (initialSchedule ? "schedule" : ""),
    workflowId: "wf-1", selectedProjectId: projectId, advancedMode: false, drawerTab: "smart-setup",
    workflowDisplayNameDraft: "Demo", workflowDescriptionDraft: "", workflowEnabledDraft: true,
    workflowInterfaceEnabledDraft: true, workflowInterfaceControlIdPrefix: "test",
    workflowInterfaceInputModeDraft: "fields",
    workflowInterfaceStrictInputsDraft: true, workflowInterfaceInputsDraft: [],
    workflowInterfaceOutputsDraft: [], workflowInterfaceReturnedFiles: [],
    workflowInterfaceTriggerChannelsDraft: channels, setWorkflowInterfaceTriggerChannelsDraft: setChannels,
    renderWorkflowContractSection: () => null, renderWorkflowInterfaceFieldEditor: () => null,
    renderWorkflowReturnedFiles: () => null,
    handleSaveWorkflowSettings: saved,
  }} /></AuthContext.Provider>;
}

function renderSettings(props = {}) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><Settings {...props} /></QueryClientProvider>);
}

function timezoneSettings(timezone?: string) {
  return <AuthContext.Provider value={{ session: { user: { timezone } } } as unknown as AuthContextValue}><Settings /></AuthContext.Provider>;
}

it.each([
  ["Daily", "Kasdien 09:00 (Europe/Vilnius)"],
  ["Weekly", "Kiekvieną savaitę: Pirmadienis, 09:00 (Europe/Vilnius)"],
  ["Monthly", "Kiekvieno mėnesio 1 dieną, 09:00 (Europe/Vilnius)"],
  ["Custom", "Pasirinktinis cron tvarkaraštis (Europe/Vilnius)"],
])("localizes the %s description in the actual schedule editor without changing time or timezone", async (repeat, expected) => {
  const messages: Record<string, string> = {
    "react.projects.daily_schedule_description": "Kasdien {time} ({zone})",
    "react.projects.weekly_schedule_description": "Kiekvieną savaitę: {weekday}, {time} ({zone})",
    "react.projects.monthly_schedule_description": "Kiekvieno mėnesio {day} dieną, {time} ({zone})",
    "react.projects.custom_schedule_description": "Pasirinktinis cron tvarkaraštis ({zone})",
    "react.projects.monday": "Pirmadienis",
  };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{timezoneSettings("Europe/Vilnius")}</QueryClientProvider></I18nContext.Provider>);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  await user.click(within(screen.getByRole("group", { name: "Repeat" })).getByRole("button", { name: repeat }));
  expect(within(screen.getByRole("group", { name: "Repeat" })).getByRole("button", { name: repeat })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByText(expected)).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Timezone" })).toHaveTextContent("Europe/Vilnius");
  expect(screen.queryByText(/^Every day|^Every month|^Every Monday|^Custom cron schedule/)).not.toBeInTheDocument();
  expect(createProjectScheduledJob).not.toHaveBeenCalled();
});

afterEach(() => vi.restoreAllMocks());

it.each([
  ["America/New_York", "Europe/Berlin", "America/New_York"],
  [undefined, "Europe/Berlin", "Europe/Berlin"],
  [undefined, "", "UTC"],
])("defaults new schedules to profile %s before browser %s", async (profile, browser, expected) => {
  vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({ timeZone: browser } as Intl.ResolvedDateTimeFormatOptions);
  vi.mocked(fetchProjectScheduledJobs).mockResolvedValue({ project_id: "project-1", timezone_context: { project_schedule_timezone: "Europe/Vilnius" }, jobs: [], runs: [] });
  const user = userEvent.setup();
  render(<QueryClientProvider client={new QueryClient()}>{timezoneSettings(profile)}</QueryClientProvider>);
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  expect(screen.getByRole("combobox", { name: "Timezone" })).toHaveTextContent(expected);
});

it("retains manual and saved timezones across profile changes, save, reopen and the live next runs", async () => {
  const user = userEvent.setup();
  const jobs: Array<Record<string, unknown>> = [];
  vi.mocked(fetchProjectScheduledJobs).mockImplementation(async () => ({ project_id: "project-1", timezone_context: {}, jobs, runs: [] }) as never);
  vi.mocked(createProjectScheduledJob).mockImplementation(async (_project, input) => {
    jobs.push({ ...input, id: "new" });
    return { scheduled_job_id: "new" };
  });
  const client = new QueryClient();
  const view = render(<QueryClientProvider client={client}>{timezoneSettings("America/New_York")}</QueryClientProvider>);
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  expect(screen.getByRole("combobox", { name: "Timezone" })).toHaveTextContent("America/New_York");
  await user.click(screen.getByRole("combobox", { name: "Timezone" }));
  await user.click(within(screen.getByRole("listbox", { name: "Timezone options" })).getByText("Europe/Vilnius"));
  view.rerender(<QueryClientProvider client={client}>{timezoneSettings("Asia/Tokyo")}</QueryClientProvider>);
  expect(screen.getByRole("combobox", { name: "Timezone" })).toHaveTextContent("Europe/Vilnius");
  await user.type(screen.getByLabelText(/^region/), "eu");
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  expect(jobs[0]).toMatchObject({ schedule_timezone: "Europe/Vilnius" });
  await user.click(screen.getByRole("button", { name: "Edit schedule: Every day at 09:00 (Europe/Vilnius)" }));
  expect(screen.getByRole("combobox", { name: "Timezone" })).toHaveTextContent("Europe/Vilnius");
  view.rerender(<QueryClientProvider client={client}>{timezoneSettings("UTC")}</QueryClientProvider>);
  expect(screen.getByRole("combobox", { name: "Timezone" })).toHaveTextContent("Europe/Vilnius");
  expect(screen.getByTestId("schedule-next-runs")).toHaveTextContent(/^Next: .*09:00/);
  // Next runs are read on the client while the timing changes; the server is asked on save.
  expect(previewProjectCron).not.toHaveBeenCalled();
});

it.each([undefined, ""])("preserves the browser fallback for existing legacy timezone %s", async (timezone) => {
  vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({ timeZone: "Europe/Berlin" } as Intl.ResolvedDateTimeFormatOptions);
  vi.mocked(fetchProjectScheduledJobs).mockResolvedValue({ project_id: "project-1", timezone_context: {}, jobs: [{ id: "legacy", workflow_id: "wf-1", enabled: false, cron_expr: "0 9 * * *", schedule_timezone: timezone, inputs: { region: "eu" } }], runs: [] });
  const user = userEvent.setup();
  render(<QueryClientProvider client={new QueryClient()}>{timezoneSettings("America/New_York")}</QueryClientProvider>);
  await user.click(await screen.findByRole("button", { name: "Edit schedule: Every day at 09:00 (Europe/Berlin)" }));
  expect(screen.getByRole("combobox", { name: "Timezone" })).toHaveTextContent("Europe/Berlin");
  expect(screen.getByTestId("schedule-next-runs")).toHaveTextContent(/^Next: .*09:00/);
});

beforeEach(() => {
  vi.clearAllMocks();
  saved.mockResolvedValue(undefined);
  vi.mocked(fetchProjectWorkflowBindings).mockResolvedValue({ workflow_ids: ["wf-1"], project_workflows: [{ workflow_id: "wf-1" }] } as never);
  vi.mocked(fetchWorkflowContract).mockResolvedValue({ workflow_id: "wf-1", enabled: true,
    interface: { enabled: true, trigger_channels: ["schedule"], expected_inputs: [{ name: "region", type: "string", required: true }], output_mode: "none" }, definition_issues: [],
  } as never);
  vi.mocked(fetchProjectScheduledJobs).mockResolvedValue({ project_id: "project-1", timezone_context: {}, jobs: [], runs: [] });
});

it("does not create or activate a schedule when selecting its trigger or saving workflow settings", async () => {
  const user = userEvent.setup();
  renderSettings({ initialSchedule: false, unitEditor: false });
  await user.click(screen.getByRole("checkbox", { name: "Schedule" }));
  // Schedules is a tab now; the settings footer carries Save only.
  expect(screen.queryByRole("button", { name: "Manage schedules" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Save workflow settings" }));
  expect(saved).toHaveBeenCalledOnce();
  expect(createProjectScheduledJob).not.toHaveBeenCalled();
  expect(updateProjectScheduledJob).not.toHaveBeenCalled();
});

it("explains the missing project context without offering a global schedule", async () => {
  renderSettings({ projectId: "" });
  expect(await screen.findByText(/Schedule requires a linked project context/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New schedule" })).not.toBeInTheDocument();
  expect(fetchProjectScheduledJobs).not.toHaveBeenCalled();
});

it("lists and edits only the current workflow and reopens persisted inputs and cron settings", async () => {
  const user = userEvent.setup();
  const row = { id: "schedule-1", workflow_id: "wf-1", enabled: false,
    cron_expr: "0 2 * * *", schedule_timezone: "Europe/Vilnius", inputs: { region: "eu" } };
  vi.mocked(fetchProjectScheduledJobs).mockResolvedValue({ project_id: "project-1", timezone_context: {}, jobs: [row, { ...row, id: "other", workflow_id: "wf-other" }], runs: [] });
  vi.mocked(updateProjectScheduledJob).mockImplementation(async (_projectId, _id, input) => {
    Object.assign(row, input);
    return { job: { ...row } };
  });
  renderSettings();
  expect(await screen.findByText("Every day at 02:00 (Europe/Vilnius)")).toBeInTheDocument();
  // The other workflow's schedule is not listed: one row, one Edit.
  expect(screen.getAllByRole("button", { name: EDIT_SCHEDULE })).toHaveLength(1);
  expect(screen.getByText("0 2 * * *")).toBeInTheDocument();
  expect(screen.getByText(/· Paused$/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Edit schedule: Every day at 02:00 (Europe/Vilnius)" }));
  expect(screen.getByLabelText(/^region/)).toHaveValue("eu");
  expect(screen.queryByLabelText("Concurrency key")).not.toBeInTheDocument();
  // A plain daily expression reopens as Daily at its time, not as a raw cron.
  expect(within(screen.getByRole("group", { name: "Repeat" })).getByRole("button", { name: "Daily" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByLabelText("Time")).toHaveValue("02:00");
  expect(screen.queryByLabelText("Cron expression")).not.toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "Enabled" })).not.toBeChecked();
  await user.clear(screen.getByLabelText(/^region/));
  await user.type(screen.getByLabelText(/^region/), "us");
  await user.click(screen.getByRole("button", { name: "Save schedule" }));
  await waitFor(() => expect(screen.queryByLabelText(/^region/)).not.toBeInTheDocument());
  expect(updateProjectScheduledJob).toHaveBeenCalledWith("project-1", "schedule-1", expect.objectContaining({
    workflow_id: "wf-1", enabled: false, inputs: { region: "us" },
    schedule_timezone: "Europe/Vilnius",
  }));
  await user.click(screen.getByRole("button", { name: EDIT_SCHEDULE }));
  expect(screen.getByLabelText(/^region/)).toHaveValue("us");
});

it("clears a saved optional input without losing passthrough extras when reopened", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchWorkflowContract).mockResolvedValue({ workflow_id: "wf-1", enabled: true,
    interface: { enabled: true, trigger_channels: ["schedule"], expected_inputs: [{ name: "region", type: "string", required: false }], output_mode: "none" }, definition_issues: [],
  } as never);
  const row = { id: "schedule-1", workflow_id: "wf-1", enabled: false,
    cron_expr: "0 2 * * *", schedule_timezone: "UTC", inputs: { region: "eu", passthrough: "keep" } };
  vi.mocked(fetchProjectScheduledJobs).mockResolvedValue({ project_id: "project-1", timezone_context: {}, jobs: [row], runs: [] });
  vi.mocked(updateProjectScheduledJob).mockImplementation(async (_projectId, _id, input) => {
    Object.assign(row, input);
    return { job: { ...row } };
  });
  renderSettings();
  await user.click(await screen.findByRole("button", { name: EDIT_SCHEDULE }));
  await user.clear(screen.getByLabelText(/^region/));
  await user.click(screen.getByRole("button", { name: "Save schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  await user.click(screen.getByRole("button", { name: EDIT_SCHEDULE }));
  expect(screen.getByLabelText(/^region/)).toHaveValue("");
  expect(row.inputs).toEqual({ passthrough: "keep" });
});

it("creates a paused schedule with required inputs only on explicit save", async () => {
  const user = userEvent.setup();
  vi.mocked(createProjectScheduledJob).mockResolvedValue({ scheduled_job_id: "new" });
  renderSettings();
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  expect(await screen.findByText(/region.*required/i)).toBeInTheDocument();
  expect(createProjectScheduledJob).not.toHaveBeenCalled();
  await user.type(screen.getByLabelText(/^region/), "eu");
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(screen.queryByRole("form", { name: "Schedule editor" })).not.toBeInTheDocument());
  expect(createProjectScheduledJob).toHaveBeenCalledWith("project-1", expect.objectContaining({ workflow_id: "wf-1", enabled: false, cron_expr: "0 9 * * *", inputs: { region: "eu" } }));
});

it("shows the next runs while the timing changes and keeps save errors visible without closing the editor", async () => {
  const user = userEvent.setup();
  vi.mocked(createProjectScheduledJob).mockRejectedValue(new Error("Invalid cron expression"));
  renderSettings();
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  await user.type(screen.getByLabelText(/^region/), "eu");
  expect(screen.getByTestId("schedule-next-runs")).toHaveTextContent(/^Next: .*09:00/);
  fireEvent.change(screen.getByLabelText("Time"), { target: { value: "07:30" } });
  expect(screen.getByTestId("schedule-next-runs")).toHaveTextContent(/^Next: .*07:30/);
  expect(previewProjectCron).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  expect(await screen.findByText("Invalid cron expression")).toBeInTheDocument();
  expect(screen.getByRole("form", { name: "Schedule editor" })).toBeInTheDocument();
});

it("reads a custom cron on the client and refuses an expression without five fields", async () => {
  const user = userEvent.setup();
  render(<QueryClientProvider client={new QueryClient()}>{timezoneSettings("UTC")}</QueryClientProvider>);
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  await user.click(within(screen.getByRole("group", { name: "Repeat" })).getByRole("button", { name: "Custom" }));
  // Custom starts from the timing chosen before it.
  expect(screen.getByLabelText("Cron expression")).toHaveValue("0 9 * * *");
  fireEvent.change(screen.getByLabelText("Cron expression"), { target: { value: "15 10 * * 1-5" } });
  expect(screen.getByTestId("schedule-next-runs")).toHaveTextContent(/^Next: .*10:15/);
  fireEvent.change(screen.getByLabelText("Cron expression"), { target: { value: "15 10 *" } });
  expect(screen.getByText("Enter five fields: minute, hour, day of month, month, day of week.")).toBeInTheDocument();
  expect(screen.getByTestId("schedule-next-runs")).toHaveTextContent("Fix the timing to see the next runs.");
  expect(previewProjectCron).not.toHaveBeenCalled();
  expect(createProjectScheduledJob).not.toHaveBeenCalled();
});

it("does not load schedules or offer editing for an unlinked project", async () => {
  vi.mocked(fetchProjectWorkflowBindings).mockResolvedValue({ workflow_ids: ["wf-other"], project_workflows: [{ workflow_id: "wf-other" }] } as never);
  renderSettings();
  expect(await screen.findByText(/not linked to the selected project/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New schedule" })).not.toBeInTheDocument();
  expect(fetchProjectScheduledJobs).not.toHaveBeenCalled();
});

it.each([
  ["Weekly", "0 9 * * 1"], ["Monthly", "0 9 1 * *"], ["Custom", "0 9 * * *"],
])("persists the explicit %s repeat mode", async (repeat, expression) => {
  const user = userEvent.setup();
  vi.mocked(createProjectScheduledJob).mockResolvedValue({ scheduled_job_id: "new" });
  renderSettings();
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  await user.type(screen.getByLabelText(/^region/), "eu");
  await user.click(within(screen.getByRole("group", { name: "Repeat" })).getByRole("button", { name: repeat }));
  await user.click(screen.getByRole("button", { name: "Create schedule" }));
  await waitFor(() => expect(createProjectScheduledJob).toHaveBeenCalledWith("project-1", expect.objectContaining({ cron_expr: expression, workflow_id: "wf-1", enabled: false })));
});

it("explains a turned-off Schedule channel and offers Turn on Schedule instead of New schedule", async () => {
  const user = userEvent.setup();
  const turnOn = vi.fn();
  vi.mocked(fetchWorkflowContract).mockResolvedValue({ workflow_id: "wf-1", enabled: true,
    interface: { enabled: true, trigger_channels: ["api"], expected_inputs: [], output_mode: "none" }, definition_issues: [],
  } as never);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <WorkflowSchedules projectId="project-1" projectName="Order operations" workflowId="wf-1" onTurnOnScheduleChannel={turnOn} />
  </QueryClientProvider>);
  expect(await screen.findByText("Schedule is turned off for this workflow")).toBeInTheDocument();
  expect(await screen.findByText("No schedules for this workflow in Order operations yet.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "New schedule" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Turn on Schedule" }));
  expect(turnOn).toHaveBeenCalledOnce();
});

it("turns a listed schedule on in place and deletes it only after the named confirmation", async () => {
  const user = userEvent.setup();
  const row = { id: "schedule-1", workflow_id: "wf-1", enabled: false, cron_expr: "0 9 * * 1", schedule_timezone: "UTC", inputs: { region: "eu" } };
  vi.mocked(fetchProjectScheduledJobs).mockResolvedValue({ project_id: "project-1", timezone_context: {}, jobs: [row], runs: [] });
  vi.mocked(toggleProjectScheduledJob).mockResolvedValue({} as never);
  vi.mocked(deleteProjectScheduledJob).mockResolvedValue({});
  renderSettings();
  const title = "Every Monday at 09:00 (UTC)";
  expect(await screen.findByText(title)).toBeInTheDocument();
  await user.click(screen.getByRole("switch", { name: `Enabled: ${title}` }));
  await waitFor(() => expect(toggleProjectScheduledJob).toHaveBeenCalledWith("project-1", "schedule-1", true));
  await user.click(screen.getByRole("button", { name: `Delete schedule: ${title}` }));
  expect(deleteProjectScheduledJob).not.toHaveBeenCalled();
  const dialog = await screen.findByRole("alertdialog", { name: `Delete ${title}?` });
  await user.click(within(dialog).getByRole("button", { name: "Delete schedule" }));
  await waitFor(() => expect(deleteProjectScheduledJob).toHaveBeenCalledWith("project-1", "schedule-1"));
});

it("keeps required inputs in view and folds optional ones with how many are set", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchWorkflowContract).mockResolvedValue({ workflow_id: "wf-1", enabled: true,
    interface: { enabled: true, trigger_channels: ["schedule"], expected_inputs: [
      { name: "region", type: "string", required: true },
      { name: "note", type: "string", required: false },
    ], output_mode: "none" }, definition_issues: [],
  } as never);
  renderSettings();
  await user.click(await screen.findByRole("button", { name: "New schedule" }));
  expect(screen.getByText("Optional inputs")).toBeInTheDocument();
  expect(screen.getByText("None set")).toBeInTheDocument();
  await user.type(screen.getByLabelText(/^note/), "hello");
  expect(screen.getByText("1 of 1 set")).toBeInTheDocument();
});
