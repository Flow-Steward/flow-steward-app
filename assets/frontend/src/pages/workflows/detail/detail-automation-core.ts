import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { SelectOption } from "@/pages/workflows/detail/detail-interface";
import { textOrEmpty } from "@/pages/workflows/detail/detail-test-run";
import { stringList } from "@/pages/workflows/detail/detail-workflow-validation";
import { trimRepeatedEdges } from "@/lib/utils/string-normalization";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import {
  commaList,
  objectOrEmpty,
  uniqueSelectOptions,
} from "@/pages/workflows/detail/detail-interface";

export type AutomationScopeKind = "workflow" | "group" | "step";

export type WorkflowReactionDraft = {
  actionTarget: string;
  actionType: string;
  actionExtra: Record<string, unknown>;
  conditions: Record<string, unknown>;
  debounceSeconds: string;
  displayName: string;
  enabled: boolean;
  eventType: string;
  idempotencyKeyTemplate: string | null;
  reactionKey: string;
  sourceId: string;
  /** Identity of a rule not saved yet, so its row keeps its place while edited. Never sent. */
  clientId?: string;
};

export type AutomationActionDefinition = {
  label: string;
  targets: SelectOption[];
};

const STEP_REACTION_EVENTS = [
  "job.step_started",
  "job.step_completed",
  "job.step_failed",
  "job.step_blocked",
];
const GROUP_REACTION_EVENTS = ["job.group_started", "job.group_completed", "job.group_failed"];
const WAIT_REACTION_EVENTS = ["job.approval_waiting", "job.timer_waiting"];

/**
 * The public reaction events each automation scope may use. The server sends
 * its own catalog with the reactions; this is the same list for when it does not.
 */
export const AUTOMATION_EVENT_PRESETS_BY_SCOPE: Record<AutomationScopeKind, string[]> =
  {
    workflow: [
      "job.completed",
      "job.failed",
      ...STEP_REACTION_EVENTS,
      ...GROUP_REACTION_EVENTS,
      ...WAIT_REACTION_EVENTS,
    ],
    group: [...GROUP_REACTION_EVENTS, ...STEP_REACTION_EVENTS, ...WAIT_REACTION_EVENTS],
    step: [...STEP_REACTION_EVENTS, ...WAIT_REACTION_EVENTS],
  };

export const AUTOMATION_ACTIONS_BY_SCOPE: Record<AutomationScopeKind, string[]> = {
  workflow: ["resume_job", "approve_job", "reject_job", "cancel_job", "notify"],
  group: ["notify"],
  step: ["retry_step", "notify"],
};

export const NOTIFY_ACTION_TYPE = "notify";
export const NOTIFY_TARGET_ALL_ROUTES = "project_routes";
export const NOTIFY_TARGET_EXTENSION_CHANNEL = "extension_channel";
export const NOTIFY_TARGET_ROUTE_KEYS = "route_keys";
export const NOTIFY_CHANNEL_CAPABILITY_NOTIFY = "notify";
export const NOTIFY_CHANNEL_CAPABILITY_HUMAN_DECISION = "human_decision";
export const NOTIFY_SEVERITY_OPTIONS = [
  "debug",
  "info",
  "warning",
  "error",
  "critical",
];

/**
 * The locale key of each built-in event label per scope, the same keys the
 * server sends. The same event reads differently per scope, so each scope has
 * its own key.
 */
const BUILTIN_EVENT_LABEL_KEYS_BY_SCOPE: Record<AutomationScopeKind, Record<string, string>> = {
  workflow: {
    "job.completed": "registry.workflow_detail.automation.event.job_completed",
    "job.failed": "registry.workflow_detail.automation.event.job_failed",
    "job.step_started": "registry.workflow_detail.automation.event.workflow_step_started",
    "job.step_completed": "registry.workflow_detail.automation.event.workflow_step_completed",
    "job.step_failed": "registry.workflow_detail.automation.event.workflow_step_failed",
    "job.step_blocked": "registry.workflow_detail.automation.event.workflow_step_blocked",
    "job.group_started": "registry.workflow_detail.automation.event.group_started",
    "job.group_completed": "registry.workflow_detail.automation.event.group_completed",
    "job.group_failed": "registry.workflow_detail.automation.event.group_failed",
    "job.approval_waiting": "registry.workflow_detail.automation.event.approval_waiting",
    "job.timer_waiting": "registry.workflow_detail.automation.event.timer_waiting",
  },
  group: {
    "job.group_started": "registry.workflow_detail.automation.event.group_started_local",
    "job.group_completed": "registry.workflow_detail.automation.event.group_completed_local",
    "job.group_failed": "registry.workflow_detail.automation.event.group_failed_local",
    "job.step_started": "registry.workflow_detail.automation.event.group_step_started",
    "job.step_completed": "registry.workflow_detail.automation.event.group_step_completed",
    "job.step_failed": "registry.workflow_detail.automation.event.group_step_failed",
    "job.step_blocked": "registry.workflow_detail.automation.event.group_step_blocked",
    "job.approval_waiting": "registry.workflow_detail.automation.event.group_approval_waiting",
    "job.timer_waiting": "registry.workflow_detail.automation.event.group_timer_waiting",
  },
  step: {
    "job.step_started": "registry.workflow_detail.automation.event.step_started",
    "job.step_completed": "registry.workflow_detail.automation.event.step_completed",
    "job.step_failed": "registry.workflow_detail.automation.event.step_failed",
    "job.step_blocked": "registry.workflow_detail.automation.event.step_blocked",
    "job.approval_waiting": "registry.workflow_detail.automation.event.step_approval_waiting",
    "job.timer_waiting": "registry.workflow_detail.automation.event.step_timer_waiting",
  },
};

