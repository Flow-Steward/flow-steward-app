// eslint-disable-next-line testing-library/no-manual-cleanup -- Unmount before restoring SVG and global descriptors.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { JobDefinitionCanvas } from "./job-definition-canvas";
import { archivedApprovalDestinations, archivedDefinitionCanvasGraph } from "./job-definition-graph";
import type { JobDefinitionPayload } from "./api";
import type { WorkflowTargetRef } from "@/pages/workflows/shared/api";
import { resolveReviewWorkspace } from "./job-detail-review-panels";
import { buildCanonicalJobDetailView } from "./job-detail-view-model";

const snapshot: JobDefinitionPayload = {
  job_id: "old-run", available: true, snapshot_id: "archive-1", schema_version: 1,
  workflow_id: "workflow", revision: 4, digest: "historical-digest",
  steps: [{ step_id: "deleted-step", display_name: "Historical approval", step_kind: "approval", group_id: "old-group", override: { approval: { prompt: "Original question" } } }],
  groups: [{ group_id: "old-group", display_name: "Historical loop", body: { kind: "sequence", entry: { kind: "step", id: "deleted-step" } } }],
  for_each: [],
  execution_graph: { schema_version: 1, entry: { kind: "group", id: "old-group" }, edges: [] },
};

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

it("projects the archived kind and group even when those no longer exist in the live workflow", () => {
  const graph = archivedDefinitionCanvasGraph(snapshot);
  expect(graph?.workflow.revision).toBe(4);
  expect(graph?.steps[0]).toMatchObject({ id: "deleted-step", kind: "approval", group_id: "old-group" });
  expect(graph?.groups[0]).toMatchObject({ id: "old-group", display_name: "Historical loop" });
  expect(snapshot.steps?.[0].step_kind).toBe("approval");
});

it.each([
  { ...snapshot, available: false, unavailable_reason: "protected" },
  { ...snapshot, schema_version: 99 },
  { ...snapshot, execution_graph: { ...snapshot.execution_graph, schema_version: 99 } },
])("fails closed for unavailable or unsupported definitions", (definition) => {
  expect(archivedDefinitionCanvasGraph(definition)).toBeNull();
  render(<JobDefinitionCanvas definition={definition} />);
  expect(screen.queryByText("Historical approval")).not.toBeInTheDocument();
  expect(screen.queryByText("Original question")).not.toBeInTheDocument();
});

it("opens archived configuration read-only without editor or automation actions", async () => {
  render(<JobDefinitionCanvas definition={snapshot} />);
  const canvas = screen.getByTestId("job-definition-canvas");
  fireEvent.click(within(canvas).getByText("Historical approval"));
  const drawer = await screen.findByRole("dialog", { name: "Archived configuration" });
  fireEvent.click(within(drawer).getByText("approval: 1 field"));
  expect(within(drawer).getByText(/"prompt": "Original question"/)).toBeVisible();
  expect(within(drawer).queryByRole("textbox")).not.toBeInTheDocument();
  expect(within(canvas).queryByText("Edit")).not.toBeInTheDocument();
  expect(within(canvas).queryByText("Edit group")).not.toBeInTheDocument();
  expect(within(canvas).queryByText("auto")).not.toBeInTheDocument();
  expect(within(canvas).queryByRole("button", { name: /Add step/ })).not.toBeInTheDocument();
});


