import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import type { AutomationScopeKind } from "@/pages/workflows/detail/detail-automation-core";
import { AutomationRuleEditor } from "@/pages/workflows/detail/detail-automation-editor";

afterEach(() => vi.restoreAllMocks());

// The server's built-in event catalog per scope: canonical id, English label, locale key, Lithuanian text.
const builtinScopes = [
  { scope: "workflow" as const, events: [
    { event_type: "job.completed", display_name: "A job completes", display_name_key: "registry.workflow_detail.automation.event.job_completed", translated: "Užduotis baigiama" },
    { event_type: "job.failed", display_name: "A job fails", display_name_key: "registry.workflow_detail.automation.event.job_failed", translated: "Užduotis nepavyksta" },
    { event_type: "job.step_started", display_name: "A step starts", display_name_key: "registry.workflow_detail.automation.event.workflow_step_started", translated: "Žingsnis pradedamas" },
    { event_type: "job.step_completed", display_name: "A step completes", display_name_key: "registry.workflow_detail.automation.event.workflow_step_completed", translated: "Žingsnis baigiamas" },
    { event_type: "job.step_failed", display_name: "A step fails", display_name_key: "registry.workflow_detail.automation.event.workflow_step_failed", translated: "Žingsnis nepavyksta" },
    { event_type: "job.step_blocked", display_name: "A step is blocked", display_name_key: "registry.workflow_detail.automation.event.workflow_step_blocked", translated: "Žingsnis užblokuojamas" },
    { event_type: "job.group_started", display_name: "A group starts", display_name_key: "registry.workflow_detail.automation.event.group_started", translated: "Grupė pradedama" },
    { event_type: "job.group_completed", display_name: "A group completes", display_name_key: "registry.workflow_detail.automation.event.group_completed", translated: "Grupė baigiama" },
    { event_type: "job.group_failed", display_name: "A group fails", display_name_key: "registry.workflow_detail.automation.event.group_failed", translated: "Grupė nepavyksta" },
    { event_type: "job.approval_waiting", display_name: "An approval is waiting", display_name_key: "registry.workflow_detail.automation.event.approval_waiting", translated: "Laukiama patvirtinimo" },
    { event_type: "job.timer_waiting", display_name: "A wait timer starts", display_name_key: "registry.workflow_detail.automation.event.timer_waiting", translated: "Paleidžiamas laukimo laikmatis" },
  ] },
  { scope: "group" as const, events: [
    { event_type: "job.group_started", display_name: "This group starts", display_name_key: "registry.workflow_detail.automation.event.group_started_local", translated: "Ši grupė pradedama" },
    { event_type: "job.group_completed", display_name: "This group completes", display_name_key: "registry.workflow_detail.automation.event.group_completed_local", translated: "Ši grupė baigiama" },
    { event_type: "job.group_failed", display_name: "This group fails", display_name_key: "registry.workflow_detail.automation.event.group_failed_local", translated: "Ši grupė nepavyksta" },
    { event_type: "job.step_started", display_name: "A step of this group starts", display_name_key: "registry.workflow_detail.automation.event.group_step_started", translated: "Šios grupės žingsnis pradedamas" },
    { event_type: "job.step_completed", display_name: "A step of this group completes", display_name_key: "registry.workflow_detail.automation.event.group_step_completed", translated: "Šios grupės žingsnis baigiamas" },
    { event_type: "job.step_failed", display_name: "A step of this group fails", display_name_key: "registry.workflow_detail.automation.event.group_step_failed", translated: "Šios grupės žingsnis nepavyksta" },
    { event_type: "job.step_blocked", display_name: "A step of this group is blocked", display_name_key: "registry.workflow_detail.automation.event.group_step_blocked", translated: "Šios grupės žingsnis užblokuojamas" },
    { event_type: "job.approval_waiting", display_name: "An approval in this group is waiting", display_name_key: "registry.workflow_detail.automation.event.group_approval_waiting", translated: "Šioje grupėje laukiama patvirtinimo" },
    { event_type: "job.timer_waiting", display_name: "A wait timer in this group starts", display_name_key: "registry.workflow_detail.automation.event.group_timer_waiting", translated: "Šioje grupėje paleidžiamas laukimo laikmatis" },
  ] },
  { scope: "step" as const, events: [
    { event_type: "job.step_started", display_name: "This step starts", display_name_key: "registry.workflow_detail.automation.event.step_started", translated: "Šis žingsnis pradedamas" },
    { event_type: "job.step_completed", display_name: "This step completes", display_name_key: "registry.workflow_detail.automation.event.step_completed", translated: "Šis žingsnis baigiamas" },
    { event_type: "job.step_failed", display_name: "This step fails", display_name_key: "registry.workflow_detail.automation.event.step_failed", translated: "Šis žingsnis nepavyksta" },
    { event_type: "job.step_blocked", display_name: "This step is blocked", display_name_key: "registry.workflow_detail.automation.event.step_blocked", translated: "Šis žingsnis užblokuojamas" },
    { event_type: "job.approval_waiting", display_name: "This step waits for approval", display_name_key: "registry.workflow_detail.automation.event.step_approval_waiting", translated: "Šis žingsnis laukia patvirtinimo" },
    { event_type: "job.timer_waiting", display_name: "This step's wait timer starts", display_name_key: "registry.workflow_detail.automation.event.step_timer_waiting", translated: "Paleidžiamas šio žingsnio laukimo laikmatis" },
  ] },
];

