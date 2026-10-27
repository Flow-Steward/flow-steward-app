// @ts-expect-error The shared platform kit is intentionally shipped as JavaScript.
import { PLATFORM_GRID_STYLES } from "../../../platform-kit/components/platform-grid-styles.js";

describe("platform grid styles", () => {
  it("keeps sticky action cells opaque with a separator during row interaction", () => {
    const style = document.createElement("style");
    style.textContent = PLATFORM_GRID_STYLES;
    document.head.append(style);
    const rules = Array.from(style.sheet?.cssRules || []).filter(
      (rule): rule is CSSStyleRule => rule instanceof CSSStyleRule,
    );

    const interactionRule = rules.find(
      (rule) =>
        rule.selectorText.includes("tbody td.row-actions-sticky") &&
        rule.selectorText.includes("tbody tr:hover td.row-actions-sticky") &&
        rule.selectorText.includes("tbody tr:focus-within td.row-actions-sticky"),
    );

    expect(interactionRule?.style.background).toBe(
      "var(--surface, var(--fs-table-card-bg, #18181f))",
    );
    expect(interactionRule?.style.borderLeft).toBe(
      "1px solid var(--border, var(--fs-table-card-border, rgb(255 255 255 / 10%)))",
    );
    style.remove();
  });
});
