import { declaredWorkflowHooks } from "@/pages/workflows/shared/workflow-hooks";

describe("declaredWorkflowHooks", () => {
  it("reads the hooks Invoke steps declare, and nothing else", () => {
    expect(
      declaredWorkflowHooks([
        { step_id: "prepare", step_kind: "set_variable" },
        { step_id: "child", step_kind: "invoke_workflow", override: { invoke_workflow: { workflow_id: "other" } } },
        {
          step_id: "before_finish",
          step_kind: "invoke_workflow",
          display_name: "Step name",
          override: { invoke_workflow: { hook: { hook_id: "before_finish", display_name: "Before the order is finished", description: "Adjust it." } } },
        },
        { step_id: "nameless", override: { invoke_workflow: { hook: { hook_id: "nameless_hook" } } } },
      ]),
    ).toEqual([
      { hookId: "before_finish", stepId: "before_finish", displayName: "Before the order is finished", description: "Adjust it.", connected: null, targetWorkflowId: "", targetName: "", bindingRevision: 0 },
      { hookId: "nameless_hook", stepId: "nameless", displayName: "nameless_hook", description: "", connected: null, targetWorkflowId: "", targetName: "", bindingRevision: 0 },
    ]);
  });

  it("returns nothing for a missing definition", () => {
    expect(declaredWorkflowHooks(undefined)).toEqual([]);
  });
});
