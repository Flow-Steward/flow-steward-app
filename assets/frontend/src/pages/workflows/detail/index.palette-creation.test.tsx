// Navigation unit cases, followed by real Hook-scope/local-controller readiness cases.
// Full page and native browser verification remain separate.
import { useState } from "react";
import { useWorkflowDrawerController } from "./detail-drawer-controller";
import { normalizeWorkflowStep } from "./detail-workflow-validation";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { queryKeys } from "@/lib/query/keys";
import * as workflowApi from "@/pages/workflows/shared/api";
import { workflowHooksQueryKey } from "@/pages/workflows/shared/hook-api";
import { useWorkflowHookScope } from "./detail-hook-scope";
import { useWorkflowChangeSet } from "./detail-change-set";
import { applyChangeOperations, definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import { useWorkflowHookNavigation } from "./detail-hook-navigation";
import type { CreateLocalStepRequest } from "@/pages/workflows/builder/canvas-types";

type CreationRequest = {
  requestId: string;
  choice: { nodeKind?: "step"; catalogKey: string; stepKind: string; displayName: string; toolCategory?: "connector" | "mcp_tools" | "extension_tools"; override?: Record<string, unknown> } | { nodeKind: "group" | "for_each"; catalogKey: string; displayName: string; containerDefaults?: { bodyKind?: "sequence" | "lanes" } };
  context: { groupId: string; forEachId: string; laneId: string; insert?: CreateLocalStepRequest["context"]["insert"] };
  expectedEdge?: CreateLocalStepRequest["expectedEdge"];
  intent?: CreateLocalStepRequest["intent"];
  signal?: AbortSignal;
  at?: CreateLocalStepRequest["at"];
};
type CreationResult = { status: string; nodeKind?: string; nodeId?: string; canvasNodeId?: string; stepId?: string; canvasStepId?: string };
type CreateCommand = (request: CreationRequest) => Promise<CreationResult>;
const request: CreationRequest = { requestId: "choice-1", choice: { catalogKey: "builtin:wait", stepKind: "wait", displayName: "Wait", override: { wait: { duration_ms: 60000 } } }, context: { groupId: "", forEachId: "", laneId: "" } };

function setup({ dirtyAllowed = true, protectedAuthor = false, pendingChild = false, child = false, needsProject = false }: { dirtyAllowed?: boolean; protectedAuthor?: boolean; pendingChild?: boolean; child?: boolean; needsProject?: boolean } = {}) {
  let preview = definitionFromWorkflow({ workflow_id: child ? "child" : "author", revision: 4, steps: [] });
  const staged: WorkflowChangeOperation[][] = [];
  const opened: unknown[][] = [];
  const owner = {
    workflowId: child ? "child" : "author", hookId: child ? "before" : "", revision: 4, generation: 1, viewEpoch: 0,
    focus: vi.fn(),
    get preview() { return preview; },
    stage: (operations: WorkflowChangeOperation | WorkflowChangeOperation[]) => {
      const batch = Array.isArray(operations) ? operations : [operations];
      staged.push(batch);
      preview = applyChangeOperations(preview, batch).definition;
    },
    stageGraphIntent: (operations: WorkflowChangeOperation[]) => {
      staged.push(operations);
      preview = applyChangeOperations(preview, operations).definition;
      return { controller: Symbol("creation"), serial: staged.length };
    },
    toCanvasId: (id: string) => child ? `hook:before:${id}` : id,
  };
  const frames = new Map(child || pendingChild ? [["before", { hookId: "before", stepId: "hook_step", targetWorkflowId: needsProject ? "" : "child", needsProject }], ["after", { hookId: "after", stepId: "after_hook", targetWorkflowId: "after_child" }]] : []);
  let failure = "";
  let ensureCount = 0;
  let confirmationCount = 0;
  let activationCount = 0;
  let acquisitionCount = 0;
  let ensure: () => Promise<string> = async () => "child";
  const hookScope = {
    authorWorkflowId: "author",
    frames, framesByHook: frames, effectiveHookId: child || pendingChild ? "before" : "",
    childChangeSet: { hasChanges: pendingChild, status: pendingChild ? "pending" : "clean" },
    childRows: { definitionRows: { groupRows: [] } },
    canActivateHook: (hookId: string) => !pendingChild || hookId === "before", activateHook: (hookId: string) => { activationCount += 1; return !pendingChild || hookId === "before"; },
    ensureHookWorkflow: async () => { ensureCount += 1; return ensure(); },
    creationFailure: () => failure,
    acquireCreationOwner: async () => { acquisitionCount += 1; return owner; },
    isCreationOwnerCurrent: (candidate: unknown) => candidate === owner,
  };
  const drawer = {
    openGroupDrawer: async () => true, openNewGroupDrawer: async () => true,
    openNewStepDrawer: async () => true, openStepDrawer: async () => true,
    openCreatedStep: (...args: unknown[]) => { opened.push(args); },
    openCreatedContainer: (...args: unknown[]) => { opened.push(args); },
  };
  const options = { allowHookDeclaration: false, hookScope, authorGroupRows: [], authorStructureLocked: protectedAuthor, drawerMode: "", selectedGroupId: "", selectedStepId: "", confirmDiscardParameters: async () => { confirmationCount += 1; return dirtyAllowed; }, searchParams: new URLSearchParams(), setSearchParams: () => undefined, setSaveError: () => undefined };
  const view = renderHook(() => useWorkflowHookNavigation(options as unknown as Parameters<typeof useWorkflowHookNavigation>[0]));
  const navigation = () => view.result.current.canvasNavigation(drawer as unknown as Parameters<typeof view.result.current.canvasNavigation>[0]) as unknown as { createStep?: CreateCommand; openStep: (id: string) => Promise<unknown> };
  const create = (next: CreationRequest = request) => navigation().createStep?.(next);
  return { options, view, navigation, create, owner, staged, opened, preview: () => preview, ensureCount: () => ensureCount, confirmationCount: () => confirmationCount, activationCount: () => activationCount, acquisitionCount: () => acquisitionCount, delayEnsure: (next: () => Promise<string>) => { ensure = next; }, replaceOwnerCheck: (next: (candidate: unknown) => boolean) => { hookScope.isCreationOwnerCurrent = next; }, failWith: (message: string) => { failure = message; } };
}


it("keeps a tool-category choice in the Hook child editor without writing that category into the child definition", async () => {
  const state = setup({ child: true });
  const choice = { catalogKey: "tool-category:extension_tools", stepKind: "connector", displayName: "Extension Tools", toolCategory: "extension_tools" as const, override: { connector: { operation_id: "", input_mapping: {}, connection_binding: {} } } };
  await act(async () => { await state.create({ ...request, choice, context: { groupId: "hook:before", forEachId: "", laneId: "" } }); });
  expect(state.staged).toHaveLength(1);
  const step = state.preview().steps[0];
  expect(step).toMatchObject({ step_kind: "connector", display_name: "Extension Tools" });
  expect(step).not.toHaveProperty("toolCategory");
  expect(state.opened).toEqual([[expect.objectContaining({ step_kind: "connector" }), `hook:before:${step.step_id}`, "extension_tools"]]);
});

it("creates the selected genuine container in one guarded batch and opens prepared container fields", async () => {
  const state = setup();
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...request, choice: { nodeKind: "group", catalogKey: "container:group", displayName: "Group" } }); });
  expect(result).toMatchObject({ status: "created", nodeKind: "group", nodeId: "group", canvasNodeId: "group:group" });
  expect(state.staged).toHaveLength(1);
  expect(state.preview().groups).toEqual([expect.objectContaining({ group_id: "group", body: { kind: "sequence" } })]);
  expect(state.opened).toEqual([[expect.objectContaining({ group_id: "group", body: { kind: "sequence" } }), "group", "group"]]);
});

