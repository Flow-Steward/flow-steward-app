import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";

import type { AutomationScopeKind } from "@/pages/workflows/detail/detail-automation-core";
import { AutomationRuleEditor } from "@/pages/workflows/detail/detail-automation-editor";

afterEach(() => vi.restoreAllMocks());

function mountRules({ reactions = [], scopeKind = "step", stepKind = "", providers = [] }: {
  reactions?: Array<Record<string, unknown>>;
  scopeKind?: AutomationScopeKind;
  stepKind?: string;
  providers?: Array<Record<string, unknown>>;
} = {}) {
  const writes: Array<Array<Record<string, unknown>>> = [];
  let stored = reactions;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
    if (String(_input).includes("/providers")) return new Response(JSON.stringify({ project_providers: providers }), { headers: { "content-type": "application/json" } });
    if (init?.method === "POST") {
      stored = (JSON.parse(String(init.body)) as { reactions: Array<Record<string, unknown>> }).reactions;
      writes.push(stored);
    }
    return new Response(JSON.stringify({ workflow_id: "wf", reactions: stored }), { headers: { "content-type": "application/json" } });
  });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
      <AutomationRuleEditor projectId={providers.length ? "project-1" : ""} scopeId="classify" scopeKind={scopeKind} stepKind={stepKind} workflowId="wf" />
    </QueryClientProvider>,
  );
  return writes;
}

it("starts an empty scope from Common rules and saves the picked one as an ordinary rule", async () => {
  const writes = mountRules();
  const user = userEvent.setup();
  const empty = await screen.findByTestId("automation-empty");
  expect(within(empty).getByText("No rules yet. Start from a common one or a blank rule:")).toBeInTheDocument();
  expect(within(empty).getByRole("button", { name: /Blank rule/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save rules" })).toBeDisabled();
  await user.click(within(empty).getByRole("button", { name: /Retry when this step fails/ }));
  const row = screen.getByRole("button", { name: /^When / });
  // Without a server catalog the event reads as its id; the server sends its labels.
  expect(row).toHaveTextContent("When job.step_failed");
  expect(row).toHaveTextContent("Retry step");
  expect(row).toHaveTextContent("Not saved");
  expect(row).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("Acts on the interrupted step.")).toBeInTheDocument();
  expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0][0]).toMatchObject({ event_type: "job.step_failed", action: { type: "retry_step", target: "interrupted_step" } });
});

it.each([
  ["agent", []],
  ["approval", ["job.approval_waiting"]],
  ["wait", ["job.timer_waiting"]],
])("offers the approval and wait events only on the step kind where they happen (%s)", async (stepKind, waits) => {
  mountRules({ stepKind });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /Blank rule/ }));
  const options = within(screen.getByRole("combobox", { name: "When" })).getAllByRole("option").map((option) => (option as HTMLOptionElement).value);
  expect(options).toEqual(["job.step_started", "job.step_completed", "job.step_failed", "job.step_blocked", ...waits]);
});

it("checks that a notification has somewhere to go before Save rules writes", async () => {
  const writes = mountRules({ scopeKind: "workflow" });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /Blank rule/ }));
  // The radio's mark is drawn over the visually hidden input: a click reaches the input through its label.
  fireEvent.click(screen.getByRole("radio", { name: /Route keys/ }));
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  expect(screen.getByText("Enter at least one route key.")).toBeInTheDocument();
  expect(writes).toHaveLength(0);
  await user.type(screen.getByRole("textbox", { name: /Route keys to notify/ }), "ops_chat");
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0][0]).toMatchObject({ event_type: "job.failed", action: { type: "notify", target: "route_keys", route_keys: ["ops_chat"] } });
});

it("removes a rule at once and Undo puts it back where it was", async () => {
  mountRules({ scopeKind: "workflow", reactions: [
    { reaction_key: "first", event_type: "job.failed", enabled: true, action: { type: "cancel_job", target: "current_job" } },
    { reaction_key: "second", event_type: "job.completed", enabled: true, action: { type: "cancel_job", target: "current_job" } },
  ] });
  const user = userEvent.setup();
  const rows = await screen.findAllByRole("button", { name: /^When / });
  await user.click(rows[0]);
  await user.click(screen.getByRole("button", { name: "Remove rule" }));
  expect(screen.getAllByRole("button", { name: /^When / })).toHaveLength(1);
  expect(screen.getByText("Rule removed. Save rules to apply.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save rules" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "Undo" }));
  const restored = screen.getAllByRole("button", { name: /^When / });
  expect(restored).toHaveLength(2);
  expect(restored[0]).toHaveTextContent("When job.failed");
  expect(screen.getByRole("button", { name: "Save rules" })).toBeDisabled();
});

it("tells the surface around it when rules are unsaved", async () => {
  const onDirtyChange = vi.fn();
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ workflow_id: "wf", reactions: [] }), { headers: { "content-type": "application/json" } }));
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AutomationRuleEditor onDirtyChange={onDirtyChange} scopeKind="workflow" workflowId="wf" />
    </QueryClientProvider>,
  );
  await userEvent.setup().click(await screen.findByRole("button", { name: /Blank rule/ }));
  await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));
});

