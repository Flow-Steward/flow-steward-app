import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { errorRoutePolicyOperation, useWorkflowCanvasHandlers, useWorkflowDeletionGuard } from "./detail-canvas-handlers";
import { useWorkflowChangeSet } from "./detail-change-set";
import { useWorkflowStepActions } from "./detail-step-actions";
import { applyChangeOperations, definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import { fallbackContext } from "@/lib/i18n/context";
import type { WorkflowChangeOperation } from "@/pages/workflows/builder/api";

const preview = definitionFromWorkflow({
  workflow_id: "wf",
  steps: [
    { step_id: "check", step_kind: "if" },
    { step_id: "yes", step_kind: "agent" },
    { step_id: "no", step_kind: "agent" },
  ],
  groups: [{ group_id: "loop", body: { kind: "sequence" } }],
  settings: {
    execution_graph: {
      schema_version: 1,
      entry: { kind: "step", id: "check" },
      edges: [{ source: { kind: "step", id: "check" }, outcome: { name: "true" }, target: { kind: "step", id: "yes" } }],
    },
  },
});

function setup(confirm = true) {
  const staged: WorkflowChangeOperation[] = [];
  const requestConfirmation = vi.fn(async () => confirm);
  const view = renderHook(() =>
    useWorkflowCanvasHandlers({
      changeSet: {
        preview,
        stage: (next: WorkflowChangeOperation | WorkflowChangeOperation[]) => {
          staged.push(...(Array.isArray(next) ? next : [next]));
        },
        stageGraphIntent: (next: WorkflowChangeOperation[]) => {
          staged.push(...next);
          return { controller: Symbol("test"), serial: staged.length };
        },
      },
      openGroupDrawer: vi.fn(),
      openNewStepDrawer: vi.fn(),
      openStepDrawer: vi.fn(),
      requestConfirmation,
      setAutomationScopeKind: vi.fn(),
      setDrawerMode: vi.fn(),
      setDrawerTab: vi.fn(),
      setSaveNotice: vi.fn(),
      setSelectedCanvasIssue: vi.fn(),
    }),
  );
  return { requestConfirmation, staged, view };
}

it.each([
  { groupId: "loop", forEachId: "", laneId: "" },
  { groupId: "", forEachId: "each_order", laneId: "" },
])("refuses to move a hook into a container: %j", async (placement) => {
  const definition = definitionFromWorkflow({ workflow_id: "wf", steps: [{ step_id: "before", step_kind: "invoke_workflow", override: { invoke_workflow: { hook: { hook_id: "before" } } } }] });
  const staged: WorkflowChangeOperation[] = [];
  const requestConfirmation = vi.fn(async () => true);
  const setSaveError = vi.fn();
  const { result } = renderHook(() => useWorkflowCanvasHandlers({ changeSet: { preview: definition, stage: (operation: WorkflowChangeOperation) => staged.push(operation) }, requestConfirmation, setSaveError, setSaveNotice: vi.fn() }));
  await act(async () => { await result.current.moveStep("before", placement); });
  expect(staged).toEqual([]);
  expect(requestConfirmation).not.toHaveBeenCalled();
  expect(setSaveError).toHaveBeenCalledWith("Hooks belong in the workflow root, before or after a Group or For each.");
});

describe("canvas graph edits stage explicit operations", () => {
  it("stages the policy and error edge together, then restores fail when removing the route", async () => {
    const { staged, view } = setup();
    await act(async () => { await view.result.current.connectPort({ kind: "step", id: "check" }, { name: "error" }, { kind: "step", id: "no" }); });
    expect(staged).toEqual([
      { op: "update_step_config", step_id: "check", patch: { error_policy: { on_error: "route_to_step" } } },
      { op: "connect", edge: { source: { kind: "step", id: "check" }, outcome: { name: "error" }, target: { kind: "step", id: "no" } } },
    ]);
    const connected = applyChangeOperations(preview, staged).definition;
    const fail = errorRoutePolicyOperation(connected, { kind: "step", id: "check" }, "fail", fallbackContext.t);
    const disconnected = applyChangeOperations(connected, [fail, { op: "disconnect", source: { kind: "step", id: "check" }, outcome: { name: "error" } }]).definition;
    expect(disconnected.steps[0].error_policy).toEqual({ on_error: "fail" });
    expect(disconnected.graph.edges.some((edge) => edge.outcome.name === "error")).toBe(false);
  });

  it.each(["retry", "continue", "await_approval"])("refuses to silently replace authored or inherited %s policy", (policy) => {
    const definition = structuredClone(preview);
    definition.steps[0].error_policy = { on_error: policy };
    expect(() => errorRoutePolicyOperation(definition, { kind: "step", id: "check" }, "route_to_step", fallbackContext.t)).toThrow(policy);
    delete definition.steps[0].error_policy;
    definition.steps[0].group_id = "loop";
    definition.groups[0].error_policy = { on_error: policy };
    expect(() => errorRoutePolicyOperation(definition, { kind: "step", id: "check" }, "route_to_step", fallbackContext.t)).toThrow(policy);
    expect(definition.groups[0].error_policy).toEqual({ on_error: policy });
  });
  it("connects a free named port without asking", async () => {
    const { requestConfirmation, staged, view } = setup();
    await act(async () => {
      await view.result.current.connectPort(
        { kind: "step", id: "check" },
        { name: "false" },
        { kind: "step", id: "no" },
      );
    });
    expect(requestConfirmation).not.toHaveBeenCalled();
    expect(staged).toEqual([
      {
        op: "connect",
        edge: { source: { kind: "step", id: "check" }, outcome: { name: "false" }, target: { kind: "step", id: "no" } },
      },
    ]);
  });

  it("replaces an outcome that already leads somewhere only after confirmation", async () => {
    const declined = setup(false);
    await act(async () => {
      await declined.view.result.current.connectPort(
        { kind: "step", id: "check" },
        { name: "true" },
        { kind: "step", id: "no" },
      );
    });
    expect(declined.requestConfirmation).toHaveBeenCalledTimes(1);
    expect(declined.staged).toEqual([]);

    const accepted = setup(true);
    await act(async () => {
      await accepted.view.result.current.connectPort(
        { kind: "step", id: "check" },
        { name: "true" },
        { kind: "step", id: "no" },
      );
    });
    expect(accepted.staged).toEqual([
      {
        op: "connect",
        edge: { source: { kind: "step", id: "check" }, outcome: { name: "true" }, target: { kind: "step", id: "no" } },
        replace: true,
      },
    ]);
  });

  it("removes a connection and moves a step only as staged operations", async () => {
    const { requestConfirmation, staged, view } = setup(true);
    act(() => {
      view.result.current.disconnectPort({ kind: "step", id: "check" }, { name: "true" });
    });
    await act(async () => {
      await view.result.current.moveStep("yes", { groupId: "loop", forEachId: "", laneId: "" });
    });
    expect(requestConfirmation).toHaveBeenCalledWith(
      // Each removed connection is named by its outcome's label (Grouping › Move step); these steps have no display names.
      expect.objectContaining({ details: ["check · True → yes"] }),
    );
    act(() => {
      view.result.current.setEntry({ kind: "step", id: "yes" }, { groupId: "loop" });
    });
    expect(staged).toEqual([
      { op: "disconnect", source: { kind: "step", id: "check" }, outcome: { name: "true" } },
      { op: "move_step", step_id: "yes", group_id: "loop", for_each_id: null, lane_id: null },
      // The loop had no entry yet: the moved step starts it, in the same move (Grouping › Move step).
      { op: "set_entry", target: { kind: "step", id: "yes" }, group_id: "loop" },
      // An explicit entry choice is still its own staged operation.
      { op: "set_entry", target: { kind: "step", id: "yes" }, group_id: "loop" },
    ]);
  });

  it("never stages a removal of a port that has no connection", () => {
    const { staged, view } = setup(true);
    act(() => {
      view.result.current.disconnectPort({ kind: "step", id: "check" }, { name: "false" });
    });
    expect(staged).toEqual([]);
  });
});

/** Supplemental product-boundary checks: the mounted page tests remain Task 1's initial RED. */
describe("guarded editor Delete keeps the latest same-owner draft", () => {
  const deleteCheck: WorkflowChangeOperation = { op: "delete_step", step_id: "check" };
  const otherEdit: WorkflowChangeOperation = { op: "create_step", step: { step_id: "later", step_kind: "stop_success" } };
  const response = (body: unknown) => new Response(JSON.stringify(body), {
    status: 200, headers: { "content-type": "application/json" },
  });
  const scopedGet = () => response({
    workflow_id: "wf", project_id: "project_alpha", scope_kind: "step", scope_id: "check",
    reactions: [], available_trigger_sources: [],
  });
  const dryRun = (revision = 7) => response({
    workflow_id: "wf", revision, valid: true, applied: false,
    issues: [], blocking_issues: [], workflow: {},
  });
  const mounted = () => {
    const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "wf", revision: 7 }));
    return renderHook(({ serverDefinition, revision, structureLocked }) => {
      const changeSet = useWorkflowChangeSet({
        workflowId: "wf", serverDefinition, serverRevision: revision, refetchWorkflowSurfaces,
      });
      const guard = useWorkflowDeletionGuard({
        changeSet,
        owner: {
          workflowId: "wf", projectId: "project_alpha", ownerKey: "wf:project_alpha:check",
          selectionId: "check", serverRevision: revision, structureLocked,
        },
        openProtectedScope: vi.fn(async () => true),
      });
      return { changeSet, guard };
    }, { initialProps: { serverDefinition: preview, revision: 7, structureLocked: false } });
  };

  it("ignores a scoped GET that finishes after another operation joins the same draft", async () => {
    let releaseGet: ((reply: Response) => void) | undefined;
    const heldGet = new Promise<Response>((resolve) => { releaseGet = resolve; });
    const requests: string[] = [];
    const changeBodies: Array<Record<string, unknown>> = [];
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input); requests.push(url);
      if (url.includes("/reactions")) return heldGet;
      if (url.endsWith("/changes")) { changeBodies.push(JSON.parse(String(init?.body || "{}")) as Record<string, unknown>); return dryRun(); }
      throw new Error(`Unexpected request: ${url}`);
    });
    const view = mounted();
    const onAccepted = vi.fn(() => view.result.current.changeSet.stage(deleteCheck));
    try {
      let pending!: Promise<boolean>;
      act(() => { pending = view.result.current.guard.requestDelete({ kind: "step", id: "check" }, onAccepted); });
      expect(requests.some((url) => url.includes("/reactions"))).toBe(true);
      act(() => view.result.current.changeSet.stage(otherEdit));
      await act(async () => { releaseGet?.(scopedGet()); expect(await pending).toBe(false); });
      expect(onAccepted).not.toHaveBeenCalled();
      expect(view.result.current.changeSet.operations).toEqual([otherEdit]);
      expect(changeBodies.some((body) => JSON.stringify(body.operations) === JSON.stringify([deleteCheck]))).toBe(false);
    } finally { view.unmount(); fetchSpy.mockRestore(); }
  });

  it("ignores a successful exact dry run after the local operations change", async () => {
    let releaseDryRun: ((reply: Response) => void) | undefined;
    const heldDryRun = new Promise<Response>((resolve) => { releaseDryRun = resolve; });
    const candidateBodies: Array<Record<string, unknown>> = [];
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/reactions")) return scopedGet();
      if (url.endsWith("/changes")) {
        const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
        if (JSON.stringify(body.operations) === JSON.stringify([deleteCheck])) {
          candidateBodies.push(body); return heldDryRun;
        }
        return dryRun();
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const view = mounted();
    const onAccepted = vi.fn(() => view.result.current.changeSet.stage(deleteCheck));
    try {
      let pending!: Promise<boolean>;
      act(() => { pending = view.result.current.guard.requestDelete({ kind: "step", id: "check" }, onAccepted); });
      await waitFor(() => expect(candidateBodies).toEqual([{ expected_revision: 7, operations: [deleteCheck], dry_run: true }]));
      act(() => view.result.current.changeSet.stage(otherEdit));
      await act(async () => { releaseDryRun?.(dryRun()); expect(await pending).toBe(false); });
      expect(onAccepted).not.toHaveBeenCalled();
      expect(view.result.current.changeSet.operations).toEqual([otherEdit]);
    } finally { view.unmount(); fetchSpy.mockRestore(); }
  });

  it("ignores a dry run from the earlier authoritative revision and preview", async () => {
    let releaseDryRun: ((reply: Response) => void) | undefined;
    const heldDryRun = new Promise<Response>((resolve) => { releaseDryRun = resolve; });
    const candidateBodies: Array<Record<string, unknown>> = [];
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/reactions")) return scopedGet();
      if (url.endsWith("/changes")) {
        const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
        candidateBodies.push(body); return heldDryRun;
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const view = mounted();
    const onAccepted = vi.fn(() => view.result.current.changeSet.stage(deleteCheck));
    try {
      let pending!: Promise<boolean>;
      act(() => { pending = view.result.current.guard.requestDelete({ kind: "step", id: "check" }, onAccepted); });
      await waitFor(() => expect(candidateBodies).toEqual([{ expected_revision: 7, operations: [deleteCheck], dry_run: true }]));
      view.rerender({ serverDefinition: definitionFromWorkflow({
        workflow_id: "wf", steps: [
          { step_id: "check", step_kind: "if", display_name: "New saved definition" },
          { step_id: "yes", step_kind: "agent" }, { step_id: "no", step_kind: "agent" },
        ], groups: [],
      }), revision: 8, structureLocked: false });
      await act(async () => { releaseDryRun?.(dryRun(7)); expect(await pending).toBe(false); });
      expect(onAccepted).not.toHaveBeenCalled();
      expect(view.result.current.changeSet.operations).toEqual([]);
      expect(view.result.current.changeSet.preview.steps[0].display_name).toBe("New saved definition");
    } finally { view.unmount(); fetchSpy.mockRestore(); }
  });

  it("refuses a pending Delete when the same owner becomes structure-locked", async () => {
    let releaseDryRun: ((reply: Response) => void) | undefined;
    const heldDryRun = new Promise<Response>((resolve) => { releaseDryRun = resolve; });
    const candidateBodies: Array<Record<string, unknown>> = [];
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/reactions")) return scopedGet();
      if (url.endsWith("/changes")) {
        const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
        if (JSON.stringify(body.operations) === JSON.stringify([deleteCheck])) {
          candidateBodies.push(body); return heldDryRun;
        }
        return dryRun();
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const view = mounted();
    const onAccepted = vi.fn(() => view.result.current.changeSet.stage(deleteCheck));
    try {
      let pending!: Promise<boolean>;
      act(() => { pending = view.result.current.guard.requestDelete({ kind: "step", id: "check" }, onAccepted); });
      await waitFor(() => expect(candidateBodies).toEqual([{ expected_revision: 7, operations: [deleteCheck], dry_run: true }]));
      view.rerender({ serverDefinition: preview, revision: 7, structureLocked: true });
      await act(async () => { releaseDryRun?.(dryRun()); expect(await pending).toBe(false); });
      expect(view.result.current.guard.currentTarget("check")).toBe(false);
      expect(onAccepted).not.toHaveBeenCalled();
      expect(view.result.current.changeSet.operations).toEqual([]);
    } finally { view.unmount(); fetchSpy.mockRestore(); }
  });

  it("deletes a local Step at once, without a confirmation that a revision change could outlive", async () => {
    const requestConfirmation = vi.fn(async () => true);
    const setSelectedStepId = vi.fn();
    const setSaveNotice = vi.fn();
    const refetchWorkflowSurfaces = vi.fn(async () => ({ workflow_id: "wf", revision: 7 }));
    const view = renderHook(({ revision }) => {
      const changeSet = useWorkflowChangeSet({
        workflowId: "wf", serverDefinition: preview, serverRevision: revision, refetchWorkflowSurfaces,
      });
      const deletionGuard = useWorkflowDeletionGuard({
        changeSet,
        owner: {
          workflowId: "wf", projectId: "project_alpha", ownerKey: "wf:project_alpha:later",
          selectionId: "later", serverRevision: revision, structureLocked: false,
        },
        openProtectedScope: vi.fn(async () => true),
      });
      const stepActions = useWorkflowStepActions({
        changeSet, deletionGuard, requestConfirmation,
        selectedStep: { runtimeId: "later", stepKind: "stop_success", local: true },
        setSelectedStepId, setSaveNotice,
      });
      return { changeSet, stepActions };
    }, { initialProps: { revision: 7 } });
    try {
      act(() => view.result.current.changeSet.stage(otherEdit));
      await waitFor(() => expect(view.result.current.changeSet.preview.steps.some((step) => step.step_id === "later")).toBe(true));
      await act(async () => { await view.result.current.stepActions.handleDeleteSelectedStep(); });
      expect(requestConfirmation).not.toHaveBeenCalled();
      expect(setSelectedStepId).toHaveBeenCalledWith("");
      expect(setSaveNotice).toHaveBeenCalledWith(expect.stringContaining("deletion"));
      expect(view.result.current.changeSet.preview.steps.some((step) => step.step_id === "later")).toBe(false);
    } finally { view.unmount(); }
  });

  it("stages exactly one Delete when the scoped GET, candidate and owner stay unchanged", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/reactions")) return scopedGet();
      if (url.endsWith("/changes")) {
        const body = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
        bodies.push(body); return dryRun();
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const view = mounted();
    const onAccepted = vi.fn(() => view.result.current.changeSet.stage(deleteCheck));
    try {
      await act(async () => {
        expect(await view.result.current.guard.requestDelete({ kind: "step", id: "check" }, onAccepted)).toBe(true);
      });
      expect(onAccepted).toHaveBeenCalledTimes(1);
      expect(view.result.current.changeSet.operations).toEqual([deleteCheck]);
      expect(bodies[0]).toEqual({ expected_revision: 7, operations: [deleteCheck], dry_run: true });
    } finally { view.unmount(); fetchSpy.mockRestore(); }
  });

  it("retires a held checking notice as soon as the same-owner operation proof changes", async () => {
    let releaseGet: ((reply: Response) => void) | undefined;
    const heldGet = new Promise<Response>((resolve) => { releaseGet = resolve; });
    let scopedGets = 0;
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/reactions")) { scopedGets += 1; return heldGet; }
      if (url.endsWith("/changes")) return dryRun(); // The controller may validate the other local edit.
      throw new Error(`Unexpected request: ${url}`);
    });
    const view = mounted();
    const onAccepted = vi.fn(() => view.result.current.changeSet.stage(deleteCheck));
    try {
      let pending!: Promise<boolean>;
      act(() => { pending = view.result.current.guard.requestDelete({ kind: "step", id: "check" }, onAccepted); });
      expect(scopedGets).toBe(1);
      expect(view.result.current.guard.notice?.state).toBe("checking");
      act(() => view.result.current.changeSet.stage(otherEdit));
      expect(view.result.current.guard.notice).toBeNull();
      act(() => view.result.current.guard.retry());
      expect(scopedGets).toBe(1);
      await act(async () => { releaseGet?.(scopedGet()); expect(await pending).toBe(false); });
      expect(view.result.current.guard.notice).toBeNull();
      expect(onAccepted).not.toHaveBeenCalled();
      expect(view.result.current.changeSet.operations).toEqual([otherEdit]);
    } finally { view.unmount(); fetchSpy.mockRestore(); }
  });

  it("removes a failed same-owner Retry when the draft changes without reusing its confirmation", async () => {
    let scopedGets = 0;
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/reactions")) { scopedGets += 1; throw new Error("temporary scoped read failure"); }
      if (url.endsWith("/changes")) return dryRun();
      throw new Error(`Unexpected request: ${url}`);
    });
    const view = mounted();
    const onAccepted = vi.fn(() => view.result.current.changeSet.stage(deleteCheck));
    try {
      await act(async () => {
        expect(await view.result.current.guard.requestDelete({ kind: "step", id: "check" }, onAccepted)).toBe(false);
      });
      expect(view.result.current.guard.notice?.state).toBe("unknown");
      expect(scopedGets).toBe(1);
      act(() => view.result.current.changeSet.stage(otherEdit));
      expect(view.result.current.guard.notice).toBeNull();
      act(() => view.result.current.guard.retry());
      expect(scopedGets).toBe(1);
      expect(onAccepted).not.toHaveBeenCalled();
    } finally { view.unmount(); fetchSpy.mockRestore(); }
  });

  it("retires a Hook-child command proof even when the closed drawer still owns the author", async () => {
    let releaseGet: ((reply: Response) => void) | undefined;
    const heldGet = new Promise<Response>((resolve) => { releaseGet = resolve; });
    let scopedGets = 0;
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/reactions")) { scopedGets += 1; return heldGet; }
      if (url.endsWith("/changes")) return dryRun();
      throw new Error(`Unexpected request: ${url}`);
    });
    const view = renderHook(() => {
      const author = useWorkflowChangeSet({ workflowId: "author", serverDefinition: definitionFromWorkflow({ workflow_id: "author", steps: [], groups: [] }),
        serverRevision: 7, refetchWorkflowSurfaces: async () => ({ workflow_id: "author", revision: 7 }) });
      const child = useWorkflowChangeSet({ workflowId: "wf", serverDefinition: preview, serverRevision: 7,
        refetchWorkflowSurfaces: async () => ({ workflow_id: "wf", revision: 7 }) });
      const guard = useWorkflowDeletionGuard({ changeSet: author,
        owner: { workflowId: "author", projectId: "project_alpha", ownerKey: "author:closed", selectionId: "", serverRevision: 7, structureLocked: false },
        openProtectedScope: vi.fn(async () => true) });
      return { author, child, guard };
    });
    const childOwner = { workflowId: "wf", projectId: "project_alpha", ownerKey: "hook:child:check", selectionId: "check", serverRevision: 7, structureLocked: false };
    const command = { captured: { changeSet: view.result.current.child, owner: childOwner },
      current: () => ({ changeSet: view.result.current.child, owner: childOwner }) };
    const onAccepted = vi.fn(() => view.result.current.child.stage(deleteCheck));
    try {
      let pending!: Promise<boolean>;
      act(() => { pending = view.result.current.guard.requestDelete({ kind: "step", id: "check" }, onAccepted, command); });
      expect(scopedGets).toBe(1);
      expect(view.result.current.guard.notice?.state).toBe("checking");
      act(() => view.result.current.child.stage(otherEdit));
      expect(view.result.current.guard.notice).toBeNull();
      await act(async () => { releaseGet?.(scopedGet()); expect(await pending).toBe(false); });
      expect(onAccepted).not.toHaveBeenCalled();
      expect(view.result.current.child.operations).toEqual([otherEdit]);
      expect(view.result.current.author.operations).toEqual([]);
    } finally { view.unmount(); fetchSpy.mockRestore(); }
  });

  it("authorizes one exact two-root candidate without staging during a Cut-style preflight", async () => {
    const scopeGets: string[] = [];
    const bodies: Array<Record<string, unknown>> = [];
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname.endsWith("/reactions")) {
        const scopeId = url.searchParams.get("scope_id") || "";
        scopeGets.push(scopeId);
        return response({ workflow_id: "wf", project_id: "project_alpha", scope_kind: "step", scope_id: scopeId,
          reactions: [], available_trigger_sources: [] });
      }
      if (url.pathname.endsWith("/changes")) {
        bodies.push(JSON.parse(String(init?.body || "{}")) as Record<string, unknown>);
        return dryRun();
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    const view = mounted();
    const selection = { canvasIds: ["check", "yes"], generation: 3, ownerScopeKey: "author:wf" };
    const batchOwner = { workflowId: "wf", projectId: "project_alpha", ownerKey: "author:wf:batch:3", selectionId: "batch",
      serverRevision: 7, structureLocked: false };
    const command = {
      captured: { changeSet: view.result.current.changeSet, owner: batchOwner },
      current: () => ({ changeSet: view.result.current.changeSet, owner: batchOwner }),
      selection, currentSelection: () => selection,
    };
    let acceptedOperations: WorkflowChangeOperation[] | null = null;
    const accepted = vi.fn((batch: { operations: WorkflowChangeOperation[] }) => { acceptedOperations = batch.operations; });
    try {
      await act(async () => {
        expect(await view.result.current.guard.requestDeleteBatch([
          { kind: "step", id: "check" }, { kind: "step", id: "yes" },
        ], accepted, command)).toBe(true);
      });
      expect(scopeGets).toEqual(["check", "yes"]);
      expect(bodies).toEqual([{ expected_revision: 7, dry_run: true, operations: [
        { op: "delete_step", step_id: "check" }, { op: "delete_step", step_id: "yes" },
      ] }]);
      expect(accepted).toHaveBeenCalledTimes(1);
      expect(acceptedOperations).toEqual(bodies[0].operations);
      expect(view.result.current.changeSet.operations).toEqual([]); // preflight never stages or writes.
    } finally { view.unmount(); fetchSpy.mockRestore(); }
  });
});
