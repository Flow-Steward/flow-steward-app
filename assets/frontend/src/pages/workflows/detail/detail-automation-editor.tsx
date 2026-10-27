import { Button } from "@/components/ui/button";
import { useI18n } from "@/lib/i18n/useI18n";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { ChevronDown, Plus, Zap } from "lucide-react";
import { useInRouterContext, useSearchParams } from "react-router";

import { useMutation, useQueries, useQuery } from "@tanstack/react-query";
import { DsLoader } from "@/components/shared/ds-loader";
import { useLoaderDelay } from "@/components/shared/use-loader-delay";
import { DsAccordion } from "@/components/shared/ds-accordion";
import { DsSwitch } from "@/components/shared/ds-switch";
import { DsBadge, DsRadio } from "@/components/shared/design-system";
import { DsPopover, type PopoverTriggerProps } from "@/components/shared/popover";

import {
  fetchProjectNotificationProviders,
  fetchProjectProviderChannels,
  type ProjectNotificationProvider,
  type ProviderChannel,
} from "@/pages/projects/notifications/api";
import {
  fetchWorkflowReactions,
  updateWorkflowReactions,
} from "@/pages/workflows/shared/reactions-api";
import {
  AUTOMATION_ACTIONS_BY_SCOPE,
  NOTIFY_ACTION_TYPE,
  NOTIFY_CHANNEL_CAPABILITY_HUMAN_DECISION,
  NOTIFY_CHANNEL_CAPABILITY_NOTIFY,
  NOTIFY_SEVERITY_OPTIONS,
  NOTIFY_TARGET_ALL_ROUTES,
  NOTIFY_TARGET_EXTENSION_CHANNEL,
  NOTIFY_TARGET_ROUTE_KEYS,
  automationEventOptions,
  automationRuleTemplates,
  blankReactionDraft,
  draftFromReactionRule,
  reactionDraftFromTemplate,
  reactionDraftProblems,
  reactionDraftsFingerprint,
  reactionPayloadFromDraft,
  notificationChannelCapabilityFromAction,
  notificationRecipientsFromAction,
  notificationRecipientsTextFromAction,
  routeKeysFromAction,
  routeKeysTextFromAction,
  sourceOptionsFromRows,
  sourceRowsFromPayload,
  targetOptionsForAction,
  withNotificationChannelSelection,
  withNotificationField,
  withNotificationRouteKeys,
  type AutomationRuleTemplate,
  type AutomationScopeKind,
  type ReactionDraftProblems,
  type WorkflowReactionDraft,
  localizedAUTOMATION_ACTION_DEFINITIONS,
} from "@/pages/workflows/detail/detail-automation-core";
import { uniqueSelectOptions } from "@/pages/workflows/detail/detail-interface";
import { textOrEmpty } from "@/pages/workflows/detail/detail-test-run";
import { DrawerFooter, DrawerFooterSpacer } from "./detail-drawer-footer";
import { SaveStateButton } from "./detail-save-button";

/** How long "Rule removed" offers Undo. */
const REMOVED_UNDO_MS = 6000;

function actionExtraForTarget(
  draft: WorkflowReactionDraft,
  nextTarget: string,
): WorkflowReactionDraft["actionExtra"] {
  if (draft.actionType !== NOTIFY_ACTION_TYPE) {
    return draft.actionExtra;
  }
  if (nextTarget === NOTIFY_TARGET_ROUTE_KEYS) {
    return withNotificationChannelSelection(draft.actionExtra, {
      providerKey: "",
      channelId: "",
    });
  }
  if (nextTarget === NOTIFY_TARGET_EXTENSION_CHANNEL) {
    return withNotificationRouteKeys(draft.actionExtra, "");
  }
  return withNotificationChannelSelection(
    withNotificationRouteKeys(draft.actionExtra, ""),
    { providerKey: "", channelId: "" },
  );
}

function indexedId(index: number, base: string): string {
  return index === 0 ? base : `${base}-${index}`;
}

function renderAddRuleButton(props: PopoverTriggerProps, label: string): ReactNode {
  return (
    <Button {...props} size="sm" type="button" variant="secondary">
      <Plus aria-hidden="true" />
      {label}
      <ChevronDown aria-hidden="true" />
    </Button>
  );
}

function renderWhen(condition: boolean, content: ReactNode): ReactNode {
  return condition ? content : null;
}

function actionExtraForType(draft: WorkflowReactionDraft, nextActionType: string) {
  if (nextActionType !== NOTIFY_ACTION_TYPE) return draft.actionExtra;
  return withNotificationField(
    draft.actionExtra,
    "severity",
    textOrEmpty(draft.actionExtra.severity) || "info",
  );
}

function channelSupportsCapability(channel: ProviderChannel, capability: string): boolean {
  if (capability === NOTIFY_CHANNEL_CAPABILITY_HUMAN_DECISION) {
    return channel.enabled && channel.capabilities.human_decision;
  }
  return channel.enabled && channel.capabilities.notify;
}

function lowerFirst(value: string): string {
  return value ? value.charAt(0).toLocaleLowerCase() + value.slice(1) : value;
}

/**
 * A link may name one rule (`?rule=<key>`, from the project's notification
 * overview). Reads it from the route and hands it over once; the editor drops
 * the parameter after it opened the rule, so a later visit starts folded.
 */
