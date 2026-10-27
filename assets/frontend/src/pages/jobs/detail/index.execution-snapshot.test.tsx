// eslint-disable-next-line testing-library/no-manual-cleanup -- Unmount before restoring SVG and global descriptors.
import { cleanup, act, screen, waitFor, within } from "@testing-library/react";

import {
  jobPayload,
  mockedFetchJobDefinition,
  mockedFetchJobIncident,
  mockedFetchJobExecutionStepsPage,
  mockedFetchJobApproval,
  primeApi,
  renderPage,
} from "./index.test-helpers";

let getBBoxBefore: PropertyDescriptor | undefined;
let resizeObserverBefore: PropertyDescriptor | undefined;
beforeEach(() => {
  getBBoxBefore = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
  resizeObserverBefore = Object.getOwnPropertyDescriptor(globalThis, "ResizeObserver");
  Object.defineProperty(SVGElement.prototype, "getBBox", {
    configurable: true, value: () => ({ x: 0, y: 0, width: 120, height: 16 }),
  });
  Object.defineProperty(globalThis, "ResizeObserver", {
    configurable: true, writable: true,
    value: class { observe() {} unobserve() {} disconnect() {} },
  });
});
afterEach(() => {
  cleanup();
  if (getBBoxBefore) Object.defineProperty(SVGElement.prototype, "getBBox", getBBoxBefore);
  else Reflect.deleteProperty(SVGElement.prototype, "getBBox");
  if (resizeObserverBefore) Object.defineProperty(globalThis, "ResizeObserver", resizeObserverBefore);
  else Reflect.deleteProperty(globalThis, "ResizeObserver");
});

function terminalRun() {
  primeApi(jobPayload({
    status: "completed",
    lifecycle: { status: { code: "completed", label: "Completed", terminal: true } },
    execution_steps: [],
  }));
  mockedFetchJobDefinition.mockResolvedValue({
    job_id: "42", available: true, workflow_id: "author", revision: 7, digest: "archive-digest",
    steps: [{ step_id: "author-hook", override: { invoke_workflow: { hook: { hook_id: "before_invoice" } } } }],
    groups: [],
  });
}

describe("JobsDetailPage durable execution snapshots", () => {
  it("emits terminal statuses, archived hook ownership and immediate child invocation identity", async () => {
    terminalRun();
    mockedFetchJobExecutionStepsPage.mockResolvedValue({
      job_id: "42", run_terminal: true,
      steps: [{ step_id: "author-hook", step_execution_id: "execution-1", occurrence: 2, status: "completed", group_id: "author-group", for_each_id: "orders" }],
      page: { has_more: false, next_cursor: "" },
      child_invocations: [{ step_id: "author-hook", step_execution_id: "execution-1", invocation_key: "42:execution-1", child_job_id: "child-1", child_workflow_id: "client" }],
    } as never);
    const onExecutionSnapshotChange = vi.fn();
    renderPage({ onExecutionSnapshotChange });

    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith({
      jobId: "42", definitionWorkflowId: "author", definitionRevision: 7, definitionDigest: "archive-digest", terminal: true,
      definitionSteps: [{ stepId: "author-hook", hookId: "before_invoice" }],
      steps: [{ stepId: "author-hook", stepExecutionId: "execution-1", occurrence: 2, status: "completed", groupId: "author-group", forEachId: "orders" }],
      childInvocations: [{ stepId: "author-hook", stepExecutionId: "execution-1", invocationKey: "42:execution-1", childJobId: "child-1", childWorkflowId: "client" }],
      complete: true,
    }));
  });

  it("loads older execution pages for a snapshot observer and retains failed member statuses", async () => {
    terminalRun();
    mockedFetchJobExecutionStepsPage.mockImplementation(async (_jobId, cursor) => cursor ? {
      job_id: "42", steps: [{ step_id: "older", step_execution_id: "old-execution", status: "failed" }],
      page: { has_more: false, next_cursor: "" },
    } : {
      job_id: "42", run_terminal: true, steps: [{ step_id: "newer", status: "completed" }],
      page: { has_more: true, next_cursor: "older-page" },
    });
    const onExecutionSnapshotChange = vi.fn();
    renderPage({ onExecutionSnapshotChange });

    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({
      complete: true,
      steps: expect.arrayContaining([
        expect.objectContaining({ stepId: "older", stepExecutionId: "old-execution", status: "failed" }),
        expect.objectContaining({ stepId: "newer", status: "completed" }),
      ]),
    })));
  });

  it("keeps ordinary detail history lazy when no snapshot observer is attached", async () => {
    terminalRun();
    mockedFetchJobExecutionStepsPage.mockResolvedValue({
      job_id: "42", steps: [], page: { has_more: true, next_cursor: "older-page" },
    });
    renderPage();

    await screen.findByTestId("job-run-overview");
    expect(mockedFetchJobExecutionStepsPage).toHaveBeenCalledTimes(1);
  });

  it("reports partial snapshots when invocation projection is truncated", async () => {
    terminalRun();
    mockedFetchJobExecutionStepsPage.mockResolvedValue({
      job_id: "42", run_terminal: true, steps: [], page: { has_more: false, next_cursor: "" },
      child_invocations: [], child_invocations_truncated: true,
    } as never);
    const onExecutionSnapshotChange = vi.fn();
    renderPage({ onExecutionSnapshotChange });

    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({ complete: false })));
  });

  it("stops a repeated history cursor and reports an incomplete snapshot", async () => {
    terminalRun();
    mockedFetchJobExecutionStepsPage.mockResolvedValue({
      job_id: "42", run_terminal: true, steps: [], page: { has_more: true, next_cursor: "repeated" },
    } as never);
    const onExecutionSnapshotChange = vi.fn();
    renderPage({ onExecutionSnapshotChange });

    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({ complete: false })));
    await waitFor(() => expect(mockedFetchJobExecutionStepsPage).toHaveBeenCalledTimes(2));
  });

  it("emits no marks when the executed definition archive is unavailable", async () => {
    terminalRun();
    mockedFetchJobDefinition.mockResolvedValue({ job_id: "42", available: false, steps: [], groups: [] });
    const onExecutionSnapshotChange = vi.fn();
    renderPage({ onExecutionSnapshotChange });

    await screen.findByTestId("job-run-definition-unavailable");
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(null));
  });
});


