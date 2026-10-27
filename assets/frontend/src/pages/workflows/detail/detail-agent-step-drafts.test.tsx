import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { definitionFromWorkflow } from "@/pages/workflows/builder/change-set";
import { useWorkflowStepActions } from "./detail-step-actions";
import { useWorkflowStepRendererController } from "./detail-step-renderer-controller";
import { useAgentStepDrafts } from "./detail-agent-step-drafts";
import { useWorkflowChangeSet } from "./detail-change-set";
import type { WorkflowStep } from "@/pages/workflows/shared/api";

const raw: WorkflowStep & { override: { block_key: string } } = { agent_id: "writer", participant_key: "orphan", input_mapping_jsonb: { value: null, flag: false }, output_mapping_jsonb: {}, override: { block_key: "block.a" } };
it("keeps untouched maps/orphan key out of wire patches, but explicit typed edit and Clear are full canonical objects", () => {
  const view = renderHook(() => useAgentStepDrafts({ owner: "workflow:A:agent", raw, createContext: undefined }));
  expect(view.result.current.snapshot("writer", raw.override, [])).toEqual({ agent_id: "writer" });
  act(() => view.result.current.editMap("input", '{"value":null,"flag":false,"zero":0,"text":"001"}'));
  expect(view.result.current.snapshot("writer", raw.override, []).input_mapping_jsonb).toEqual({ value: null, flag: false, zero: 0, text: "001" });
  act(() => view.result.current.editMap("input", "{}"));
  expect(view.result.current.snapshot("writer", raw.override, []).input_mapping_jsonb).toEqual({});
});
it("refuses invalid raw snapshot and preserves it against unrelated readback", () => {
  const view = renderHook(({ data }) => useAgentStepDrafts({ owner: "A", raw: data, createContext: undefined }), { initialProps: { data: raw } });
  act(() => view.result.current.editMap("input", '{"value":'));
  expect(() => view.result.current.snapshot("writer", {}, [])).toThrow();
  view.rerender({ data: { ...raw, input_mapping_jsonb: { value: "server" } } });
  expect(view.result.current.maps.input.text).toBe('{"value":'); expect(view.result.current.invalid).toBe(true);
});
it("rejects owner ABA and prevents newer raw/Advanced drafts from accepting an old committed readback", () => {
  const view = renderHook(({ owner }) => useAgentStepDrafts({ owner, raw, createContext: undefined }), { initialProps: { owner: "A" } });
  const old = view.result.current.capture(); view.rerender({ owner: "B" }); view.rerender({ owner: "A" });
  expect(view.result.current.isCurrent(old)).toBe(false);
  let request: ReturnType<typeof view.result.current.beginRequest> | undefined;
  act(() => { request = view.result.current.beginRequest(); view.result.current.touchOverride(); });
  expect(view.result.current.isCurrent(request!)).toBe(true); expect(view.result.current.isUnedited(request!)).toBe(false); expect(view.result.current.mayReconcileReadback()).toBe(false);
  act(() => view.result.current.reconcile({ ...raw, input_mapping_jsonb: { wrong: "old ACK" } }));
  expect(view.result.current.maps.input.text).not.toContain("old ACK");
});
it("accepts its own unchanged ACK without retiring the submission token, and old endRequest cannot retire a new request", () => {
  const view = renderHook(() => useAgentStepDrafts({ owner: "A", raw, createContext: undefined }));
  act(() => view.result.current.editMap("input", '{"value":null}'));
  let old: ReturnType<typeof view.result.current.beginRequest> | undefined;
  act(() => { old = view.result.current.beginRequest(); view.result.current.reconcile({ ...raw, input_mapping_jsonb: { value: null } }); });
  expect(view.result.current.isUnedited(old!)).toBe(true); expect(view.result.current.dirty).toBe(false);
  let current: ReturnType<typeof view.result.current.beginRequest> | undefined;
  act(() => { current = view.result.current.beginRequest(); view.result.current.endRequest(old!); view.result.current.touchOverride(); });
  expect(view.result.current.isCurrent(current!)).toBe(true); expect(view.result.current.mayReconcileReadback()).toBe(false);
});

