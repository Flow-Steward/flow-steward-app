import { act, renderHook, waitFor } from "@testing-library/react";

import { fetchWorkflowCurrentRevision } from "@/pages/registry/changelog/api";
import { useWorkflowRestoreContext } from "@/pages/registry/changelog/use-workflow-restore-context";

vi.mock("@/pages/registry/changelog/api", () => ({ fetchWorkflowCurrentRevision: vi.fn() }));

const mockedFetchRevision = vi.mocked(fetchWorkflowCurrentRevision);

type Deferred = { resolve: (revision: number) => void; reject: (error: Error) => void };

/** Each read waits until the test answers it, in any order. */
function deferRevisionReads(): Deferred[] {
  const reads: Deferred[] = [];
  mockedFetchRevision.mockImplementation(() => new Promise<number>((resolve, reject) => {
    reads.push({ resolve, reject });
  }));
  return reads;
}

type Props = { open: boolean; entityType: string; entityId: string; entryId: string };

function renderContext(initial: Props) {
  return renderHook((props: Props) => useWorkflowRestoreContext(props), { initialProps: initial });
}

const A = { open: true, entityType: "workflow", entityId: "wf-a", entryId: "12" };
const B = { open: true, entityType: "workflow", entityId: "wf-b", entryId: "30" };

describe("useWorkflowRestoreContext", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("reads the revision once when the view opens and keeps it on rerender", async () => {
    const reads = deferRevisionReads();
    const { result, rerender } = renderContext(A);

    expect(result.current.context).toMatchObject({ status: "loading", expectedRevision: null });
    await act(async () => reads[0]?.resolve(4));
    expect(result.current.context).toMatchObject({
      workflowId: "wf-a", entryId: "12", status: "ready", expectedRevision: 4,
    });

    rerender({ ...A });
    rerender({ ...A });
    expect(mockedFetchRevision).toHaveBeenCalledTimes(1);
    expect(mockedFetchRevision).toHaveBeenCalledWith("wf-a");
    expect(result.current.context.expectedRevision).toBe(4);
  });

  it("offers no revision while loading or after the read failed", async () => {
    const reads = deferRevisionReads();
    const { result } = renderContext(A);
    const loading = result.current.context;

    expect(result.current.isCurrent(loading)).toBe(false);
    await act(async () => reads[0]?.reject(new Error("offline")));

    expect(result.current.context).toMatchObject({ status: "error", expectedRevision: null });
    expect(result.current.isCurrent(result.current.context)).toBe(false);
    expect(mockedFetchRevision).toHaveBeenCalledTimes(1);
  });

  it("drops a late answer for workflow A once the view shows B", async () => {
    const reads = deferRevisionReads();
    const { result, rerender } = renderContext(A);

    rerender(B);
    await act(async () => reads[0]?.resolve(4));
    expect(result.current.context).toMatchObject({ workflowId: "wf-b", status: "loading", expectedRevision: null });

    await act(async () => reads[1]?.resolve(9));
    expect(result.current.context).toMatchObject({ workflowId: "wf-b", entryId: "30", status: "ready", expectedRevision: 9 });
  });

  it("does not accept the first A answer after A, then B, then A again", async () => {
    const reads = deferRevisionReads();
    const { result, rerender } = renderContext(A);

    rerender(B);
    rerender(A);
    expect(mockedFetchRevision).toHaveBeenCalledTimes(3);
    await act(async () => reads[0]?.resolve(4));
    expect(result.current.context).toMatchObject({ workflowId: "wf-a", status: "loading", expectedRevision: null });

    await act(async () => reads[2]?.resolve(5));
    expect(result.current.context).toMatchObject({ workflowId: "wf-a", status: "ready", expectedRevision: 5 });
    await act(async () => reads[1]?.resolve(9));
    expect(result.current.context).toMatchObject({ workflowId: "wf-a", expectedRevision: 5 });
  });

  it("keeps a closed view closed when its read answers later", async () => {
    const reads = deferRevisionReads();
    const { result, rerender } = renderContext(A);

    rerender({ ...A, open: false });
    await act(async () => reads[0]?.resolve(4));

    expect(result.current.context).toMatchObject({ status: "idle", expectedRevision: null });
  });

  it("starts a new context only when the view reopens", async () => {
    const reads = deferRevisionReads();
    const { result, rerender } = renderContext(A);
    await act(async () => reads[0]?.resolve(4));
    const first = result.current.context;

    rerender({ ...A, open: false });
    expect(result.current.isCurrent(first)).toBe(false);
    rerender(A);
    expect(result.current.context.status).toBe("loading");
    await act(async () => reads[1]?.resolve(5));

    expect(result.current.context).toMatchObject({ status: "ready", expectedRevision: 5 });
    expect(result.current.context.generation).not.toBe(first.generation);
    expect(result.current.isCurrent(first)).toBe(false);
    expect(result.current.isCurrent(result.current.context)).toBe(true);
  });

  it("ends the context on a conflict and does not read the revision again", async () => {
    const reads = deferRevisionReads();
    const { result } = renderContext(A);
    await act(async () => reads[0]?.resolve(4));
    const captured = result.current.context;

    act(() => result.current.markConflict(captured));

    expect(result.current.context).toMatchObject({ status: "conflict", expectedRevision: null });
    expect(result.current.isCurrent(captured)).toBe(false);
    expect(mockedFetchRevision).toHaveBeenCalledTimes(1);
  });

  it("ignores a conflict reported for an earlier context", async () => {
    const reads = deferRevisionReads();
    const { result, rerender } = renderContext(A);
    await act(async () => reads[0]?.resolve(4));
    const earlier = result.current.context;
    rerender(B);
    await act(async () => reads[1]?.resolve(9));

    act(() => result.current.markConflict(earlier));

    await waitFor(() => expect(result.current.context).toMatchObject({ workflowId: "wf-b", status: "ready", expectedRevision: 9 }));
  });

  it("has no context for other entity types", () => {
    deferRevisionReads();
    const { result } = renderContext({ open: true, entityType: "tool", entityId: "t1", entryId: "3" });

    expect(result.current.context.status).toBe("idle");
    expect(mockedFetchRevision).not.toHaveBeenCalled();
  });
});