export function localizedAUTOMATION_ACTION_DEFINITIONS(t: I18nContextValue["t"] = fallbackContext.t): Record<
  string,
  AutomationActionDefinition
> {
  return {
  resume_job: {
    label: t("react.workflows.resume_job", "Resume job"),
    targets: [{ value: "current_job", label: t("react.workflows.current_job", "Current job") }],
  },
  approve_job: {
    label: t("react.workflows.approve_job", "Approve job"),
    targets: [{ value: "current_job", label: t("react.workflows.current_job", "Current job") }],
  },
  reject_job: {
    label: t("react.workflows.reject_job", "Reject job"),
    targets: [{ value: "current_job", label: t("react.workflows.current_job", "Current job") }],
  },
  cancel_job: {
    label: t("react.workflows.cancel_job", "Cancel job"),
    targets: [{ value: "current_job", label: t("react.workflows.current_job", "Current job") }],
  },
  retry_step: {
    label: t("react.workflows.retry_step", "Retry step"),
    targets: [{ value: "interrupted_step", label: t("react.workflows.interrupted_step", "Interrupted step") }],
  },
  notify: {
    label: t("react.workflows.send_notification", "Send notification"),
    targets: [
      { value: NOTIFY_TARGET_ALL_ROUTES, label: t("react.workflows.default_matching_channels", "Default matching channels") },
      { value: NOTIFY_TARGET_EXTENSION_CHANNEL, label: t("react.workflows.specific_extension_channel", "Specific extension channel") },
      { value: NOTIFY_TARGET_ROUTE_KEYS, label: t("react.workflows.route_keys_i_enter", "Route keys I enter") },
    ],
  },
};
}

export const AUTOMATION_ACTION_DEFINITIONS: Record<
  string,
  AutomationActionDefinition
> = localizedAUTOMATION_ACTION_DEFINITIONS();

export function localizedSTEP_KIND_OPTIONS(t: I18nContextValue["t"] = fallbackContext.t): SelectOption[] {
  return [
  { value: "agent", label: t("react.workflows.step_kind_agent", "Agent") },
  { value: "connector", label: t("react.workflows.step_kind_connector", "Action step") },
  { value: "dataset_query", label: t("react.workflows.step_kind_dataset_query", "DataSet") },
  { value: "invoke_workflow", label: t("react.workflows.step_kind_invoke_workflow", "Invoke workflow") },
  { value: "if", label: t("react.workflows.step_kind_if", "If") },
  { value: "switch", label: t("react.workflows.step_kind_switch", "Switch") },
  { value: "approval", label: t("react.workflows.step_kind_approval", "Approval") },
  { value: "wait", label: t("react.workflows.step_kind_wait", "Wait") },
  { value: "stop_success", label: t("react.workflows.step_kind_stop_success", "Stop · success") },
  { value: "stop_fail", label: t("react.workflows.step_kind_stop_fail", "Stop · fail") },
  { value: "set_variable", label: t("react.workflows.step_kind_set_variable", "Set variable") },
  { value: "collect", label: t("react.workflows.step_kind_collect", "Collect") },
  { value: "continue", label: t("react.workflows.step_kind_continue", "Continue") },
  { value: "break", label: t("react.workflows.step_kind_break", "Break") },
  { value: "batch", label: t("react.workflows.step_kind_batch", "Batch") },
  { value: "http_request", label: t("react.workflows.step_kind_http_request", "HTTP request") },
  { value: "transform", label: t("react.workflows.step_kind_transform", "Transform") },
  { value: "tabular_feed", label: t("react.workflows.step_kind_tabular_feed", "Tabular feed") },
  { value: "file_from_base64", label: t("react.workflows.step_kind_file_from_base64", "File from base64") },
  { value: "template", label: t("react.workflows.step_kind_template", "Template") },

];
}

