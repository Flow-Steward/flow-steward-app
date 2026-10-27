import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { stepConfigPatch, useWorkflowStepActions } from "@/pages/workflows/detail/detail-step-actions";
import { definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";
import type { GraphEditNotice } from "@/pages/workflows/builder/canvas-core";
import { useCanvasToastFeed } from "@/pages/workflows/builder/canvas-toast-feed";

const preview = definitionFromWorkflow({
  workflow_id: "wf_demo",
  steps: [
    { step_id: "compose_post", step_kind: "template", display_name: "Compose post" },
    { step_id: "route", step_kind: "switch", override: { switch: { selector: "state.x", cases: [{ case_id: "a", when: "a" }] } } },
    { step_id: "publish", step_kind: "connector" },
    { step_id: "check_order", step_kind: "agent", for_each_id: "each_order" },
  ],
  groups: [{ group_id: "loop", body: { kind: "sequence" } }],
  for_each: [{ for_each_id: "each_order", group_id: "loop", over: "state.orders", item_name: "order", body: { kind: "sequence", entry: { kind: "step", id: "check_order" } } }],
  settings: {
    execution_graph: {
      schema_version: 1,
      entry: { kind: "step", id: "compose_post" },
      edges: [
        { source: { kind: "step", id: "compose_post" }, outcome: { name: "success" }, target: { kind: "step", id: "route" } },
      ],
    },
  },
});

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } });
}

function setup(overrides: Record<string, unknown> = {}) {
  const staged: WorkflowChangeOperation[] = [];
  const changeSet = {
    dropStagedStep: vi.fn(),
    operations: [] as WorkflowChangeOperation[],
    preview,
    rebase: vi.fn(),
    reportConflict: vi.fn(),
    stage: vi.fn((next: WorkflowChangeOperation | WorkflowChangeOperation[]) => {
      staged.push(...(Array.isArray(next) ? next : [next]));
    }),
    stageGraphIntent: vi.fn((next: WorkflowChangeOperation[]) => { staged.push(...next); return { controller: Symbol("accepted-intent"), serial: staged.length }; }),
    workflowId: "wf_demo", placementViewKey: "", placementViewEpoch: 0,
    touchesStep: vi.fn(() => false),
    updateStagedStep: vi.fn((stepId: string, patch: Record<string, unknown>) => ({ step_id: stepId, ...patch })),
  };
  const ctx = {
    changeSet,
    // Authorization belongs to the mounted Task 1 guard tests. This unit fixture
    // only exercises the editor's post-authorization command contract.
    deletionGuard: {
      currentTarget: vi.fn(() => true),
      requestDelete: vi.fn(async (_target: unknown, onAccepted: () => void) => { onAccepted(); return true; }),
    },
    createAsEntry: false,
    refetchWorkflowSurfaces: vi.fn().mockResolvedValue(undefined),
    requestConfirmation: vi.fn(async () => true),
    selectedStep: {
      local: false,
      raw: { step_id: "compose_post", override: { template: { text: "x" } } },
      runtimeId: "compose_post",
      stepKind: "template",
    },
    serverRevision: 7,
    focusCanvasNode: vi.fn(),
    setCreateAsEntry: vi.fn(),
    setSaveError: vi.fn(),
    setSaveNotice: vi.fn(),
    setSavingWorkflowSurface: vi.fn(),
    setSelectedStepId: vi.fn(),
    setStepCreateContext: vi.fn(),
    setStepOverrideDraft: vi.fn(),
    stepAgentId: "",
    stepBlockKey: "",
    stepCreateContext: null,
    stepDisplayName: "Compose post",
    stepKindDraft: "template",
    stepOptional: false,
    stepOverrideDraft: JSON.stringify({ template: { text: "y", result_target: "state.outputs.x" } }),
    workflowId: "wf_demo",
    ...overrides,
  };
  const view = renderHook(() => useWorkflowStepActions(ctx));
  return { changeSet, ctx, staged, view };
}

afterEach(() => {
  vi.restoreAllMocks();
});