it("admits the actual kind controller only after raw Cancel/Discard and rejects owner ABA during its held confirmation", async () => {
  let release: ((value: boolean) => void) | undefined;
  const confirm = vi.fn(() => new Promise<boolean>((resolve) => { release = resolve; }));
  const setKind = vi.fn();
  const view = renderHook(({ owner }) => {
    const drafts = useAgentStepDrafts({ owner, raw, createContext: undefined });
    const utils = useWorkflowStepRendererController({ agentDrafts: drafts, stepKindDraft: "agent", stepOverrideDraft: "{}", stepKindConfig: {}, selectedStep: { runtimeId: "A" }, confirmDiscardInvalidInput: confirm, setStepKindDraft: setKind, setStepToolCategoryDraft: vi.fn(), setStepOverrideDraft: vi.fn() });
    return { drafts, renderer: utils };
  }, { initialProps: { owner: "A" } });
  act(() => view.result.current.drafts.editMap("input", '{"value":'));
  let attempt: Promise<void> | undefined;
  act(() => { attempt = view.result.current.renderer.handleStepKindChange("stop_success"); });
  expect(confirm).toHaveBeenCalledWith(true);
  await act(async () => { release?.(false); await attempt; });
  expect(setKind).not.toHaveBeenCalled(); expect(view.result.current.drafts.invalid).toBe(true);
  act(() => { attempt = view.result.current.renderer.handleStepKindChange("stop_success"); });
  await act(async () => { release?.(true); await attempt; });
  expect(setKind).toHaveBeenCalledExactlyOnceWith("stop_success"); expect(view.result.current.drafts.invalid).toBe(false);
  setKind.mockClear(); act(() => view.result.current.drafts.editMap("input", '{"value":'));
  act(() => { attempt = view.result.current.renderer.handleStepKindChange("stop_success"); });
  view.rerender({ owner: "B" }); view.rerender({ owner: "A" });
  await act(async () => { release?.(true); await attempt; });
  expect(setKind).not.toHaveBeenCalled();
});

it.each(["accepted", "owner ABA", "newer map", "newer Advanced", "newer request"])("fences and acknowledges actual held kind-to-Agent staging: %s", async (scenario) => {
  const definition = definitionFromWorkflow({ workflow_id: "wf", steps: [{ step_id: "A", step_kind: "template", override: { template: { text: "old" } } }] });
  const view = renderHook(({ owner }) => {
    const changeSet = useWorkflowChangeSet({ workflowId: "wf", serverDefinition: definition, serverRevision: 7, refetchWorkflowSurfaces: async () => undefined });
    const drafts = useAgentStepDrafts({ owner, raw: definition.steps[0], createContext: undefined });
    const actions = useWorkflowStepActions({ agentDrafts: drafts, agentWorkflowRecord: {}, stepKindDraft: "agent", stepAgentId: "writer", stepDisplayName: "Agent", stepOverrideDraft: "{}", workflowId: "wf", selectedStep: { local: false, runtimeId: "A", stepKind: "template", raw: definition.steps[0] }, changeSet, setSaveError: vi.fn(), setSaveNotice: vi.fn() });
    return { drafts, actions, changeSet };
  }, { initialProps: { owner: "A" } });
  act(() => { view.result.current.drafts.editDirect(); view.result.current.drafts.editMap("input", '{"accepted":false}'); });
  let attempt: Promise<void> | undefined;
  act(() => { attempt = view.result.current.actions.handleSaveStep(); });
  await waitFor(() => expect(view.result.current.actions.pendingKindChange).toMatchObject({ stepId: "A", dropped: [] }));
  const held = view.result.current.actions.pendingKindChange;
  if (scenario === "owner ABA") { view.rerender({ owner: "B" }); view.rerender({ owner: "A" }); }
  else if (scenario === "newer map") act(() => view.result.current.drafts.editMap("input", '{"newer":'));
  else if (scenario === "newer Advanced") act(() => view.result.current.drafts.touchOverride());
  else if (scenario === "newer request") act(() => { view.result.current.drafts.beginRequest(); });
  await act(async () => { held?.resolve(true); await attempt; });
  expect(view.result.current.actions.pendingKindChange).toBeNull();
  if (scenario === "accepted") {
    expect(view.result.current.changeSet.preview.steps[0]).toMatchObject({ step_id: "A", step_kind: "agent", agent_id: "writer", input_mapping_jsonb: { accepted: false }, override: {} });
    expect(view.result.current.changeSet.preview.steps[0]).not.toHaveProperty("override.template");
    expect(view.result.current.drafts.dirty).toBe(false);
    expect(JSON.parse(view.result.current.drafts.maps.input.text)).toEqual({ accepted: false });
  } else {
    expect(view.result.current.changeSet.operations).toEqual([]);
    if (scenario === "newer map") { expect(view.result.current.drafts.maps.input.text).toBe('{"newer":'); expect(view.result.current.drafts.invalid).toBe(true); }
    if (scenario !== "owner ABA") expect(view.result.current.drafts.dirty).toBe(true);
  }
});

