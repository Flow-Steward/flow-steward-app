import { isHttpError } from "@/lib/http/errors";
import type { I18nContextValue } from "@/lib/i18n/context";
import { translatedExtensionActionErrorGuidance } from "@/pages/extensions/extension-action-error-guidance";
import {
  listFromUnknown,
  objectOrEmpty,
  stringFrom,
  stringListFromUnknown,
} from "@/pages/extensions/extension-manifest-utils";

/** The URL parameter that keeps a page's chosen connection (ADR 0067). */
export const PAGE_CONNECTION_PARAM = "extension_connection";
/** URL page parameters: `p.<name>` keys merged into every query's params on the page. */
export const PAGE_PARAMETER_PREFIX = "p.";
const PAGE_PARAMETER_NAME = /^[a-z][a-z0-9_]{0,63}$/;
const PAGE_PARAMETER_MAX_COUNT = 20;
const PAGE_PARAMETER_MAX_LENGTH = 1024;

/** The connection types a page or a query declares; empty when it runs without one. */
export function declaredConnectionTypes(declaration: unknown): string[] {
  return stringListFromUnknown(objectOrEmpty(objectOrEmpty(declaration).connection).connection_type_ids);
}

export function pageConnectionTypes(page: Record<string, unknown> | undefined): string[] {
  return declaredConnectionTypes(page);
}

/** The connection types a query of the compiled manifest declares. */
export function queryConnectionTypes(manifest: unknown, queryId: string): string[] {
  const root = objectOrEmpty(manifest);
  const rows = listFromUnknown(root.query_definitions).length
    ? listFromUnknown(root.query_definitions)
    : listFromUnknown(objectOrEmpty(root.compiled_manifest).query_definitions);
  const query = rows.find((row) => stringFrom(row.query_id) === queryId);
  return query ? declaredConnectionTypes(query) : [];
}

/** Connections of this install, enabled, of one of the page's types. */
export function pageConnectionCandidates(
  rows: ReadonlyArray<Record<string, unknown>> | undefined,
  types: readonly string[],
  installId: string,
): Array<Record<string, unknown>> {
  if (!types.length) return [];
  return (rows || []).filter((row) => (
    row.enabled !== false
    && types.includes(stringFrom(row.connection_type))
    && (!installId || stringFrom(row.extension_install_id) === installId)
  ));
}

/** The connection a page uses: the one in the URL if it is a candidate, or the only one. */
export function selectedPageConnectionId(
  candidates: ReadonlyArray<Record<string, unknown>>,
  requestedId: string,
): string {
  if (requestedId && candidates.some((row) => stringFrom(row.connection_id) === requestedId)) {
    return requestedId;
  }
  return candidates.length === 1 ? stringFrom(candidates[0].connection_id) : "";
}

/** A query request's connection target: only for a query that declares a connection. */
export function queryConnectionTarget(
  manifest: unknown,
  queryId: string,
  connectionId: string,
): { target: { connection_id: string } } | Record<string, never> {
  return connectionId && queryConnectionTypes(manifest, queryId).length
    ? { target: { connection_id: connectionId } }
    : {};
}

/**
 * The target an action of a connection page sends: a row's or a form's fields,
 * with the page's connection last so a row value never replaces it. A connection
 * form's own selection stays when the action was not started from a row.
 */
export function pageActionTarget(
  target: Record<string, unknown>,
  pageConnectionId: string,
  fromRow: boolean,
): Record<string, unknown> {
  if (!pageConnectionId) return target;
  if (!fromRow && stringFrom(target.connection_id)) return target;
  return { ...target, connection_id: pageConnectionId };
}

/**
 * What a query component shows when its load failed: plain guidance for a code a
 * person can act on (an unapproved connection, a missing project), else the error.
 */
export function queryFailureMessage(error: unknown, t: I18nContextValue["t"], fallback: string): string {
  if (!(error instanceof Error)) return fallback;
  const payload = isHttpError(error) ? objectOrEmpty(error.payload) : {};
  return translatedExtensionActionErrorGuidance(t, stringFrom(payload.code), stringFrom(payload.message))
    || error.message
    || fallback;
}

/** The page's URL parameters, bounded; names are lowercase identifiers. */
export function pageUrlParameters(search: string): Record<string, string> {
  const parameters: Record<string, string> = {};
  for (const [key, value] of new URLSearchParams(search)) {
    if (!key.startsWith(PAGE_PARAMETER_PREFIX)) continue;
    const name = key.slice(PAGE_PARAMETER_PREFIX.length);
    if (!PAGE_PARAMETER_NAME.test(name) || value.length > PAGE_PARAMETER_MAX_LENGTH) continue;
    if (Object.keys(parameters).length >= PAGE_PARAMETER_MAX_COUNT) break;
    parameters[name] = value;
  }
  return parameters;
}

/** Drops the page's own URL state when the person leaves it for another page. */
export function withoutPageUrlParameters(params: URLSearchParams): URLSearchParams {
  for (const key of [...params.keys()]) {
    if (key.startsWith(PAGE_PARAMETER_PREFIX)) params.delete(key);
  }
  return params;
}