describe("Approval destinations from the actual archived definition", () => {
  const owner = { jobId: "old-run", workflowId: "workflow", stepId: "review", stepExecutionId: "review-execution" };
  function approvalArchive(target: WorkflowTargetRef): JobDefinitionPayload {
    const review = { step_id: "review", display_name: "Historical review", step_kind: "approval",
      ...(target.kind === "group_exit" ? { group_id: "old-group", ...(target.lane_id ? { lane_id: target.lane_id } : {}) } : {}),
      ...(target.kind === "for_each_exit" ? { for_each_id: "old-items", ...(target.lane_id ? { lane_id: target.lane_id } : {}) } : {}),
    };
    const groupEntry = { kind: "step", id: target.kind === "group_exit" ? "review" : "group-member" };
    const itemEntry = { kind: "step", id: target.kind === "for_each_exit" ? "review" : "item-member" };
    return { ...snapshot, steps: [review,
      { step_id: "group-member", step_kind: "stop_success", group_id: "old-group", display_name: "Historical group member" },
      { step_id: "item-member", step_kind: "stop_success", for_each_id: "old-items", display_name: "Historical item member" },
    ], groups: [{ group_id: "old-group", display_name: "Historical group", body: target.kind === "group_exit" && target.lane_id
      ? { kind: "lanes", lanes: [{ lane_id: "audit-lane", display_name: "Historical lane", entry: groupEntry }] }
      : { kind: "sequence", entry: groupEntry } }],
    for_each: [{ for_each_id: "old-items", display_name: "Historical items", over: "state.items", item_name: "item", body: target.kind === "for_each_exit" && target.lane_id
      ? { kind: "lanes", lanes: [{ lane_id: "audit-lane", display_name: "Historical item lane", entry: itemEntry }] }
      : { kind: "sequence", entry: itemEntry } }],
    execution_graph: { schema_version: 1, entry: target.kind === "group_exit" ? { kind: "group", id: "old-group" }
      : target.kind === "for_each_exit" ? { kind: "for_each", id: "old-items" } : { kind: "step", id: "review" },
      edges: ["approved", "rejected"].map((name) => ({ source: { kind: "step", id: "review" }, outcome: { name }, target, rework: false })) },
    };
  }

  it.each<{ target: WorkflowTargetRef; label: string }>([
    { target: { kind: "group", id: "old-group" }, label: "Historical group" },
    { target: { kind: "for_each", id: "old-items" }, label: "Historical items" },
    { target: { kind: "group_exit", group_id: "old-group", lane_id: null }, label: "exit of Historical group" },
    { target: { kind: "group_exit", group_id: "old-group", lane_id: "audit-lane" }, label: "exit of Historical group / Historical lane" },
    { target: { kind: "for_each_exit", for_each_id: "old-items", lane_id: "audit-lane" }, label: "end of item in Historical items" },
  ])("uses archived labels for $target.kind and its actual lane", ({ target, label }) => {
    const definition = approvalArchive(target);
    expect(archivedApprovalDestinations(definition, owner)).toEqual({ approved: label, rejected: label });
    expect(definition.revision).toBe(4);
    expect(definition.digest).toBe("historical-digest");
  });

  it.each([
    { reason: "another job's gate", patch: { job_id: "foreign-run" } },
    { reason: "a missing gate step", patch: { step_id: "" } },
    { reason: "a step absent from the archive", patch: { step_id: "current-live-review" } },
    { reason: "missing execution identity", patch: { step_execution_id: "" } },
  ])("keeps the real Review workspace but refuses destinations for $reason", ({ patch }) => {
    const definition = approvalArchive({ kind: "group", id: "old-group" });
    const canonicalDetail = buildCanonicalJobDetailView({ detailEntity: { job: { id: "old-run", selected_workflow: "workflow", status: "awaiting_approval" },
      sections: { review_workspace: { kind: "document_review", title: "Historical question", content: "Published review value", thread_id: "old-thread" } } } });
    const approvalGate = { gate_kind: "approval", approval_id: "old-gate", job_id: "old-run", thread_id: "old-thread",
      step_id: "review", step_execution_id: "review-execution", ...patch };
    const workspace = resolveReviewWorkspace({ approvalPayload: undefined, approvalGate, canonicalDetail, canMutate: true,
      currentJobId: "old-run", runDefinition: definition });
    expect(workspace).toMatchObject({ title: "Historical question", content: "Published review value", thread_id: "old-thread",
      destinations: { approved: null, rejected: null } });
  });

  it("does not substitute a current name or unknown lane when the archived route target is missing", () => {
    const definition = approvalArchive({ kind: "group_exit", group_id: "old-group", lane_id: "audit-lane" });
    const missingLane = { ...definition, groups: [{ group_id: "old-group", display_name: "Historical group", body: { kind: "lanes", lanes: [] } }] };
    expect(archivedApprovalDestinations(missingLane, owner)).toEqual({ approved: null, rejected: null });
    expect(archivedApprovalDestinations(approvalArchive({ kind: "group", id: "current-live-group" }), owner)).toEqual({ approved: null, rejected: null });
    expect(archivedApprovalDestinations(undefined, owner)).toEqual({ approved: null, rejected: null });
  });
});