it.each([
  { groupId: "loop", forEachId: "", laneId: "" },
  { groupId: "", forEachId: "each_order", laneId: "" },
])("refuses to create a hook in a container: %j", async (stepCreateContext) => {
  const fetchMock = vi.spyOn(globalThis, "fetch");
  const { ctx, staged, view } = setup({ selectedStep: null, stepCreateContext, stepKindDraft: "invoke_workflow", stepOverrideDraft: JSON.stringify({ invoke_workflow: { hook: { hook_id: "before" } } }) });
  await act(async () => { await view.result.current.handleSaveStep(); });
  expect(staged).toEqual([]);
  expect(fetchMock).not.toHaveBeenCalled();
  expect(ctx.setSaveError).toHaveBeenCalledWith("Hooks belong in the workflow root, before or after a Group or For each.");
});

describe("saving an existing step's parameters", () => {
  it("clears an extension install when replacing it with a system tool", () => {
    const patch = stepConfigPatch({
      stepKindDraft: "connector",
      selectedStep: { raw: { connector_id: "extension.fixture.mail", connector_install_id: "old-install" } },
    }, { connector: { connector_id: "connector.std.text", connector_install_id: "", operation_id: "trim", input_mapping: {}, output_mapping: {}, connection_binding: {} } });
    expect(patch.connector_id).toBe("connector.std.text");
    expect(patch.connector_install_id).toBe("");
    expect(patch.connection_binding_jsonb).toEqual({});
    expect(patch.input_mapping_jsonb).toEqual({});
    expect(patch.output_mapping_jsonb).toEqual({});
  });
  it("sends exactly one update_step_config with the revision on screen", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ workflow_id: "wf_demo", revision: 8, valid: true, issues: [], workflow: {} }),
    );
    const { changeSet, ctx, view } = setup();

    await act(async () => {
      await view.result.current.handleSaveStep();
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toBe("/api/workflows/wf_demo/changes");
    const body = JSON.parse(String(init?.body));
    expect(body.expected_revision).toBe(7);
    expect(body.operations).toHaveLength(1);
    expect(body.operations[0]).toMatchObject({
      op: "update_step_config",
      step_id: "compose_post",
      patch: { display_name: "Compose post", override: { template: { text: "y", result_target: "state.outputs.x" } } },
    });
    expect(body.operations[0].patch).not.toHaveProperty("step_kind");
    expect(body.operations[0].patch).not.toHaveProperty("group_id");
    // The unapplied structure continues from the revision this save produced.
    expect(changeSet.rebase).toHaveBeenCalledWith(8, 7);
    expect(ctx.refetchWorkflowSurfaces).toHaveBeenCalled();
    expect(view.result.current.stepFormSaveState).toBe("saved");
  });

  it("keeps everything on a revision conflict and reports it explicitly", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        { detail: { code: "workflow_revision_conflict", message: "Changed", workflow_id: "wf_demo", expected_revision: 7 } },
        409,
      ),
    );
    const { changeSet, ctx, view } = setup();

    await act(async () => {
      await view.result.current.handleSaveStep();
    });

    expect(changeSet.reportConflict).toHaveBeenCalled();
    expect(changeSet.rebase).not.toHaveBeenCalled();
    expect(ctx.setSaveError).toHaveBeenCalledWith(expect.stringMatching(/changed after it was loaded/));
    expect(view.result.current.stepFormSaveState).toBe("error");
  });

  it("refuses to save a step the unapplied structure already touches", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const { changeSet, ctx, view } = setup();
    changeSet.touchesStep.mockReturnValue(true);

    await act(async () => {
      await view.result.current.handleSaveStep();
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(ctx.setSaveError).toHaveBeenCalledWith(expect.stringMatching(/Apply or discard/));
  });

  it("stages a switch whose cases changed, because its ports are structure", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const { staged, view } = setup({
      selectedStep: {
        local: false,
        raw: { step_id: "route", override: { switch: { selector: "state.x", cases: [{ case_id: "a", when: "a" }] } } },
        runtimeId: "route",
        stepKind: "switch",
      },
      stepDisplayName: "Route",
      stepKindDraft: "switch",
      stepOverrideDraft: JSON.stringify({
        switch: { selector: "state.x", cases: [{ case_id: "a", when: "a" }, { case_id: "case_2", when: "b" }] },
      }),
    });

    await act(async () => {
      await view.result.current.handleSaveStep();
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(staged).toEqual([
      expect.objectContaining({ op: "update_step_config", step_id: "route" }),
    ]);
  });
});

