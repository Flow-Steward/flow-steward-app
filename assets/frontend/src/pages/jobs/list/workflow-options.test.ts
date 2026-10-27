import { describe, expect, it } from "vitest";
import { findWorkflowSource, workflowFilterOptions, type WorkflowSource } from "./workflow-options";

describe("historical extension workflow identity", () => {
  it("preserves a historical name when an unrelated sole extension reuses its local workflow id", () => {
    const oldId = "extension.removed-shopify.import_data";
    const newId = "extension.current-gmail.import_data";
    const sources = new Map<string, WorkflowSource>([[newId, {
      id: newId, name: "Import Gmail messages", installId: "current-gmail", projectId: "",
    }]]);
    expect(findWorkflowSource(oldId, sources)).toBeNull();
    expect(workflowFilterOptions(new Map([[oldId, "Import Shopify products"]]), sources, {
      projects: new Map(), extensions: new Map([["current-gmail", "Gmail"]]),
    })).toEqual([{ value: oldId, label: "Import Shopify products" }]);
  });
});
