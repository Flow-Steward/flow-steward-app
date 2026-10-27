import { act, renderHook } from "@testing-library/react";
import { startTransition, type ReactNode } from "react";
import { flushSync } from "react-dom";
import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { definitionFromWorkflow } from "../builder/change-set";
import { applyRefusalMessage, useWorkflowChangeSet } from "./detail-change-set";
import { useWorkflowCanvasHandlers } from "./detail-canvas-handlers";
import type { WorkflowChangeOperation } from "../builder/api";
import { useWorkflowStepActions } from "./detail-step-actions";
import { useWorkflowGroupActions } from "./detail-group-actions";
import { useWorkflowSettingsController } from "./detail-workflow-settings-controller";

const definition = definitionFromWorkflow({ workflow_id: "workflow_a", revision: 7, steps: [], groups: [] });
const operation = { op: "create_step", step: { step_id: "done", step_kind: "stop_success", display_name: "Done" } } as const;
const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });

function setup(serverRevision = 7) {
  let persistedRevision = serverRevision;
  const writes: Array<Record<string, unknown>> = [];
  const transport = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
    if (String(input).endsWith("/changes")) {
      if (body.expected_revision !== persistedRevision) return response({ detail: { code: "workflow_revision_conflict" } }, 409);
      if (!body.dry_run) {
        writes.push(body);
        const configSave = (body.operations as Array<{ op: string }>).some(({ op }) => op === "update_step_config" || op === "update_group_config");
        if (configSave) persistedRevision += 1;
      }
      return response({ workflow_id: "workflow_a", revision: persistedRevision, valid: true, issues: [], workflow: {} });
    }
    if (String(input) === "/api/workflows/workflow_a" && init?.method === "PATCH") {
      if (body.expected_revision !== persistedRevision) return response({ detail: { code: "workflow_revision_conflict" } }, 409);
      persistedRevision += 1;
      return response({ workflow_id: "workflow_a", revision: persistedRevision });
    }
    throw new Error(`Unexpected request: ${String(input)}`);
  });
  const refetchWorkflowSurfaces = vi.fn<() => Promise<unknown>>(async () => ({ workflow_id: "workflow_a", revision: persistedRevision }));
  const view = renderHook(({ revision }) => {
    const changeSet = useWorkflowChangeSet({ workflowId: "workflow_a", serverDefinition: definition, serverRevision: revision, refetchWorkflowSurfaces });
    const settings = useWorkflowSettingsController({
      changeSet, workflowId: "workflow_a", workflowRecord: { workflow_id: "workflow_a", revision, settings: {} },
      refetchWorkflowSurfaces, setSaveError: vi.fn(), setSaveNotice: vi.fn(), setSavingWorkflowSurface: vi.fn(),
    });
    const common = { changeSet, workflowId: "workflow_a", serverRevision: revision, refetchWorkflowSurfaces, setSaveError: vi.fn(), setSaveNotice: vi.fn(), setSavingWorkflowSurface: vi.fn() };
    const step = useWorkflowStepActions({
      ...common,
      selectedStep: { local: false, runtimeId: "compose", stepKind: "template", raw: { step_id: "compose", override: { template: { text: "before" } } } },
      stepKindDraft: "template", stepDisplayName: "Compose", stepOverrideDraft: JSON.stringify({ template: { text: "after" } }),
      stepAgentId: "", stepBlockKey: "", stepOptional: false,
    });
    const group = useWorkflowGroupActions({
      ...common, groupCreateMode: false,
      selectedGroup: { local: false, groupId: "loop", raw: { group_id: "loop", body: { kind: "sequence" } } },
      groupDraft: { containerKind: "group", bodyKind: "sequence", displayName: "Loop renamed", description: "", over: "", itemName: "item", indexName: "", parentGroupId: "", lanes: "", concurrencyLimit: "", groupId: "loop" },
    });
    return { changeSet, settings, step, group };
  }, { initialProps: { revision: serverRevision } });
  return { ...view, refetchWorkflowSurfaces, transport, writes, setPersistedRevision: (revision: number) => { persistedRevision = revision; } };
}

afterEach(() => vi.restoreAllMocks());

it("reloads an already-observed revision after an own-settings conflict without dropping structural edits", async () => {
  const view = setup();
  act(() => view.result.current.changeSet.stage(operation));
  view.setPersistedRevision(8);
  view.rerender({ revision: 8 }); // Settings save already refreshed the page before Reload.
  await act(async () => { await view.result.current.changeSet.apply(); });
  expect(view.result.current.changeSet.status).toBe("conflict");
  await act(async () => { await view.result.current.changeSet.reload(); });
  expect(view.result.current.changeSet.operations).toEqual([operation]);
  expect(view.result.current.changeSet.baseRevision).toBe(8);
  let applied = false;
  await act(async () => { applied = await view.result.current.changeSet.apply(); });
  expect(applied).toBe(true);
  expect(view.writes).toEqual([{ expected_revision: 8, operations: [operation] }]);
});

it("bases Reload on its fetched definition even before query props have rerendered", async () => {
  const view = setup();
  act(() => view.result.current.changeSet.stage(operation));
  view.setPersistedRevision(9);
  act(() => view.result.current.changeSet.reportConflict());
  await act(async () => { await view.result.current.changeSet.reload(); });
  expect(view.result.current.changeSet.baseRevision).toBe(9);
  expect(view.result.current.changeSet.operations).toEqual([operation]);
});

it("rebases structural edits after this author's successful settings save", async () => {
  const view = setup();
  act(() => view.result.current.changeSet.stage(operation));
  await act(async () => { await view.result.current.settings.handleSaveWorkflowSettings(); });
  expect(view.result.current.changeSet.baseRevision).toBe(8);
  expect(view.result.current.changeSet.operations).toEqual([operation]);
  let applied = false;
  await act(async () => { applied = await view.result.current.changeSet.apply(); });
  expect(applied).toBe(true);
  expect(view.writes).toEqual([{ expected_revision: 8, operations: [operation] }]);
});

