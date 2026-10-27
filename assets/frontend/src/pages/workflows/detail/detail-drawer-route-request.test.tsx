import { act, renderHook, waitFor } from "@testing-library/react";
import { useState } from "react";

import { useWorkflowDrawerController } from "./detail-drawer-controller";
import { normalizeWorkflowStep } from "./detail-workflow-validation";

vi.mock("@/lib/auth/use-is-admin", () => ({ useIsAdmin: () => true }));

function setup(firstAllowed = true, query = "project_id=project-a&drawer=step&step_id=target&step_tab=test-run") {
  let release!: (allowed: boolean) => void;
  const held = new Promise<boolean>((resolve) => { release = resolve; });
  const preparations: Promise<boolean>[] = [];
  const prepareSelection = vi.fn(() => {
    const promise = preparations.length ? held : Promise.resolve(firstAllowed);
    preparations.push(promise);
    return promise;
  });
  const clearRoute = vi.fn();
  const step = normalizeWorkflowStep({ step_id: "target", display_name: "Target step", step_kind: "set_variable", override: {} }, 0);
  const stepRowsById = new Map([["target", step]]);
  const route = new URLSearchParams(query);
  const view = renderHook(({ params, requestKey }: { params: URLSearchParams; requestKey: string }) => {
    const [state, setState] = useState({ mode: "", tab: "smart-setup", stepId: "", label: "" });
    const drawer = useWorkflowDrawerController({
      definitionRows: { stepRowsById, groupRows: [] }, drawerMode: state.mode, drawerTab: state.tab,
      selectedStepId: state.stepId, selectedGroupId: "", searchParams: params, drawerRequestKey: requestKey,
      groupDraft: {}, groupCreateMode: false, prepareSelection,
      confirmDiscardInvalidInput: vi.fn(async () => true), hasInvalidStepInput: false,
      setDrawerMode: (mode: string) => setState((previous) => ({ ...previous, mode })),
      setDrawerTab: (tab: string) => setState((previous) => ({ ...previous, tab })),
      setSelectedStepId: (stepId: string) => setState((previous) => ({ ...previous, stepId })),
      setStepDisplayName: (label: string) => setState((previous) => ({ ...previous, label })),
      setStepCreateContext: vi.fn(), setSelectedGroupId: vi.fn(), setStepKindDraft: vi.fn(),
      setStepToolCategoryDraft: vi.fn(), setStepOverrideDraft: vi.fn(), setStepAgentId: vi.fn(),
      setStepOptional: vi.fn(), setStepBlockKey: vi.fn(), setAutomationScopeKind: vi.fn(),
      setReactionFormVisible: vi.fn(), setSearchParams: clearRoute,
    });
    return { state, changeTab: drawer.changeDrawerTab };
  }, { initialProps: { params: route, requestKey: "workflow-a-first-location" } });
  return { view, route, release, prepareSelection, clearRoute };
}

describe("routed Step Play request lifetime", () => {
  it("opens its initial Test Run once, retains a later manual tab, and renews same-query A→B→A locations", async () => {
    const fixture = setup();
    try {
      await waitFor(() => expect(fixture.view.result.current.state).toMatchObject({ mode: "step", tab: "test-run", stepId: "target" }));
      // The router publishes the already-requested clear after the first drawer
      // commit. A replay admitted before this boundary must not win later.
      fixture.view.rerender({ params: new URLSearchParams("project_id=project-a"), requestKey: "cleared-location" });
      await act(async () => { await fixture.view.result.current.changeTab("smart-setup"); });
      expect(fixture.view.result.current.state.tab).toBe("smart-setup");
      await act(async () => { fixture.release(true); });
      expect(fixture.view.result.current.state.tab).toBe("smart-setup");
      expect(fixture.prepareSelection).toHaveBeenCalledTimes(1);

      // React Router can reuse one search object for different path/history
      // entries; each actual location is a new request, including a return.
      fixture.view.rerender({ params: fixture.route, requestKey: "workflow-b-location" });
      await waitFor(() => expect(fixture.view.result.current.state.tab).toBe("test-run"));
      expect(fixture.prepareSelection).toHaveBeenCalledTimes(2);
      await act(async () => { await fixture.view.result.current.changeTab("smart-setup"); });
      fixture.view.rerender({ params: fixture.route, requestKey: "workflow-a-return-location" });
      await waitFor(() => expect(fixture.view.result.current.state.tab).toBe("test-run"));
      expect(fixture.prepareSelection).toHaveBeenCalledTimes(3);
    } finally { fixture.release(true); fixture.view.unmount(); }
  });

  it("opens a step's Automation tab from a link that names one of its rules", async () => {
    const fixture = setup(true, "project_id=project-a&drawer=step&step_id=target&step_tab=automation&rule=notify_owner");
    try {
      await waitFor(() => expect(fixture.view.result.current.state).toMatchObject({ mode: "step", tab: "automation", stepId: "target" }));
      // The drawer parameters are consumed; the rule key stays for the Automation editor.
      const cleared = fixture.clearRoute.mock.calls.at(-1)?.[0] as URLSearchParams;
      expect(cleared.get("drawer")).toBeNull();
      expect(cleared.get("rule")).toBe("notify_owner");
    } finally { fixture.release(true); fixture.view.unmount(); }
  });

  it("retires a refused routed request without opening the drawer or skipping the next location's guard", async () => {
    const fixture = setup(false);
    try {
      await waitFor(() => expect(fixture.prepareSelection).toHaveBeenCalledTimes(1));
      expect(fixture.view.result.current.state).toEqual({ mode: "", tab: "smart-setup", stepId: "", label: "" });
      expect(fixture.clearRoute).toHaveBeenCalledWith(expect.any(URLSearchParams), { replace: true });
      fixture.view.rerender({ params: new URLSearchParams("project_id=project-a"), requestKey: "refusal-cleared" });
      fixture.view.rerender({ params: fixture.route, requestKey: "fresh-request" });
      await act(async () => { fixture.release(true); });
      await waitFor(() => expect(fixture.view.result.current.state.tab).toBe("test-run"));
      expect(fixture.prepareSelection).toHaveBeenCalledTimes(2);
    } finally { fixture.release(true); fixture.view.unmount(); }
  });
});
