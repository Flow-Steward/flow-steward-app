import { fetchJson } from "@/lib/http/client";
import { fallbackContext, type I18nContextValue } from "@/lib/i18n/context";
import type { ExtensionRecord } from "@/pages/extensions/api";
import type { ExtensionCatalogEntry } from "@/pages/extensions/catalog-api";
import type { RequestState } from "@/pages/extensions/marketplace-model";

/**
 * Requests to install an extension (ADR 0084): an administrator of a company
 * account asks for a catalog extension; the installation administrator installs
 * it through the catalog's own install call and approves, which turns it on for
 * that company, or declines it with a reason.
 */
export type ExtensionInstallRequestStatus = "pending" | "approved" | "declined" | "cancelled";

export type ExtensionInstallRequest = {
  id: string;
  account_id: string;
  account_name: string;
  extension_id: string;
  extension_name: string;
  requested_by: string;
  requested_by_name: string;
  note: string;
  status: ExtensionInstallRequestStatus;
  decided_at: string;
  decline_reason: string;
  created_at: string;
};

/**
 * `installation`: every pending request, for the installation administrator.
 * `account`: this company's own requests, newest first.
 */
export type ExtensionInstallRequestList = {
  scope: "installation" | "account";
  items: ExtensionInstallRequest[];
};

export const EXTENSION_INSTALL_REQUESTS_QUERY_KEY = ["extensions", "install-requests"] as const;

/** At most this many characters in a note or a reason, as the server allows. */
export const INSTALL_REQUEST_TEXT_LIMIT = 500;

const BASE = "/api/extensions/install-requests";

export async function fetchExtensionInstallRequests() {
  return fetchJson<ExtensionInstallRequestList>(BASE, { cache: "no-store" });
}

export async function requestExtensionInstall(extensionId: string, note = "") {
  return fetchJson<ExtensionInstallRequest>(BASE, {
    method: "POST",
    body: JSON.stringify({ extension_id: extensionId, note: note.trim() }),
  });
}

export async function cancelExtensionInstallRequest(requestId: string) {
  return fetchJson<ExtensionInstallRequest>(`${BASE}/${encodeURIComponent(requestId)}/cancel`, { method: "POST" });
}

export async function approveExtensionInstallRequest(requestId: string) {
  return fetchJson<ExtensionInstallRequest>(`${BASE}/${encodeURIComponent(requestId)}/approve`, { method: "POST" });
}

export async function declineExtensionInstallRequest(requestId: string, reason = "") {
  return fetchJson<ExtensionInstallRequest>(`${BASE}/${encodeURIComponent(requestId)}/decline`, {
    method: "POST",
    body: JSON.stringify({ reason: reason.trim() }),
  });
}

/** The newest request of each extension; the list arrives newest first. */
export function latestRequestByExtension(
  items: readonly ExtensionInstallRequest[],
): Record<string, ExtensionInstallRequest> {
  const latest: Record<string, ExtensionInstallRequest> = {};
  for (const item of items) {
    if (!latest[item.extension_id]) latest[item.extension_id] = item;
  }
  return latest;
}

/**
 * Installed on this server but not turned on for this company: offered as
 * "Enable for this company", never requested again.
 */
export function enableableExtensions(extensions: readonly ExtensionRecord[]): Record<string, ExtensionRecord> {
  return Object.fromEntries(
    extensions
      .filter((extension) => extension.account_enablement === "not_enabled" || extension.account_enablement === "disabled")
      .map((extension) => [extension.extension_id, extension]),
  );
}

/** Whether the catalog item is already on this server, so Install only needs approving. */
export function catalogEntryInstalled(entry: ExtensionCatalogEntry | undefined): boolean {
  return Boolean(entry && entry.display_state !== "install" && entry.display_state !== "incompatible");
}

/** What an account administrator can do with one catalog item, besides reading it. */
export type InstallRequestActions = {
  /** This viewer asks instead of installing (a company account's administrator). */
  canRequest: boolean;
  latestById: Readonly<Record<string, ExtensionInstallRequest>>;
  enableableById: Readonly<Record<string, ExtensionRecord>>;
  busyById: Readonly<Record<string, boolean>>;
  onRequest: (entry: ExtensionCatalogEntry) => void;
  onCancel: (request: ExtensionInstallRequest) => void;
  onEnable: (extension: ExtensionRecord) => void;
};

/** The catalog item's request state for this viewer; none when they install directly. */
export function requestStateFor(
  extensionId: string,
  actions: InstallRequestActions | undefined,
): RequestState | undefined {
  if (!actions?.canRequest) return undefined;
  const latest = actions.latestById[extensionId];
  return {
    canRequest: true,
    pending: latest?.status === "pending",
    enableable: Boolean(actions.enableableById[extensionId]),
    declined: latest?.status === "declined",
    declineReason: latest?.decline_reason ?? "",
  };
}

/** The button's words for a request action. */
export function requestActionLabel(kind: "request" | "requested" | "enable", t: I18nContextValue["t"] = fallbackContext.t): string {
  if (kind === "requested") return t("react.extensions.cancel_install_request", "Cancel request");
  if (kind === "enable") return t("react.extensions.enable_for_this_company", "Enable for this company");
  return t("react.extensions.request_installation", "Request installation");
}

/** Run the request action the item offers. */
export function runRequestAction(
  kind: "request" | "requested" | "enable",
  entry: ExtensionCatalogEntry,
  actions: InstallRequestActions,
): void {
  if (kind === "request") {
    actions.onRequest(entry);
    return;
  }
  if (kind === "requested") {
    const latest = actions.latestById[entry.extension_id];
    if (latest) actions.onCancel(latest);
    return;
  }
  const extension = actions.enableableById[entry.extension_id];
  if (extension) actions.onEnable(extension);
}
