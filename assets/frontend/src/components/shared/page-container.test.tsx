import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PageContainer } from "@/components/shared/design-system";
import styles from "@/components/shared/design-system.css?raw";

describe("PageContainer", () => {
  it("publishes the declared mode on the element", () => {
    render(<PageContainer width="wide">content</PageContainer>);
    const el = screen.getByTestId("page-container");
    expect(el).toHaveAttribute("data-page-width", "wide");
    expect(el).toHaveClass("fs-page");
  });

  it("keeps a caller's own classes alongside the published one", () => {
    render(
      <PageContainer className="space-y-6" width="standard">
        content
      </PageContainer>,
    );
    const el = screen.getByTestId("page-container");
    expect(el).toHaveClass("fs-page");
    expect(el).toHaveClass("space-y-6");
  });

  it("renders its children", () => {
    render(<PageContainer width="fluid">hello</PageContainer>);
    expect(screen.getByText("hello")).toBeInTheDocument();
  });
});

describe(".fs-page contract", () => {
  it("centres itself and refuses to collapse", () => {
    expect(styles).toMatch(/\.fs-page\s*\{[^}]*margin-inline:\s*auto/);
    expect(styles).toMatch(/\.fs-page\s*\{[^}]*min-width:\s*0/);
  });

  it("takes every ceiling from a token", () => {
    expect(styles).toMatch(
      /\.fs-page\[data-page-width="standard"\]\s*\{\s*max-width:\s*var\(--fs-page-width-standard\)/,
    );
    expect(styles).toMatch(
      /\.fs-page\[data-page-width="wide"\]\s*\{\s*max-width:\s*var\(--fs-page-width-wide\)/,
    );
    expect(styles).toMatch(/\.fs-page\[data-page-width="fluid"\]\s*\{\s*max-width:\s*none/);
  });

  it("gives the form block its own measure, never the page", () => {
    expect(styles).toMatch(/\.fs-form-block\s*\{\s*max-width:\s*var\(--fs-form-block-measure\)/);
    expect(styles).not.toMatch(/\.fs-page[^{]*\{[^}]*--fs-form-block-measure/);
  });
});

describe("reduced motion", () => {
  it("collapses animation and transition duration under .fs-page too", () => {
    const block = styles.match(
      /@media \(prefers-reduced-motion: reduce\)\s*\{([^]*?)\n\}/,
    );
    expect(block).not.toBeNull();
    const body = block![1];
    expect(body).toMatch(/(?<![\w-])\.fs-page \*(?![\w:-])/);
    expect(body).toMatch(/(?<![\w-])\.fs-page \*::before(?![\w-])/);
    expect(body).toMatch(/(?<![\w-])\.fs-page \*::after(?![\w-])/);
  });
});

describe(".fs-page-sections", () => {
  it("carries the page's vertical rhythm and no width at all", () => {
    expect(styles).toMatch(
      /\.fs-page-sections\s*\{[^}]*display:\s*grid[^}]*gap:\s*var\(--fs-dashboard-section-gap\)/,
    );
    // The container owns width. A second width here would win over max-width
    // and the container would look broken.
    expect(styles).not.toMatch(/\.fs-page-sections\s*\{[^}]*(?<![\w-])width:/);
    expect(styles).not.toMatch(/\.fs-page-sections\s*\{[^}]*max-width:/);
  });
});

function pageFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...pageFiles(full));
    else if (entry.name.endsWith(".tsx") && !entry.name.includes(".test.")) out.push(full);
  }
  return out;
}

// A whole-token match: `fs-settings` must not be immediately preceded or
// followed by a class-name character, so `fs-settings-detail` and
// `fs-settings-section` (different classes, different rules) don't trip it.
const FS_SETTINGS_TOKEN = /(?<![\w-])fs-settings(?![\w-])/;

describe("page roots", () => {
  it("never puts the published container and the legacy width in the same file", () => {
    const offenders: string[] = [];
    for (const file of pageFiles("src/pages")) {
      const body = readFileSync(file, "utf8");
      if (body.includes("<PageContainer") && FS_SETTINGS_TOKEN.test(body)) {
        offenders.push(file);
      }
    }
    expect(
      offenders,
      "fs-settings caps at 1240px and would beat the container's max-width",
    ).toEqual([]);
  });

  it("never puts the published container and the legacy form measure on one page", () => {
    const offenders: string[] = [];
    for (const file of pageFiles("src/pages")) {
      const body = readFileSync(file, "utf8");
      if (body.includes("<PageContainer") && /(?<![\w-])fs-settings-detail(?![\w-])/.test(body)) {
        offenders.push(file);
      }
    }
    expect(
      offenders,
      "fs-settings-detail caps at 880px and would beat the page container's max-width",
    ).toEqual([]);
  });
});