it("leaves unsupported loaded map data untouched until an explicit object repair", () => {
  const unsupported = { input_mapping_jsonb: null, output_mapping_jsonb: ["legacy"], override: {} };
  const view = renderHook(() => useAgentStepDrafts({ owner: "unsupported", raw: unsupported, createContext: undefined }));
  expect(view.result.current.maps.input.text).toBe("null"); expect(view.result.current.maps.output.text).toContain("legacy");
  expect(view.result.current.snapshot("writer", {}, [])).toEqual({ agent_id: "writer" });
  act(() => view.result.current.editMap("input", "null")); expect(() => view.result.current.snapshot("writer", {}, [])).toThrow();
  act(() => view.result.current.editMap("input", "{}")); expect(view.result.current.snapshot("writer", {}, [])).toEqual({ agent_id: "writer", input_mapping_jsonb: {} });
});

it("refuses an actual held Save ACK after owner ABA before shared revision/status publication", async () => {
  let release: ((response: Response) => void) | undefined;
  const transport = vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>((resolve) => { release = resolve; }));
  const rebase = vi.fn(); const refetch = vi.fn(); const notice = vi.fn();
  const view = renderHook(({ owner }) => {
    const drafts = useAgentStepDrafts({ owner, raw, createContext: undefined });
    const actions = useWorkflowStepActions({ agentDrafts: drafts, agentWorkflowRecord: {}, stepKindDraft: "agent", stepAgentId: "writer", stepDisplayName: "Agent", stepOverrideDraft: "{}", workflowId: "wf", serverRevision: 7, selectedStep: { local: false, runtimeId: "A", stepKind: "agent", raw }, changeSet: { operations: [], touchesStep: () => false, rebase }, refetchWorkflowSurfaces: refetch, setSaveError: vi.fn(), setSaveNotice: notice, setSavingWorkflowSurface: vi.fn() });
    return { drafts, actions };
  }, { initialProps: { owner: "A" } });
  try {
    let attempt: Promise<void> | undefined;
    act(() => { attempt = view.result.current.actions.handleSaveStep(); }); expect(transport).toHaveBeenCalledTimes(1);
    view.rerender({ owner: "B" }); view.rerender({ owner: "A" });
    await act(async () => { release?.(new Response(JSON.stringify({ revision: 8, applied: true, issues: [] }), { status: 200 })); await attempt; });
    expect(rebase).not.toHaveBeenCalled(); expect(refetch).not.toHaveBeenCalled(); expect(notice).not.toHaveBeenCalledWith("Step parameters saved.");
  } finally { transport.mockRestore(); }
});


it.each([
  ["matching acknowledged row", "wf", 8, 1, true, false],
  ["foreign workflow", "foreign", 8, 1, true, true],
  ["different revision", "wf", 9, 1, true, true],
  ["ambiguous duplicate row", "wf", 8, 2, true, true],
  ["unapplied response", "wf", 8, 1, false, true],
] as const)("reconciles a delayed Agent Save readback only for its %s", async (_case, readWorkflow, readRevision, copies, applied, remainsDirty) => {
  const saved = { ...raw, step_id: "A", input_mapping_jsonb: { value: null, flag: false, zero: 0 } };
  const transport = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ revision: 8, applied, issues: [] }), { status: 200 }));
  const readback = { workflow_id: readWorkflow, revision: readRevision, steps: Array.from({ length: copies }, () => saved) };
  const refetch = vi.fn(async () => readback);
  const view = renderHook(({ data }) => {
    const drafts = useAgentStepDrafts({ owner: "wf:A:agent", raw: data, createContext: undefined });
    const actions = useWorkflowStepActions({ agentDrafts: drafts, agentWorkflowRecord: {}, stepKindDraft: "agent", stepAgentId: "writer", stepDisplayName: "Agent", stepOverrideDraft: "{}", workflowId: "wf", serverRevision: 7, selectedStep: { local: false, runtimeId: "A", stepKind: "agent", raw: data }, changeSet: { operations: [], touchesStep: () => false, rebase: vi.fn() }, refetchWorkflowSurfaces: refetch, setSaveError: vi.fn(), setSaveNotice: vi.fn(), setSavingWorkflowSurface: vi.fn() });
    return { drafts, actions };
  }, { initialProps: { data: raw } });
  try {
    act(() => view.result.current.drafts.editMap("input", '{"value":null,"flag":false,"zero":0}'));
    await act(async () => view.result.current.actions.handleSaveStep());
    expect(refetch).toHaveBeenCalledOnce();
    expect(view.result.current.drafts.dirty).toBe(remainsDirty);
    // Query notifications may commit after Save's finally; only the verified ACK may retire this draft.
    view.rerender({ data: saved });
    expect(view.result.current.drafts.dirty).toBe(remainsDirty);
    expect(JSON.parse(view.result.current.drafts.maps.input.text)).toEqual({ value: null, flag: false, zero: 0 });
    expect(transport).toHaveBeenCalledOnce();
  } finally { view.unmount(); transport.mockRestore(); }
});

