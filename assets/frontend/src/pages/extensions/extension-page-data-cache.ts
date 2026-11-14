/**
 * The last data each extension page showed, so a person who comes back to a
 * page (Back, a link, another tab of the hub) sees it at once while the page
 * loads it again. Kept in memory for the browser tab only, bounded, and keyed
 * by everything that decides what a query returns.
 */
type PageData = Record<string, Record<string, unknown>>;

const MAX_PAGES = 30;
const pages = new Map<string, PageData>();

export function extensionPageDataKey(parts: ReadonlyArray<string | undefined | null>): string {
  return parts.map((part) => String(part ?? "")).join("\u0000");
}

export function cachedExtensionPageData(key: string): PageData | undefined {
  const data = pages.get(key);
  if (data === undefined) return undefined;
  // Most recently used last, so the oldest page is the one dropped.
  pages.delete(key);
  pages.set(key, data);
  return data;
}

export function rememberExtensionComponentData(key: string, componentId: string, data: Record<string, unknown>) {
  const current = pages.get(key) ?? {};
  pages.delete(key);
  pages.set(key, { ...current, [componentId]: data });
  while (pages.size > MAX_PAGES) {
    const oldest = pages.keys().next().value;
    if (oldest === undefined) break;
    pages.delete(oldest);
  }
}

/** Test seam: forget every page. */
export function clearExtensionPageDataCache() {
  pages.clear();
}
