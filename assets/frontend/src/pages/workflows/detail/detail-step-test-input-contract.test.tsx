import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { useWorkflowTestRunController } from "./detail-test-run-controller";
import * as api from "../shared/simulate-api";

function setup(stepKind: string, rows: Record<string, unknown>[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const context = {
    workflowId: "wf", selectedProjectId: "project", serverRevision: 7,
    selectedStep: { runtimeId: "step", local: false, stepKind },
    stepContractPreviewQuery: { data: { locked_required: rows, custom: [{ name: "agent_custom", source: "", type: "text" }] } },
    workflowContractQuery: { data: {} }, stepTestRunBlockedReason: "", workflowTestRunBlockedReason: "",
  };
  const view = renderHook(() => useWorkflowTestRunController(context), { wrapper });
  onTestFinished(() => { view.unmount(); client.clear(); });
  return view;
}

afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

it.each(["http_request", "connector", "agent"])("excludes only literal configuration while preserving state and custom inputs for %s", async (kind) => {
  const launch = vi.spyOn(api, "testRunWorkflowStep").mockResolvedValue({ workflow_id: "wf", step_id: "step", step_kind: kind, status: "completed", needs_user_approval: false });
  const view = setup(kind, [
    { name: "method", source: "literal", resolved: true, type: "text" },
    { name: "configured", source: "literal:fixed", resolved: true, type: "text" },
    { name: "url", source: "state.target_url", type: "text" },
    { name: "agent_raw", source: "", type: "text" },
    { name: "unresolved", source: "skill.custom", type: "text" },
  ]);
  expect(view.result.current.stepTestRunInputRows.map((row) => row.name)).toEqual(["url", "agent_raw", "unresolved", "agent_custom"]);
  await act(async () => {
    view.result.current.setStepTestRunSummary("Exercise saved request");
    view.result.current.setStepTestRunInputs({ method: "DELETE", configured: "override", url: "https://example.test/orders", agent_raw: "raw", unresolved: "mapped", agent_custom: "custom" });
  });
  await act(async () => { await view.result.current.stepTestRunMutation.mutateAsync(); });
  expect(launch).toHaveBeenCalledWith(expect.objectContaining({
    inputs: { url: "https://example.test/orders", agent_raw: "raw", unresolved: "mapped", agent_custom: "custom" },
    inputBindings: [{ name: "url", source: "state.target_url", type: "text" }],
    expectedRevision: 7,
  }));
  view.unmount();
});

it("keeps runtime requirements separate from optional test overrides and literal configuration", async () => {
  const launch = vi.spyOn(api, "testRunWorkflowStep").mockResolvedValue({ workflow_id: "wf", step_id: "step", step_kind: "batch", status: "failed", needs_user_approval: false });
  const view = setup("batch", [
    { name: "source_path", source: "state.collected", required: true, resolved: true, type: "array" },
    { name: "optional_rows", source: "state.inputs.optional_rows", required: false, resolved: false, type: "array" },
    { name: "configured", source: "literal", required: true, resolved: true, type: "text" },
  ]);
  expect(view.result.current.stepTestRunInputRows.find((row) => row.name === "source_path")).toMatchObject({ requiredByStep: true, required: false });
  expect(view.result.current.stepTestRunInputRows.find((row) => row.name === "optional_rows")).toMatchObject({ requiredByStep: false, required: false });
  expect(view.result.current.stepTestRunInputRows.some((row) => row.name === "configured")).toBe(false);
  await act(async () => { view.result.current.setStepTestRunSummary("Exercise the intentional missing input"); });
  await act(async () => { await view.result.current.stepTestRunMutation.mutateAsync(); });
  expect(launch).toHaveBeenCalledWith(expect.objectContaining({ inputs: {}, inputBindings: [], expectedRevision: 7 }));
});