const switchArchive: JobDefinitionPayload = {
  ...snapshot, groups: [], for_each: [],
  steps: [{ step_id: "choose", step_kind: "switch", display_name: "Historical choice",
    override: { switch: { selector: "state.channel", cases: [{ case_id: "a", when: "a" }, { case_id: "b", when: "b" }] } } },
    { step_id: "done", step_kind: "stop_success", display_name: "Historical done" }],
  execution_graph: { schema_version: 1, entry: { kind: "step", id: "choose" }, edges: ["a", "b"].map((case_id) => ({
    source: { kind: "step", id: "choose" }, outcome: { name: "case", case_id }, target: { kind: "step", id: "done" },
  })) },
};
function switchPage(changes: Record<string, unknown> = {}) {
  return { job_id: "old-run", steps: [], page: { has_more: true, next_cursor: "later" }, run_trace: {
    schema_version: 1, job_id: "old-run", workflow_id: "workflow", snapshot_id: "archive-1", revision: 4, digest: "historical-digest",
    covered_from_start: true, terminal: true, truncated: false, last_sequence: 1,
    transitions: [{ id: "b".repeat(64), sequence: 1, source: { kind: "step", id: "choose" }, outcome: { name: "case", case_id: "b" },
      target: { kind: "step", id: "done" }, step_execution_id: "choose-pass", attempt: 1, frames: [] }], ...changes,
  } };
}

function archivedSvg(selector: string, owner?: Element | null) {
  const canvas = owner === undefined ? screen.getByTestId("job-definition-canvas") : owner;
  // Exact SVG line/arrow geometry has no accessible role; preserve its actual owning subtree.

  return canvas?.querySelector(selector) ?? null;
}

function archivedSvgAll(selector: string) {
  const canvas = screen.getByTestId("job-definition-canvas");
  // Count actual SVG lines inside the Testing Library located archive canvas.

  return canvas.querySelectorAll(selector);
}

it("draws only the recorded archived switch case, including its line, despite a shared target and partial step page", async () => {
  const { waitFor } = await import("@testing-library/react");
  const view = render(<JobDefinitionCanvas definition={switchArchive} jobId="old-run" executionPage={switchPage()} />);
  await waitFor(() => expect(archivedSvgAll(".react-flow__edge-path[style*='--fs-workflow-edge-ran']")).toHaveLength(1));
  const taken = archivedSvg("[data-id='edge:step:choose:case:b']");
  expect(archivedSvg(".react-flow__edge-path", taken)).toHaveStyle({ stroke: "var(--fs-workflow-edge-ran)" });
  const ordinary = archivedSvg("[data-id='edge:step:choose:case:a']");
  expect(archivedSvg(".react-flow__edge-path", ordinary)).toHaveStyle({ stroke: "var(--fs-workflow-edge)" });
  expect(archivedSvg(".animated")).toBeNull();
  view.rerender(<JobDefinitionCanvas definition={switchArchive} jobId="old-run" executionPage={switchPage({ digest: "foreign-digest" })} />);
  await waitFor(() => expect(archivedSvgAll(".react-flow__edge-path[style*='--fs-workflow-edge-ran']")).toHaveLength(0));
});

it.each([{ truncated: true }, { covered_from_start: false }, { snapshot_id: "foreign" }, { job_id: "foreign" }])(
  "keeps the archived graph ordinary for unverified trace %j", async (changes) => {
    render(<JobDefinitionCanvas definition={switchArchive} jobId="old-run" executionPage={switchPage(changes)} />);
    expect(within(screen.getByTestId("job-definition-canvas")).getByText("Historical choice")).toBeInTheDocument();
    await waitFor(() => expect(archivedSvgAll(".react-flow__edge-path").length).toBeGreaterThan(1));
    expect(archivedSvg(".react-flow__edge-path[style*='--fs-workflow-edge-ran']")).toBeNull();
  });


