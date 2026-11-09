import { describe, expect, it, vi } from "vitest";

vi.mock("@/pages/jobs/detail/api", () => ({ cancelJob: vi.fn() }));

import { isLiveRun } from "./detail-run-bar-controller";

describe("a run the builder follows", () => {
  it("is live when its own task source is not a Test Run", () => {
    expect(isLiveRun({ job: { task_source: "workflow_run" } })).toBe(true);
    expect(isLiveRun({ task_source: "ui" })).toBe(true);
    expect(isLiveRun({ job: { task_source: "workflow_test_run" } })).toBe(false);
    // Unknown is not live: the bar keeps calling it a Test Run, as before.
    expect(isLiveRun({})).toBe(false);
    expect(isLiveRun(null)).toBe(false);
  });
});
