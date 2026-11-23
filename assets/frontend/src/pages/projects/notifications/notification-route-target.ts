import type { NotificationConnection, ProjectNotificationProvider } from "./api";

/**
 * What a notification route addresses, decided by its provider and the
 * connection it names.
 *
 * A connection says for itself whether it delivers to a recipient (an address
 * typed on the route), to a channel the route picks, or to the one channel it is
 * bound to. A provider whose connections disagree, or which has a bound channel
 * type, needs the route to name its connection first. A provider whose
 * connections all address the same kind keeps working without one, as it always
 * did. Nothing here reads a provider key: everything comes from the listing.
 */
export type RouteTarget =
  | { kind: "recipient" }
  | { kind: "channel" }
  | { kind: "fixed_channel"; label: string }
  /** `missing` is true when no connection is named; false when the named one cannot carry routes. */
  | { kind: "connection_required"; missing: boolean }
  /** The provider is not in the inventory (disabled, uninstalled, not loaded). */
  | { kind: "unknown" };

export const RECIPIENT_TARGET_KIND = "recipient";
export const CHANNEL_TARGET_KIND = "channel";
const CONNECTION_CHANNEL_SOURCE = "connection";

/** The provider's connections a route may name: eligible and enabled. */
export function routeConnections(
  provider: ProjectNotificationProvider | null | undefined,
): NotificationConnection[] {
  return (provider?.connections ?? []).filter(
    (connection) =>
      connection.connection_id
      && connection.enabled !== false
      && (connection.target_kind === RECIPIENT_TARGET_KIND || connection.target_kind === CHANNEL_TARGET_KIND),
  );
}

/** Whether every route of this provider must name its connection. */
export function providerRequiresConnection(provider: ProjectNotificationProvider | null | undefined): boolean {
  return provider?.requires_connection === true;
}

/** The connection a provider-change should preselect: the only one there is, or none. */
export function defaultRouteConnectionId(provider: ProjectNotificationProvider | null | undefined): string {
  if (!provider) return "";
  if (!providerRequiresConnection(provider)) {
    return typeof provider.connection_id === "string" ? provider.connection_id.trim() : "";
  }
  const connections = routeConnections(provider);
  return connections.length === 1 ? connections[0].connection_id : "";
}

function connectionTarget(connection: NotificationConnection): RouteTarget {
  if (connection.target_kind === RECIPIENT_TARGET_KIND) {
    return { kind: "recipient" };
  }
  if (connection.channel_source === CONNECTION_CHANNEL_SOURCE) {
    return {
      kind: "fixed_channel",
      label: connection.channel_label || connection.display_name || connection.connection_id,
    };
  }
  return { kind: "channel" };
}

export function resolveRouteTarget(
  provider: ProjectNotificationProvider | null | undefined,
  connectionId: string,
): RouteTarget {
  if (!provider) {
    return { kind: "unknown" };
  }
  const id = connectionId.trim();
  const connection = id
    ? routeConnections(provider).find((candidate) => candidate.connection_id === id)
    : undefined;
  if (connection) {
    return connectionTarget(connection);
  }
  if (providerRequiresConnection(provider)) {
    return { kind: "connection_required", missing: !id };
  }
  // A single-kind provider: its declared kind decides, with or without a connection.
  return provider.notification_target_kind === RECIPIENT_TARGET_KIND
    ? { kind: "recipient" }
    : { kind: "channel" };
}

/**
 * The key a channel list is cached under. A provider that needs a connection
 * lists channels per connection; any other provider lists them once, as before.
 */
export function channelLookupKey(
  provider: ProjectNotificationProvider | null | undefined,
  providerKey: string,
  connectionId: string,
): { key: string; connectionId: string } {
  const id = providerRequiresConnection(provider) ? connectionId.trim() : "";
  return { key: id ? `${providerKey}\u0000${id}` : providerKey, connectionId: id };
}

/** Errors the server returns when a route's connection does not fit its provider. */
export const ROUTE_CONNECTION_ERROR_CODES: ReadonlySet<string> = new Set([
  "notification_connection_required",
  "notification_connection_incompatible",
]);
