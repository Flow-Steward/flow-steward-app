import { describe, expect, it } from "vitest";

import themes from "@/design-system/01_tokens/themes.css?raw";
import root from "@/styles/index.css?raw";

function bareRootColorScheme(css: string): string | undefined {
  // The first `:root {` block (not `:root[data-theme=...]`) that sets color-scheme.
  for (const match of css.matchAll(/(^|[\s,}])(:root(?:,\s*\[data-theme="light"\])?)\s*\{([^}]*)\}/g)) {
    const scheme = /color-scheme:\s*(\w+)/.exec(match[3])?.[1];
    if (scheme) return scheme;
  }
  return undefined;
}

describe("default color scheme", () => {
  it("matches the default token theme when no theme is stored", () => {
    // Public pages (sign-in, OAuth consent) render before any theme is chosen. A
    // dark native scheme over light tokens painted plain text white on white.
    expect(bareRootColorScheme(themes)).toBe("light");
    expect(bareRootColorScheme(root)).toBe(bareRootColorScheme(themes));
  });
});
