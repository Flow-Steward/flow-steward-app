import { workspaceInitial } from "@/components/layout/workspace-initial";

// Canon navigation/sidebar.md, "The workspace mark".
describe("workspaceInitial", () => {
  it("is one upper-case character, the first of the name", () => {
    expect(workspaceInitial("Default workspace")).toBe("D");
    expect(workspaceInitial("acme operations")).toBe("A");
    expect(workspaceInitial("  Northwind Analytics ")).toBe("N");
  });

  it("skips a leading digit, emoji or symbol for the first letter", () => {
    expect(workspaceInitial("42 Labs")).toBe("L");
    expect(workspaceInitial("🚀 Rocket team")).toBe("R");
    expect(workspaceInitial("#support")).toBe("S");
    expect(workspaceInitial("Ärzte Team")).toBe("Ä");
  });

  it("falls back to the first character as written when there is no letter", () => {
    expect(workspaceInitial("2026")).toBe("2");
    expect(workspaceInitial("🚀")).toBe("🚀");
  });
});