export const STEP_KIND_OPTIONS: SelectOption[] = localizedSTEP_KIND_OPTIONS();

export type NewStepDrawerDefaults = {
  agentId?: string;
  displayName?: string;
  override?: string;
  stepKind?: string;
};

export function localizedIF_OPERATOR_OPTIONS(t: I18nContextValue["t"] = fallbackContext.t): SelectOption[] {
  return [
  { value: "always_true", label: t("react.workflows.always_true", "always true") },
  { value: "always_false", label: t("react.workflows.always_false", "always false") },
  { value: "equals", label: t("react.workflows.equals", "equals") },
  { value: "not_equals", label: t("react.workflows.not_equals", "not equals") },
  { value: "exists", label: t("react.workflows.exists", "exists") },
  { value: "truthy", label: t("react.workflows.truthy", "truthy") },
  { value: "gt", label: t("react.workflows.greater_than", "greater than") },
  { value: "gte", label: t("react.workflows.greater_than_or_equal", "greater than or equal") },
  { value: "lt", label: t("react.workflows.less_than", "less than") },
  { value: "lte", label: t("react.workflows.less_than_or_equal", "less than or equal") },
  { value: "contains", label: t("react.workflows.contains", "contains") },
  { value: "in", label: t("react.workflows.in_list", "in list") },
  { value: "not_in", label: t("react.workflows.not_in_list", "not in list") },
];
}

export const IF_OPERATOR_OPTIONS: SelectOption[] = localizedIF_OPERATOR_OPTIONS();

export const IF_OPERATORS_WITH_VALUE = new Set([
  "equals",
  "not_equals",
  "gt",
  "gte",
  "lt",
  "lte",
  "contains",
  "in",
  "not_in",
]);
export const IF_CONSTANT_OPERATORS = new Set(["always_true", "always_false"]);


export const connectorLikeStepKinds = new Set(["connector"]);

export function localizedWORKFLOW_INTERFACE_TRIGGER_CHANNELS(t: I18nContextValue["t"] = fallbackContext.t): SelectOption[] {
  return [
  { value: "ui", label: "UI" },
  { value: "api", label: "API" },
  { value: "mcp", label: "MCP" },
  { value: "webhook", label: t("react.workflows.incoming_webhook", "Incoming Webhook") },
  { value: "schedule", label: t("project_detail.scheduled.fields.schedule", "Schedule") },
  { value: "parent_workflow", label: t("react.workflows.parent_workflow", "Parent workflow") },
];
}

export const WORKFLOW_INTERFACE_TRIGGER_CHANNELS: SelectOption[] = localizedWORKFLOW_INTERFACE_TRIGGER_CHANNELS();

export function localizedWORKFLOW_INTERFACE_INPUT_MODES(t: I18nContextValue["t"] = fallbackContext.t): SelectOption[] {
  return [
  { value: "fields", label: t("react.workflows.form_fields", "Form fields") },
  { value: "json_schema", label: t("react.workflows.custom_json_schema", "Custom JSON schema") },
  { value: "passthrough", label: t("react.workflows.raw_json_payload", "Raw JSON payload") },
];
}

export const WORKFLOW_INTERFACE_INPUT_MODES: SelectOption[] = localizedWORKFLOW_INTERFACE_INPUT_MODES();

export function localizedWORKFLOW_INTERFACE_FIELD_TYPES(t: I18nContextValue["t"] = fallbackContext.t): SelectOption[] {
  return [
  { value: "string", label: t("react.workflows.string", "string") },
  { value: "paragraph", label: t("react.workflows.paragraph", "paragraph") },
  { value: "number", label: t("react.workflows.number", "number") },
  { value: "boolean", label: t("react.workflows.boolean", "boolean") },
  { value: "select", label: t("react.extensions.select", "select") },
  { value: "object", label: t("react.workflows.object", "object") },
  { value: "array", label: t("react.workflows.array", "array") },
  { value: "file", label: t("react.workflows.file", "file") },
  { value: "secret", label: t("react.workflows.secret", "secret") },
];
}

export const WORKFLOW_INTERFACE_FIELD_TYPES: SelectOption[] = localizedWORKFLOW_INTERFACE_FIELD_TYPES();


export function sourceRowsFromPayload(
  payload: unknown,
  scopeKind: AutomationScopeKind,
): Array<Record<string, unknown>> {
  const row = objectOrEmpty(payload);
  const sources = Array.isArray(row.available_trigger_sources)
    ? (row.available_trigger_sources as Array<Record<string, unknown>>).filter(
        (item) =>
          Boolean(item) && typeof item === "object" && !Array.isArray(item),
      )
    : [];
  if (sources.length) {
    return sources;
  }
  return [
    {
      source_id: "__workflow__",
      display_name: "Workflow runtime",
      display_name_key: "react.workflows.workflow_runtime",
      events: AUTOMATION_EVENT_PRESETS_BY_SCOPE[scopeKind].map((eventType) => ({
        event_type: eventType,
        display_name: eventType,
        display_name_key: BUILTIN_EVENT_LABEL_KEYS_BY_SCOPE[scopeKind][eventType],
      })),
    },
  ];
}

