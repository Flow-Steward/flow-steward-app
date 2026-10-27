import { useLayoutEffect } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { WorkflowCanvasGraph } from "@/pages/workflows/shared/api";
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { JobDefinitionPayload, JobExecutionSnapshot, JobExecutionStepsPage } from "@/pages/jobs/detail/api";
import { useJobExecutionSnapshot } from "@/pages/jobs/detail/job-execution-snapshot";
import type * as JobExecutionSnapshotModule from "@/pages/jobs/detail/job-execution-snapshot";
import { useWorkflowHookExecutionMarks, type ControlledParentRun, type ControlledRunOwner } from "./detail-hook-run-marks";

vi.mock("@/pages/jobs/detail/job-execution-snapshot", () => ({ useJobExecutionSnapshot: vi.fn() }));
const read = vi.mocked(useJobExecutionSnapshot);
const frames = new Map([
  ["before_step", { hookId: "before", stepId: "before_step", targetWorkflowId: "client", targetRevision: 3 }],
  ["after_step", { hookId: "after", stepId: "after_step", targetWorkflowId: "client", targetRevision: 3 }],
]);
const graph = { steps: [{ id: "hook:before:send" }, { id: "hook:after:send" }], groups: [], edges: [] } as never;
const child: JobExecutionSnapshot = { jobId: "child", definitionWorkflowId: "client", definitionRevision: 3,
  definitionSteps: [{ stepId: "send" }], steps: [{ stepId: "send", stepExecutionId: "send-1", status: "failed" }], childInvocations: [], complete: true, terminal: true };
const parent: JobExecutionSnapshot = { jobId: "parent", definitionWorkflowId: "author", definitionRevision: 4,
  definitionSteps: [{ stepId: "before_step", hookId: "before" }], steps: [{ stepId: "before_step", stepExecutionId: "parent-1", status: "failed" }],
  childInvocations: [{ stepId: "before_step", stepExecutionId: "parent-1", invocationKey: "parent:parent-1", childJobId: "child", childWorkflowId: "client" }], complete: true, terminal: true };
function Harness({ revision = 4 }: Readonly<{ revision?: number }>) {
  const state = useWorkflowHookExecutionMarks(graph, frames, "author", revision);
  return <>{state.observers}<output data-testid="marks">{JSON.stringify(state.marks)}</output>
    <button onClick={() => state.onWorkflowSnapshot(parent)}>Whole run</button>
    <button onClick={() => state.onHookTestRunStarted("child", "after")}>Isolated after</button>
    <button onClick={() => state.onHookTestRunStarted("new-child", "after")}>New isolated run</button></>;
}
beforeEach(() => {
  read.mockReset();
  read.mockImplementation((jobId) => ({ snapshot: jobId === "child" ? child : null, pending: false, error: null }));
});
it("reads the actual linked child and paints only the originating hook after a whole run", async () => {
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Whole run" }));
  await waitFor(() => expect(JSON.parse(screen.getByTestId("marks").textContent || "{}")).toEqual({
    "hook:before:send": { status: "failed", jobId: "child", stepExecutionId: "send-1" },
  }));
  expect(read).toHaveBeenCalledWith("child", true);
});
it("isolated tests identify their hook and an old result cannot paint a newly started run", async () => {
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Isolated after" }));
  await waitFor(() => expect(screen.getByTestId("marks")).toHaveTextContent("hook:after:send"));
  fireEvent.click(screen.getByRole("button", { name: "New isolated run" }));
  await waitFor(() => expect(screen.getByTestId("marks")).toHaveTextContent("{}"));
});
it("removes whole-run marks when the author definition changes", async () => {
  const view = render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Whole run" }));
  await waitFor(() => expect(screen.getByTestId("marks")).toHaveTextContent("hook:before:send"));
  view.rerender(<Harness revision={5} />);
  await waitFor(() => expect(screen.getByTestId("marks")).toHaveTextContent("{}"));
});
it("does not paint denied or unavailable child archives", async () => {
  read.mockReturnValue({ snapshot: null, pending: false, error: new Error("Access denied") });
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Whole run" }));
  await waitFor(() => expect(read).toHaveBeenCalledWith("child", true));
  expect(screen.getByTestId("marks")).toHaveTextContent("{}");
});