it("refuses an internally matching foreign archive/page pair for the selected job", async () => {
  render(<JobDefinitionCanvas definition={switchArchive} jobId="different-selected-run" executionPage={switchPage()} />);
  await waitFor(() => expect(archivedSvgAll(".react-flow__edge-path").length).toBeGreaterThan(1));
  expect(archivedSvg(".react-flow__edge-path[style*='--fs-workflow-edge-ran']")).toBeNull();
});


// D062: public committed container facts, independent of operation-row completion.
function containerArchive(): JobDefinitionPayload {
  return { ...snapshot,
    steps: [{ step_id: "collect", display_name: "Collect item", step_kind: "collect", for_each_id: "old-items" }],
    groups: [{ group_id: "old-group", display_name: "Historical group", body: { kind: "sequence", entry: { kind: "for_each", id: "old-items" } } }],
    for_each: [{ for_each_id: "old-items", group_id: "old-group", display_name: "Historical items", over: "state.items", item_name: "item",
      body: { kind: "sequence", entry: { kind: "step", id: "collect" } } }],
    execution_graph: { schema_version: 1, entry: { kind: "group", id: "old-group" }, edges: [
      { source: { kind: "step", id: "collect" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "old-items", lane_id: null } },
      { source: { kind: "for_each", id: "old-items" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "old-group", lane_id: null } },
    ] } };
}
function containerProgress(changes: Record<string, unknown> = {}) {
  return { schema_version: 1, job_id: "old-run", project_id: "alpha", workflow_id: "workflow", snapshot_id: "archive-1", revision: 4,
    digest: "historical-digest", covered_from_start: true, truncated: false, checkpoint_terminal: true, last_sequence: 12,
    passes: [
      { container: { kind: "group", id: "old-group" }, execution_id: "group-pass", parent_frames: [],
        started_sequence: 1, updated_sequence: 12, finished_sequence: 12, current: false, outcome: "success" },
      { container: { kind: "for_each", id: "old-items" }, execution_id: "items-pass", parent_frames: [{ kind: "group", id: "old-group", execution_id: "group-pass" }],
        started_sequence: 2, updated_sequence: 11, finished_sequence: 11, current: false, outcome: "success",
        for_each: { item_index: 3, source_count: 5, selected_count: 5, processed_count: 4, applied_limit: 100, truncated: false, broke_at: 3 } },
    ], ...changes };
}
function containerCanvasProps(progress: unknown, definition = containerArchive()) {
  return { definition, jobId: "old-run", projectId: "alpha", executionPage: {
    job_id: "old-run", run_terminal: false, container_progress: progress,
    steps: [{ step_id: "collect", step_execution_id: "collect-pass", status: "completed", duration_ms: 90000 }],
    page: { has_more: true, next_cursor: "older" },
  } };
}

it("shows committed nested container finishes and Break 4 of 5 without inventing child-derived duration", async () => {
  render(<JobDefinitionCanvas {...containerCanvasProps(containerProgress())} />);
  const items = await screen.findByTestId("workflow-container-runtime:old-items");
  expect(within(items).getByText("4 items done")).toBeVisible();
  expect(items).toHaveAttribute("data-execution-id", "items-pass");
  const group = screen.getByTestId("workflow-container-runtime:old-group");
  expect(within(group).getByText("Done")).toBeVisible();
  expect(group).toHaveAttribute("data-execution-id", "group-pass");
  expect(within(items).queryByText(/90s|1m 30s|5 of 5/)).not.toBeInTheDocument();
  expect(within(group).queryByText(/items done|90s|1m 30s/)).not.toBeInTheDocument();
});

it.each([
  { schema_version: 2 }, { job_id: "foreign-run" }, { project_id: "foreign-project" }, { workflow_id: "foreign-workflow" },
  { snapshot_id: "foreign-archive" }, { digest: "foreign-digest" }, { revision: 5 }, { covered_from_start: false }, { truncated: true },
])("retires visible container facts for unverified public evidence %j", async (changes) => {
  const view = render(<JobDefinitionCanvas {...containerCanvasProps(containerProgress())} />);
  await screen.findByTestId("workflow-container-runtime:old-items");
  view.rerender(<JobDefinitionCanvas {...containerCanvasProps(containerProgress(changes))} />);
  expect(screen.queryByTestId("workflow-container-runtime:old-items")).not.toBeInTheDocument();
  expect(screen.queryByTestId("workflow-container-runtime:old-group")).not.toBeInTheDocument();
  expect(screen.getByText("Historical items")).toBeVisible();
});