export function eventOptionsForSource(
  sources: Array<Record<string, unknown>>,
  sourceId: string,
  currentEventType = "",
  t: I18nContextValue["t"] = fallbackContext.t,
): SelectOption[] {
  const normalizedSourceId = sourceId || "__workflow__";
  const source =
    sources.find((row) => textOrEmpty(row.source_id) === normalizedSourceId) ||
    sources[0] ||
    {};
  const events = Array.isArray(source.events)
    ? (source.events as Array<Record<string, unknown>>)
    : [];
  const options = events
    .map((event) => ({
      value: textOrEmpty(event.event_type),
      label: textOrEmpty(event.display_name_key)
        ? t(textOrEmpty(event.display_name_key), textOrEmpty(event.display_name) || textOrEmpty(event.event_type))
        : textOrEmpty(event.display_name) || textOrEmpty(event.event_type),
    }))
    .filter((option) => Boolean(option.value));
  if (
    currentEventType &&
    !options.some((option) => option.value === currentEventType)
  ) {
    options.push({ value: currentEventType, label: currentEventType });
  }
  return uniqueSelectOptions(options);
}

export function sourceOptionsFromRows(
  sources: Array<Record<string, unknown>>, t: I18nContextValue["t"] = fallbackContext.t
): SelectOption[] {
  return uniqueSelectOptions(
    sources.map((source) => ({
      value: textOrEmpty(source.source_id) || "__workflow__",
      label: textOrEmpty(source.display_name_key)
        ? t(textOrEmpty(source.display_name_key), textOrEmpty(source.display_name) || textOrEmpty(source.source_id))
        : textOrEmpty(source.display_name) ||
        textOrEmpty(source.source_id) ||
        t("react.workflows.workflow_runtime", "Workflow runtime"),
    })),
  );
}

export function actionTypeFromRule(
  rule: Record<string, unknown>,
  scopeKind: AutomationScopeKind,
): string {
  const action = objectOrEmpty(rule.action);
  return (
    textOrEmpty(action.type) ||
    textOrEmpty(rule.reaction_type) ||
    AUTOMATION_ACTIONS_BY_SCOPE[scopeKind][0] ||
    "notify"
  );
}

export function targetOptionsForAction(
  actionType: string,
  currentTarget = "", t: I18nContextValue["t"] = fallbackContext.t
): SelectOption[] {
  return uniqueSelectOptions([
    ...(localizedAUTOMATION_ACTION_DEFINITIONS(t)[actionType]?.targets ?? []),
    ...(currentTarget ? [{ value: currentTarget, label: currentTarget }] : []),
  ]);
}

export function routeKeysFromAction(action: Record<string, unknown>): string[] {
  const value = action.route_keys ?? action.route_key ?? "";
  if (Array.isArray(value)) {
    return stringList(value);
  }
  return commaList(textOrEmpty(value));
}

export function routeKeysTextFromAction(action: Record<string, unknown>): string {
  return routeKeysFromAction(action).join(", ");
}

export function withNotificationRouteKeys(
  actionExtra: Record<string, unknown>,
  routeKeysText: string,
): Record<string, unknown> {
  const next = { ...actionExtra };
  const routeKeys = commaList(routeKeysText);
  delete next.route_key;
  if (routeKeys.length) {
    next.route_keys = routeKeys;
  } else {
    delete next.route_keys;
  }
  return next;
}

export function withNotificationField(
  actionExtra: Record<string, unknown>,
  field: "message" | "severity",
  value: string,
): Record<string, unknown> {
  const next = { ...actionExtra };
  const normalized = field === "message" ? value : textOrEmpty(value);
  if (normalized) {
    next[field] = normalized;
  } else {
    delete next[field];
  }
  return next;
}

export function notificationChannelCapabilityFromAction(
  actionExtra: Record<string, unknown>,
): string {
  const normalized = textOrEmpty(actionExtra.channel_capability).toLowerCase();
  return normalized === NOTIFY_CHANNEL_CAPABILITY_HUMAN_DECISION
    ? NOTIFY_CHANNEL_CAPABILITY_HUMAN_DECISION
    : NOTIFY_CHANNEL_CAPABILITY_NOTIFY;
}

/** Email addresses an automation sends to, from a list or comma-separated text. */
export function notificationRecipientsFromAction(actionExtra: Record<string, unknown>): string[] {
  const raw = actionExtra.recipients;
  const items = Array.isArray(raw) ? raw : textOrEmpty(raw).split(",");
  return items.map((item) => textOrEmpty(item)).filter(Boolean);
}

