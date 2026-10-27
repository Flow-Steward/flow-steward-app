import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, useLayoutEffect, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { HttpError } from "@/lib/http/errors";
import * as api from "@/pages/workflows/shared/simulate-api";
import type { WorkflowTestRunResult } from "@/pages/workflows/shared/simulate-api";
import { useWorkflowTestRunController, type WorkflowTestRunSubmissionBridge, type WorkflowTestRunSubmissionProof } from "./detail-test-run-controller";

const initialOwner: WorkflowTestRunSubmissionProof = { workflowId: "wf_owner", projectId: "project-a", editorRevision: 7, ownerEpoch: 1 };
const jobKey = "fs.workflow-run-job.wf_owner";
const threadKey = "fs.workflow-test-run.wf_owner";
function dto(overrides: Partial<WorkflowTestRunResult> = {}): WorkflowTestRunResult {
  return { workflow_id: initialOwner.workflowId, project_id: initialOwner.projectId,
    status: "completed", needs_user_approval: false, job_id: "job-new", thread_id: "thread-new", ...overrides };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function sameOwner(a: WorkflowTestRunSubmissionProof | null, b: WorkflowTestRunSubmissionProof) {
  return Boolean(a && a.workflowId === b.workflowId && a.projectId === b.projectId &&
    a.editorRevision === b.editorRevision && a.ownerEpoch === b.ownerEpoch);
}
function validationFailure() {
  return new HttpError({ status: 422, url: "/api/workflows/wf_owner/test-run",
    payload: { detail: [{ path: "inputs.city", message: "City was rejected by the server." }] } });
}
function revisionFailure() {
  return new HttpError({ status: 409, url: "/api/workflows/wf_owner/test-run",
    payload: { detail: { code: "workflow_revision_conflict" } } });
}

const ownerUnmounts: Array<() => void> = [];
type OwnerProps = { owner: WorkflowTestRunSubmissionProof; ready: boolean };
function mountOwner(options: { legacy?: boolean; nullBridge?: boolean; ready?: boolean; events?: string[]; blocked?: string; launchLock?: { current: symbol | null } } = {}) {
  const events = options.events ?? [];
  const authority = { current: null as WorkflowTestRunSubmissionProof | null };
  const capture = vi.fn(() => { events.push("capture"); return authority.current; });
  const accept = vi.fn((payload: WorkflowTestRunResult, proof: WorkflowTestRunSubmissionProof) => {
    events.push("accept");
    return sameOwner(authority.current, proof) && payload.workflow_id === proof.workflowId &&
      payload.project_id === proof.projectId && Boolean(payload.job_id);
  });
  const bridge: WorkflowTestRunSubmissionBridge = {
    captureSubmissionOwner: capture, isCurrent: (proof) => sameOwner(authority.current, proof), acceptAcknowledgement: accept,
  };
  const onSubmitted = vi.fn(() => { events.push("submitted"); });
  const onConflict = vi.fn();
  const contract: { data: { interface: { expected_inputs: Array<Record<string, unknown>> } }; isFetching?: boolean } = { data: { interface: { expected_inputs: [{ name: "city", type: "string", required: false }] } } };
  const stepContract = { data: {} };
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => createElement(I18nContext.Provider,
    { value: fallbackContext }, createElement(QueryClientProvider, { client: queryClient }, children));
  const view = renderHook(({ owner, ready }: OwnerProps) => {
    const controller = useWorkflowTestRunController({
      workflowId: owner.workflowId, selectedProjectId: owner.projectId, serverRevision: owner.editorRevision,
      workflowContractQuery: contract, stepContractPreviewQuery: stepContract,
      workflowTestRunBlockedReason: options.blocked,
      launchLock: options.launchLock,
      onWorkflowTestRunSubmitted: onSubmitted, onRevisionConflict: onConflict,
      submissionOwner: options.legacy ? undefined : options.nullBridge ? null : bridge,
    });
    const { refreshWorkflowTestRunSubmissionOwner } = controller;
    // This is the explicit proposed parent ordering, using a real layout effect:
    // register committed owner authority first, then signal the controller.
    useLayoutEffect(() => {
      authority.current = ready ? owner : null;
      refreshWorkflowTestRunSubmissionOwner();
      return () => { if (authority.current === owner) authority.current = null; };
    }, [refreshWorkflowTestRunSubmissionOwner, owner, ready]);
    return controller;
  }, { wrapper, initialProps: { owner: initialOwner, ready: options.ready ?? true } });
  ownerUnmounts.push(view.unmount);
  return { ...view, capture, accept, onSubmitted, onConflict, queryClient, events, contract,
    edit: (city = "Vilnius", summary = "Owner-specific summary", comment = "Keep my comment") => act(() => {
      view.result.current.updateWorkflowTestRunInput("city", city);
      view.result.current.updateWorkflowTestRunSummary(summary);
      view.result.current.setWorkflowTestRunApprovalComment(comment);
    }),
  };
}

beforeEach(() => { globalThis.localStorage.clear(); });
afterEach(() => {
  // Retire these owned subscriptions before restoring timers, mocks and storage.
  for (const unmount of ownerUnmounts.splice(0)) unmount();
  vi.restoreAllMocks(); vi.useRealTimers(); globalThis.localStorage.clear();
});

describe("dispatch owner proof and unchanged raw result", () => {
  it("captures before HTTP and accepts before state/storage/Submitted, retaining the exact raw DTO", async () => {
    const events: string[] = [];
    const payload = dto();
    const post = vi.spyOn(api, "testRunWorkflow").mockImplementation(async () => { events.push("POST"); return payload; });
    const view = mountOwner({ events });
    view.edit();
    events.length = 0;
    const originalAccept = view.accept.getMockImplementation();
    view.accept.mockImplementation((response, proof) => {
      expect(view.result.current.workflowTestRunPayload).toBeNull();
      expect(globalThis.localStorage.getItem(jobKey)).toBeNull();
      expect(globalThis.localStorage.getItem(threadKey)).toBeNull();
      expect(view.result.current.workflowTestRunApprovalComment).toBe("Keep my comment");
      return originalAccept?.(response, proof) ?? false;
    });
    let response: WorkflowTestRunResult | undefined;
    await act(async () => { response = await view.result.current.workflowTestRunMutation.mutateAsync(); });

    expect(events).toEqual(["capture", "POST", "accept", "submitted"]);
    expect(view.accept.mock.calls[0]?.[1]).toBe(view.capture.mock.results.at(-1)?.value);
    expect(post).toHaveBeenCalledWith(expect.objectContaining({ workflowId: "wf_owner", projectId: "project-a", expectedRevision: 7,
      taskSummary: "Owner-specific summary", inputs: { city: "Vilnius" } }));
    expect(response).toBe(payload);
    await waitFor(() => expect(view.result.current.workflowTestRunMutation.data).toBe(payload));
    expect(view.result.current.workflowTestRunMutationIsCurrent).toBe(true);
    expect(view.result.current.workflowTestRunPayload).toBe(payload);
    expect(view.result.current.workflowTestRunJobId).toBe("job-new");
    expect(view.result.current.workflowTestRunApprovalComment).toBe("");
    expect(globalThis.localStorage.getItem(jobKey)).toBe("job-new");
    expect(globalThis.localStorage.getItem(threadKey)).toBe("thread-new");
    expect(view.onSubmitted).toHaveBeenCalledWith(payload);
  });

  it("keeps no-argument legacy mutation calls and ordinary result shape", async () => {
    const payload = dto({ project_id: undefined });
    const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValue(payload);
    const view = mountOwner({ legacy: true });
    view.edit();
    let response: WorkflowTestRunResult | undefined;
    await act(async () => { response = await view.result.current.workflowTestRunMutation.mutateAsync(); });
    expect(response).toBe(payload);
    expect(post).toHaveBeenCalledTimes(1);
    expect(view.accept).not.toHaveBeenCalled();
    expect(view.result.current.workflowTestRunPayload).toBe(payload);
    expect(view.result.current.workflowTestRunMutation.data).toBe(payload);
    expect(view.onSubmitted).toHaveBeenCalledWith(payload);
  });
});

describe("not-ready and concurrent direct admission", () => {
  it.each(["null", "not registered"] as const)("fails closed with %s owner without erasing edits or writing storage", async (mode) => {
    const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValue(dto());
    const view = mountOwner({ nullBridge: mode === "null", ready: mode !== "not registered" });
    view.edit();
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync().catch(() => undefined); });
    expect(post).not.toHaveBeenCalled();
    expect(view.accept).not.toHaveBeenCalled();
    expect(view.onSubmitted).not.toHaveBeenCalled();
    expect(view.result.current.workflowTestRunSummary).toBe("Owner-specific summary");
    expect(view.result.current.workflowTestRunInputs.city).toBe("Vilnius");
    expect(view.result.current.workflowTestRunApprovalComment).toBe("Keep my comment");
    expect(view.result.current.workflowTestRunPayload).toBeNull();
    expect(view.result.current.workflowTestRunFieldErrors).toEqual({});
    expect(view.result.current.workflowTestRunMutationIsCurrent).toBe(false);
    expect(globalThis.localStorage.getItem(jobKey)).toBeNull();
    expect(globalThis.localStorage.getItem(threadKey)).toBeNull();
  });

  it("a rejected second direct call neither releases first admission nor borrows its raw observer authority", async () => {
    const held = deferred<WorkflowTestRunResult>();
    const post = vi.spyOn(api, "testRunWorkflow").mockReturnValueOnce(held.promise).mockResolvedValue(dto({ job_id: "job-next", thread_id: "thread-next" }));
    const view = mountOwner();
    view.edit();
    let first!: Promise<WorkflowTestRunResult>;
    act(() => { first = view.result.current.workflowTestRunMutation.mutateAsync(); });
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    let secondError: Error | undefined;
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync().catch((error: Error) => { secondError = error; }); });
    expect(secondError).toBeInstanceOf(Error);
    await waitFor(() => expect(view.result.current.workflowTestRunMutation.error).toBe(secondError));
    expect(view.result.current.workflowTestRunMutation.context).toBeUndefined();
    expect(view.result.current.workflowTestRunMutationIsCurrent).toBe(false);
    expect(view.result.current.workflowTestRunMessage).toBe("");
    act(() => view.result.current.startWorkflowTestRun());
    expect(post).toHaveBeenCalledTimes(1); // second onSettled did not release the first token.
    const payload = dto();
    await act(async () => { held.resolve(payload); expect(await first).toBe(payload); });
    await waitFor(() => expect(view.result.current.workflowTestRunPayload).toBe(payload));
    expect(view.onSubmitted).toHaveBeenCalledTimes(1);
    expect(view.result.current.workflowTestRunMutation.error).toBe(secondError);
    expect(view.result.current.workflowTestRunMutation.data).toBeUndefined();
    expect(view.result.current.workflowTestRunMutationIsCurrent).toBe(false);
    act(() => view.result.current.startWorkflowTestRun());
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2)); // only first's own settlement released it.
  });
});

