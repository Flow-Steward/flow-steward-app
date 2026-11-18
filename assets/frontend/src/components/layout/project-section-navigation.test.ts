import { buildProjectSectionNavigation, firstSectionPath } from "@/components/layout/project-section-navigation";
import type { ProjectListItem } from "@/pages/projects/list/api";

function project(id: string, name: string, sections: Array<{ id: string; title: string; order?: number }>): ProjectListItem {
  return { id, name, user_sections: sections.map((section) => ({ workflow_id: "wf", ...section })) };
}

describe("buildProjectSectionNavigation", () => {
  it("shows no groups when no project has sections", () => {
    expect(buildProjectSectionNavigation([project("p1", "A", [])], { singleLabel: "Sections" })).toEqual([]);
  });

  it("puts one project's sections at the top level, without the project name", () => {
    const nav = buildProjectSectionNavigation(
      [project("p1", "Acme", [{ id: "b", title: "Invoices", order: 2 }, { id: "a", title: "Receipts", order: 1 }]), project("p2", "Empty", [])],
      { singleLabel: "Sections" },
    );
    expect(nav).toHaveLength(1);
    expect(nav[0].label).toBe("Sections");
    expect(nav[0].items.map((item) => item.label)).toEqual(["Receipts", "Invoices"]);
    expect(nav[0].items[0].to).toBe("/projects/p1/sections/a");
  });

  it("groups several projects by name and keeps alike sections apart", () => {
    const nav = buildProjectSectionNavigation(
      [project("p1", "Acme", [{ id: "inv", title: "Invoices" }]), project("p2", "Beta", [{ id: "inv", title: "Invoices" }])],
      { singleLabel: "Sections" },
    );
    expect(nav.map((section) => section.label)).toEqual(["Acme", "Beta"]);
    const keys = nav.flatMap((section) => section.items.map((item) => item.key));
    expect(new Set(keys).size).toBe(2);
    expect(firstSectionPath(nav)).toBe("/projects/p1/sections/inv");
  });
});