it("preserves recorded ordinary-step timing including zero in the archived execution snapshot", async () => {
  terminalRun();
  mockedFetchJobDefinition.mockResolvedValue({
    job_id: "42", available: true, workflow_id: "author", revision: 7, digest: "archive-digest",
    steps: [{ step_id: "prepare_local_audit_data", step_kind: "transform" }, { step_id: "done", step_kind: "stop_success" }],
    groups: [], for_each: [],
  });
  mockedFetchJobExecutionStepsPage.mockResolvedValue({
    job_id: "42", run_terminal: true,
    steps: [
      { step_id: "prepare_local_audit_data", step_execution_id: "transform-execution-1", occurrence: 1,
        status: "completed", started_at: "2026-10-18T12:24:00.000Z", finished_at: "2026-10-18T12:24:00.033Z", duration_ms: 33 },
      { step_id: "done", step_execution_id: "done-execution-1", occurrence: 1,
        status: "completed", started_at: "2026-10-18T12:24:30.000Z", finished_at: "2026-10-18T12:24:30.000Z", duration_ms: 0 },
    ],
    page: { count: 2, has_more: false, next_cursor: "" }, child_invocations: [],
  });
  const onExecutionSnapshotChange = vi.fn();
  renderPage({ onExecutionSnapshotChange });

  await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({
    jobId: "42", definitionWorkflowId: "author", definitionRevision: 7, definitionDigest: "archive-digest",
    complete: true, terminal: true,
    steps: [
      expect.objectContaining({ stepId: "prepare_local_audit_data", stepExecutionId: "transform-execution-1", occurrence: 1,
        status: "completed", startedAt: "2026-10-18T12:24:00.000Z", finishedAt: "2026-10-18T12:24:00.033Z", durationMs: 33 }),
      expect.objectContaining({ stepId: "done", stepExecutionId: "done-execution-1", occurrence: 1,
        status: "completed", startedAt: "2026-10-18T12:24:30.000Z", finishedAt: "2026-10-18T12:24:30.000Z", durationMs: 0 }),
    ],
  })));
});

