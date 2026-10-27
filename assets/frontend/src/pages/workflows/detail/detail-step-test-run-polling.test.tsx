import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { useWorkflowTestRunController } from "./detail-test-run-controller";
import * as api from "../shared/simulate-api";

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const context = {
    workflowId: "wf-long-wait", selectedProjectId: "project", serverRevision: 7,
    selectedStep: { runtimeId: "wait", local: false, stepKind: "wait" },
    stepContractPreviewQuery: { data: {} }, workflowContractQuery: { data: {} },
    stepTestRunBlockedReason: "", workflowTestRunBlockedReason: "",
  };
  return renderHook(() => useWorkflowTestRunController(context), { wrapper });
}

afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); localStorage.clear(); });

it.each(["awaiting_timer", "awaiting_child"])("keeps polling a step in %s beyond one minute until it completes", async (status) => {
  vi.useFakeTimers();
  const started = Date.now();
  const pending = { workflow_id: "wf-long-wait", step_id: "wait", step_kind: "wait", thread_id: "long-step", status, needs_user_approval: false };
  vi.spyOn(api, "testRunWorkflowStep").mockResolvedValue(pending);
  const fetchState = vi.spyOn(api, "fetchWorkflowTestRunState").mockImplementation(async () => ({
    ...pending, status: Date.now() - started >= 70_000 ? "completed" : status,
  }));
  const view = setup();
  await act(async () => { view.result.current.stepTestRunMutation.mutate(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
  expect(view.result.current.stepTestRunPayload?.status).toBe("completed");
  expect(view.result.current.stepTestRunMessage).toBe("Step test run completed successfully.");
  expect(fetchState.mock.calls.every(([thread, options]) => thread === "long-step" && options?.presentation === "background")).toBe(true);
  const completedCalls = fetchState.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(fetchState).toHaveBeenCalledTimes(completedCalls);
  view.unmount();
});

it("retries transient polling errors and cancels outstanding step polling on unmount", async () => {
  vi.useFakeTimers();
  const pending = { workflow_id: "wf-long-wait", step_id: "wait", step_kind: "wait", thread_id: "long-step", status: "awaiting_child", needs_user_approval: false };
  vi.spyOn(api, "testRunWorkflowStep").mockResolvedValue(pending);
  let resolveLate: ((state: typeof pending) => void) | undefined;
  const fetchState = vi.spyOn(api, "fetchWorkflowTestRunState")
    .mockRejectedValueOnce(new Error("temporary network failure"))
    .mockImplementation(() => new Promise((resolve) => { resolveLate = resolve; }));
  const view = setup();
  await act(async () => { view.result.current.stepTestRunMutation.mutate(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
  expect(fetchState).toHaveBeenCalledTimes(2);
  expect(view.result.current.stepTestRunPayload?.status).toBe("awaiting_child");
  view.unmount();
  await act(async () => { resolveLate?.(pending); await vi.advanceTimersByTimeAsync(10_000); });
  expect(fetchState).toHaveBeenCalledTimes(2);
});


type StepRunOwner = { workflowId: string; selectedProjectId: string; serverRevision: number };

function setupStepRunOwner(owner: StepRunOwner) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: PropsWithChildren) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const selectedStep = { runtimeId: "shared-step", local: false, stepKind: "set_variable" };
  const stepContractPreviewQuery = { data: { optional: [{ name: "note", type: "text", required: false, source: "state.inputs.note" }] } };
  const workflowContractQuery = { data: {} };
  return renderHook((currentOwner: StepRunOwner) => useWorkflowTestRunController({
    ...currentOwner, selectedStep, stepContractPreviewQuery, workflowContractQuery,
    stepTestRunBlockedReason: "", workflowTestRunBlockedReason: "",
  }), { wrapper, initialProps: owner });
}

it.each([
  { scope: "workflow", next: { workflowId: "workflow-b", selectedProjectId: "project-a", serverRevision: 7 } },
  { scope: "project", next: { workflowId: "workflow-a", selectedProjectId: "project-b", serverRevision: 7 } },
  { scope: "revision", next: { workflowId: "workflow-a", selectedProjectId: "project-a", serverRevision: 8 } },
])("does not present a finished isolated result under a different $scope with the same step id", async ({ next }) => {
  const owner = { workflowId: "workflow-a", selectedProjectId: "project-a", serverRevision: 7 };
  const payload = { workflow_id: owner.workflowId, step_id: "shared-step", step_kind: "set_variable", status: "completed", final_status: "completed", result: { value: "Only owner A" } };
  const submit = vi.spyOn(api, "testRunWorkflowStep").mockResolvedValue(payload);
  const view = setupStepRunOwner(owner);
  await act(async () => { await view.result.current.stepTestRunMutation.mutateAsync(); });
  expect(submit).toHaveBeenCalledWith(expect.objectContaining({ workflowId: owner.workflowId, projectId: owner.selectedProjectId, expectedRevision: 7, stepId: "shared-step" }));
  expect(view.result.current.stepTestRunPayload).toEqual(payload);
  view.rerender(next);
  expect(view.result.current.stepTestRunPayload).toBeNull();
  expect(view.result.current.stepTestRunMessage).toBe("");
  view.unmount();
});

it("retires a held isolated ACK after workflow A to B to A even when the step id and revision match", async () => {
  const owner = { workflowId: "workflow-a", selectedProjectId: "project-a", serverRevision: 7 };
  const payload = { workflow_id: owner.workflowId, step_id: "shared-step", step_kind: "set_variable", status: "completed", final_status: "completed", result: { value: "Retired admission" } };
  let acknowledge: ((value: api.StepTestRunResult) => void) | undefined;
  const submit = vi.spyOn(api, "testRunWorkflowStep").mockImplementation(() => new Promise((resolve) => { acknowledge = resolve; }));
  const view = setupStepRunOwner(owner);
  await act(async () => { view.result.current.stepTestRunMutation.mutate(); });
  expect(submit).toHaveBeenCalledTimes(1);
  view.rerender({ ...owner, workflowId: "workflow-b" });
  view.rerender(owner);
  await act(async () => { acknowledge?.(payload); });
  expect(view.result.current.stepTestRunPayload).toBeNull();
  expect(view.result.current.stepTestRunMessage).toBe("");
  expect(submit).toHaveBeenCalledTimes(1);
  view.unmount();
});


it("does not publish a held isolated failure after workflow A to B to A", async () => {
  const owner = { workflowId: "workflow-a", selectedProjectId: "project-a", serverRevision: 7 };
  let rejectSubmission: ((reason: Error) => void) | undefined;
  const submit = vi.spyOn(api, "testRunWorkflowStep").mockImplementation(() => new Promise((_resolve, reject) => { rejectSubmission = reject; }));
  const view = setupStepRunOwner(owner);
  await act(async () => { view.result.current.stepTestRunMutation.mutate(); });
  expect(submit).toHaveBeenCalledTimes(1);
  view.rerender({ ...owner, workflowId: "workflow-b" });
  view.rerender(owner);
  await act(async () => { rejectSubmission?.(new Error("Retired owner failure")); });
  expect(view.result.current.stepTestRunMessage).toBe("");
  expect(view.result.current.stepTestRunPayload).toBeNull();
  expect(submit).toHaveBeenCalledTimes(1);
  view.unmount();
});

it("retires an outstanding isolated poll after workflow A to B to A", async () => {
  vi.useFakeTimers();
  const owner = { workflowId: "workflow-a", selectedProjectId: "project-a", serverRevision: 7 };
  const pending = { workflow_id: owner.workflowId, step_id: "shared-step", step_kind: "set_variable", thread_id: "retired-step-thread", status: "awaiting_child", needs_user_approval: false };
  vi.spyOn(api, "testRunWorkflowStep").mockResolvedValue(pending);
  let finishPoll: ((value: api.WorkflowTestRunResult) => void) | undefined;
  const fetchState = vi.spyOn(api, "fetchWorkflowTestRunState").mockImplementation(() => new Promise((resolve) => { finishPoll = resolve; }));
  const view = setupStepRunOwner(owner);
  await act(async () => { await view.result.current.stepTestRunMutation.mutateAsync(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
  expect(fetchState).toHaveBeenCalledTimes(1);
  expect(fetchState).toHaveBeenCalledWith("retired-step-thread", { presentation: "background" });
  view.rerender({ ...owner, workflowId: "workflow-b" });
  view.rerender(owner);
  await act(async () => { finishPoll?.({ ...pending, status: "completed", final_status: "completed", outputs: { retired: true } }); });
  expect(view.result.current.stepTestRunPayload).toBeNull();
  expect(view.result.current.stepTestRunMessage).toBe("");
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(fetchState).toHaveBeenCalledTimes(1);
  view.unmount();
});

it("does not carry an isolated summary or same-name input into another project", async () => {
  const owner = { workflowId: "workflow-a", selectedProjectId: "project-a", serverRevision: 7 };
  const view = setupStepRunOwner(owner);
  const initialSummary = view.result.current.stepTestRunSummary;
  await act(async () => {
    view.result.current.setStepTestRunSummary("Private owner A summary");
    view.result.current.setStepTestRunInputs({ note: "Private owner A value" });
  });
  expect(view.result.current.stepTestRunSummary).toBe("Private owner A summary");
  expect(view.result.current.stepTestRunInputs.note).toBe("Private owner A value");
  view.rerender({ ...owner, selectedProjectId: "project-b" });
  expect(view.result.current.stepTestRunSummary).toBe(initialSummary);
  expect(view.result.current.stepTestRunInputs.note).toBe("");
  view.unmount();
});


it("retires a held isolated ACK and drafts when only Hook binding A to B to A changes", async () => {
  const ownerA = {
    workflowId: "shared-child", selectedProjectId: "project-a", serverRevision: 3,
    stepTestRunScope: JSON.stringify(["parent-workflow", 9, "hook-a", "shared-child"]),
  };
  const ownerB = { ...ownerA, stepTestRunScope: JSON.stringify(["parent-workflow", 9, "hook-b", "shared-child"]) };
  const payload = { workflow_id: ownerA.workflowId, step_id: "shared-step", step_kind: "set_variable", status: "completed", final_status: "completed", result: { value: "Retired Hook A admission" } };
  let acknowledge: ((value: api.StepTestRunResult) => void) | undefined;
  const submit = vi.spyOn(api, "testRunWorkflowStep").mockImplementation(() => new Promise((resolve) => { acknowledge = resolve; }));
  const view = setupStepRunOwner(ownerA);
  const initialSummary = view.result.current.stepTestRunSummary;
  await act(async () => {
    view.result.current.setStepTestRunSummary("Only Hook A draft");
    view.result.current.setStepTestRunInputs({ note: "Only Hook A note" });
  });
  await act(async () => { view.result.current.stepTestRunMutation.mutate(); });
  expect(submit).toHaveBeenCalledTimes(1);
  expect(submit).toHaveBeenCalledWith(expect.objectContaining({
    workflowId: "shared-child", projectId: "project-a", expectedRevision: 3, stepId: "shared-step",
    taskSummary: "Only Hook A draft", inputs: { note: "Only Hook A note" },
  }));
  view.rerender(ownerB);
  view.rerender(ownerA);
  await act(async () => { acknowledge?.(payload); });
  expect(view.result.current.stepTestRunPayload).toBeNull();
  expect(view.result.current.stepTestRunMessage).toBe("");
  expect(view.result.current.stepTestRunSummary).toBe(initialSummary);
  expect(view.result.current.stepTestRunInputs.note).toBe("");
  expect(submit).toHaveBeenCalledTimes(1);
  view.unmount();
});