it("dirty cancellation of a container choice stages nothing and never materializes a hook", async () => {
  const state = setup({ dirtyAllowed: false, child: true });
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...request, choice: { nodeKind: "for_each", catalogKey: "container:for_each", displayName: "For each" }, context: { groupId: "hook:before", forEachId: "", laneId: "" } }); });
  expect(result?.status).toBe("cancelled");
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
  expect(state.ensureCount()).toBe(0);
});

it("dirty cancellation returns cancelled before any local operation or materialization", async () => {
  const state = setup({ dirtyAllowed: false });
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create(); });
  expect(result).toEqual({ status: "cancelled" });
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
  expect(state.ensureCount()).toBe(0);
});

it("refuses protected author creation before local operations", async () => {
  const state = setup({ protectedAuthor: true });
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create(); });
  expect(result?.status).toBe("refused");
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
  expect(state.ensureCount()).toBe(0);
});

it("refuses a different hook while the current child has unapplied changes", async () => {
  const state = setup({ child: true, pendingChild: true });
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...request, context: { groupId: "hook:after", forEachId: "", laneId: "" } }); });
  expect(result?.status).toBe("refused");
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
  expect(state.ensureCount()).toBe(0);
});

it("creates in the authoritative author owner when the composite canvas also carries child drafts", async () => {
  const state = setup({ pendingChild: true });
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create(); });
  expect(result).toEqual({ status: "created", stepId: "wait", canvasStepId: "wait" });
  expect(state.owner.workflowId).toBe("author");
  expect(state.preview().steps[0]).toMatchObject({ step_id: "wait", step_kind: "wait" });
  expect(state.staged).toHaveLength(1);
});

it("stages the actual choice once and hands prepared fields to the editor before rows rerender", async () => {
  const state = setup();
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create(); });
  expect(result).toEqual({ status: "created", stepId: "wait", canvasStepId: "wait" });
  expect(state.staged).toHaveLength(1);
  expect(state.preview().steps).toEqual([expect.objectContaining({ step_id: "wait", step_kind: "wait", display_name: "Wait", override: { wait: { duration_ms: 60000 } } })]);
  expect(state.opened).toEqual([[expect.objectContaining({ step_id: "wait", step_kind: "wait", display_name: "Wait", override: { wait: { duration_ms: 60000 } } }), "wait"]]);
});

it("keeps a connector with missing connection and required inputs as an honest local draft", async () => {
  const state = setup();
  const next = { ...request, choice: { catalogKey: "catalog:mail:send", stepKind: "connector", displayName: "Send mail", override: { connector: { connector_id: "extension.fixture.mail", operation_id: "send", input_mapping: {}, connection_binding: {} } } } };
  await act(async () => { await state.create(next); });
  expect(state.staged).toHaveLength(1);
  expect(state.preview().steps[0]).toMatchObject({ step_kind: "connector", override: { connector: { operation_id: "send", input_mapping: {}, connection_binding: {} } } });
  expect(state.preview().steps[0].override).not.toHaveProperty("connector.connection_binding.connection_id");
});

it("cancels a delayed hook materialization after another navigation without stale local writes", async () => {
  const state = setup({ child: true });
  let resolve!: (id: string) => void;
  state.delayEnsure(() => new Promise((done) => { resolve = done; }));
  const hookRequest = { ...request, context: { groupId: "hook:before", forEachId: "", laneId: "" } };
  let pending: Promise<CreationResult> | undefined;
  await act(async () => { pending = state.create(hookRequest); await Promise.resolve(); });
  await act(async () => { await state.view.result.current.prepareSelection("step", "other"); resolve?.("child"); });
  expect((await pending)?.status).toBe("cancelled");
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
});

it("shows the real reason a hook could not take its first step, not a generic refusal (R2-10)", async () => {
  const state = setup({ child: true });
  state.delayEnsure(async () => { state.failWith("The hook could not take steps: Workflow 'order_flow' not found"); return ""; });
  const hookRequest = { ...request, context: { groupId: "hook:before", forEachId: "", laneId: "" } };
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create(hookRequest); });
  expect(result).toEqual({ status: "failed", message: "The hook could not take steps: Workflow 'order_flow' not found" });
  expect(state.staged).toEqual([]);
});

it("refuses a hook's Add step without a project before anything is prepared, saying why (R2-10)", async () => {
  const state = setup({ child: true, needsProject: true });
  const hookRequest = { ...request, context: { groupId: "hook:before", forEachId: "", laneId: "" } };
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create(hookRequest); });
  expect(result).toEqual({ status: "refused", message: "Choose a project in the header first: each project keeps its own steps in a hook." });
  expect(state.ensureCount()).toBe(0);
  expect(state.confirmationCount()).toBe(0);
  expect(state.staged).toEqual([]);
});

it("refuses contradictory projected owners before normalization can erase a virtual Hook root", async () => {
  const state = setup({ child: true });
  state.owner.stage({ op: "create_for_each", for_each: { for_each_id: "each", over: "state.items", item_name: "item", body: { kind: "sequence" } } });
  state.staged.length = 0;
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...request, context: { groupId: "hook:after", forEachId: "hook:before:each", laneId: "" } }); });
  expect(result?.status).toBe("refused");
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
  expect(state.ensureCount()).toBe(0);
});

it("uses the current child owner and deduplicates repeated choice delivery", async () => {
  const state = setup({ child: true });
  const hookRequest = { ...request, context: { groupId: "hook:before", forEachId: "", laneId: "" } };
  let results: Array<CreationResult | undefined> = [];
  await act(async () => { results = await Promise.all([state.create(hookRequest), state.create(hookRequest)]); });
  expect(results).toEqual([{ status: "created", stepId: "wait", canvasStepId: "hook:before:wait" }, { status: "created", stepId: "wait", canvasStepId: "hook:before:wait" }]);
  expect(state.staged).toHaveLength(1);
  expect(state.ensureCount()).toBe(1);
  expect(state.preview().steps[0]).toMatchObject({ step_id: "wait", group_id: null, for_each_id: null });
});

it("refuses a replaced readiness capability and cancels a closed request without writes", async () => {
  const state = setup();
  state.replaceOwnerCheck(() => false);
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create(); });
  expect(result?.status).toBe("cancelled");
  expect(state.staged).toEqual([]);
  const controller = new AbortController(); controller.abort();
  await act(async () => { result = await state.create({ ...request, requestId: "closed", signal: controller.signal }); });
  expect(result?.status).toBe("cancelled");
  expect(state.opened).toEqual([]);
});

it("unmount cancels a materialization continuation before it can stage into a captured owner", async () => {
  const state = setup({ child: true });
  let resolve!: (id: string) => void;
  state.delayEnsure(() => new Promise((done) => { resolve = done; }));
  let pending: Promise<CreationResult> | undefined;
  await act(async () => { pending = state.create({ ...request, context: { groupId: "hook:before", forEachId: "", laneId: "" } }); await Promise.resolve(); });
  state.view.unmount();
  resolve?.("child");
  expect((await pending)?.status).toBe("cancelled");
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
});