it("projects the recorded run failure separately from successful operation statuses", async () => {
  terminalRun();
  mockedFetchJobDefinition.mockResolvedValue({ job_id: "42", available: true, workflow_id: "author", revision: 7,
    steps: [{ step_id: "stop", display_name: "Intentional Stop fail", step_kind: "stop_fail" }], groups: [] });
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: true,
    run_error: { code: "workflow.stopped", message: "Expected local audit failure", step_id: "stop" },
    steps: [{ step_id: "stop", step_execution_id: "stop-pass", status: "completed", duration_ms: 0 }],
    page: { has_more: false, next_cursor: "" }, child_invocations: [] });
  const onExecutionSnapshotChange = vi.fn();
  renderPage({ onExecutionSnapshotChange });
  await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({
    definitionSteps: [{ stepId: "stop", label: "Intentional Stop fail" }],
    runError: { code: "workflow.stopped", message: "Expected local audit failure", stepId: "stop" },
    steps: [{ stepId: "stop", stepExecutionId: "stop-pass", status: "completed", durationMs: 0 }],
  })));
});

function inspectionFixture() {
  primeApi(jobPayload({ id: "42", status: "failed", selected_workflow: "author", project_id: "alpha", execution_steps: [],
    lifecycle: { status: { code: "failed", terminal: true } } }));
  mockedFetchJobDefinition.mockResolvedValue({ job_id: "42", available: true, workflow_id: "author", revision: 7,
    digest: "archive-7", steps: [{ step_id: "transform", display_name: "Transform item", step_kind: "transform" }], groups: [] });
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: true, steps: [
    { step_id: "transform", step_execution_id: "earlier", occurrence: 1, status: "completed", output: { receipt: "Earlier occurrence" } },
    { step_id: "transform", step_execution_id: "failed", occurrence: 2, status: "failed", output: { receipt: "Exact failed occurrence" } },
  ], page: { has_more: false, next_cursor: "" } });
}
const inspectionOwner = { jobId: "42", workflowId: "author", projectId: "alpha", generation: 1, isCurrent: () => true };

it("consumes the exact archived execution request instead of another occurrence of the same step", async () => {
  inspectionFixture();
  const view = renderPage({ embedded: true, expectedWorkflowId: "author", expectedProjectId: "alpha", executionOwner: inspectionOwner,
    inspectionRequest: { serial: 1, kind: "step", jobId: "42", stepId: "transform", stepExecutionId: "failed",
      definitionWorkflowId: "author", definitionRevision: 7, isCurrent: () => true } });
  try {
    const inspector = await screen.findByTestId("job-step-inspector-drawer");
    expect(within(inspector).getByText("Exact failed occurrence")).toBeVisible();
    expect(within(inspector).queryByText("Earlier occurrence")).not.toBeInTheDocument();
    expect(mockedFetchJobExecutionStepsPage).toHaveBeenCalledOnce();
  } finally { view.unmount(); view.queryClient.clear(); }
});

it.each(["retired", "wrong revision", "missing occurrence", "foreign job"])("refuses %s inspection requests without a nearest-row fallback", async (reason) => {
  inspectionFixture();
  const view = renderPage({ embedded: true, expectedWorkflowId: "author", expectedProjectId: "alpha", executionOwner: inspectionOwner,
    inspectionRequest: { serial: 1, kind: "step", jobId: reason === "foreign job" ? "other" : "42", stepId: "transform",
      stepExecutionId: reason === "missing occurrence" ? "absent" : "failed", definitionWorkflowId: "author",
      definitionRevision: reason === "wrong revision" ? 6 : 7, isCurrent: () => reason !== "retired" } });
  try {
    await screen.findByTestId("job-run-overview");
    await waitFor(() => expect(mockedFetchJobExecutionStepsPage).toHaveBeenCalledOnce());
    expect(screen.queryByTestId("job-step-inspector-drawer")).not.toBeInTheDocument();
  } finally { view.unmount(); view.queryClient.clear(); }
});

