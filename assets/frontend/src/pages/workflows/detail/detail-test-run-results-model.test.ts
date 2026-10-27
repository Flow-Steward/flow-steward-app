import { fallbackContext } from "@/lib/i18n/context";
import type { JobDefinitionPayload } from "@/pages/jobs/detail/api";
import {
  buildRunPasses,
  countPasses,
  declaredStepOutputTargets,
  definitionNames,
  formatRunDuration,
  approvalShownValues,
  keptOutputsFromPass,
  runPassState,
  valuePreview,
  valueText,
} from "./detail-test-run-results-model";

const t = fallbackContext.t;

it.each([
  [40, "< 0.1 s"], [400, "0.4 s"], [2400, "2.4 s"], [14_000, "14 s"], [null, ""], [-1, ""],
] as const)("says %s ms as %s", (ms, text) => {
  expect(formatRunDuration(ms, t)).toBe(text);
});

it("maps step statuses to the run words, and a kept result to Kept", () => {
  expect(runPassState("completed")).toBe("done");
  expect(runPassState("completed", true)).toBe("kept");
  expect(runPassState("failed")).toBe("failed");
  expect(runPassState("in_progress")).toBe("running");
  expect(runPassState("awaiting_approval")).toBe("waiting");
  expect(runPassState("skipped")).toBe("skipped");
});

const definition: JobDefinitionPayload = {
  job_id: "run", available: true,
  steps: [{ step_id: "fetch", display_name: "Fetch order", step_kind: "http_request" }, { step_id: "pack", display_name: "Pack in batches", step_kind: "batch" },
    { step_id: "send", display_name: "Send confirmation", step_kind: "invoke_workflow" }, { step_id: "approve", display_name: "Approve order", step_kind: "approval" }],
  groups: [{ group_id: "notify", display_name: "Notify customer" }],
  for_each: [{ for_each_id: "fulfil", display_name: "Fulfilment" }],
};

it("makes one row per pass with where it ran, and a header for each loop item", () => {
  const passes = buildRunPasses([
    { step_id: "fetch", step_execution_id: "f", status: "completed", duration_ms: 400, timestamp: "2026-10-21T10:00:01Z", inputs: { "inputs.order_id": "A-1001" }, outputs: { "state.order": { id: "A-1001" } } },
    { step_id: "approve", step_execution_id: "a", status: "completed", timestamp: "2026-10-21T10:00:02Z", outputs: { decision: "approved" } },
    { step_id: "pack", step_execution_id: "p1", status: "completed", for_each_id: "fulfil", iteration: 0, timestamp: "2026-10-21T10:00:03Z" },
    { step_id: "pack", step_execution_id: "p2", status: "failed", for_each_id: "fulfil", iteration: 1, error: "No stock", timestamp: "2026-10-21T10:00:04Z" },
    { step_id: "send", step_execution_id: "s", status: "completed", group_id: "notify", lane_id: "email", skill: "kept_test_output", timestamp: "2026-10-21T10:00:05Z" },
  ], definitionNames(definition), null, t);
  expect(passes.map((pass) => [pass.label, pass.place, pass.state])).toEqual([
    ["Fetch order", "Workflow", "done"],
    ["Approve order", "Workflow", "done"],
    ["Pack in batches", "Fulfilment · item 1 of 2", "done"],
    ["Pack in batches", "Fulfilment · item 2 of 2", "failed"],
    ["Send confirmation", "Notify customer · lane email", "kept"],
  ]);
  expect(passes[1]).toEqual(expect.objectContaining({ kindLabel: "Approval", decision: "Approved" }));
  expect(passes.map((pass) => pass.itemHeader)).toEqual(["", "", "Fulfilment: item 1 of 2", "Fulfilment: item 2 of 2", ""]);
  expect(passes[3].error).toBe("No stock");
  expect(countPasses(passes)).toEqual({ done: 4, skipped: 0, failed: 1 });
});

it("shows the start of a value longer than 8 lines or 240 characters", () => {
  const long = valueText({ id: "A-1001", line_items: Array.from({ length: 6 }, (_, index) => ({ sku: `SKU-${index}` })) });
  const preview = valuePreview(long);
  expect(preview.long).toBe(true);
  expect(preview.preview.split("\n")).toHaveLength(8);
  expect(preview.lines).toBeGreaterThan(8);
  expect(valuePreview(valueText("standard"))).toEqual({ long: false, lines: 1, preview: "\"standard\"" });
});

it("keeps a pass's outputs under their output targets", () => {
  const [pass] = buildRunPasses([{ step_id: "fetch", step_execution_id: "f", status: "completed", outputs: { order: { id: 1 } } }], definitionNames(definition), null, t);
  expect(keptOutputsFromPass(pass, [])).toEqual({ "state.order": { id: 1 } });
  expect(keptOutputsFromPass(pass, ["state.order"])).toEqual({ "state.order": { id: 1 } });
  expect(keptOutputsFromPass({ ...pass, outputs: { tracking_number: "JD1", carrier: "DHL" } }, ["state.shipment"]))
    .toEqual({ "state.shipment": { tracking_number: "JD1", carrier: "DHL" } });
});

it("keeps a built-in step's value under the target it wrote, not its report fields", () => {
  const [pass] = buildRunPasses([{ step_id: "transform", step_execution_id: "t", status: "completed",
    outputs: { value: [{ status: "packed" }], result_target: "state.packed_items" } }], definitionNames(definition), null, t);
  expect(keptOutputsFromPass(pass, [])).toEqual({ "state.packed_items": [{ status: "packed" }] });
  expect(keptOutputsFromPass({ ...pass, outputs: { value: "# Post", format: "markdown", result_target: "state.outputs.post" } }, ["state.outputs.post"]))
    .toEqual({ "state.outputs.post": "# Post" });
});

it("reads declared output targets from a step contract preview", () => {
  expect(declaredStepOutputTargets({ outputs: [{ target: "state.shipment" }, { name: "classification" }, {}] })).toEqual(["state.shipment", "state.classification"]);
  expect(declaredStepOutputTargets(undefined)).toEqual([]);
});

it("reads an Approval's shown outputs back as values, not as the JSON text of them", () => {
  const list = JSON.stringify([{ status: "packed" }, { status: "packed" }], null, 2);
  expect(approvalShownValues(["state.packed_items"], list)).toEqual([{ name: "state.packed_items", value: [{ status: "packed" }, { status: "packed" }] }]);
  expect(approvalShownValues(["state.summary"], "Ship 2 orders today")).toEqual([{ name: "state.summary", value: "Ship 2 orders today" }]);
  expect(approvalShownValues(["state.a", "state.b"], JSON.stringify({ "state.a": 1, "state.b": "two" })))
    .toEqual([{ name: "state.a", value: 1 }, { name: "state.b", value: "two" }]);
  expect(approvalShownValues(["state.a", "state.b"], "not json")).toEqual([]);
  expect(approvalShownValues(undefined, "anything")).toEqual([]);
});