// Real hook-scope lifecycle and real local change-set controllers. Query fixtures
// supply definitions only; no Apply or successful persistence is simulated.
function setupReadyOwner({ twoHooks = false, cachedChild = false, sharedChild = false, rootLoop = false, protectedAuthor = false }: { twoHooks?: boolean; cachedChild?: boolean; sharedChild?: boolean; rootLoop?: boolean; protectedAuthor?: boolean } = {}) {
  const hookStep = (stepId: string, hookId: string) => ({ step_id: stepId, step_kind: "invoke_workflow", override: { invoke_workflow: { hook: { hook_id: hookId, display_name: hookId === "before" ? "Before" : "After" } } } });
  const authorWorkflow = { workflow_id: "author", revision: 4, protected_workflow: protectedAuthor, steps: [hookStep("hook_step", "before"), ...(twoHooks ? [hookStep("after_step", "after")] : [])] };
  const authorDefinition = definitionFromWorkflow(authorWorkflow);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, gcTime: Infinity } } });
  const hookRow = (hookId: string, stepId: string, target: string) => ({ hook_id: hookId, step_id: stepId, display_name: hookId === "before" ? "Before" : "After", binding_revision: 1, binding: { target_workflow_id: target } });
  const hookRows = [hookRow("before", "hook_step", "child"), ...(twoHooks ? [hookRow("after", "after_step", sharedChild ? "child" : "other_child")] : [])];
  const childGraph: workflowApi.WorkflowCanvasGraph = { workflow: { id: "child", display_name: "Child", enabled: false, callable_by_parent: false }, interface_contract: {}, output_contract: {}, entry: null, steps: [], groups: [], edges: [], validation_issues: [], automation_rules: [] };
  const childDetail = { workflow_id: "child", revision: 9, display_name: "Child", steps: [] } as workflowApi.WorkflowDetail;
  if (rootLoop) {
    const entry: workflowApi.WorkflowNodeRef = { kind: "for_each", id: "each" };
    const body = { kind: "sequence", entry: { kind: "step", id: "body" } };
    const edges: workflowApi.WorkflowGraphEdge[] = [
      { source: entry, outcome: { name: "success" }, target: { kind: "step", id: "hook_end" } },
      { source: { kind: "step", id: "body" }, outcome: { name: "success" }, target: { kind: "for_each_exit", for_each_id: "each", lane_id: null } },
    ];
    childDetail.steps = [
      { step_id: "body", step_kind: "wait", for_each_id: "each", override: { wait: { duration_ms: 60000 } } },
      { step_id: "hook_end", step_kind: "stop_success", group_id: null, for_each_id: null, lane_id: null },
    ];
    childDetail.for_each = [{ for_each_id: "each", display_name: "Each", over: "state.items", item_name: "item", body }];
    childDetail.settings = { execution_graph: { schema_version: 1, entry, edges } };
    childGraph.workflow.revision = 9;
    childGraph.entry = entry;
    childGraph.edges = edges;
    childGraph.for_each = [{ id: "each", for_each_id: "each", display_name: "Each", over: "state.items", item_name: "item", body }];
    childGraph.steps = [
      { id: "body", step_id: "body", kind: "wait", for_each_id: "each", display_name: "Body", config_summary: "" },
      { id: "hook_end", step_id: "hook_end", kind: "stop_success", display_name: "End hook", config_summary: "" },
    ];
  }
  const seedProject = (projectId: string) => {
    queryClient.setQueryData(workflowHooksQueryKey(projectId, "author"), { hooks: hookRows });
    queryClient.setQueryData(["workflows", "where-used", projectId, "child"], { consumers: [] });
    if (twoHooks && !sharedChild) queryClient.setQueryData(["workflows", "where-used", projectId, "other_child"], { consumers: [] });
  };
  seedProject("project");
  queryClient.setQueryData(queryKeys.workflowCanvasGraph("child"), childGraph);
  if (cachedChild) queryClient.setQueryData(queryKeys.workflowDetail("child"), childDetail);
  if (twoHooks && !sharedChild) {
    queryClient.setQueryData(queryKeys.workflowCanvasGraph("other_child"), { ...childGraph, workflow: { ...childGraph.workflow, id: "other_child" } });
    queryClient.setQueryData(queryKeys.workflowDetail("other_child"), { ...childDetail, workflow_id: "other_child" });
  }
  let resolveDetail!: (value: workflowApi.WorkflowDetail) => void;
  const detail = new Promise<workflowApi.WorkflowDetail>((resolve) => { resolveDetail = resolve; });
  const fetchDetail = vi.spyOn(workflowApi, "fetchWorkflowDetail").mockImplementation((id) => id === "child" ? detail : Promise.reject(new Error(`Unexpected definition request: ${id}`)));
  const noop = () => undefined;
  const configuration = { projectId: "project", focusCanvasNode: vi.fn() };
  const opened: unknown[][] = [];
  const drawer = { openGroupDrawer: async () => undefined, openNewGroupDrawer: async () => undefined, openNewStepDrawer: async () => undefined, openStepDrawer: async () => true, openCreatedStep: (...args: unknown[]) => { opened.push(args); }, openCreatedContainer: (...args: unknown[]) => { opened.push(args); } };
  const view = renderHook(() => {
    const author = useWorkflowChangeSet({ workflowId: "author", serverDefinition: authorDefinition, serverRevision: 4, refetchWorkflowSurfaces: async () => authorWorkflow });
    const scope = useWorkflowHookScope({ authorChangeSet: author, enabled: true, projectId: configuration.projectId, workflowId: "author", workflow: authorWorkflow, focusCanvasNode: configuration.focusCanvasNode, selectedStepId: "", selectedGroupId: "", drawerMode: "", groupCreateMode: false, stepCreateContext: null, structureLocked: false, openNewStepDrawerRef: { current: noop }, openStepDrawerRef: { current: noop }, openGroupDrawerRef: { current: noop }, requestConfirmation: async () => true, setSelectedGroupId: noop, setAutomationScopeKind: noop, setDrawerMode: noop, setDrawerTab: noop, setSaveError: noop, setSaveNotice: noop, setSelectedCanvasIssue: noop, setSelectedStepId: noop });
    const navigation = useWorkflowHookNavigation({ hookScope: scope, ownerKey: `author:${configuration.projectId}`, authorGroupRows: [], authorStructureLocked: protectedAuthor, drawerMode: "", selectedGroupId: "", selectedStepId: "", confirmDiscardParameters: async () => true, searchParams: new URLSearchParams(), setSearchParams: noop, setSaveError: noop });
    return { author, scope, navigation: navigation.canvasNavigation(drawer), prepareSelection: navigation.prepareSelection };
  }, { wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider> });
  const release = () => resolveDetail(childDetail);
  const acquire = async (hookId = "before", workflowId = "child") => {
    const owner = await view.result.current.scope.acquireCreationOwner(hookId, workflowId);
    if (!owner) throw new Error(`Expected a committed creation owner for ${hookId || "author"}/${workflowId}`);
    return owner;
  };
  const close = () => { view.unmount(); queryClient.clear(); fetchDetail.mockRestore(); };
  return { view, release, close, opened, fetchDetail, queryClient, configuration, seedProject, acquire };
}

