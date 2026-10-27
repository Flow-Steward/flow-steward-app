import { buildExecutionSteps, formatElapsed, mergeExecutionSteps, mergeLiveFiles, streamFrameToExecutionStep, withLiveStepActivity } from "@/pages/jobs/detail/job-detail-execution";

const running = { scope_id: "export", status: "in_progress", started_at: "2026-10-06T10:00:00Z" };
const done = { scope_id: "fetch", status: "completed" };

function eventFrame(stepId: string, title: string, timestamp: string) {
  return { type: "event", data: { title, timestamp, detail: { step_id: stepId } } };
}

describe("withLiveStepActivity", () => {
  it("gives a running step its own newest runtime event", () => {
    const steps = withLiveStepActivity([done, running], {
      isTerminal: false,
      runtime: { last_response_excerpt: "export: running for 2 min", heartbeat_at: "2026-10-06T10:02:00Z" },
      streamEvents: [
        eventFrame("export", "Tool call: shopify.export_products", "2026-10-06T10:00:05Z"),
        eventFrame("fetch", "Tool result: http.get", "2026-10-06T09:59:00Z"),
      ],
    });

    expect(steps[0]).toBe(done);
    expect(steps[1]).toMatchObject({
      live_activity: "Tool call: shopify.export_products",
      live_activity_at: "2026-10-06T10:00:05Z",
    });
  });

  it("falls back to the job heartbeat when the only running step has no event yet", () => {
    const [step] = withLiveStepActivity([running], {
      isTerminal: false,
      runtime: { last_response_excerpt: "export: running for 2 min", heartbeat_at: "2026-10-06T10:02:00Z" },
      streamEvents: [],
    });

    expect(step).toMatchObject({
      live_activity: "export: running for 2 min",
      live_activity_at: "2026-10-06T10:02:00Z",
    });
  });

  it("does not guess which of several running steps the heartbeat names", () => {
    const other = { ...running, scope_id: "import" };
    const steps = withLiveStepActivity([running, other], {
      isTerminal: false,
      runtime: { last_response_excerpt: "export: running", heartbeat_at: "2026-10-06T10:02:00Z" },
      streamEvents: [],
    });

    expect(steps).toEqual([running, other]);
  });

  it("carries an extension's progress count onto its running step", () => {
    const [step] = withLiveStepActivity([running], {
      isTerminal: false,
      runtime: {},
      streamEvents: [
        {
          type: "event",
          data: {
            event_type: "step_progress",
            title: "Exporting products (1200 of 5400)",
            timestamp: "2026-10-06T10:03:00Z",
            detail: { step_id: "export", message: "Exporting products", done: 1200, total: 5400 },
          },
        },
      ],
    });

    expect(step).toMatchObject({
      live_activity: "Exporting products",
      live_progress_done: 1200,
      live_progress_total: 5400,
    });
  });

  it("leaves a finished job alone", () => {
    const steps = [running];
    expect(withLiveStepActivity(steps, { isTerminal: true, runtime: {}, streamEvents: [] })).toBe(steps);
  });
});

describe("live files", () => {
  function fileReady(stepId: string, artifactId: string, id = artifactId) {
    return {
      id,
      event_type: "artifact_ready",
      title: "File ready: export.jsonl",
      timestamp: "2026-10-06T10:04:00Z",
      detail: {
        step_id: stepId,
        artifact_id: artifactId,
        filename: "export.jsonl",
        size_bytes: 2048,
        download_url: `/api/jobs/job-1/artifacts/${artifactId}`,
      },
    };
  }

  it("keeps one row per file, however often its event arrives", () => {
    let files = mergeLiveFiles([], fileReady("export", "art-1", "event-1"));
    files = mergeLiveFiles(files, fileReady("export", "art-1", "event-2"));
    files = mergeLiveFiles(files, fileReady("export", "art-2"));

    expect(files.map((row) => (row.detail as { artifact_id: string }).artifact_id)).toEqual(["art-1", "art-2"]);
  });

  it("shows a finished file on its own running step only", () => {
    const other = { ...running, scope_id: "import" };
    const [exportStep, importStep] = withLiveStepActivity([running, other], {
      isTerminal: false,
      liveFiles: [fileReady("export", "art-1")],
      runtime: {},
      streamEvents: [{ type: "event", data: fileReady("export", "art-1") }],
    });

    expect(exportStep.live_files).toEqual([
      {
        id: "art-1",
        filename: "export.jsonl",
        size_bytes: 2048,
        content_type: "",
        download_url: "/api/jobs/job-1/artifacts/art-1",
      },
    ]);
    // A file-ready event is not the step's activity text.
    expect(exportStep.live_activity).toBeUndefined();
    expect(importStep.live_files).toBeUndefined();
  });
});

describe("formatElapsed", () => {
  it.each([
    [0, "0 s"],
    [42_000, "42 s"],
    [192_000, "3 min 12 s"],
    [3_780_000, "1 h 3 min"],
  ])("formats %i ms as %s", (milliseconds, text) => {
    expect(formatElapsed(milliseconds)).toBe(text);
  });
});