it("retires a request while archived rows are held instead of opening it after the owner has moved", async () => {
  inspectionFixture();
  let release!: (value: Awaited<ReturnType<typeof mockedFetchJobExecutionStepsPage>>) => void;
  const held = new Promise<Awaited<ReturnType<typeof mockedFetchJobExecutionStepsPage>>>((resolve) => { release = resolve; });
  mockedFetchJobExecutionStepsPage.mockReturnValue(held);
  let current = true;
  const view = renderPage({ embedded: true, expectedWorkflowId: "author", expectedProjectId: "alpha", executionOwner: inspectionOwner,
    inspectionRequest: { serial: 1, kind: "step", jobId: "42", stepId: "transform", stepExecutionId: "failed",
      definitionWorkflowId: "author", definitionRevision: 7, isCurrent: () => current } });
  try {
    await waitFor(() => expect(mockedFetchJobExecutionStepsPage).toHaveBeenCalledOnce());
    current = false;
    await act(async () => { release({ job_id: "42", steps: [{ step_id: "transform", step_execution_id: "failed", status: "failed" }],
      page: { has_more: false, next_cursor: "" } }); await held; });
    expect(screen.queryByTestId("job-step-inspector-drawer")).not.toBeInTheDocument();
  } finally { view.unmount(); view.queryClient.clear(); }
});

it.each(["gate-current", "gate-old"])("focuses Review only when its requested approval identity matches %s", async (requestedId) => {
  const gate = { approval_id: "gate-current", job_id: "42", thread_id: "thread-42", gate_kind: "approval", message: "Review the selected request?" };
  primeApi(jobPayload({ id: "42", status: "awaiting_approval", selected_workflow: "author", project_id: "alpha", execution_steps: [],
    lifecycle: { status: { code: "awaiting_approval", terminal: false }, approval: { state: "awaiting_approval", gate } } }));
  mockedFetchJobDefinition.mockResolvedValue({ job_id: "42", available: false, steps: [], groups: [] });
  mockedFetchJobApproval.mockResolvedValue({ job_id: "42", approval_id: "gate-current", payload: gate });
  const view = renderPage({ embedded: true, expectedWorkflowId: "author", expectedProjectId: "alpha", executionOwner: inspectionOwner,
    inspectionRequest: { serial: 1, kind: "review", jobId: "42", approvalId: requestedId, isCurrent: () => true } });
  try {
    const workspace = await screen.findByRole("region", { name: "Review workspace" });
    await waitFor(() => expect(workspace).toHaveTextContent("Review the selected request?"));
    if (requestedId === "gate-current") await waitFor(() => expect(workspace).toHaveFocus());
    else expect(workspace).not.toHaveFocus();
  } finally { view.unmount(); view.queryClient.clear(); }
});

it("preserves the public run error execution ID independently of a prior failed occurrence", async () => {
  terminalRun();
  mockedFetchJobDefinition.mockResolvedValue({ job_id: "42", available: true, workflow_id: "author", revision: 7,
    steps: [{ step_id: "stop", step_kind: "stop_fail" }], groups: [] });
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: true,
    run_error: { code: "workflow.stopped", message: "Current intentional failure", step_id: "stop", step_execution_id: "current-stop" },
    steps: [{ step_id: "stop", step_execution_id: "prior-stop", status: "failed" },
      { step_id: "stop", step_execution_id: "current-stop", status: "completed", duration_ms: 0 }],
    page: { has_more: false, next_cursor: "" } });
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ onExecutionSnapshotChange });
  try {
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({
      runError: { code: "workflow.stopped", message: "Current intentional failure", stepId: "stop", stepExecutionId: "current-stop" },
      steps: [expect.objectContaining({ stepExecutionId: "prior-stop", status: "failed" }),
        expect.objectContaining({ stepExecutionId: "current-stop", status: "completed", durationMs: 0 })],
    })));
  } finally { view.unmount(); view.queryClient.clear(); }
});

it("does not publish an execution row with conflicting populated logical step aliases", async () => {
  terminalRun();
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: true,
    run_error: { code: "failed", step_id: "author-hook", step_execution_id: "conflicting-pass" },
    steps: [{ step_id: "other-step", workflow_step_id: "author-hook", step_execution_id: "conflicting-pass", status: "failed" }],
    page: { has_more: false, next_cursor: "" } });
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ onExecutionSnapshotChange });
  try {
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({
      runError: expect.objectContaining({ stepExecutionId: "conflicting-pass" }), steps: [],
    })));
  } finally { view.unmount(); view.queryClient.clear(); }
});