const selectedProof = { workflowId: "author", projectId: "project-a", editorRevision: 4, jobId: "parent", generation: 11 };
function ControlledHarness({ controlledParent, controlledOwner = { projectId: "project-a", ownerEpoch: 1 } }: Readonly<{
  controlledParent: ControlledParentRun | null; controlledOwner?: ControlledRunOwner;
}>) {
  const state = useWorkflowHookExecutionMarks(graph, frames, "author", 4, "legacy-parent", true, { parent: controlledParent, owner: controlledOwner });
  return <>{state.observers}<output data-testid="controlled-marks">{JSON.stringify(state.marks)}</output>
    <button onClick={() => state.onWorkflowSnapshot(parent)}>Old Results callback</button>
    <button onClick={() => state.onHookTestRunStarted("child", "after")}>Run isolated after</button></>;
}

it("makes explicit null a controlled empty parent, without subscribing to or reviving legacy parent marks", async () => {
  render(<ControlledHarness controlledParent={null} />);
  expect(read).toHaveBeenCalledWith("", false);
  expect(read).not.toHaveBeenCalledWith("legacy-parent", true);
  fireEvent.click(screen.getByRole("button", { name: "Old Results callback" }));
  expect(screen.getByTestId("controlled-marks")).toHaveTextContent("{}");
  fireEvent.click(screen.getByRole("button", { name: "Run isolated after" }));
  await waitFor(() => expect(screen.getByTestId("controlled-marks")).toHaveTextContent("hook:after:send"));
  expect(read).toHaveBeenCalledWith("child", true, "project-a");
});

it("uses only the selected verified parent and actual linked child, never the legacy acknowledged parent", async () => {
  const view = render(<ControlledHarness controlledParent={{ proof: selectedProof, snapshot: parent }} />);
  await waitFor(() => expect(JSON.parse(screen.getByTestId("controlled-marks").textContent || "{}")).toEqual({
    "hook:before:send": { status: "failed", jobId: "child", stepExecutionId: "send-1" },
  }));
  expect(read).toHaveBeenCalledWith("", false);
  expect(read).not.toHaveBeenCalledWith("legacy-parent", true);
  fireEvent.click(screen.getByRole("button", { name: "Old Results callback" }));
  expect(screen.getByTestId("controlled-marks")).toHaveTextContent("hook:before:send");
  view.rerender(<ControlledHarness controlledParent={{ proof: selectedProof, snapshot: null }} />);
  await waitFor(() => expect(screen.getByTestId("controlled-marks")).toHaveTextContent("{}"));
});

it("retires controlled parent and child evidence on selection generation, job, revision and explicit null", async () => {
  const view = render(<ControlledHarness controlledParent={{ proof: selectedProof, snapshot: parent }} />);
  await waitFor(() => expect(screen.getByTestId("controlled-marks")).toHaveTextContent("hook:before:send"));
  view.rerender(<ControlledHarness controlledParent={{ proof: { ...selectedProof, generation: 12 }, snapshot: parent }} />);
  await waitFor(() => expect(screen.getByTestId("controlled-marks")).toHaveTextContent("hook:before:send"));
  view.rerender(<ControlledHarness controlledParent={{ proof: { ...selectedProof, jobId: "other", generation: 13 }, snapshot: parent }} />);
  await waitFor(() => expect(screen.getByTestId("controlled-marks")).toHaveTextContent("{}"));
  view.rerender(<ControlledHarness controlledParent={{ proof: { ...selectedProof, editorRevision: 5, generation: 14 }, snapshot: parent }} />);
  expect(screen.getByTestId("controlled-marks")).toHaveTextContent("{}");
  view.rerender(<ControlledHarness controlledParent={null} />);
  expect(screen.getByTestId("controlled-marks")).toHaveTextContent("{}");
});

