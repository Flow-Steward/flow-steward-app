import type { JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import { hookExecutionMarks, hookInvocationForExecution } from "./detail-hook-execution-marks";

const frame = { hookId: "before", stepId: "before_step", targetWorkflowId: "client", targetRevision: 3 };
const graph = { steps: [{ id: "hook:before:send", step_id: "hook:before:send" }], groups: [], edges: [] } as never;
const link = { stepId: "before_step", stepExecutionId: "execution-2", invocationKey: "parent:execution-2", childJobId: "child", childWorkflowId: "client" };
function snapshot(overrides: Partial<JobExecutionSnapshot> = {}): JobExecutionSnapshot {
  return { jobId: "parent", definitionWorkflowId: "author", definitionRevision: 4,
    definitionSteps: [{ stepId: "before_step", hookId: "before" }],
    steps: [{ stepId: "before_step", stepExecutionId: "execution-2", occurrence: 2, status: "completed" }],
    childInvocations: [link], terminal: true, complete: true, ...overrides };
}

it("selects the executed hook's child using archived hook and step execution ownership", () => {
  expect(hookInvocationForExecution(snapshot(), frame, "author", 4)).toEqual(link);
  expect(hookInvocationForExecution(snapshot(), { ...frame, hookId: "after", stepId: "after_step" }, "author", 4)).toBeNull();
});
it("never paints a different parent revision, changed hook declaration or incomplete history", () => {
  expect(hookInvocationForExecution(snapshot(), frame, "author", 5)).toBeNull();
  expect(hookInvocationForExecution(snapshot({ definitionSteps: [{ stepId: "before_step", hookId: "old_hook" }] }), frame, "author", 4)).toBeNull();
  expect(hookInvocationForExecution(snapshot({ complete: false }), frame, "author", 4)).toBeNull();
});
it("does not reuse an earlier invocation when a newer hook execution has not linked its child", () => {
  const history = snapshot({ steps: [{ stepId: "before_step", stepExecutionId: "execution-3", occurrence: 3, status: "in_progress" }, ...snapshot().steps] });
  expect(hookInvocationForExecution(history, frame, "author", 4)).toBeNull();
});
it("refuses ambiguous child links for one parent execution", () => {
  expect(hookInvocationForExecution(snapshot({ childInvocations: [link, { ...link, childJobId: "another" }] }), frame, "author", 4)).toBeNull();
});
it("retains terminal child member marks for the exact displayed child revision", () => {
  const child = snapshot({ jobId: "child", definitionWorkflowId: "client", definitionRevision: 3,
    steps: [{ stepId: "send", stepExecutionId: "send-2", occurrence: 2, status: "failed" }, { stepId: "send", stepExecutionId: "send-1", occurrence: 1, status: "completed" }] });
  expect(hookExecutionMarks(child, frame, graph)).toEqual({ "hook:before:send": { status: "failed", jobId: "child", stepExecutionId: "send-2", occurrence: 2 } });
  expect(hookExecutionMarks({ ...child, definitionRevision: 2 }, frame, graph)).toEqual({});
  expect(hookExecutionMarks({ ...child, complete: false }, frame, graph)).toEqual({});
  expect(hookExecutionMarks(child, { ...frame, hookId: "after" }, graph)).toEqual({});
});