it("acknowledges the exact local canonical baseline and retires older server requests", () => {
  const view = renderHook(() => useAgentStepDrafts({ owner: "wf:hook-a:A", raw, createContext: undefined }));
  const canonical = { ...raw, agent_id: "reviewer", participant_key: "reviewer", input_mapping_jsonb: { flag: false, zero: 0, nullable: null, code: "001" }, output_mapping_jsonb: { result: "state.result" } };
  let request: ReturnType<typeof view.result.current.capture>;
  act(() => {
    view.result.current.editParticipant("reviewer", "reviewer");
    view.result.current.editMap("input", JSON.stringify(canonical.input_mapping_jsonb));
    view.result.current.editMap("output", JSON.stringify(canonical.output_mapping_jsonb));
    view.result.current.touchBlock(); view.result.current.touchOverride();
    request = view.result.current.beginRequest();
  });
  const local = view.result.current.capture();
  act(() => expect(view.result.current.acknowledgeStaged(local, canonical)).toBe(true));
  expect(view.result.current.dirty).toBe(false);
  expect(view.result.current.mode).toBe("participant"); expect(view.result.current.participantKey).toBe("reviewer");
  expect(JSON.parse(view.result.current.maps.input.text)).toEqual({ flag: false, zero: 0, nullable: null, code: "001" });
  expect(JSON.parse(view.result.current.maps.output.text)).toEqual({ result: "state.result" });
  expect(view.result.current.isCurrent(request!)).toBe(false);
  expect(view.result.current.snapshot("reviewer", raw.override, [])).toEqual({ agent_id: "reviewer" });
  act(() => view.result.current.editMap("input", '{"newer":'));
  act(() => view.result.current.reconcile({ ...raw, input_mapping_jsonb: { old: "ACK" } }));
  expect(view.result.current.dirty).toBe(true); expect(view.result.current.invalid).toBe(true);
  expect(view.result.current.maps.input.text).toBe('{"newer":');
  act(() => expect(view.result.current.acknowledgeStaged(local, canonical)).toBe(false));
});

it.each(["newer edit", "workflow", "Hook", "editor", "request"])("rejects local acknowledgment after %s identity or intent advances", (boundary) => {
  const view = renderHook(({ owner }) => useAgentStepDrafts({ owner, raw, createContext: undefined }), { initialProps: { owner: "wf:hook-a:A" } });
  act(() => view.result.current.editMap("input", '{"newer":'));
  const token = view.result.current.capture();
  if (boundary === "workflow" || boundary === "Hook") {
    view.rerender({ owner: boundary === "workflow" ? "other:hook-a:A" : "wf:hook-b:A" });
    view.rerender({ owner: "wf:hook-a:A" });
  } else act(() => { if (boundary === "editor") view.result.current.reset(raw); else if (boundary === "request") view.result.current.beginRequest(); });
  if (boundary !== "request") act(() => view.result.current.editMap("input", '{"newer":'));
  act(() => expect(view.result.current.acknowledgeStaged(token, raw)).toBe(false));
  expect(view.result.current.maps.input.text).toBe('{"newer":');
  expect(view.result.current.invalid).toBe(true); expect(view.result.current.dirty).toBe(true);
});