it("does not use an own settings save to silently accept somebody else's newer base", async () => {
  const view = setup();
  act(() => view.result.current.changeSet.stage(operation));
  view.setPersistedRevision(8);
  view.rerender({ revision: 8 }); // Another writer has already changed the definition.
  await act(async () => { await view.result.current.settings.handleSaveWorkflowSettings(); });
  expect(view.result.current.changeSet.baseRevision).toBe(7);
  await act(async () => { await view.result.current.changeSet.apply(); });
  expect(view.result.current.changeSet.status).toBe("conflict");
  expect(view.writes).toEqual([]);
  expect(view.result.current.changeSet.operations).toEqual([operation]);
});

it("keeps the conflict and local operations when Reload cannot fetch a current definition", async () => {
  const view = setup();
  act(() => { view.result.current.changeSet.stage(operation); view.result.current.changeSet.reportConflict(); });
  view.refetchWorkflowSurfaces.mockResolvedValueOnce(null);
  await act(async () => { expect(await view.result.current.changeSet.reload()).toBe(false); });
  expect(view.result.current.changeSet.baseRevision).toBe(7);
  expect(view.result.current.changeSet.status).toBe("conflict");
  expect(view.result.current.changeSet.operations).toEqual([operation]);
  expect(view.result.current.changeSet.applyError).toContain("could not be reloaded");
});

it.each(["step", "group"] as const)("does not swallow an external revision through an own %s config save", async (kind) => {
  const view = setup();
  act(() => view.result.current.changeSet.stage(operation));
  view.setPersistedRevision(8);
  view.rerender({ revision: 8 });
  await act(async () => {
    if (kind === "step") await view.result.current.step.handleSaveStep();
    else await view.result.current.group.handleSaveGroup();
  });
  expect(view.writes).toHaveLength(1);
  expect(view.writes[0]).toMatchObject({ expected_revision: 8, operations: [{ op: `update_${kind}_config` }] });
  expect(view.result.current.changeSet.baseRevision).toBe(7);
  await act(async () => { await view.result.current.changeSet.apply(); });
  expect(view.result.current.changeSet.status).toBe("conflict");
  expect(view.writes).toHaveLength(1);
  expect(view.result.current.changeSet.operations).toEqual([operation]);
});

it.each(["step", "group"] as const)("continues pending structure after an own %s config save on the same base", async (kind) => {
  const view = setup();
  act(() => view.result.current.changeSet.stage(operation));
  await act(async () => {
    if (kind === "step") await view.result.current.step.handleSaveStep();
    else await view.result.current.group.handleSaveGroup();
  });
  expect(view.result.current.changeSet.baseRevision).toBe(8);
  expect(view.result.current.changeSet.operations).toEqual([operation]);
  await act(async () => { expect(await view.result.current.changeSet.apply()).toBe(true); });
  expect(view.writes).toHaveLength(2);
  expect(view.writes[1]).toEqual({ expected_revision: 8, operations: [operation] });
});


it.each(["applied", "refused"] as const)("preserves locale provenance while deduplicating %s Apply diagnostics", async (outcome) => {
  const owner = { code: "workflow.step_io.error", path: "workflow.steps.done", message: "Owner diagnostic" };
  const keyed = { ...owner, message_key: "fixture.source", message_params: { source: "state.a", label: "A" } };
  const reordered = { ...keyed, message_params: { label: "A", source: "state.a" } };
  const otherParams = { ...keyed, message_params: { source: "state.b", label: "A" } };
  const otherKey = { ...keyed, message_key: "fixture.field" };
  const issues = [owner, keyed, reordered, otherParams, otherKey, keyed];
  const original = JSON.stringify(issues);
  const messages: Record<string, string> = { "fixture.source": "Šaltinis {source}: {label}", "fixture.field": "Laukas {source}: {label}" };
  function Wrapper({ children }: { children: ReactNode }) {
    return <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, fallback = "", params) => interpolate(messages[key] || fallback, params) }}>{children}</I18nContext.Provider>;
  }
  const transport = vi.spyOn(globalThis, "fetch").mockResolvedValue(response(
    outcome === "refused" ? { detail: { issues } } : { workflow_id: "workflow_a", revision: 8, valid: false, issues },
    outcome === "refused" ? 400 : 200,
  ));
  const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "workflow_a", revision: 8 }));
  vi.useFakeTimers();
  const view = renderHook(() => useWorkflowChangeSet({ workflowId: "workflow_a", serverDefinition: definition, serverRevision: 7, refetchWorkflowSurfaces }), { wrapper: Wrapper });
  try {
    act(() => view.result.current.stage(operation));
    await act(async () => { expect(await view.result.current.apply()).toBe(outcome === "applied"); });
    expect(view.result.current.applyIssues).toEqual([owner, keyed, otherParams, otherKey]);
    expect(JSON.stringify(issues)).toBe(original);
    expect(transport).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(transport.mock.calls[0][1]?.body))).toEqual({ expected_revision: 7, operations: [operation] });
    if (outcome === "refused") {
      expect(view.result.current.applyError).toBe("Owner diagnostic Šaltinis state.a: A Šaltinis state.b: A Laukas state.a: A");
      expect(view.result.current.operations).toEqual([operation]);
      expect(refetchWorkflowSurfaces).not.toHaveBeenCalled();
    } else {
      expect(view.result.current.operations).toEqual([]);
      expect(refetchWorkflowSurfaces).toHaveBeenCalledTimes(1);
    }
  } finally {
    view.unmount();
    vi.clearAllTimers();
    vi.useRealTimers();
  }
});


it.each(["settings", "step", "group"] as const)("retires old Apply issues after an own %s save advances the definition", async (kind) => {
  const issue = { code: "workflow.step_io.error", path: "steps.shape_result", message: "Missing state.inputs.payload" };
  const view = setup();
  view.transport.mockImplementationOnce(async () => {
    view.setPersistedRevision(8);
    return response({ workflow_id: "workflow_a", revision: 8, valid: false, issues: [issue], workflow: {} });
  });
  act(() => view.result.current.changeSet.stage(operation));
  await act(async () => { expect(await view.result.current.changeSet.apply()).toBe(true); });
  expect(view.result.current.changeSet.applyIssues).toEqual([issue]);
  view.rerender({ revision: 8 });
  expect(view.result.current.changeSet.applyIssues).toEqual([issue]);
  await act(async () => {
    if (kind === "settings") await view.result.current.settings.handleSaveWorkflowSettings();
    else if (kind === "step") await view.result.current.step.handleSaveStep();
    else await view.result.current.group.handleSaveGroup();
  });
  expect(view.result.current.changeSet.applyIssues).toEqual([]);
  expect(view.result.current.changeSet.status).toBe("clean");
});


