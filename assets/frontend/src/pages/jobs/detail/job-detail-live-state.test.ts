import { describe, expect, it } from "vitest";

import { DEFAULT_JOB_STREAM_VIEW_STATE } from "@/pages/jobs/detail/job-detail-core";
import {
  DEFAULT_APPROVAL_GATE_LIVE_STATE,
  approvalForApprovalGate,
  detailForApprovalGate,
  finalizedJobStreamState,
  finalRefreshRetryDelay,
  reduceApprovalGate,
} from "@/pages/jobs/detail/job-detail-live-state";

describe("job detail live state", () => {
  it("keeps the REST approval when the stream never saw that gate", () => {
    // AC17-R30: a silent, finished or revision-raced stream reports kind "none"
    // without ever having seen the gate. Discarding the REST approval there left the
    // decision panel with no approval_id, so Approve sent nothing at all.
    const restApproval = {
      job_id: "job-1",
      approval_id: "approval-live",
      payload: { approval_id: "approval-live", message: "Review gate" },
    };
    const silentStream = reduceApprovalGate(DEFAULT_APPROVAL_GATE_LIVE_STATE, {
      type: "status",
      status: "in_progress",
    });

    expect(silentStream.kind).toBe("none");
    expect(silentStream.closedApprovalId).toBeFalsy();
    expect(approvalForApprovalGate(restApproval, silentStream, "job-1")).toEqual(restApproval);
  });

  it("keeps the REST approval when a stale revision made the stream ignore it", () => {
    const finalized = finalizedJobStreamState({
      ...DEFAULT_JOB_STREAM_VIEW_STATE,
      approvalGate: DEFAULT_APPROVAL_GATE_LIVE_STATE,
    }).approvalGate;
    const restApproval = { job_id: "job-1", approval_id: "approval-live" };
    const ignoredStaleRest = reduceApprovalGate(finalized, {
      type: "rest",
      approval: restApproval,
      observedRevision: 0,
    });

    expect(ignoredStaleRest).toBe(finalized);
    expect(approvalForApprovalGate(restApproval, ignoredStaleRest, "job-1")).toEqual(restApproval);
  });

  it("keeps a versioned no-active-gate tombstone after a closed approval", () => {
    const active = reduceApprovalGate(DEFAULT_APPROVAL_GATE_LIVE_STATE, {
      type: "approval",
      frame: { approval_id: "approval-1", review_workspace: { title: "Review" } },
    });
    const closed = reduceApprovalGate(active, { type: "status", status: "in_progress" });

    expect(closed).toEqual({
      revision: 2,
      kind: "none",
      frame: null,
      closedApprovalId: "approval-1",
    });
    expect(
      approvalForApprovalGate(
        { job_id: "job-1", approval_id: "approval-1" },
        closed,
        "job-1",
      ),
    ).toBeUndefined();
    expect(
      detailForApprovalGate(
        {
          id: "job-1",
          status: "in_progress",
          review_workspace: { title: "stale" },
          lifecycle: { review_workspace: { title: "stale" } },
          sections: { review_workspace: { title: "stale" } },
          projection: { review_workspace: { title: "stale" } },
        },
        closed,
        "job-1",
      ),
    ).toEqual({
      id: "job-1",
      status: "in_progress",
      lifecycle: {},
      sections: {},
      projection: {},
    });
  });

  it("activates a new REST approval after the previous stream gate was tombstoned", () => {
    const gateA = reduceApprovalGate(DEFAULT_APPROVAL_GATE_LIVE_STATE, {
      type: "approval",
      frame: { approval_id: "approval-a" },
    });
    const closed = reduceApprovalGate(gateA, { type: "status", status: "in_progress" });

    const staleRest = reduceApprovalGate(closed, {
      type: "rest",
      approval: { job_id: "job-1", approval_id: "approval-a" },
      observedRevision: closed.revision,
    });
    expect(staleRest).toBe(closed);

    const gateB = reduceApprovalGate(closed, {
      type: "rest",
      approval: {
        job_id: "job-1",
        approval_id: "approval-b",
        payload: { approval_id: "approval-b", message: "Review gate B" },
      },
      observedRevision: closed.revision,
    });

    expect(gateB).toMatchObject({
      revision: 3,
      kind: "active",
      frame: { approval_id: "approval-b", message: "Review gate B" },
    });
    expect(approvalForApprovalGate(gateB.frame as never, gateB, "job-1")).toMatchObject({
      approval_id: "approval-b",
    });
  });

  it("records the initial REST approval identity so a later refetch cannot resurrect it", () => {
    const initial = reduceApprovalGate(DEFAULT_APPROVAL_GATE_LIVE_STATE, {
      type: "rest",
      approval: { job_id: "job-1", approval_id: "approval-a" },
      observedRevision: 0,
    });
    expect(initial).toMatchObject({ kind: "active", frame: { approval_id: "approval-a" } });

    const closed = reduceApprovalGate(initial, { type: "status", status: "in_progress" });
    const staleRefetch = reduceApprovalGate(closed, {
      type: "rest",
      approval: { job_id: "job-1", approval_id: "approval-a" },
      observedRevision: closed.revision,
    });

    expect(staleRefetch).toBe(closed);
  });

  it("uses the REST identity when a closing status races the initial reconciliation", () => {
    const closed = reduceApprovalGate(DEFAULT_APPROVAL_GATE_LIVE_STATE, {
      type: "status",
      status: "in_progress",
      approvalId: "approval-a",
    });
    const staleRest = reduceApprovalGate(closed, {
      type: "rest",
      approval: { job_id: "job-1", approval_id: "approval-a" },
      observedRevision: closed.revision,
    });

    expect(staleRest).toBe(closed);
    expect(closed.closedApprovalId).toBe("approval-a");
  });

  it("replaces an active stream gate with a newer REST gate", () => {
    const gateA = reduceApprovalGate(DEFAULT_APPROVAL_GATE_LIVE_STATE, {
      type: "approval",
      frame: { approval_id: "approval-a" },
    });

    const gateB = reduceApprovalGate(gateA, {
      type: "rest",
      approval: { job_id: "job-1", approval_id: "approval-b" },
      observedRevision: gateA.revision,
    });

    expect(gateB).toMatchObject({
      revision: 2,
      kind: "active",
      frame: { approval_id: "approval-b" },
    });
  });

  it("clears an active stream gate after a newer REST snapshot has no approval", () => {
    const gateA = reduceApprovalGate(DEFAULT_APPROVAL_GATE_LIVE_STATE, {
      type: "approval",
      frame: { approval_id: "approval-a" },
    });

    const cleared = reduceApprovalGate(gateA, {
      type: "rest",
      approval: undefined,
      observedRevision: gateA.revision,
    });

    expect(cleared).toEqual({
      revision: 2,
      kind: "none",
      frame: null,
      closedApprovalId: "approval-a",
    });
  });

  it("ignores a REST snapshot that started before the current stream revision", () => {
    const gateB = reduceApprovalGate(DEFAULT_APPROVAL_GATE_LIVE_STATE, {
      type: "approval",
      frame: { approval_id: "approval-b" },
    });

    const staleGateA = reduceApprovalGate(gateB, {
      type: "rest",
      approval: { job_id: "job-1", approval_id: "approval-a" },
      observedRevision: 0,
    });

    expect(staleGateA).toBe(gateB);
  });

  it("bounds final refresh retries with exponential backoff", () => {
    expect([1, 2, 3, 4].map(finalRefreshRetryDelay)).toEqual([250, 500, 1000, null]);
  });

  it("drops temporary stream steps after the canonical terminal refresh", () => {
    const finalized = finalizedJobStreamState({
      approvalGate: {
        revision: 2,
        kind: "active",
        frame: { approval_id: "approval-1" },
      },
      done: false,
      liveFiles: [{ id: "event-1", event_type: "artifact_ready" }],
      liveStepRows: [
        { step_id: "stream-child-1", title: "invoke_workflow" },
        { step_id: "stream-child-2", title: "invoke_workflow" },
      ],
      streamEvents: [{ type: "done" }],
    });

    expect(finalized).toEqual({
      approvalGate: {
        revision: 3,
        kind: "none",
        frame: null,
        closedApprovalId: "approval-1",
      },
      done: true,
      liveFiles: [],
      liveStepRows: [],
      streamEvents: [{ type: "done" }],
    });
  });
});
