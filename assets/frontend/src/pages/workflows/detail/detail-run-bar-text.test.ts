import { fallbackContext } from "@/lib/i18n/context";
import type { JobDefinitionPayload, JobExecutionSnapshot } from "@/pages/jobs/detail/api";
import { runBarText, type RunBarTextInput } from "./detail-run-bar-text";

const t = fallbackContext.t;
const definition: JobDefinitionPayload = {
  job_id: "run", available: true, workflow_id: "order_fulfilment", revision: 3,
  steps: [
    { step_id: "classify", display_name: "Classify request", step_kind: "agent" },
    { step_id: "pack", display_name: "Pack in batches", step_kind: "batch", for_each_id: "fulfil" },
    { step_id: "ship", display_name: "Create shipment", step_kind: "connector", for_each_id: "fulfil" },
    { step_id: "send", display_name: "Send confirmation", step_kind: "invoke_workflow", group_id: "notify", lane_id: "email" },
    { step_id: "receipt", display_name: "Build receipt", step_kind: "transform", group_id: "notify", lane_id: "receipt" },
    { step_id: "approve", display_name: "Approve order", step_kind: "approval", override: { approval: { message: "Approve this order for fulfilment?" } } },
  ],
  groups: [{ group_id: "notify", display_name: "Notify customer" }],
  for_each: [{ for_each_id: "fulfil", display_name: "Fulfilment" }],
};

function snapshot(steps: JobExecutionSnapshot["steps"], extra: Partial<JobExecutionSnapshot> = {}): JobExecutionSnapshot {
  return { jobId: "run", definitionWorkflowId: "order_fulfilment", definitionRevision: 3, definitionSteps: [], steps, childInvocations: [], terminal: false, complete: true, ...extra };
}
const done = (stepId: string, index: number) => ({ stepId, stepExecutionId: `${stepId}-${index}`, status: "completed" });

function input(overrides: Partial<RunBarTextInput>): RunBarTextInput {
  return { statusCode: "in_progress", isTerminal: false, snapshot: null, definition, detail: {}, durationMs: 600, now: 0, timezone: "UTC", ...overrides };
}

it("names the running step, the steps done and the time, never ids", () => {
  const text = runBarText(input({ snapshot: snapshot([done("a", 1), done("b", 2), done("c", 3), { stepId: "classify", stepExecutionId: "x", status: "in_progress" }]) }), t);
  expect(text).toEqual({ tone: "running", title: "Test Run running", sub: "Classify request · 3 steps done · 0.6 s" });
});

it("says the loop item and every running lane", () => {
  const loop = runBarText(input({ durationMs: 6900, snapshot: snapshot([{ stepId: "pack", stepExecutionId: "p", status: "in_progress", forEachId: "fulfil", iteration: 0 },
    { stepId: "x", stepExecutionId: "x2", status: "completed", forEachId: "fulfil", iteration: 1 }]) }), t);
  expect(loop.sub).toBe("Pack in batches (Fulfilment · item 1 of 2) · 1 step done · 6.9 s");
  const lanes = runBarText(input({ durationMs: 11000, snapshot: snapshot([
    { stepId: "send", stepExecutionId: "s", status: "in_progress", groupId: "notify", laneId: "email" },
    { stepId: "receipt", stepExecutionId: "r", status: "running", groupId: "notify", laneId: "receipt" },
  ]) }), t);
  expect(lanes.sub).toBe("Send confirmation (Notify customer · lane email) + Build receipt (Notify customer · lane receipt) · 0 steps done · 11 s");
});

it("finishes with the time and the steps done", () => {
  const text = runBarText(input({ statusCode: "completed", isTerminal: true, durationMs: 14000, snapshot: snapshot(Array.from({ length: 20 }, (_, index) => done("s", index)), { terminal: true }) }), t);
  expect(text).toEqual({ tone: "success", title: "Test Run finished in 14 s", sub: "20 steps done" });
});

it("does not count the steps of a finished run from a step history read before it ended", () => {
  const text = runBarText(input({ statusCode: "completed", isTerminal: true, durationMs: 1000, snapshot: snapshot([]) }), t);
  expect(text).toEqual({ tone: "success", title: "Test Run finished in 1.0 s", sub: "" });
});

it("names where a failure happened and why", () => {
  const text = runBarText(input({ statusCode: "failed", isTerminal: true, snapshot: snapshot(
    [{ stepId: "ship", stepExecutionId: "fail", status: "failed", forEachId: "fulfil", iteration: 0 }],
    { runError: { message: "Action shipping.dhl · create_shipment needs a connection binding before this step can run.", stepId: "ship", stepExecutionId: "fail" },
      containerProgress: { progress: { passes: [{ container: { kind: "for_each", id: "fulfil" }, for_each: { selected_count: 2 } }] } } as unknown as JobExecutionSnapshot["containerProgress"] },
  ) }), t);
  expect(text).toEqual({ tone: "danger", title: "Failed at Create shipment",
    sub: "Fulfilment · item 1 of 2 · Action shipping.dhl · create_shipment needs a connection binding before this step can run." });
});

it("carries the approval request, or says to decide in the open panel", () => {
  const waiting = snapshot([...Array.from({ length: 7 }, (_, index) => done("s", index)), { stepId: "approve", stepExecutionId: "ap", status: "awaiting_approval" }]);
  expect(runBarText(input({ statusCode: "awaiting_approval", snapshot: waiting }), t)).toEqual({
    tone: "attention", title: "Approve order is waiting for your decision", sub: "Approve this order for fulfilment? · 7 steps done" });
  expect(runBarText(input({ statusCode: "awaiting_approval", snapshot: waiting, decisionPanelOpen: true }), t).sub).toBe("Decide in the panel · 7 steps done");
});

it("says until when a wait step waits", () => {
  const text = runBarText(input({ statusCode: "awaiting_timer", durationMs: 3000, detail: { runtime: { workflow_wait: { due_at: "2026-10-21T15:08:51Z" } } },
    snapshot: snapshot([{ stepId: "classify", stepExecutionId: "w", status: "awaiting_timer" }]) }), t);
  expect(text.title).toBe("Waiting until 03:08:51 PM");
  expect(text.sub).toBe("Classify request · 0 steps done · 3.0 s");
});