it("keeps Apply issues on the same revision, a different predecessor, or a conflict", async () => {
  const issue = { code: "workflow.step_io.error", path: "steps.shape_result", message: "Missing state.inputs.payload" };
  const view = setup();
  view.transport.mockImplementationOnce(async () => {
    view.setPersistedRevision(8);
    return response({ workflow_id: "workflow_a", revision: 8, valid: false, issues: [issue], workflow: {} });
  });
  act(() => view.result.current.changeSet.stage(operation));
  await act(async () => { await view.result.current.changeSet.apply(); });
  view.rerender({ revision: 8 });
  act(() => view.result.current.changeSet.rebase(8, 8));
  expect(view.result.current.changeSet.applyIssues).toEqual([issue]);
  act(() => view.result.current.changeSet.rebase(9, 7));
  expect(view.result.current.changeSet.applyIssues).toEqual([issue]);
  act(() => view.result.current.changeSet.reportConflict());
  act(() => view.result.current.changeSet.rebase(9, 8));
  expect(view.result.current.changeSet.applyIssues).toEqual([issue]);
  expect(view.result.current.changeSet.status).toBe("conflict");
});


it("retires a refused Apply's structured error after an own save without dropping pending edits", async () => {
  const issue = { code: "workflow.step_io.error", path: "steps.shape_result", message: "Missing state.inputs.payload" };
  const view = setup();
  view.transport.mockImplementationOnce(async () => response({ detail: { issues: [issue] } }, 400));
  act(() => view.result.current.changeSet.stage(operation));
  await act(async () => { expect(await view.result.current.changeSet.apply()).toBe(false); });
  expect(view.result.current.changeSet.applyError).toBe(issue.message);
  await act(async () => { await view.result.current.settings.handleSaveWorkflowSettings(); });
  expect(view.result.current.changeSet.applyIssues).toEqual([]);
  expect(view.result.current.changeSet.applyError).toBe("");
  expect(view.result.current.changeSet.status).toBe("pending");
  expect(view.result.current.changeSet.baseRevision).toBe(8);
  expect(view.result.current.changeSet.operations).toEqual([operation]);
  await act(async () => { expect(await view.result.current.changeSet.apply()).toBe(true); });
  expect(view.writes).toEqual([{ expected_revision: 8, operations: [operation] }]);
});

it("keeps an unstructured Apply transport failure visible after an own settings save", async () => {
  const view = setup();
  view.transport.mockRejectedValueOnce(new Error("Apply connection lost"));
  act(() => view.result.current.changeSet.stage(operation));
  await act(async () => { expect(await view.result.current.changeSet.apply()).toBe(false); });
  await act(async () => { await view.result.current.settings.handleSaveWorkflowSettings(); });
  expect(view.result.current.changeSet.applyError).toBe("Apply connection lost");
  expect(view.result.current.changeSet.status).toBe("error");
  expect(view.result.current.changeSet.operations).toEqual([operation]);
});


describe("a set an in-place edit left stale (Group → If → HTTP request → Approval, stand repro)", () => {
  const ref = (id: string) => ({ kind: "step" as const, id });
  const inGroup = (stepId: string, kind: string) => ({ step_id: stepId, step_kind: kind, group_id: "test_group", for_each_id: null, lane_id: null });
  const connect = (source: string, outcome: string, target: string): WorkflowChangeOperation => ({
    op: "connect",
    edge: { source: ref(source), outcome: { name: outcome } as never, target: ref(target) },
  });
  const groupBuild: WorkflowChangeOperation[] = [
    { op: "create_group", group: { group_id: "test_group", display_name: "Test Group", body: { kind: "sequence" } } },
    { op: "create_step", step: inGroup("if", "if") },
    { op: "set_entry", target: ref("if"), group_id: "test_group" },
    { op: "create_step", step: inGroup("http_request", "http_request") },
    connect("if", "true", "http_request"),
    { op: "create_step", step: inGroup("approval", "approval") },
    connect("http_request", "success", "approval"),
  ];
  const edgeUnknown = { code: "workflow.change.edge_unknown", message: "There is no edge on this outcome", path: "operations[7].outcome" };

  function setupBuilder(refuse = false) {
    const writes: Array<Record<string, unknown>> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
      if (!String(input).endsWith("/changes")) throw new Error(`Unexpected request: ${String(input)}`);
      if (body.dry_run) return response({ workflow_id: "workflow_a", revision: 7, valid: false, issues: [], blocking_issues: [], workflow: {} });
      writes.push(body);
      if (refuse) return response({ detail: { issues: [edgeUnknown] } }, 400);
      return response({ workflow_id: "workflow_a", revision: 8, valid: false, issues: [], workflow: {} });
    });
    const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "workflow_a", revision: 8 }));
    const view = renderHook(() => {
      const changeSet = useWorkflowChangeSet({ workflowId: "workflow_a", serverDefinition: definition, serverRevision: 7, refetchWorkflowSurfaces });
      const handlers = useWorkflowCanvasHandlers({
        changeSet, openGroupDrawer: vi.fn(), openNewStepDrawer: vi.fn(), openStepDrawer: vi.fn(), requestConfirmation: vi.fn(async () => true),
        setAutomationScopeKind: vi.fn(), setDrawerMode: vi.fn(), setDrawerTab: vi.fn(), setSaveNotice: vi.fn(), setSelectedCanvasIssue: vi.fn(),
      });
      return { changeSet, handlers };
    });
    return { view, writes };
  }

  it("applies without the disconnect once the new step it pointed to is deleted", async () => {
    const { view, writes } = setupBuilder();
    // Add after Approval · Approved → Tmp; then Approved → Not connected; then delete Tmp.
    act(() => view.result.current.changeSet.stage([...groupBuild, { op: "create_step", step: inGroup("tmp", "agent") }, connect("approval", "approved", "tmp")]));
    act(() => view.result.current.handlers.disconnectPort(ref("approval"), { name: "approved" }));
    expect(view.result.current.changeSet.operations.at(-1)).toEqual({ op: "disconnect", source: ref("approval"), outcome: { name: "approved" } });
    act(() => view.result.current.changeSet.dropStagedStep("tmp"));
    expect(view.result.current.changeSet.operations).toEqual(groupBuild);
    await act(async () => { expect(await view.result.current.changeSet.apply()).toBe(true); });
    expect(writes).toEqual([{ expected_revision: 7, operations: groupBuild }]);
  });

  it("stages the kind edit of an inserted step as its own action, keeping the edge it was inserted on", () => {
    const { view } = setupBuilder();
    act(() => view.result.current.changeSet.stage([
      ...groupBuild.slice(0, 5),
      { op: "insert_step_between", source: ref("if"), outcome: { name: "true" }, step: { step_id: "gate", step_kind: "agent" } },
    ]));
    expect(view.result.current.changeSet.operations.at(-1)?.op).toBe("insert_step_between");
    act(() => { view.result.current.changeSet.updateStagedStep("gate", { step_kind: "approval" }); });
    expect(view.result.current.changeSet.operations.slice(5)).toEqual([
      { op: "insert_step_between", source: ref("if"), outcome: { name: "true" }, step: { step_id: "gate", step_kind: "agent" } },
      { op: "change_step_kind", step_id: "gate", step: { step_kind: "approval" } },
    ]);
    const { preview } = view.result.current.changeSet;
    expect(preview.steps.find((step) => step.step_id === "gate")?.step_kind).toBe("approval");
    expect(preview.graph.edges.some((edge) => edge.source.id === "if" && edge.outcome.name === "true" && edge.target.kind === "step" && edge.target.id === "gate")).toBe(true);
    expect(preview.graph.edges.some((edge) => edge.source.id === "gate")).toBe(false);
    // Undo takes back the kind edit only.
    act(() => view.result.current.changeSet.undoGraphIntent());
    expect(view.result.current.changeSet.preview.steps.find((step) => step.step_id === "gate")?.step_kind).toBe("agent");
  });

  it("says that nothing was applied when Apply is refused, and keeps the set", async () => {
    const { view } = setupBuilder(true);
    act(() => view.result.current.changeSet.stage(groupBuild));
    await act(async () => { expect(await view.result.current.changeSet.apply()).toBe(false); });
    const { applyError, applyRefused, operations, status } = view.result.current.changeSet;
    expect(applyRefused).toBe(true);
    expect(applyRefusalMessage(applyError, applyRefused)).toBe("The changes were not applied: There is no edge on this outcome");
    expect(status).toBe("error");
    expect(operations).toEqual(groupBuild);
    act(() => view.result.current.changeSet.stage(connect("approval", "approved", "if")));
    expect(view.result.current.changeSet.applyRefused).toBe(false);
  });
});


