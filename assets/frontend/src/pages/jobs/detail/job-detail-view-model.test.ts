import { describe, expect, it } from "vitest";

import {
  detailStepLabel,
  jobUsageTotals,
  jobDetailPollingInterval,
  jobFinalAnswer,
  jobReportOutput,
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

describe("job usage totals", () => {
  const rows = [{ agent_label: "answerer", model: "unknown/model", tokens: 868, calls: 1 }];
  it.each([
    { projected: { rows, total_tokens: 0 }, breakdown: {} },
    { projected: { rows: [], total_tokens: 0 }, breakdown: { total_tokens: 0 } },
  ])("preserves an authoritative zero over fallback rows", ({ projected, breakdown }) => {
    expect(jobUsageTotals({ projected, breakdown, detailUsage: { tokens_total: 99 }, rows })).toEqual({ totalTokensValue: 0 });
  });
  it("retains tokens from the breakdown when the compact section is empty", () => {
    expect(jobUsageTotals({ projected: { rows: [], total_tokens: 0 }, breakdown: {}, detailUsage: {}, rows })).toEqual({ totalTokensValue: 868 });
  });
  it("falls back to known rows when the total is explicitly unknown", () => {
    expect(jobUsageTotals({ projected: {}, breakdown: { total_tokens: null }, detailUsage: {}, rows })).toEqual({ totalTokensValue: 868 });
  });
  it("keeps projected totals when the projection carries rows or live usage", () => {
    expect(jobUsageTotals({ projected: { total_tokens: 900 }, breakdown: {}, detailUsage: {}, rows })).toEqual({ totalTokensValue: 900 });
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

describe("jobReportOutput", () => {
  it("reads the selected output from the persisted public result", () => {
    const report = { kind: "fs.report" };
    expect(jobReportOutput({ workflow_result: { outputs: { weekly: report }, report_output: "weekly" } })).toEqual({
      name: "weekly",
      value: report,
    });
  });

  it.each([
    [{}],
    [{ workflow_result: null }],
    [{ workflow_result: { outputs: { weekly: {} } } }],
    [{ workflow_result: { outputs: {}, report_output: "constructor" } }],
    [{ workflow_result: { outputs: { weekly: {} }, report_output: "other" } }],
  ])("finds no report in %j", (detail) => {
    expect(jobReportOutput(detail)).toBeNull();
  });
});
