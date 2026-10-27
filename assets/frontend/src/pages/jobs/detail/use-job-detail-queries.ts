import { useEffect } from "react";

import { useQuery } from "@tanstack/react-query";

import {
  fetchJobApproval,
  fetchJobApprovalHistory,
  fetchJobCostBreakdown,
  fetchJobDecisionOverrides,
  fetchJobDefinition,
  fetchJobIncident,
} from "./api";
import { approvalQueryKey } from "./job-detail-core";
import type { RestApprovalSnapshot } from "./job-detail-live-state";

type QueryOptions = {
  approvalActive: boolean;
  captureApprovalRevision: () => number;
  hasReviewWorkspace: boolean;
  hasFailureSummary: boolean;
  isTerminal: boolean;
  jobId: string;
  needsApproval: boolean;
  reconcileRestApproval: (snapshot: RestApprovalSnapshot | undefined) => void;
};

export function useJobDetailQueries(options: QueryOptions) {
  const {
    approvalActive,
    captureApprovalRevision,
    hasReviewWorkspace,
    hasFailureSummary,
    isTerminal,
    jobId,
    needsApproval,
    reconcileRestApproval,
  } = options;
  const active = Boolean(jobId);
  const incidentQuery = useQuery({
    queryKey: ["jobs", "detail", jobId, "incident"],
    queryFn: () => fetchJobIncident(jobId),
    enabled: active && hasFailureSummary,
    refetchInterval: isTerminal ? false : 15_000,
  });
  const decisionOverridesQuery = useQuery({
    queryKey: ["jobs", "detail", jobId, "decision-overrides"],
    queryFn: () => fetchJobDecisionOverrides(jobId),
    enabled: active,
    refetchInterval: isTerminal ? false : 15_000,
  });
  const costBreakdownQuery = useQuery({
    queryKey: ["jobs", "detail", jobId, "cost-breakdown"],
    queryFn: () => fetchJobCostBreakdown(jobId),
    enabled: active,
    refetchInterval: isTerminal ? false : 20_000,
  });
  const approvalQuery = useQuery({
    queryKey: approvalQueryKey(jobId),
    queryFn: async () => {
      const observedRevision = captureApprovalRevision();
      return {
        approval: await fetchJobApproval(jobId),
        observedRevision,
      };
    },
    enabled: active && (needsApproval || approvalActive || hasReviewWorkspace),
    refetchInterval: needsApproval || approvalActive ? 10_000 : false,
    retry: false,
  });
  // A snapshot never changes once pinned, so it is read once per job.
  const definitionQuery = useQuery({
    queryKey: ["jobs", "detail", jobId, "definition"],
    queryFn: () => fetchJobDefinition(jobId),
    enabled: active,
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });
  const approvalHistoryQuery = useQuery({
    queryKey: ["jobs", "detail", jobId, "approval-history"],
    queryFn: () => fetchJobApprovalHistory(jobId),
    enabled: active,
    refetchInterval: isTerminal ? false : 15_000,
  });
  useEffect(
    () => reconcileRestApproval(approvalQuery.data),
    [approvalQuery.data, reconcileRestApproval],
  );

  return {
    approvalQuery,
    approvalHistoryQuery,
    costBreakdownQuery,
    decisionOverridesQuery,
    definitionQuery,
    incidentQuery,
  };
}
