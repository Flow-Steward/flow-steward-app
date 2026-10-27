import type { PropsWithChildren } from "react";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { HttpError } from "@/lib/http/errors";
import { queryKeys } from "@/lib/query/keys";
import { approvalQueryKey } from "./job-detail-core";
import { submitApprovalDecision } from "./api";
import { useJobDetailActions } from "./use-job-detail-actions";

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  submitApprovalDecision: vi.fn(),
}));

it.each([409, 403])("refreshes stale approval state only for a conflict (HTTP %s)", async (status) => {
  const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  queryClient.setQueryData(queryKeys.jobDetail("job-effect"), { status: "awaiting_approval" });
  queryClient.setQueryData(approvalQueryKey("job-effect"), { approval_id: "gate-effect" });
  const error = new HttpError({ status, url: "/api/approvals/thread-7", payload: { detail: "Decision is unavailable" } });
  vi.mocked(submitApprovalDecision).mockRejectedValueOnce(error);
  const setError = vi.fn();
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={queryClient}><MemoryRouter>{children}</MemoryRouter></QueryClientProvider>
  );
  const { result } = renderHook(() => useJobDetailActions("job-effect", vi.fn(), setError), { wrapper });

  await act(async () => {
    await expect(result.current.submitApprovalFromWorkspace("thread-7", {
      approval_id: "gate-effect", approved: true, comment: "Verified with provider",
    })).rejects.toBe(error);
  });

  expect(setError).toHaveBeenLastCalledWith("Decision is unavailable");
  expect(queryClient.getQueryState(queryKeys.jobDetail("job-effect"))?.isInvalidated).toBe(status === 409);
  expect(queryClient.getQueryState(approvalQueryKey("job-effect"))?.isInvalidated).toBe(status === 409);
  queryClient.clear();
});
