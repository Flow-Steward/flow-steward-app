import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync("src/pages/registry/shared/catalog-surface.tsx", "utf8");

// A whole-token match: `fs-settings` must not be immediately preceded or
// followed by a class-name character, so `fs-settings-detail` and
// `fs-settings-section` (different classes, different rules) don't trip it.
const FS_SETTINGS_TOKEN = /(?<![\w-])fs-settings(?![\w-])/;

describe("RegistryCatalogSurface width", () => {
  it("declares the wide page mode", () => {
    expect(source).toMatch(/<PageContainer[^>]*width="wide"/);
  });

  it("imports the published container rather than rolling its own", () => {
    expect(source).toMatch(/PageContainer/);
    expect(source).not.toMatch(/max-w-\[/);
  });

  it("does not keep the legacy 1240px cap alongside it", () => {
    expect(source).not.toMatch(FS_SETTINGS_TOKEN);
  });
});

const OWN_PAGES = [
  "src/pages/registry/changelog/index.tsx",
  "src/pages/registry/mcp/index.tsx",
  "src/pages/registry/datasets/index.tsx",
  "src/pages/registry/data-connections/index.tsx",
  "src/pages/registry/workspaces/index.tsx",
];

describe("registry pages with their own layout", () => {
  for (const path of OWN_PAGES) {
    it(`${path} declares the wide mode and drops the legacy cap`, () => {
      const body = readFileSync(path, "utf8");
      expect(body).toMatch(/<PageContainer[^>]*width="wide"/);
      expect(body).not.toMatch(FS_SETTINGS_TOKEN);
    });
  }
});
