import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import { firstReadableText, humanizeDiagnosticLabel, listFromUnknown, objectOrEmpty, readableText, safeLinkHref, structuredPayloadSummary, type Entity } from "./job-detail-core";
import { firstStringFromUnknownValue, isTruthy } from "@/lib/utils/unknown-values";

export type EvidenceItem = {
  key: string;
  label: string;
  summary: string;
  href: string;
};



function getTechnicalEvidenceLabels(t: I18nContextValue["t"] = fallbackContext.t) {
  return new Set([
  t("react.jobs.action_id", "action id"),
  "assigned",
  t("react.jobs.connection_binding", "connection binding"),
  t("react.jobs.connector_id", "connector id"),
  t("react.jobs.current_step", "current step"),
  "error",
  t("react.jobs.error_detail", "error detail"),
  t("react.jobs.extension_id", "extension id"),
  t("react.jobs.operation_kind", "operation kind"),
  t("react.jobs.parallel_execution_order", "parallel execution order"),
  t("react.jobs.parallel_lanes", "parallel lanes"),
  t("react.jobs.request_semantics", "request semantics"),
  t("react.jobs.result_targets", "result targets"),
  t("react.jobs.step_id", "step id"),
  t("react.jobs.step_kind", "step kind"),
  t("react.jobs.workflow_data", "workflow data"),
  t("react.jobs.workflow_terminal", "workflow terminal"),
]);
}

export function artifactEvidenceLabel(key: string, t: I18nContextValue["t"] = fallbackContext.t): string {
  const normalized = firstStringFromUnknownValue([key, ""]).trim().toLowerCase();
  if (normalized === "raw_output" || normalized === "raw result") {
    return t("react.jobs.structured_result", "Structured result");
  }
  if (normalized === "output") {
    return t("react.jobs.step_output", "Step output");
  }
  if (normalized === "source_artifact") {
    return t("react.jobs.input_catalog_csv", "Input catalog CSV");
  }
  if (normalized === "input_artifact") {
    return t("react.jobs.input_file", "Input file");
  }
  if (normalized === "catalog_artifact") {
    return t("react.jobs.generated_catalog_csv", "Generated catalog CSV");
  }
  if (normalized === "artifact") {
    return t("react.jobs.generated_artifact", "Generated artifact");
  }
  return humanizeDiagnosticLabel(key);
}

export function artifactEvidenceSummary(value: unknown, t: I18nContextValue["t"] = fallbackContext.t): string {
  const structuredSummary = structuredPayloadSummary(value);
  if (structuredSummary) {
    return structuredSummary;
  }
  const artifact = objectOrEmpty(value);
  const filename = firstReadableText(artifact.filename, artifact.name, artifact.path, artifact.url, artifact.href);
  const parts = [
    filename,
    artifact.row_count !== undefined && artifact.row_count !== null ? t("react.jobs.value1_rows", "{value1} rows", { value1: String(Number(artifact.row_count)) }) : "",
    artifact.size_bytes !== undefined && artifact.size_bytes !== null ? t("react.jobs.value1_bytes", "{value1} bytes", { value1: String(Number(artifact.size_bytes)) }) : "",
  ].filter(isTruthy);
  return parts.join(" · ") || readableText(value, 320);
}

export function collectEvidenceItems(step: Entity, t: I18nContextValue["t"] = fallbackContext.t): EvidenceItem[] {
  const items: EvidenceItem[] = [];
  const appendItem = (key: string, label: unknown, summary: unknown, href: unknown = "", labelKey = "") => {
    const structuredSummary = structuredPayloadSummary(summary);
    const readableSummary = structuredSummary || readableText(summary, 320);
    const rawLabel = readableText(label, 120);
    const labelText = labelKey ? t(labelKey, rawLabel) : rawLabel;
    const normalizedLabel = labelText.toLowerCase();
    const readableLabel =
      structuredSummary && (normalizedLabel === "output" || normalizedLabel === "raw output")
        ? t("react.jobs.step_output", "Step output")
        : labelText || t("jobs.detail.diagnostics.evidence", "Evidence");
    const safeHref = safeLinkHref(href);
    if (!labelKey && !safeHref && getTechnicalEvidenceLabels(t).has(normalizedLabel)) {
      return;
    }
    if (!readableSummary && !safeHref) {
      return;
    }
    items.push({
      key,
      label: readableLabel,
      summary: readableSummary || safeHref,
      href: safeHref,
    });
  };

  listFromUnknown(step.evidence).forEach((item, index) => {
    appendItem(
      `evidence-${index}`,
      item.title || item.label || item.meta,
      item.summary || item.value || item.detail || item.message,
      item.href || item.url || item.path,
      firstReadableText(item.title_key),
    );
  });

  for (const sourceKey of ["artifacts"]) {
    const source = objectOrEmpty(step[sourceKey]);
    Object.entries(source).forEach(([key, value]) => {
      const artifact = objectOrEmpty(value);
      const href =
        artifact.download_url ||
        artifact.job_attachment_url ||
        artifact.href ||
        artifact.url ||
        (typeof value === "string" ? value : "");
      appendItem(`${sourceKey}-${key}`, artifactEvidenceLabel(key, t), artifactEvidenceSummary(value, t), href);
    });
  }

  return items.slice(0, 8);
}