it("retains an alias-only logical step through the real merge without confusing execution identity", async () => {
  terminalRun();
  mockedFetchJobDefinition.mockResolvedValue({ job_id: "42", available: true, workflow_id: "author", revision: 7,
    steps: [{ step_id: "stop", step_kind: "stop_fail" }], groups: [] });
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: true,
    run_error: { code: "workflow.stopped", message: "Current intentional failure", step_id: "stop", step_execution_id: "current-stop" },
    steps: [{ id: "message-prior-stop", step_id: "stop", step_execution_id: "prior-stop", status: "failed" },
      { id: "message-current-stop", workflow_step_id: "stop", step_execution_id: "current-stop", status: "completed", duration_ms: 0 }],
    page: { has_more: false, next_cursor: "" } });
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ onExecutionSnapshotChange });
  try {
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({
      complete: true, terminal: true,
      runError: { code: "workflow.stopped", message: "Current intentional failure", stepId: "stop", stepExecutionId: "current-stop" },
      steps: [{ stepId: "stop", stepExecutionId: "prior-stop", status: "failed" },
        { stepId: "stop", stepExecutionId: "current-stop", status: "completed", durationMs: 0 }],
    })));
  } finally { view.unmount(); view.queryClient.clear(); }
});


function takenTraceFixture() {
  const source = { kind: "step", id: "choose" };
  const target = { kind: "step", id: "done" };
  const definition = { job_id: "42", available: true, workflow_id: "author", revision: 7,
    snapshot_id: "archive-7", digest: "digest-7", schema_version: 1,
    steps: [{ step_id: "choose", step_kind: "switch", override: { switch: { selector: "state.channel", cases: [{ case_id: "a", when: "a" }, { case_id: "b", when: "b" }] } } }, { step_id: "done", step_kind: "stop_success" }], groups: [], for_each: [],
    execution_graph: { schema_version: 1, entry: source, edges: [
      { source, outcome: { name: "case", case_id: "a" }, target },
      { source, outcome: { name: "case", case_id: "b" }, target },
    ] } };
  const trace = { schema_version: 1, job_id: "42", workflow_id: "author", snapshot_id: "archive-7", revision: 7, digest: "digest-7",
    covered_from_start: true, terminal: false, truncated: false, last_sequence: 1, transitions: [
      { id: "a".repeat(64), sequence: 1, source, outcome: { name: "case", case_id: "b" }, target,
        step_execution_id: "choose-pass", attempt: 1, frames: [] },
    ] };
  return { definition, trace };
}

it("publishes exact first-page taken case evidence independently of incomplete step history", async () => {
  terminalRun();
  const { definition, trace } = takenTraceFixture();
  mockedFetchJobDefinition.mockResolvedValue(definition as never);
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: true, run_trace: trace,
    steps: [], page: { has_more: true, next_cursor: "repeated" } } as never);
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ onExecutionSnapshotChange });
  try {
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({
      definitionSnapshotId: "archive-7", complete: false,
      runTrace: { trace, edges: [definition.execution_graph.edges[1]] },
    })));
    await waitFor(() => expect(mockedFetchJobExecutionStepsPage).toHaveBeenCalledTimes(2));
  } finally { view.unmount(); view.queryClient.clear(); }
});

it.each(["missing", "job", "workflow", "archive", "digest", "revision", "truncated", "partial", "case", "execution", "scope", "schema"])(
  "keeps %s trace evidence out of the real snapshot consumer", async (reason) => {
    terminalRun();
    const { definition, trace } = takenTraceFixture();
    const changes: Record<string, unknown> = {
      job: { job_id: "other" }, workflow: { workflow_id: "other" }, archive: { snapshot_id: "other" },
      digest: { digest: "other" }, revision: { revision: 6 }, truncated: { truncated: true }, partial: { covered_from_start: false },
      schema: { schema_version: 2 },
      case: { transitions: [{ ...trace.transitions[0], outcome: { name: "case", case_id: "absent" } }] },
      execution: { transitions: [{ ...trace.transitions[0], step_execution_id: null }] },
      scope: { transitions: [{ ...trace.transitions[0], frames: [{ kind: "group", id: "foreign", execution_id: "group-pass" }] }] },
    };
    mockedFetchJobDefinition.mockResolvedValue(definition as never);
    mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: true,
      ...(reason === "missing" ? {} : { run_trace: { ...trace, ...(changes[reason] as object) } }),
      steps: [], page: { has_more: false, next_cursor: "" } } as never);
    const onExecutionSnapshotChange = vi.fn();
    const view = renderPage({ onExecutionSnapshotChange });
    try {
      await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({ complete: true })));
      expect(onExecutionSnapshotChange.mock.calls.at(-1)?.[0]).not.toHaveProperty("runTrace");
    } finally { view.unmount(); view.queryClient.clear(); }
  });

