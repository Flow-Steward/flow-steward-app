import type { ExtensionRecord } from "@/pages/extensions/api";

/**
 * The row is an extension the installation installed, seen from another
 * account: it is turned on or off for this company, never installed or
 * removed here (ADR 0020, step C6). Kept apart from the API module so pages
 * that mock the API still render.
 */
export function isAccountEnablementRow(extension: ExtensionRecord | null | undefined): boolean {
  return typeof extension?.account_enablement === "string" && extension.account_enablement !== "";
}
