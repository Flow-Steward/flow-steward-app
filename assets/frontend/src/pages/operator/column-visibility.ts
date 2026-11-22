/**
 * Which columns of a section's list one person hides (ADR 0075): the admin sets
 * the columns, each person may hide some in this browser. Storage may be missing
 * or refuse (private windows, blocked site data); the list then shows every column.
 */

const PREFIX = "fs.operator.hidden-columns:";

export function hiddenColumnsKey(projectId: string, workflowId: string): string {
  return `${PREFIX}${projectId}:${workflowId}`;
}

export function readHiddenColumns(key: string): string[] {
  try {
    const raw = globalThis.localStorage?.getItem(key);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

export function writeHiddenColumns(key: string, hidden: readonly string[]): void {
  try {
    if (hidden.length) globalThis.localStorage?.setItem(key, JSON.stringify([...hidden]));
    else globalThis.localStorage?.removeItem(key);
  } catch {
    // The choice still holds for this visit.
  }
}
