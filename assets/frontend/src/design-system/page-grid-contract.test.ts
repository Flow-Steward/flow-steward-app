import { describe, expect, it } from "vitest";

import themes from "@/design-system/01_tokens/themes.css?raw";
import index from "@/styles/index.css?raw";

/** Each token is declared once per file; duplicates indicate drift. */
function tokenValue(css: string, name: string): string {
  const matches = [...css.matchAll(new RegExp(`${name}:\\s*([^;]+);`, "g"))];
  expect(matches, `${name} must be declared exactly once`).toHaveLength(1);
  return matches[0][1].trim();
}

const SCALE: Array<[string, string]> = [
  ["sm", "40rem"],
  ["md", "48rem"],
  ["lg", "64rem"],
  ["xl", "80rem"],
  ["2xl", "96rem"],
];

describe("page grid tokens", () => {
  it("carries the published breakpoint scale, and only it", () => {
    for (const [name, value] of SCALE) {
      expect(tokenValue(themes, `--fs-breakpoint-${name}`)).toBe(value);
    }
    const declared = [...themes.matchAll(/--fs-breakpoint-[a-z0-9]+:/g)];
    expect(declared, "the scale is closed at five values").toHaveLength(5);
  });

  it("carries the published container widths", () => {
    expect(tokenValue(themes, "--fs-page-width-standard")).toBe("1280px");
    expect(tokenValue(themes, "--fs-page-width-wide")).toBe("1600px");
    expect(tokenValue(themes, "--fs-form-block-measure")).toBe("880px");
  });

  it("derives the gutters from the spacing scale rather than literals", () => {
    expect(tokenValue(themes, "--fs-page-gutter")).toBe("var(--fs-space-300)");
    expect(tokenValue(themes, "--fs-page-gutter-compact")).toBe("var(--fs-space-200)");
  });

  it("introduces no global field width token", () => {
    expect(/--fs-(field|input)-width\b/.test(themes)).toBe(false);
  });
});

describe("Tailwind theme mirror", () => {
  it("declares a @theme block", () => {
    expect(index).toMatch(/@theme\s*\{/);
  });

  it("mirrors every breakpoint literally and identically", () => {
    // Tailwind 4 generates its variants at build time, so @theme needs literal
    // values — var() would produce variants that never fire. The mirror is
    // therefore a copy, and this test is what keeps the copy honest.
    for (const [name, value] of SCALE) {
      expect(tokenValue(index, `--breakpoint-${name}`)).toBe(value);
      expect(tokenValue(index, `--breakpoint-${name}`)).toBe(tokenValue(themes, `--fs-breakpoint-${name}`));
    }
  });

  it("declares no breakpoint the published scale does not have", () => {
    const declared = [...index.matchAll(/--breakpoint-([a-z0-9]+):/g)].map((m) => m[1]);
    expect(declared.sort()).toEqual(SCALE.map(([name]) => name).sort());
  });
});
