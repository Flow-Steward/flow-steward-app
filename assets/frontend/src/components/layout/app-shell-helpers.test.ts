import { describe, expect, it } from "vitest";

import {
  defaultSearchRoute,
  isGlobalSearchCategory,
  normalizeGlobalSearchGroups,
  readPlatformGlobalSearchConfig,
  readStoredThemeMode,
  persistThemeMode,
} from "@/components/layout/app-shell-helpers";

const RELEASE_CORE_SEARCH_GROUPS = [
  "extensions",
  "guardrails",
  "data_contracts",
  "fields",
  "published_blocks",
  "datasets",
  "saved_queries",
  "model_providers",
  "model_routes",
  "users",
  "workspaces",
] as const;

describe("global search release-core fallback", () => {
  it("accepts every release-core category exposed by the backend registry", () => {
    const config = readPlatformGlobalSearchConfig();
    const configured = new Set(config.categories.map((category) => category.value));

    for (const group of RELEASE_CORE_SEARCH_GROUPS) {
      expect(isGlobalSearchCategory(group)).toBe(true);
      expect(configured.has(group)).toBe(true);
      expect(config.groupLabels[group]).toBeTruthy();
    }
  });

  it("does not expose or route retired knowledge search results", () => {
    const config = readPlatformGlobalSearchConfig();

    expect(isGlobalSearchCategory("kb")).toBe(false);
    expect(config.categories.map((category) => category.value)).not.toContain("kb");
    expect(config.groupLabels.kb).toBeUndefined();
    expect(
      normalizeGlobalSearchGroups({
        groups: [{ group: "kb", total: 1, items: [{ group: "kb", id: "kb-1" }] }],
      }),
    ).toEqual([]);
    expect(defaultSearchRoute({ group: "kb", id: "kb-1", project_id: "p-1" })).toBe(
      "/dashboard",
    );
  });
});

describe("stored theme preference", () => {
  it("reads the current preference and ignores an old-only preference", () => {
    localStorage.setItem("platform-theme", "light");
    expect(readStoredThemeMode()).toBeNull();

    localStorage.setItem("platform.ui_theme", "dark");
    expect(readStoredThemeMode()).toBe("dark");
  });

  it("writes only the current preference", () => {
    localStorage.setItem("platform-theme", "dark");

    persistThemeMode("light");

    expect(localStorage.getItem("platform.ui_theme")).toBe("light");
    expect(localStorage.getItem("platform-theme")).toBe("dark");
  });

  it("tolerates unavailable storage for reads and writes", () => {
    const read = vi.spyOn(localStorage, "getItem").mockImplementation(() => {
      throw new DOMException("Storage unavailable", "SecurityError");
    });
    const write = vi.spyOn(localStorage, "setItem").mockImplementation(() => {
      throw new DOMException("Storage unavailable", "SecurityError");
    });

    try {
      expect(readStoredThemeMode()).toBeNull();
      expect(() => persistThemeMode("light")).not.toThrow();
    } finally {
      read.mockRestore();
      write.mockRestore();
    }
  });
});