describe("adding a new step", () => {
  it("stages a create_step with a stable new id in the chosen group and an explicit entry", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const { ctx, staged, view } = setup({
      createAsEntry: true,
      selectedStep: null,
      stepCreateContext: { groupId: "loop", laneId: "", insert: null },
    });

    await act(async () => {
      await view.result.current.handleSaveStep();
    });

    expect(fetchMock).not.toHaveBeenCalled();
    // "compose_post" is taken, so the new step gets the next free id.
    expect(staged).toEqual([
      {
        op: "create_step",
        step: expect.objectContaining({
          step_id: "compose_post_2",
          step_kind: "template",
          group_id: "loop",
          lane_id: null,
          display_name: "Compose post",
        }),
      },
      { op: "set_entry", target: { kind: "step", id: "compose_post_2" }, group_id: "loop" },
    ]);
    expect(ctx.setSelectedStepId).toHaveBeenCalledWith("compose_post_2");
  });

  it("inserts a new ordinary step into an existing connection", async () => {
    const { staged, view } = setup({
      selectedStep: null,
      stepCreateContext: {
        groupId: "",
        laneId: "",
        insert: { label: "compose_post · success → route", outcome: { name: "success" }, source: { kind: "step", id: "compose_post" } },
      },
      stepDisplayName: "Check spelling",
      stepKindDraft: "transform",
      stepOverrideDraft: JSON.stringify({ transform: { input: { source: "state.x" }, result_target: "state.transformed" } }),
    });

    await act(async () => {
      await view.result.current.handleSaveStep();
    });

    expect(staged).toEqual([
      {
        op: "insert_step_between",
        source: { kind: "step", id: "compose_post" },
        outcome: { name: "success" },
        step: expect.objectContaining({ step_id: "check_spelling", step_kind: "transform" }),
      },
    ]);
  });

  it("creates a step after a free outcome already connected to it, in the source's For each body", async () => {
    const { changeSet, ctx, staged, view } = setup({
      selectedStep: null,
      stepCreateContext: {
        groupId: "",
        forEachId: "each_order",
        laneId: "",
        insert: { append: true, label: "check_order · success", outcome: { name: "success" }, source: { kind: "step", id: "check_order" } },
      },
      stepDisplayName: "Route order",
      stepKindDraft: "if",
      stepOverrideDraft: JSON.stringify({ if: { selector: "state.order.total", operator: "gt", value: 10 } }),
    });

    await act(async () => {
      await view.result.current.handleSaveStep();
    });

    expect(staged).toEqual([
      {
        op: "create_step",
        step: expect.objectContaining({ step_id: "route_order", step_kind: "if", for_each_id: "each_order", group_id: null, lane_id: null }),
      },
      {
        op: "connect",
        edge: { source: { kind: "step", id: "check_order" }, outcome: { name: "success" }, target: { kind: "step", id: "route_order" } },
      },
    ]);
    expect(ctx.focusCanvasNode).toHaveBeenCalledWith("route_order", { place: false, creation: {
      target: { kind: "step", id: "route_order" }, token: changeSet.stageGraphIntent.mock.results.at(-1)?.value,
      ownerWorkflowId: "wf_demo", viewKey: "", viewEpoch: 0,
    } });
  });

  it("asks the canvas to focus each step it adds, and to place only an unconnected root step", async () => {
    const root = setup({ selectedStep: null, stepCreateContext: { groupId: "", laneId: "" }, stepDisplayName: "Fresh", stepKindDraft: "set_variable", stepOverrideDraft: "{}" });
    await act(async () => {
      await root.view.result.current.handleSaveStep();
    });
    expect(root.ctx.focusCanvasNode).toHaveBeenCalledWith("fresh", { place: true, creation: {
      target: { kind: "step", id: "fresh" }, token: root.changeSet.stageGraphIntent.mock.results.at(-1)?.value,
      ownerWorkflowId: "wf_demo", viewKey: "", viewEpoch: 0,
    } });

    const inGroup = setup({ selectedStep: null, stepCreateContext: { groupId: "loop", laneId: "" }, stepDisplayName: "Inner", stepKindDraft: "set_variable", stepOverrideDraft: "{}" });
    await act(async () => {
      await inGroup.view.result.current.handleSaveStep();
    });
    expect(inGroup.ctx.focusCanvasNode).toHaveBeenCalledWith("inner", { place: false, creation: {
      target: { kind: "step", id: "inner" }, token: inGroup.changeSet.stageGraphIntent.mock.results.at(-1)?.value,
      ownerWorkflowId: "wf_demo", viewKey: "", viewEpoch: 0,
    } });

    const inserted = setup({
      selectedStep: null,
      stepCreateContext: { groupId: "", laneId: "", insert: { label: "compose_post · success → route", outcome: { name: "success" }, source: { kind: "step", id: "compose_post" } } },
      stepDisplayName: "Between",
      stepKindDraft: "set_variable",
      stepOverrideDraft: "{}",
    });
    await act(async () => {
      await inserted.view.result.current.handleSaveStep();
    });
    expect(inserted.staged[0]).toMatchObject({ op: "insert_step_between" });
    expect(inserted.ctx.focusCanvasNode).toHaveBeenCalledWith("between", { place: false, creation: {
      target: { kind: "step", id: "between" }, token: inserted.changeSet.stageGraphIntent.mock.results.at(-1)?.value,
      ownerWorkflowId: "wf_demo", viewKey: "", viewEpoch: 0,
    } });
  });

  it("keeps a not-yet-applied step's parameters in its create operation", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const { changeSet, view } = setup({
      selectedStep: { local: true, raw: { step_id: "draft" }, runtimeId: "draft" },
    });

    await act(async () => {
      await view.result.current.handleSaveStep();
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(changeSet.updateStagedStep).toHaveBeenCalledWith(
      "draft",
      expect.objectContaining({ step_kind: "template", display_name: "Compose post" }),
    );
  });
});

