import { useQuery } from "@tanstack/react-query";
import { queryKeys } from "@/lib/query/keys";
import { fetchJobDetail } from "./api";
import { jobDetailPollingInterval } from "./job-detail-view-model";

/** One compact job detail cache/runner; the disabled handoff observer reads cache. */
export function useJobDetailQuery(jobId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.jobDetail(jobId),
    queryFn: () => fetchJobDetail(jobId),
    enabled: Boolean(jobId) && enabled,
    refetchInterval: (query) => jobDetailPollingInterval(query.state.data, 10_000),
  });
}
