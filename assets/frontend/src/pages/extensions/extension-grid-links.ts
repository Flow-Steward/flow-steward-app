import { objectOrEmpty, stringFrom, valueAtPath } from "@/pages/extensions/extension-manifest-utils";
import { PAGE_PARAMETER_PREFIX } from "@/pages/extensions/extension-page-connection";

/**
 * Grid links of an extension page: another page of the same extension with
 * `p.<name>` parameters filled from the row, or an in-app path the row holds.
 * The host builds every address; an extension never supplies a scheme or a
 * host, and a value that is not a same-app path renders as plain text.
 */
const ROW_PLACEHOLDER = /\{row\.([^{}]*)\}/g;
const PAGE_PARAMETER_NAME = /^[a-z][a-z0-9_]{0,63}$/;
/** Address state an extension page keeps when it links to another of its pages. */
const KEPT_PAGE_STATE = new Set(["project_id", "extension_connection"]);

/** The router's path for an in-app address: the app's own base (`/app`) belongs to the router. */
export function appRouterPath(path: string): string {
  const base = (import.meta.env.BASE_URL || "/app/").replace(/\/$/, "");
  if (base && base !== "/" && (path === base || path.startsWith(`${base}/`) || path.startsWith(`${base}?`))) {
    const rest = path.slice(base.length);
    return rest.startsWith("/") ? rest : `/${rest}`;
  }
  return path;
}

/** An in-app path the router can open: `/…`, never `//host`, a backslash trick, a scheme or whitespace. */
export function inAppPath(value: unknown): string {
  const path = stringFrom(value);
  if (!path.startsWith("/") || path.startsWith("//") || path.startsWith("/\\")) return "";
  // A control character or whitespace anywhere could hide another destination.
  for (const character of path) {
    const code = character.codePointAt(0) ?? 0;
    if (code <= 0x20 || code === 0x7f || character === "\\") return "";
  }
  return appRouterPath(path);
}

function filledParameter(template: string, row: Record<string, unknown>): string | null {
  let missing = false;
  const value = template.replace(ROW_PLACEHOLDER, (_match, field: string) => {
    const raw = valueAtPath(row, field.trim());
    const text = raw === null || raw === undefined || typeof raw === "object" ? "" : stringFrom(raw);
    if (!text) missing = true;
    return text;
  });
  return missing ? null : value;
}

/** The address of a page of this extension, keeping the project and connection the page shows. */
export function extensionPageAddress(
  extensionId: string,
  pageId: string,
  parameters: Record<string, string>,
  currentSearch: string,
): string {
  const current = new URLSearchParams(currentSearch);
  const next = new URLSearchParams();
  for (const [key, value] of current) {
    if (KEPT_PAGE_STATE.has(key)) next.set(key, value);
  }
  next.set("extension_page", pageId);
  for (const [name, value] of Object.entries(parameters)) {
    if (PAGE_PARAMETER_NAME.test(name)) next.set(`${PAGE_PARAMETER_PREFIX}${name}`, value);
  }
  // URLSearchParams encodes every name and value.
  return `/extensions/${encodeURIComponent(extensionId)}?${next.toString()}`;
}

/** Where one grid link of one row goes, or "" when the row cannot fill it. */
export function gridLinkHref(
  rawLink: unknown,
  row: Record<string, unknown>,
  { extensionId, search }: Readonly<{ extensionId: string; search: string }>,
): string {
  const link = objectOrEmpty(rawLink);
  const pathField = stringFrom(link.path_field);
  if (pathField) return inAppPath(valueAtPath(row, pathField));
  const pageId = stringFrom(link.page);
  if (!pageId || !extensionId) return "";
  const parameters: Record<string, string> = {};
  for (const [name, template] of Object.entries(objectOrEmpty(link.params))) {
    const value = filledParameter(stringFrom(template), row);
    if (value === null) return "";
    parameters[name] = value;
  }
  return extensionPageAddress(extensionId, pageId, parameters, search);
}

/** The link a column declares; a `type: link` column without one holds an in-app path itself. */
export function gridColumnLink(column: Record<string, unknown>): Record<string, unknown> | null {
  if (column.link && typeof column.link === "object") return objectOrEmpty(column.link);
  if (stringFrom(column.type).toLowerCase() === "link") return { path_field: stringFrom(column.key) };
  return null;
}

/** The address with one page parameter set (or removed when empty); other page parameters stay. */
export function withPageParameter(search: string, name: string, value: string): string {
  const params = new URLSearchParams(search);
  const key = `${PAGE_PARAMETER_PREFIX}${name}`;
  if (value) params.set(key, value);
  else params.delete(key);
  const text = params.toString();
  return text ? `?${text}` : "";
}