// The frame exit is a projection of the child's hidden finishing step.
function rootLoopInsertion(state: ReturnType<typeof setupReadyOwner>): CreationRequest {
  const expectedEdge = state.view.result.current.scope.frames.get("hook_step")?.memberEdges.find((edge) => edge.source.kind === "for_each" && edge.source.id === "hook:before:each" && edge.outcome.name === "success");
  if (!expectedEdge) throw new Error("Expected the real projected For each Next connection");
  expect(expectedEdge.target).toEqual({ kind: "group_exit", group_id: "hook:before", lane_id: null });
  const context = { groupId: "hook:before", forEachId: "", laneId: "", insert: { source: expectedEdge.source, outcome: expectedEdge.outcome, label: "Next" } };
  return { requestId: "root-loop-invoke", choice: { catalogKey: "builtin:invoke_workflow", stepKind: "invoke_workflow", displayName: "Invoke workflow" }, context, expectedEdge, intent: { source: expectedEdge.source, outcome: expectedEdge.outcome, scope: { groupId: context.groupId, forEachId: "", laneId: "" } } };
}

it("inserts ordinary Invoke at the own Hook root exit through one real child journal action with Undo/Redo", async () => {
  const state = setupReadyOwner({ cachedChild: true, rootLoop: true, protectedAuthor: true, twoHooks: true });
  try {
    await waitFor(() => expect(state.view.result.current.scope.framesByHook.has("before")).toBe(true));
    act(() => { state.view.result.current.scope.activateHook("before"); });
    await waitFor(() => expect(state.view.result.current.scope.childRevision).toBe(9));
    const beforeChild = structuredClone(state.view.result.current.scope.childChangeSet.preview);
    const beforeAuthor = structuredClone(state.view.result.current.author.preview);
    const beforeForeign = structuredClone(state.queryClient.getQueryData(queryKeys.workflowDetail("other_child")));
    const beforeItems = state.view.result.current.scope.childChangeSet.journal.items.length;
    const owner = await state.acquire();
    const stage = vi.spyOn(owner, "stageGraphIntent");
    const next = rootLoopInsertion(state);
    let result: CreationResult | undefined;
    await act(async () => { result = await state.view.result.current.navigation.createStep(next); });
    expect(result).toEqual({ status: "created", stepId: "invoke_workflow", canvasStepId: "hook:before:invoke_workflow" });
    expect(stage).toHaveBeenCalledTimes(1);
    const child = state.view.result.current.scope.childChangeSet;
    expect(child.operations).toEqual([{ op: "insert_step_between", source: { kind: "for_each", id: "each" }, outcome: { name: "success" }, step: expect.objectContaining({ step_id: "invoke_workflow", step_kind: "invoke_workflow", group_id: null, for_each_id: null, lane_id: null }) }]);
    expect(child.journal.items).toHaveLength(beforeItems + 1);
    expect(child.preview.steps).toEqual([...beforeChild.steps, expect.objectContaining({ step_id: "invoke_workflow", step_kind: "invoke_workflow", group_id: null, for_each_id: null, lane_id: null })]);
    expect(child.preview.steps.at(-1)?.override).not.toHaveProperty("invoke_workflow.hook");
    expect(child.preview.graph).toEqual({ ...beforeChild.graph, edges: [
      { source: { kind: "for_each", id: "each" }, outcome: { name: "success" }, target: { kind: "step", id: "invoke_workflow" } },
      { source: { kind: "step", id: "invoke_workflow" }, outcome: { name: "success" }, target: { kind: "step", id: "hook_end" }, rework: false },
      beforeChild.graph.edges[1],
    ] });
    expect(child.preview.forEach).toEqual(beforeChild.forEach);
    expect(child.preview.groups).toEqual(beforeChild.groups);
    expect(state.opened).toEqual([[expect.objectContaining({ step_id: "invoke_workflow", step_kind: "invoke_workflow" }), "hook:before:invoke_workflow"]]);
    const afterChild = structuredClone(child.preview);
    const assertOtherOwnersUnchanged = () => {
      expect(state.view.result.current.author.operations).toEqual([]);
      expect(state.view.result.current.author.journal.items).toEqual([]);
      expect(state.view.result.current.author.preview).toEqual(beforeAuthor);
      expect(state.queryClient.getQueryData(queryKeys.workflowDetail("other_child"))).toEqual(beforeForeign);
    };
    assertOtherOwnersUnchanged();
    act(() => state.view.result.current.scope.childChangeSet.undoGraphIntent());
    expect(state.view.result.current.scope.childChangeSet.preview).toEqual(beforeChild);
    expect(state.view.result.current.scope.childChangeSet.operations).toEqual([]);
    expect(state.view.result.current.scope.childChangeSet.canUndo).toBe(false);
    assertOtherOwnersUnchanged();
    act(() => state.view.result.current.scope.childChangeSet.redoGraphIntent());
    expect(state.view.result.current.scope.childChangeSet.preview).toEqual(afterChild);
    expect(state.view.result.current.scope.childChangeSet.journal.items).toHaveLength(beforeItems + 1);
    assertOtherOwnersUnchanged();
  } finally { state.close(); }
});

it("retains actionable expected-connection freshness refusal for an own Hook root exit", async () => {
  const state = setupReadyOwner({ cachedChild: true, rootLoop: true, protectedAuthor: true });
  try {
    await waitFor(() => expect(state.view.result.current.scope.childRevision).toBe(9));
    const before = structuredClone(state.view.result.current.scope.childChangeSet.preview);
    const next = rootLoopInsertion(state);
    const stale = { ...next, expectedEdge: { ...next.expectedEdge!, rework: true } };
    let result: CreationResult | undefined;
    await act(async () => { result = await state.view.result.current.navigation.createStep(stale); });
    expect(result).toEqual({ status: "failed", message: "The connection changed. Reopen Add step." });
    expect(state.view.result.current.scope.childChangeSet.preview).toEqual(before);
    expect(state.view.result.current.scope.childChangeSet.operations).toEqual([]);
    expect(state.view.result.current.scope.childChangeSet.journal.items).toEqual([]);
    expect(state.view.result.current.author.operations).toEqual([]);
    expect(state.opened).toEqual([]);
  } finally { state.close(); }
});

it("waits for the real current child controller before staging the selected step into that owner", async () => {
  const state = setupReadyOwner();
  try {
    await waitFor(() => expect(state.view.result.current.scope.framesByHook.has("before")).toBe(true));
    expect(state.view.result.current.scope.framesByHook.get("before")).toMatchObject({ stepId: "hook_step", connected: true, targetWorkflowId: "child", bindingRevision: 1 });
    expect(state.view.result.current.scope.childWorkflowId).toBe("child");
    expect(state.view.result.current.scope.childRevision).toBeNull();
    let pending: Promise<unknown> | undefined;
    await act(async () => { pending = state.view.result.current.navigation.createStep({ ...request, context: { groupId: "hook:before", forEachId: "", laneId: "" } }); await Promise.resolve(); });
    expect(state.view.result.current.author.operations).toEqual([]);
    expect(state.view.result.current.scope.childChangeSet.operations).toEqual([]);
    expect(state.opened).toEqual([]);
    await act(async () => { state.release(); await Promise.resolve(); });
    await waitFor(() => expect(state.view.result.current.scope.childChangeSet.operations).toHaveLength(1));
    expect(await pending).toEqual({ status: "created", stepId: "wait", canvasStepId: "hook:before:wait" });
    expect(state.view.result.current.author.operations).toEqual([]);
    expect(state.view.result.current.scope.childChangeSet.preview.steps).toEqual([expect.objectContaining({ step_id: "wait", step_kind: "wait", group_id: null })]);
    expect(state.view.result.current.scope.childRevision).toBe(9);
    expect(state.opened).toEqual([[expect.objectContaining({ step_id: "wait", step_kind: "wait" }), "hook:before:wait"]]);
    // The real presentation composite now carries a child draft. A later author
    // choice still stages only through the current author capability.
    await act(async () => { await state.view.result.current.navigation.createStep({ ...request, requestId: "author-choice", choice: { catalogKey: "builtin:wait", stepKind: "wait", displayName: "Author wait" } }); });
    expect(state.view.result.current.author.preview.steps).toEqual(expect.arrayContaining([expect.objectContaining({ step_id: "author_wait", step_kind: "wait" })]));
    expect(state.view.result.current.scope.childChangeSet.preview.steps).toHaveLength(1);
    expect(state.view.result.current.scope.childChangeSet.preview.steps[0].step_id).toBe("wait");
  } finally { state.close(); }
});

