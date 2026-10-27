import baseTokens from "@/design-system/01_tokens/base.css?inline";
import fontFaces from "@/design-system/01_tokens/fonts.css?inline";
import themeTokens from "@/design-system/01_tokens/themes.css?inline";
import authStyles from "@/pages/auth/login/public-auth-sign-in.css?inline";
import settingsStyles from "@/components/shared/design-system.css?raw";
import shellStyles from "@/styles/design-system-shell.css?raw";

const migratedStyles = [shellStyles, settingsStyles, authStyles];

describe("Design System typography contract", () => {
  it("does not leave local foundation stylesheets as browser runtime imports", () => {
    expect(themeTokens).not.toMatch(
      /@import\s+(?:url\()?['"]?\.\/(?:fonts|base)\.css(?:\?[^'"\s)]*)?/,
    );
  });

  it("bundles each canonical family locally for the existing CSP", () => {
    expect(fontFaces).toContain('font-family: "Alan Sans"');
    expect(fontFaces).toContain('font-family: "Google Sans Flex"');
    expect(fontFaces).toContain('font-family: "DM Sans"');
    expect(fontFaces).toContain('font-family: "Google Sans Code"');
    expect(fontFaces).not.toMatch(/https?:\/\//);
  });

  it("exposes the five semantic roles and the canonical weight range", () => {
    expect(baseTokens).toMatch(/--ff-logo:\s*'Alan Sans'/);
    expect(baseTokens).toMatch(/--ff-display:\s*'Google Sans Flex'/);
    expect(baseTokens).toMatch(/--ff-body:\s*'Google Sans Flex'/);
    expect(baseTokens).toMatch(/--ff-data:\s*'DM Sans'/);
    expect(baseTokens).toMatch(/--ff-code:\s*'Google Sans Code'/);
    expect(baseTokens).toMatch(/--fs-type-weight-regular:\s*400;/);
    expect(baseTokens).toMatch(/--fs-type-weight-medium:\s*500;/);
  });

  it("routes component tokens to their intended semantic faces", () => {
    expect(themeTokens).toMatch(/--fs-sidebar-logo-font:\s*var\(--ff-logo\);/);
    expect(themeTokens).toMatch(/--fs-button-font:\s*var\(--ff-body\);/);
    expect(themeTokens).toMatch(/--fs-metric-card-figure-font:\s*var\(--ff-data\);/);
    expect(themeTokens).toMatch(/--fs-count-pill-font:\s*var\(--ff-data\);/);
    expect(themeTokens).toMatch(/--fs-form-title-font:\s*var\(--ff-display\);/);
  });

  it("sets dates in the data face with tabular figures, per the canon date contract", () => {
    expect(settingsStyles).toMatch(
      /\.fs-input\[type="date"\]\s*\{\s*font-family:\s*var\(--ff-data\);\s*font-variant-numeric:\s*tabular-nums;/,
    );
  });

  it("keeps token imports bundler-resolvable instead of leaking runtime @import requests", () => {
    // A query string (?v=...) makes the bundler treat the import as an external
    // URL, so it survives into the built CSS and 404s at runtime.
    expect(themeTokens).not.toMatch(/@import[^;]*\?/);
  });

  it("keeps migrated surfaces on whole-pixel sizes and weights 400 or 500", () => {
    for (const styles of migratedStyles) {
      expect(styles).not.toMatch(/font-size:\s*\d+\.\d+px/);
      expect(styles).not.toMatch(/font-weight:\s*(?:[0-3]\d{2}|[6-9]\d{2})\s*;/);
      expect(styles).not.toMatch(/font-family:\s*var\(--fs-font-(?:body|display|mono)\)/);
    }
  });
});