it("uses actual lane states for the exact For each item while operation history is partial", async () => {
  const definition = containerArchive();
  definition.steps = [
    { step_id: "collect", for_each_id: "old-items", lane_id: "subtotal", step_kind: "transform" },
    { step_id: "ship", for_each_id: "old-items", lane_id: "shipping", step_kind: "transform" },
  ];
  definition.for_each = [{ ...definition.for_each![0], body: { kind: "lanes", lanes: [
    { lane_id: "subtotal", entry: { kind: "step", id: "collect" } }, { lane_id: "shipping", entry: { kind: "step", id: "ship" } },
  ] } }];
  definition.execution_graph = { schema_version: 1, entry: { kind: "group", id: "old-group" }, edges: [
    { source: { kind: "step", id: "collect" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "old-items", lane_id: "subtotal" } },
    { source: { kind: "step", id: "ship" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "old-items", lane_id: "shipping" } },
    { source: { kind: "for_each", id: "old-items" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "old-group", lane_id: null } },
  ] };
  const progress = containerProgress();
  const passes = progress.passes.map((pass) => ({ ...pass, current: true, outcome: null, finished_sequence: null,
    ...(pass.container.kind === "for_each" ? {
      for_each: { item_index: 0, source_count: 2, selected_count: 2, processed_count: 0, applied_limit: 100, truncated: false, broke_at: null },
      lanes: { item_index: 0, completed_count: 1, total_count: 2, rows: [{ lane_id: "subtotal", status: "completed" }, { lane_id: "shipping", status: "escalated" }] },
    } : {}),
  }));
  render(<JobDefinitionCanvas {...containerCanvasProps({ ...progress, checkpoint_terminal: false, passes }, definition)} />);
  const items = await screen.findByTestId("workflow-container-runtime:old-items");
  expect(within(items).getByText("Item 1 of 2")).toBeVisible();
  expect(within(items).getByText("Lanes 1 of 2 done")).toBeVisible();
  expect(within(items).queryByText("All lanes done")).not.toBeInTheDocument();
});

it("does not turn a terminal retained unfinished pass into completed or current", async () => {
  const progress = containerProgress();
  const unfinished = progress.passes.map((pass) => ({ ...pass, current: true, outcome: null, finished_sequence: null }));
  const view = render(<JobDefinitionCanvas {...containerCanvasProps({ ...progress, checkpoint_terminal: false, passes: unfinished })} />);
  const items = await screen.findByTestId("workflow-container-runtime:old-items");
  expect(items).toHaveAttribute("data-run", "running");
  view.rerender(<JobDefinitionCanvas {...containerCanvasProps({ ...progress, passes: unfinished.map((pass) => ({ ...pass, current: false })) })} />);
  expect(screen.queryByTestId("workflow-container-runtime:old-items")).not.toBeInTheDocument();
  expect(screen.queryByTestId("workflow-container-runtime:old-group")).not.toBeInTheDocument();
});

it("shows a real empty selection as zero of zero without a fabricated first item", async () => {
  const progress = containerProgress();
  const passes = progress.passes.map((pass) => ({ ...pass, ...(pass.container.kind === "for_each" ? {
    for_each: { item_index: null, source_count: 0, selected_count: 0, processed_count: 0, applied_limit: 100, truncated: false, broke_at: null },
  } : {}) }));
  render(<JobDefinitionCanvas {...containerCanvasProps({ ...progress, passes })} />);
  const items = await screen.findByTestId("workflow-container-runtime:old-items");
  expect(within(items).getByText("0 items done")).toBeVisible();
  expect(within(items).queryByText(/Item 1|1 of 0/)).not.toBeInTheDocument();
});