it("real readiness waiters cancel on navigation and unmount without staging through a stale capability", async () => {
  const state = setupReadyOwner();
  let pending: Promise<unknown> | undefined;
  try {
    await waitFor(() => expect(state.view.result.current.scope.framesByHook.has("before")).toBe(true));
    await act(async () => { pending = state.view.result.current.navigation.createStep({ ...request, context: { groupId: "hook:before", forEachId: "", laneId: "" } }); await Promise.resolve(); });
    await act(async () => { await state.view.result.current.prepareSelection("step", "author_step"); state.release(); });
    expect(await pending).toEqual({ status: "cancelled" });
    expect(state.view.result.current.author.operations).toEqual([]);
    expect(state.view.result.current.scope.childChangeSet.operations).toEqual([]);
    expect(state.opened).toEqual([]);
    const abort = new AbortController();
    let owner: Promise<unknown> | undefined;
    await act(async () => { owner = state.view.result.current.scope.acquireCreationOwner("before", "not-current-child", abort.signal); });
    state.view.unmount();
    expect(await owner).toBeNull();
  } finally { state.close(); }
});


it.each(["author", "child"])("retains the exact committed %s capability across an unrelated render and stages once", async (subject) => {
  const state = setupReadyOwner({ cachedChild: true });
  try {
    await waitFor(() => expect(state.view.result.current.scope.childRevision).toBe(9));
    const hookId = subject === "child" ? "before" : "";
    const workflowId = subject === "child" ? "child" : "author";
    const owner = await state.acquire(hookId, workflowId);
    expect(state.view.result.current.scope.isCreationOwnerCurrent(owner)).toBe(true);
    state.view.rerender();
    expect(state.view.result.current.scope.isCreationOwnerCurrent(owner)).toBe(true);
    expect(await state.acquire(hookId, workflowId)).toBe(owner);
    let result: unknown;
    await act(async () => { result = await state.view.result.current.navigation.createStep({ ...request, context: { groupId: hookId ? "hook:before" : "", forEachId: "", laneId: "" } }); });
    expect(result).toMatchObject({ status: "created", stepId: "wait" });
    const current = subject === "child" ? state.view.result.current.scope.childChangeSet : state.view.result.current.author;
    const other = subject === "child" ? state.view.result.current.author : state.view.result.current.scope.childChangeSet;
    expect(current.operations).toHaveLength(1);
    expect(current.operations[0]).toMatchObject({ op: "create_step", step: { step_id: "wait", step_kind: "wait" } });
    expect(other.operations).toEqual([]);
    expect(state.opened).toHaveLength(1);
  } finally { state.close(); }
});

it("creates through the aligned real child controller on the first cached two-Hook choice", async () => {
  const state = setupReadyOwner({ twoHooks: true, cachedChild: true });
  try {
    await waitFor(() => expect(state.view.result.current.scope.framesByHook.size).toBe(2));
    expect(state.view.result.current.scope.effectiveHookId).toBe("");
    expect(state.view.result.current.scope.childChangeSet.workflowId).toBe("author");
    let pending: Promise<unknown> | undefined;
    await act(async () => { pending = state.view.result.current.navigation.createStep({ ...request, context: { groupId: "hook:before", forEachId: "", laneId: "" } }); await Promise.resolve(); });
    await waitFor(() => expect(state.view.result.current.scope.childChangeSet.operations).toHaveLength(1));
    const result = await pending;
    expect(result).toEqual({ status: "created", stepId: "wait", canvasStepId: "hook:before:wait" });
    expect(state.view.result.current.author.operations).toEqual([]);
    expect(state.view.result.current.scope.childRevision).toBe(9);
    expect(state.view.result.current.scope.childChangeSet.operations).toEqual([expect.objectContaining({ op: "create_step", step: expect.objectContaining({ step_id: "wait", step_kind: "wait" }) })]);
    expect(state.opened).toEqual([[expect.objectContaining({ step_id: "wait", step_kind: "wait" }), "hook:before:wait"]]);
  } finally { state.close(); }
});

it.each(["preview", "revision"])("retires the real child capability when its %s changes", async (boundary) => {
  const state = setupReadyOwner({ cachedChild: true });
  try {
    await waitFor(() => expect(state.view.result.current.scope.childRevision).toBe(9));
    const owner = await state.acquire();
    await act(async () => {
      if (boundary === "preview") state.view.result.current.scope.childChangeSet.stage({ op: "create_step", step: { step_id: "local_wait", step_kind: "wait", override: { wait: { duration_ms: 60000 } } } });
      else state.queryClient.setQueryData(queryKeys.workflowDetail("child"), { workflow_id: "child", revision: 10, display_name: "Child", steps: [] });
    });
    if (boundary === "revision") await waitFor(() => expect(state.view.result.current.scope.childRevision).toBe(10));
    expect(state.view.result.current.scope.isCreationOwnerCurrent(owner)).toBe(false);
    const current = await state.acquire();
    expect(current).not.toBe(owner);
    expect(current.generation).toBeGreaterThan(owner.generation);
    expect(current.preview).toBe(state.view.result.current.scope.childChangeSet.preview);
    expect(current.revision).toBe(boundary === "revision" ? 10 : 9);
    expect(state.view.result.current.author.operations).toEqual([]);
    expect(state.opened).toEqual([]);
  } finally { state.close(); }
});

it("retires the previous Hook capability through a real same-child before-after-before transition", async () => {
  const state = setupReadyOwner({ twoHooks: true, cachedChild: true, sharedChild: true });
  try {
    await waitFor(() => expect(state.view.result.current.scope.framesByHook.size).toBe(2));
    await act(async () => { expect(state.view.result.current.scope.activateHook("before")).toBe(true); });
    const owner = await state.acquire();
    await act(async () => { expect(state.view.result.current.scope.activateHook("after")).toBe(true); });
    expect(state.view.result.current.scope.isCreationOwnerCurrent(owner)).toBe(false);
    await act(async () => { expect(state.view.result.current.scope.activateHook("before")).toBe(true); });
    expect(state.view.result.current.scope.isCreationOwnerCurrent(owner)).toBe(false);
    const current = await state.acquire();
    expect(current).not.toBe(owner);
    expect(current.generation).toBeGreaterThan(owner.generation);
    expect(state.view.result.current.author.operations).toEqual([]);
    expect(state.view.result.current.scope.childChangeSet.operations).toEqual([]);
    expect(state.opened).toEqual([]);
  } finally { state.close(); }
});

