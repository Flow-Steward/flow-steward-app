import { act, renderHook, waitFor } from "@testing-library/react";
import { useState } from "react";

import type { WorkflowStepRow } from "./detail-helpers";
import type { WorkflowHookScope } from "./detail-hook-scope";
import { useWorkflowDrawerController } from "./detail-drawer-controller";
import { useWorkflowHookNavigation } from "./detail-hook-navigation";

// This owner-hook boundary is entered by an authorized author. Page/role and
// actual modal interaction remain covered in the separate mounted17case suite.
vi.mock("@/lib/auth/use-is-admin", () => ({ useIsAdmin: () => true }));

type Owner = { workflowId: string; projectId: string };
type Stage = "invalid" | "parameters";
const ownerA: Owner = { workflowId: "workflow-a", projectId: "project-a" };
const target: WorkflowStepRow = {
  runtimeId: "target", primaryLabel: "Target step", stepKind: "set_variable", stepKey: "target",
  agentId: "", agentLabel: "", blockKey: "", groupId: "", forEachId: "", laneId: "", local: false,
  optional: false, ordinal: 1, secondaryLabel: "", skillLabel: "", skillId: "",
  raw: { step_id: "target", step_kind: "set_variable", override: { set_variable: { target: "state.value", value: 1 } } },
};
function deferred() {
  let resolve: (value: boolean) => void = () => { throw new Error("Uninitialized confirmation"); };
  const promise = new Promise<boolean>((done) => { resolve = done; });
  return { promise, resolve };
}
function setup(stage: Stage, holdAcceptedPreparation = false) {
  const held = deferred();
  const acceptedPreparation = deferred();
  const preparedSettled = vi.fn();
  const invalidConfirmation = vi.fn(() => held.promise);
  const parameterConfirmation = vi.fn(() => stage === "parameters" ? held.promise : Promise.resolve(true));
  const prepared = vi.fn();
  const activateHook = vi.fn(() => true);
  const setSearchParams = vi.fn();
  const writes: Array<{ field: string; value: unknown }> = [];
  const frames: WorkflowHookScope["frames"] = new Map();
  const stepRowsById = new Map([["target", target]]);
  const searchParams = new URLSearchParams();
  const view = renderHook(({ owner }: { owner: Owner }) => {
    const [state, setState] = useState({ drawerMode: "step", drawerTab: "smart-setup",
      selectedStepId: stage === "invalid" ? "target" : "before", label: "Retained unfinished draft", raw: "retained raw draft" });
    const record = (field: string, value: unknown) => { writes.push({ field, value }); };
    // Only the author navigation port is exercised; child policy/creation is not
    // fabricated here. The actual navigation and drawer implementations run.
    const hookScope = { authorWorkflowId: owner.workflowId, frames, framesByHook: frames,
      effectiveHookId: "", childChangeSet: { hasChanges: false, status: "clean" },
      childRows: { definitionRows: { groupRows: [], stepRowsById: new Map() } },
      canActivateHook: () => true, activateHook,
    } as unknown as WorkflowHookScope;
    const navigation = useWorkflowHookNavigation({
      ownerKey: JSON.stringify([owner.workflowId, owner.projectId]), hookScope,
      authorGroupRows: [], authorStructureLocked: false, drawerMode: state.drawerMode,
      selectedGroupId: "", selectedStepId: state.selectedStepId, confirmDiscardParameters: parameterConfirmation,
      searchParams, setSearchParams, setSaveError: () => undefined,
    });
    const drawer = useWorkflowDrawerController({
      advancedMode: false, definitionRows: { stepRowsById, groupRows: [] },
      drawerMode: state.drawerMode, drawerTab: state.drawerTab, groupCreateMode: false,
      groupDraft: { containerKind: "group" }, groupFormDirty: false, savingWorkflowSurface: "",
      searchParams, selectedGroupId: "", selectedStepId: state.selectedStepId,
      confirmDiscardParameters: parameterConfirmation, confirmDiscardInvalidInput: invalidConfirmation,
      hasInvalidStepInput: stage === "invalid", parameterSwitchNeedsConfirmation: true,
      prepareSelection: async (mode: string, id: string) => {
        prepared(mode, id);
        const allowed = await navigation.prepareSelection(mode, id);
        preparedSettled(allowed);
        // An explicit adapter-return boundary AFTER the real guard accepted;
        // no guard result is invented, and no drawer setter has run yet.
        if (allowed && holdAcceptedPreparation) await acceptedPreparation.promise;
        return allowed;
      },
      setDrawerMode: (value: string) => { record("drawerMode", value); setState((current) => ({ ...current, drawerMode: value })); },
      setDrawerTab: (value: string) => { record("drawerTab", value); setState((current) => ({ ...current, drawerTab: value })); },
      setSelectedStepId: (value: string) => { record("selectedStepId", value); setState((current) => ({ ...current, selectedStepId: value })); },
      setStepDisplayName: (value: string) => { record("label", value); setState((current) => ({ ...current, label: value })); },
      setStepOverrideDraft: (value: string) => { record("raw", value); setState((current) => ({ ...current, raw: value })); },
      setStepCreateContext: (value: unknown) => record("createContext", value),
      setSelectedGroupId: (value: unknown) => record("group", value),
      setStepKindDraft: (value: unknown) => record("kind", value),
      setStepToolCategoryDraft: (value: unknown) => record("category", value),
      setStepAgentId: (value: unknown) => record("agent", value),
      setStepOptional: (value: unknown) => record("optional", value),
      setStepBlockKey: (value: unknown) => record("block", value),
      setAutomationScopeKind: (value: unknown) => record("automation", value),
      setReactionFormVisible: (value: unknown) => record("reaction", value),
      setSearchParams,
    });
    return { state, openStep: navigation.canvasNavigation(drawer).openStep };
  }, { initialProps: { owner: ownerA } });
  return { view, held, acceptedPreparation, preparedSettled, invalidConfirmation, parameterConfirmation, prepared, activateHook, setSearchParams, writes };
}
function alternate(dimension: "workflow" | "project"): Owner {
  return dimension === "workflow" ? { ...ownerA, workflowId: "workflow-b" } : { ...ownerA, projectId: "project-b" };
}

