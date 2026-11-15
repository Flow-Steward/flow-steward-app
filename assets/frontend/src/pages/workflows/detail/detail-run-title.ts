/**
 * The workflow's run title (settings.run_title): a template over its inputs, such
 * as "{{ inputs.site_url }}", that names each run in Jobs, on the job page and on
 * an Invoke's link to its child run. The server renders it and refuses a template
 * that reads a secret or an undeclared input; this mirrors those checks while the
 * author types.
 */
import type { WorkflowInterfaceFieldDraft } from "@/pages/workflows/detail/detail-interface";

/** The server refuses a longer template. */
export const RUN_TITLE_TEMPLATE_MAX = 300;

const PLACEHOLDER = /\{\{(.*?)\}\}/g;
const INPUT_REFERENCE = /^inputs\.([A-Za-z_]\w{0,127})$/;
const SECRET_PARTS = [
  "access_token", "api_key", "authorization", "client_secret", "cookie", "credential", "credentials",
  "password", "passwd", "private_key", "refresh_token", "secret", "session", "token",
];

export type RunTitleIssue =
  | { kind: "placeholder"; expression: string }
  | { kind: "secret"; name: string }
  | { kind: "unknown"; name: string }
  | { kind: "too_long" };

function secretLikeName(name: string): boolean {
  const normalized = name.trim().toLowerCase().replaceAll("-", "_").replaceAll(" ", "_");
  return SECRET_PARTS.some((part) => normalized === part || normalized.startsWith(`${part}_`) || normalized.endsWith(`_${part}`));
}

/** Inputs a run title may show: declared, named, and not secret. */
export function runTitleInputNames(rows: readonly WorkflowInterfaceFieldDraft[]): string[] {
  return rows
    .filter((row) => String(row.type || "").trim() !== "secret")
    .map((row) => row.name.trim())
    .filter((name) => name && !secretLikeName(name));
}

/** Problems of a template against the inputs being edited (fields mode only knows the names). */
export function runTitleIssues(
  template: string,
  rows: readonly WorkflowInterfaceFieldDraft[],
  { inputMode = "fields" }: { inputMode?: string } = {},
): RunTitleIssue[] {
  const text = template.trim();
  if (!text) return [];
  if (text.length > RUN_TITLE_TEMPLATE_MAX) return [{ kind: "too_long" }];
  const declared = new Set(rows.map((row) => row.name.trim()).filter(Boolean));
  const secret = new Set(rows.filter((row) => String(row.type || "").trim() === "secret").map((row) => row.name.trim()));
  const issues: RunTitleIssue[] = [];
  for (const match of text.matchAll(PLACEHOLDER)) {
    const expression = match[1].trim();
    const reference = INPUT_REFERENCE.exec(expression);
    if (!reference) {
      issues.push({ kind: "placeholder", expression });
      continue;
    }
    const name = reference[1];
    if (secret.has(name) || secretLikeName(name)) issues.push({ kind: "secret", name });
    else if (inputMode === "fields" && !declared.has(name)) issues.push({ kind: "unknown", name });
  }
  return issues;
}