describe("execution history identity", () => {
  it("keeps all six executions when two parallel lanes repeat within a loop", () => {
    const rows = [
      { step_id: "prepare", step_execution_id: "prepare-1" },
      { step_id: "left_record", step_execution_id: "left-1" },
      { step_id: "right_record", step_execution_id: "right-1" },
      { step_id: "left_record", step_execution_id: "left-2" },
      { step_id: "right_record", step_execution_id: "right-2" },
      { step_id: "done", step_execution_id: "done-1" },
    ].map((row, index) => ({
      ...row,
      id: `message-${index}`,
      attempt: 1,
      title: row.step_id,
      status: "completed",
      timestamp: `2026-10-15T05:54:4${index}Z`,
    }));

    const steps = buildExecutionSteps({
      detailEntity: { sections: { execution_steps: rows } },
      incident: undefined,
      historyRows: rows.slice(0, 3),
    });

    expect(steps.map((row) => row.step_execution_id)).toEqual([
      "prepare-1", "left-1", "right-1", "left-2", "right-2", "done-1",
    ]);
    expect(steps.map((row) => row.step_id)).toEqual([
      "prepare", "left_record", "right_record", "left_record", "right_record", "done",
    ]);
  });

  it("updates the same execution from history, detail and stream without merging a later iteration", () => {
    const first = {
      id: "history-left", step_id: "left_record", step_execution_id: "left-1",
      group_id: "process", title: "left_record", status: "in_progress", inputs: { item: "alpha" },
    };
    const second = { ...first, id: "detail-left", step_execution_id: "left-2", inputs: { item: "beta" } };
    const completed = streamFrameToExecutionStep("message", {
      ...first, id: "message-left", status: "completed", summary: "alpha recorded", outputs: { item: "alpha" },
    }, 0)!;

    const steps = buildExecutionSteps({
      historyRows: [first],
      detailEntity: { sections: { execution_steps: [first, second] } },
      streamRows: [completed],
      incident: undefined,
    });

    expect(steps).toHaveLength(2);
    expect(steps[0]).toMatchObject({
      step_id: "left_record", step_execution_id: "left-1", status: "completed",
      inputs: { item: "alpha" }, outputs: { item: "alpha" },
    });
    expect(steps[1]).toMatchObject({ step_execution_id: "left-2", status: "in_progress", inputs: { item: "beta" } });
  });

  it("keeps identified canonical metadata when a generic stream message adds a summary", () => {
    const canonical = {
      id: "canonical", step_id: "draft", step_execution_id: "draft-1",
      title: "Reviewer: Draft Plan", status: "completed", status_label: "Completed",
      group_id: "design", agent_id: "reviewer", skill: "draft_plan",
    };
    const generic = streamFrameToExecutionStep("message", {
      step_id: "draft", step_execution_id: "draft-1", from_agent: "reviewer",
      group_id: "design", skill: "draft_plan", summary: "Architecture designed.",
    }, 0)!;

    const steps = mergeExecutionSteps([canonical, generic, generic]);

    expect(steps).toHaveLength(1);
    expect(steps[0]).toMatchObject({
      id: "canonical", title: "Reviewer: Draft Plan", status: "completed",
      status_label: "Completed", summary: "Architecture designed.",
    });
  });

  it("accepts explicit completion while keeping canonical metadata through a delayed generic stream", () => {
    const canonical = {
      id: "canonical", step_id: "draft", step_execution_id: "draft-1",
      title: "Reviewer: Draft Plan", status: "in_progress", status_label: "In progress",
    };
    const completed = streamFrameToExecutionStep("message", {
      step_id: "draft", step_execution_id: "draft-1", from_agent: "reviewer",
      status: "completed", summary: "Architecture designed.", outputs: { result: "ready" },
    }, 0)!;
    const generic = streamFrameToExecutionStep("message", {
      step_id: "draft", step_execution_id: "draft-1", from_agent: "reviewer", summary: "Delivery recorded.",
    }, 1)!;

    const steps = mergeExecutionSteps([canonical, completed, generic]);

    expect(steps).toHaveLength(1);
    expect(steps[0]).toMatchObject({
      id: "canonical", title: "Reviewer: Draft Plan", status: "completed",
      status_label: "Completed", outputs: { result: "ready" },
    });
  });

  it("does not replace a live completion status with a later generic recorded message", () => {
    const completed = streamFrameToExecutionStep("message", {
      step_id: "draft", step_execution_id: "draft-1", from_agent: "reviewer",
      status: "completed", summary: "Architecture designed.",
    }, 0)!;
    const generic = streamFrameToExecutionStep("message", {
      step_id: "draft", step_execution_id: "draft-1", from_agent: "reviewer", summary: "Delivery recorded.",
    }, 1)!;

    expect(mergeExecutionSteps([completed, generic])).toEqual([
      expect.objectContaining({ step_execution_id: "draft-1", status: "completed", status_label: "Completed" }),
    ]);
  });

  it("does not infer execution identity from a legacy row with the same title", () => {
    const steps = mergeExecutionSteps([
      { step_id: "left_record", step_execution_id: "left-1", title: "left_record", group_id: "process" },
      { id: "legacy-message", title: "left_record", group_id: "process", source_type: "message" },
    ]);

    expect(steps).toHaveLength(2);
    expect(steps[0].step_execution_id).toBe("left-1");
    expect(steps[1].step_execution_id).toBeUndefined();
  });

  it("retains legacy semantic deduplication and canonical details without execution IDs", () => {
    const steps = mergeExecutionSteps([
      { id: "canonical", title: "Reviewer: Draft Plan", group_id: "design", agent_id: "reviewer", skill: "draft_plan", status: "completed" },
      { id: "stream", title: "reviewer", group_id: "design", agent_id: "reviewer", skill: "draft_plan", status: "recorded", summary: "Architecture designed.", source_type: "message" },
    ]);

    expect(steps).toHaveLength(1);
    expect(steps[0]).toMatchObject({ id: "canonical", title: "Reviewer: Draft Plan", status: "completed", summary: "Architecture designed." });
  });
});