it("rejects stale isolated starts across selected owner A to B to A and keeps the current launch", () => {
  const view = renderHook(({ input }) => useWorkflowHookExecutionMarks(graph, frames, "author", 4, "legacy-parent", true, { parent: input }), {
    initialProps: { input: { proof: selectedProof, snapshot: null } as ControlledParentRun | null },
  });
  const oldStart = view.result.current.onHookTestRunStarted;
  const oldWorkflow = view.result.current.onWorkflowSnapshot;
  act(() => oldStart("child", "after"));
  expect(view.result.current.observers).toHaveLength(1);
  view.rerender({ input: { proof: { ...selectedProof, projectId: "project-b", generation: 12 }, snapshot: null } });
  expect(view.result.current.observers).toHaveLength(0);
  view.rerender({ input: { proof: selectedProof, snapshot: null } }); // A returns with the same public proof; local epoch still retires old callbacks.
  expect(view.result.current.observers).toHaveLength(0);
  act(() => { oldStart("child", "after"); oldWorkflow(parent); });
  expect(view.result.current.observers).toHaveLength(0);
  act(() => view.result.current.onHookTestRunStarted("child", "after"));
  expect(view.result.current.observers).toHaveLength(1);
});


it.each([
  ["workflow", { workflowId: "other-author", revision: 4 }],
  ["revision", { workflowId: "author", revision: 5 }],
] as const)("retires controlled-null isolated callbacks across %s A to B to A", (_label, other) => {
  const view = renderHook(({ workflowId, revision }) => useWorkflowHookExecutionMarks(graph, frames, workflowId, revision, "legacy-parent", true, { parent: null, owner: { projectId: "project-a", ownerEpoch: 1 } }), { initialProps: { workflowId: "author", revision: 4 } });
  const oldStart = view.result.current.onHookTestRunStarted;
  act(() => oldStart("child", "after"));
  expect(view.result.current.observers).toHaveLength(1);
  view.rerender(other);
  expect(view.result.current.observers).toHaveLength(0);
  view.rerender({ workflowId: "author", revision: 4 });
  expect(view.result.current.observers).toHaveLength(0);
  act(() => oldStart("child", "after"));
  expect(view.result.current.observers).toHaveLength(0);
  act(() => view.result.current.onHookTestRunStarted("child", "after"));
  expect(view.result.current.observers).toHaveLength(1);
});


it("fails closed for controlled-null Hook isolation without a current project owner epoch", () => {
  const view = renderHook(() => useWorkflowHookExecutionMarks(graph, frames, "author", 4, "legacy-parent", true, { parent: null }));
  expect(read).toHaveBeenCalledWith("", false);
  act(() => view.result.current.onHookTestRunStarted("child", "after"));
  expect(view.result.current.observers).toHaveLength(0);
  expect(view.result.current.marks).toEqual({});
});

it("retires null-parent isolated Hook callbacks across project A to B to A without erasing the current B launch", () => {
  const view = renderHook(({ projectId, ownerEpoch }) => useWorkflowHookExecutionMarks(graph, frames, "author", 4, "legacy-parent", true, { parent: null, owner: { projectId, ownerEpoch } }), {
    initialProps: { projectId: "project-a", ownerEpoch: 1 },
  });
  const oldStart = view.result.current.onHookTestRunStarted;
  act(() => oldStart("child", "after"));
  expect(view.result.current.observers).toHaveLength(1);
  view.rerender({ projectId: "project-b", ownerEpoch: 2 });
  expect(view.result.current.observers).toHaveLength(0);
  act(() => view.result.current.onHookTestRunStarted("child", "after"));
  expect(view.result.current.observers).toHaveLength(1); // B's current launch is not retired by A's callback.
  const currentBSourceKey = view.result.current.observers[0]?.key;
  act(() => oldStart("stale-child", "before"));
  expect(view.result.current.observers).toHaveLength(1);
  expect(view.result.current.observers[0]?.key).toBe(currentBSourceKey);
  view.rerender({ projectId: "project-a", ownerEpoch: 3 });
  expect(view.result.current.observers).toHaveLength(0);
  act(() => oldStart("stale-child", "before"));
  expect(view.result.current.observers).toHaveLength(0);
  act(() => view.result.current.onHookTestRunStarted("child", "after"));
  expect(view.result.current.observers).toHaveLength(1);
});

