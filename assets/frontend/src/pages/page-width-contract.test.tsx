import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import dashboardStyles from "@/pages/dashboard/dashboard-page.css?raw";

const WIDE_PAGES = [
  "src/pages/jobs/list/index.tsx",
  "src/pages/projects/list/index.tsx",
  "src/pages/trash/index.tsx",
  "src/pages/monitoring/index.tsx",
];

describe("operational list pages", () => {
  for (const path of WIDE_PAGES) {
    it(`${path} declares the wide mode and keeps no legacy cap`, () => {
      const body = readFileSync(path, "utf8");
      expect(body).toMatch(/<PageContainer[^>]*width="wide"/);
      expect(body).not.toMatch(/(?<![\w-])fs-settings(?![\w-])/);
    });
  }
});

const REMAINING_WIDE = [
  "src/pages/extensions/index.tsx",
  "src/pages/workflows/list/index.tsx",
];

describe("extensions hub and the workflow catalogue", () => {
  for (const path of REMAINING_WIDE) {
    it(`${path} declares the wide mode and keeps no legacy cap`, () => {
      const body = readFileSync(path, "utf8");
      expect(body).toMatch(/<PageContainer[^>]*width="wide"/);
      expect(body).not.toMatch(/(?<![\w-])fs-settings(?![\w-])/);
    });
  }
});

describe("dashboard", () => {
  it("declares the wide mode", () => {
    const body = readFileSync("src/pages/dashboard/index.tsx", "utf8");
    expect(body).toMatch(/<PageContainer[^>]*width="wide"/);
  });

  it("collapses its rail on the published viewport scale, not the old 1200px literal", () => {
    expect(dashboardStyles).not.toMatch(/container-type:\s*inline-size/);
    expect(dashboardStyles).toMatch(/@media[^{]*\(\s*width\s*<\s*theme\(--breakpoint-xl\)/);
    expect(dashboardStyles).not.toMatch(/@media[^{]*1200px/);
  });
});
