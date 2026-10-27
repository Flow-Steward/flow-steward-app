import recipientCases from "@/test/fixtures/notification-recipient-cases.json";
import {
  AUTOMATION_ACTIONS_BY_SCOPE,
  AUTOMATION_EVENT_PRESETS_BY_SCOPE,
  NOTIFY_ACTION_TYPE,
  sourceRowsFromPayload,
  NOTIFY_TARGET_EXTENSION_CHANNEL,
  localizedSTEP_KIND_OPTIONS,
  actionExtraForPayload,
  notificationRecipientsTextFromAction,
  withNotificationChannelSelection,
  type WorkflowReactionDraft,
  automationEventAllowed,
  automationRuleTemplates,
  blankReactionDraft,
  reactionDraftFromTemplate,
  reactionDraftProblems,
} from "@/pages/workflows/detail/detail-automation-core";

it("uses locale keys for all human-facing step kinds without changing their canonical values", () => {
  const t = (key: string) => `LT:${key}`;
  const options = localizedSTEP_KIND_OPTIONS(t);
  expect(options.every((option) => option.label.startsWith("LT:"))).toBe(true);
  expect(options.find((option) => option.value === "stop_success")?.label).toBe("LT:react.workflows.step_kind_stop_success");
  expect(options.find((option) => option.value === "set_variable")?.value).toBe("set_variable");
});

it.each([
  {
    scope: "workflow" as const,
    expected: [
      ["job.completed", "registry.workflow_detail.automation.event.job_completed"],
      ["job.failed", "registry.workflow_detail.automation.event.job_failed"],
      ["job.step_started", "registry.workflow_detail.automation.event.workflow_step_started"],
      ["job.step_completed", "registry.workflow_detail.automation.event.workflow_step_completed"],
      ["job.step_failed", "registry.workflow_detail.automation.event.workflow_step_failed"],
      ["job.step_blocked", "registry.workflow_detail.automation.event.workflow_step_blocked"],
      ["job.group_started", "registry.workflow_detail.automation.event.group_started"],
      ["job.group_completed", "registry.workflow_detail.automation.event.group_completed"],
      ["job.group_failed", "registry.workflow_detail.automation.event.group_failed"],
      ["job.approval_waiting", "registry.workflow_detail.automation.event.approval_waiting"],
      ["job.timer_waiting", "registry.workflow_detail.automation.event.timer_waiting"],
    ],
  },
  {
    scope: "group" as const,
    expected: [
      ["job.group_started", "registry.workflow_detail.automation.event.group_started_local"],
      ["job.group_completed", "registry.workflow_detail.automation.event.group_completed_local"],
      ["job.group_failed", "registry.workflow_detail.automation.event.group_failed_local"],
      ["job.step_started", "registry.workflow_detail.automation.event.group_step_started"],
      ["job.step_completed", "registry.workflow_detail.automation.event.group_step_completed"],
      ["job.step_failed", "registry.workflow_detail.automation.event.group_step_failed"],
      ["job.step_blocked", "registry.workflow_detail.automation.event.group_step_blocked"],
      ["job.approval_waiting", "registry.workflow_detail.automation.event.group_approval_waiting"],
      ["job.timer_waiting", "registry.workflow_detail.automation.event.group_timer_waiting"],
    ],
  },
  {
    scope: "step" as const,
    expected: [
      ["job.step_started", "registry.workflow_detail.automation.event.step_started"],
      ["job.step_completed", "registry.workflow_detail.automation.event.step_completed"],
      ["job.step_failed", "registry.workflow_detail.automation.event.step_failed"],
      ["job.step_blocked", "registry.workflow_detail.automation.event.step_blocked"],
      ["job.approval_waiting", "registry.workflow_detail.automation.event.step_approval_waiting"],
      ["job.timer_waiting", "registry.workflow_detail.automation.event.step_timer_waiting"],
    ],
  },
])("labels generated $scope fallback events with explicit locale keys", ({ scope, expected }) => {
  const rows = sourceRowsFromPayload({}, scope);
  expect(rows[0].source_id).toBe("__workflow__");
  const events = rows[0].events as Array<Record<string, unknown>>;
  expect(events.map((event) => [event.event_type, event.display_name_key])).toEqual(expected);
});

