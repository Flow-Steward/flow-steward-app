import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import type { ExtensionRecord } from "@/pages/extensions/api";

type Entity = Record<string, unknown>;

function asEntity(value: unknown): Entity {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Entity) : {};
}

/**
 * What the install check found worth an administrator's look: the code review
 * of the extension's own code (raw sockets, processes, absolute paths) and any
 * other warning compiling it produced. "file:line: what why", one per entry.
 */
export function extensionInstallWarnings(record: ExtensionRecord | null | undefined): string[] {
  if (!record) return [];
  const summary = asEntity(record.validation_summary);
  const compiled = asEntity(record.compiled_manifest);
  const rows = [
    summary.validation_warnings,
    summary.compile_warnings,
    compiled.validation_warnings,
    compiled.compile_warnings,
  ].flatMap((entry) => (Array.isArray(entry) ? entry : []));
  const messages = rows
    .map((row) => firstStringFromUnknownValue([asEntity(row).message, ""]).trim())
    .filter(Boolean);
  return [...new Set(messages)];
}