export function notificationRecipientsTextFromAction(actionExtra: Record<string, unknown>): string {
  const raw = actionExtra.recipients;
  return Array.isArray(raw) ? raw.map((item) => textOrEmpty(item)).join(", ") : typeof raw === "string" ? raw : "";
}

export function withNotificationChannelSelection(
  actionExtra: Record<string, unknown>,
  patch: {
    channelCapability?: string;
    channelId?: string;
    providerKey?: string;
    /** Email addresses, comma-separated, for an email provider. */
    recipients?: string;
  },
): Record<string, unknown> {
  const next = { ...actionExtra };
  if (patch.recipients !== undefined) {
    if (patch.recipients.trim()) {
      next.recipients = patch.recipients;
    } else {
      delete next.recipients;
    }
  }
  if (patch.providerKey !== undefined) {
    const providerKey = textOrEmpty(patch.providerKey);
    if (providerKey) {
      next.provider_key = providerKey;
    } else {
      delete next.provider_key;
    }
  }
  if (patch.channelId !== undefined) {
    const channelId = textOrEmpty(patch.channelId);
    if (channelId) {
      next.channel_id = channelId;
    } else {
      delete next.channel_id;
    }
  }
  if (patch.channelCapability !== undefined) {
    const channelCapability = textOrEmpty(patch.channelCapability).toLowerCase();
    if (channelCapability) {
      next.channel_capability = channelCapability;
    } else {
      delete next.channel_capability;
    }
  }
  return next;
}

export function actionExtraForPayload(
  draft: WorkflowReactionDraft,
): Record<string, unknown> {
  const next = { ...draft.actionExtra };
  delete next.type;
  delete next.target;
  if (draft.actionType !== NOTIFY_ACTION_TYPE) {
    delete next.route_key;
    delete next.route_keys;
    delete next.provider_key;
    delete next.channel_id;
    delete next.channel_capability;
    delete next.recipients;
    delete next.message;
    delete next.severity;
    return next;
  }
  next.severity = textOrEmpty(next.severity) || "info";
  if (draft.actionTarget === NOTIFY_TARGET_ROUTE_KEYS) {
    const routeKeys = routeKeysFromAction(next);
    if (routeKeys.length) {
      next.route_keys = routeKeys;
    } else {
      delete next.route_keys;
    }
    delete next.route_key;
    delete next.provider_key;
    delete next.channel_id;
    delete next.channel_capability;
    delete next.recipients;
  } else if (draft.actionTarget === NOTIFY_TARGET_EXTENSION_CHANNEL) {
    delete next.route_key;
    delete next.route_keys;
    const providerKey = textOrEmpty(next.provider_key);
    const channelId = textOrEmpty(next.channel_id);
    const recipients = notificationRecipientsFromAction(next);
    if (!providerKey) {
      delete next.provider_key;
    }
    if (!channelId) {
      delete next.channel_id;
    }
    if (recipients.length) {
      next.recipients = recipients;
    } else {
      delete next.recipients;
    }
    next.channel_capability = notificationChannelCapabilityFromAction(next);
  } else {
    delete next.route_key;
    delete next.route_keys;
    delete next.provider_key;
    delete next.channel_id;
    delete next.channel_capability;
    delete next.recipients;
  }
  if (!textOrEmpty(next.message)) {
    delete next.message;
  }
  return next;
}

export function actionTargetFromRule(
  rule: Record<string, unknown>,
  scopeKind: AutomationScopeKind, t: I18nContextValue["t"] = fallbackContext.t
): string {
  const action = objectOrEmpty(rule.action);
  const actionType = actionTypeFromRule(rule, scopeKind);
  return (
    textOrEmpty(action.target) ||
    textOrEmpty(rule.target) ||
    targetOptionsForAction(actionType, undefined, t)[0]?.value ||
    ""
  );
}

export function draftFromReactionRule(
  value: unknown,
  scopeKind: AutomationScopeKind,
  sources: Array<Record<string, unknown>>, t: I18nContextValue["t"] = fallbackContext.t
): WorkflowReactionDraft {
  const rule = objectOrEmpty(value);
  const conditions = objectOrEmpty(rule.conditions);
  const sourceId = textOrEmpty(conditions.source) || "__workflow__";
  const eventOptions = eventOptionsForSource(
    sources,
    sourceId,
    textOrEmpty(rule.event_type),
  );
  const action = objectOrEmpty(rule.action);
  return {
    actionExtra: action,
    actionTarget: actionTargetFromRule(rule, scopeKind, t),
    actionType: actionTypeFromRule(rule, scopeKind),
    conditions,
    debounceSeconds: textOrEmpty(rule.debounce_seconds) || "0",
    displayName: textOrEmpty(rule.display_name),
    enabled: rule.enabled === undefined ? true : Boolean(rule.enabled),
    eventType: textOrEmpty(rule.event_type) || eventOptions[0]?.value || "",
    idempotencyKeyTemplate: typeof rule.idempotency_key_template === "string" ? rule.idempotency_key_template : null,
    reactionKey: textOrEmpty(rule.reaction_key),
    sourceId,
  };
}