it("retains the exact declaration draft and field issue when the server changes to Production before Apply", async () => {
  const issue = { code: "workflow.hook.declaration_requires_development", path: "steps.zone.override.invoke_workflow.hook", severity: "error", message: "Creating a hook zone requires a Development environment." };
  const draft: WorkflowChangeOperation = { op: "create_step", step: { step_id: "zone", step_kind: "invoke_workflow", override: { invoke_workflow: { hook: { hook_id: "before", contract_version: 1, display_name: "Before", input_schema: { type: "object", properties: {} }, input_mapping: {}, stage_contract: { summary: "Before completion" } } } } } };
  const transport = vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ detail: { issues: [issue] } }, 400));
  const refetchWorkflowSurfaces = vi.fn();
  vi.useFakeTimers();
  const view = renderHook(() => useWorkflowChangeSet({ workflowId: "workflow_a", serverDefinition: definition, serverRevision: 7, refetchWorkflowSurfaces }));
  try {
    act(() => view.result.current.stage(draft));
    await act(async () => { expect(await view.result.current.apply()).toBe(false); });
    expect(view.result.current.status).toBe("error");
    expect(view.result.current.operations).toEqual([draft]);
    expect(view.result.current.preview.steps[0]).toMatchObject(draft.step);
    expect(view.result.current.applyIssues).toEqual([issue]);
    expect(view.result.current.applyError).toContain("Development");
    expect(refetchWorkflowSurfaces).not.toHaveBeenCalled();
    expect(JSON.parse(String(transport.mock.calls[0][1]?.body))).toEqual({ expected_revision: 7, operations: [draft] });
  } finally { view.unmount(); vi.clearAllTimers(); vi.useRealTimers(); }
});