it("keeps a failed container badge distinct from the accepted green error-route line and arrow", async () => {
  const definition: JobDefinitionPayload = { ...snapshot,
    steps: [...snapshot.steps!, { step_id: "recovered", step_kind: "stop_success", display_name: "Recovered" }],
    execution_graph: { schema_version: 1, entry: { kind: "group", id: "old-group" }, edges: [
      { source: { kind: "group", id: "old-group" }, outcome: { name: "error" }, target: { kind: "step", id: "recovered" } },
    ] } };
  const transition = { id: "f".repeat(64), sequence: 1, source: { kind: "group", id: "old-group" }, outcome: { name: "error" },
    target: { kind: "step", id: "recovered" }, container_execution_id: "group-pass", frames: [{ kind: "group", id: "old-group", execution_id: "group-pass" }] };
  const progress = containerProgress({ passes: [{ container: { kind: "group", id: "old-group" }, execution_id: "group-pass", parent_frames: [],
    started_sequence: 1, updated_sequence: 2, finished_sequence: 2, current: false, outcome: "error",
    failure: { code: "boundary_failed", message: "Expected local failure", boundary: "exit",
      handling: { kind: "route", execution_id: "group-pass", transition_id: transition.id } },
  }], last_sequence: 2 });
  const props = containerCanvasProps(progress, definition);
  const run_trace = { schema_version: 1, job_id: "old-run", workflow_id: "workflow", snapshot_id: "archive-1", revision: 4, digest: "historical-digest",
    covered_from_start: true, truncated: false, terminal: true, last_sequence: 1, transitions: [transition] };
  render(<JobDefinitionCanvas {...props} executionPage={{ ...props.executionPage, run_trace }} />);
  const group = await screen.findByTestId("workflow-container-runtime:old-group");
  expect(within(group).getByText("Failed")).toBeVisible();
  expect(within(group).queryByText("Completed")).not.toBeInTheDocument();
  await waitFor(() => expect(archivedSvgAll(".react-flow__edge-path[style*='--fs-workflow-edge-ran']")).toHaveLength(1));
  const taken = archivedSvg("[data-id='edge:group:old-group:error']");
  expect(taken).not.toBeNull();
  expect(archivedSvg(".react-flow__edge-path", taken)).toHaveStyle({ stroke: "var(--fs-workflow-edge-ran)" });
  for (const edge of archivedSvgAll(".react-flow__edge")) {
    if (edge.getAttribute("data-id") === "edge:group:old-group:error") continue;
    expect(archivedSvg(".react-flow__edge-path", edge)).toHaveStyle({ stroke: "var(--fs-workflow-edge)" });
  }
  expect(archivedSvg(".animated")).toBeNull();
});

// The newest observed pass wins even when it never reached a finish boundary.
it("suppresses an older completed container when a newer terminal pass remains unfinished", async () => {
  const progress = containerProgress();
  const newer = [
    { container: { kind: "group", id: "old-group" }, execution_id: "new-group-pass", parent_frames: [],
      started_sequence: 13, updated_sequence: 20, finished_sequence: null, current: false, outcome: null },
    { container: { kind: "for_each", id: "old-items" }, execution_id: "new-items-pass",
      parent_frames: [{ kind: "group", id: "old-group", execution_id: "new-group-pass" }],
      started_sequence: 14, updated_sequence: 19, finished_sequence: null, current: false, outcome: null,
      for_each: { item_index: 1, source_count: 3, selected_count: 3, processed_count: 1, applied_limit: 100, truncated: false, broke_at: null } },
  ];
  const view = render(<JobDefinitionCanvas {...containerCanvasProps(progress)} />);
  await screen.findByTestId("workflow-container-runtime:old-items");
  view.rerender(<JobDefinitionCanvas {...containerCanvasProps({ ...progress, last_sequence: 20, passes: [...progress.passes, ...newer] })} />);
  await waitFor(() => expect(screen.queryByTestId("workflow-container-runtime:old-items")).not.toBeInTheDocument());
  expect(screen.queryByTestId("workflow-container-runtime:old-group")).not.toBeInTheDocument();
  expect(screen.getByText("Historical items")).toBeVisible();
  expect(screen.getByText("Historical group")).toBeVisible();
});