describe("deleting a step", () => {
  it("stages the deletion at once, without a confirmation, and announces it on the canvas with Undo", async () => {
    const nativeConfirm = vi.spyOn(globalThis, "confirm");
    const announced: GraphEditNotice[] = [];
    const listen = (notice: GraphEditNotice) => { announced.push(notice); };
    const { unmount } = renderHook(() => useCanvasToastFeed(listen));
    const { ctx, staged, view } = setup();

    await act(async () => {
      await view.result.current.handleDeleteSelectedStep();
    });

    expect(ctx.requestConfirmation).not.toHaveBeenCalled();
    expect(nativeConfirm).not.toHaveBeenCalled();
    expect(staged).toEqual([{ op: "delete_step", step_id: "compose_post" }]);
    expect(ctx.setSelectedStepId).toHaveBeenCalledWith("");
    expect(announced).toEqual([expect.objectContaining({ label: "compose_post deleted.", tone: "success" })]);
    unmount();
  });

  it("stages a reversible local step deletion instead of dropping its creation", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const { changeSet, ctx, staged, view } = setup({
      selectedStep: { local: true, raw: { step_id: "draft" }, runtimeId: "draft" },
    });

    await act(async () => {
      await view.result.current.handleDeleteSelectedStep();
    });

    expect(ctx.deletionGuard.currentTarget).toHaveBeenCalledWith("draft");
    expect(changeSet.stageGraphIntent).toHaveBeenCalledExactlyOnceWith(
      [{ op: "delete_step", step_id: "draft" }],
      "Step deletion added to the unapplied structure.",
    );
    expect(staged).toEqual([{ op: "delete_step", step_id: "draft" }]);
    expect(changeSet.dropStagedStep).not.toHaveBeenCalled();
    expect(ctx.deletionGuard.requestDelete).not.toHaveBeenCalled();
    expect(ctx.setSelectedStepId).toHaveBeenCalledWith("");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("changing a saved step's kind", () => {
  it("stages change_step_kind after confirming the connections the new kind drops", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const { ctx, staged, view } = setup({
      selectedStep: {
        local: false,
        raw: { step_id: "route", override: { switch: { selector: "state.x", cases: [{ case_id: "a", when: "a" }] } } },
        runtimeId: "route",
        stepKind: "switch",
      },
      stepDisplayName: "Route",
      stepKindDraft: "transform",
      stepOverrideDraft: JSON.stringify({ transform: { input: { source: "state.x" }, result_target: "state.transformed" } }),
    });

    // The kind change asks inline, in the editor's footer, not in a modal.
    let saving: Promise<void> = Promise.resolve();
    await act(async () => {
      saving = view.result.current.handleSaveStep();
    });

    expect(ctx.requestConfirmation).not.toHaveBeenCalled();
    expect(view.result.current.pendingKindChange).toMatchObject({ stepId: "route", dropped: [] });
    expect(staged).toEqual([]);
    await act(async () => {
      view.result.current.pendingKindChange?.resolve(true);
      await saving;
    });

    expect(view.result.current.pendingKindChange).toBeNull();
    expect(view.result.current.stepSaveSerial).toEqual({ count: 1, staged: true });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(staged).toEqual([
      {
        op: "change_step_kind",
        step_id: "route",
        step: expect.objectContaining({
          step_kind: "transform",
          display_name: "Route",
          override: { transform: { input: { source: "state.x" }, result_target: "state.transformed" } },
        }),
      },
    ]);
    const step = (staged[0] as { step: Record<string, unknown> }).step;
    expect(step).not.toHaveProperty("step_id");
    expect(step).not.toHaveProperty("group_id");
    expect(step).not.toHaveProperty("lane_id");
  });

  it("lists the dropped connections and stages nothing when declined", async () => {
    const { ctx, staged, view } = setup({
      stepKindDraft: "stop_success",
      stepOverrideDraft: JSON.stringify({}),
    });

    let saving: Promise<void> = Promise.resolve();
    await act(async () => {
      saving = view.result.current.handleSaveStep();
    });

    expect(view.result.current.pendingKindChange?.dropped).toEqual(["compose_post · success → route"]);
    await act(async () => {
      view.result.current.pendingKindChange?.resolve(false);
      await saving;
    });

    expect(ctx.requestConfirmation).not.toHaveBeenCalled();
    expect(view.result.current.pendingKindChange).toBeNull();
    expect(view.result.current.stepSaveSerial.count).toBe(0);
    expect(staged).toEqual([]);
  });

  it("answers a waiting kind change with Cancel when the editor closes", async () => {
    const { staged, view } = setup({
      stepKindDraft: "stop_success",
      stepOverrideDraft: JSON.stringify({}),
    });
    let saving: Promise<void> = Promise.resolve();
    await act(async () => {
      saving = view.result.current.handleSaveStep();
    });
    expect(view.result.current.pendingKindChange).not.toBeNull();

    await act(async () => { view.unmount(); await saving; });

    expect(staged).toEqual([]);
  });

  it("keeps editing a staged kind change in its operation instead of saving the step", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const { changeSet, staged, view } = setup({ stepKindDraft: "transform", stepOverrideDraft: JSON.stringify({ transform: { input: { source: "state.x" }, result_target: "state.transformed" } }) });
    changeSet.operations.push({ op: "change_step_kind", step_id: "compose_post", step: { step_kind: "transform" } });
    changeSet.touchesStep.mockReturnValue(true);

    await act(async () => {
      await view.result.current.handleSaveStep();
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(staged).toEqual([]);
    expect(changeSet.updateStagedStep).toHaveBeenCalledWith(
      "compose_post",
      expect.objectContaining({ step_kind: "transform", display_name: "Compose post" }),
    );
  });
});

it("refuses a stale editor whose selected step vanished instead of creating a new root step", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch");
  const { ctx, staged, view } = setup({ selectedStep: null, selectedStepId: "removed", stepCreateContext: null });
  await act(async () => { await view.result.current.handleSaveStep(); });
  expect(staged).toEqual([]);
  expect(fetchMock).not.toHaveBeenCalled();
  expect(ctx.setSaveError).toHaveBeenCalledWith(expect.stringMatching(/no longer available|no longer exists/i));
});

function numericActionSetup(kind: string, config: Record<string, unknown>, overrides: Record<string, unknown> = {}) {
  return setup({
    stepKindDraft: kind,
    selectedStep: {
      local: false,
      runtimeId: "compose_post",
      stepKind: kind,
      raw: { step_id: "compose_post", override: { [kind]: config } },
    },
    stepOverrideDraft: JSON.stringify({ [kind]: config }),
    ...overrides,
  });
}

describe("numeric step Save boundary", () => {
  it.each([
    { kind: "wait", config: {} },
    { kind: "wait", config: { duration_ms: "" } },
    { kind: "wait", config: { duration_ms: -1 } },
    { kind: "wait", config: { duration_ms: 1.5 } },
    { kind: "wait", config: { duration_ms: true } },
    { kind: "wait", config: { duration_ms: "0" } },
    { kind: "wait", config: { duration_ms: 31536000001 } },
    { kind: "wait", config: { duration_ms: 0, input_mapping: { duration_ms: "state.delay" } } },
    { kind: "dataset_query", config: { row_limit: 0 } },
    { kind: "dataset_query", config: { row_limit: 1.5 } },
    { kind: "dataset_query", config: { row_limit: "1e" } },
    { kind: "dataset_query", config: { row_limit: "²" } },
    { kind: "dataset_query", config: { row_limit: "⑦" } },
    { kind: "http_request", config: {} },
    { kind: "http_request", config: { timeout_seconds: "" } },
    { kind: "http_request", config: { timeout_seconds: 0 } },
    { kind: "http_request", config: { timeout_seconds: true } },
    { kind: "http_request", config: { timeout_seconds: "١e" } },
    { kind: "http_request", config: { timeout_seconds: 30, max_response_bytes: "1e" } },
    { kind: "http_request", config: { timeout_seconds: 30, max_response_bytes: 0 } },
    { kind: "http_request", config: { timeout_seconds: 30, max_response_bytes: 1.5 } },
    { kind: "http_request", config: { timeout_seconds: 30, max_response_bytes: "4096" } },
    { kind: "http_request", config: { timeout_seconds: 30, max_response_bytes: 5242881 } },
    { kind: "http_request", config: { timeout_seconds: 30, response_mode: "artifact", max_response_bytes: 209715201 } },
  ])("refuses invalid $kind candidate $config before an API write", async ({ kind, config }) => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ revision: 8 }));
    const { ctx, changeSet, staged, view } = numericActionSetup(kind, config);
    await act(async () => { await view.result.current.handleSaveStep(); });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(staged).toEqual([]);
    expect(changeSet.updateStagedStep).not.toHaveBeenCalled();
    expect(changeSet.rebase).not.toHaveBeenCalled();
    expect(ctx.setSaveError).toHaveBeenCalledWith(expect.any(String));
    expect(ctx.setSaveError.mock.calls.at(-1)?.[0]).not.toBe("");
  });

  it.each(["new", "local", "kind-change"])("refuses invalid Wait before changing %s staged structure", async (mode) => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ revision: 8 }));
    const instance = numericActionSetup("wait", { duration_ms: -1 }, mode === "new" ? {
      selectedStep: null,
      stepCreateContext: {},
    } : mode === "local" ? {
      selectedStep: { local: true, runtimeId: "compose_post", stepKind: "wait", raw: { step_id: "compose_post" } },
    } : {});
    if (mode === "kind-change") instance.changeSet.operations.push({
      op: "change_step_kind", step_id: "compose_post", step: { step_kind: "wait", override: { wait: { duration_ms: 0 } } },
    });
    const before = structuredClone(instance.changeSet.operations);
    await act(async () => { await instance.view.result.current.handleSaveStep(); });
    expect(instance.staged).toEqual([]);
    expect(instance.changeSet.updateStagedStep).not.toHaveBeenCalled();
    expect(instance.changeSet.operations).toEqual(before);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(instance.ctx.setSaveError).toHaveBeenCalled();
  });

  it.each([
    { kind: "dataset_query", config: { row_limit: "1e" } },
    { kind: "http_request", config: { timeout_seconds: 30, max_response_bytes: "1e" } },
  ])("refuses incomplete optional $kind input before updating a staged step", async ({ kind, config }) => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ revision: 8 }));
    const { ctx, changeSet, staged, view } = numericActionSetup(kind, config, {
      selectedStep: { local: true, runtimeId: "compose_post", stepKind: kind, raw: { step_id: "compose_post" } },
    });
    const before = structuredClone(changeSet.operations);
    await act(async () => { await view.result.current.handleSaveStep(); });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(staged).toEqual([]);
    expect(changeSet.updateStagedStep).not.toHaveBeenCalled();
    expect(changeSet.operations).toEqual(before);
    expect(changeSet.rebase).not.toHaveBeenCalled();
    expect(ctx.setSaveError).toHaveBeenCalled();
  });

  it("validates overrideInput itself rather than accepting the ordinary valid draft", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ revision: 8 }));
    const { ctx, view } = numericActionSetup("wait", { duration_ms: 0 });
    await act(async () => { await view.result.current.handleSaveStep({ wait: { duration_ms: -1 } }); });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(ctx.setStepOverrideDraft).not.toHaveBeenCalled();
    expect(ctx.setSaveError).toHaveBeenCalled();
  });

  // Mapping values use the established runtime dialect: a literal 0 and a
  // structured source are both valid. Key presence is the authoring XOR rule.
  it.each([
    { kind: "wait", config: { duration_ms: 0 } },
    { kind: "wait", config: { duration_ms: 31536000000 } },
    { kind: "wait", config: { input_mapping: { duration_ms: 0 } } },
    { kind: "wait", config: { input_mapping: { duration_ms: { source: "state.inputs.duration" } } } },
    { kind: "dataset_query", config: { row_limit: "" } },
    { kind: "dataset_query", config: { row_limit: "7" } },
    { kind: "dataset_query", config: { row_limit: "７" } },
    { kind: "dataset_query", config: {} },
    { kind: "http_request", config: { timeout_seconds: 0.01, max_response_bytes: "" } },
    { kind: "http_request", config: { timeout_seconds: "30" } },
    { kind: "http_request", config: { timeout_seconds: "0.01" } },
    { kind: "http_request", config: { timeout_seconds: "12.5" } },
    { kind: "http_request", config: { timeout_seconds: "١.٥" } },
    { kind: "http_request", config: { timeout_seconds: 12.5, response_mode: "artifact", max_response_bytes: 209715200 } },
    { kind: "template", config: { text: "", result_target: "state.result" } },
  ])("preserves valid $kind config $config and its revision envelope", async ({ kind, config }) => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ revision: 8 }));
    const { ctx, changeSet, view } = numericActionSetup(kind, config, { stepDisplayName: "Renamed without changing numeric values" });
    await act(async () => { await view.result.current.handleSaveStep(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse(String(init?.body));
    expect(body.expected_revision).toBe(7);
    expect(body.operations).toHaveLength(1);
    expect(body.operations[0]).toMatchObject({ op: "update_step_config", step_id: "compose_post", patch: { display_name: "Renamed without changing numeric values" } });
    expect(body.operations[0].patch.override).toEqual({ [kind]: config });
    expect(changeSet.rebase).toHaveBeenCalledWith(8, 7);
    expect(ctx.setSaveError).not.toHaveBeenCalled();
  });
});