it("does not adopt a trace supplied only on a later execution page", async () => {
  terminalRun();
  const { definition, trace } = takenTraceFixture();
  mockedFetchJobDefinition.mockResolvedValue(definition as never);
  mockedFetchJobExecutionStepsPage.mockImplementation(async (_jobId, cursor) => ({ job_id: "42", run_terminal: true,
    ...(cursor ? { run_trace: trace } : {}), steps: [], page: { has_more: !cursor, next_cursor: cursor ? "" : "older" } }) as never);
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ onExecutionSnapshotChange });
  try {
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({ complete: true })));
    expect(onExecutionSnapshotChange.mock.calls.at(-1)?.[0]).not.toHaveProperty("runTrace");
  } finally { view.unmount(); view.queryClient.clear(); }
});

it("draws the first-page trace through Jobs detail without activating eager history pagination", async () => {
  terminalRun();
  const { definition, trace } = takenTraceFixture();
  mockedFetchJobDefinition.mockResolvedValue(definition as never);
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: true, run_trace: trace,
    steps: [], page: { has_more: true, next_cursor: "older" } } as never);
  const view = renderPage();
  try {
    const canvas = await screen.findByTestId("job-definition-canvas");
    // Exact SVG arrow paint has no accessible role; the owning canvas is located through Testing Library.
    // eslint-disable-next-line testing-library/no-node-access
    await waitFor(() => expect(canvas.querySelectorAll(".react-flow__edge-path[style*='--fs-workflow-edge-ran']")).toHaveLength(1));
    expect(mockedFetchJobExecutionStepsPage).toHaveBeenCalledOnce();
  } finally { view.unmount(); view.queryClient.clear(); }
});


// D062: feed only the existing authorized Jobs queries; no fake validator or new module import.
function containerSnapshotFixture() {
  primeApi(jobPayload({ id: "42", project_id: "alpha", selected_workflow: "author", status: "in_progress", execution_steps: [] }));
  const definition = { job_id: "42", available: true, workflow_id: "author", revision: 7, snapshot_id: "archive-7", digest: "digest-7", schema_version: 1,
    steps: [{ step_id: "member", step_kind: "transform", group_id: "batch" }],
    groups: [{ group_id: "batch", display_name: "Batch", body: { kind: "sequence", entry: { kind: "step", id: "member" } } }], for_each: [],
    execution_graph: { schema_version: 1, entry: { kind: "group", id: "batch" }, edges: [
      { source: { kind: "step", id: "member" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "batch", lane_id: null } },
    ] } };
  const progress = { schema_version: 1, job_id: "42", project_id: "alpha", workflow_id: "author", revision: 7, snapshot_id: "archive-7", digest: "digest-7",
    covered_from_start: true, truncated: false, checkpoint_terminal: false, last_sequence: 1,
    passes: [{ container: { kind: "group", id: "batch" }, execution_id: "batch-pass", parent_frames: [],
      started_sequence: 1, updated_sequence: 1, finished_sequence: null, current: true, outcome: null }],
  };
  mockedFetchJobDefinition.mockResolvedValue(definition);
  return { definition, progress };
}

it("publishes verified container progress through the actual Jobs observer before step history is complete", async () => {
  const { progress } = containerSnapshotFixture();
  const first = { job_id: "42", run_terminal: false, container_progress: progress, steps: [], page: { has_more: true, next_cursor: "older" } };
  const later = { job_id: "42", steps: [], page: { has_more: false, next_cursor: "" } };
  let release!: (value: typeof later) => void;
  const held = new Promise<typeof later>((resolve) => { release = resolve; });
  mockedFetchJobExecutionStepsPage.mockImplementation(async (_jobId, cursor) => cursor ? held : first);
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ embedded: true, expectedWorkflowId: "author", expectedProjectId: "alpha", executionOwner: inspectionOwner, onExecutionSnapshotChange });
  try {
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({
      jobId: "42", definitionWorkflowId: "author", definitionRevision: 7, definitionSnapshotId: "archive-7", definitionDigest: "digest-7",
      complete: false, containerProgress: { progress },
    })));
    // Enabling the verified embedded observer may refresh the shared first page.
    // Pagination must still acquire the exact owned older cursor only once.
    await waitFor(() => expect(mockedFetchJobExecutionStepsPage.mock.calls.filter(([, cursor]) => cursor === "older")).toHaveLength(1));
    const requests = mockedFetchJobExecutionStepsPage.mock.calls.map(([jobId, cursor]) => ({ jobId, cursor: cursor ?? "" }));
    expect(requests).toContainEqual({ jobId: "42", cursor: "" });
    for (const { jobId, cursor } of requests) {
      expect(jobId).toBe("42");
      expect(["", "older"]).toContain(cursor);
    }
    await act(async () => { release(later); await held; });
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({ complete: true, containerProgress: { progress } })));
  } finally { release(later); view.unmount(); view.queryClient.clear(); }
});

