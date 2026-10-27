import { render, screen } from "@testing-library/react";

import { ShellLayout } from "@/components/layout/app-shell-layout";
import settingsStyles from "@/components/shared/design-system.css?raw";
import baseTokens from "@/design-system/01_tokens/base.css?inline";
import themeTokens from "@/design-system/01_tokens/themes.css?inline";
import shellStyles from "@/styles/design-system-shell.css?raw";

describe("ShellLayout", () => {
  it("owns one fixed sidebar, one header, and one main content boundary", () => {
    render(
      <ShellLayout
        header={<div>Header slot</div>}
        maintenance={<div>Maintenance slot</div>}
        railState="expanded"
        sidebar={<div>Sidebar slot</div>}
      >
        <div>Page slot</div>
      </ShellLayout>,
    );

    const layout = screen.getByTestId("app-shell-layout");
    expect(layout).toHaveAttribute("data-rail-state", "expanded");
    expect(screen.getByTestId("app-shell-sidebar-frame")).toHaveTextContent("Sidebar slot");
    expect(screen.getByTestId("app-shell-header-frame")).toHaveTextContent("Header slot");
    const maintenance = screen.getByTestId("app-shell-maintenance-frame");
    expect(maintenance).toHaveTextContent("Maintenance slot");
    expect(
      maintenance.compareDocumentPosition(screen.getByTestId("app-shell-header-frame")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByRole("main")).toHaveTextContent("Page slot");
  });

  it("exposes collapsed state to the same sidebar and content inset", () => {
    render(
      <ShellLayout header={<div />} railState="collapsed" sidebar={<div />}>
        <div />
      </ShellLayout>,
    );

    expect(screen.getByTestId("app-shell-layout")).toHaveAttribute(
      "data-rail-state",
      "collapsed",
    );
  });

  it("keeps the app Header and Sidebar Header on the same 64px visual track", () => {
    expect(themeTokens).toMatch(/--fs-sidebar-brand-height:\s*64px;/);
    expect(themeTokens).toMatch(/--fs-header-height:\s*64px;/);
    expect(shellStyles).toMatch(
      /\.fs-shell-header-frame\s*\{[^}]*min-height:\s*var\(--fs-header-height\);[^}]*background:\s*var\(--fs-header-bg\);/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-header\s*\{[^}]*height:\s*calc\(var\(--fs-header-height\) - 1px\);[^}]*box-sizing:\s*border-box;/s,
    );
  });

  it("accounts for the frame's own border-bottom inside the published 64px header height", () => {
    // .fs-shell-header-frame carries a 1px border-bottom (below) and is the
    // element min-height:var(--fs-header-height) is set on. Without both of
    // these, the border adds a 65th pixel instead of living inside the 64px
    // band: box-sizing:border-box makes the frame's min-height floor count
    // the border, and .fs-app-header's height is reduced by that same 1px so
    // the row plus the rule together still sum to exactly 64px. Verified by
    // measuring computed getBoundingClientRect() heights in a real browser
    // against the built CSS - jsdom does not lay out real box models, so this
    // regex is a proxy for the mechanism, not a substitute for that
    // measurement.
    expect(shellStyles).toMatch(
      /@media \(width >= theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-shell-header-frame\s*\{[^}]*box-sizing:\s*border-box;[^}]*min-height:\s*var\(--fs-header-height\);/,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-header\s*\{[^}]*height:\s*calc\(var\(--fs-header-height\) - 1px\);/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-shell-header-frame\s*\{[^}]*border-bottom:\s*1px solid var\(--fs-header-border\);/s,
    );
  });

  it("pins the canonical brand and workspace before the scrolling navigation", () => {
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar\s*\{[^}]*grid-template-rows:\s*var\(--fs-sidebar-brand-height\) auto minmax\(0, 1fr\) auto;/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__brand-header\s*\{[^}]*height:\s*var\(--fs-sidebar-brand-height\);[^}]*border-bottom:\s*1px solid var\(--fs-sidebar-border\);/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__brand-wordmark\s*\{[^}]*font-family:\s*var\(--fs-sidebar-logo-font\);[^}]*font-size:\s*var\(--fs-sidebar-logo-font-size\);[^}]*font-weight:\s*var\(--fs-sidebar-logo-font-weight\);/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar\[data-rail-state="collapsed"\] \.fs-app-sidebar__brand\s*\{[^}]*justify-content:\s*center;[^}]*padding-inline:\s*0;/s,
    );
  });

  it("preserves the workspace icon vertical center when the Sidebar collapses", () => {
    // Canon draws the collapsed switcher as a centred 32x32 square. The row keeps
    // its expanded height through the block margin, so the avatar's centre
    // does not move when the rail collapses.
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar\[data-rail-state="collapsed"\] \.fs-app-sidebar__workspace-trigger\s*\{[^}]*width:\s*var\(--fs-sidebar-collapsed-button-size\);[^}]*height:\s*var\(--fs-sidebar-collapsed-button-size\);[^}]*margin-block:\s*calc\(\(var\(--fs-sidebar-workspace-height\) - var\(--fs-sidebar-collapsed-button-size\)\) \/ 2\);/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar\[data-rail-state="collapsed"\] \.fs-app-sidebar__account-trigger\s*\{[^}]*height:\s*var\(--fs-sidebar-collapsed-button-size\);/s,
    );
  });

  it("keeps the non-rail composition below the tablet breakpoint", () => {
    expect(shellStyles).toMatch(
      /@media \(width < theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-shell-layout\s*\{[\s\S]*display: flex;[\s\S]*flex-direction: column;[\s\S]*\}[\s\S]*\.fs-shell-sidebar-frame\s*\{[\s\S]*position: static;[\s\S]*order: 2;[\s\S]*\}[\s\S]*\.fs-shell-inset\s*\{[\s\S]*order: 1;[\s\S]*margin-left: 0;/,
    );
    expect(shellStyles).toMatch(
      /@media \(width >= theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-shell-sidebar-frame\s*\{[\s\S]*position: fixed;[\s\S]*width: calc\(var\(--fs-shell-sidebar-width\) \+ 1px\);[\s\S]*\.fs-shell-inset\s*\{[\s\S]*margin-left: calc\(var\(--fs-shell-sidebar-width\) \+ 1px\);/,
    );
  });

  it("lets rail overlays escape while sidebar content owns scrolling", () => {
    expect(shellStyles).toMatch(
      /@media \(width >= theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-shell-sidebar-frame\s*\{[\s\S]*overflow: visible;/,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__content\s*\{[\s\S]*overflow-y: auto;/,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar\[data-rail-state="collapsed"\] \.fs-app-sidebar__content\s*\{[^}]*overflow: visible;/s,
    );
  });

  it("matches the collapsed Sidebar tooltip geometry from the design system", () => {
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__tooltip\s*\{[^}]*left: calc\(100% \+ 10px\);[^}]*top: 50%;[^}]*padding: 5px 10px;[^}]*border-radius: var\(--fs-tooltip-radius\);[^}]*font-size: 12px;[^}]*font-weight: var\(--fs-type-weight-medium\);[^}]*transform: translateY\(-50%\) translateX\(-4px\);/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__tooltip::before\s*\{[^}]*border-right-color: var\(--fs-sidebar-tooltip-bg\);/s,
    );
  });

  it("maps every shell tooltip to the compact semantic radius", () => {
    expect(baseTokens).toMatch(/--fs-radius-compact:\s*var\(--fs-radius-4\);/);
    expect(themeTokens).toMatch(/--fs-tooltip-radius:\s*var\(--fs-radius-compact,\s*4px\);/);
    expect(shellStyles).toMatch(
      /\.fs-shell-tooltip\s*\{[^}]*border-radius: var\(--fs-tooltip-radius\);/s,
    );
  });

  it("contains sidebar chrome without clipping escaping menus", () => {
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__brand-header,\s*\.fs-app-sidebar__workspace-section,\s*\.fs-app-sidebar__footer\s*\{[^}]*min-width:\s*0;/s,
    );
    expect(shellStyles).not.toMatch(
      /\.fs-app-sidebar__brand-header,\s*\.fs-app-sidebar__workspace-section,\s*\.fs-app-sidebar__footer\s*\{[^}]*overflow:\s*hidden;/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-shell-menu\s*\{[^}]*min-width:\s*0;/s,
    );
  });

  it("maps workspace, account trigger, and account-menu identity to the canonical Sidebar typography", () => {
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__workspace-meta\s*\{[^}]*font-size:\s*14px;[^}]*line-height:\s*1\.2;/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__account-meta\s*\{[^}]*font-size:\s*13px;[^}]*line-height:\s*1\.25;/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__workspace-key,\s*\.fs-app-sidebar__account-secondary\s*\{[^}]*font-size:\s*12px;/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-shell-menu__identity \.fs-app-sidebar__account-secondary\s*\{[^}]*font-size:\s*11px;/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__account-trigger \.fs-app-sidebar__account-name\s*\{[^}]*font-weight:\s*500;/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-shell-menu__identity \.fs-app-sidebar__account-name\s*\{[^}]*font-weight:\s*var\(--fs-type-weight-medium\);/s,
    );
  });

  it("uses canonical select-menu surface and dropdown hover tokens", () => {
    expect(shellStyles).toMatch(
      /\.fs-shell-menu-panel\s*\{[\s\S]*padding: var\(--fs-form-dialog-select-menu-padding\);[\s\S]*border-radius: var\(--fs-form-dialog-select-menu-radius\);[\s\S]*box-shadow: var\(--fs-form-dialog-select-menu-shadow\);/,
    );
    expect(shellStyles).toMatch(
      /\.fs-shell-menu-panel\s*\{[^}]*width:\s*224px;[^}]*box-sizing:\s*border-box;/s,
    );
    expect(shellStyles).toMatch(
      /\.fs-shell-menu__item:hover,\s*\.fs-shell-menu__action:hover\s*\{[\s\S]*background: var\(--fs-dropdown-item-hover-bg\);/,
    );
    expect(shellStyles).not.toMatch(/\.fs-app-sidebar__theme\s*\{/);
  });

  it("lets the sidebar return to content height in the non-rail composition", () => {
    expect(shellStyles).toMatch(
      /@media \(width < theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-app-sidebar\s*\{[\s\S]*height: auto;/,
    );
  });

  it("restores readable collapsed controls and contained menus below 768px", () => {
    expect(shellStyles).toMatch(
      /@media \(width < theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-app-sidebar\[data-rail-state="collapsed"\] \.fs-app-sidebar__group-label,[\s\S]*\{[^}]*position: static;[^}]*width: auto;[^}]*height: auto;[^}]*overflow: visible;[^}]*clip-path: none;/,
    );
    expect(shellStyles).toMatch(
      /@media \(width < theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-app-sidebar\[data-rail-state="collapsed"\] \.fs-app-sidebar__item\s*\{[^}]*justify-content: flex-start;[^}]*width: 100%;[^}]*margin-inline: 0;[^}]*padding: 0 8px;/,
    );
    expect(shellStyles).toMatch(
      /@media \(width < theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-app-sidebar\[data-rail-state="collapsed"\] \.fs-app-sidebar__workspace-trigger,[\s\S]*\{[^}]*justify-content: flex-start;[^}]*width: 100%;[^}]*margin-inline: 0;[^}]*padding: 0 8px;/,
    );
    expect(shellStyles).toMatch(
      /@media \(width < theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-shell-menu-panel\s*\{[^}]*left: 0;[^}]*right: 0;[^}]*width: auto;[^}]*max-width: 100%;/,
    );
  });

  it("contains persistent account status in expanded, collapsed, and mobile layouts", () => {
    expect(shellStyles).toMatch(
      /\.fs-app-sidebar__account-status\s*\{[^}]*min-width: 0;[^}]*max-width: 100%;[^}]*overflow-wrap: anywhere;/,
    );
    expect(shellStyles).toMatch(
      /@media \(width >= theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-app-sidebar\[data-rail-state="collapsed"\] \.fs-app-sidebar__account-status\s*\{[^}]*position: absolute;[^}]*left: calc\(100% \+ 4px\);[^}]*width: 240px;[^}]*max-width: calc\(100vw - var\(--fs-sidebar-width-collapsed\) - 12px\);/,
    );
    expect(shellStyles).toMatch(
      /@media \(width < theme\(--breakpoint-md\)\)\s*\{[\s\S]*\.fs-app-sidebar__account-status\s*\{[^}]*position: static;[^}]*width: auto;[^}]*max-width: 100%;/,
    );
  });

  it("removes rail and tooltip transitions when reduced motion is requested", () => {
    expect(shellStyles).toMatch(
      /@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*\.fs-shell-sidebar-frame,[\s\S]*\.fs-shell-inset,[\s\S]*\.fs-shell-tooltip\s*\{[\s\S]*transition: none;/,
    );
  });

  it("keeps the header row on the inset's edges whatever width the page declares", () => {
    // The toggle and breadcrumb sit by the rail and Search by the window edge;
    // a row capped to the page's width moved them on every change of page mode.
    expect(shellStyles).not.toMatch(/--fs-shell-content-cap/);
    expect(shellStyles).toMatch(/\.fs-app-header\s*\{[^}]*width:\s*100%/);
    expect(shellStyles).not.toMatch(/\.fs-app-header\s*\{[^}]*max-width:/);
    expect(shellStyles).not.toMatch(/\.fs-app-header\s*\{[^}]*margin-inline:\s*auto/);
    // The rule under the bar spans the frame, not the row.
    expect(shellStyles).toMatch(/\.fs-shell-header-frame\s*\{[^}]*border-bottom:/);
    expect(shellStyles).not.toMatch(/\.fs-app-header\s*\{[^}]*border-bottom:/);
  });

  it("keeps every shell/settings media query on the published breakpoint tokens, not a hand-written value", () => {
    // Matches the *shape* of a hand-written breakpoint - any numeric width
    // with a px/rem/em unit inside an @media condition - rather than a fixed
    // list of literals, so unit variants (47.9375rem), decimal-pixel
    // boundary forms (767.98px, -0.02px) and em values can't slip past by
    // not being on the list.
    const numericWidthUnit = /(\d+(?:\.\d+)?)(px|rem|em)\b/;
    // design-system-shell.css and design-system.css both also carry queries
    // on unpublished values that belong to a later slice (container-query
    // migration). Named here, not silently ignored, so the exemption is
    // visible and bounded rather than open-ended.
    const unpublishedPxExceptions = new Set([380, 430, 520, 560, 720, 940, 1200]);

    function findHandWrittenBreakpoints(css: string, label: string): string[] {
      const offenders: string[] = [];
      const mediaHeaderPattern = /@media\s*\(([^)]*)/g;
      let match: RegExpExecArray | null;
      while ((match = mediaHeaderPattern.exec(css)) !== null) {
        const condition = match[1];
        const widthMatch = numericWidthUnit.exec(condition);
        if (!widthMatch) continue;
        const [, rawValue, unit] = widthMatch;
        const value = Number.parseFloat(rawValue);
        if (unit === "px" && unpublishedPxExceptions.has(value)) continue;
        offenders.push(`${label}: @media (${condition.trim()}) — found ${rawValue}${unit}`);
      }
      return offenders;
    }

    const offenders = [
      ...findHandWrittenBreakpoints(shellStyles, "design-system-shell.css"),
      ...findHandWrittenBreakpoints(settingsStyles, "design-system.css"),
    ];

    expect(
      offenders,
      `found a hand-written breakpoint value inside @media — use theme(--breakpoint-*) instead:\n${offenders.join("\n")}`,
    ).toEqual([]);
  });
});