describe("contextual Step Play committed owner after confirmation", () => {
  it.each([
    { stage: "invalid", dimension: "workflow" }, { stage: "invalid", dimension: "project" },
    { stage: "parameters", dimension: "workflow" }, { stage: "parameters", dimension: "project" },
  ] as const)("refuses old $dimension A→B→A after awaited $stage confirmation before drawer writes", async ({ stage, dimension }) => {
    const state = setup(stage);
    try {
      let opening: Promise<boolean | void> | undefined;
      act(() => { opening = state.view.result.current.openStep("target", "", "test-run"); });
      await waitFor(() => expect(stage === "invalid" ? state.invalidConfirmation : state.parameterConfirmation).toHaveBeenCalledTimes(1));
      const before = state.view.result.current.state;
      expect(state.writes).toEqual([]);
      if (stage === "invalid") expect(state.prepared).not.toHaveBeenCalled();
      else expect(state.prepared).toHaveBeenCalledWith("step", "target");
      state.view.rerender({ owner: alternate(dimension) });
      expect(state.writes).toEqual([]);
      state.view.rerender({ owner: { ...ownerA } });
      let result: boolean | void = undefined;
      await act(async () => { state.held.resolve(true); result = await opening; });
      expect(result).toBe(false);
      expect(state.view.result.current.state).toEqual(before);
      expect(state.writes).toEqual([]);
      expect(state.setSearchParams).not.toHaveBeenCalled();
      expect(state.activateHook).not.toHaveBeenCalled();
      // Invalid-input retirement must occur BEFORE it starts a new selection
      // guard on the returned owner; rereading a latest ref would admit it.
      if (stage === "invalid") expect(state.prepared).not.toHaveBeenCalled();
    } finally { state.view.unmount(); }
  });

  it("refuses a retired project after real prepareSelection accepted but before its awaited adapter returns", async () => {
    const state = setup("parameters", true);
    try {
      let opening: Promise<boolean | void> | undefined;
      act(() => { opening = state.view.result.current.openStep("target", "", "test-run"); });
      await waitFor(() => expect(state.parameterConfirmation).toHaveBeenCalledTimes(1));
      await act(async () => { state.held.resolve(true); });
      await waitFor(() => expect(state.preparedSettled).toHaveBeenCalledWith(true));
      const before = state.view.result.current.state;
      expect(state.writes).toEqual([]);
      state.view.rerender({ owner: alternate("project") });
      state.view.rerender({ owner: { ...ownerA } });
      let result: boolean | void = undefined;
      await act(async () => { state.acceptedPreparation.resolve(true); result = await opening; });
      expect(result).toBe(false);
      expect(state.writes).toEqual([]);
      expect(state.view.result.current.state).toEqual(before);
      expect(state.setSearchParams).not.toHaveBeenCalled();
      expect(state.activateHook).not.toHaveBeenCalled();
    } finally { state.view.unmount(); }
  });

  it.each(["workflow", "project"] as const)("refuses a captured old $dimension command before any confirmation after A→B→A", async (dimension) => {
    const state = setup("invalid");
    try {
      const oldOpen = state.view.result.current.openStep;
      const before = state.view.result.current.state;
      state.view.rerender({ owner: alternate(dimension) });
      state.view.rerender({ owner: { ...ownerA } });
      let result: boolean | void = undefined;
      await act(async () => { result = await oldOpen("target", "", "test-run"); });
      expect(result).toBe(false);
      expect(state.invalidConfirmation).not.toHaveBeenCalled();
      expect(state.prepared).not.toHaveBeenCalled();
      expect(state.writes).toEqual([]);
      expect(state.view.result.current.state).toEqual(before);
      expect(state.setSearchParams).not.toHaveBeenCalled();
    } finally { state.view.unmount(); }
  });

  it.each(["invalid", "parameters"] as const)("opens the real target test tab when the same owner accepts its held %s guard", async (stage) => {
    const state = setup(stage);
    try {
      let opening: Promise<boolean | void> | undefined;
      act(() => { opening = state.view.result.current.openStep("target", "", "test-run"); });
      await waitFor(() => expect(stage === "invalid" ? state.invalidConfirmation : state.parameterConfirmation).toHaveBeenCalledTimes(1));
      let result: boolean | void = undefined;
      await act(async () => { state.held.resolve(true); result = await opening; });
      expect(result).toBe(true);
      expect(state.view.result.current.state).toMatchObject({ drawerMode: "step", drawerTab: "test-run", selectedStepId: "target" });
      expect(state.prepared).toHaveBeenCalledWith("step", "target");
      if (stage === "invalid") expect(state.view.result.current.state.label).toBe("Retained unfinished draft");
      else expect(state.view.result.current.state.label).toBe("Target step");
      expect(state.setSearchParams).not.toHaveBeenCalled();
    } finally { state.view.unmount(); }
  });
});