function notifyDraft(actionExtra: Record<string, unknown>, actionType = NOTIFY_ACTION_TYPE): WorkflowReactionDraft {
  return {
    actionTarget: NOTIFY_TARGET_EXTENSION_CHANNEL,
    actionType,
    actionExtra,
    conditions: {},
    debounceSeconds: "",
    displayName: "Tell ops",
    enabled: true,
    eventType: "job.failed",
    idempotencyKeyTemplate: null,
    reactionKey: "tell_ops",
    sourceId: "__workflow__",
  };
}

describe("email recipients on a notify automation", () => {
  it("saves the typed addresses as a list for an email provider", () => {
    const actionExtra = withNotificationChannelSelection(
      { provider_key: "acme.mail-provider" },
      { recipients: "ops@example.com,  anna@example.com ," },
    );

    expect(actionExtraForPayload(notifyDraft(actionExtra))).toMatchObject({
      provider_key: "acme.mail-provider",
      recipients: ["ops@example.com", "anna@example.com"],
    });
  });

  it("shows saved addresses back as one editable line", () => {
    expect(notificationRecipientsTextFromAction({ recipients: ["ops@example.com", "anna@example.com"] })).toBe(
      "ops@example.com, anna@example.com",
    );
  });

  it("drops recipients when the action is no longer a notification", () => {
    const payload = actionExtraForPayload(notifyDraft({ recipients: ["ops@example.com"] }, "cancel_job"));

    expect(payload).not.toHaveProperty("recipients");
  });

  it("clears recipients when the field is emptied", () => {
    expect(withNotificationChannelSelection({ recipients: "ops@example.com" }, { recipients: " " })).not.toHaveProperty(
      "recipients",
    );
  });
});

describe("reaction event catalog per automation scope", () => {
  const STEP_EVENTS = ["job.step_started", "job.step_completed", "job.step_failed", "job.step_blocked"];
  const GROUP_EVENTS = ["job.group_started", "job.group_completed", "job.group_failed"];
  const WAIT_EVENTS = ["job.approval_waiting", "job.timer_waiting"];

  it("offers exactly the public events each scope may use", () => {
    expect(AUTOMATION_EVENT_PRESETS_BY_SCOPE.workflow).toEqual([
      "job.completed",
      "job.failed",
      ...STEP_EVENTS,
      ...GROUP_EVENTS,
      ...WAIT_EVENTS,
    ]);
    expect(AUTOMATION_EVENT_PRESETS_BY_SCOPE.group).toEqual([...GROUP_EVENTS, ...STEP_EVENTS, ...WAIT_EVENTS]);
    expect(AUTOMATION_EVENT_PRESETS_BY_SCOPE.step).toEqual([...STEP_EVENTS, ...WAIT_EVENTS]);
    expect(Object.keys(AUTOMATION_EVENT_PRESETS_BY_SCOPE).sort()).toEqual(["group", "step", "workflow"]);
  });

  it("never offers the retired phase or gate events", () => {
    const retired = [
      "job.phase_started",
      "job.phase_completed",
      "job.phase_failed",
      "job.gate_waiting",
      "job.awaiting_approval",
      "job.awaiting_timer",
    ];
    Object.values(AUTOMATION_EVENT_PRESETS_BY_SCOPE).forEach((events) => {
      retired.forEach((event) => expect(events).not.toContain(event));
    });
  });

  it("keeps group automation to notifications and step automation to step retries", () => {
    expect(AUTOMATION_ACTIONS_BY_SCOPE.group).toEqual(["notify"]);
    expect(AUTOMATION_ACTIONS_BY_SCOPE.step).toEqual(["retry_step", "notify"]);
  });

  it("falls back to the scope catalog only when the server sends no sources", () => {
    const fallback = sourceRowsFromPayload({}, "group");
    expect((fallback[0].events as Array<{ event_type: string }>).map((event) => event.event_type)).toEqual(
      AUTOMATION_EVENT_PRESETS_BY_SCOPE.group,
    );
    const served = sourceRowsFromPayload(
      { available_trigger_sources: [{ source_id: "__workflow__", events: [{ event_type: "job.group_failed" }] }] },
      "group",
    );
    expect(served).toEqual([{ source_id: "__workflow__", events: [{ event_type: "job.group_failed" }] }]);
  });
});

