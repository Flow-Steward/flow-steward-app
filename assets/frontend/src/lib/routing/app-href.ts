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