function RuleFromRoute({ onRule }: Readonly<{ onRule: (ruleKey: string, done: () => void) => void }>) {
  const [searchParams, setSearchParams] = useSearchParams();
  const ruleKey = searchParams.get("rule") || "";
  useEffect(() => {
    if (!ruleKey) return;
    onRule(ruleKey, () => setSearchParams((current) => {
      const next = new URLSearchParams(current);
      next.delete("rule");
      return next;
    }, { replace: true }));
  }, [onRule, ruleKey, setSearchParams]);
  return null;
}

/** A row's identity: the saved key, or the client id of a rule not saved yet. */
function ruleId(draft: WorkflowReactionDraft, index: number): string {
  return draft.clientId || draft.reactionKey || `rule-${index}`;
}

function withEnabled(list: WorkflowReactionDraft[], reactionKey: string, enabled: boolean): WorkflowReactionDraft[] {
  return list.map((draft) => (draft.reactionKey === reactionKey ? { ...draft, enabled } : draft));
}

const FIELD_LABEL = "block text-xs fs-ink-secondary";
const FIELD_ERROR = "fs-field-helper error block";

/**
 * The automation rules of one scope: the workflow, a group or a step.
 *
 * With no rules it offers Common rules and a blank rule. Each rule is one row,
 * "When … → what it does", folded until opened; a saved rule turns on and off
 * from its row at once, while every other change waits for Save rules, which
 * checks first that each notification has somewhere to go. Removing a rule
 * offers Undo. A step rule offers the approval and wait-timer events only on an
 * approval or a wait step, where they can happen.
 */
