import type { I18nContextValue } from "@/lib/i18n/context";
import type { WorkflowConsumer } from "@/pages/workflows/shared/hook-api";

/** Presentation metadata never falls back to an identity supplied alongside it. */
export function readableMetadata(value: unknown, identities: unknown[] = []): string {
  const name = typeof value === "string" ? value.trim() : "";
  return name && !identities.some((id) => typeof id === "string" && id.trim() === name) ? name : "";
}

/** Keep equal human labels distinct without printing their opaque option values. */
export function distinctLabelOptions<T extends { value: string; label: string }>(options: T[]): T[] {
  const groups = new Map<string, { value: string; index: number }[]>();
  const reserved = new Set(options.map((option) => option.label));
  const labels = new Map<number, string>();
  options.forEach((option, index) => {
    const values = groups.get(option.label) || [];
    values.push({ value: option.value, index });
    groups.set(option.label, values);
  });
  for (const [label, values] of groups) {
    if (values.length < 2) continue;
    values.sort((left, right) => left.value < right.value ? -1 : left.value > right.value ? 1 : left.index - right.index);
    let ordinal = 1;
    for (const { index } of values) {
      let candidate: string;
      do {
        candidate = `${label} #${ordinal++}`;
      } while (reserved.has(candidate));
      reserved.add(candidate);
      labels.set(index, candidate);
    }
  }
  return options.map((option, index) => {
    const label = labels.get(index);
    return label === undefined ? option : { ...option, label };
  });
}

export function consumerPresentationKey(consumer: WorkflowConsumer): string {
  return JSON.stringify([consumer.kind, consumer.caller_workflow_id, consumer.project_id || "", consumer.hook_id || "", consumer.step_id || ""]);
}

/** Extension + hook/action; personal workflow + Invoke step. Context stays compact. */
export function workflowConsumerLabels(consumers: WorkflowConsumer[], t: I18nContextValue["t"]): string[] {
  const rows = consumers.map((consumer) => {
    const extension = Boolean(consumer.caller_extension_id || consumer.caller_extension_name || consumer.source_install_id);
    const owner = extension
      ? readableMetadata(consumer.caller_extension_name, [consumer.caller_extension_id, consumer.source_install_id]) || t("react.workflows.unavailable_extension", "Unavailable extension")
      : readableMetadata(consumer.caller_display_name, [consumer.caller_workflow_id]) || t("react.workflows.workflow_unavailable", "Workflow unavailable");
    const action = consumer.kind === "hook"
      ? readableMetadata(consumer.hook_display_name, [consumer.hook_id, consumer.step_id]) || t("react.workflows.hook", "Hook")
      : readableMetadata(consumer.step_display_name, [consumer.step_id]) || t("react.workflows.step", "Step");
    return { value: consumerPresentationKey(consumer), label: `${owner} · ${action}` };
  });
  return distinctLabelOptions(rows).map((row) => row.label);
}
