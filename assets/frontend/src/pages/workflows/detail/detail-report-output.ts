/**
 * The workflow's report output (ADR 0059): one required output of type object
 * that holds the report snapshot a Template step in report format writes. The
 * Interface stores only its name; the layout lives in the step.
 */
import type { WorkflowInterfaceFieldDraft } from "@/pages/workflows/detail/detail-interface";

const OBJECT_TYPES = new Set(["object", "json"]);

/** Names of the declared outputs that can hold the report. */
export function reportOutputCandidates(rows: readonly WorkflowInterfaceFieldDraft[]): string[] {
  return rows
    .filter((row) => row.required && OBJECT_TYPES.has(String(row.type).trim().toLowerCase()))
    .map((row) => row.name.trim())
    .filter(Boolean);
}

/** A selection that no longer names a usable output, e.g. after a rename. */
export function staleReportOutput(selection: string, rows: readonly WorkflowInterfaceFieldDraft[]): boolean {
  const name = selection.trim();
  return Boolean(name) && !reportOutputCandidates(rows).includes(name);
}