describe("retired owner outcomes and submitted field values", () => {
  it.each(["success", "422", "409"] as const)("ignores old %s after project A to B to A", async (outcome) => {
    const held = deferred<WorkflowTestRunResult>();
    const post = vi.spyOn(api, "testRunWorkflow").mockReturnValue(held.promise);
    const view = mountOwner();
    view.edit("A city", "A summary", "A comment");
    let pending!: Promise<WorkflowTestRunResult | undefined>;
    act(() => { pending = view.result.current.workflowTestRunMutation.mutateAsync().catch(() => undefined); });
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    view.rerender({ owner: { ...initialOwner, projectId: "project-b", ownerEpoch: 2 }, ready: true });
    view.rerender({ owner: { ...initialOwner, ownerEpoch: 3 }, ready: true });
    view.edit("Returned city", "Returned summary", "Returned comment");
    act(() => view.result.current.setWorkflowTestRunMessage("Current owner notice"));
    await act(async () => {
      if (outcome === "success") held.resolve(dto());
      else held.reject(outcome === "422" ? validationFailure() : revisionFailure());
      await pending;
    });
    expect(view.accept).not.toHaveBeenCalled();
    expect(view.onSubmitted).not.toHaveBeenCalled();
    expect(view.onConflict).not.toHaveBeenCalled();
    expect(view.result.current.workflowTestRunPayload).toBeNull();
    expect(view.result.current.workflowTestRunJobId).toBe("");
    expect(view.result.current.workflowTestRunSummary).toBe("Returned summary");
    expect(view.result.current.workflowTestRunInputs.city).toBe("Returned city");
    expect(view.result.current.workflowTestRunApprovalComment).toBe("Returned comment");
    expect(view.result.current.workflowTestRunMessage).toBe("Current owner notice");
    expect(view.result.current.workflowTestRunFieldErrors).toEqual({});
    expect(view.result.current.workflowTestRunMutationIsCurrent).toBe(false);
    expect(globalThis.localStorage.getItem(jobKey)).toBeNull();
    expect(globalThis.localStorage.getItem(threadKey)).toBeNull();
  });

  it("associates a current 422 with the submitted value and removes it when that value is edited", async () => {
    const held = deferred<WorkflowTestRunResult>();
    const post = vi.spyOn(api, "testRunWorkflow").mockReturnValueOnce(held.promise).mockRejectedValue(validationFailure());
    const view = mountOwner();
    view.edit("Submitted city");
    let pending!: Promise<WorkflowTestRunResult | undefined>;
    act(() => { pending = view.result.current.workflowTestRunMutation.mutateAsync().catch(() => undefined); });
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    act(() => view.result.current.updateWorkflowTestRunInput("city", "Edited while pending"));
    await act(async () => { held.reject(validationFailure()); await pending; });
    expect(view.result.current.workflowTestRunInputs.city).toBe("Edited while pending");
    expect(view.result.current.workflowTestRunFieldErrors["inputs.city"]).toBeUndefined();
    view.edit("Submitted city");
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync().catch(() => undefined); });
    expect(view.result.current.workflowTestRunFieldErrors["inputs.city"]).toBe("City was rejected by the server.");
    expect(view.result.current.workflowTestRunMutationIsCurrent).toBe(true);
    act(() => view.result.current.updateWorkflowTestRunInput("city", "New city"));
    expect(view.result.current.workflowTestRunInputs.city).toBe("New city");
    expect(view.result.current.workflowTestRunFieldErrors["inputs.city"]).toBeUndefined();
  });
});