it("rejects a controlled parent whose supplied current project contradicts its selected proof", () => {
  const view = renderHook(() => useWorkflowHookExecutionMarks(graph, frames, "author", 4, "legacy-parent", true, { parent: { proof: selectedProof, snapshot: parent }, owner: { projectId: "project-b", ownerEpoch: 2 } }));
  expect(view.result.current.marks).toEqual({});
  expect(view.result.current.observers).toHaveLength(0);
  act(() => view.result.current.onHookTestRunStarted("child", "after"));
  expect(view.result.current.observers).toHaveLength(0);
});


it("fences taken edges by the selected owner and the actual child archive without depending on child step completeness", async () => {
  const { jobExecutionTrace, runtimeEdgeKey } = await import("@/pages/jobs/detail/job-execution-trace");
  const edge = { source: { kind: "step" as const, id: "send" }, outcome: { name: "success" as const }, target: { kind: "step" as const, id: "done" } };
  const definition = { job_id: "child", available: true, workflow_id: "client", revision: 3, snapshot_id: "child-archive", digest: "child-digest", schema_version: 1,
    steps: [{ step_id: "send" }, { step_id: "done" }], groups: [], for_each: [], execution_graph: { schema_version: 1, edges: [edge] } };
  const runTrace = jobExecutionTrace("child", definition, { job_id: "child", steps: [], page: {}, run_trace: {
    schema_version: 1, job_id: "child", workflow_id: "client", revision: 3, snapshot_id: "child-archive", digest: "child-digest",
    covered_from_start: true, terminal: false, truncated: false, last_sequence: 1,
    transitions: [{ ...edge, id: "c".repeat(64), sequence: 1, step_execution_id: "send-pass", attempt: 1, frames: [] }],
  } });
  expect(runTrace).toBeDefined();
  const tracedChild: JobExecutionSnapshot = { ...child, complete: false, definitionSnapshotId: "child-archive", definitionDigest: "child-digest", runTrace };
  read.mockImplementation((jobId) => ({ snapshot: jobId === "child" ? tracedChild : null, pending: false, error: null }));
  const traceGraph = { workflow: { id: "author", revision: 4 }, steps: [{ id: "hook:before:send" }, { id: "hook:before:done" }], groups: [],
    edges: [{ ...edge, source: { kind: "step", id: "hook:before:send" }, target: { kind: "step", id: "hook:before:done" } }] } as never;
  function TraceHarness({ run, revision = 4 }: Readonly<{ run: ControlledParentRun | null; revision?: number }>) {
    const state = useWorkflowHookExecutionMarks(traceGraph, frames, "author", revision, undefined, false, { parent: run, owner: { projectId: "project-a", ownerEpoch: 1 } });
    return <>{state.observers}<output data-testid="taken-edges">{JSON.stringify([...state.edges.keys()])}</output></>;
  }
  const view = render(<TraceHarness run={{ proof: selectedProof, snapshot: parent }} />);
  await waitFor(() => expect(JSON.parse(screen.getByTestId("taken-edges").textContent || "[]")).toEqual([runtimeEdgeKey({ ...edge, source: { kind: "step", id: "hook:before:send" }, target: { kind: "step", id: "hook:before:done" } })]));
  view.rerender(<TraceHarness run={{ proof: { ...selectedProof, jobId: "other", generation: 12 }, snapshot: parent }} />);
  expect(screen.getByTestId("taken-edges")).toHaveTextContent("[]");
  view.rerender(<TraceHarness run={{ proof: selectedProof, snapshot: parent }} revision={0} />);
  expect(screen.getByTestId("taken-edges")).toHaveTextContent("[]");
});