it.each(["missing", "project", "job", "workflow", "archive", "revision", "digest", "partial", "truncated", "parent", "unknown container", "duplicate pass", "terminal current"])(
  "keeps %s container evidence out of the real verified Jobs observer", async (reason) => {
    const { progress } = containerSnapshotFixture();
    const changes: Record<string, object> = {
      project: { project_id: "foreign" }, job: { job_id: "foreign" }, workflow: { workflow_id: "foreign" }, archive: { snapshot_id: "foreign" },
      revision: { revision: 6 }, digest: { digest: "foreign" }, partial: { covered_from_start: false }, truncated: { truncated: true },
      parent: { passes: [{ ...progress.passes[0], parent_frames: [{ kind: "group", id: "absent", execution_id: "absent-pass" }] }] },
      "unknown container": { passes: [{ ...progress.passes[0], container: { kind: "group", id: "current-live-only" } }] },
      "duplicate pass": { passes: [progress.passes[0], progress.passes[0]] }, "terminal current": { checkpoint_terminal: true },
    };
    const page = { job_id: "42", run_terminal: false, steps: [], page: { has_more: false, next_cursor: "" },
      ...(reason === "missing" ? {} : { container_progress: { ...progress, ...changes[reason] } }) };
    mockedFetchJobExecutionStepsPage.mockResolvedValue(page);
    const onExecutionSnapshotChange = vi.fn();
    const view = renderPage({ embedded: true, expectedWorkflowId: "author", expectedProjectId: "alpha", executionOwner: inspectionOwner, onExecutionSnapshotChange });
    try {
      await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({ jobId: "42", complete: true })));
      expect(onExecutionSnapshotChange.mock.calls.at(-1)?.[0]).not.toHaveProperty("containerProgress");
    } finally { view.unmount(); view.queryClient.clear(); }
  });

it("does not acquire container authority from a later page when the first page had none", async () => {
  const { progress } = containerSnapshotFixture();
  mockedFetchJobExecutionStepsPage.mockImplementation(async (_jobId, cursor) => ({ job_id: "42", run_terminal: false,
    ...(cursor ? { container_progress: progress } : {}), steps: [], page: { has_more: !cursor, next_cursor: cursor ? "" : "older" } }));
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ embedded: true, expectedWorkflowId: "author", expectedProjectId: "alpha", executionOwner: inspectionOwner, onExecutionSnapshotChange });
  try {
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({ complete: true })));
    expect(onExecutionSnapshotChange.mock.calls.at(-1)?.[0]).not.toHaveProperty("containerProgress");
    // The verified observer may refresh page one; only the owned older cursor is unique.
    const requests = mockedFetchJobExecutionStepsPage.mock.calls.map(([jobId, cursor]) => ({ jobId, cursor: cursor ?? "" }));
    expect(requests.filter(({ cursor }) => cursor === "older")).toHaveLength(1);
    expect(requests).toContainEqual({ jobId: "42", cursor: "" });
    for (const { jobId, cursor } of requests) {
      expect(jobId).toBe("42");
      expect(["", "older"]).toContain(cursor);
    }
  } finally { view.unmount(); view.queryClient.clear(); }
});