describe("ACK rejection stays separate from raw transport success", () => {
  it.each([
    ["wrong project", { project_id: "other-project" }],
    ["missing job", { job_id: null }],
  ] as const)("does not adopt %s ACK or rewrite either pointer", async (_label, patch) => {
    const payload = dto(patch);
    vi.spyOn(api, "testRunWorkflow").mockResolvedValue(payload);
    const view = mountOwner();
    view.edit();
    await act(async () => { expect(await view.result.current.workflowTestRunMutation.mutateAsync()).toBe(payload); });
    expect(view.accept).toHaveBeenCalledWith(payload, initialOwner);
    expect(view.result.current.workflowTestRunMutation.data).toBe(payload);
    expect(view.result.current.workflowTestRunPayload).toBeNull();
    expect(view.result.current.workflowTestRunJobId).toBe("");
    expect(view.result.current.workflowTestRunApprovalComment).toBe("Keep my comment");
    expect(view.onSubmitted).not.toHaveBeenCalled();
    expect(globalThis.localStorage.getItem(jobKey)).toBeNull();
    expect(globalThis.localStorage.getItem(threadKey)).toBeNull();
  });
});

describe("deterministic restoration and pointer boundaries", () => {
  it("restores only after explicit committed readiness; repeated refresh is harmless and restore is not ACK", async () => {
    globalThis.localStorage.setItem(threadKey, "thread-remembered");
    const payload = dto({ status: "awaiting_approval", needs_user_approval: true, thread_id: "thread-remembered", approval_id: "approval-1" });
    const read = vi.spyOn(api, "fetchWorkflowTestRunState").mockResolvedValue(payload);
    const view = mountOwner({ ready: false });
    expect(read).not.toHaveBeenCalled();
    view.rerender({ owner: initialOwner, ready: true });
    await waitFor(() => expect(view.result.current.workflowTestRunPayload).toBe(payload));
    expect(read).toHaveBeenCalledTimes(1);
    act(() => {
      view.result.current.refreshWorkflowTestRunSubmissionOwner();
      view.result.current.refreshWorkflowTestRunSubmissionOwner();
      view.result.current.refreshWorkflowTestRunSubmissionOwner();
    });
    expect(read).toHaveBeenCalledTimes(1);
    expect(view.result.current.workflowTestRunPayload).toBe(payload);
    expect(view.accept).not.toHaveBeenCalled();
    expect(view.onSubmitted).not.toHaveBeenCalled();
    expect(globalThis.localStorage.getItem(jobKey)).toBeNull();
    expect(globalThis.localStorage.getItem(threadKey)).toBe("thread-remembered");
  });

  it("does not let an old same-owner restore replace a new ACK and remembered thread", async () => {
    globalThis.localStorage.setItem(threadKey, "thread-old");
    const held = deferred<WorkflowTestRunResult>();
    const read = vi.spyOn(api, "fetchWorkflowTestRunState").mockReturnValue(held.promise);
    const payload = dto();
    vi.spyOn(api, "testRunWorkflow").mockResolvedValue(payload);
    const view = mountOwner();
    await waitFor(() => expect(read).toHaveBeenCalledWith("thread-old"));
    view.edit();
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
    expect(view.result.current.workflowTestRunPayload).toBe(payload);
    expect(globalThis.localStorage.getItem(threadKey)).toBe("thread-new");
    await act(async () => { held.resolve(dto({ job_id: "job-old", thread_id: "thread-old", status: "awaiting_approval", needs_user_approval: true })); await held.promise; });
    expect(view.result.current.workflowTestRunPayload).toBe(payload);
    expect(view.result.current.workflowTestRunJobId).toBe("job-new");
    expect(globalThis.localStorage.getItem(threadKey)).toBe("thread-new");
    expect(globalThis.localStorage.getItem(jobKey)).toBe("job-new");
    expect(view.accept).toHaveBeenCalledTimes(1);
    expect(view.onSubmitted).toHaveBeenCalledTimes(1);
  });

  it.each(["foreign", "missing"] as const)("does not adopt or delete a %s project restored pointer", async (kind) => {
    globalThis.localStorage.setItem(threadKey, "thread-other");
    const read = vi.spyOn(api, "fetchWorkflowTestRunState").mockResolvedValue(dto({
      thread_id: "thread-other", project_id: kind === "foreign" ? "other-project" : undefined,
    }));
    const view = mountOwner();
    await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
    await act(async () => { await read.mock.results[0]?.value; });
    expect(view.result.current.workflowTestRunPayload).toBeNull();
    expect(view.result.current.workflowTestRunMessage).toBe("");
    expect(globalThis.localStorage.getItem(threadKey)).toBe("thread-other");
    expect(view.accept).not.toHaveBeenCalled();
    expect(view.onSubmitted).not.toHaveBeenCalled();
  });

  it("does not remove a replaced foreign pointer when a later accepted DTO has no thread", async () => {
    globalThis.localStorage.setItem(threadKey, "thread-owned");
    vi.spyOn(api, "fetchWorkflowTestRunState").mockResolvedValue(dto({ thread_id: "thread-owned" }));
    vi.spyOn(api, "testRunWorkflow").mockResolvedValue(dto({ thread_id: null }));
    const view = mountOwner();
    await waitFor(() => expect(view.result.current.workflowTestRunPayload?.thread_id).toBe("thread-owned"));
    globalThis.localStorage.setItem(threadKey, "thread-foreign-replacement");
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
    expect(view.result.current.workflowTestRunPayload?.thread_id).toBeNull();
    expect(globalThis.localStorage.getItem(threadKey)).toBe("thread-foreign-replacement");
  });
});