describe("Transform required values at the save command boundary", () => {
  it.each([
    { missing: "source", blank: "", message: "Source is required." },
    { missing: "source", blank: "   ", message: "Source is required." },
    { missing: "result_target", blank: "", message: "Result target is required." },
    { missing: "result_target", blank: "   ", message: "Result target is required." },
  ])("refuses blank $missing before any API or staged write: '$blank'", async ({ missing, blank, message }) => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ workflow_id: "wf_demo", revision: 8, valid: false, issues: [], workflow: {} }));
    const transform = {
      input: { source: missing === "source" ? blank : "state.inputs.audit" },
      result_target: missing === "result_target" ? blank : "state.audit_transformed",
      fields: [{ value: { literal: false }, to: "flag", type: "boolean", actions: [] }],
    };
    const { changeSet, ctx, staged, view } = setup({
      selectedStep: { local: false, raw: { step_id: "normalize_audit", override: { transform } }, runtimeId: "normalize_audit", stepKind: "transform" },
      stepKindDraft: "transform",
      stepOverrideDraft: JSON.stringify({ transform }),
      stepInputErrors: {},
    });
    // Direct callback invocation deliberately has no rendered-field validation state.
    await act(async () => { await view.result.current.handleSaveStep({ transform }); });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(staged).toEqual([]);
    expect(changeSet.updateStagedStep).not.toHaveBeenCalled();
    expect(ctx.setSaveError).toHaveBeenCalledWith(message);
    expect(ctx.setSaveNotice).not.toHaveBeenCalledWith("Step parameters saved.");
  });

  it("keeps the valid disabled-draft save path and literal false when unrelated readiness issues remain", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({
      workflow_id: "wf_demo", revision: 8, valid: false,
      issues: [{ code: "workflow.agent_missing", message: "An unrelated step needs an agent." }], workflow: { enabled: false },
    }));
    const transform = { input: { source: "state.inputs.audit" }, result_target: "state.audit_transformed", fields: [{ value: { literal: false }, to: "flag", type: "boolean", actions: [] }] };
    const { ctx, view } = setup({
      selectedStep: { local: false, raw: { step_id: "normalize_audit", override: { transform } }, runtimeId: "normalize_audit", stepKind: "transform" },
      stepKindDraft: "transform", stepOverrideDraft: JSON.stringify({ transform }),
    });
    await act(async () => { await view.result.current.handleSaveStep(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body).toMatchObject({ expected_revision: 7, operations: [{ op: "update_step_config", step_id: "normalize_audit", patch: { override: { transform } } }] });
    expect(view.result.current.stepFormSaveState).toBe("saved");
    expect(ctx.setSaveError).not.toHaveBeenCalled();
  });
});


