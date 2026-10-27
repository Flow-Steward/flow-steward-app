import { act, renderHook } from "@testing-library/react";
import type { WorkflowCanvasGraph, WorkflowCanvasIssue } from "@/pages/workflows/shared/api";
import { useFocusedValidationIssue } from "./detail-focused-validation-issue";

const issue: WorkflowCanvasIssue = { scope: "step", scope_id: "query", step_id: "query",
  severity: "error", code: "missing", title: "Query is missing", field_path: "steps.query.template" };
const graph = (revision: number, issues: WorkflowCanvasIssue[], workflowId = "author"): WorkflowCanvasGraph => ({
  workflow: { id: workflowId, display_name: "Author", enabled: false, callable_by_parent: false, revision },
  interface_contract: {}, output_contract: {}, steps: [], groups: [], edges: [], validation_issues: issues, automation_rules: [],
});
const initial = () => ({ workflowId: "author", projectId: "project-a", revision: 7,
  graph: graph(7, [issue]), isSuccess: true, isFetching: false });

it("keeps the current issue through pending/error/stale/foreign responses and clears only current clean validation", () => {
  const props = initial();
  const view = renderHook(useFocusedValidationIssue, { initialProps: props });
  act(() => view.result.current.setSelectedCanvasIssue(issue));
  for (const next of [
    { ...props, revision: 8, graph: graph(8, []), isFetching: true },
    { ...props, revision: 8, graph: graph(8, []), isSuccess: false },
    { ...props, revision: 8, graph: graph(7, []) },
    { ...props, revision: 8, graph: graph(8, [], "foreign") },
  ]) {
    view.rerender(next);
    expect(view.result.current.selectedCanvasIssue).toEqual(issue);
  }
  view.rerender({ ...props, revision: 8, graph: graph(8, []) });
  expect(view.result.current.selectedCanvasIssue).toBeNull();
  view.rerender({ ...props, revision: 9, graph: graph(9, [issue]) });
  expect(view.result.current.selectedCanvasIssue).toBeNull();
});

it("uses fresh wording for the same still-applicable issue without selecting a different field", () => {
  const props = initial();
  const view = renderHook(useFocusedValidationIssue, { initialProps: props });
  act(() => view.result.current.setSelectedCanvasIssue(issue));
  const fresh = { ...issue, title: "The selected query is still missing", suggested_action: "Choose a query" };
  view.rerender({ ...props, revision: 8, graph: graph(8, [fresh]) });
  expect(view.result.current.selectedCanvasIssue).toEqual(fresh);
  view.rerender({ ...props, revision: 9, graph: graph(9, [{ ...fresh, field_path: "steps.query.timeout" }]) });
  expect(view.result.current.selectedCanvasIssue).toBeNull();
});

it.each(["projectId", "workflowId"] as const)("retires %s A→B→A and ignores a retained old selection callback", (field) => {
  const props = initial();
  const view = renderHook(useFocusedValidationIssue, { initialProps: props });
  act(() => view.result.current.setSelectedCanvasIssue(issue));
  const oldSelect = view.result.current.setSelectedCanvasIssue;
  view.rerender({ ...props, [field]: "foreign" });
  expect(view.result.current.selectedCanvasIssue).toBeNull();
  view.rerender(props);
  act(() => oldSelect(issue));
  expect(view.result.current.selectedCanvasIssue).toBeNull();
  act(() => view.result.current.setSelectedCanvasIssue(issue));
  expect(view.result.current.selectedCanvasIssue).toEqual(issue);
});