it("does not revive the exact preview and stage capability after project A-B-A", async () => {
  const state = setupReadyOwner({ cachedChild: true });
  try {
    await waitFor(() => expect(state.view.result.current.scope.childRevision).toBe(9));
    const owner = await state.acquire();
    state.seedProject("other-project");
    state.configuration.projectId = "other-project";
    state.view.rerender();
    expect(state.view.result.current.scope.isCreationOwnerCurrent(owner)).toBe(false);
    state.configuration.projectId = "project";
    state.view.rerender();
    expect(state.view.result.current.scope.isCreationOwnerCurrent(owner)).toBe(false);
    const current = await state.acquire();
    expect(current).not.toBe(owner);
    expect(current.generation).toBeGreaterThan(owner.generation);
    expect(current.preview).toBe(owner.preview);
    expect(current.stage).toBe(owner.stage);
    expect(state.view.result.current.author.operations).toEqual([]);
    expect(state.view.result.current.scope.childChangeSet.operations).toEqual([]);
    expect(state.opened).toEqual([]);
  } finally { state.close(); }
});


const childRootContext: CreationRequest["context"] = { groupId: "hook:before", forEachId: "", laneId: "" };
const childSource: workflowApi.WorkflowNodeRef = { kind: "step", id: "hook:before:source" };
const childInsert: NonNullable<CreationRequest["context"]["insert"]> = { source: childSource, outcome: { name: "success" }, label: "Next" };
const childEdge: workflowApi.WorkflowGraphEdge = { source: childSource, outcome: { name: "success" }, target: { kind: "step", id: "hook:before:target" } };
const scopedInsertContext = { ...childRootContext, insert: childInsert };
// Hostile payloads may carry an exit where the typed canvas only permits a node.
const rootExitAsSource = { kind: "group_exit", group_id: "hook:before", lane_id: null } as unknown as workflowApi.WorkflowNodeRef;

const invalidCreationScopes: Array<{ name: string; context: CreationRequest["context"]; expectedEdge?: CreationRequest["expectedEdge"]; intent?: CreationRequest["intent"] }> = [
  { name: "actual Group and For each owners in one Hook", context: { groupId: "hook:before:group", forEachId: "hook:before:each", laneId: "" } },
  { name: "author Group and For each owners", context: { groupId: "group", forEachId: "each", laneId: "" } },
  { name: "Hook root paired with an author For each", context: { groupId: "hook:before", forEachId: "each", laneId: "" } },
  { name: "a virtual Hook root used as a For each", context: { groupId: "", forEachId: "hook:before", laneId: "" } },
  { name: "author placement with a projected insert source", context: { groupId: "group", forEachId: "", laneId: "", insert: childInsert } },
  { name: "child placement with an author insert source", context: { ...childRootContext, insert: { ...childInsert, source: { kind: "step", id: "source" } } } },
  { name: "child placement with another Hook insert source", context: { ...childRootContext, insert: { ...childInsert, source: { kind: "step", id: "hook:after:source" } } } },
  { name: "another Hook expected-edge source", context: scopedInsertContext, expectedEdge: { ...childEdge, source: { kind: "step", id: "hook:after:source" } } },
  { name: "an author expected-edge source in a child", context: scopedInsertContext, expectedEdge: { ...childEdge, source: { kind: "step", id: "source" } } },
  { name: "an author expected-edge target in a child", context: scopedInsertContext, expectedEdge: { ...childEdge, target: { kind: "step", id: "target" } } },
  { name: "another Hook root exit", context: scopedInsertContext, expectedEdge: { ...childEdge, target: { kind: "group_exit", group_id: "hook:after", lane_id: null } } },
  { name: "an own Hook root exit with a lane", context: scopedInsertContext, expectedEdge: { ...childEdge, target: { kind: "group_exit", group_id: "hook:before", lane_id: "lane" } } },
  { name: "a noncanonical own Hook root exit", context: scopedInsertContext, expectedEdge: { ...childEdge, target: { kind: "group_exit", group_id: "group:hook:before", lane_id: null } } },
  { name: "a bare Hook frame For each exit", context: scopedInsertContext, expectedEdge: { ...childEdge, target: { kind: "for_each_exit", for_each_id: "hook:before", lane_id: null } } },
  { name: "an own Hook root exit used as an insertion source", context: { ...childRootContext, insert: { ...childInsert, source: rootExitAsSource } } },
  { name: "an own Hook root exit used as an expected-edge source", context: scopedInsertContext, expectedEdge: { ...childEdge, source: rootExitAsSource } },
  { name: "an own Hook root exit used as an intent source", context: scopedInsertContext, intent: { source: rootExitAsSource, outcome: { name: "success" }, scope: childRootContext } },
  { name: "a bare Hook frame used as a source", context: { ...childRootContext, insert: { ...childInsert, source: { kind: "for_each", id: "hook:before" } } } },
  { name: "a malformed Hook frame source", context: { ...childRootContext, insert: { ...childInsert, source: { kind: "for_each", id: "hook:" } } } },
  { name: "another Hook expected Group exit", context: scopedInsertContext, expectedEdge: { ...childEdge, target: { kind: "group_exit", group_id: "hook:after:group", lane_id: null } } },
  { name: "another Hook expected For each exit", context: scopedInsertContext, expectedEdge: { ...childEdge, target: { kind: "for_each_exit", for_each_id: "hook:after:each", lane_id: null } } },
  { name: "an author expected Group exit in a child", context: scopedInsertContext, expectedEdge: { ...childEdge, target: { kind: "group_exit", group_id: "group", lane_id: null } } },
  { name: "an author expected For each exit in a child", context: scopedInsertContext, expectedEdge: { ...childEdge, target: { kind: "for_each_exit", for_each_id: "each", lane_id: null } } },
  { name: "an expected edge without an insertion", context: childRootContext, expectedEdge: childEdge },
  { name: "another Hook in supplied intent scope", context: scopedInsertContext, intent: { source: childSource, outcome: { name: "success" }, scope: { groupId: "hook:after", forEachId: "", laneId: "" } } },
  { name: "another Hook in supplied intent source", context: scopedInsertContext, intent: { source: { kind: "step", id: "hook:after:source" }, outcome: { name: "success" }, scope: childRootContext } },
];

it.each(invalidCreationScopes)("refuses $name before confirmation, activation, materialization or writes", async ({ context, expectedEdge, intent }) => {
  const state = setup({ child: true });
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...request, context, expectedEdge, intent }); });
  expect(result?.status).toBe("refused");
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
  expect(state.confirmationCount()).toBe(0);
  expect(state.activationCount()).toBe(0);
  expect(state.ensureCount()).toBe(0);
  expect(state.acquisitionCount()).toBe(0);
});

it.each([
  { name: "a direct projected For each", context: { groupId: "", forEachId: "hook:before:each", laneId: "" }, groupId: null, forEachId: "each" },
  { name: "a projected Group", context: { groupId: "hook:before:group", forEachId: "", laneId: "" }, groupId: "group", forEachId: null },
  { name: "a For each with the same visual Hook root", context: { groupId: "hook:before", forEachId: "hook:before:each", laneId: "" }, groupId: null, forEachId: "each" },
])("preserves $name after validating every projected owner", async ({ context, groupId, forEachId }) => {
  const state = setup({ child: true });
  state.owner.stage([
    { op: "create_group", group: { group_id: "group", body: { kind: "sequence" } } },
    { op: "create_for_each", for_each: { for_each_id: "each", over: "state.items", item_name: "item", body: { kind: "sequence" } } },
  ]);
  state.staged.length = 0;
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...request, context }); });
  expect(result).toEqual({ status: "created", stepId: "wait", canvasStepId: "hook:before:wait" });
  expect(state.preview().steps[0]).toMatchObject({ group_id: groupId, for_each_id: forEachId });
  expect(state.staged).toHaveLength(1);
  expect(state.ensureCount()).toBe(1);
  expect(state.opened).toEqual([[expect.objectContaining({ step_id: "wait", group_id: groupId, for_each_id: forEachId }), "hook:before:wait"]]);
});