describe("retired polling and approval provenance", () => {
  it("ignores a held old poll after owner retirement", async () => {
    globalThis.localStorage.setItem(threadKey, "thread-old");
    const poll = deferred<WorkflowTestRunResult>();
    const read = vi.spyOn(api, "fetchWorkflowTestRunState")
      .mockResolvedValueOnce(dto({ thread_id: "thread-old", status: "queued" })).mockReturnValue(poll.promise);
    vi.useFakeTimers();
    const view = mountOwner();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(view.result.current.workflowTestRunPayload?.status).toBe("queued");
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(read).toHaveBeenCalledTimes(2);
    // The new owner has no remembered thread; it must not consume A's held poll.
    globalThis.localStorage.removeItem(threadKey);
    view.rerender({ owner: { ...initialOwner, projectId: "project-b", ownerEpoch: 2 }, ready: true });
    view.edit("B city", "B summary", "B comment");
    act(() => view.result.current.setWorkflowTestRunMessage("B notice"));
    await act(async () => { poll.resolve(dto({ thread_id: "thread-old", status: "failed" })); await poll.promise; });
    expect(view.result.current.workflowTestRunPayload).toBeNull();
    expect(view.result.current.workflowTestRunMessage).toBe("B notice");
    expect(view.result.current.workflowTestRunApprovalComment).toBe("B comment");
    expect(globalThis.localStorage.getItem(threadKey)).toBeNull();
  });

  it.each(["success", "failure"] as const)("ignores held approval %s after owner/thread retirement", async (outcome) => {
    globalThis.localStorage.setItem(threadKey, "thread-old");
    vi.spyOn(api, "fetchWorkflowTestRunState").mockResolvedValue(dto({ thread_id: "thread-old", status: "awaiting_approval", needs_user_approval: true, approval_id: "approval-old" }));
    const held = deferred<Record<string, unknown>>();
    const approval = vi.spyOn(api, "submitWorkflowTestRunApproval").mockReturnValue(held.promise);
    const view = mountOwner();
    await waitFor(() => expect(view.result.current.workflowTestRunPayload?.approval_id).toBe("approval-old"));
    view.edit("A city", "A summary", "Approve A");
    let pending!: Promise<WorkflowTestRunResult | undefined>;
    act(() => { pending = view.result.current.workflowTestRunApprovalMutation.mutateAsync({ approved: true }).catch(() => undefined); });
    await waitFor(() => expect(approval).toHaveBeenCalledWith("thread-old", { approval_id: "approval-old", approved: true, comment: "Approve A" }));
    globalThis.localStorage.removeItem(threadKey);
    view.rerender({ owner: { ...initialOwner, projectId: "project-b", ownerEpoch: 2 }, ready: true });
    view.edit("B city", "B summary", "B comment");
    act(() => view.result.current.setWorkflowTestRunMessage("B notice"));
    await act(async () => { if (outcome === "success") held.resolve({}); else held.reject(new Error("Old approval failed")); await pending; });
    expect(view.result.current.workflowTestRunPayload).toBeNull();
    expect(view.result.current.workflowTestRunMessage).toBe("B notice");
    expect(view.result.current.workflowTestRunApprovalComment).toBe("B comment");
    expect(view.result.current.workflowTestRunApprovalMutationIsCurrent).toBe(false);
    expect(globalThis.localStorage.getItem(threadKey)).toBeNull();
    expect(view.onSubmitted).not.toHaveBeenCalled();
  });
});