const eventMessages = Object.fromEntries(
  builtinScopes.flatMap(({ events }) => events.map((event) => [event.display_name_key, event.translated])),
);

const groupScope = builtinScopes[1];
// The group scope's events as the server sends them.
const groupScopeEvents = groupScope.events.map((event) => ({ event_type: event.event_type, display_name: event.display_name, display_name_key: event.display_name_key }));

function mountAutomation(sources: Array<Record<string, unknown>>, scopeKind: AutomationScopeKind = "group") {
  const writes: Array<Record<string, unknown>> = [];
  let reactions: Array<Record<string, unknown>> = [{
    reaction_key: "owner_notice", display_name: "Owner rule", enabled: true,
    event_type: scopeKind === "workflow" ? "job.completed" : "job.step_started", conditions: { owner_flag: "preserve" },
    action: { type: "notify", target: "route_keys", route_keys: ["owner-channel"], message: "Owner message", severity: "info" },
    debounce_seconds: 7, idempotency_key_template: null,
  }];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
    if (url.pathname !== "/api/workflows/owner-workflow/reactions") throw new Error(`Unexpected request: ${url.pathname}`);
    if (init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as { reactions: Array<Record<string, unknown>> };
      writes.push(...body.reactions);
      reactions = body.reactions;
    }
    return new Response(JSON.stringify({ workflow_id: "owner-workflow", reactions, available_trigger_sources: sources }), { headers: { "content-type": "application/json" } });
  });
  const messages: Record<string, string> = {
        "react.workflows.workflow_runtime": "Darbo eigos vykdymo aplinka",
    ...eventMessages,
    "react.workflows.notification_destination": "Pranešimo paskirties vieta",
    "events.severity.debug": "Derinimas",
    "events.severity.info": "Informacija",
    "events.severity.warning": "Įspėjimas",
    "events.severity.error": "Klaida",
    "events.severity.critical": "Kritinė",
  };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AutomationRuleEditor workflowId="owner-workflow" scopeKind={scopeKind} scopeId="owner-group" />
    </QueryClientProvider>
  </I18nContext.Provider>);
  return writes;
}

/** Rules are folded rows; a test opens the first one to reach its fields. */
async function openFirstRule() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /^When / }));
}

const builtinSource = { source_id: "__workflow__", source_kind: "workflow", display_name: "Workflow runtime", display_name_key: "react.workflows.workflow_runtime", events: groupScopeEvents };

it.each(builtinScopes)("translates generated $scope fallback events in the real editor while saving canonical values", async ({ scope, events }) => {
  const writes = mountAutomation([], scope);
  const user = userEvent.setup();
  await openFirstRule();
  const eventSelect = await screen.findByRole("combobox", { name: "When" });
  expect(within(eventSelect).getAllByRole("option").map((option) => [option.textContent, (option as HTMLOptionElement).value])).toEqual(events.map((event) => [event.translated, event.event_type]));
  await user.selectOptions(eventSelect, events[1].event_type);
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toMatchObject({ event_type: events[1].event_type, reaction_key: "owner_notice", display_name: "Owner rule", conditions: { owner_flag: "preserve" }, action: { type: "notify", target: "route_keys", route_keys: ["owner-channel"], message: "Owner message" } });
});

it.each(builtinScopes)("translates explicitly keyed $scope server events", async ({ scope, events }) => {
  mountAutomation([{ ...builtinSource, events: events.map((event) => ({ event_type: event.event_type, display_name: event.display_name, display_name_key: event.display_name_key })) }], scope);
  await openFirstRule();
  const eventSelect = await screen.findByRole("combobox", { name: "When" });
  expect(within(eventSelect).getAllByRole("option").map((option) => option.textContent)).toEqual(events.map((event) => event.translated));
});