it.each([false, true])("keeps outer Hook frames in the author graph for insertion=%s", async (occupied) => {
  const state = setup({ pendingChild: true });
  const edge: workflowApi.WorkflowGraphEdge = { source: { kind: "step", id: "hook_step" }, outcome: { name: "success" }, target: { kind: "step", id: "after_hook" } };
  state.owner.stage([
    { op: "create_step", step: { step_id: "hook_step", step_kind: "invoke_workflow", override: { invoke_workflow: { hook: { hook_id: "before", display_name: "Before" } } } } },
    { op: "create_step", step: { step_id: "after_hook", step_kind: "invoke_workflow", override: { invoke_workflow: { hook: { hook_id: "after", display_name: "After" } } } } },
    ...(occupied ? [{ op: "connect" as const, edge }] : []),
  ]);
  state.staged.length = 0;
  const source: workflowApi.WorkflowNodeRef = { kind: "group", id: "hook:before" };
  const projectedEdge: workflowApi.WorkflowGraphEdge = { ...edge, source, target: { kind: "group", id: "hook:after" } };
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...request, context: { ...request.context, insert: { source, outcome: edge.outcome, label: "Next", append: !occupied } }, expectedEdge: occupied ? projectedEdge : undefined }); });
  expect(result).toEqual({ status: "created", stepId: "wait", canvasStepId: "wait" });
  expect(state.staged).toHaveLength(1);
  expect(state.ensureCount()).toBe(0);
  expect(state.preview().graph.edges).toEqual(expect.arrayContaining([{ source: edge.source, outcome: edge.outcome, target: { kind: "step", id: "wait" } }]));
  if (occupied) expect(state.preview().graph.edges).toEqual(expect.arrayContaining([{ source: { kind: "step", id: "wait" }, outcome: edge.outcome, target: edge.target, rework: false }]));
});

it.each(["group", "for_each"] as const)("preserves a same-Hook expected %s exit during ordinary Next insertion", async (kind) => {
  const state = setup({ child: true });
  const placement = kind === "group" ? { group_id: "scope" } : { for_each_id: "scope" };
  const target: workflowApi.WorkflowTargetRef = kind === "group" ? { kind: "group_exit", group_id: "scope", lane_id: null } : { kind: "for_each_exit", for_each_id: "scope", lane_id: null };
  const container: WorkflowChangeOperation = kind === "group" ? { op: "create_group", group: { group_id: "scope", body: { kind: "sequence" } } } : { op: "create_for_each", for_each: { for_each_id: "scope", over: "state.items", item_name: "item", body: { kind: "sequence" } } };
  state.owner.stage([container, { op: "create_step", step: { step_id: "source", step_kind: "wait", ...placement } }, { op: "connect", edge: { source: { kind: "step", id: "source" }, outcome: { name: "success" }, target } }]);
  state.staged.length = 0;
  const context = { groupId: kind === "group" ? "hook:before:scope" : "", forEachId: kind === "for_each" ? "hook:before:scope" : "", laneId: "", insert: childInsert };
  const projectedTarget: workflowApi.WorkflowTargetRef = kind === "group" ? { kind: "group_exit", group_id: "hook:before:scope", lane_id: null } : { kind: "for_each_exit", for_each_id: "hook:before:scope", lane_id: null };
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...request, context, expectedEdge: { ...childEdge, target: projectedTarget } }); });
  expect(result?.status).toBe("created");
  expect(state.staged).toHaveLength(1);
  expect(state.ensureCount()).toBe(1);
  expect(state.preview().graph.edges).toEqual(expect.arrayContaining([{ source: { kind: "step", id: "wait" }, outcome: { name: "success" }, target, rework: false }]));
});


// DEVHOOK03: crafted choices reach the real command, not merely the palette.
const declarationRequest: CreationRequest = { ...request, choice: { catalogKey: "hook:declaration", stepKind: "invoke_workflow", displayName: "Hook", override: { invoke_workflow: { hook: { hook_id: "before", contract_version: 1, display_name: "Before", input_schema: { type: "object", properties: {} }, input_mapping: {}, stage_contract: { summary: "Before completion" } } } } } };

it("refuses a crafted new declaration under default Production before any owner transition", async () => {
  const state = setup();
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create(declarationRequest); });
  expect(result?.status).toBe("refused");
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
  expect(state.confirmationCount()).toBe(0);
  expect(state.acquisitionCount()).toBe(0);
});

it.each([null, false, {}, []])("refuses incomplete raw hook presence %j in Production before owner transition", async (hook) => {
  const state = setup();
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...request, choice: { catalogKey: "crafted", stepKind: "invoke_workflow", displayName: "Incomplete", override: { invoke_workflow: { hook } } } }); });
  expect(result?.status).toBe("refused");
  expect(state.confirmationCount()).toBe(0);
  expect(state.ensureCount()).toBe(0);
  expect(state.staged).toEqual([]);
});

it("refuses a crafted nested declaration before creating a child workflow", async () => {
  const state = setup({ child: true });
  state.options.allowHookDeclaration = true; state.view.rerender();
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...declarationRequest, context: childRootContext }); });
  expect(result?.status).toBe("refused");
  expect(state.ensureCount()).toBe(0);
  expect(state.activationCount()).toBe(0);
  expect(state.staged).toEqual([]);
});

it("allows one Development root declaration through the existing owner and create-step batch", async () => {
  const state = setup();
  state.options.allowHookDeclaration = true; state.view.rerender();
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create(declarationRequest); });
  expect(result?.status).toBe("created");
  expect(state.staged).toHaveLength(1);
  expect(state.preview().groups).toEqual([]);
  expect(state.preview().steps[0]).toMatchObject({ step_kind: "invoke_workflow", override: "override" in declarationRequest.choice ? declarationRequest.choice.override : undefined });
  expect(state.ensureCount()).toBe(0);
});

it("rechecks declaration eligibility after an asynchronous dirty confirmation", async () => {
  const state = setup();
  state.options.allowHookDeclaration = true; state.view.rerender();
  let release!: (value: boolean) => void;
  state.options.confirmDiscardParameters = () => new Promise<boolean>((resolve) => { release = resolve; });
  state.view.rerender();
  let pending: Promise<CreationResult> | undefined;
  await act(async () => { pending = state.create(declarationRequest); await Promise.resolve(); });
  expect(release).toEqual(expect.any(Function));
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
  state.options.allowHookDeclaration = false;
  state.view.rerender();
  await act(async () => { release(true); });
  expect((await pending)?.status).toBe("refused");
  expect(state.staged).toEqual([]);
  expect(state.opened).toEqual([]);
});