describe("common rules and checks before Save rules", () => {
  it("offers approval and wait-timer events on a step only where they can happen", () => {
    expect(automationEventAllowed("job.approval_waiting", "step", "agent")).toBe(false);
    expect(automationEventAllowed("job.approval_waiting", "step", "approval")).toBe(true);
    expect(automationEventAllowed("job.timer_waiting", "step", "wait")).toBe(true);
    expect(automationEventAllowed("job.timer_waiting", "step", "approval")).toBe(false);
    expect(automationEventAllowed("job.step_failed", "step", "agent")).toBe(true);
    // A group or the workflow can hold an approval anywhere; an unknown step kind keeps every event.
    expect(automationEventAllowed("job.approval_waiting", "group", "")).toBe(true);
    expect(automationEventAllowed("job.approval_waiting", "step", "")).toBe(true);
  });

  it("fills a template's fields into an ordinary rule", () => {
    const [notifyFailed, decide] = automationRuleTemplates("workflow");
    const draft = reactionDraftFromTemplate(notifyFailed, "workflow", []);
    expect(draft).toMatchObject({ eventType: "job.failed", actionType: NOTIFY_ACTION_TYPE, actionTarget: "project_routes", actionExtra: { severity: "error" } });
    expect(reactionDraftFromTemplate(decide, "workflow", []).actionExtra).toMatchObject({ channel_capability: "human_decision" });
    expect(automationRuleTemplates("step").map((template) => template.rule.actionType)).toEqual(["retry_step", NOTIFY_ACTION_TYPE, NOTIFY_ACTION_TYPE]);
  });

  it("starts a blank rule as a failure notification of its own scope", () => {
    expect(blankReactionDraft("workflow", []).eventType).toBe("job.failed");
    expect(blankReactionDraft("group", []).eventType).toBe("job.group_failed");
    expect(blankReactionDraft("step", [], "agent")).toMatchObject({ eventType: "job.step_failed", actionTarget: "project_routes" });
  });

  it("names what keeps a notification from going anywhere", () => {
    const context = { recipientProviderKeys: new Set(["email"]), channelIdsFor: (key: string) => (key === "slack" ? ["ops"] : null) };
    const base = blankReactionDraft("workflow", []);
    expect(reactionDraftProblems(base, context)).toEqual({});
    expect(reactionDraftProblems({ ...base, actionTarget: "route_keys" }, context)).toHaveProperty("routeKeys");
    expect(reactionDraftProblems({ ...base, actionTarget: "extension_channel" }, context)).toHaveProperty("provider");
    expect(reactionDraftProblems({ ...base, actionTarget: "extension_channel", actionExtra: { provider_key: "email" } }, context)).toHaveProperty("recipients");
    expect(reactionDraftProblems({ ...base, actionTarget: "extension_channel", actionExtra: { provider_key: "slack" } }, context)).toHaveProperty("channel");
    expect(reactionDraftProblems({ ...base, actionTarget: "extension_channel", actionExtra: { provider_key: "slack", channel_id: "ops" } }, context)).toEqual({});
    expect(reactionDraftProblems({ ...base, actionType: "cancel_job", actionTarget: "current_job" }, context)).toEqual({});
  });
});


describe("declared recipient email admission parity", () => {
  const context = { recipientProviderKeys: new Set(["acme.mailer"]),
    recipientFormatFor: () => "email", channelIdsFor: () => null };
  it.each(recipientCases)("$name", ({ recipients, valid }) => {
    const draft = notifyDraft({ provider_key: "acme.mailer", recipients });
    expect(Boolean(reactionDraftProblems(draft, context).recipients)).toBe(!valid);
    if (valid) expect(actionExtraForPayload(draft).recipients).toEqual(recipients);
  });
  it("keeps generic recipient contracts and disabled drafts editable", () => {
    const draft = notifyDraft({ provider_key: "acme.mailer", recipients: "route:ops" });
    expect(reactionDraftProblems(draft, { ...context, recipientFormatFor: () => null })).toEqual({});
    expect(reactionDraftProblems({ ...draft, enabled: false }, context)).toEqual({});
    const emptyDisabled = { ...draft, enabled: false, actionExtra: { provider_key: "acme.mailer", recipients: [] } };
    expect(reactionDraftProblems(emptyDisabled, { ...context, recipientFormatFor: () => null })).toHaveProperty("recipients");
    expect(reactionDraftProblems({ ...emptyDisabled, actionTarget: "route_keys" }, context)).toHaveProperty("routeKeys");
  });
});


it("localizes the declared email error", () => {
  const t = (key: string) => `LT:${key}`;
  const draft = notifyDraft({ provider_key: "acme.mailer", recipients: "not-an-email" });
  expect(reactionDraftProblems(draft, { recipientProviderKeys: new Set(["acme.mailer"]), recipientFormatFor: () => "email", channelIdsFor: () => null }, t).recipients).toBe("LT:react.workflows.automation_email_addresses_invalid");
});
