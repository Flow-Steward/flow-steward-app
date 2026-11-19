import { buildProjectSectionNavigation, firstSectionPath } from "@/components/layout/project-section-navigation";
import type { OperatorSectionItem } from "@/pages/operator/types";

function item(projectId: string, projectName: string, workflowId: string, title: string, runnable = true): OperatorSectionItem {
  return {
    project_id: projectId,
    project_name: projectName,
    workflow_id: workflowId,
    workflow_name: workflowId,
    title,
    runnable,
    blocked_reasons: runnable ? [] : [{ code: "operator.effects_not_acknowledged", message: "Not acknowledged" }],
  };
}

describe("buildProjectSectionNavigation", () => {
  it("shows nothing when no project has sections", () => {
    const nav = buildProjectSectionNavigation([], { singleLabel: "Sections" });
    expect(nav).toEqual([]);
    expect(firstSectionPath(nav)).toBe("");
  });

  it("puts one project's sections at the top level, without the project name", () => {
    const nav = buildProjectSectionNavigation(
      [item("p1", "Acme", "wf.invoices", "Invoices"), item("p1", "Acme", "wf.receipts", "Receipts", false)],
      { singleLabel: "Sections" },
    );
    expect(nav).toHaveLength(1);
    expect(nav[0].label).toBe("Sections");
    expect(nav[0].collapsible).toBeFalsy();
    // A blocked section stays in the menu; its page explains why.
    expect(nav[0].items.map((entry) => entry.label)).toEqual(["Invoices", "Receipts"]);
    expect(nav[0].items[0].to).toBe("/projects/p1/workflows/wf.invoices/work");
  });

  it("groups several projects into collapsible groups and keeps the same workflow apart", () => {
    const nav = buildProjectSectionNavigation(
      [item("p1", "Acme", "wf.invoices", "Invoices"), item("p2", "Beta", "wf.invoices", "Invoices"), item("p2", "Beta", "wf.receipts", "Receipts")],
      { singleLabel: "Sections" },
    );
    expect(nav.map((section) => section.label)).toEqual(["Acme", "Beta"]);
    expect(nav.every((section) => section.collapsible)).toBe(true);
    expect(nav[1].items.map((entry) => entry.label)).toEqual(["Invoices", "Receipts"]);
    const keys = nav.flatMap((section) => section.items.map((entry) => entry.key));
    expect(new Set(keys).size).toBe(3);
    expect(firstSectionPath(nav)).toBe("/projects/p1/workflows/wf.invoices/work");
  });
});