it.each(builtinScopes)("preserves identical unkeyed owner $scope event text and source identifiers", async ({ events }) => {
  const workflowEvents = builtinScopes[0].events;
  const writes = mountAutomation([
    { ...builtinSource, events: workflowEvents.map((event) => ({ event_type: event.event_type, display_name: event.display_name, display_name_key: event.display_name_key })) },
    { source_id: "owner.extension", source_kind: "extension", display_name: "Workflow runtime", connection_id: "owner-channel", events: events.map((event) => ({ event_type: event.event_type, display_name: event.display_name })) },
  ], "workflow");
  const user = userEvent.setup();
  await openFirstRule();
  const source = await screen.findByRole("combobox", { name: "Trigger source" });
  await user.selectOptions(source, "owner.extension");
  const eventSelect = screen.getByRole("combobox", { name: "When" });
  expect(within(eventSelect).getAllByRole("option").map((option) => option.textContent)).toEqual(events.map((event) => event.display_name));
  await user.selectOptions(eventSelect, events[1].event_type);
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toMatchObject({ event_type: events[1].event_type, conditions: { source: "owner.extension", owner_flag: "preserve" }, action: { route_keys: ["owner-channel"], message: "Owner message" } });
});

it("summarises a rule in its row with the translated event and never the saved scope enum", async () => {
  mountAutomation([builtinSource]);
  const row = await screen.findByRole("button", { name: /^When / });
  expect(row).toHaveTextContent("When šios grupės žingsnis pradedamas");
  expect(row).toHaveTextContent("owner-channel · Informacija");
  expect(row).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText(/\bgroup\b/)).not.toBeInTheDocument();
});

it("renders keyed built-in source and group-scope event labels while saving canonical identifiers and owner content", async () => {
  const writes = mountAutomation([builtinSource]);
  const user = userEvent.setup();
  await openFirstRule();
  // One source: there is nothing to choose, so Trigger source is not asked.
  expect(screen.queryByRole("combobox", { name: "Trigger source" })).not.toBeInTheDocument();
  const events = await screen.findByRole("combobox", { name: "When" });
  expect(screen.getByRole("combobox", { name: "Then" })).toHaveValue("notify");
  expect(within(events).getAllByRole("option").map((option) => [option.textContent, (option as HTMLOptionElement).value])).toEqual(
    groupScope.events.map((event) => [event.translated, event.event_type]),
  );
  await user.selectOptions(events, "job.step_completed");
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toMatchObject({
    reaction_key: "owner_notice", display_name: "Owner rule", event_type: "job.step_completed",
    conditions: { owner_flag: "preserve" },
    action: { target: "route_keys", route_keys: ["owner-channel"], message: "Owner message", severity: "info" },
  });
  expect(writes[0].conditions).not.toHaveProperty("source");
});

it("shows the saved notification destination and localizes severity while preserving the saved target and severity values", async () => {
  const writes = mountAutomation([builtinSource]);
  const user = userEvent.setup();
  await openFirstRule();
  expect(await screen.findByRole("radio", { name: /Route keys/ })).toBeChecked();
  expect(screen.getByRole("textbox", { name: /Route keys to notify/ })).toHaveValue("owner-channel");
  const severity = screen.getByRole("combobox", { name: "Severity" });
  expect(within(severity).getAllByRole("option").map((option) => [option.textContent, (option as HTMLOptionElement).value])).toEqual([
    ["Derinimas", "debug"], ["Informacija", "info"], ["Įspėjimas", "warning"], ["Klaida", "error"], ["Kritinė", "critical"],
  ]);
  await user.selectOptions(severity, "critical");
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toMatchObject({ action: { target: "route_keys", route_keys: ["owner-channel"], message: "Owner message", severity: "critical" } });
});

it("does not translate identical unkeyed extension source and event labels", async () => {
  mountAutomation([{ ...builtinSource, events: [{ event_type: "job.completed", display_name: "A job completes" }] }, { source_id: "owner.extension", source_kind: "extension", display_name: "Workflow runtime", connection_id: "owner-channel", events: [{ event_type: "job.step_started", display_name: "This step starts" }] }], "workflow");
  const user = userEvent.setup();
  await openFirstRule();
  const source = await screen.findByRole("combobox", { name: "Trigger source" });
  expect(within(source).getByRole("option", { name: "Workflow runtime" })).toHaveValue("owner.extension");
  await user.selectOptions(source, "owner.extension");
  expect(within(screen.getByRole("combobox", { name: "When" })).getByRole("option", { name: "This step starts" })).toHaveValue("job.step_started");
});
