import { act, renderHook } from "@testing-library/react";
import { useHttpRequestIntent } from "./detail-http-request-intent";
import { useWorkflowDetailDomBridge } from "./detail-dom-bridge";

it("rejects owner, request and field ABA even when values/IDs return", () => {
  const callback = vi.fn();
  const view = renderHook(({ owner }) => useHttpRequestIntent(owner, callback), { initialProps: { owner: "A" } });
  let token = view.result.current.capture();
  act(() => { view.result.current.touchField("url"); view.result.current.touchField("url"); });
  expect(view.result.current.isCurrent(token, "url")).toBe(false);
  token = view.result.current.capture();
  view.rerender({ owner: "B" }); view.rerender({ owner: "A" });
  expect(view.result.current.isCurrent(token)).toBe(false);
  token = view.result.current.beginRequest();
  view.result.current.beginRequest();
  expect(view.result.current.isCurrent(token)).toBe(false);
});
it("prevents queued old registration insertion and cleanup from touching a newer editor", () => {
  const pending: Array<() => void> = [];
  const errors: Record<string, string> = {};
  const view = renderHook(() => useHttpRequestIntent("A", (name, message, current) => {
    pending.push(() => { if (current && !current()) return; if (message) errors[name] = message; else delete errors[name]; });
  }));
  const old = view.result.current.registration("field");
  act(() => view.result.current.raw(old, "old invalid"));
  act(() => view.result.current.beginEditorSession());
  const newer = view.result.current.registration("field");
  act(() => view.result.current.raw(newer, "new invalid"));
  act(() => view.result.current.release(old));
  act(() => pending.forEach((publish) => publish()));
  expect(errors).toEqual({ [newer.name]: "new invalid" });
  expect(view.result.current.hasRaw()).toBe(true);
});

it("hands off its recorded own readback but protects a newer raw or typed edit", () => {
  const view = renderHook(() => useHttpRequestIntent("A", () => undefined));
  const token = view.result.current.beginRequest();
  expect(view.result.current.ownsPendingReadback()).toBe(true);
  expect(view.result.current.mayReconcileReadback()).toBe(true);
  act(() => view.result.current.touchField("url"));
  expect(view.result.current.ownsPendingReadback()).toBe(false);
  expect(view.result.current.mayReconcileReadback()).toBe(false);
  act(() => view.result.current.endRequest(token));
  const registration = view.result.current.registration("status");
  act(() => view.result.current.raw(registration, "partial CSV"));
  expect(view.result.current.mayReconcileReadback()).toBe(false);
});

it("checks per-control publication generation when old error and clear callbacks are queued", () => {
  const pending: Array<() => void> = [];
  const errors: Record<string,string> = {};
  const view = renderHook(() => useHttpRequestIntent("A", (name, message, current) => {
    pending.push(() => { if (current && !current()) return; if (message) errors[name] = message; else delete errors[name]; });
  }));
  const reg = view.result.current.registration("same-field");
  act(() => { view.result.current.raw(reg, "first"); view.result.current.raw(reg, ""); view.result.current.raw(reg, "newer"); });
  act(() => pending.reverse().forEach((publish) => publish()));
  expect(errors).toEqual({ [reg.name]: "newer" });
});

it("retires conditional field errors when their auth or response-mode dependency changes", () => {
  const view = renderHook(() => useHttpRequestIntent("A", () => undefined));
  const token = view.result.current.beginRequest();
  act(() => view.result.current.touchField("auth_mode"));
  expect(view.result.current.isCurrent(token, "auth_ref")).toBe(false);
  expect(view.result.current.isCurrent(token, "url")).toBe(false);
  act(() => view.result.current.touchField("response_mode"));
  expect(view.result.current.isCurrent(token, "max_response_bytes")).toBe(false);
});

it("retires every field proof when Advanced override text is edited", () => {
  const view = renderHook(() => useHttpRequestIntent("A", () => undefined));
  const token = view.result.current.beginRequest();
  act(() => view.result.current.touchOverride());
  expect(view.result.current.isCurrent(token, "url")).toBe(false);
  expect(view.result.current.isCurrent(token, "headers")).toBe(false);
});


it.each(["row metadata", "revision", "workflow"] as const)("keeps acknowledged HTTP feedback through delayed readback, then retires it on a later %s change", (change) => {
  const original = { step_id: "http_a", step_kind: "http_request", override: { http_request: { url: "before" } } };
  const acknowledged = { ...original, override: { http_request: { url: "" } } };
  const external = { ...acknowledged, display_name: "Externally renamed" };
  const setters = {
    setStepDisplayName: vi.fn(), setStepKindDraft: vi.fn(), setStepOverrideDraft: vi.fn(),
    setStepAgentId: vi.fn(), setStepOptional: vi.fn(), setStepBlockKey: vi.fn(),
  };
  const view = renderHook(({ workflowId, revision, raw }) => {
    const intent = useHttpRequestIntent(`${workflowId}:http_a:http_request`, () => undefined);
    useWorkflowDetailDomBridge({
      ...setters, workflowContractQuery: { data: undefined }, httpIntent: intent, stepKindDraft: "http_request",
      stepWorkflowId: workflowId, stepServerRevision: revision,
      selectedStep: { runtimeId: "http_a", stepKind: "http_request", raw },
    });
    return intent;
  }, { initialProps: { workflowId: "workflow-a", revision: 7, raw: original } });
  const token = view.result.current.beginRequest();
  const identity = { workflowId: "workflow-a", revision: 8, signature: `http_a:${JSON.stringify(acknowledged)}` };
  view.result.current.acknowledgeRequest(token, identity);
  view.result.current.recordAcknowledgedReadback(token, identity);
  // Deliberately settle transport before the actual bridge receives the row.
  view.result.current.endRequest(token);
  expect(view.result.current.ownsPendingReadback()).toBe(false);
  view.rerender({ workflowId: "workflow-a", revision: 8, raw: acknowledged });
  expect(view.result.current.isCurrent(token, "url")).toBe(true);
  expect(view.result.current.reconcileAcknowledgedReadback(identity)).toBe(false);

  view.rerender({ workflowId: change === "workflow" ? "workflow-b" : "workflow-a", revision: 9, raw: change === "row metadata" ? external : acknowledged });
  expect(view.result.current.isCurrent(token, "url")).toBe(false);
  // Equal data, revision and workflow IDs returning cannot revive the token.
  view.rerender({ workflowId: "workflow-a", revision: 8, raw: acknowledged });
  expect(view.result.current.isCurrent(token, "url")).toBe(false);
});