describe("fresh Run again from accepted public inputs", () => {
  it("uses immutable accepted values with the current applied revision while preserving a separate form draft", async () => {
    const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValueOnce(dto()).mockResolvedValue(dto({ job_id: "job-again", thread_id: "thread-again" }));
    const view = mountOwner();
    view.edit("Vilnius", "Accepted summary");
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
    view.edit("Kaunas", "Separate draft");
    view.rerender({ owner: { ...initialOwner, editorRevision: 8, ownerEpoch: 2 }, ready: true });
    await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("admitted"); });
    expect(post).toHaveBeenNthCalledWith(2, expect.objectContaining({ expectedRevision: 8, taskSummary: "Accepted summary", inputs: { city: "Vilnius" }, dryRun: false }));
    expect(view.result.current.workflowTestRunInputs.city).toBe("Kaunas");
    expect(view.result.current.workflowTestRunSummary).toBe("Separate draft");
  });

  it("requires the form for a historical or reloaded job without borrowing the current draft", async () => {
    const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValue(dto());
    const view = mountOwner();
    view.edit("Kaunas", "Current draft is not historical evidence");
    await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("history-job")).toBe("needs-inputs"); });
    expect(post).not.toHaveBeenCalled();
    expect(view.result.current.workflowTestRunInputs.city).toBe("Kaunas");
  });

  it.each(["secret", "unsupported", "contract changed"])("does not directly reuse %s inputs", async (refusal) => {
    const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValue(dto());
    const view = mountOwner();
    if (refusal !== "contract changed") {
      view.contract.data = { interface: { expected_inputs: [{ name: "city", type: refusal === "secret" ? "secret" : "opaque_provider_value", required: false }] } };
      view.rerender({ owner: initialOwner, ready: true });
    }
    view.edit("private-value");
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
    if (refusal === "contract changed") {
      view.contract.data = { interface: { expected_inputs: [{ name: "city", type: "number", required: true }] } };
      view.rerender({ owner: initialOwner, ready: true });
    }
    await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("needs-inputs"); });
    expect(post).toHaveBeenCalledTimes(1);
    expect(globalThis.localStorage.getItem(jobKey)).toBe("job-new");
    expect([...Object.values(globalThis.localStorage)].join(" ")).not.toContain("private-value");
  });

  it("keeps falsy public values and excludes fixed defaults from the fresh request", async () => {
    const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValue(dto());
    const view = mountOwner();
    view.contract.data = { interface: { expected_inputs: [{ name: "zero", type: "number", required: false }, { name: "enabled", type: "boolean", required: false },
      { name: "fixed", type: "string", required: true, default_configured: true, default: "server-fixed" }] } };
    view.rerender({ owner: initialOwner, ready: true });
    act(() => { view.result.current.updateWorkflowTestRunInput("zero", "0"); view.result.current.updateWorkflowTestRunInput("enabled", "false"); });
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
    act(() => { view.result.current.updateWorkflowTestRunInput("zero", "9"); view.result.current.updateWorkflowTestRunInput("enabled", "true"); });
    await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("admitted"); });
    expect(post).toHaveBeenNthCalledWith(2, expect.objectContaining({ inputs: { zero: 0, enabled: false } }));
    expect(view.result.current.workflowTestRunInputs).toMatchObject({ zero: "9", enabled: "true" });
  });

  it("revokes rejected reuse evidence without attaching its errors to an unrelated form draft", async () => {
    const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValueOnce(dto()).mockRejectedValueOnce(validationFailure());
    const view = mountOwner();
    view.edit("Vilnius", "Accepted summary");
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
    view.edit("Kaunas", "Separate draft");
    await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("needs-inputs"); });
    expect(view.result.current.workflowTestRunInputs.city).toBe("Kaunas");
    expect(view.result.current.workflowTestRunFieldErrors["inputs.city"]).toBeUndefined();
    expect(view.result.current.workflowTestRunMessage).toContain("previous run's inputs were rejected");
    await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("needs-inputs"); });
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("keeps the single submission slot and retires late reuse outcomes on project ABA", async () => {
    const held = deferred<WorkflowTestRunResult>();
    const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValueOnce(dto()).mockReturnValueOnce(held.promise);
    const view = mountOwner();
    view.edit();
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
    let first!: Promise<string>;
    act(() => { first = view.result.current.startWorkflowTestRunAgain("job-new"); });
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("blocked"); });
    view.rerender({ owner: { ...initialOwner, projectId: "project-b", ownerEpoch: 2 }, ready: true });
    view.rerender({ owner: { ...initialOwner, ownerEpoch: 3 }, ready: true });
    await act(async () => { held.resolve(dto({ job_id: "retired-again" })); expect(await first).toBe("blocked"); });
    expect(view.onSubmitted).toHaveBeenCalledTimes(1);
    expect(globalThis.localStorage.getItem(jobKey)).toBe("job-new");
    await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("needs-inputs"); });
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("preserves dirty and revision-conflict guards for a fresh repeated launch", async () => {
    const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValueOnce(dto()).mockRejectedValueOnce(revisionFailure());
    const options = { blocked: "" };
    const view = mountOwner(options);
    view.edit();
    await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
    options.blocked = "Apply or discard structure changes before running a test.";
    view.rerender({ owner: initialOwner, ready: true });
    await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("blocked"); });
    expect(post).toHaveBeenCalledTimes(1);
    options.blocked = "";
    view.rerender({ owner: initialOwner, ready: true });
    await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("blocked"); });
    expect(view.onConflict).toHaveBeenCalledOnce();
    expect(post).toHaveBeenCalledTimes(2);
  });
});