export function defaultReactionDraft(
  scopeKind: AutomationScopeKind,
  sources: Array<Record<string, unknown>>, t: I18nContextValue["t"] = fallbackContext.t
): WorkflowReactionDraft {
  const sourceId = textOrEmpty(sources[0]?.source_id) || "__workflow__";
  const eventType =
    eventOptionsForSource(sources, sourceId)[0]?.value ||
    AUTOMATION_EVENT_PRESETS_BY_SCOPE[scopeKind][0] ||
    "job.completed";
  const actionType = AUTOMATION_ACTIONS_BY_SCOPE[scopeKind][0] || "notify";
  return {
    actionExtra: {},
    actionTarget: targetOptionsForAction(actionType, undefined, t)[0]?.value || "",
    actionType,
    conditions: {},
    debounceSeconds: "0",
    displayName: "",
    enabled: true,
    eventType,
    idempotencyKeyTemplate: null,
    reactionKey: "",
    sourceId,
  };
}

export function reactionKeyFromDraft(
  draft: WorkflowReactionDraft,
  index: number,
): string {
  const raw =
    draft.reactionKey ||
    `${draft.eventType}_${draft.actionType}_${draft.actionTarget || index + 1}`;
  const token = raw
    .trim()
    .toLowerCase()
    .replaceAll(/[^a-z0-9]+/g, "_");
  const normalized = trimRepeatedEdges(token, "_");
  return normalized || `reaction_${index + 1}`;
}

export function reactionPayloadFromDraft(
  draft: WorkflowReactionDraft,
  index: number,
): Record<string, unknown> {
  const sourceId =
    draft.sourceId && draft.sourceId !== "__workflow__" ? draft.sourceId : "";
  const debounceSeconds = Number.parseInt(draft.debounceSeconds || "0", 10);
  const conditions = {
    ...draft.conditions,
    ...(sourceId ? { source: sourceId } : {}),
  };
  if (!sourceId) {
    delete conditions.source;
  }
  return {
    reaction_key: reactionKeyFromDraft(draft, index),
    display_name:
      draft.displayName.trim() || `${draft.eventType} -> ${draft.actionType}`,
    event_type: draft.eventType,
    conditions,
    action: {
      ...actionExtraForPayload(draft),
      type: draft.actionType,
      target: draft.actionTarget,
    },
    debounce_seconds: Number.isFinite(debounceSeconds)
      ? Math.max(0, debounceSeconds)
      : 0,
    enabled: draft.enabled,
    idempotency_key_template: draft.idempotencyKeyTemplate,
  };
}

/** The run pauses at an approval or a wait timer only on those steps: elsewhere these events never fire. */
const STEP_KIND_ONLY_EVENTS: Record<string, string> = {
  "job.approval_waiting": "approval",
  "job.timer_waiting": "wait",
};

/**
 * Whether a step-scoped rule may react to an event. An approval waits only on
 * an approval step and a wait timer starts only on a wait step; an unknown kind
 * keeps every event, so a caller that does not say what the step is loses nothing.
 */
export function automationEventAllowed(eventType: string, scopeKind: AutomationScopeKind, stepKind = ""): boolean {
  if (scopeKind !== "step" || !stepKind) return true;
  const onlyOn = STEP_KIND_ONLY_EVENTS[eventType];
  return !onlyOn || onlyOn === stepKind;
}

/** The events a rule of this scope can choose; a rule already saved on another event keeps it. */
export function automationEventOptions(
  sources: Array<Record<string, unknown>>,
  sourceId: string,
  currentEventType: string,
  scopeKind: AutomationScopeKind,
  stepKind = "",
  t: I18nContextValue["t"] = fallbackContext.t,
): SelectOption[] {
  return eventOptionsForSource(sources, sourceId, currentEventType, t).filter(
    (option) => option.value === currentEventType || automationEventAllowed(option.value, scopeKind, stepKind),
  );
}

type AutomationRuleShape = {
  eventType: string;
  actionType: string;
  actionTarget: string;
  severity?: string;
  channelCapability?: string;
};

export type AutomationRuleTemplate = {
  id: string;
  label: string;
  help: string;
  rule: AutomationRuleShape;
};

/** A blank rule starts as something useful: a notification when the scope fails. */
const BLANK_RULE_BY_SCOPE: Record<AutomationScopeKind, AutomationRuleShape> = {
  workflow: { eventType: "job.failed", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_ALL_ROUTES, severity: "info" },
  group: { eventType: "job.group_failed", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_ALL_ROUTES, severity: "info" },
  step: { eventType: "job.step_failed", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_ALL_ROUTES, severity: "info" },
};

