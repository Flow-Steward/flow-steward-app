import { useI18n } from "@/lib/i18n/useI18n";
import type { Dispatch, SetStateAction } from "react";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";

import {
  acknowledgeJob,
  cancelJob,
  restartJob,
  unacknowledgeJob,
  submitApprovalDecision,
  type ApprovalDecisionPayload,
} from "./api";
import {
  approvalQueryKey,
  dispatchApprovalsRefresh,
  safeEntityId,
} from "./job-detail-core";
import { queryKeys } from "@/lib/query/keys";
import { isHttpError } from "@/lib/http/errors";
import { ignoreAsyncResult } from "@/lib/utils/async";

type MessageSetter = Dispatch<SetStateAction<string>>;

export function useJobDetailActions(
  jobId: string,
  setMessage: MessageSetter,
  setError: MessageSetter,
) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const approvalMutation = useMutation({
    mutationFn: ({
      threadId,
      payload,
    }: {
      jobId: string;
      threadId: string;
      payload: ApprovalDecisionPayload;
    }) => submitApprovalDecision(threadId, payload),
    onError: (error, submitted) => {
      setError(
        error instanceof Error ? error.message : t("react.jobs.approval_request_failed", "Approval request failed."),
      );
      if (isHttpError(error) && error.status === 409) {
        ignoreAsyncResult(
          queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(submitted.jobId) }),
        );
        ignoreAsyncResult(
          queryClient.invalidateQueries({ queryKey: approvalQueryKey(submitted.jobId) }),
        );
        dispatchApprovalsRefresh();
      }
    },
    onSuccess: (_response, submitted) => {
      setError("");
      ignoreAsyncResult(
        queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(submitted.jobId) }),
      );
      ignoreAsyncResult(
        queryClient.invalidateQueries({ queryKey: approvalQueryKey(submitted.jobId) }),
      );
      dispatchApprovalsRefresh();
    },
  });
  const cancelMutation = useMutation({
    mutationFn: () => cancelJob(jobId),
    onError: (error) =>
      setError(
        error instanceof Error ? error.message : t("react.jobs.cancel_request_failed", "Cancel request failed."),
      ),
    onSuccess: () => {
      setError("");
      setMessage(t("react.jobs.cancel_request_sent_refreshing_job_state", "Cancel request sent. Refreshing job state…"));
      ignoreAsyncResult(
        queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(jobId) }),
      );
      dispatchApprovalsRefresh();
    },
  });
  const acknowledgeMutation = useMutation({
    mutationFn: (acknowledged: boolean) => (acknowledged ? acknowledgeJob(jobId) : unacknowledgeJob(jobId)),
    onError: (error) =>
      setError(
        error instanceof Error ? error.message : t("react.jobs.acknowledge_request_failed", "Acknowledge request failed."),
      ),
    onSuccess: () => {
      setError("");
      ignoreAsyncResult(
        queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(jobId) }),
      );
      ignoreAsyncResult(
        queryClient.invalidateQueries({ queryKey: ["dashboard", "summary"] }),
      );
    },
  });
  function requestAcknowledgeJob(acknowledged: boolean) {
    if (acknowledgeMutation.isPending || !jobId) return;
    setMessage("");
    setError("");
    acknowledgeMutation.mutate(acknowledged);
  }
  const restartMutation = useMutation({
    mutationFn: () => restartJob(jobId),
    onError: (error) =>
      setError(
        error instanceof Error ? error.message : t("react.jobs.restart_request_failed", "Restart request failed."),
      ),
    onSuccess: (payload) => {
      setError("");
      const newJobId = safeEntityId(payload.new_job_id);
      if (newJobId) {
        setMessage(t("react.jobs.job_restarted_as_value1", "Job restarted as {value1}.", { value1: String(newJobId) }));
        ignoreAsyncResult(navigate(`/jobs/${newJobId}`));
        return;
      }
      setMessage(t("react.jobs.restart_request_sent_refreshing_job_state", "Restart request sent. Refreshing job state…"));
      ignoreAsyncResult(
        queryClient.invalidateQueries({ queryKey: queryKeys.jobDetail(jobId) }),
      );
    },
  });

  async function submitApprovalFromWorkspace(
    threadId: string,
    payload: ApprovalDecisionPayload,
  ) {
    setMessage("");
    setError("");
    await approvalMutation.mutateAsync({ jobId, threadId, payload });
  }
  function requestCancelJob() {
    if (cancelMutation.isPending || !jobId) return;
    if (
      globalThis.window !== undefined &&
      !globalThis.confirm(t("react.jobs.confirm_cancel_current_run", "Cancel this job? The current run will stop."))
    )
      return;
    setMessage("");
    setError("");
    cancelMutation.mutate();
  }
  function requestRestartJob() {
    if (restartMutation.isPending || !jobId) return;
    if (
      globalThis.window !== undefined &&
      !globalThis.confirm(t("react.jobs.confirm_restart_same_parameters", "Restart this job with the same task parameters?"))
    )
      return;
    setMessage("");
    setError("");
    restartMutation.mutate();
  }

  return {
    acknowledgeMutation,
    approvalMutation,
    cancelMutation,
    requestAcknowledgeJob,
    requestCancelJob,
    requestRestartJob,
    restartMutation,
    submitApprovalFromWorkspace,
  };
}