it("rechecks the selected source before HTTP and retires its late ACK without selecting another run", async () => {
  const held = deferred<WorkflowTestRunResult>();
  const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValueOnce(dto()).mockReturnValue(held.promise);
  const view = mountOwner();
  view.edit();
  await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
  let current = true;
  let refused!: Promise<string>;
  act(() => { refused = view.result.current.startWorkflowTestRunAgain("job-new", () => current); current = false; });
  await act(async () => { expect(await refused).toBe("blocked"); });
  expect(post).toHaveBeenCalledTimes(1);
  current = true;
  let pending!: Promise<string>;
  act(() => { pending = view.result.current.startWorkflowTestRunAgain("job-new", () => current); });
  await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
  current = false;
  await act(async () => { held.resolve(dto({ job_id: "retired-source-job" })); expect(await pending).toBe("blocked"); });
  expect(view.accept).toHaveBeenCalledTimes(1);
  expect(view.onSubmitted).toHaveBeenCalledTimes(1);
  expect(globalThis.localStorage.getItem(jobKey)).toBe("job-new");
});

it("does not reuse values while the current public contract is being refreshed", async () => {
  const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValue(dto());
  const view = mountOwner();
  view.edit();
  await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
  view.contract.isFetching = true;
  view.rerender({ owner: initialOwner, ready: true });
  await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("needs-inputs"); });
  expect(post).toHaveBeenCalledTimes(1);
});

