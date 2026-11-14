import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstReadableText, humanizeDiagnosticLabel, type Entity } from "./job-detail-core";

function technicalUsageLabel(value: string): boolean {
  return /^extension[.\s]/iu.test(value)
    || /^(?:runnable\s*sequence|workflow[ _-]*routing)$/iu.test(value)
    || /^[a-f\d]{8}[-\s][a-f\d]{4}[-\s][a-f\d]{4}[-\s][a-f\d]{4}[-\s][a-f\d]{12}$/iu.test(value);
}

/** Format recorded metadata only; IDs and usage amounts remain unchanged. */
export function usageAgentLabel(row: Entity, t: I18nContextValue["t"] = fallbackContext.t): string {
  const labels = [row.agent_display_name, row.agent_label].map((value) => firstReadableText(value));
  const friendly = labels.find((label) => label && !technicalUsageLabel(label));
  if (friendly) return friendly;
  const agent = firstReadableText(row.agent_id, row.agent, ...labels);
  if (/^runnable\s*sequence$/iu.test(agent)) {
    return t("react.jobs.model_usage_processing", "Model processing");
  }
  if (/^workflow[ _-]*routing$/iu.test(agent)) {
    return t("react.jobs.model_usage_routing", "Workflow routing");
  }
  const extensionAgent = agent.match(/^extension\.[^.]+\.(.+)$/iu)?.[1];
  if (extensionAgent) return humanizeDiagnosticLabel(extensionAgent);
  if (!agent || technicalUsageLabel(agent)) return t("react.jobs.model_usage_agent", "Agent");
  return humanizeDiagnosticLabel(agent);
}