function repeatedLaneArchive() {
  const definition = containerArchive();
  definition.steps = [
    { step_id: "collect", for_each_id: "old-items", lane_id: "subtotal", step_kind: "transform" },
    { step_id: "ship", for_each_id: "old-items", lane_id: "shipping", step_kind: "transform" },
  ];
  definition.for_each = [{ ...definition.for_each![0], body: { kind: "lanes", lanes: [
    { lane_id: "subtotal", entry: { kind: "step", id: "collect" } }, { lane_id: "shipping", entry: { kind: "step", id: "ship" } },
  ] } }];
  definition.execution_graph = { schema_version: 1, entry: { kind: "group", id: "old-group" }, edges: [
    { source: { kind: "step", id: "collect" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "old-items", lane_id: "subtotal" } },
    { source: { kind: "step", id: "ship" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "old-items", lane_id: "shipping" } },
    { source: { kind: "for_each", id: "old-items" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "old-group", lane_id: null } },
  ] };
  return definition;
}
function repeatedLaneProgress() {
  const progress = containerProgress();
  return { ...progress, checkpoint_terminal: false, last_sequence: 20, passes: [...progress.passes.map((pass) => ({ ...pass, ...(pass.container.kind === "for_each" ? {
    for_each: { item_index: 1, source_count: 2, selected_count: 2, processed_count: 2, applied_limit: 100, truncated: false, broke_at: null },
    lanes: { item_index: 1, completed_count: 2, total_count: 2, rows: [{ lane_id: "subtotal", status: "completed" }, { lane_id: "shipping", status: "completed" }] },
  } : {}) })),
    { container: { kind: "group", id: "old-group" }, execution_id: "current-group", parent_frames: [],
      started_sequence: 13, updated_sequence: 20, finished_sequence: null, current: true, outcome: null },
    { container: { kind: "for_each", id: "old-items" }, execution_id: "current-items",
      parent_frames: [{ kind: "group", id: "old-group", execution_id: "current-group" }],
      started_sequence: 14, updated_sequence: 19, finished_sequence: null, current: true, outcome: null,
      for_each: { item_index: 1, source_count: 3, selected_count: 3, processed_count: 1, applied_limit: 100, truncated: false, broke_at: null },
      lanes: { item_index: 1, completed_count: 0, total_count: 2, rows: [{ lane_id: "subtotal", status: "pending" }, { lane_id: "shipping", status: "pending" }] } },
  ] };
}

it("selects the current repeated parent and item instead of a previous pass's finish or lanes", async () => {
  render(<JobDefinitionCanvas {...containerCanvasProps(repeatedLaneProgress(), repeatedLaneArchive())} />);
  const items = await screen.findByTestId("workflow-container-runtime:old-items");
  expect(items).toHaveAttribute("data-execution-id", "current-items");
  expect(within(items).getByText("Item 2 of 3")).toBeVisible();
  expect(within(items).getByText("Lanes 0 of 2 done")).toBeVisible();
  expect(within(items).queryByText(/items done|4 of 5|Lanes 1 of 2 done/)).not.toBeInTheDocument();
  const group = screen.getByTestId("workflow-container-runtime:old-group");
  expect(group).toHaveAttribute("data-execution-id", "current-group");
  expect(within(group).getByText("Running")).toBeVisible();
});

it.each(["parent execution", "item scope", "foreign lane", "lane count"])("rejects repeated-pass evidence with a contradictory %s", async (reason) => {
  const definition = repeatedLaneArchive();
  const valid = repeatedLaneProgress();
  const view = render(<JobDefinitionCanvas {...containerCanvasProps(valid, definition)} />);
  await screen.findByTestId("workflow-container-runtime:old-items");
  const invalid = structuredClone(valid);
  const item = invalid.passes.find((pass) => pass.execution_id === "current-items");
  if (!item || !("lanes" in item) || !item.lanes) throw new Error("Expected the fixture current lane pass");
  if (reason === "parent execution") item.parent_frames[0].execution_id = "group-pass";
  else if (reason === "item scope") item.lanes.item_index = 0;
  else if (reason === "foreign lane") item.lanes.rows[1].lane_id = "not-declared";
  else item.lanes.completed_count = 2;
  view.rerender(<JobDefinitionCanvas {...containerCanvasProps(invalid, definition)} />);
  await waitFor(() => expect(screen.queryByTestId("workflow-container-runtime:old-items")).not.toBeInTheDocument());
  expect(screen.queryByTestId("workflow-container-runtime:old-group")).not.toBeInTheDocument();
});