/** Common rules: each is an ordinary rule with its fields filled in, editable after it is added. */
export function automationRuleTemplates(
  scopeKind: AutomationScopeKind,
  t: I18nContextValue["t"] = fallbackContext.t,
  stepKind = "",
): AutomationRuleTemplate[] {
  const errorToRoutes = t("react.workflows.automation_template_error_routes", "Error to matching channels.");
  const decision = t("react.workflows.automation_template_decision_help", "Human decision in a channel you pick.");
  const byScope: Record<AutomationScopeKind, AutomationRuleTemplate[]> = {
    workflow: [
      { id: "notify_failed", label: t("react.workflows.automation_template_notify_run_failed", "Notify when a run fails"), help: errorToRoutes,
        rule: { eventType: "job.failed", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_ALL_ROUTES, severity: "error" } },
      { id: "decide_approval", label: t("react.workflows.automation_template_decide_approval", "Ask for a decision when an approval is waiting"), help: decision,
        rule: { eventType: "job.approval_waiting", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_EXTENSION_CHANNEL, channelCapability: NOTIFY_CHANNEL_CAPABILITY_HUMAN_DECISION } },
      { id: "notify_completed", label: t("react.workflows.automation_template_notify_run_completed", "Notify when a run completes"), help: t("react.workflows.automation_template_info_routes", "Info to matching channels."),
        rule: { eventType: "job.completed", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_ALL_ROUTES, severity: "info" } },
      { id: "cancel_blocked", label: t("react.workflows.automation_template_cancel_blocked", "Cancel the run when a step is blocked"), help: t("react.workflows.automation_template_cancel_blocked_help", "Stops the job instead of waiting."),
        rule: { eventType: "job.step_blocked", actionType: "cancel_job", actionTarget: "current_job" } },
    ],
    group: [
      { id: "notify_group_failed", label: t("react.workflows.automation_template_notify_group_failed", "Notify when this group fails"), help: errorToRoutes,
        rule: { eventType: "job.group_failed", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_ALL_ROUTES, severity: "error" } },
      { id: "notify_group_completed", label: t("react.workflows.automation_template_notify_group_completed", "Notify when this group completes"), help: t("react.workflows.automation_template_info_routes", "Info to matching channels."),
        rule: { eventType: "job.group_completed", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_ALL_ROUTES, severity: "info" } },
      { id: "decide_group_approval", label: t("react.workflows.automation_template_decide_group_approval", "Ask for a decision when an approval in this group is waiting"), help: decision,
        rule: { eventType: "job.approval_waiting", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_EXTENSION_CHANNEL, channelCapability: NOTIFY_CHANNEL_CAPABILITY_HUMAN_DECISION } },
    ],
    step: [
      { id: "retry_step_failed", label: t("react.workflows.automation_template_retry_step_failed", "Retry when this step fails"), help: t("react.workflows.automation_template_retry_step_help", "Runs the interrupted step again."),
        rule: { eventType: "job.step_failed", actionType: "retry_step", actionTarget: "interrupted_step" } },
      { id: "notify_step_failed", label: t("react.workflows.automation_template_notify_step_failed", "Notify when this step fails"), help: errorToRoutes,
        rule: { eventType: "job.step_failed", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_ALL_ROUTES, severity: "error" } },
      { id: "notify_step_blocked", label: t("react.workflows.automation_template_notify_step_blocked", "Notify when this step is blocked"), help: t("react.workflows.automation_template_warning_routes", "Warning to matching channels."),
        rule: { eventType: "job.step_blocked", actionType: NOTIFY_ACTION_TYPE, actionTarget: NOTIFY_TARGET_ALL_ROUTES, severity: "warning" } },
    ],
  };
  return byScope[scopeKind].filter((template) => automationEventAllowed(template.rule.eventType, scopeKind, stepKind));
}

function draftFromRuleShape(
  shape: AutomationRuleShape,
  scopeKind: AutomationScopeKind,
  sources: Array<Record<string, unknown>>,
  stepKind: string,
  t: I18nContextValue["t"],
): WorkflowReactionDraft {
  const draft = defaultReactionDraft(scopeKind, sources, t);
  const events = automationEventOptions(sources, draft.sourceId, "", scopeKind, stepKind, t);
  let actionExtra: Record<string, unknown> = {};
  if (shape.actionType === NOTIFY_ACTION_TYPE) {
    actionExtra = withNotificationField(actionExtra, "severity", shape.severity || "info");
    if (shape.channelCapability) actionExtra = withNotificationChannelSelection(actionExtra, { channelCapability: shape.channelCapability });
  }
  return {
    ...draft,
    actionExtra,
    actionTarget: shape.actionTarget,
    actionType: shape.actionType,
    // A source whose catalog lacks the event keeps its own first event; with no catalog the rule's own event stands.
    eventType: !events.length || events.some((option) => option.value === shape.eventType) ? shape.eventType : events[0].value,
  };
}

