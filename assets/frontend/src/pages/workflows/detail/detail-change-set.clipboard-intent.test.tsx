import { act, renderHook } from "@testing-library/react";
import { definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import { useWorkflowChangeSet } from "./detail-change-set";

it("keeps a staged copy's identity through a valid parameter edit and Redo, then retires it on Discard", () => {
  const serverDefinition = definitionFromWorkflow({ workflow_id: "weekly_report", revision: 5, steps: [], groups: [] });
  const view = renderHook(() => useWorkflowChangeSet({
    workflowId: "weekly_report", serverRevision: 5, serverDefinition,
    refetchWorkflowSurfaces: async () => undefined,
  }));
  const accepted = { current: null as ReturnType<typeof view.result.current.stageGraphIntent> };
  act(() => { accepted.current = view.result.current.stageGraphIntent([{
    op: "create_step", step: { step_id: "plan_2", step_kind: "set_variable", display_name: "Plan work copy" },
  }]); });
  expect(accepted.current).not.toBeNull();
  if (!accepted.current) throw new Error("Expected a staged graph intent");
  expect(view.result.current.hasGraphIntent(accepted.current)).toBe(true);
  act(() => { view.result.current.updateStagedStep("plan_2", { display_name: "Edited copy" }); });
  expect(view.result.current.operations).toEqual([{
    op: "create_step", step: { step_id: "plan_2", step_kind: "set_variable", display_name: "Edited copy" },
  }]);
  expect(view.result.current.hasGraphIntent(accepted.current)).toBe(true);
  // The parameter edit is its own history action: the first Undo takes back only it.
  act(() => { view.result.current.undoGraphIntent(); });
  expect(view.result.current.hasGraphIntent(accepted.current)).toBe(true);
  expect(view.result.current.operations).toEqual([{
    op: "create_step", step: { step_id: "plan_2", step_kind: "set_variable", display_name: "Plan work copy" },
  }]);
  act(() => { view.result.current.undoGraphIntent(); });
  expect(view.result.current.hasGraphIntent(accepted.current)).toBe(false);
  act(() => { view.result.current.redoGraphIntent(); });
  act(() => { view.result.current.redoGraphIntent(); });
  expect(view.result.current.hasGraphIntent(accepted.current)).toBe(true);
  act(() => { view.result.current.discard(); });
  expect(view.result.current.hasGraphIntent(accepted.current)).toBe(false);
  const replacement = { current: null as ReturnType<typeof view.result.current.stageGraphIntent> };
  act(() => { replacement.current = view.result.current.stageGraphIntent([{
    op: "create_step", step: { step_id: "plan_2", step_kind: "set_variable", display_name: "Another copy" },
  }]); });
  expect(replacement.current).not.toBeNull();
  expect(view.result.current.hasGraphIntent(accepted.current)).toBe(false);
  if (replacement.current) expect(view.result.current.hasGraphIntent(replacement.current)).toBe(true);
});
