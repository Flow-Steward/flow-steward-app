import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";

import { useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/lib/query/keys";
import { useJobStream } from "@/lib/realtime/useJobStream";
import type { JobApprovalPayload, JobDetail } from "./api";
import {
  mergeExecutionSteps,
  mergeLiveFiles,
  streamFrameToExecutionStep,
} from "./job-detail-execution";
import {
  approvalQueryKey,
  dispatchApprovalsRefresh,
  firstReadableText,
  objectOrEmpty,
  type Entity,
  type JobStreamViewState,
} from "./job-detail-core";
import {
  approvalIdentity,
  finalizedJobStreamState,
  finalRefreshRetryDelay,
  reduceApprovalGate,
} from "./job-detail-live-state";
import {
  costBreakdownStreamPatch,
  decisionDiagnosticsPatch,
  jobDetailWithApprovalFrame,
  jobDetailWithoutStaleSummaries,
  jobMetadataPatch,
  lifecycleRuntimePatch,
  lifecycleStatusPatch,
  mergeJobRecord,
  mergeLifecycleRecord,
  mergeSectionsRecord,
} from "./job-detail-view-model";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";

type Options = {
  approval: JobApprovalPayload | undefined;
  currentJobId: string;
  jobId: string;
  setError: Dispatch<SetStateAction<string>>;
  setStreamState: Dispatch<SetStateAction<JobStreamViewState>>;
  streamState: JobStreamViewState;
};

export function useJobDetailRealtime({
  approval,
  currentJobId,
  jobId,
  setError: setJobActionError,
  setStreamState,
  streamState,
}: Options) {
  const queryClient = useQueryClient();
  const streamEvents = streamState.streamEvents;
  const approvalRefreshKey = useRef("");
  const pendingApprovalRefresh = useRef<{ key: string; frame: Entity } | null>(null);
  const finalRefreshState = useRef<{
    jobId: string;
    inFlight: boolean;
    attempt: number;
    timer: ReturnType<typeof setTimeout> | null;
  }>({ jobId: currentJobId, inFlight: false, attempt: 0, timer: null });

  useEffect(() => {
    approvalRefreshKey.current = "";
    pendingApprovalRefresh.current = null;
    const state = finalRefreshState.current;
    if (state.timer !== null) {
      clearTimeout(state.timer);
    }
    finalRefreshState.current = {
      jobId: currentJobId,
      inFlight: false,
      attempt: 0,
      timer: null,
    };
    return () => {
      const latest = finalRefreshState.current;
      if (latest.jobId === currentJobId && latest.timer !== null) {
        clearTimeout(latest.timer);
      }
    };
  }, [currentJobId]);

  useEffect(() => {
    const pending = pendingApprovalRefresh.current;
    if (pending?.frame !== streamState.approvalGate.frame || pending.key === approvalRefreshKey.current) return;
    approvalRefreshKey.current = pending.key;
    // The approval query captures its stream revision when it starts. Wait for
    // this exact frame to commit: a prior render's passive effect can run after
    // onMessage stores the pending refresh but before its state update commits.
    void queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(jobId), exact: true });
    void queryClient.invalidateQueries({ queryKey: approvalQueryKey(jobId), exact: true });
  }, [jobId, queryClient, streamState.approvalGate.frame, streamState.approvalGate.revision]);

  async function finalizeStreamAfterRefresh(targetJobId = currentJobId) {
    const state = finalRefreshState.current;
    if (state.jobId !== targetJobId || state.inFlight || streamState.done) {
      return;
    }
    state.inFlight = true;
    const finalQueryKeys = [
      queryKeys.jobDetail(targetJobId),
      queryKeys.jobExecutionSteps(targetJobId, ""),
      ["jobs", "detail", targetJobId, "incident"],
      ["jobs", "detail", targetJobId, "messages"],
      ["jobs", "detail", targetJobId, "decision-overrides"],
      ["jobs", "detail", targetJobId, "cost-breakdown"],
      ["jobs", "detail", targetJobId, "approval-history"],
      approvalQueryKey(targetJobId),
    ];
    try {
      await Promise.all(
        finalQueryKeys.map((queryKey) =>
          queryClient.refetchQueries(
            { queryKey, exact: true, type: "active" },
            { throwOnError: true },
          ),
        ),
      );
    } catch {
      if (finalRefreshState.current.jobId !== targetJobId) {
        return;
      }
      state.inFlight = false;
      state.attempt += 1;
      const delay = finalRefreshRetryDelay(state.attempt);
      if (delay === null) {
        setJobActionError(
          "Live updates ended before the final refresh completed. Refresh the page.",
        );
        setStreamState((previous) => ({ ...previous, done: true }));
        return;
      }
      state.timer = setTimeout(() => {
        if (finalRefreshState.current.jobId === targetJobId) {
          finalRefreshState.current.timer = null;
          void finalizeStreamAfterRefresh(targetJobId);
        }
      }, delay);
      return;
    }
    if (finalRefreshState.current.jobId !== targetJobId) {
      return;
    }
    state.inFlight = false;
    state.attempt = 0;
    setStreamState(finalizedJobStreamState);
    dispatchApprovalsRefresh();
  }

  useJobStream(jobId, {
    enabled: Boolean(jobId) && !streamState.done,
    onMessage: (payload) => {
      setStreamState((previous) => ({
        ...previous,
        streamEvents: [payload, ...previous.streamEvents].slice(0, 60),
      }));
      const frame = objectOrEmpty(payload);
      const frameType = firstStringFromUnknownValue([
        frame.type,
        frame.event_type,
        "",
      ])
        .trim()
        .toLowerCase();
      const frameData = frame.data;
      const frameRecord = objectOrEmpty(frameData);
      if (frameType === "event" && firstStringFromUnknownValue([frameRecord.event_type, ""]) === "artifact_ready") {
        setStreamState((previous) => ({
          ...previous,
          liveFiles: mergeLiveFiles(previous.liveFiles, frameRecord),
        }));
      }
      const liveStep = streamFrameToExecutionStep(
        frameType,
        frameRecord,
        streamEvents.length,
      );
      if (liveStep) {
        setStreamState((previous) => ({
          ...previous,
          liveStepRows: mergeExecutionSteps([
            ...previous.liveStepRows,
            liveStep,
          ]),
        }));
      }

      const previousDetail = queryClient.getQueryData<JobDetail>(queryKeys.jobDetail(jobId));
      const refreshedBase = (frameType === "status" || frameType === "runtime")
        ? jobDetailWithoutStaleSummaries(previousDetail, frameRecord)
        : previousDetail;
      const lifecycleChanged = refreshedBase !== previousDetail;
      if (lifecycleChanged) {
        queryClient.setQueryData(queryKeys.jobDetail(jobId), refreshedBase);
      }
      if (frameType === "status") {
        const incomingStatus = firstReadableText(
          frameRecord.status,
        ).toLowerCase();
        if (incomingStatus !== "awaiting_approval") {
          approvalRefreshKey.current = "";
          pendingApprovalRefresh.current = null;
        }
        setStreamState((previous) => ({
          ...previous,
          approvalGate: reduceApprovalGate(previous.approvalGate, {
            type: "status",
            status: incomingStatus,
            approvalId: approvalIdentity(approval),
          }),
        }));
        queryClient.setQueryData<JobDetail | undefined>(
          queryKeys.jobDetail(jobId),
          (current) => ({
            ...(current ||
              ({
                id: currentJobId,
                status: firstStringFromUnknownValue([
                  frameRecord.status,
                  "unknown",
                ]),
              } as JobDetail)),
            ...frameRecord,
            job: mergeJobRecord(current, jobMetadataPatch(frameRecord)),
            lifecycle: mergeLifecycleRecord(
              current,
              lifecycleStatusPatch(frameRecord, current),
            ),
          }),
        );
        dispatchApprovalsRefresh();
      } else if (frameType === "runtime") {
        queryClient.setQueryData<JobDetail | undefined>(
          queryKeys.jobDetail(jobId),
          (current) => ({
            ...(current ||
              ({ id: currentJobId, status: "unknown" } as JobDetail)),
            runtime: {
              ...objectOrEmpty(current?.runtime),
              ...frameRecord,
            },
            job: mergeJobRecord(current, jobMetadataPatch(frameRecord)),
            lifecycle: mergeLifecycleRecord(
              current,
              lifecycleRuntimePatch(frameRecord, current),
            ),
          }),
        );
      } else if (frameType === "approval") {
        setStreamState((previous) => ({
          ...previous,
          approvalGate: reduceApprovalGate(previous.approvalGate, {
            type: "approval",
            frame: frameRecord,
          }),
        }));
        queryClient.setQueryData<JobDetail | undefined>(
          queryKeys.jobDetail(jobId),
          (current) =>
            jobDetailWithApprovalFrame(current, frameRecord, currentJobId),
        );
        const incomingApprovalId = approvalIdentity(frameRecord);
        const approvalChanged = !previousDetail || jobDetailWithoutStaleSummaries(previousDetail, {
          ...frameRecord, status: "awaiting_approval",
        }) !== previousDetail;
        const frameRefreshKey = JSON.stringify([
          incomingApprovalId,
          firstReadableText(frameRecord.current_step_id, frameRecord.step_id),
          firstReadableText(frameRecord.thread_id),
        ]);
        if (approvalChanged || !incomingApprovalId || pendingApprovalRefresh.current?.key === frameRefreshKey) {
          pendingApprovalRefresh.current = { frame: frameRecord, key: frameRefreshKey };
        }
        dispatchApprovalsRefresh();
      } else if (frameType === "approval_history" && Array.isArray(frameData)) {
        const diagnosticsPatchForHistory = (current?: JobDetail) =>
          decisionDiagnosticsPatch(frameData, current);
        queryClient.setQueryData<JobDetail | undefined>(
          queryKeys.jobDetail(jobId),
          (current) => ({
            ...(current ||
              ({ id: currentJobId, status: "unknown" } as JobDetail)),
            previous_decisions: frameData,
            diagnostics: diagnosticsPatchForHistory(current),
            sections: mergeSectionsRecord(current, {
              previous_decisions: frameData,
              diagnostics: diagnosticsPatchForHistory(current),
            }),
          }),
        );
        dispatchApprovalsRefresh();
      } else if (frameType === "cost") {
        const projectedCostBreakdown = (current?: JobDetail) =>
          costBreakdownStreamPatch(frameRecord, current);
        queryClient.setQueryData<JobDetail | undefined>(
          queryKeys.jobDetail(jobId),
          (current) => ({
            ...(current ||
              ({ id: currentJobId, status: "unknown" } as JobDetail)),
            cost: {
              ...objectOrEmpty(current?.cost),
              ...frameRecord,
            },
            cost_breakdown: projectedCostBreakdown(current),
            sections: mergeSectionsRecord(current, {
              cost_breakdown: projectedCostBreakdown(current),
            }),
            lifecycle: mergeLifecycleRecord(current, {
              costs: projectedCostBreakdown(current),
            }),
          }),
        );
      } else if (frameType === "done") {
        void finalizeStreamAfterRefresh();
      }
      if (lifecycleChanged) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(jobId), exact: true });
      }
    },
  });
}
