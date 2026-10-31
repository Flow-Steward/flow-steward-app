import { HttpError } from "@/lib/http/errors";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

/** One object the server named in an id or name conflict; only objects the caller may see. */
export type RegistryConflictMatch = {
  id: string;
  display_name: string;
  enabled: boolean;
  usedBy: Array<{ entity_type: string; id: string; display_name: string }>;
};

/** The 409 an agent, skill or workflow writer returns for a taken id or display name. */
export type RegistryConflict = {
  code: "registry_id_conflict" | "registry_name_conflict";
  matches: RegistryConflictMatch[];
  suggestedDisplayName: string;
  suggestedId: string;
};

function recordOrNull(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function recordList(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.map(recordOrNull).filter((item): item is Record<string, unknown> => item !== null)
    : [];
}

/** The structured conflict in a failed write, or null for any other failure. */
export function registryConflictFromError(error: unknown): RegistryConflict | null {
  if (!(error instanceof HttpError) || error.status !== 409) return null;
  const detail = recordOrNull(recordOrNull(error.payload)?.detail);
  const code = detail?.code;
  if (!detail || (code !== "registry_id_conflict" && code !== "registry_name_conflict")) return null;
  return {
    code,
    matches: recordList(detail.matches).map((match) => ({
      id: stringFromUnknownValue(match.id),
      display_name: stringFromUnknownValue(match.display_name),
      enabled: match.enabled === true,
      usedBy: recordList(match.used_by).map((row) => ({
        entity_type: stringFromUnknownValue(row.entity_type),
        id: stringFromUnknownValue(row.id),
        display_name: stringFromUnknownValue(row.display_name),
      })),
    })),
    suggestedDisplayName: stringFromUnknownValue(detail.suggested_display_name),
    suggestedId: stringFromUnknownValue(detail.suggested_id),
  };
}