describe("one owner-local graph intent history", () => {
  type HistoryChangeSet = ReturnType<typeof useWorkflowChangeSet> & {
    stageGraphIntent: (operations: WorkflowChangeOperation[]) => void;
    undoGraphIntent: () => void;
    redoGraphIntent: () => void;
  };
  const ref = (id: string) => ({ kind: "step" as const, id });
  const created: WorkflowChangeOperation = {
    op: "create_step",
    step: { step_id: "done", step_kind: "stop_success", display_name: "Done" },
  };
  const connected: WorkflowChangeOperation = {
    op: "connect",
    edge: { source: ref("compose"), outcome: { name: "success" }, target: ref("done") },
  };
  const base = (text: string) => definitionFromWorkflow({
    workflow_id: "workflow_a",
    revision: 7,
    steps: [
      { step_id: "start", step_kind: "agent", display_name: "Start" },
      { step_id: "compose", step_kind: "template", display_name: "Compose", override: { template: { text } } },
    ],
    groups: [],
    settings: {
      execution_graph: {
        schema_version: 1,
        entry: ref("start"),
        edges: [{ source: ref("start"), outcome: { name: "success" }, target: ref("compose") }],
      },
    },
  });
  const grouped = definitionFromWorkflow({
    workflow_id: "workflow_a",
    revision: 7,
    steps: [
      { step_id: "inner_a", step_kind: "agent", display_name: "A", group_id: "loop" },
      { step_id: "inner_b", step_kind: "agent", display_name: "B", group_id: "loop" },
    ],
    groups: [{ group_id: "loop", display_name: "Loop", body: { kind: "sequence", entry: ref("inner_a") } }],
    settings: {
      execution_graph: {
        schema_version: 1,
        entry: { kind: "group", id: "loop" },
        edges: [{ source: ref("inner_a"), outcome: { name: "success" }, target: ref("inner_b") }],
      },
    },
  });
  const controller = (value: ReturnType<typeof useWorkflowChangeSet>): HistoryChangeSet => value as HistoryChangeSet;

  it("undoes create plus connect as one intent, returns to a clean net-zero set, and redoes once", async () => {
    const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "workflow_a", revision: 7 }));
    const stableDefinition = base("before");
    const view = renderHook(() => useWorkflowChangeSet({
      workflowId: "workflow_a", serverDefinition: stableDefinition, serverRevision: 7, refetchWorkflowSurfaces,
    }));
    expect(controller(view.result.current).stageGraphIntent).toEqual(expect.any(Function));
    expect(controller(view.result.current).undoGraphIntent).toEqual(expect.any(Function));
    expect(controller(view.result.current).redoGraphIntent).toEqual(expect.any(Function));

    await act(() => controller(view.result.current).stageGraphIntent([created, connected]));
    expect(view.result.current.operations).toEqual([created, connected]);
    expect(view.result.current.preview.steps.some((step) => step.step_id === "done")).toBe(true);
    expect(view.result.current.status).toBe("pending");

    act(() => controller(view.result.current).undoGraphIntent());
    expect(view.result.current.operations).toEqual([]);
    expect(view.result.current.hasChanges).toBe(false);
    expect(view.result.current.status).toBe("clean");
    expect(view.result.current.preview.steps.some((step) => step.step_id === "done")).toBe(false);

    act(() => controller(view.result.current).redoGraphIntent());
    expect(view.result.current.operations).toEqual([created, connected]);
    expect(view.result.current.hasChanges).toBe(true);
    act(() => controller(view.result.current).undoGraphIntent());
    expect(view.result.current.operations).toEqual([]);
  });

  it("lets a toast's Undo reverse exactly its own change, and only while it is the latest intent", async () => {
    const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "workflow_a", revision: 7 }));
    const stableDefinition = base("before");
    const view = renderHook(() => useWorkflowChangeSet({
      workflowId: "workflow_a", serverDefinition: stableDefinition, serverRevision: 7, refetchWorkflowSurfaces,
    }));
    expect(view.result.current.lastStagedGraphIntent()).toBeNull();
    await act(() => controller(view.result.current).stageGraphIntent([created]));
    const first = view.result.current.lastStagedGraphIntent();
    expect(first).not.toBeNull();
    await act(() => controller(view.result.current).stageGraphIntent([connected]));
    const second = view.result.current.lastStagedGraphIntent();
    expect(second).not.toBe(first);
    let undone = true;
    act(() => { undone = view.result.current.undoGraphIntentIfLatest(first!); });
    expect(undone).toBe(false);
    expect(view.result.current.operations).toEqual([created, connected]);
    act(() => { undone = view.result.current.undoGraphIntentIfLatest(second!); });
    expect(undone).toBe(true);
    expect(view.result.current.operations).toEqual([created]);
    act(() => { undone = view.result.current.undoGraphIntentIfLatest(first!); });
    expect(undone).toBe(true);
    expect(view.result.current.operations).toEqual([]);
  });

  it.each([
    {
      name: "Group closure",
      operations: [{ op: "delete_group", group_id: "loop", with_contents: true }] as WorkflowChangeOperation[],
      changed: (value: ReturnType<typeof useWorkflowChangeSet>) => value.preview.groups.length === 0 && value.preview.steps.length === 0,
      restored: (value: ReturnType<typeof useWorkflowChangeSet>) => value.preview.groups.length === 1 && value.preview.steps.length === 2,
    },
    {
      name: "two-step placement",
      operations: [
        { op: "move_step", step_id: "inner_a", group_id: null, lane_id: null },
        { op: "move_step", step_id: "inner_b", group_id: null, lane_id: null },
      ] as WorkflowChangeOperation[],
      changed: (value: ReturnType<typeof useWorkflowChangeSet>) => value.preview.steps.every((step) => !step.group_id),
      restored: (value: ReturnType<typeof useWorkflowChangeSet>) => value.preview.steps.every((step) => step.group_id === "loop"),
    },
  ])("reverses $name as one user intent instead of one operation at a time", async ({ operations, changed, restored }) => {
    const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "workflow_a", revision: 7 }));
    const view = renderHook(() => useWorkflowChangeSet({
      workflowId: "workflow_a", serverDefinition: grouped, serverRevision: 7, refetchWorkflowSurfaces,
    }));
    await act(() => controller(view.result.current).stageGraphIntent(operations));
    expect(changed(view.result.current)).toBe(true);
    act(() => controller(view.result.current).undoGraphIntent());
    expect(restored(view.result.current)).toBe(true);
    expect(view.result.current.operations).toEqual([]);
    expect(view.result.current.status).toBe("clean");
    act(() => controller(view.result.current).redoGraphIntent());
    expect(changed(view.result.current)).toBe(true);
  });

  it("undoes a delete against the latest saved parameters and applies later work at the newer revision", async () => {
    const writes: Array<Record<string, unknown>> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (!String(input).endsWith("/changes")) throw new Error(`Unexpected request: ${String(input)}`);
      const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
      if (!body.dry_run) writes.push(body);
      return response({ workflow_id: "workflow_a", revision: 9, valid: true, issues: [], workflow: {} });
    });
    const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "workflow_a", revision: 9 }));
    const view = renderHook(({ serverDefinition, revision }) => useWorkflowChangeSet({
      workflowId: "workflow_a", serverDefinition, serverRevision: revision, refetchWorkflowSurfaces,
    }), { initialProps: { serverDefinition: base("before"), revision: 7 } });

    await act(() => controller(view.result.current).stageGraphIntent([{ op: "delete_step", step_id: "compose" }]));
    expect(view.result.current.preview.steps.some((step) => step.step_id === "compose")).toBe(false);
    view.rerender({ serverDefinition: base("saved after delete was staged"), revision: 8 });
    act(() => view.result.current.rebase(8, 7));
    act(() => controller(view.result.current).undoGraphIntent());
    expect(view.result.current.preview.steps.find((step) => step.step_id === "compose")?.override).toEqual({
      template: { text: "saved after delete was staged" },
    });
    expect(view.result.current.operations).toEqual([]);
    expect(view.result.current.status).toBe("clean");

    await act(() => controller(view.result.current).stageGraphIntent([created]));
    await act(async () => { expect(await view.result.current.apply()).toBe(true); });
    expect(writes).toEqual([{ expected_revision: 8, operations: [created] }]);
  });

  it.each(["discard", "apply"] as const)("does not resurrect a prior unapplied intent after %s", async (boundary) => {
    const writes: Array<Record<string, unknown>> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (!String(input).endsWith("/changes")) throw new Error(`Unexpected request: ${String(input)}`);
      const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
      if (!body.dry_run) writes.push(body);
      return response({ workflow_id: "workflow_a", revision: 8, valid: true, issues: [], workflow: {} });
    });
    const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "workflow_a", revision: 8 }));
    const stableDefinition = base("before");
    const view = renderHook(() => useWorkflowChangeSet({
      workflowId: "workflow_a", serverDefinition: stableDefinition, serverRevision: 7, refetchWorkflowSurfaces,
    }));
    await act(() => controller(view.result.current).stageGraphIntent([created, connected]));
    if (boundary === "discard") act(() => view.result.current.discard());
    else await act(async () => { expect(await view.result.current.apply()).toBe(true); });
    act(() => controller(view.result.current).redoGraphIntent());
    act(() => controller(view.result.current).undoGraphIntent());
    if (boundary === "discard") {
      expect(view.result.current.operations).toEqual([]);
      expect(view.result.current.status).toBe("clean");
    } else {
      // The applied intent stays in the history: Undo stages its reverse and writes nothing.
      expect(view.result.current.operations).toEqual([{ op: "delete_step", step_id: "done" }]);
      expect(view.result.current.status).toBe("pending");
      act(() => controller(view.result.current).redoGraphIntent());
      expect(view.result.current.operations).toEqual([]);
      expect(view.result.current.status).toBe("clean");
    }
    expect(writes).toHaveLength(boundary === "apply" ? 1 : 0);
  });

  it("clears a refused Apply error when Undo leaves no pending graph intent", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (!String(input).endsWith("/changes")) throw new Error(`Unexpected request: ${String(input)}`);
      const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
      if (body.dry_run) return response({ workflow_id: "workflow_a", revision: 7, applied: false, valid: false, issues: [], blocking_issues: [], workflow: {} });
      return response({ detail: { issues: [{ code: "workflow.change.invalid", message: "The graph intent was refused" }] } }, 400);
    });
    const stableDefinition = base("before");
    const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "workflow_a", revision: 7 }));
    const view = renderHook(() => useWorkflowChangeSet({
      workflowId: "workflow_a", serverDefinition: stableDefinition, serverRevision: 7, refetchWorkflowSurfaces,
    }));
    await act(() => controller(view.result.current).stageGraphIntent([created]));
    await act(async () => { expect(await view.result.current.apply()).toBe(false); });
    expect(view.result.current.status).toBe("error");
    expect(view.result.current.applyError).toContain("refused");
    act(() => controller(view.result.current).undoGraphIntent());
    expect(view.result.current.operations).toEqual([]);
    expect(view.result.current.status).toBe("clean");
    expect(view.result.current.applyError).toBe("");
    expect(view.result.current.applyIssues).toEqual([]);
    expect(view.result.current.applyRefused).toBe(false);
    expect(refetchWorkflowSurfaces).not.toHaveBeenCalled();
  });

  it("does not replay a previous workflow owner's intent after the owner changes", async () => {
    const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "workflow_b", revision: 4 }));
    const view = renderHook(({ workflowId, serverDefinition, revision }) => useWorkflowChangeSet({
      workflowId, serverDefinition, serverRevision: revision, refetchWorkflowSurfaces,
    }), { initialProps: { workflowId: "workflow_a", serverDefinition: base("before"), revision: 7 } });
    await act(() => controller(view.result.current).stageGraphIntent([created]));
    view.rerender({ workflowId: "workflow_b", serverDefinition: definitionFromWorkflow({
      workflow_id: "workflow_b", revision: 4, steps: [{ step_id: "only_b", step_kind: "agent" }], groups: [],
    }), revision: 4 });
    expect(view.result.current.operations).toEqual([]);
    act(() => controller(view.result.current).undoGraphIntent());
    act(() => controller(view.result.current).redoGraphIntent());
    expect(view.result.current.operations).toEqual([]);
    expect(view.result.current.preview.steps.map((step) => step.step_id)).toEqual(["only_b"]);
    expect(view.result.current.status).toBe("clean");
  });
});