// D062 exercises the real snapshot reader/parser. Only the existing transport
// functions are substituted; every observed child snapshot has real memoization.
const containerHookGraph: WorkflowCanvasGraph = {
  workflow: { id: "author", revision: 4, display_name: "Author", enabled: true, callable_by_parent: false },
  interface_contract: {}, output_contract: {}, entry: { kind: "group", id: "batch" },
  groups: [{ id: "batch", display_name: "Parent batch", body: { kind: "sequence", entry: { kind: "group", id: "hook:before" } } }, ...["before", "after"].flatMap((hookId) => [
    { id: `hook:${hookId}`, display_name: hookId, parent: { kind: "group" as const, id: "batch" }, body: { kind: "sequence" },
      hook: { hookId, stepId: `${hookId}_step`, displayName: hookId, connected: true, targetWorkflowId: "client", targetName: "Client", usedIn: 2 } },
    { id: `hook:${hookId}:batch`, display_name: `${hookId} batch`, parent: { kind: "group" as const, id: `hook:${hookId}` },
      body: { kind: "sequence", entry: { kind: "step", id: `hook:${hookId}:member` } } },
  ])],
  steps: ["before", "after"].map((hookId) => ({ id: `hook:${hookId}:member`, group_id: `hook:${hookId}:batch`,
    kind: "transform", display_name: `${hookId} member`, config_summary: "", ports: [] })),
  edges: [], validation_issues: [], automation_rules: [],
};
function containerParent(jobId: string, beforeJobId: string, afterJobId?: string): JobExecutionSnapshot {
  const links = [{ hookId: "before", childJobId: beforeJobId }, ...(afterJobId ? [{ hookId: "after", childJobId: afterJobId }] : [])];
  return { jobId, definitionWorkflowId: "author", definitionRevision: 4, definitionSnapshotId: `${jobId}-archive`, definitionDigest: `${jobId}-digest`,
    definitionSteps: links.map(({ hookId }) => ({ stepId: `${hookId}_step`, hookId })),
    steps: links.map(({ hookId }) => ({ stepId: `${hookId}_step`, stepExecutionId: `${jobId}-${hookId}-invoke`, status: "completed" })),
    childInvocations: links.map(({ hookId, childJobId }) => ({ stepId: `${hookId}_step`, stepExecutionId: `${jobId}-${hookId}-invoke`,
      invocationKey: `${jobId}:${hookId}`, childJobId, childWorkflowId: "client" })), complete: true, terminal: true };
}
function ContainerHookHarness({ run, projectId = "project-a", ownerEpoch = 1, revision = 4, frameMap = frames, onState }: Readonly<{
  run: ControlledParentRun | null; projectId?: string; ownerEpoch?: number; revision?: number;
  frameMap?: typeof frames; onState?: (value: ReturnType<typeof useWorkflowHookExecutionMarks>) => void;
}>) {
  const state = useWorkflowHookExecutionMarks(containerHookGraph, frameMap, "author", revision, undefined, false, { parent: run, owner: { projectId, ownerEpoch } });
  // The proposed optional result is unknown here; tests never manufacture validated evidence.
  const result: typeof state & { containerMarks?: unknown } = state;
  useLayoutEffect(() => { onState?.(state); }, [onState, state]);
  return <>{state.observers}<output data-testid="container-hook-marks">{JSON.stringify(result.containerMarks ?? {})}</output></>;
}
async function containerTransport() {
  const api = await import("@/pages/jobs/detail/api");
  const actual = await vi.importActual<typeof JobExecutionSnapshotModule>("@/pages/jobs/detail/job-execution-snapshot");
  const definitions = new Map<string, JobDefinitionPayload>();
  const pages = new Map<string, JobExecutionStepsPage & { container_progress?: unknown }>();
  function definition(jobId: string) {
    return { job_id: jobId, available: true, workflow_id: "client", revision: 3, snapshot_id: `${jobId}-archive`, digest: `${jobId}-digest`, schema_version: 1,
      steps: [{ step_id: "member", step_kind: "transform", group_id: "batch" }],
      groups: [{ group_id: "batch", display_name: "Batch", body: { kind: "sequence", entry: { kind: "step", id: "member" } } }], for_each: [],
      execution_graph: { schema_version: 1, entry: { kind: "group", id: "batch" }, edges: [
        { source: { kind: "step", id: "member" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "batch", lane_id: null } },
      ] } };
  }
  function page(jobId: string, projectId: string, executionId: string) {
    return { job_id: jobId, run_terminal: true, steps: [], page: { has_more: false, next_cursor: "" }, container_progress: {
      schema_version: 1, job_id: jobId, project_id: projectId, workflow_id: "client", revision: 3, snapshot_id: `${jobId}-archive`, digest: `${jobId}-digest`,
      covered_from_start: true, truncated: false, checkpoint_terminal: true, last_sequence: 2,
      passes: [{ container: { kind: "group", id: "batch" }, execution_id: executionId, parent_frames: [],
        started_sequence: 1, updated_sequence: 2, finished_sequence: 2, current: false, outcome: "success" }],
    } };
  }
  for (const [jobId, projectId, executionId] of [["child-before", "project-a", "before-pass"], ["child-after", "project-a", "after-pass"], ["child-b", "project-b", "b-pass"]]) {
    definitions.set(jobId, definition(jobId));
    pages.set(jobId, page(jobId, projectId, executionId));
  }
  definitions.set("parent", { ...definition("parent"), workflow_id: "author", revision: 4,
    steps: [{ step_id: "before_step", step_kind: "invoke_workflow", group_id: "batch", override: { invoke_workflow: { hook: { hook_id: "before" } } } }],
    groups: [{ group_id: "batch", display_name: "Parent batch", body: { kind: "sequence", entry: { kind: "step", id: "before_step" } } }],
    execution_graph: { schema_version: 1, entry: { kind: "group", id: "batch" }, edges: [
      { source: { kind: "step", id: "before_step" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "batch", lane_id: null } },
    ] },
  });
  const parentPage = page("parent", "project-a", "parent-pass");
  pages.set("parent", { ...parentPage, container_progress: { ...parentPage.container_progress, workflow_id: "author", revision: 4 },
    steps: [{ step_id: "before_step", step_execution_id: "parent-before-invoke", status: "completed", group_id: "batch" }],
    child_invocations: [{ step_id: "before_step", step_execution_id: "parent-before-invoke", invocation_key: "parent:before",
      child_job_id: "child-before", child_workflow_id: "client" }],
  });
  const definitionRead = vi.spyOn(api, "fetchJobDefinition").mockImplementation(async (jobId) => definitions.get(jobId)
    ?? { job_id: jobId, available: false, steps: [], groups: [] });
  const executionRead = vi.spyOn(api, "fetchJobExecutionStepsPage").mockImplementation(async (jobId) => pages.get(jobId)
    ?? { job_id: jobId, steps: [], page: { has_more: false, next_cursor: "" } });
  read.mockImplementation(actual.useJobExecutionSnapshot);
  return { restore: () => { definitionRead.mockRestore(); executionRead.mockRestore(); } };
}

