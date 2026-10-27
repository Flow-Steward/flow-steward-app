import designSystemStyles from "@/components/shared/design-system.css?raw";
import globalStyles from "@/styles/index.css?raw";

describe("sideways overscroll", () => {
  it("keeps the page from rubber-banding sideways", () => {
    expect(globalStyles).toMatch(/html,\s*body\s*\{[^}]*overscroll-behavior-x:\s*none;/);
  });

  it("stops a sideways scroll at the edge of a table and of a tab row", () => {
    expect(designSystemStyles).toMatch(/\.fs-table-scroll\s*\{[^}]*overscroll-behavior-x:\s*contain;/);
    expect(designSystemStyles).toMatch(/\.fs-tabs-scroll\s*\{[^}]*overscroll-behavior-x:\s*contain;/);
  });
});