it("opens the rule a link names (from the project notification overview) and drops the request", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
    workflow_id: "wf",
    reactions: [
      { reaction_key: "first", event_type: "job.completed", enabled: true, action: { type: "notify", target: "project_routes" } },
      { reaction_key: "second", event_type: "job.failed", enabled: true, action: { type: "notify", target: "project_routes" } },
    ],
  }), { headers: { "content-type": "application/json" } }));
  function Search() {
    return <output data-testid="search">{useLocation().search}</output>;
  }
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={["/registry/workflows/wf/builder?project_id=p1&rule=second"]}>
        <AutomationRuleEditor scopeKind="workflow" workflowId="wf" />
        <Search />
      </MemoryRouter>
    </QueryClientProvider>,
  );

  const second = await screen.findByRole("button", { name: /When job\.failed/ });
  await waitFor(() => expect(second).toHaveAttribute("aria-expanded", "true"));
  expect(screen.getByRole("button", { name: /When job\.completed/ })).toHaveAttribute("aria-expanded", "false");
  await waitFor(() => expect(screen.getByTestId("search")).toHaveTextContent("?project_id=p1"));
  expect(screen.getByTestId("search")).not.toHaveTextContent("rule=");
});


it.each(["not-an-email", "ops@example.com, not-an-email"])("refuses %s inline and preserves correction before saving", async (invalid) => {
  const writes = mountRules({ scopeKind: "workflow", providers: [{
    provider_key: "acme.mailer", display_name: "Mailer", features: ["notify"],
    notification_target_kind: "recipient", notification_recipient_format: "email",
  }], reactions: [{ reaction_key: "mail", event_type: "job.failed", enabled: true,
    action: { type: "notify", target: "extension_channel", provider_key: "acme.mailer", recipients: ["old@example.com"] } }] });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /^When / }));
  const input = await screen.findByRole("textbox", { name: /Email addresses/ });
  await user.clear(input);
  await user.type(input, invalid);
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  expect(writes).toHaveLength(0);
  expect(input).toHaveValue(invalid);
  expect(input).toHaveAttribute("aria-invalid", "true");
  expect(input).toHaveAccessibleDescription("Enter valid email addresses, separated by commas.");
  await user.clear(input);
  await user.type(input, "Ops+tag@Example.COM, anna@localhost");
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0][0].action).toMatchObject({ recipients: ["Ops+tag@Example.COM", "anna@localhost"] });
});


it("refuses enabling an old malformed email rule and opens the correction field", async () => {
  const writes = mountRules({ scopeKind: "workflow", providers: [{
    provider_key: "acme.mailer", display_name: "Mailer", features: ["notify"],
    notification_target_kind: "recipient", notification_recipient_format: "email",
  }], reactions: [{ reaction_key: "mail", event_type: "job.failed", enabled: false,
    action: { type: "notify", target: "extension_channel", provider_key: "acme.mailer", recipients: ["not-an-email"] } }] });
  const user = userEvent.setup();
  const toggle = await screen.findByRole("switch");
  await user.click(await screen.findByRole("button", { name: /^When / }));
  const input = await screen.findByRole("textbox", { name: /Email addresses/ });
  await user.click(toggle);
  expect(writes).toHaveLength(0);
  expect(toggle).not.toBeChecked();
  expect(input).toHaveValue("not-an-email");
  expect(input).toHaveAccessibleDescription("Enter valid email addresses, separated by commas.");
  await user.clear(input);
  await user.type(input, "ops@example.com");
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0][0]).toMatchObject({ enabled: false, action: { recipients: ["ops@example.com"] } });
  await waitFor(() => expect(screen.queryAllByText("Enter valid email addresses, separated by commas.")).toHaveLength(0));
  await user.click(screen.getByRole("switch"));
  await waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1][0]).toMatchObject({ enabled: true, action: { recipients: ["ops@example.com"] } });
});

it("labels undeclared recipient providers generically and preserves their identifiers", async () => {
  const writes = mountRules({ scopeKind: "workflow", providers: [{
    provider_key: "acme.targets", display_name: "Targets", features: ["notify"], notification_target_kind: "recipient",
  }], reactions: [{ reaction_key: "route", event_type: "job.failed", enabled: true,
    action: { type: "notify", target: "extension_channel", provider_key: "acme.targets", recipients: ["room:old"] } }] });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /^When / }));
  const input = await screen.findByRole("textbox", { name: /^Recipients$/ });
  await user.clear(input);
  await user.type(input, "room:new, user:42");
  await user.click(screen.getByRole("button", { name: "Save rules" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0][0].action).toMatchObject({ recipients: ["room:new", "user:42"] });
});


it("keeps the existing toggle path for an undeclared generic recipient provider", async () => {
  const writes = mountRules({ scopeKind: "workflow", providers: [{
    provider_key: "acme.targets", display_name: "Targets", features: ["notify"], notification_target_kind: "recipient",
  }], reactions: [{ reaction_key: "route", event_type: "job.failed", enabled: false,
    action: { type: "notify", target: "extension_channel", provider_key: "acme.targets", recipients: [] } }] });
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /^When / }));
  await screen.findByRole("textbox", { name: /^Recipients$/ });
  await user.click(screen.getByRole("switch"));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0][0]).toMatchObject({ enabled: true, action: { provider_key: "acme.targets" } });
});
