import repeaterStyles from "@/components/workflow-inputs/workflow-input-fields.css?raw";
import operatorStyles from "@/pages/operator/operator.css?raw";

describe("operator job view layout", () => {
  it("lays every job out as its review, the panel alone and readable when the job has no files", () => {
    expect(operatorStyles).not.toMatch(/fs-operator-job-columns/);
    // A visually hidden label inside the panel is placed against it, never against the page.
    expect(operatorStyles).toMatch(/\.fs-operator-review-scroll\s*\{\s*position:\s*relative;/);
    expect(operatorStyles).toMatch(/\.fs-operator-review\[data-original="none"\]\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\);/);
    expect(operatorStyles).toMatch(/\.fs-operator-review\[data-original="none"\] \.fs-operator-review-scroll > \*\s*\{[^}]*max-width:\s*var\(--fs-page-width-standard\);/);
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
  it("fills the space under the header and any banner from 80rem up, each column scrolling, and stacks below", () => {
    expect(operatorStyles).toMatch(/\.fs-shell-main:has\(\.fs-operator-review\)\s*\{\s*padding:\s*0;/);
    expect(operatorStyles).toMatch(/\.fs-operator-review\s*\{\s*display:\s*grid;\s*grid-template-columns:\s*minmax\(0, 1fr\);/);
    expect(operatorStyles).toMatch(
      /@media \(width >= 80rem\)\s*\{[^@]*\.fs-shell-inset:has\(\.fs-operator-review\)\s*\{\s*display:\s*flex;\s*flex-direction:\s*column;\s*height:\s*100dvh;/,
    );
    expect(operatorStyles).toMatch(
      /\.fs-operator-review\s*\{\s*flex:\s*1 1 auto;\s*grid-template-columns:\s*minmax\(340px, 38%\) minmax\(0, 1fr\);\s*min-height:\s*0;/,
    );
    // Below lg the decision stays in view at the bottom of the screen.
    expect(operatorStyles).toMatch(/\.fs-operator-review-foot\s*\{\s*position:\s*sticky;\s*bottom:\s*0;/);
  });
});