it("projects an unapplied author declaration from the real author change set", async () => {
  const state = setupReadyOwner();
  try {
    await waitFor(() => expect(state.view.result.current.scope.framesByHook.has("before")).toBe(true));
    const operation: WorkflowChangeOperation = { op: "create_step", step: { step_id: "new_zone", step_kind: "invoke_workflow", override: { invoke_workflow: { hook: { hook_id: "after", display_name: "After review" } } } } };
    act(() => state.view.result.current.author.stage(operation));
    await waitFor(() => expect(state.view.result.current.scope.framesByHook.get("after")).toMatchObject({ hookId: "after", stepId: "new_zone", displayName: "After review", targetWorkflowId: "" }));
    expect(state.view.result.current.scope.childChangeSet.operations).toEqual([]);
    act(() => state.view.result.current.author.discard());
    await waitFor(() => expect(state.view.result.current.scope.framesByHook.has("after")).toBe(false));
    expect(state.view.result.current.scope.framesByHook.has("before")).toBe(true);
  } finally { state.close(); }
});

// Real journal ownership, real drawer state; only transport is replaced.
function localToolDraftFixture() {
  const transport = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ valid: true, issues: [] }), { status: 200 }));
  const initial = { workflow: "author", project: "project-a", hook: "", child: "author" };
  const definition = definitionFromWorkflow({ workflow_id: "author", revision: 4, steps: [] });
  const step = { step_id: "tools", step_kind: "connector", display_name: "MCP Tools", connector_id: "", connector_install_id: "", override: { connector: { operation_id: "" } } };
  const refetch = async () => undefined;
  const view = renderHook((owner: typeof initial) => {
    const changeSet = useWorkflowChangeSet({ workflowId: owner.child, serverRevision: 4, serverDefinition: definition, placementViewKey: owner.hook, refetchWorkflowSurfaces: refetch });
    const [state, setState] = useState({ selected: "", category: "", mode: "", tab: "smart-setup" });
    const canvasId = (id: string) => owner.hook ? `hook:${owner.hook}:${id}` : id;
    const rows = new Map(changeSet.preview.steps.map((item, index) => [canvasId(String(item.step_id || "")), normalizeWorkflowStep(item, index, changeSet.operations.some((op) => (op.op === "create_step" || op.op === "insert_step_between") && op.step.step_id === item.step_id))]));
    const drawer = useWorkflowDrawerController({
      localToolDraftOwnerKey: JSON.stringify([owner.workflow, owner.project]), stepToolCategoryDraft: state.category,
      captureLocalToolDraftOwner: () => changeSet.captureStagedStepNoticeOwner("tools"),
      definitionRows: { stepRowsById: rows, groupRows: [] }, selectedStepId: state.selected, selectedGroupId: "",
      drawerMode: state.mode, drawerTab: state.tab, groupDraft: {}, searchParams: new URLSearchParams(),
      confirmDiscardInvalidInput: async () => true, confirmDiscardParameters: async () => true,
      setSelectedStepId: (selected: string) => setState((current) => ({ ...current, selected })),
      setStepToolCategoryDraft: (category: string) => setState((current) => ({ ...current, category })),
      setDrawerMode: (mode: string) => setState((current) => ({ ...current, mode })),
      setDrawerTab: (tab: string) => setState((current) => ({ ...current, tab })),
      setStepDisplayName: () => undefined, setStepKindDraft: () => undefined, setStepOverrideDraft: () => undefined,
      setStepAgentId: () => undefined, setStepOptional: () => undefined, setStepBlockKey: () => undefined,
      setGroupCreateMode: () => undefined, setStepCreateContext: () => undefined, setSelectedGroupId: () => undefined,
      setAutomationScopeKind: () => undefined, setReactionFormVisible: () => undefined, setSelectedCanvasIssue: () => undefined,
    });
    return { changeSet, drawer, state, canvasId, clearSelection: () => setState((current) => ({ ...current, selected: "", category: "" })) };
  }, { initialProps: initial });
  const create = async (category?: "mcp_tools" | "extension_tools") => {
    await act(async () => {
      view.result.current.changeSet.stage({ op: "create_step", step });
      if (category) view.result.current.drawer.openCreatedStep(step, view.result.current.canvasId("tools"), category);
    });
  };
  const reopen = async () => { await act(async () => { await view.result.current.drawer.openStepDrawer(view.result.current.canvasId("tools")); }); };
  return { view, initial, create, reopen, transport };
}

it("keeps the unconfigured category across ordinary local parameter edits and another selection", async () => {
  const fixture = localToolDraftFixture();
  try {
    await fixture.create("mcp_tools");
    await act(async () => { fixture.view.result.current.changeSet.updateStagedStep("tools", { display_name: "Renamed unfinished tool" }); fixture.view.result.current.clearSelection(); });
    await fixture.reopen();
    expect(fixture.view.result.current.state.category).toBe("mcp_tools");
    expect(fixture.view.result.current.changeSet.preview.steps[0]).toMatchObject({ step_kind: "connector", display_name: "Renamed unfinished tool", connector_id: "" });
    expect(fixture.view.result.current.changeSet.preview.steps[0]).not.toHaveProperty("toolCategory");
  } finally { fixture.view.unmount(); fixture.transport.mockRestore(); }
});

it.each(["discard", "removed", "workflow-aba", "project-aba", "hook-aba"])("retires the category on %s without lending it to the returned local identity", async (boundary) => {
  const fixture = localToolDraftFixture();
  try {
    await fixture.create("extension_tools");
    if (boundary === "discard" || boundary === "removed") {
      await act(async () => { if (boundary === "discard") fixture.view.result.current.changeSet.discard(); else fixture.view.result.current.changeSet.dropStagedStep("tools"); });
    } else {
      const changed = boundary === "workflow-aba" ? { workflow: "other", child: "other" } : boundary === "project-aba" ? { project: "other-project" } : { hook: "other-hook" };
      fixture.view.rerender({ ...fixture.initial, ...changed });
      fixture.view.rerender(fixture.initial);
    }
    await act(async () => { fixture.view.result.current.clearSelection(); });
    if (["discard", "removed", "workflow-aba"].includes(boundary)) await fixture.create();
    await fixture.reopen();
    expect(fixture.view.result.current.state.category).toBe("");
    expect(fixture.view.result.current.changeSet.preview.steps[0]).toMatchObject({ step_id: "tools", step_kind: "connector", connector_id: "" });
  } finally { fixture.view.unmount(); fixture.transport.mockRestore(); }
});

it("lets a configured runtime connector identity retire the empty creation hint", async () => {
  const fixture = localToolDraftFixture();
  try {
    await fixture.create("extension_tools");
    await act(async () => { fixture.view.result.current.changeSet.updateStagedStep("tools", { connector_id: "connector.mcp.actual" }); fixture.view.result.current.clearSelection(); });
    await fixture.reopen();
    expect(fixture.view.result.current.state.category).toBe("");
    expect(fixture.view.result.current.changeSet.preview.steps[0].connector_id).toBe("connector.mcp.actual");
  } finally { fixture.view.unmount(); fixture.transport.mockRestore(); }
});


it("passes the exact root at-pointer point to the confirmed creation focus", async () => {
  const state = setup();
  const at = { point: { x: 608, y: 576 }, clientPoint: { x: 300, y: 400 } };
  let result: CreationResult | undefined;
  await act(async () => { result = await state.create({ ...request, at }); });
  expect(result).toEqual({ status: "created", stepId: "wait", canvasStepId: "wait" });
  expect(state.owner.focus).toHaveBeenCalledWith("wait", { place: true, creation: {
    target: { kind: "step", id: "wait" }, token: expect.objectContaining({ serial: 1 }),
    ownerWorkflowId: "author", viewKey: "", viewEpoch: 0,
    intent: undefined, point: at.point, selectionGeneration: undefined,
  } });
});
