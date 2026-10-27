import { createAndBindWorkflowHook, fetchWorkflowHooks, HookProjectRequiredError } from "@/pages/workflows/shared/hook-api";

describe("hook requests without a project (R2-10)", () => {
  it("never call /api/projects//… with an empty project id", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    try {
      expect(() => createAndBindWorkflowHook("", "order_flow", "adjust_order", { expected_revision: 0, child_workflow_id: "order_flow_adjust_order" })).toThrow(HookProjectRequiredError);
      expect(() => fetchWorkflowHooks("  ", "order_flow")).toThrow(HookProjectRequiredError);
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      fetchSpy.mockRestore();
    }
  });
});