export function AutomationRuleEditor({
  projectId = "",
  scopeId = "",
  scopeKind,
  stepKind = "",
  workflowId,
  onDirtyChange,
}: Readonly<{
  projectId?: string;
  scopeId?: string;
  scopeKind: AutomationScopeKind;
  /** The kind of the step a step-scoped editor belongs to. */
  stepKind?: string;
  workflowId: string;
  /** Unsaved rules exist: a surface that closes or switches the editor asks first. */
  onDirtyChange?: (dirty: boolean) => void;
}>) {
  const { t } = useI18n();
  const formId = useId();
  const [drafts, setDrafts] = useState<WorkflowReactionDraft[] | null>(null);
  const [openRuleId, setOpenRuleId] = useState<string | null>(null);
  const [tried, setTried] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [removed, setRemoved] = useState<{ draft: WorkflowReactionDraft; index: number } | null>(null);
  const [toggleErrors, setToggleErrors] = useState<Record<string, string>>({});
  const [pendingToggles, setPendingToggles] = useState<Record<string, boolean>>({});
  const [live, setLive] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const clientIdSerial = useRef(0);
  const toggleChain = useRef<Promise<unknown>>(Promise.resolve());
  const actionTypes = AUTOMATION_ACTIONS_BY_SCOPE[scopeKind];
  const actionDefinitions = localizedAUTOMATION_ACTION_DEFINITIONS(t);
  const reactionsQuery = useQuery({
    queryKey: ["workflows", "reactions", workflowId, scopeKind, scopeId],
    queryFn: () => fetchWorkflowReactions({ workflowId, scopeKind, scopeId, projectId }),
    enabled: Boolean(workflowId && (scopeKind === "workflow" || scopeId)),
  });
  const rulesLoaderVisible = useLoaderDelay(reactionsQuery.isLoading);
  const sources = useMemo(
    () => sourceRowsFromPayload(reactionsQuery.data, scopeKind),
    [reactionsQuery.data, scopeKind],
  );
  const sourceOptions = useMemo(
    () => sourceOptionsFromRows(sources, t),
    [sources, t],
  );
  const serverDrafts = useMemo(
    () =>
      (Array.isArray(reactionsQuery.data?.reactions)
        ? reactionsQuery.data.reactions
        : []
      ).map((reaction) => draftFromReactionRule(reaction, scopeKind, sources, t)),
    [reactionsQuery.data, scopeKind, sources, t],
  );
  const currentDrafts = drafts ?? serverDrafts;
  // Switches still on their way are not unsaved changes: compare against the
  // saved list as they will leave it.
  const savedView = useMemo(
    () => Object.entries(pendingToggles).reduce((list, [key, enabled]) => withEnabled(list, key, enabled), serverDrafts),
    [pendingToggles, serverDrafts],
  );
  const savedListRef = useRef(serverDrafts);
  if (!Object.keys(pendingToggles).length) savedListRef.current = serverDrafts;
  const dirty = drafts !== null && reactionDraftsFingerprint(drafts) !== reactionDraftsFingerprint(savedView);
  const templates = useMemo(() => automationRuleTemplates(scopeKind, t, stepKind), [scopeKind, stepKind, t]);
  const inRouter = useInRouterContext();
  const [requestedRule, setRequestedRule] = useState<{ key: string; done: () => void } | null>(null);
  const requestRule = useCallback((key: string, done: () => void) => setRequestedRule({ key, done }), []);
  useEffect(() => {
    if (!requestedRule || !reactionsQuery.isSuccess) return;
    const index = serverDrafts.findIndex((draft) => draft.reactionKey === requestedRule.key);
    if (index >= 0) {
      const id = ruleId(serverDrafts[index], index);
      setOpenRuleId(id);
      globalThis.requestAnimationFrame?.(() => {
        const rows = globalThis.document ? [...globalThis.document.querySelectorAll<HTMLElement>("[data-rule-key]")] : [];
        rows.find((row) => row.dataset.ruleKey === requestedRule.key)?.scrollIntoView?.({ block: "nearest" });
      });
    }
    requestedRule.done();
    setRequestedRule(null);
  }, [reactionsQuery.isSuccess, requestedRule, serverDrafts]);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);
  useEffect(() => {
    if (!dirty) return undefined;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    globalThis.addEventListener("beforeunload", warn);
    return () => globalThis.removeEventListener("beforeunload", warn);
  }, [dirty]);
  useEffect(() => {
    if (!removed) return undefined;
    const timer = globalThis.setTimeout(() => setRemoved(null), REMOVED_UNDO_MS);
    return () => globalThis.clearTimeout(timer);
  }, [removed]);

  const saveMutation = useMutation({
    mutationFn: (payload: Array<Record<string, unknown>>) =>
      updateWorkflowReactions({
        projectId,
        workflowId,
        reactions: payload,
        scopeKind,
        scopeId,
      }),
    onSuccess: async () => {
      setToggleErrors({});
      setDrafts(null);
      setTried(false);
      setRemoved(null);
      setOpenRuleId(null);
      await reactionsQuery.refetch();
    },
    onError: (error) => {
      setSaveError(
        error instanceof Error
          ? error.message
          : t("react.workflows.failed_to_save_automation_rules", "Failed to save automation rules."),
      );
    },
  });
  const notificationProvidersQuery = useQuery({
    queryKey: ["projects", "workflow-notification-providers", projectId],
    queryFn: () => fetchProjectNotificationProviders(projectId),
    enabled: Boolean(projectId),
    retry: false,
  });
  const providerOptions = useMemo(
    () => (Array.isArray(notificationProvidersQuery.data) ? notificationProvidersQuery.data : []),
    [notificationProvidersQuery.data],
  );
  const recipientProviderKeys = useMemo(
    () =>
      new Set(
        providerOptions
          .filter((provider) => provider.notification_target_kind === "recipient")
          .map((provider) => provider.provider_key),
      ),
    [providerOptions],
  );
  const providerKeysForChannels = useMemo(() => {
    const keys = new Set<string>();
    for (const draft of currentDrafts) {
      const providerKey =
        draft.actionTarget === NOTIFY_TARGET_EXTENSION_CHANNEL
          ? textOrEmpty(draft.actionExtra.provider_key)
          : "";
      // An email provider addresses people; it has no channels to list.
      if (providerKey && !recipientProviderKeys.has(providerKey)) {
        keys.add(providerKey);
      }
    }
    return [...keys];
  }, [currentDrafts, recipientProviderKeys]);
  const providerChannelQueries = useQueries({
    queries: providerKeysForChannels.map((providerKey) => ({
      queryKey: ["projects", "workflow-notification-channels", projectId, providerKey],
      queryFn: () => fetchProjectProviderChannels(projectId, providerKey),
      enabled: Boolean(projectId && providerKey),
      retry: false,
    })),
  });
  const channelsByProvider = useMemo(() => {
    const next = new Map<string, ProviderChannel[]>();
    providerKeysForChannels.forEach((providerKey, index) => {
      const rows = providerChannelQueries[index]?.data;
      if (Array.isArray(rows)) {
        next.set(providerKey, rows);
      }
    });
    return next;
  }, [providerChannelQueries, providerKeysForChannels]);

  function problemsFor(draft: WorkflowReactionDraft): ReactionDraftProblems {
    return reactionDraftProblems(draft, {
      recipientProviderKeys,
      recipientFormatFor: (providerKey) => providerOptions.find((provider) => provider.provider_key === providerKey)?.notification_recipient_format || null,
      channelIdsFor: (providerKey) => {
        const channels = channelsByProvider.get(providerKey);
        if (!channels) return null;
        const capability = notificationChannelCapabilityFromAction(draft.actionExtra);
        return channels.filter((channel) => channelSupportsCapability(channel, capability)).map((channel) => channel.channel_id);
      },
    }, t);
  }

  function addRule(next: WorkflowReactionDraft) {
    clientIdSerial.current += 1;
    const draft = { ...next, clientId: `new-${clientIdSerial.current}` };
    setSaveError("");
    setAddOpen(false);
    setDrafts((current) => [...(current ?? serverDrafts), draft]);
    setOpenRuleId(draft.clientId);
  }

  function addTemplate(template: AutomationRuleTemplate) {
    addRule(reactionDraftFromTemplate(template, scopeKind, sources, stepKind, t));
  }

  function addBlank() {
    addRule(blankReactionDraft(scopeKind, sources, stepKind, t));
  }

  function updateDraft(index: number, patch: Partial<WorkflowReactionDraft>) {
    setDrafts((current) => {
      const source = current ?? serverDrafts;
      return source.map((draft, currentIndex) =>
        currentIndex === index ? { ...draft, ...patch } : draft,
      );
    });
  }

  function removeDraft(index: number) {
    const draft = currentDrafts[index];
    if (!draft) return;
    setDrafts(currentDrafts.filter((_, currentIndex) => currentIndex !== index));
    setOpenRuleId(null);
    setRemoved({ draft, index });
    setLive("");
  }

  function undoRemove() {
    if (!removed) return;
    const { draft, index } = removed;
    setDrafts((current) => {
      const source = current ?? serverDrafts;
      // A saved rule comes back as the server holds its switch now.
      const saved = draft.reactionKey ? savedView.find((item) => item.reactionKey === draft.reactionKey) : undefined;
      const back = saved ? { ...draft, enabled: saved.enabled } : draft;
      return [...source.slice(0, index), back, ...source.slice(index)];
    });
    setOpenRuleId(ruleId(draft, index));
    setRemoved(null);
  }

  /**
   * A saved rule's switch writes at once: the request is the saved list with
   * only this rule's switch changed, so other unsaved edits stay unsaved.
   * Writes run one at a time, in the order they were made.
   */
  function toggleRule(index: number, next: boolean) {
    const draft = currentDrafts[index];
    const key = draft?.reactionKey;
    if (!key) return;
    const saved = savedListRef.current.find((item) => item.reactionKey === key);
    const declaredEmail = providerOptions.some((provider) => provider.provider_key === textOrEmpty(saved?.actionExtra.provider_key)
      && provider.notification_recipient_format === "email");
    const recipientProblem = next && saved && declaredEmail ? problemsFor({ ...saved, enabled: true }).recipients : undefined;
    if (recipientProblem) {
      setToggleErrors((current) => ({ ...current, [key]: recipientProblem }));
      setOpenRuleId(ruleId(draft, index));
      return;
    }
    setToggleErrors((current) => {
      const rest = { ...current };
      delete rest[key];
      return rest;
    });
    setLive("");
    setDrafts((current) => (current === null ? null : withEnabled(current, key, next)));
    setPendingToggles((current) => ({ ...current, [key]: next }));
    toggleChain.current = toggleChain.current.then(async () => {
      const base = withEnabled(savedListRef.current, key, next);
      try {
        await updateWorkflowReactions({
          projectId,
          workflowId,
          reactions: base.map((item, itemIndex) => reactionPayloadFromDraft(item, itemIndex)),
          scopeKind,
          scopeId,
        });
        savedListRef.current = base;
        setLive(next ? t("react.workflows.automation_rule_turned_on", "Rule turned on.") : t("react.workflows.automation_rule_turned_off", "Rule turned off."));
      } catch {
        const text = next
          ? t("react.workflows.automation_rule_turn_on_failed", "Could not turn the rule on. Try again.")
          : t("react.workflows.automation_rule_turn_off_failed", "Could not turn the rule off. Try again.");
        // The switch returns to the saved position and the row says why.
        setDrafts((current) => (current === null ? null : withEnabled(current, key, !next)));
        setToggleErrors((current) => ({ ...current, [key]: text }));
        setLive(text);
      } finally {
        setPendingToggles((current) => {
          if (current[key] !== next) return current;
          const rest = { ...current };
          delete rest[key];
          return rest;
        });
        await reactionsQuery.refetch();
      }
    });
  }

  function submitDrafts() {
    setSaveError("");
    setTried(true);
    const invalidIndex = currentDrafts.findIndex((draft) => Object.keys(problemsFor(draft)).length > 0);
    if (invalidIndex >= 0) {
      setOpenRuleId(ruleId(currentDrafts[invalidIndex], invalidIndex));
      return;
    }
    saveMutation.mutate(currentDrafts.map((draft, index) => reactionPayloadFromDraft(draft, index)));
  }

  function eventLabel(draft: WorkflowReactionDraft): string {
    const option = automationEventOptions(sources, draft.sourceId, draft.eventType, scopeKind, stepKind, t)
      .find((item) => item.value === draft.eventType);
    return option?.label || draft.eventType;
  }

  function severityLabel(draft: WorkflowReactionDraft): string {
    const severity = textOrEmpty(draft.actionExtra.severity) || "info";
    return t(`events.severity.${severity}`, severity.charAt(0).toUpperCase() + severity.slice(1));
  }

  /** What the row says about where a notification goes. */
  function ruleSummary(draft: WorkflowReactionDraft): string {
    if (draft.actionType !== NOTIFY_ACTION_TYPE) return "";
    if (draft.actionTarget === NOTIFY_TARGET_ROUTE_KEYS) {
      const keys = routeKeysFromAction(draft.actionExtra);
      return `${keys.length ? keys.join(", ") : t("react.workflows.automation_no_route_keys_yet", "No route keys yet")} · ${severityLabel(draft)}`;
    }
    if (draft.actionTarget === NOTIFY_TARGET_EXTENSION_CHANNEL) {
      const providerKey = textOrEmpty(draft.actionExtra.provider_key);
      const provider = providerOptions.find((item) => item.provider_key === providerKey);
      if (!providerKey) return t("react.workflows.automation_no_app_yet", "No app yet");
      const providerName = provider?.display_name || providerKey;
      if (recipientProviderKeys.has(providerKey)) {
        const recipients = notificationRecipientsFromAction(draft.actionExtra);
        return `${providerName} · ${recipients.length ? recipients.join(", ") : t("react.workflows.automation_no_recipients_yet", "no recipients yet")}`;
      }
      const channelId = textOrEmpty(draft.actionExtra.channel_id);
      const channel = (channelsByProvider.get(providerKey) || []).find((item) => item.channel_id === channelId);
      const useCase = notificationChannelCapabilityFromAction(draft.actionExtra) === NOTIFY_CHANNEL_CAPABILITY_HUMAN_DECISION
        ? t("react.workflows.human_decision", "Human decision")
        : t("registry.workflow_detail.automation.action.notify", "Notify");
      return `${providerName} · ${channel?.display_name || channelId || t("react.workflows.automation_no_channel_yet", "no channel yet")} · ${useCase}`;
    }
    return `${t("react.workflows.automation_matching_channels", "Matching channels")} · ${severityLabel(draft)}`;
  }

  const destinations = [
    { value: NOTIFY_TARGET_ALL_ROUTES, label: t("react.workflows.automation_matching_channels", "Matching channels"), help: t("react.workflows.automation_matching_channels_help", "Every enabled project channel that matches this event and severity.") },
    { value: NOTIFY_TARGET_EXTENSION_CHANNEL, label: t("react.workflows.automation_specific_channel", "A specific channel"), help: t("react.workflows.automation_specific_channel_help", "One channel of a connected app, or email addresses.") },
    { value: NOTIFY_TARGET_ROUTE_KEYS, label: t("react.workflows.automation_route_keys", "Route keys"), help: t("react.workflows.automation_route_keys_help", "route_key values from Project Configuration › Notifications.") },
  ];
  const actsOn = scopeKind === "step"
    ? t("react.workflows.automation_acts_on_step", "Acts on the interrupted step.")
    : t("react.workflows.automation_acts_on_job", "Acts on the current job.");

  function renderTemplateButton(template: AutomationRuleTemplate, onPick: () => void) {
    return (
      <button
        className="flex w-full items-start gap-2 rounded-lg border border-default px-3 py-2 text-left text-xs fs-ink-primary hover:bg-[var(--fs-bg-surface-muted)]"
        key={template.id}
        onClick={onPick}
        type="button"
      >
        <Zap aria-hidden="true" className="mt-0.5 h-4 w-4 flex-none fs-ink-tertiary" />
        <span className="flex min-w-0 flex-col">
          <span>{template.label}</span>
          <span className="fs-ink-tertiary">{template.help}</span>
        </span>
      </button>
    );
  }

  function renderBlankButton() {
    return (
      <button
        className="flex w-full items-start gap-2 rounded-lg border border-default px-3 py-2 text-left text-xs fs-ink-primary hover:bg-[var(--fs-bg-surface-muted)]"
        onClick={addBlank}
        type="button"
      >
        <Plus aria-hidden="true" className="mt-0.5 h-4 w-4 flex-none fs-ink-tertiary" />
        <span className="flex min-w-0 flex-col">
          <span>{t("react.workflows.automation_blank_rule", "Blank rule")}</span>
          <span className="fs-ink-tertiary">{t("react.workflows.automation_blank_rule_help", "Choose when it runs and what it does.")}</span>
        </span>
      </button>
    );
  }

  function renderNotifyFields(draft: WorkflowReactionDraft, index: number, problems: ReactionDraftProblems) {
    const showError = (key: keyof ReactionDraftProblems) => (tried ? problems[key] || "" : "");
    const selectedProviderKey = textOrEmpty(draft.actionExtra.provider_key);
    const channelCapability = notificationChannelCapabilityFromAction(draft.actionExtra);
    const providerChannels = channelsByProvider.get(selectedProviderKey) || [];
    const filteredChannels = providerChannels.filter((channel) => channelSupportsCapability(channel, channelCapability));
    const recipientProvider = recipientProviderKeys.has(selectedProviderKey);
    const emailProvider = providerOptions.find((provider) => provider.provider_key === selectedProviderKey)?.notification_recipient_format === "email";
    const destinationName = `${formId}-destination-${index}`;
    const routeKeysError = showError("routeKeys");
    const providerError = showError("provider");
    const recipientsError = showError("recipients") || (draft.reactionKey && toggleErrors[draft.reactionKey]
      ? problemsFor({ ...draft, enabled: true }).recipients : "");
    const channelError = showError("channel");
    return (
      <div className="space-y-3" id={indexedId(index, "rxn-notify-config")}>
        <fieldset className="space-y-1">
          <legend className="text-xs fs-ink-secondary">{t("react.workflows.send_to", "Send to")}</legend>
          {destinations.map((destination) => (
            <DsRadio
              checked={draft.actionTarget === destination.value}
              key={destination.value}
              name={destinationName}
              onChange={() => {
                if (draft.actionTarget === destination.value) return;
                updateDraft(index, {
                  actionExtra: actionExtraForTarget(draft, destination.value),
                  actionTarget: destination.value,
                });
              }}
              value={destination.value}
            >
              <span className="flex flex-col">
                <span>{destination.label}</span>
                <span className="text-xs fs-ink-tertiary">{destination.help}</span>
              </span>
            </DsRadio>
          ))}
        </fieldset>
        {renderWhen(draft.actionTarget === NOTIFY_TARGET_ROUTE_KEYS, (
          <label className={FIELD_LABEL} htmlFor={indexedId(index, "rxn-notify-route-keys")}>
            <span>{t("react.workflows.route_keys_to_notify", "Route keys to notify")}</span>
            <input
              aria-describedby={indexedId(index, "rxn-notify-route-help")}
              aria-invalid={routeKeysError ? true : undefined}
              className="fs-input mt-1 w-full font-mono"
              id={indexedId(index, "rxn-notify-route-keys")}
              onChange={(event) =>
                updateDraft(index, { actionExtra: withNotificationRouteKeys(draft.actionExtra, event.target.value) })
              }
              placeholder="release_alerts, ops_chat"
              value={routeKeysTextFromAction(draft.actionExtra)}
            />
            <span className="fs-field-helper block" id={indexedId(index, "rxn-notify-route-help")}>
              {t("react.workflows.automation_route_keys_separate", "Separate keys with commas.")}
            </span>
            {routeKeysError ? <span className={FIELD_ERROR}>{routeKeysError}</span> : null}
          </label>
        ))}
        {renderWhen(draft.actionTarget === NOTIFY_TARGET_EXTENSION_CHANNEL, (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className={FIELD_LABEL} htmlFor={indexedId(index, "rxn-notify-provider")}>
              <span>{t("react.workflows.automation_app", "App")}</span>
              <select
                aria-invalid={providerError ? true : undefined}
                className="fs-input mt-1 w-full"
                id={indexedId(index, "rxn-notify-provider")}
                onChange={(event) =>
                  updateDraft(index, {
                    actionExtra: withNotificationChannelSelection(draft.actionExtra, {
                      providerKey: event.target.value,
                      channelId: "",
                    }),
                  })
                }
                value={selectedProviderKey}
              >
                <option value="">{t("react.workflows.automation_select_app", "Select app")}</option>
                {providerOptions.map((provider: ProjectNotificationProvider) => (
                  <option key={provider.provider_key} value={provider.provider_key}>
                    {provider.display_name || provider.provider_key}
                  </option>
                ))}
              </select>
              {providerError ? <span className={FIELD_ERROR}>{providerError}</span> : null}
            </label>
            {renderWhen(recipientProvider, (
              <label className={`${FIELD_LABEL} sm:col-span-2`} htmlFor={indexedId(index, "rxn-notify-recipients")}>
                <span>{emailProvider ? t("react.workflows.automation_email_addresses", "Email addresses") : t("react.workflows.automation_generic_recipients", "Recipients")}</span>
                <input
                  aria-describedby={recipientsError ? indexedId(index, "rxn-notify-recipients-error") : undefined}
                  aria-invalid={recipientsError ? true : undefined}
                  className="fs-input mt-1 w-full"
                  id={indexedId(index, "rxn-notify-recipients")}
                  onChange={(event) =>
                    updateDraft(index, {
                      actionExtra: withNotificationChannelSelection(draft.actionExtra, { recipients: event.target.value }),
                    })
                  }
                  placeholder={emailProvider ? "ops@company.com, anna@company.com" : undefined}
                  type="text"
                  value={notificationRecipientsTextFromAction(draft.actionExtra)}
                />
                {recipientsError ? <span className={FIELD_ERROR} id={indexedId(index, "rxn-notify-recipients-error")}>{recipientsError}</span> : null}
              </label>
            ))}
            {renderWhen(!recipientProvider, (<>
              <label className={FIELD_LABEL} htmlFor={indexedId(index, "rxn-notify-capability")}>
                <span>{t("react.workflows.use_case", "Use case")}</span>
                <select
                  className="fs-input mt-1 w-full"
                  id={indexedId(index, "rxn-notify-capability")}
                  onChange={(event) =>
                    updateDraft(index, {
                      actionExtra: withNotificationChannelSelection(draft.actionExtra, {
                        channelCapability: event.target.value,
                        channelId: "",
                      }),
                    })
                  }
                  value={channelCapability}
                >
                  <option value={NOTIFY_CHANNEL_CAPABILITY_NOTIFY}>{t("registry.workflow_detail.automation.action.notify", "Notify")}</option>
                  <option value={NOTIFY_CHANNEL_CAPABILITY_HUMAN_DECISION}>{t("react.workflows.human_decision", "Human decision")}</option>
                </select>
              </label>
              <label className={`${FIELD_LABEL} sm:col-span-2`} htmlFor={indexedId(index, "rxn-notify-channel")}>
                <span>{t("settings.notifications.columns.channel", "Channel")}</span>
                <select
                  aria-invalid={channelError ? true : undefined}
                  className="fs-input mt-1 w-full"
                  id={indexedId(index, "rxn-notify-channel")}
                  onChange={(event) =>
                    updateDraft(index, {
                      actionExtra: withNotificationChannelSelection(draft.actionExtra, { channelId: event.target.value }),
                    })
                  }
                  value={textOrEmpty(draft.actionExtra.channel_id)}
                >
                  <option value="">{t("react.workflows.select_channel", "Select channel")}</option>
                  {filteredChannels.map((channel) => (
                    <option key={channel.channel_id} value={channel.channel_id}>
                      {channel.display_name} ({channel.channel_id})
                    </option>
                  ))}
                </select>
                {channelError ? <span className={FIELD_ERROR}>{channelError}</span> : null}
              </label>
              {renderWhen(!projectId, (
                <p className="text-xs fs-ink-attention sm:col-span-2">
                  {t("react.workflows.select_a_project_to_target_a_specific_extension_channel", "Select a project to target a specific extension channel.")}</p>
              ))}
              {renderWhen(Boolean(selectedProviderKey && !filteredChannels.length), (
                <p className="text-xs fs-ink-attention sm:col-span-2">
                  {t("react.workflows.automation_no_compatible_channels", "No compatible channels are configured for this app and use case.")}</p>
              ))}
            </>))}
          </div>
        ))}
        {renderWhen(draft.actionTarget !== NOTIFY_TARGET_EXTENSION_CHANNEL, (
          <label className={FIELD_LABEL} htmlFor={indexedId(index, "rxn-notify-severity")}>
            <span>{t("security_overview.columns.severity", "Severity")}</span>
            <select
              className="fs-input mt-1 w-full"
              id={indexedId(index, "rxn-notify-severity")}
              onChange={(event) =>
                updateDraft(index, {
                  actionExtra: withNotificationField(draft.actionExtra, "severity", event.target.value),
                })
              }
              value={textOrEmpty(draft.actionExtra.severity) || "info"}
            >
              {NOTIFY_SEVERITY_OPTIONS.map((severity) => (
                <option key={severity} value={severity}>
                  {t(`events.severity.${severity}`, severity)}
                </option>
              ))}
            </select>
          </label>
        ))}
        <label className={FIELD_LABEL} htmlFor={indexedId(index, "rxn-notify-message")}>
          <span>{t("registry.workflow_detail.field.message", "Message")}</span>
          <textarea
            className="fs-textarea mt-1 w-full"
            id={indexedId(index, "rxn-notify-message")}
            onChange={(event) =>
              updateDraft(index, {
                actionExtra: withNotificationField(draft.actionExtra, "message", event.target.value),
              })
            }
            placeholder={t("react.workflows.optional_message_for_this_notification", "Optional message for this notification")}
            rows={2}
            value={typeof draft.actionExtra.message === "string" ? draft.actionExtra.message : ""}
          />
        </label>
      </div>
    );
  }

  function renderRulePanel(draft: WorkflowReactionDraft, index: number, problems: ReactionDraftProblems) {
    const eventOptions = automationEventOptions(sources, draft.sourceId, draft.eventType, scopeKind, stepKind, t);
    const eventError = tried ? problems.event || "" : "";
    const actionError = tried ? problems.action || "" : "";
    return (
      <div className="space-y-3 p-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <label className={FIELD_LABEL} htmlFor={indexedId(index, "rxn-event-preset")}>
            <span>{t("react.workflows.automation_when", "When")}</span>
            <select
              aria-invalid={eventError ? true : undefined}
              className="fs-input mt-1 w-full"
              id={indexedId(index, "rxn-event-preset")}
              onChange={(event) => updateDraft(index, { eventType: event.target.value })}
              value={draft.eventType}
            >
              {eventOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            {eventError ? <span className={FIELD_ERROR}>{eventError}</span> : null}
          </label>
          <label className={FIELD_LABEL} htmlFor={indexedId(index, "rxn-action-type")}>
            <span>{t("react.workflows.automation_then", "Then")}</span>
            <select
              aria-invalid={actionError ? true : undefined}
              className="fs-input mt-1 w-full"
              id={indexedId(index, "rxn-action-type")}
              onChange={(event) => {
                const nextActionType = event.target.value;
                updateDraft(index, {
                  actionExtra: actionExtraForType(draft, nextActionType),
                  actionTarget: targetOptionsForAction(nextActionType, undefined, t)[0]?.value || "",
                  actionType: nextActionType,
                });
              }}
              value={draft.actionType}
            >
              {uniqueSelectOptions([
                ...actionTypes.map((actionType) => ({
                  value: actionType,
                  label: actionDefinitions[actionType]?.label || actionType,
                })),
                { value: draft.actionType, label: draft.actionType },
              ]).map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            {actionError ? <span className={FIELD_ERROR}>{actionError}</span> : null}
          </label>
        </div>
        {renderWhen(sourceOptions.length > 1, (
          <label className={FIELD_LABEL} htmlFor={`rxn-source-${index}`}>
            <span>{t("react.workflows.trigger_source", "Trigger source")}</span>
            <select
              className="fs-input mt-1 w-full"
              id={`rxn-source-${index}`}
              onChange={(event) => {
                const nextSource = event.target.value;
                const nextEvent =
                  automationEventOptions(sources, nextSource, "", scopeKind, stepKind, t)
                    .find((option) => option.value === draft.eventType)?.value
                  || automationEventOptions(sources, nextSource, "", scopeKind, stepKind, t)[0]?.value
                  || draft.eventType;
                updateDraft(index, { sourceId: nextSource, eventType: nextEvent });
              }}
              value={draft.sourceId || "__workflow__"}
            >
              {sourceOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        ))}
        {draft.actionType === NOTIFY_ACTION_TYPE
          ? renderNotifyFields(draft, index, problems)
          : <p className="text-xs fs-ink-tertiary">{actsOn}</p>}
        <Button onClick={() => removeDraft(index)} size="sm" type="button" variant="ghost">
          <span className="fs-ink-danger">{t("react.workflows.automation_remove_rule", "Remove rule")}</span>
        </Button>
      </div>
    );
  }

  const items = currentDrafts.map((draft, index) => {
    const id = ruleId(draft, index);
    const problems = problemsFor(draft);
    const invalid = tried && Object.keys(problems).length > 0;
    const actionLabel = actionDefinitions[draft.actionType]?.label || draft.actionType;
    const when = t("react.workflows.automation_rule_when", "When {event}", { event: lowerFirst(eventLabel(draft)) });
    const summary = ruleSummary(draft);
    const saved = Boolean(draft.reactionKey) && savedView.some((item) => item.reactionKey === draft.reactionKey);
    const toggleError = draft.reactionKey ? toggleErrors[draft.reactionKey] || "" : "";
    const errorId = `${formId}-rule-${index}-error`;
    return {
      id,
      header: (
        <span className={`flex min-w-0 items-center gap-2${draft.enabled ? "" : " fs-ink-tertiary"}`} data-invalid={invalid || undefined} data-rule-key={draft.reactionKey || undefined}>
          <span className="truncate">{when}</span>
          <span aria-hidden="true">→</span>
          <span className="truncate">{actionLabel}</span>
          {summary ? <span className="truncate text-xs font-normal fs-ink-tertiary">{summary}</span> : null}
          {saved ? null : <DsBadge tone="orange">{t("react.workflows.automation_not_saved", "Not saved")}</DsBadge>}
          {invalid ? <span className="fs-sr-only">{t("react.workflows.automation_rule_needs_attention", "Needs attention")}</span> : null}
        </span>
      ),
      accessory: saved ? (
        <DsSwitch
          checked={draft.enabled}
          describedBy={toggleError ? errorId : undefined}
          disabled={saveMutation.isPending}
          label={t("react.workflows.automation_enable_rule", "Enable rule: {rule}", { rule: lowerFirst(`${when}, ${actionLabel.toLocaleLowerCase()}`) })}
          onCheckedChange={(next) => toggleRule(index, next)}
        />
      ) : undefined,
      panel: (
        <>
          {toggleError ? <p className={`${FIELD_ERROR} px-3 pt-2`} id={errorId}>{toggleError}</p> : null}
          {renderRulePanel(draft, index, problems)}
        </>
      ),
    };
  });

  const togglesPending = Object.keys(pendingToggles).length > 0;

  return (
    <div className="mt-3 space-y-3" id="reaction-form">
      {inRouter ? <RuleFromRoute onRule={requestRule} /> : null}
      {rulesLoaderVisible ? (
        <div className="inline-flex items-center gap-2 text-xs fs-ink-secondary">
          <DsLoader variant="orbit-inline" />
          {t("react.workflows.loading_automation_rules", "Loading automation rules...")}</div>
      ) : null}
      {renderWhen(!reactionsQuery.isLoading && !currentDrafts.length, (
        <div className="space-y-2" data-testid="automation-empty">
          <p className="text-xs fs-ink-secondary">
            {t("react.workflows.automation_no_rules_yet", "No rules yet. Start from a common one or a blank rule:")}</p>
          {templates.map((template) => renderTemplateButton(template, () => addTemplate(template)))}
          {renderBlankButton()}
        </div>
      ))}
      {renderWhen(currentDrafts.length > 0, (
        <DsAccordion items={items} onOpenChange={setOpenRuleId} openId={openRuleId} />
      ))}
      {renderWhen(currentDrafts.length > 0, (
        <DsPopover
          label={t("react.workflows.automation_common_rules", "Common rules")}
          onOpenChange={setAddOpen}
          open={addOpen}
          trigger={(props) => renderAddRuleButton(props, t("react.workflows.automation_add_rule", "Add rule"))}
        >
          <div className="space-y-2 p-1">
            <p className="text-[11px] uppercase tracking-[0.13em] fs-ink-tertiary">{t("react.workflows.automation_common_rules", "Common rules")}</p>
            {templates.map((template) => renderTemplateButton(template, () => addTemplate(template)))}
            {renderBlankButton()}
          </div>
        </DsPopover>
      ))}
      {removed ? (
        <div className="flex flex-wrap items-center gap-2 text-xs fs-ink-secondary" role="status">
          <span>{t("react.workflows.automation_rule_removed", "Rule removed. Save rules to apply.")}</span>
          <Button onClick={undoRemove} size="sm" type="button" variant="secondary">{t("react.workflows.undo", "Undo")}</Button>
        </div>
      ) : null}
      {saveError ? <p className="text-xs fs-ink-danger" role="alert">{saveError}</p> : null}
      <span aria-live="polite" className="fs-sr-only" role="status">{live}</span>
      <DrawerFooter testId="automation-footer">
        {dirty ? <span className="text-xs font-medium fs-ink-attention">{t("react.workflows.unsaved_changes", "Unsaved changes")}</span> : null}
        <DrawerFooterSpacer />
        <SaveStateButton
          dirty={dirty}
          disabled={togglesPending}
          doneAnnouncement={t("react.workflows.automation_rules_saved", "Automation rules saved.")}
          doneLabel={t("react.workflows.saved_short", "Saved")}
          failed={Boolean(saveError)}
          label={t("react.workflows.save_rules", "Save rules")}
          onClick={submitDrafts}
          saving={saveMutation.isPending}
          savingLabel={t("react.workflows.saving_rules", "Saving rules…")}
        />
      </DrawerFooter>
      <div className="hidden" id="rxn-advanced-event-panel" />
      <div className="hidden" id="rxn-advanced-json-panel" />
    </div>
  );
}