it("keeps a locally edited creation in the journal when Delete is undone", async () => {
  const view = setup();
  const create: WorkflowChangeOperation = {
    op: "create_step",
    step: { step_id: "draft_plan", step_kind: "template", display_name: "Draft plan", override: { template: { text: "first" } } },
  };
  act(() => view.result.current.changeSet.stage(create));
  act(() => { view.result.current.changeSet.updateStagedStep("draft_plan", { override: { template: { text: "edited" } } }); });
  expect(view.result.current.changeSet.preview.steps.find((step) => step.step_id === "draft_plan")?.override).toEqual({ template: { text: "edited" } });

  await act(() => view.result.current.changeSet.stageGraphIntent([{ op: "delete_step", step_id: "draft_plan" }]));
  expect(view.result.current.changeSet.preview.steps.some((step) => step.step_id === "draft_plan")).toBe(false);
  expect(view.result.current.changeSet.operations.map(({ op }) => op)).toEqual(["create_step", "delete_step"]);
  expect(view.result.current.changeSet.canUndo).toBe(true);

  act(() => view.result.current.changeSet.undoGraphIntent());
  expect(view.result.current.changeSet.preview.steps.find((step) => step.step_id === "draft_plan")?.override).toEqual({ template: { text: "edited" } });
  expect(view.result.current.changeSet.operations.map(({ op }) => op)).toEqual(["create_step"]);
  // The parameter edit is its own action: the next Undo takes back exactly it.
  act(() => view.result.current.changeSet.undoGraphIntent());
  expect(view.result.current.changeSet.preview.steps.find((step) => step.step_id === "draft_plan")?.override).toEqual({ template: { text: "first" } });
  expect(view.result.current.changeSet.operations.map(({ op }) => op)).toEqual(["create_step"]);
  expect(view.writes).toEqual([]);
});