/** A rule added from Common rules: the template's fields, then edited like any other rule. */
export function reactionDraftFromTemplate(
  template: AutomationRuleTemplate,
  scopeKind: AutomationScopeKind,
  sources: Array<Record<string, unknown>>,
  stepKind = "",
  t: I18nContextValue["t"] = fallbackContext.t,
): WorkflowReactionDraft {
  return draftFromRuleShape(template.rule, scopeKind, sources, stepKind, t);
}

/** Blank rule: chooses when it runs and what it does, starting from a failure notification. */
export function blankReactionDraft(
  scopeKind: AutomationScopeKind,
  sources: Array<Record<string, unknown>>,
  stepKind = "",
  t: I18nContextValue["t"] = fallbackContext.t,
): WorkflowReactionDraft {
  return draftFromRuleShape(BLANK_RULE_BY_SCOPE[scopeKind], scopeKind, sources, stepKind, t);
}

export type ReactionDraftProblems = Partial<Record<"event" | "action" | "routeKeys" | "recipients" | "provider" | "channel", string>>;

function validLiteralEmailRecipient(value: unknown): boolean {
  if (typeof value !== "string") return false;
  // Existing expression-shaped entries defer literal validation only.
  if (value.includes("{{")) return true;
  if (value.includes("\r") || value.includes("\n")) return false;
  if (!value.trim()) return true; // Existing CSV/list normalization ignores empty entries.
  const input = document.createElement("input");
  input.type = "email";
  input.required = true;
  input.value = value.trim();
  return input.validity.valid;
}

/**
 * What keeps a rule from doing anything once saved: the checks run before Save
 * rules. A notification needs somewhere to go: route keys, email addresses, an
 * app, or a channel when the app has channels to pick from.
 */
export function reactionDraftProblems(
  draft: WorkflowReactionDraft,
  context: { recipientProviderKeys: ReadonlySet<string>; recipientFormatFor?: (providerKey: string) => string | null; channelIdsFor: (providerKey: string) => string[] | null },
  t: I18nContextValue["t"] = fallbackContext.t,
): ReactionDraftProblems {
  const problems: ReactionDraftProblems = {};
  if (!textOrEmpty(draft.eventType)) problems.event = t("react.workflows.automation_choose_event", "Choose when this rule runs.");
  if (!textOrEmpty(draft.actionType)) problems.action = t("react.workflows.automation_choose_action", "Choose what this rule does.");
  if (draft.actionType !== NOTIFY_ACTION_TYPE) return problems;
  if (draft.actionTarget === NOTIFY_TARGET_ROUTE_KEYS && !routeKeysFromAction(draft.actionExtra).length) {
    problems.routeKeys = t("react.workflows.automation_route_keys_required", "Enter at least one route key.");
  }
  if (draft.actionTarget === NOTIFY_TARGET_EXTENSION_CHANNEL) {
    const providerKey = textOrEmpty(draft.actionExtra.provider_key);
    if (!providerKey) {
      problems.provider = t("react.workflows.automation_app_required", "Choose an app.");
    } else if (context.recipientProviderKeys.has(providerKey)) {
      const emailFormat = context.recipientFormatFor?.(providerKey) === "email";
      if (emailFormat && !draft.enabled) return problems;
      if (!notificationRecipientsFromAction(draft.actionExtra).length) {
        problems.recipients = emailFormat
          ? t("react.workflows.automation_recipients_required", "Enter at least one email address.")
          : t("react.workflows.automation_generic_recipients_required", "Enter at least one recipient.");
      } else if (emailFormat) {
        const raw = draft.actionExtra.recipients;
        const values = Array.isArray(raw) ? raw : stringFromUnknownValue(raw).split(",");
        if (!values.every(validLiteralEmailRecipient)) {
          problems.recipients = t("react.workflows.automation_email_addresses_invalid", "Enter valid email addresses, separated by commas.");
        }
      }
    } else {
      const channels = context.channelIdsFor(providerKey);
      if (channels?.length && !channels.includes(textOrEmpty(draft.actionExtra.channel_id))) {
        problems.channel = t("react.workflows.automation_channel_required", "Choose a channel.");
      }
    }
  }
  return problems;
}

/** Comparable form of a rule list: what Save rules would send. */
export function reactionDraftsFingerprint(drafts: WorkflowReactionDraft[]): string {
  return JSON.stringify(drafts.map((draft, index) => reactionPayloadFromDraft(draft, index)));
}