it("validates accepted numeric inputs independently of an incomplete new form draft", async () => {
  const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValue(dto());
  const view = mountOwner();
  view.contract.data = { interface: { expected_inputs: [{ name: "city", type: "number", required: true }] } };
  view.rerender({ owner: initialOwner, ready: true });
  view.edit("0");
  await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
  view.edit("2e");
  expect(view.result.current.workflowTestRunHasValidationErrors).toBe(true);
  await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("admitted"); });
  expect(post).toHaveBeenNthCalledWith(2, expect.objectContaining({ inputs: { city: 0 } }));
  expect(view.result.current.workflowTestRunInputs.city).toBe("2e");
});

it("requires a known applied revision before directly reusing accepted inputs", async () => {
  const post = vi.spyOn(api, "testRunWorkflow").mockResolvedValue(dto());
  const view = mountOwner();
  view.edit();
  await act(async () => { await view.result.current.workflowTestRunMutation.mutateAsync(); });
  view.rerender({ owner: { ...initialOwner, editorRevision: null, ownerEpoch: 2 }, ready: true });
  await act(async () => { expect(await view.result.current.startWorkflowTestRunAgain("job-new")).toBe("needs-inputs"); });
  expect(post).toHaveBeenCalledTimes(1);
});


describe("ordinary and Test Run launch exclusion", () => {
  it("does not post a Test Run while an ordinary run owns the shared launch slot", async () => {
    const post = vi.spyOn(api, "testRunWorkflow");
    const launchLock = { current: Symbol("ordinary-run") as symbol | null };
    const view = mountOwner({ launchLock });
    view.edit();
    act(() => view.result.current.startWorkflowTestRun());
    await waitFor(() => expect(view.result.current.workflowTestRunMutation.isError).toBe(true));
    expect(post).not.toHaveBeenCalled();
    expect(launchLock.current).not.toBeNull();
  });
});


it("does not post an isolated step Test Run while an ordinary launch owns the slot", async () => {
  const post = vi.spyOn(api, "testRunWorkflowStep");
  const launchLock = { current: Symbol("ordinary-run") as symbol | null };
  const view = mountOwner({ launchLock });
  act(() => view.result.current.stepTestRunMutation.mutate());
  await waitFor(() => expect(view.result.current.stepTestRunMutation.isError).toBe(true));
  expect(post).not.toHaveBeenCalled();
  expect(launchLock.current).not.toBeNull();
});
