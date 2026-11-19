import repeaterStyles from "@/components/workflow-inputs/workflow-input-fields.css?raw";
import operatorStyles from "@/pages/operator/operator.css?raw";

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
