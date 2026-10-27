import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { MemoryRouter } from "react-router";

import { useWorkflowHeaderActionsController } from "@/pages/workflows/detail/detail-header-actions-controller";

/**
 * Saving a Template step with other input types left its Test Run form on the
 * old inputs until a reload: the form reads the step contract preview, cached
 * per step, and the refresh after a save did not touch it.
 */
describe("refreshing the workflow after a save", () => {
  it("marks the cached step contract previews of this workflow as stale", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const previewKey = ["workflows", "step-contract-preview", "wf_demo", "compose", "step-1", "", "template", ""];
    const otherWorkflowKey = ["workflows", "step-contract-preview", "wf_other", "compose", "step-9", "", "template", ""];
    queryClient.setQueryData(previewKey, { optional: [{ name: "header", type: "text" }] });
    queryClient.setQueryData(otherWorkflowKey, { optional: [] });
    const refreshedWorkflow = { workflow_id: "wf_demo", revision: 8 };
    const detailRefetch = vi.fn().mockResolvedValue({ data: refreshedWorkflow, isError: false });
    const refetched = () => ({ refetch: vi.fn().mockResolvedValue(undefined) });
    const wrapper = ({ children }: PropsWithChildren) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    );
    const { result } = renderHook(
      () =>
        useWorkflowHeaderActionsController({
          canvasGraphQuery: refetched(),
          dagQuery: refetched(),
          detailQuery: { refetch: detailRefetch },
          navigate: vi.fn(),
          selectedProjectId: "",
          surface: "builder",
          validationQuery: refetched(),
          workflowContractQuery: refetched(),
          workflowId: "wf_demo",
        }),
      { wrapper },
    );

    await act(async () => {
      expect(await result.current.refetchWorkflowSurfaces()).toEqual(refreshedWorkflow);
    });

    expect(queryClient.getQueryState(previewKey)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(otherWorkflowKey)?.isInvalidated).toBe(false);

    // React Query can retain stale data when the requested refresh fails.
    detailRefetch.mockResolvedValueOnce({ data: refreshedWorkflow, isError: true });
    await act(async () => {
      expect(await result.current.refetchWorkflowSurfaces()).toBeNull();
    });
  });
});
