import { afterEach, describe, expect, it, vi } from "vitest";

import {
  httpRequestLabel,
  mergeStepTestRunState,
  stepTestRunFailureDetail,
  testRunBannerMessage,
  waitForWorkflowTestRunState,
  workflowTestRunIsPending,
} from "@/pages/workflows/detail/detail-test-run";
import * as simulateApi from "@/pages/workflows/shared/simulate-api";

describe("asynchronous workflow test runs", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });
  it("treats accepted queued jobs as pending", () => {
    expect(workflowTestRunIsPending({ status: "queued" })).toBe(true);
    expect(workflowTestRunIsPending({ status: "awaiting_approval" })).toBe(true);
    expect(workflowTestRunIsPending({ status: "completed" })).toBe(false);
  });

  it("keeps a run paused on a Wait or a child workflow pending, never completed", () => {
    expect(workflowTestRunIsPending({ status: "awaiting_timer" })).toBe(true);
    expect(workflowTestRunIsPending({ status: "awaiting_child" })).toBe(true);
    expect(testRunBannerMessage("workflow", { status: "awaiting_timer" })).toBe(
      "Workflow test run is paused on a Wait step.",
    );
    expect(testRunBannerMessage("workflow", { status: "awaiting_child" })).toBe(
      "Workflow test run is waiting for a child workflow.",
    );
  });

  it("does not call a still-queued step test run completed", () => {
    expect(testRunBannerMessage("step", { status: "queued" })).toBe(
      "Step test run is still queued.",
    );
    expect(testRunBannerMessage("step", { status: "pending" })).toBe(
      "Step test run is still pending.",
    );
  });

  it("does not invent a GET request when durable step details are absent", () => {
    expect(httpRequestLabel({})).toBe("—");
    expect(httpRequestLabel({
      request: { method: "POST", url: "https://example.test/orders" },
    })).toBe("POST https://example.test/orders");
  });

  it("prefers a failed step error and falls back to its final report", () => {
    expect(stepTestRunFailureDetail({
      status: "failed",
      error: "Exact connector failure",
      final_report: "Generic final report",
    })).toBe("Exact connector failure");
    expect(stepTestRunFailureDetail({
      final_status: "error",
      error: "  ",
      final_report: "Fallback failure report",
    })).toBe("Fallback failure report");
    expect(stepTestRunFailureDetail({
      final_status: "success",
      error: "Stale failure",
      final_report: "Completed successfully",
    })).toBe("");
  });

  it("keeps a queued status after polling is exhausted", async () => {
    const fetchState = vi.spyOn(simulateApi, "fetchWorkflowTestRunState").mockResolvedValue({
      status: "queued",
      workflow_id: "wf-demo",
      needs_user_approval: false,
    });

    const result = await waitForWorkflowTestRunState("thread-queued", {
      attempts: 1,
      intervalMs: 0,
    });

    expect(result.status).toBe("queued");
    expect(testRunBannerMessage("step", result)).toBe("Step test run is still queued.");
    fetchState.mockRestore();
  });

  it("polls the durable GET contract until the worker reaches terminal state", async () => {
    const fetchState = vi
      .spyOn(simulateApi, "fetchWorkflowTestRunState")
      .mockResolvedValueOnce({
        status: "queued",
        workflow_id: "wf-demo",
        needs_user_approval: false,
      })
      .mockResolvedValueOnce({
        status: "completed",
        final_status: "completed",
        workflow_id: "wf-demo",
        needs_user_approval: false,
      });

    const result = await waitForWorkflowTestRunState("thread-1", {
      attempts: 2,
      intervalMs: 0,
    });

    expect(result.final_status).toBe("completed");
    expect(fetchState).toHaveBeenCalledTimes(2);
    expect(fetchState.mock.calls).toEqual([
      ["thread-1", { presentation: "background" }],
      ["thread-1", { presentation: "background" }],
    ]);
    fetchState.mockRestore();
  });

  it("preserves selected step identity when durable state reaches terminal status", () => {
    const result = mergeStepTestRunState(
      {
        status: "queued",
        workflow_id: "wf-demo",
        group_id: "build",
        step_id: "compile",
        step_kind: "shell",
      },
      {
        status: "completed",
        final_status: "completed",
        workflow_id: "wf-demo",
        needs_user_approval: false,
      },
    );

    expect(result).toMatchObject({
      status: "completed",
      final_status: "completed",
      workflow_id: "wf-demo",
      group_id: "build",
      step_id: "compile",
      step_kind: "shell",
    });
  });
});