it("does not authorize a newer revision through the still-pending transport path", () => {
  const view = renderHook(() => useHttpRequestIntent("owner", () => undefined));
  const token = view.result.current.beginRequest();
  const identity = { workflowId: "child", revision: 8, signature: "same-step-row" };
  expect(view.result.current.reconcileAcknowledgedReadback(identity)).toBe(false);
  view.result.current.acknowledgeRequest(token, identity);
  expect(view.result.current.reconcileAcknowledgedReadback({ ...identity, revision: 9 })).toBe(false);
  // The mismatched canonical readback consumed the receipt; returning revision8 cannot revive it.
  expect(view.result.current.reconcileAcknowledgedReadback(identity)).toBe(false);
});

it("does not record an acknowledged readback after a newer field edit or request", () => {
  const view = renderHook(() => useHttpRequestIntent("A", () => undefined));
  const identity = { workflowId: "A", revision: 8, signature: "same-values-returned" };
  let token = view.result.current.beginRequest();
  view.result.current.acknowledgeRequest(token, identity);
  act(() => { view.result.current.touchField("url"); view.result.current.touchField("url"); });
  view.result.current.recordAcknowledgedReadback(token, identity);
  view.result.current.endRequest(token);
  expect(view.result.current.reconcileAcknowledgedReadback(identity)).toBe(false);
  token = view.result.current.beginRequest();
  view.result.current.acknowledgeRequest(token, identity);
  view.result.current.beginRequest();
  view.result.current.recordAcknowledgedReadback(token, identity);
  expect(view.result.current.reconcileAcknowledgedReadback(identity)).toBe(false);
});

it.each(["agent", "http_request"])("does not reload an Agent draft on a revision-only change of its saved %s row", (savedKind) => {
  const raw = { step_id: "step", step_kind: savedKind };
  const setters = {
    setStepDisplayName: vi.fn(), setStepKindDraft: vi.fn(), setStepOverrideDraft: vi.fn(),
    setStepAgentId: vi.fn(), setStepOptional: vi.fn(), setStepBlockKey: vi.fn(),
  };
  const view = renderHook(({ revision }) => {
    const intent = useHttpRequestIntent("agent-draft", () => undefined);
    useWorkflowDetailDomBridge({
      ...setters, workflowContractQuery: { data: undefined }, httpIntent: intent, stepKindDraft: "agent", stepWorkflowId: "workflow", stepServerRevision: revision,
      selectedStep: { runtimeId: "step", stepKind: savedKind, raw },
    });
  }, { initialProps: { revision: 7 } });
  Object.values(setters).forEach((setter) => setter.mockClear());
  view.rerender({ revision: 8 });
  for (const setter of Object.values(setters)) expect(setter).not.toHaveBeenCalled();
});


it("retires whole-step readback and completion without discarding current field feedback", () => {
  const view = renderHook(() => useHttpRequestIntent("A", () => undefined));
  const token = view.result.current.beginRequest();
  const identity = { workflowId: "A", revision: 8, signature: "same-raw-row" };
  view.result.current.acknowledgeRequest(token, identity);
  act(() => view.result.current.touchStepParameters());
  expect(view.result.current.isUnedited(token)).toBe(false);
  expect(view.result.current.ownsPendingReadback()).toBe(false);
  expect(view.result.current.mayReconcileReadback()).toBe(false);
  // A label/Optional change does not change the HTTP field that the server rejected.
  expect(view.result.current.isCurrent(token)).toBe(true);
  expect(view.result.current.isCurrent(token, "url")).toBe(true);
  expect(view.result.current.isCurrent(token, "auth_ref")).toBe(true);
  view.result.current.recordAcknowledgedReadback(token, identity);
  view.result.current.endRequest(token);
  expect(view.result.current.reconcileAcknowledgedReadback(identity)).toBe(false);

  const newer = view.result.current.beginRequest();
  expect(view.result.current.isUnedited(newer)).toBe(true);
  act(() => { view.result.current.touchStepParameters(); view.result.current.touchStepParameters(); });
  // Restoring displayed values still cannot restore the old whole-step generation.
  expect(view.result.current.isUnedited(newer)).toBe(false);
  expect(view.result.current.isCurrent(newer, "url")).toBe(true);
});