it.each(["local", "staged kind"])("acknowledges accepted %s Agent updates and same-value edits without durable saves", async (mode) => {
  const definition = definitionFromWorkflow({ workflow_id: "wf", steps: mode === "local" ? [] : [{ step_id: "A", step_kind: "template", override: { template: { text: "old" } } }] });
  const view = renderHook(() => {
    const changeSet = useWorkflowChangeSet({ workflowId: "wf", serverDefinition: definition, serverRevision: 7, refetchWorkflowSurfaces: async () => undefined });
    const current = changeSet.preview.steps.find((step) => step.step_id === "A") || {};
    const drafts = useAgentStepDrafts({ owner: "wf:A", raw: current, createContext: undefined });
    const actions = useWorkflowStepActions({ agentDrafts: drafts, agentWorkflowRecord: {}, stepKindDraft: "agent", stepAgentId: "writer", stepDisplayName: "Agent", stepOverrideDraft: "{}", workflowId: "wf", selectedStep: { local: mode === "local", runtimeId: "A", stepKind: "agent", raw: current }, changeSet, setSaveError: vi.fn(), setSaveNotice: vi.fn() });
    return { drafts, actions, changeSet };
  });
  act(() => view.result.current.changeSet.stage(mode === "local" ? { op: "create_step", step: { step_id: "A", step_kind: "agent", agent_id: "writer" } } : { op: "change_step_kind", step_id: "A", step: { step_kind: "agent", agent_id: "writer" } }));
  act(() => { view.result.current.drafts.editDirect(); view.result.current.drafts.editMap("input", '{"flag":false,"zero":0,"nullable":null,"code":"001"}'); });
  await act(async () => view.result.current.actions.handleSaveStep());
  expect(view.result.current.drafts.dirty).toBe(false);
  expect(view.result.current.changeSet.preview.steps[0]).toMatchObject({ step_kind: "agent", agent_id: "writer", input_mapping_jsonb: { flag: false, zero: 0, nullable: null, code: "001" } });
  const journal = view.result.current.changeSet.journal;
  act(() => view.result.current.drafts.editMap("input", '{"flag":false,"zero":0,"nullable":null,"code":"001"}'));
  await act(async () => view.result.current.actions.handleSaveStep());
  expect(view.result.current.drafts.dirty).toBe(false);
  expect(view.result.current.changeSet.journal).toBe(journal);
  expect(view.result.current.actions.stepSaveSerial).toEqual({ count: 2, staged: true });
});

it.each(["local", "staged kind", "confirmed kind"])("keeps Agent drafts dirty when %s staging is rejected", async (mode) => {
  const definition = definitionFromWorkflow({ workflow_id: "wf", steps: [{ step_id: "A", step_kind: "template" }] });
  const notice = vi.fn();
  const view = renderHook(() => {
    const drafts = useAgentStepDrafts({ owner: "wf:A", raw, createContext: undefined });
    const actions = useWorkflowStepActions({ agentDrafts: drafts, agentWorkflowRecord: {}, stepKindDraft: "agent", stepAgentId: "writer", stepDisplayName: "Agent", stepOverrideDraft: "{}", workflowId: "wf", selectedStep: { local: mode === "local", runtimeId: "A", stepKind: mode === "confirmed kind" ? "template" : "agent", raw }, changeSet: { preview: definition, operations: mode === "staged kind" ? [{ op: "change_step_kind", step_id: "A" }] : [], updateStagedStep: () => null, stageGraphIntent: () => null }, setSaveError: vi.fn(), setSaveNotice: notice });
    return { drafts, actions };
  });
  act(() => view.result.current.drafts.editMap("input", '{"retained":false}'));
  let attempt: Promise<void> | undefined;
  act(() => { attempt = view.result.current.actions.handleSaveStep(); });
  if (mode === "confirmed kind") {
    await waitFor(() => expect(view.result.current.actions.pendingKindChange).not.toBeNull());
    await act(async () => { view.result.current.actions.pendingKindChange?.resolve(true); await attempt; });
  } else await act(async () => attempt);
  expect(view.result.current.drafts.dirty).toBe(true);
  expect(view.result.current.drafts.maps.input.text).toBe('{"retained":false}');
  expect(view.result.current.actions.stepSaveSerial.count).toBe(0);
  expect(notice).not.toHaveBeenCalled();
});
