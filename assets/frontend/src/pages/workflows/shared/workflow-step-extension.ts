/** A canonical extension id: dot-separated lowercase labels, e.g. `acme.crm-sync`. */
const CANONICAL_EXTENSION_ID = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/**
 * The extension a connector step comes from, to show its logo; "" when none is
 * named. Same rule as the server's `step_extension_id`: the override's
 * `extension_id` first, then `extension.<extension_id>`; an install-scoped id
 * (`extension.<install>.<operation>`) never counts.
 */
export function stepExtensionId(step: Record<string, unknown> | null | undefined): string {
  const source = record(step);
  const kind = text(source.step_kind) || text(source.kind);
  if (kind && kind !== "connector") return "";
  const configured = text(record(record(source.override).connector).extension_id);
  if (configured) return CANONICAL_EXTENSION_ID.test(configured) ? configured : "";
  const connectorId = text(source.connector_id);
  if (!connectorId.startsWith("extension.")) return "";
  const extensionId = connectorId.slice("extension.".length);
  const head = extensionId.split(".", 1)[0];
  if (!CANONICAL_EXTENSION_ID.test(extensionId) || head.startsWith("pi_") || UUID.test(head)) return "";
  return extensionId;
}
