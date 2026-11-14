import {
  cachedExtensionPageData,
  clearExtensionPageDataCache,
  extensionPageDataKey,
  rememberExtensionComponentData,
} from "@/pages/extensions/extension-page-data-cache";

describe("extension page data cache", () => {
  beforeEach(() => clearExtensionPageDataCache());

  it("keeps the last data of each component of a page", () => {
    const key = extensionPageDataKey(["acme.store", "runs", "p-1", "c-1", "{}"]);
    rememberExtensionComponentData(key, "grid", { items: [1] });
    rememberExtensionComponentData(key, "stats", { total: 2 });

    expect(cachedExtensionPageData(key)).toEqual({ grid: { items: [1] }, stats: { total: 2 } });
    expect(cachedExtensionPageData(extensionPageDataKey(["acme.store", "runs", "p-2", "c-1", "{}"]))).toBeUndefined();
  });

  it("forgets the least recently used page first", () => {
    for (let index = 0; index < 31; index += 1) {
      rememberExtensionComponentData(`page-${index}`, "grid", { index });
    }
    expect(cachedExtensionPageData("page-0")).toBeUndefined();
    expect(cachedExtensionPageData("page-30")).toEqual({ grid: { index: 30 } });
  });
});
