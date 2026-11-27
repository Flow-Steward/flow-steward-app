import type { AuthSession, RegisteredPageRecord, SettingsPageRecord } from "@/lib/auth/AuthProvider";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { trimRepeatedEdges } from "@/lib/utils/string-normalization";

/**
 * Page permissions (the page registry's vocabulary) the server grants without
 * platform administration. Any other declared permission (`view_settings`,
 * `admin`, `manage_users`, ...) keeps the page to people who administer the
 * platform, as the built-in settings tabs are.
 */
const OPEN_PAGE_PERMISSIONS = new Set(["", "public", "always", "authenticated", "viewer", "operator"]);

type PageRecord = SettingsPageRecord | RegisteredPageRecord;

function routePath(path: unknown): string {
  const normalized = `/${trimRepeatedEdges(firstStringFromUnknownValue([path, ""]).trim(), "/")}`;
  return normalized === "/" ? "/" : normalized;
}

/** The bootstrap entry of the page at `path`: a settings page first, else a registered page. */
export function bootstrapPageAt(
  session: Pick<AuthSession, "settings_pages" | "registered_pages"> | null | undefined,
  path: string,
): PageRecord | undefined {
  const wanted = routePath(path);
  return (
    (session?.settings_pages ?? []).find((page) => routePath(page.path) === wanted)
    ?? (session?.registered_pages ?? []).find((page) => routePath(page.path) === wanted)
  );
}

/**
 * Whether a person who does not administer the platform may open this page: the
 * bootstrap listed it (the server checked its permission and entitlement) and the
 * permission it declares is one the server grants without administration. A
 * built-in (Core) page never opens this way.
 */
export function pageOpensWithoutAdministration(page: PageRecord | null | undefined): boolean {
  if (!page || page.core_page === true) return false;
  const declared = firstStringFromUnknownValue([page.required_permission, ""]).trim().toLowerCase();
  return OPEN_PAGE_PERMISSIONS.has(declared);
}
