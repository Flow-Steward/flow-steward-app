import { useCallback, useLayoutEffect, useRef } from "react";

import { useKeyedState } from "@/lib/react/useKeyedState";
import type { JobApprovalPayload, JobDetail } from "@/pages/jobs/detail/api";
import {
  type ApprovalGateLiveState,
  DEFAULT_JOB_STREAM_VIEW_STATE,
  type Entity,
  type JobStreamViewState,
  objectOrEmpty,
} from "@/pages/jobs/detail/job-detail-core";
import { approvalResponseFromStream } from "@/pages/jobs/detail/job-detail-review-panels";
import { jobDetailWithApprovalFrame } from "@/pages/jobs/detail/job-detail-view-model";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

export type ApprovalGateEvent =
  | { type: "approval"; frame: Entity }
  | {
      type: "rest";
      approval: JobApprovalPayload | undefined;
      observedRevision: number;
    }
  | { type: "status"; status: string; approvalId?: string };

export const DEFAULT_APPROVAL_GATE_LIVE_STATE: ApprovalGateLiveState = {
  revision: 0,
  kind: "unknown",
  frame: null,
};

export type RestApprovalSnapshot = {
  approval: JobApprovalPayload | undefined;
  observedRevision: number;
};

export function useJobLiveState(stateKey: string) {
  const [state, setState] = useKeyedState<JobStreamViewState>(
    stateKey,
    DEFAULT_JOB_STREAM_VIEW_STATE,
  );
  const approvalRevisionRef = useRef(state.approvalGate.revision);
  useLayoutEffect(() => {
    approvalRevisionRef.current = state.approvalGate.revision;
  }, [state.approvalGate.revision]);
  const captureApprovalRevision = useCallback(() => approvalRevisionRef.current, []);
  const reconcileRestApproval = useCallback(
    (snapshot: RestApprovalSnapshot | undefined) => {
      if (!snapshot) return;
      setState((previous) => {
        const approvalGate = reduceApprovalGate(previous.approvalGate, {
          type: "rest",
          approval: snapshot.approval,
          observedRevision: snapshot.observedRevision,
        });
        return approvalGate === previous.approvalGate ? previous : { ...previous, approvalGate };
      });
    },
    [setState],
  );
  return { state, setState, captureApprovalRevision, reconcileRestApproval };
}

export function approvalIdentity(value: unknown): string {
  const record = objectOrEmpty(value);
  const payload = objectOrEmpty(record.payload);
  return firstStringFromUnknownValue([
    record.approval_id,
    record.interrupt_id,
    record.id,
    payload.approval_id,
    payload.interrupt_id,
    payload.id,
    "",
  ]).trim();
}

export function reduceApprovalGate(
  current: ApprovalGateLiveState,
  event: ApprovalGateEvent,
): ApprovalGateLiveState {
  if (event.type === "status") {
    if (!event.status || event.status === "awaiting_approval") {
      return current;
    }
    return {
      revision: current.revision + 1,
      kind: "none",
      frame: null,
      closedApprovalId:
        approvalIdentity(current.frame) || event.approvalId || current.closedApprovalId,
    };
  }
  if (event.type === "rest") {
    if (event.observedRevision < current.revision) {
      return current;
    }
    const root = objectOrEmpty(event.approval);
    const payload = objectOrEmpty(root.payload);
    const incomingFrame = { ...root, ...payload };
    const incomingId = approvalIdentity(incomingFrame);
    if (!incomingId) {
      return {
        revision: current.revision + 1,
        kind: "none",
        frame: null,
        closedApprovalId: approvalIdentity(current.frame) || current.closedApprovalId,
      };
    }
    if (current.kind === "none" && incomingId === current.closedApprovalId) {
      return current;
    }
    return {
      revision: current.revision + 1,
      kind: "active",
      frame: incomingFrame,
    };
  }
  return {
    revision: current.revision + 1,
    kind: "active",
    frame: event.frame,
    closedApprovalId: undefined,
  };
}

function omitApprovalFields(value: Entity): Entity {
  const rest = { ...value };
  for (const key of ["approval", "approval_gate", "review_workspace", "needs_approval"]) {
    Reflect.deleteProperty(rest, key);
  }
  return rest;
}

export function detailForApprovalGate(
  detail: JobDetail | undefined,
  gate: ApprovalGateLiveState,
  currentJobId: string,
): JobDetail | undefined {
  if (gate.kind === "active" && gate.frame) {
    return jobDetailWithApprovalFrame(detail, gate.frame, currentJobId);
  }
  if (gate.kind !== "none" || !detail) {
    return detail;
  }
  const record = omitApprovalFields(objectOrEmpty(detail));
  const lifecycle = omitApprovalFields(objectOrEmpty(record.lifecycle));
  const sections = omitApprovalFields(objectOrEmpty(record.sections));
  const projection = omitApprovalFields(objectOrEmpty(record.projection));
  return {
    ...record,
    lifecycle,
    sections,
    projection,
  } as JobDetail;
}

export function approvalForApprovalGate(
  current: JobApprovalPayload | undefined,
  gate: ApprovalGateLiveState,
  currentJobId: string,
): JobApprovalPayload | undefined {
  if (gate.kind === "none") {
    // "none" means the stream is not showing a gate, which is not the same as the
    // REST endpoint having none. Discard the REST approval only when the stream
    // positively closed this very approval; otherwise a silent, finished or
    // revision-raced stream would erase a live gate and leave the operator with a
    // decision panel that has no approval id and therefore cannot submit.
    const closedApprovalId = (gate.closedApprovalId || "").trim();
    return closedApprovalId && approvalIdentity(current) === closedApprovalId
      ? undefined
      : current;
  }
  if (gate.kind === "active" && gate.frame) {
    return approvalResponseFromStream(current, gate.frame, currentJobId);
  }
  return current;
}

export function finalRefreshRetryDelay(attempt: number): number | null {
  if (attempt >= 4) {
    return null;
  }
  return 250 * 2 ** Math.max(0, attempt - 1);
}

export function finalizedJobStreamState(
  current: JobStreamViewState,
): JobStreamViewState {
  return {
    ...current,
    approvalGate: reduceApprovalGate(current.approvalGate, {
      type: "status",
      status: "completed",
    }),
    done: true,
    liveFiles: [],
    liveStepRows: [],
  };
}
