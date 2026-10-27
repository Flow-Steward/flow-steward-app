import { describe, expect, it } from "vitest";

import {
  detailStepLabel,
  jobCostTotals,
  jobDetailPollingInterval,
  jobFinalAnswer,
  jobMetadataPatch,
  lifecycleApprovalPatch,
  lifecycleRuntimePatch,
} from "@/pages/jobs/detail/job-detail-view-model";

describe("jobDetailPollingInterval", () => {
  it("stops polling after a terminal job response", () => {
    expect(jobDetailPollingInterval({ id: "42", status: "completed" }, 10_000)).toBe(false);
    expect(
      jobDetailPollingInterval(
        { id: "42", status: "in_progress", lifecycle: { status: { terminal: true } } },
        10_000,
      ),
    ).toBe(false);
  });

  it("keeps polling while the job is active or not loaded", () => {
    expect(jobDetailPollingInterval({ id: "42", status: "in_progress" }, 10_000)).toBe(10_000);
    expect(jobDetailPollingInterval(undefined, 10_000)).toBe(10_000);
  });
});

describe("job cost totals", () => {
  // What the compact job detail really sends: a cost section with no rows and
  // zero totals, because it skipped the ledger read.
  const emptyCompactSection = { rows: [], total_tokens: 0, total_cost_usd: 0, known_cost_usd: 0, total_cost_display: "$0.00", has_data: false };
  const breakdown = { breakdown: [], total_cost_usd: 0.000171, known_cost_usd: 0.000171, unknown_cli_tokens: 0, cost_knownness: "known" };
  const fallbackRows = [{ agent_label: "acme_answerer", model: "acme/model", tokens: 868, calls: 1 }];

  it("reads the totals from the cost-breakdown endpoint when the rows come from it", () => {
    const totals = jobCostTotals({ projected: emptyCompactSection, breakdown, detailCost: {}, rows: fallbackRows });

    expect(totals.totalTokensValue).toBe(868);
    expect(totals.totalCostValue).toBeCloseTo(0.000171);
    expect(totals.totalCostDisplay).toBe("$0.0002");
  });

  it("keeps the projected totals when the projection carries the rows", () => {
    const projected = { rows: fallbackRows, total_tokens: 900, known_cost_usd: 0.5, total_cost_display: "$0.50" };
    const totals = jobCostTotals({ projected, breakdown, detailCost: {}, rows: fallbackRows });

    expect(totals.totalTokensValue).toBe(900);
    expect(totals.totalCostDisplay).toBe("$0.50");
  });

  it("keeps a projection that counted something even without rows, such as a live cost frame", () => {
    const projected = { rows: [], total_tokens: 900, total_cost_usd: 2.5, total_cost_display: "$2.5000" };
    const totals = jobCostTotals({ projected, breakdown, detailCost: {}, rows: fallbackRows });

    expect(totals.totalTokensValue).toBe(900);
    expect(totals.totalCostDisplay).toBe("$2.5000");
  });
});

describe("job final answer", () => {
  const agentStep = (outputs: Record<string, unknown>, overrides: Record<string, unknown> = {}) => ({
    agent: "acme_answerer",
    status: "completed",
    title: "Ask: One word answer",
    outputs,
    ...overrides,
  });

  it("is the last finished agent step's outputs, without the shared workflow data", () => {
    const steps = [
      agentStep({ answer: "Mars" }),
      { status: "completed", outputs: { counter: 1 } },
      agentStep({ answer: "Jupiter", workflow_data: { question: "q" } }),
      agentStep({ answer: "Saturn" }, { status: "failed" }),
      // The stop step after it reports through a built-in runner.
      agentStep({ status: "completed", workflow_terminal: "stop_success" }, { agent: "control_flow_step", title: "Ask: Control Flow" }),
    ];

    expect(jobFinalAnswer({}, steps)).toEqual({ source: "From Ask: One word answer", outputs: { answer: "Jupiter" } });
  });

  it("prefers the workflow's declared outputs", () => {
    const detail = { workflow_run: { result: { outputs: { planet: "Jupiter" } } } };

    expect(jobFinalAnswer(detail, [agentStep({ answer: "Mars" })])).toEqual({ source: "Workflow outputs", outputs: { planet: "Jupiter" } });
  });

  it("is nothing when no agent step answered", () => {
    expect(jobFinalAnswer({}, [{ status: "completed", outputs: { counter: 1 } }, agentStep({ workflow_data: {} })])).toBeNull();
  });
});

describe("job current step", () => {
  const current = {
    id: "42",
    status: "in_progress",
    lifecycle: { step: { code: "draft", label: "Draft answer" } },
  };

  it("labels the header from the lifecycle step, then the job and runtime step ids", () => {
    expect(detailStepLabel(current)).toBe("Draft answer");
    expect(detailStepLabel({ job: { current_step_id: "check_stock" } })).toBe("Check Stock");
    expect(detailStepLabel({}, { current_step_id: "send_reply" })).toBe("Send Reply");
    expect(detailStepLabel({}, {})).toBe("—");
  });

  it("moves the lifecycle step from a runtime frame and drops the stale label", () => {
    expect(lifecycleRuntimePatch({ current_step_id: "draft" }, current)).toEqual({
      step: { code: "draft", label: "Draft answer" },
    });
    expect(lifecycleRuntimePatch({ current_step_id: "review", current_group_id: "loop" }, current)).toEqual({
      step: { code: "review", label: "" },
    });
    expect(lifecycleRuntimePatch({ current_step_id: null, active_steps: [] }, current)).toEqual({});
  });

  it("reads the gate step from an approval frame", () => {
    expect(lifecycleApprovalPatch({ step_id: "approve_order", group_id: "orders" }, {}, current)).toMatchObject({
      status: { code: "awaiting_approval" },
      step: { code: "approve_order", label: "" },
    });
  });

  it("copies the current step and group ids into the job record", () => {
    expect(jobMetadataPatch({ status: "in_progress", current_step_id: "draft", current_group_id: "loop" })).toEqual({
      status: "in_progress",
      current_step_id: "draft",
      current_group_id: "loop",
    });
  });
});


describe("compact public workflow result", () => {
  it.each(["Hello-World", false, 0, null, { nested: [false, 0, null] }])("keeps the declared value %j without a workflow snapshot", (value) => {
    const detail = { workflow_run: null, workflow_result: { outputs: { result: value }, artifacts: {} } };
    expect(jobFinalAnswer(detail, [])).toEqual({ source: "Workflow outputs", outputs: { result: value } });
  });
});
