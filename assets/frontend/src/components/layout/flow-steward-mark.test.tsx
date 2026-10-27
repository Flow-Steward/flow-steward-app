import { renderToStaticMarkup } from "react-dom/server";

import { FlowStewardMark } from "@/components/layout/flow-steward-mark";
import shellStyles from "@/styles/design-system-shell.css?raw";

describe("FlowStewardMark under strict style-src", () => {
  it("renders independent animated marks without an inline stylesheet", () => {
    const view = renderToStaticMarkup(<>
      <FlowStewardMark className="first-mark" />
      <FlowStewardMark className="second-mark" />
    </>);

    // React inserting a style element violates the actual app CSP even when
    // the stylesheet lives inside SVG. Rendering must not require a CSP hash.
    expect(view).not.toMatch(/<style(?:\s|>)/);
    const stillIds = Array.from(view.matchAll(/id="(fs-logo-[^"]+-still)"/g), (match) => match[1]);
    expect(stillIds).toHaveLength(2);
    expect(new Set(stillIds).size).toBe(2);
    for (const id of stillIds) {
      expect(view).toContain(`--fs-logo-still-fill:url(#${id})`);
    }
    expect(view.match(/<animateTransform\b/g)).toHaveLength(2);
    expect(view.match(/<path\b/g)).toHaveLength(6);
    expect(view.match(/class="fs-flow-mark__shape"/g)).toHaveLength(2);
    expect(view.match(/fill="url\(#fs-logo-[^"]+-flow\)"/g)).toHaveLength(2);
  });

  it("selects each mark's still gradient through the external reduced-motion rule", () => {
    expect(shellStyles).toMatch(
      /@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*?\.fs-flow-mark__shape\s*\{\s*fill: var\(--fs-logo-still-fill\);\s*\}/,
    );
  });
});
