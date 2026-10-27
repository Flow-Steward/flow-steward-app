import type { I18nContextValue } from "@/lib/i18n/context";
import { MCP_CATALOG_QUERY_KEY, type McpCatalogItem } from "./api";
export type McpProjectOption = { id: string; name?: string };
type Translate = I18nContextValue["t"];

export function signInLabel(mode: string, t: Translate): string {
  if (mode === "oauth2") return t("react.registry.mcp_catalog.sign_in_oauth", "Sign in with the service");
  if (mode === "headers") return t("react.registry.mcp_catalog.sign_in_key", "API key or token");
  return t("react.registry.mcp_catalog.sign_in_none", "No sign-in");
}

const withoutTrailingSlash = (url: string) => {
  const trimmed = url.trim();
  let end = trimmed.length;
  while (end > 0 && trimmed[end - 1] === "/") end -= 1;
  return trimmed.slice(0, end);
};

/**
 * The page's one "Documentation" link: the server's own help page, else the
 * vendor's evidence page; "" when that address is already shown as the website
 * or a remote's setup guide (the designer: never the same address twice).
 */
export function documentationUrl(item: Pick<McpCatalogItem, "official" | "website_url" | "help_url" | "remotes">): string {
  const candidate = item.help_url || item.official?.evidence_url || "";
  if (!candidate) return "";
  const shown = [item.website_url, ...item.remotes.map((remote) => remote.auth.setup_url)]
    .filter(Boolean)
    .map(withoutTrailingSlash);
  return shown.includes(withoutTrailingSlash(candidate)) ? "" : candidate;
}

export function itemQueryKey(projectId: string, itemId: string) {
  return [...MCP_CATALOG_QUERY_KEY, projectId, "item", itemId] as const;
}