it("retains complete empty container evidence distinctly from legacy absence", async () => {
  const { definition, progress } = containerSnapshotFixture();
  mockedFetchJobDefinition.mockResolvedValue({ ...definition, groups: [], steps: [{ step_id: "done", step_kind: "stop_success" }],
    execution_graph: { schema_version: 1, entry: { kind: "step", id: "done" }, edges: [] } });
  const empty = { ...progress, passes: [], last_sequence: 0 };
  const page = { job_id: "42", run_terminal: false, container_progress: empty, steps: [], page: { has_more: false, next_cursor: "" } };
  mockedFetchJobExecutionStepsPage.mockResolvedValue(page);
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ embedded: true, expectedWorkflowId: "author", expectedProjectId: "alpha", executionOwner: inspectionOwner, onExecutionSnapshotChange });
  try {
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({ complete: true, containerProgress: { progress: empty } })));
  } finally { view.unmount(); view.queryClient.clear(); }
});

it("forwards the verified detail project through the actual Jobs archive canvas as well as snapshot publication", async () => {
  const { progress } = containerSnapshotFixture();
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: false, container_progress: progress,
    steps: [], page: { has_more: false, next_cursor: "" } });
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ onExecutionSnapshotChange });
  try {
    const mark = await screen.findByTestId("workflow-container-runtime:batch");
    expect(within(mark).getByText("Running")).toBeVisible();
    expect(mark).toHaveAttribute("data-execution-id", "batch-pass");
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({ containerProgress: { progress } })));
  } finally { view.unmount(); view.queryClient.clear(); }
});

it.each([undefined, "another-project"])("does not borrow incident/back-link project authority when detail project is %s", async (projectId) => {
  const { definition, progress } = containerSnapshotFixture();
  // Incident/back-link context is useful navigation, but cannot authorize this envelope.
  primeApi(jobPayload({ id: "42", project_id: projectId, selected_workflow: "author", status: "in_progress", execution_steps: [] }));
  mockedFetchJobIncident.mockResolvedValue({ job_id: "42", project_id: "alpha", related_attempts: [] });
  mockedFetchJobDefinition.mockResolvedValue(definition);
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: false, container_progress: progress,
    steps: [], page: { has_more: false, next_cursor: "" } });
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ onExecutionSnapshotChange });
  try {
    await screen.findByTestId("job-definition-canvas");
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({ jobId: "42", complete: true })));
    expect(onExecutionSnapshotChange.mock.calls.at(-1)?.[0]).not.toHaveProperty("containerProgress");
    expect(screen.queryByTestId("workflow-container-runtime:batch")).not.toBeInTheDocument();
    expect(screen.getByText("Batch")).toBeVisible();
  } finally { view.unmount(); view.queryClient.clear(); }
});


it("retains the executed archive canvas and provenance on standalone Job Details", async () => {
  containerSnapshotFixture();
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: false,
    steps: [], page: { has_more: false, next_cursor: "" } });
  const view = renderPage();
  try {
    const page = await screen.findByTestId("job-detail-page");
    expect(await within(page).findByTestId("job-definition-canvas")).toBeVisible();
    expect(within(page).getByTestId("job-run-definition-revision")).toHaveTextContent("Executed definition: revision 7 · digest-7");
    expect(within(page).getByTestId("job-execution-waterfall")).toBeInTheDocument();
  } finally { view.unmount(); view.queryClient.clear(); }
});


it("keeps Builder-embedded historical provenance and snapshot publication without an archive canvas", async () => {
  const { progress } = containerSnapshotFixture();
  mockedFetchJobExecutionStepsPage.mockResolvedValue({ job_id: "42", run_terminal: false, container_progress: progress,
    steps: [], page: { has_more: false, next_cursor: "" } });
  const onExecutionSnapshotChange = vi.fn();
  const view = renderPage({ embedded: true, expectedWorkflowId: "author", expectedProjectId: "alpha",
    executionOwner: inspectionOwner, onExecutionSnapshotChange });
  try {
    await waitFor(() => expect(onExecutionSnapshotChange).toHaveBeenLastCalledWith(expect.objectContaining({
      jobId: "42", definitionRevision: 7, definitionDigest: "digest-7", containerProgress: { progress },
    })));
    const results = screen.getByTestId("workflow-builder-job-details");
    expect(within(results).getByTestId("job-run-definition-revision")).toHaveTextContent("Executed definition: revision 7 · digest-7");
    expect(within(results).queryByTestId("job-definition-canvas")).not.toBeInTheDocument();
    expect(within(results).getByTestId("job-execution-waterfall")).toBeInTheDocument();
  } finally { view.unmount(); view.queryClient.clear(); }
});