it("keeps two same-named child containers under their actual Hook jobs and authoritative project", async () => {
  const transport = await containerTransport();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let view: ReturnType<typeof render> | undefined;
  try {
    view = render(<QueryClientProvider client={queryClient}><ContainerHookHarness
      run={{ proof: selectedProof, snapshot: containerParent("parent", "child-before", "child-after") }} /></QueryClientProvider>);
    const marks = screen.getByTestId("container-hook-marks");
    await waitFor(() => expect(marks).toHaveTextContent("group:hook:before:batch"));
    expect(marks).toHaveTextContent("group:hook:after:batch");
    expect(marks).toHaveTextContent("before-pass");
    expect(marks).toHaveTextContent("after-pass");
    expect(marks).toHaveTextContent("child-before");
    expect(marks).toHaveTextContent("child-after");
    expect(marks).not.toHaveTextContent('"group:batch"');
    expect(marks).not.toHaveTextContent('"group:hook:before"');
    expect(read).toHaveBeenCalledWith("child-before", true, "project-a");
    expect(read).toHaveBeenCalledWith("child-after", true, "project-a");
  } finally { view?.unmount(); queryClient.clear(); transport.restore(); }
});

it.each(["unapplied revision", "child revision"])("retires previously visible Hook aggregate evidence for %s", async (reason) => {
  const transport = await containerTransport();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const run = { proof: selectedProof, snapshot: containerParent("parent", "child-before") };
  let view: ReturnType<typeof render> | undefined;
  try {
    view = render(<QueryClientProvider client={queryClient}><ContainerHookHarness run={run} /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("before-pass"));
    const changedFrames = new Map([...frames].map(([key, value]) => [key, { ...value, targetRevision: reason === "child revision" ? 4 : value.targetRevision }] as const));
    view.rerender(<QueryClientProvider client={queryClient}><ContainerHookHarness run={run}
      revision={reason === "unapplied revision" ? 0 : 4} frameMap={changedFrames} /></QueryClientProvider>);
    expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("{}");
  } finally { view?.unmount(); queryClient.clear(); transport.restore(); }
});

