import baseTokens from "@/design-system/01_tokens/base.css?raw";
import themeTokens from "@/design-system/01_tokens/themes.css?raw";

/** The light theme's rules: everything before the dark theme's block. */
const lightTheme = themeTokens.slice(0, themeTokens.indexOf('[data-theme="dark"] {'));
const darkTheme = themeTokens.slice(themeTokens.indexOf('[data-theme="dark"] {'));

describe("Design System New badge tokens (Iridescent)", () => {
  it("publishes the four Iridescent stops in the palette", () => {
    expect(baseTokens).toMatch(/--fs-iridescent-1:\s*#9457F5;/);
    expect(baseTokens).toMatch(/--fs-iridescent-2:\s*#8A6BF4;/);
    expect(baseTokens).toMatch(/--fs-iridescent-3:\s*#5E97F4;/);
    expect(baseTokens).toMatch(/--fs-iridescent-4:\s*#67CDEF;/);
  });

  it("gives the light theme the gradient, an 88% veil and primary ink", () => {
    expect(lightTheme).toContain(
      "--fs-badge-new-gradient: linear-gradient(120deg, var(--fs-iridescent-1) 0%, var(--fs-iridescent-2) 34%, var(--fs-iridescent-3) 64%, var(--fs-iridescent-4) 100%);",
    );
    expect(lightTheme).toContain("--fs-badge-new-veil: color-mix(in srgb, var(--fs-bg-surface) 88%, transparent);");
    expect(lightTheme).toContain("--fs-badge-new-text: var(--fs-text-primary);");
  });

  it("gives the dark theme a thinner 80% veil, so the gradient stays as visible", () => {
    expect(darkTheme).toContain("--fs-badge-new-veil: color-mix(in srgb, var(--fs-bg-surface) 80%, transparent);");
    expect(darkTheme).toContain("--fs-badge-new-text: var(--fs-text-primary);");
    // The gradient itself is the brand's, the same in both themes: declared once.
    expect(themeTokens.match(/--fs-badge-new-gradient:/g)).toHaveLength(1);
  });
});
