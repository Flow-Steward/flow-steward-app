import repeaterStyles from "@/components/workflow-inputs/workflow-input-fields.css?raw";
import operatorStyles from "@/pages/operator/operator.css?raw";

describe("operator job view layout", () => {
  it("puts what was sent beside what came back, the text result wider, one column when narrow", () => {
    expect(operatorStyles).toMatch(/\.fs-operator-job-columns\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) minmax\(0, 1fr\);/);
    expect(operatorStyles).toMatch(/\.fs-operator-job-columns--text\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) minmax\(0, 2fr\);/);
    expect(operatorStyles).toMatch(/@media \(max-width: 960px\)\s*\{\s*\.fs-operator-job-columns,\s*\.fs-operator-job-columns--text\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\);/);
  });
});

describe("operator job view at narrow width", () => {
  it("scrolls a result table inside its card, never the page", () => {
    // The repeater table scrolls sideways in its own frame...
    expect(repeaterStyles).toMatch(/\.fs-workflow-repeater-scroll\s*\{[^}]*max-width:\s*100%;[^}]*overflow-x:\s*auto;/);
    // ...and the value column may shrink below the table's width.
    expect(operatorStyles).toMatch(/\.fs-operator-value-row\s*\{[^}]*grid-template-columns:\s*minmax\(8rem, 14rem\) minmax\(0, 1fr\);/);
    expect(operatorStyles).toMatch(/\.fs-operator-value-row dd\s*\{[^}]*min-width:\s*0;/);
    expect(operatorStyles).toMatch(/@media \(max-width: 640px\)\s*\{\s*\.fs-operator-value-row\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\);/);
    expect(operatorStyles).toMatch(/\.fs-operator-readonly-form\s*\{[^}]*min-width:\s*0;[^}]*max-width:\s*100%;/);
  });
});

describe("operator review page layout", () => {
  it("fills the space under the header from lg up, each column scrolling, and stacks below", () => {
    expect(operatorStyles).toMatch(/\.fs-shell-main:has\(\.fs-operator-review\)\s*\{\s*padding:\s*0;/);
    expect(operatorStyles).toMatch(/\.fs-operator-review\s*\{\s*display:\s*grid;\s*grid-template-columns:\s*minmax\(0, 1fr\);/);
    expect(operatorStyles).toMatch(
      /@media \(width >= 64rem\)\s*\{\s*\.fs-operator-review\s*\{\s*grid-template-columns:\s*minmax\(340px, 38%\) minmax\(0, 1fr\);\s*height:\s*calc\(100dvh - var\(--fs-header-height\)\);/,
    );
    // Below lg the decision stays in view at the bottom of the screen.
    expect(operatorStyles).toMatch(/\.fs-operator-review-foot\s*\{\s*position:\s*sticky;\s*bottom:\s*0;/);
  });
});