it("fences parsed Hook aggregates across project and selected-run A to B to A, including a retired observer callback", async () => {
  const transport = await containerTransport();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onState = vi.fn<(value: ReturnType<typeof useWorkflowHookExecutionMarks>) => void>();
  const a = { proof: selectedProof, snapshot: containerParent("parent", "child-before") };
  let view: ReturnType<typeof render> | undefined;
  try {
    view = render(<QueryClientProvider client={queryClient}><ContainerHookHarness run={a} onState={onState} /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("before-pass"));
    const oldState = onState.mock.calls.at(-1)?.[0];
    const retired = oldState?.observers[0]?.props;
    const oldSnapshot = read.mock.results.flatMap((result) => result.type === "return" && result.value.snapshot?.jobId === "child-before"
      ? [result.value.snapshot] : []).at(-1);
    if (!oldState || !retired || !oldSnapshot) throw new Error("Expected the real initial Hook observer and parsed child snapshot");
    const b = { proof: { ...selectedProof, jobId: "parent-b", projectId: "project-b", generation: 12 }, snapshot: containerParent("parent-b", "child-b") };
    view.rerender(<QueryClientProvider client={queryClient}><ContainerHookHarness run={b} projectId="project-b" ownerEpoch={2} onState={onState} /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("b-pass"));
    act(() => { retired.onSnapshot(retired.source.key, oldSnapshot); oldState.onHookTestRunStarted("child-before", "before"); });
    expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("b-pass");
    expect(screen.getByTestId("container-hook-marks")).not.toHaveTextContent("before-pass");
    view.rerender(<QueryClientProvider client={queryClient}><ContainerHookHarness run={null} ownerEpoch={3} onState={onState} /></QueryClientProvider>);
    expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("{}");
    act(() => { retired.onSnapshot(retired.source.key, oldSnapshot); oldState.onHookTestRunStarted("child-before", "before"); });
    expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("{}");
    const current = onState.mock.calls.at(-1)?.[0];
    if (!current) throw new Error("Expected current controlled-null owner");
    act(() => current.onHookTestRunStarted("child-before", "before"));
    await waitFor(() => expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("before-pass"));
  } finally { view?.unmount(); queryClient.clear(); transport.restore(); }
});

// This structural call signature only permits the reviewed optional authority
// argument; all returned evidence still comes from the actual snapshot hook.
const useScopedContainerSnapshot: (jobId: string, enabled: boolean, expectedProjectId?: string) => ReturnType<typeof useJobExecutionSnapshot> = useJobExecutionSnapshot;
function ParsedParentContainerHarness({ projectId, ownerEpoch }: Readonly<{ projectId: string; ownerEpoch: number }>) {
  const { snapshot } = useScopedContainerSnapshot("parent", true, projectId);
  return <ContainerHookHarness run={{ proof: { ...selectedProof, projectId, generation: ownerEpoch }, snapshot }}
    projectId={projectId} ownerEpoch={ownerEpoch} />;
}
it("uses real parsed parent and child evidence independently and retires both when Builder project authority changes", async () => {
  const transport = await containerTransport();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let view: ReturnType<typeof render> | undefined;
  try {
    view = render(<QueryClientProvider client={queryClient}><ParsedParentContainerHarness projectId="project-a" ownerEpoch={1} /></QueryClientProvider>);
    const marks = screen.getByTestId("container-hook-marks");
    await waitFor(() => expect(marks).toHaveTextContent('"group:batch"'));
    await waitFor(() => expect(marks).toHaveTextContent("group:hook:before:batch"));
    expect(marks).toHaveTextContent("parent-pass");
    expect(marks).toHaveTextContent("before-pass");
    view.rerender(<QueryClientProvider client={queryClient}><ParsedParentContainerHarness projectId="project-b" ownerEpoch={2} /></QueryClientProvider>);
    expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("{}");
    view.rerender(<QueryClientProvider client={queryClient}><ParsedParentContainerHarness projectId="project-a" ownerEpoch={3} /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("parent-pass"));
    await waitFor(() => expect(screen.getByTestId("container-hook-marks")).toHaveTextContent("before-pass"));
  } finally { view?.unmount(); queryClient.clear(); transport.restore(); }
});