it.each([
  { label: "applied ACK", applied: true, readWorkflow: "wf_demo", readRevision: 8, copies: 1, ack: true, proof: true },
  { label: "explicitly unapplied", applied: false, readWorkflow: "wf_demo", readRevision: 8, copies: 1, ack: false, proof: false },
  { label: "missing applied flag", applied: undefined, readWorkflow: "wf_demo", readRevision: 8, copies: 1, ack: false, proof: false },
  { label: "foreign workflow", applied: true, readWorkflow: "foreign", readRevision: 8, copies: 1, ack: true, proof: false },
  { label: "newer readback revision", applied: true, readWorkflow: "wf_demo", readRevision: 9, copies: 1, ack: true, proof: false },
  { label: "missing step", applied: true, readWorkflow: "wf_demo", readRevision: 8, copies: 0, ack: true, proof: false },
  { label: "ambiguous step", applied: true, readWorkflow: "wf_demo", readRevision: 8, copies: 2, ack: true, proof: false },
])("records HTTP readback authority only for an explicit applied ACK and its exact unique scoped row: $label", async ({ applied, readWorkflow, readRevision, copies, ack, proof }) => {
  const token = { owner: "wf_demo:compose_post:http_request", editor: 1, request: 1, fields: {} };
  const http = { method: "GET", url: "", response_target: "state.response", expected_statuses: [], timeout_seconds: 30 };
  const savedStep = { step_id: "compose_post", step_kind: "http_request", override: { http_request: http } };
  const acknowledgeRequest = vi.fn();
  const recordAcknowledgedReadback = vi.fn();
  const httpIntent = {
    hasRaw: () => false, beginRequest: () => token, isCurrent: () => true, isUnedited: () => true,
    acknowledgeRequest, recordAcknowledgedReadback, endRequest: vi.fn(),
  };
  const transport = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ revision: 8, applied, valid: false, issues: [], workflow: {} }));
  const { ctx, view } = setup({
    httpIntent, selectedStep: { local: false, runtimeId: "compose_post", stepKind: "http_request", raw: savedStep },
    stepKindDraft: "http_request", stepOverrideDraft: JSON.stringify({ http_request: http }),
    refetchWorkflowSurfaces: vi.fn(async () => ({ workflow_id: readWorkflow, revision: readRevision, steps: Array.from({ length: copies }, () => savedStep) })),
  });
  await act(async () => { await view.result.current.handleSaveStep(); });
  expect(transport).toHaveBeenCalledTimes(1);
  expect(ctx.setSaveError).not.toHaveBeenCalled();
  expect(acknowledgeRequest).toHaveBeenCalledTimes(ack ? 1 : 0);
  expect(recordAcknowledgedReadback).toHaveBeenCalledTimes(proof ? 1 : 0);
  if (proof) expect(recordAcknowledgedReadback).toHaveBeenCalledWith(token, {
    workflowId: "wf_demo", revision: 8, signature: `compose_post:${JSON.stringify(savedStep)}`,
  });
});

it("publishes local parameter feedback through its typed journal owner instead of the unowned notice setter", async () => {
  const setStagedStepSaveNotice = vi.fn();
  const noticeOwner = { isCurrent: () => true };
  const instance = setup({ selectedStep: { local: true, raw: { step_id: "draft" }, runtimeId: "draft" }, setStagedStepSaveNotice });
  Object.assign(instance.changeSet, { captureStagedStepNoticeOwner: vi.fn(() => noticeOwner) });
  await act(async () => { await instance.view.result.current.handleSaveStep(); });
  expect(instance.changeSet.updateStagedStep).toHaveBeenCalledWith("draft", expect.any(Object));
  expect(setStagedStepSaveNotice).toHaveBeenCalledWith("The new step's parameters are kept with the unapplied structure.", noticeOwner);
  expect(instance.ctx.setSaveNotice).not.toHaveBeenCalled();
});