it("keeps a staged-step notice tied to its exact creation across parameter edits and retires it after Apply", async () => {
  const view = setup();
  try {
    act(() => view.result.current.changeSet.stage(operation));
    const noticeOwner = view.result.current.changeSet.captureStagedStepNoticeOwner("done");
    expect(noticeOwner?.isCurrent()).toBe(true);
    act(() => { view.result.current.changeSet.updateStagedStep("done", { display_name: "Updated stop" }); });
    expect(noticeOwner?.isCurrent()).toBe(true);
    act(() => view.result.current.changeSet.stage({ op: "create_step", step: { step_id: "other", step_kind: "stop_success" } }));
    expect(noticeOwner?.isCurrent()).toBe(true);
    await act(async () => { expect(await view.result.current.changeSet.apply()).toBe(true); });
    expect(noticeOwner?.isCurrent()).toBe(false);
    act(() => view.result.current.changeSet.stage(operation));
    expect(noticeOwner?.isCurrent()).toBe(false);
    expect(view.result.current.changeSet.captureStagedStepNoticeOwner("done")?.isCurrent()).toBe(true);
  } finally { view.unmount(); }
});

it("retires a discarded creation even when its journal entry number and logical step are reused", () => {
  const view = setup();
  try {
    act(() => view.result.current.changeSet.stage(operation));
    const noticeOwner = view.result.current.changeSet.captureStagedStepNoticeOwner("done");
    act(() => view.result.current.changeSet.discard());
    expect(noticeOwner?.isCurrent()).toBe(false);
    act(() => view.result.current.changeSet.stage(operation));
    expect(noticeOwner?.isCurrent()).toBe(false);
    expect(view.result.current.changeSet.captureStagedStepNoticeOwner("done")?.isCurrent()).toBe(true);
  } finally { view.unmount(); }
});

it("retains the current notice proof after a refused Apply", async () => {
  const view = setup();
  try {
    act(() => view.result.current.changeSet.stage(operation));
    const noticeOwner = view.result.current.changeSet.captureStagedStepNoticeOwner("done");
    view.transport.mockResolvedValue(response({ detail: { message: "Still invalid", code: "invalid_changes" } }, 422));
    await act(async () => { expect(await view.result.current.changeSet.apply()).toBe(false); });
    expect(noticeOwner?.isCurrent()).toBe(true);
    expect(view.result.current.changeSet.hasChanges).toBe(true);
  } finally { view.unmount(); }
});

it.each(["workflow", "Hook view"])("does not revive an obsolete staged notice after %s A→B→A", (scope) => {
  const view = renderHook(({ workflowId, placementViewKey }) => useWorkflowChangeSet({ workflowId, placementViewKey,
    serverDefinition: definition, serverRevision: 7, refetchWorkflowSurfaces: async () => undefined }),
  { initialProps: { workflowId: "workflow_a", placementViewKey: "hook-a" } });
  try {
    act(() => view.result.current.stage(operation));
    const noticeOwner = view.result.current.captureStagedStepNoticeOwner("done");
    expect(noticeOwner?.isCurrent()).toBe(true);
    view.rerender({ workflowId: scope === "workflow" ? "workflow_b" : "workflow_a", placementViewKey: scope === "workflow" ? "hook-a" : "hook-b" });
    view.rerender({ workflowId: "workflow_a", placementViewKey: "hook-a" });
    expect(noticeOwner?.isCurrent()).toBe(false);
  } finally { view.unmount(); }
});

it("returns isolated accepted canonical staging rows, including no-op and kind replacement", () => {
  const view = setup();
  try {
    act(() => view.result.current.changeSet.stage(operation));
    let accepted: ReturnType<typeof view.result.current.changeSet.updateStagedStep>;
    act(() => { accepted = view.result.current.changeSet.updateStagedStep("done", { display_name: "Agent", step_kind: "agent", agent_id: "writer", input_mapping_jsonb: { flag: false, zero: 0, nullable: null, code: "001" } }); });
    expect(accepted!).toEqual({ step_id: "done", step_kind: "agent", display_name: "Agent", agent_id: "writer", group_id: null, for_each_id: null, lane_id: null, input_mapping_jsonb: { flag: false, zero: 0, nullable: null, code: "001" } });
    expect(view.result.current.changeSet.preview.steps[0]).toEqual(accepted!);
    const history = view.result.current.changeSet.journal;
    act(() => { accepted = view.result.current.changeSet.updateStagedStep("done", { step_kind: "agent", agent_id: "writer" }); });
    expect(accepted!).toEqual(view.result.current.changeSet.preview.steps[0]);
    expect(view.result.current.changeSet.journal).toBe(history);
    (accepted!.input_mapping_jsonb as Record<string, unknown>).flag = "mutated outside owner";
    expect(view.result.current.changeSet.preview.steps[0].input_mapping_jsonb).toEqual({ flag: false, zero: 0, nullable: null, code: "001" });
    act(() => { accepted = view.result.current.changeSet.updateStagedStep("done", { step_kind: "stop_success" }); });
    expect(accepted!).toEqual({ step_id: "done", step_kind: "stop_success", display_name: "Agent", group_id: null, for_each_id: null, lane_id: null });
    expect(view.result.current.changeSet.preview.steps[0]).toEqual(accepted!);
    expect(view.result.current.changeSet.updateStagedStep("missing", {})).toBeNull();
  } finally { view.unmount(); }
});

it.each(["workflow", "Hook"])("rejects obsolete local-update callbacks across %s ABA", (boundary) => {
  const view = renderHook(({ workflowId, placementViewKey }) => useWorkflowChangeSet({ workflowId, placementViewKey, serverDefinition: definition, serverRevision: 7, refetchWorkflowSurfaces: async () => undefined }), { initialProps: { workflowId: "workflow_a", placementViewKey: "hook-a" } });
  try {
    act(() => view.result.current.stage(operation));
    const stale = view.result.current.updateStagedStep;
    view.rerender({ workflowId: boundary === "workflow" ? "workflow_b" : "workflow_a", placementViewKey: boundary === "Hook" ? "hook-b" : "hook-a" });
    view.rerender({ workflowId: "workflow_a", placementViewKey: "hook-a" });
    act(() => expect(stale("done", { display_name: "obsolete" })).toBeNull());
    expect(view.result.current.preview.steps.some((step) => step.display_name === "obsolete")).toBe(false);
  } finally { view.unmount(); }
});


