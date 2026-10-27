import { useEffect, useMemo } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { queryKeys } from "@/lib/query/keys";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import {
  fetchJobDefinition,
  fetchJobExecutionStepsPage,
  type JobExecutionSnapshot,
} from "./api";
import { mergeExecutionStepsChronologically } from "./job-detail-execution";

import { jobExecutionTrace } from "./job-execution-trace";
import { jobContainerProgress } from "./job-container-progress";

const MAX_EXECUTION_PAGES = 100;

function text(...values: unknown[]): string {
  return firstStringFromUnknownValue(values).trim();
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

/**
 * Opt-in archived canvas evidence. Shares the inspector's queries, reads only
 * this job, and follows bounded history pages without following child links.
 */
export function useJobExecutionSnapshot(jobId: string, enabled: boolean, expectedProjectId?: string) {
  const active = enabled && Boolean(jobId);
  const definitionQuery = useQuery({
    queryKey: ["jobs", "detail", jobId, "definition"],
    queryFn: () => fetchJobDefinition(jobId),
    enabled: active,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });
  const historyQuery = useInfiniteQuery({
    queryKey: queryKeys.jobExecutionSteps(jobId, ""),
    queryFn: ({ pageParam }) => fetchJobExecutionStepsPage(jobId, pageParam),
    initialPageParam: "",
    getNextPageParam: (page) => page.page?.has_more && page.page.next_cursor ? page.page.next_cursor : undefined,
    enabled: active,
    retry: false,
    refetchInterval: (query) => query.state.data?.pages[0]?.run_terminal ? false : 3_000,
  });
  const pages = historyQuery.data?.pages;
  const nextCursor = pages?.at(-1)?.page?.next_cursor || "";
  const more = Boolean(pages?.at(-1)?.page?.has_more);
  const repeatedCursor = Boolean(historyQuery.data?.pageParams.includes(nextCursor));
  const canLoadMore = more && Boolean(nextCursor) && !repeatedCursor && (pages?.length || 0) < MAX_EXECUTION_PAGES;
  const { fetchNextPage, isFetching, isError } = historyQuery;
  useEffect(() => {
    if (active && canLoadMore && !isFetching && !isError) void fetchNextPage();
  }, [active, canLoadMore, fetchNextPage, isError, isFetching, nextCursor]);

  const snapshot = useMemo<JobExecutionSnapshot | null>(() => {
    const definition = definitionQuery.data;
    const history = historyQuery.data?.pages;
    if (
      !active || definitionQuery.isError || !definition?.available || !definition.workflow_id
      || !Number.isInteger(definition.revision) || Number(definition.revision) <= 0
      || !history?.length || definition.job_id !== jobId || history.some((page) => page.job_id !== jobId)
    ) return null;
    const sourceSteps = history.flatMap((page) => page.steps || []).flatMap((step) => {
      const alias = text(step.workflow_step_id), stepId = text(step.step_id);
      if (alias && stepId && alias !== stepId) return [];
      // Preserve logical identity before merge fills missing IDs with row keys.
      return [alias && !stepId ? { ...step, step_id: alias } : step];
    });
    const steps = mergeExecutionStepsChronologically(sourceSteps);
    const runTrace = jobExecutionTrace(jobId, definition, history[0]);
    const containerProgress = jobContainerProgress(jobId, expectedProjectId, definition, history[0]);
    const error = history[0].run_error;
    const errorCode = text(error?.code), errorMessage = text(error?.message), errorStepId = text(error?.step_id);
    const errorStepExecutionId = text(error?.step_execution_id);
    return {
      jobId,
      definitionWorkflowId: definition.workflow_id,
      definitionRevision: Number(definition.revision),
      ...(definition.snapshot_id ? { definitionSnapshotId: definition.snapshot_id } : {}),
      ...(runTrace ? { runTrace } : {}),
      ...(containerProgress ? { containerProgress } : {}),
      ...(definition.digest ? { definitionDigest: definition.digest } : {}),
      definitionSteps: (definition.steps || []).flatMap((step) => {
        const stepId = text(step.step_id);
        if (!stepId) return [];
        const hookId = text(object(object(object(step.override).invoke_workflow).hook).hook_id);
        const label = text(step.display_name);
        return [{ stepId, ...(hookId ? { hookId } : {}), ...(label ? { label } : {}) }];
      }),
      ...(errorCode || errorMessage || errorStepId || errorStepExecutionId ? { runError: {
        ...(errorCode ? { code: errorCode } : {}), ...(errorMessage ? { message: errorMessage } : {}), ...(errorStepId ? { stepId: errorStepId } : {}),
        ...(errorStepExecutionId ? { stepExecutionId: errorStepExecutionId } : {}),
      } } : {}),
      steps: steps.flatMap((step) => {
        const stepId = text(step.workflow_step_id, step.step_id);
        // Merged logical claims must still agree before archived attribution.
        if (text(step.workflow_step_id) && text(step.step_id) && text(step.workflow_step_id) !== text(step.step_id)) return [];
        const status = text(step.status);
        if (!stepId || !status) return [];
        const stepExecutionId = text(step.step_execution_id);
        const groupId = text(step.group_id);
        const forEachId = text(step.for_each_id);
        const laneId = text(step.lane_id);
        const startedAt = text(step.started_at);
        const finishedAt = text(step.finished_at);
        const durationMs = typeof step.duration_ms === "number" && Number.isFinite(step.duration_ms) && step.duration_ms >= 0
          ? step.duration_ms : undefined;
        return [{
          stepId, status,
          ...(stepExecutionId ? { stepExecutionId } : {}),
          ...(Number.isInteger(step.occurrence) ? { occurrence: Number(step.occurrence) } : {}),
          ...(groupId ? { groupId } : {}),
          ...(forEachId ? { forEachId } : {}),
          ...(laneId ? { laneId } : {}),
          ...(Number.isInteger(step.iteration) && Number(step.iteration) >= 0 ? { iteration: Number(step.iteration) } : {}),
          ...(Number.isInteger(step.attempt) && Number(step.attempt) >= 1 ? { attempt: Number(step.attempt) } : {}),
          ...(startedAt ? { startedAt } : {}),
          ...(finishedAt ? { finishedAt } : {}),
          ...(durationMs !== undefined ? { durationMs } : {}),
          ...(text(step.skill) === "kept_test_output" ? { kept: true } : {}),
        }];
      }),
      childInvocations: (history[0].child_invocations || []).map((invocation) => ({
        stepId: invocation.step_id,
        stepExecutionId: invocation.step_execution_id,
        invocationKey: invocation.invocation_key,
        childJobId: invocation.child_job_id,
        childWorkflowId: invocation.child_workflow_id,
      })),
      terminal: history[0].run_terminal === true,
      complete: !history.at(-1)?.page?.has_more && !history[0].child_invocations_truncated && !historyQuery.isError,
    };
  }, [active, definitionQuery.data, definitionQuery.isError, historyQuery.data, historyQuery.isError, jobId, expectedProjectId]);
  const error = definitionQuery.error || historyQuery.error;
  return {
    snapshot,
    pending: active && (definitionQuery.isPending || historyQuery.isPending || historyQuery.isFetchingNextPage),
    error: active && error ? (error instanceof Error ? error : new Error(String(error))) : null,
  };
}
