import { trimRepeatedEnd } from "@/lib/utils/string-normalization";

export function appHref(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const configuredBase = trimRepeatedEnd(import.meta.env.BASE_URL || "/app/", "/");
  if (!configuredBase || configuredBase === "/") {
    return normalizedPath;
  }
  if (normalizedPath === configuredBase || normalizedPath.startsWith(`${configuredBase}/`)) {
    return normalizedPath;
  }
  return `${configuredBase}${normalizedPath}`;
}

/**
 * The router path for an app-absolute href (`/app/registry/...` → `/registry/...`), or null when the
 * href is not under the app's base or the app is not running under that base right now (the
 * router then has no basename, and only a full page load can reach the href).
 */
export function appRoutePath(href: string, currentPathname: string = globalThis.location?.pathname ?? ""): string | null {
  const configuredBase = trimRepeatedEnd(import.meta.env.BASE_URL || "/app/", "/");
  if (!configuredBase || configuredBase === "/") return href.startsWith("/") ? href : null;
  const underBase = (path: string) => path === configuredBase || path.startsWith(`${configuredBase}/`) || path.startsWith(`${configuredBase}?`);
  if (!underBase(href) || !underBase(currentPathname)) return null;
  const rest = href.slice(configuredBase.length);
  return rest.startsWith("/") ? rest : `/${rest}`;
}
