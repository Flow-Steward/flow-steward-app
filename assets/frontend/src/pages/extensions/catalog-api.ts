import { fetchJson } from "@/lib/http/client";
import { isHttpError } from "@/lib/http/errors";

/**
 * The display state one card is in. Cards and rows only show it as a badge;
 * the extension page turns `install` into Install and `update_available` into
 * Update, which runs the same catalog install call.
 */
export type ExtensionCatalogDisplayState =
  | "install"
  | "installed"
  | "update_available"
  | "installed_newer"
  | "installed_disabled"
  | "incompatible";

export type ExtensionCatalogAvailability = {
  kind: "extension_catalog_availability";
  configured: boolean;
  available: boolean;
  reason?: string;
  message?: string;
  catalog_revision?: string;
  published_at?: string;
  item_count: number;
  fetched_at?: string;
  stale: boolean;
  last_refresh_error?: string;
};

export type ExtensionCatalogEntry = {
  extension_id: string;
  version: string;
  name: string;
  summary: string;
  description?: string;
  categories: string[];
  capabilities: string[];
  repository_url: string;
  release_zip_url: string;
  release_zip_bytes: number;
  release_zip_sha256: string;
  permissions_summary: string[];
  setup_summary?: string;
  compatible_flow_steward?: { min?: string; max?: string };
  publication_status: string;
  verification_status: string;
  /** Runtime targets with their own archive; empty for a portable extension. */
  runtime_targets?: string[];
  /** False when the entry ships per-target archives and none is for this server. */
  available_for_host?: boolean;
  /** When the extension entered the catalog, and when this release did. */
  added_at?: string;
  updated_at?: string;
  /** Free search words; search matches them, no facet shows them. */
  tags?: string[];
  /** The logo, verified against the index digest and served by Flow Steward. */
  has_icon: boolean;
  icon_url: string;
  display_state: ExtensionCatalogDisplayState;
  compatible: boolean;
  installed_version: string;
  installed_install_id: string;
  installed_state: string;
  /**
   * For an update: permissions this release asks for that the installed one
   * did not. Empty when nothing is added or the installed set is unknown.
   */
  added_permissions?: string[];
};

export type ExtensionCatalogListing = {
  kind: "extension_catalog_listing";
  availability: ExtensionCatalogAvailability;
  catalog_revision?: string;
  schema_version?: string;
  /** Category id -> its name; the list is shared with the workflow catalog. */
  category_labels?: Record<string, string>;
  /** Capability id -> its name, from Core; an id without one is shown as itself. */
  capability_labels?: Record<string, string>;
  /** Permission scope -> its name, from Core. */
  permission_labels?: Record<string, string>;
  items: ExtensionCatalogEntry[];
};

/** Shared so the page can refresh the catalog after an install it drove itself. */
export const EXTENSION_CATALOG_QUERY_KEY = ["extensions", "catalog"] as const;

/** Load the current catalog joined with this account's installs. No ZIP is fetched. */
export async function fetchExtensionCatalog() {
  return fetchJson<ExtensionCatalogListing>("/api/extensions/catalog");
}

/**
 * Install one catalog release, or update an older install to it. The backend
 * fetches and verifies the archive and hands it to the existing installer, the
 * same one an uploaded ZIP goes through; the browser never touches the artifact.
 *
 * `version` and `releaseSha256` pin the exact release the card showed. The
 * listing and the install can be served by two processes with independent
 * caches, and a republished version keeps its number while changing its bytes,
 * so the backend refuses rather than substituting a release nobody was shown.
 *
 * `approvedBrowserAuthorityImpactHash` replays an approval the user granted
 * after a `412`, exactly as the file-upload install path does.
 */
export async function installExtensionFromCatalog(
  extensionId: string,
  version: string,
  releaseSha256: string,
  approvedBrowserAuthorityImpactHash = "",
) {
  return fetchJson<Record<string, unknown>>("/api/extensions/catalog/install", {
    method: "POST",
    body: JSON.stringify({
      extension_id: extensionId,
      version,
      release_sha256: releaseSha256,
      ...(approvedBrowserAuthorityImpactHash
        ? { approved_browser_authority_impact_hash: approvedBrowserAuthorityImpactHash }
        : {}),
    }),
  });
}

/**
 * The install refusal for a release the catalog no longer lists: a newer
 * version (or new bytes) was published after the page loaded its listing.
 */
export const CATALOG_RELEASE_CHANGED = "extension_catalog_release_changed";

/** The `code` a catalog endpoint refused with, or "". Never the message text. */
export function catalogErrorCode(error: unknown): string {
  if (!isHttpError(error)) return "";
  const payload = error.payload;
  if (typeof payload !== "object" || payload === null) return "";
  const code = (payload as { code?: unknown }).code;
  return typeof code === "string" ? code : "";
}

/** What a Marketplace install or update is doing right now. Owned by the page. */
export type ExtensionCatalogInstallPhase = "idle" | "installing" | "awaiting_approval";

/**
 * How the last attempt ended, so the extension's page and its card can frame
 * `status` without reading it: a failure is the server's reason under a danger
 * alert, a cancellation a plain notice, and an install is announced.
 * `superseded` means the catalog published another release after the page
 * listed this one: the listing is reloaded and nothing is installed until the
 * user presses the button again, now naming the new version.
 */
export type ExtensionCatalogInstallOutcome = "" | "installed" | "updated" | "failed" | "cancelled" | "superseded";

export type ExtensionCatalogInstallState = {
  phase: ExtensionCatalogInstallPhase;
  status: string;
  outcome?: ExtensionCatalogInstallOutcome;
};