it.each(["return to rendered value", "different field"])("rejects a superseded same-batch Agent updater: %s", (scenario) => {
  const view = setup();
  try {
    act(() => view.result.current.changeSet.stage({ op: "create_step", step: { step_id: "agent", step_kind: "agent", display_name: "Agent", agent_id: "writer", input_mapping_jsonb: { value: "A" } } }));
    const update = view.result.current.changeSet.updateStagedStep;
    let first: ReturnType<typeof update> = null;
    let superseded: ReturnType<typeof update> = null;
    act(() => {
      first = update("agent", { input_mapping_jsonb: { value: "B" } });
      superseded = update("agent", scenario === "return to rendered value" ? { input_mapping_jsonb: { value: "A" } } : { display_name: "Queued label" });
    });
    expect(first).toMatchObject({ step_id: "agent", display_name: "Agent", input_mapping_jsonb: { value: "B" } });
    expect(superseded).toBeNull();
    expect(view.result.current.changeSet.preview.steps[0]).toEqual(first);
    // Retaining the old callback across a commit cannot revive its former preview.
    act(() => expect(update("agent", { input_mapping_jsonb: { value: "A" } })).toBeNull());
    let fresh: ReturnType<typeof update> = null;
    act(() => { fresh = view.result.current.changeSet.updateStagedStep("agent", { input_mapping_jsonb: { value: "A" } }); });
    expect(fresh).toMatchObject({ input_mapping_jsonb: { value: "A" } });
    expect(view.result.current.changeSet.preview.steps[0]).toEqual(fresh);
  } finally { view.unmount(); }
});

it.each(["stage", "discard", "restore"])("rejects an old preview's no-op acknowledgment after queued %s", (command) => {
  const view = setup();
  try {
    const emptyJournal = view.result.current.changeSet.journal;
    act(() => view.result.current.changeSet.stage(operation));
    const update = view.result.current.changeSet.updateStagedStep;
    let accepted: ReturnType<typeof update> = null;
    act(() => {
      if (command === "stage") view.result.current.changeSet.stage({ op: "update_step_config", step_id: "done", patch: { display_name: "Queued" } });
      else if (command === "restore") view.result.current.changeSet.restoreJournal({ journal: emptyJournal, checkpointDefinition: definition, checkpointRevision: 7 });
      else view.result.current.changeSet.discard();
      accepted = update("done", { display_name: "Done" });
    });
    expect(accepted).toBeNull();
    if (command === "stage") expect(view.result.current.changeSet.preview.steps[0].display_name).toBe("Queued");
    else expect(view.result.current.changeSet.preview.steps).toEqual([]);
  } finally { view.unmount(); }
});


it.each(["diagnostic render", "journal render"])("rejects a canonical ACK from an urgent %s that skipped a queued transition", async (urgent) => {
  const refetchWorkflowSurfaces = async () => undefined;
  const view = renderHook(() => {
    const changeSet = useWorkflowChangeSet({ workflowId: "workflow_a", serverDefinition: definition, serverRevision: 7, refetchWorkflowSurfaces });
    return changeSet;
  });
  try {
    act(() => view.result.current.stage({ op: "create_step", step: { step_id: "agent", step_kind: "agent", display_name: "Agent", agent_id: "writer", input_mapping_jsonb: { value: "A" } } }));
    // Keep this priority test in conflict, with dry-run checks idle.
    if (urgent === "journal render") act(() => view.result.current.reportConflict());
    await act(async () => {
      startTransition(() => { view.result.current.stage({ op: "update_step_config", step_id: "agent", patch: { input_mapping_jsonb: { value: "B" } } }); });
      flushSync(() => {
        if (urgent === "diagnostic render") view.result.current.reportConflict();
        else view.result.current.stage({ op: "update_step_config", step_id: "agent", patch: { display_name: "Queued label" } });
      });
      expect(view.result.current.preview.steps[0].input_mapping_jsonb).toEqual({ value: "A" });
      expect(view.result.current.updateStagedStep("agent", { input_mapping_jsonb: { value: "A" } })).toBeNull();
    });
    expect(view.result.current.preview.steps[0].input_mapping_jsonb).toEqual({ value: "B" });
    if (urgent === "journal render") expect(view.result.current.preview.steps[0].display_name).toBe("Queued label");
    let fresh: ReturnType<typeof view.result.current.updateStagedStep> = null;
    act(() => { fresh = view.result.current.updateStagedStep("agent", { input_mapping_jsonb: { value: "C" } }); });
    expect(fresh).toMatchObject({ input_mapping_jsonb: { value: "C" } });
    expect(view.result.current.preview.steps[0]).toEqual(fresh);
  } finally { view.unmount(); }
});


it.each(["checked", "refused", "unavailable"] as const)("resets a committed %s check to idle when conflict makes it inapplicable", async (outcome) => {
  vi.useFakeTimers();
  const view = setup();
  const issue = { code: "fixture.check", message: "Old check issue" };
  if (outcome === "unavailable") view.transport.mockRejectedValue(new Error("Offline check"));
  else view.transport.mockResolvedValue(outcome === "checked"
    ? response({ workflow_id: "workflow_a", revision: 7, valid: true, issues: [issue] })
    : response({ detail: { issues: [issue] } }, 400));
  try {
    act(() => view.result.current.changeSet.stage(operation));
    await act(async () => vi.advanceTimersByTimeAsync(400));
    expect(view.result.current.changeSet.checkState).toBe(outcome);
    act(() => view.result.current.changeSet.reportConflict());
    expect(view.result.current.changeSet.checkState).toBe("idle");
    expect(view.result.current.changeSet.blockingIssueMessages).toEqual([]);
    expect(view.result.current.changeSet.keptIssueMessages).toEqual([]);
  } finally { view.unmount(); vi.clearAllTimers(); vi.useRealTimers(); }
});
