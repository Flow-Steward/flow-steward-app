import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import userEvent from "@testing-library/user-event";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import {
  jobPayload,
  mockJobStream,
  mockedFetchJobCostBreakdown,
  mockedFetchJobDecisionOverrides,
  mockedFetchJobApproval,
  mockedFetchJobApprovalHistory,
  mockedFetchJobDetail,
  mockedFetchJobExecutionStepsPage,
  mockedUseJobStream,
  primeApi,
  renderPage,
} from "./index.test-helpers";
import { JobsDetailPage } from "@/pages/jobs/detail";
import { submitApprovalDecision } from "@/pages/jobs/detail/api";
import type * as JobApi from "@/pages/jobs/detail/api";
import { queryKeys } from "@/lib/query/keys";

describe("JobsDetailPage live updates", () => {
  it("preserves a typed optional approve note through a same-gate refresh and retained history", async () => {
    const actual = await vi.importActual<typeof JobApi>("@/pages/jobs/detail/api");
    const user = userEvent.setup();
    const draft = "  Approved after checking the recorded output  ";
    const approvalKey = ["jobs", "detail", "42", "approval"];
    const historyKey = ["jobs", "detail", "42", "approval-history"];
    primeApi(jobPayload({ status: "awaiting_approval", thread_id: "thread-42", execution_steps: [] }));
    const previousApproval = mockedFetchJobApproval.getMockImplementation();
    const previousHistory = mockedFetchJobApprovalHistory.getMockImplementation();
    const previousSubmit = vi.mocked(submitApprovalDecision).getMockImplementation();
    mockedFetchJobApproval.mockImplementation(actual.fetchJobApproval);
    mockedFetchJobApprovalHistory.mockImplementation(actual.fetchJobApprovalHistory);
    vi.mocked(submitApprovalDecision).mockImplementation(actual.submitApprovalDecision);
    let gate: JobApi.JobApprovalPayload | undefined = {
      job_id: "42", thread_id: "thread-42", approval_id: "gate-note",
      payload: { approval_id: "gate-note", thread_id: "thread-42", message: "Review the recorded output",
        allowed_actions: ["approve", "reject"],
        review_workspace: { kind: "document_review", comment_required_on_reject: true } },
    };
    const posts: JobApi.ApprovalDecisionPayload[] = [];
    const history: JobApi.JobApprovalHistoryPayload = { job_id: "42", approvals: [] };
    let approvalReads = 0;
    let retainedHistoryReads = 0;
    const transport = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = new URL(String(input), "http://localhost").pathname;
      const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
        status, headers: { "content-type": "application/json" } });
      if (path === "/api/jobs/42/approval") {
        approvalReads += 1;
        return gate ? json(gate) : json({ code: "approval_not_awaiting" }, 409);
      }
      if (path === "/api/approvals/thread-42" && init?.method === "POST") {
        const decision = JSON.parse(String(init.body)) as JobApi.ApprovalDecisionPayload;
        posts.push(decision);
        // Match ApprovalRepository's public history fields; PostgreSQL coverage
        // separately proves that the real read model retains this comment.
        history.approvals = [{ approval_id: decision.approval_id, approved: decision.approved,
          action: null, comment: decision.comment, stage: "Review", question: "", content_preview: "",
          override_content_preview: "", actor: "operator", actor_type: "user",
          timestamp: "2026-10-24T10:00:00Z" }];
        gate = undefined;
        mockedFetchJobDetail.mockResolvedValue(jobPayload({ status: "completed", thread_id: "thread-42", execution_steps: [] }) as never);
        return json({ status: "resume_pending", job_id: "42", approval_id: decision.approval_id }, 202);
      }
      if (path === "/api/jobs/42/approvals") {
        if (posts.length) retainedHistoryReads += 1;
        return json(history);
      }
      throw new Error(`Unexpected approval transport: ${init?.method || "GET"} ${path}`);
    });
    const view = renderPage();
    let reloaded: ReturnType<typeof renderPage> | undefined;
    try {
      const textarea = await screen.findByRole("textbox", { name: "Comment for rejection" });
      expect(textarea).not.toBeRequired();
      await user.type(textarea, draft);
      expect(textarea).toHaveValue(draft);
      const initialReads = approvalReads;
      gate = { ...gate!, payload: { ...gate!.payload, review_workspace: { kind: "document_review",
        comment_required_on_reject: true, intro: "Recorded output refreshed for the same gate" } } };
      await act(async () => {
        await view.queryClient.refetchQueries({ queryKey: queryKeys.jobDetail("42"), exact: true });
        await view.queryClient.refetchQueries({ queryKey: approvalKey, exact: true });
      });
      expect(approvalReads).toBeGreaterThan(initialReads);
      expect(await screen.findByText("Recorded output refreshed for the same gate")).toBeInTheDocument();
      expect(view.queryClient.getQueryData(approvalKey)).toMatchObject({ approval: { approval_id: "gate-note" } });
      expect(screen.getByRole("textbox", { name: "Comment for rejection" })).toHaveValue(draft);
      await user.click(screen.getByTestId("approval-approve-button"));
      await waitFor(() => expect(posts).toEqual([{
        approval_id: "gate-note", approved: true, comment: "Approved after checking the recorded output",
      }]));
      await screen.findByText("Decision history");
      expect(await screen.findByText("Approved after checking the recorded output")).toBeInTheDocument();
      expect(retainedHistoryReads).toBeGreaterThan(0);
      expect(view.queryClient.getQueryData(historyKey)).toMatchObject({ approvals: [{
        approval_id: "gate-note", approved: true, comment: "Approved after checking the recorded output",
      }] });
      expect(screen.queryByTestId("approval-approve-button")).not.toBeInTheDocument();
      expect(screen.queryByTestId("job-approval-error")).not.toBeInTheDocument();
      view.unmount();
      view.queryClient.clear();
      reloaded = renderPage();
      await screen.findByText("Decision history");
      expect(await screen.findByText("Approved after checking the recorded output")).toBeInTheDocument();
      expect(screen.queryByTestId("approval-comment-input")).not.toBeInTheDocument();
    } finally {
      reloaded?.unmount();
      reloaded?.queryClient.clear();
      view.unmount();
      view.queryClient.clear();
      transport.mockRestore();
      mockedFetchJobApproval.mockReset();
      mockedFetchJobApprovalHistory.mockReset();
      vi.mocked(submitApprovalDecision).mockReset();
      if (previousApproval) mockedFetchJobApproval.mockImplementation(previousApproval);
      if (previousHistory) mockedFetchJobApprovalHistory.mockImplementation(previousHistory);
      if (previousSubmit) vi.mocked(submitApprovalDecision).mockImplementation(previousSubmit);
    }
  });

  it.each(["completed", "awaiting_timer", "second_gate"])("clears the decision notice when the submitted gate advances to %s", async (nextState) => {
    primeApi(jobPayload({ status: "awaiting_approval", thread_id: "thread-42" }));
    mockedFetchJobApproval.mockResolvedValue({
      job_id: "42", thread_id: "thread-42", approval_id: "gate-first",
      payload: { approval_id: "gate-first", thread_id: "thread-42", allowed_actions: ["approve", "reject"] },
    });
    vi.mocked(submitApprovalDecision).mockResolvedValue({ status: "in_progress" } as never);
    renderPage();
    fireEvent.click(await screen.findByTestId("approval-approve-button"));
    expect(await screen.findByText("Decision submitted. Refreshing job state…")).toBeInTheDocument();
    // The same gate's refresh does not by itself mean the decision was applied.
    act(() => { mockJobStream.latestOptions?.onMessage?.({ type: "status", data: { status: "awaiting_approval" } }); });
    expect(screen.getByText("Decision submitted. Refreshing job state…")).toBeInTheDocument();
    mockedFetchJobDetail.mockImplementation(() => new Promise(() => undefined));
    mockedFetchJobApproval.mockImplementation(() => new Promise(() => undefined));
    act(() => {
      mockJobStream.latestOptions?.onMessage?.(nextState === "second_gate"
        ? { type: "approval", data: { approval_id: "gate-second", thread_id: "thread-42", allowed_actions: ["approve", "reject"] } }
        : { type: "status", data: { status: nextState, current_step_id: nextState === "completed" ? "accepted" : "wait_next" } });
    });
    await waitFor(() => expect(screen.queryByText("Decision submitted. Refreshing job state…")).not.toBeInTheDocument());
    if (nextState === "second_gate") expect(screen.getByTestId("approval-approve-button")).toBeEnabled();
  });

  it.each(["before", "after"])("does not carry a decision notice into another embedded job when submission resolves %s switching", async (resolutionOrder) => {
    primeApi(jobPayload({ status: "awaiting_approval", thread_id: "thread-42" }));
    mockedFetchJobApproval.mockResolvedValue({
      job_id: "42", thread_id: "thread-42", approval_id: "gate-first",
      payload: { approval_id: "gate-first", thread_id: "thread-42", allowed_actions: ["approve", "reject"] },
    });
    let finishSubmission!: () => void;
    vi.mocked(submitApprovalDecision).mockImplementation(() => new Promise((resolve) => {
      finishSubmission = () => resolve({ status: "in_progress" } as never);
    }));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const page = (jobId: string) => <QueryClientProvider client={queryClient}><MemoryRouter><JobsDetailPage embedded jobId={jobId} /></MemoryRouter></QueryClientProvider>;
    const view = render(page("42"));
    fireEvent.click(await screen.findByTestId("approval-approve-button"));
    await waitFor(() => expect(submitApprovalDecision).toHaveBeenCalledTimes(1));
    if (resolutionOrder === "before") {
      await act(async () => { finishSubmission(); });
      expect(await screen.findByText("Decision submitted. Refreshing job state…")).toBeInTheDocument();
    }
    mockedFetchJobDetail.mockResolvedValue(jobPayload({ id: "43", status: "awaiting_approval", thread_id: "thread-43" }) as never);
    mockedFetchJobApproval.mockResolvedValue({
      job_id: "43", thread_id: "thread-43", approval_id: "gate-new-run",
      payload: { approval_id: "gate-new-run", thread_id: "thread-43", allowed_actions: ["approve", "reject"] },
    });
    view.rerender(page("43"));
    await screen.findByTestId("approval-approve-button");
    if (resolutionOrder === "after") await act(async () => { finishSubmission(); });
    await waitFor(() => expect(screen.queryByText("Decision submitted. Refreshing job state…")).not.toBeInTheDocument());
    expect(screen.getByTestId("approval-approve-button")).toBeEnabled();
  });

  it.each(["empty", "conflict"])("polls a canonical gate after an identityless frame's %s response", async (responseKind) => {
    primeApi(jobPayload({ status: "awaiting_approval" }));
    mockedFetchJobApproval.mockResolvedValueOnce(undefined);
    if (responseKind === "empty") mockedFetchJobApproval.mockResolvedValueOnce(undefined);
    else mockedFetchJobApproval.mockRejectedValueOnce(new Error("Approval projection is not ready"));
    mockedFetchJobApproval.mockResolvedValue({
      job_id: "42", thread_id: "thread-polled", approval_id: "gate-polled",
      payload: {
        thread_id: "thread-polled", approval_id: "gate-polled",
        allowed_actions: ["approve", "reject"],
        review_workspace: { kind: "document_review", title: "Polled canonical gate" },
      },
    });
    vi.useFakeTimers();
    const view = renderPage();
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(50); });
      expect(mockedFetchJobApproval).toHaveBeenCalledTimes(1);
      act(() => {
        mockJobStream.latestOptions?.onMessage?.({
          type: "approval", data: { thread_id: "thread-polled" },
        });
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(50); });
      expect(mockedFetchJobApproval).toHaveBeenCalledTimes(2);
      expect(screen.queryByTestId("approval-approve-button")).not.toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(10_050); });
      expect(screen.getByText("Polled canonical gate")).toBeInTheDocument();
      expect(screen.getByTestId("approval-approve-button")).toBeEnabled();
      expect(mockedFetchJobApproval.mock.calls.length).toBeGreaterThanOrEqual(3);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it.each([false, true])("hydrates an approval frame from a fresh canonical query (identified duplicates: %s)", async (identifiedDuplicates) => {
    primeApi(jobPayload({ status: "awaiting_approval" }));
    mockedFetchJobApproval.mockResolvedValueOnce(undefined);
    const { queryClient } = renderPage();
    await screen.findByTestId("job-primary-container");
    await waitFor(() => expect(mockedFetchJobApproval).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(queryClient.getQueryState(["jobs", "detail", "42", "approval"])).toMatchObject({
      status: "success",
      fetchStatus: "idle",
    }));
    mockedFetchJobApproval.mockResolvedValue({
      job_id: "42", thread_id: "thread-current", approval_id: "gate-current",
      payload: {
        thread_id: "thread-current", approval_id: "gate-current",
        allowed_actions: ["approve", "reject"],
        review_workspace: { kind: "document_review", title: "Canonical review workspace" },
      },
    });
    const frame = {
      thread_id: "thread-current",
      ...(identifiedDuplicates ? { approval_id: "gate-current" } : {}),
      review_workspace: { kind: "document_review", title: "Generic streamed workspace" },
    };
    act(() => {
      mockJobStream.latestOptions?.onMessage?.({ type: "approval", data: frame });
      if (identifiedDuplicates) mockJobStream.latestOptions?.onMessage?.({ type: "approval", data: { ...frame } });
    });
    expect(await screen.findByText("Canonical review workspace")).toBeInTheDocument();
    expect(screen.getByTestId("approval-approve-button")).toBeEnabled();
    expect(screen.queryByText("Generic streamed workspace")).not.toBeInTheDocument();
    expect(mockedFetchJobApproval).toHaveBeenCalledTimes(2);
    if (identifiedDuplicates) {
      act(() => {
        mockJobStream.latestOptions?.onMessage?.({
          type: "approval", data: { ...frame, review_workspace: { kind: "document_review", title: "Canonical review workspace" } },
        });
      });
      expect(screen.getByText("Canonical review workspace")).toBeInTheDocument();
      expect(mockedFetchJobApproval).toHaveBeenCalledTimes(2);
    }
  });

  it("refreshes canonical summaries across a wait and a second approval gate", async () => {
    const first = jobPayload({
      status: "awaiting_approval",
      current_step_id: "review_first",
      lifecycle: {
        status: { code: "awaiting_approval", label: "Waiting for approval" },
        step: { code: "review_first", label: "Review First" },
      },
      sections: { review_workspace: { thread_id: "thread-42", approval_id: "gate-first", title: "First gate" } },
      projection: {
        hero_summary: { headline: "First gate summary", facts: [{ label: "Current step", value: "Review First" }] },
        next_action: { title: "Review the first gate", body: "First gate action" },
      },
      hero_summary: { headline: "Legacy first summary" },
      next_action: { title: "Legacy first action" },
    });
    primeApi(first);
    mockedFetchJobApproval.mockImplementation(() => new Promise(() => undefined));
    const view = renderPage();
    expect(await screen.findByText("First gate summary")).toBeInTheDocument();
    mockedFetchJobDetail.mockImplementation(() => new Promise(() => undefined));

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "status", data: { status: "awaiting_timer", current_step_id: "wait_second" },
      });
    });
    expect(screen.queryByText("First gate summary")).not.toBeInTheDocument();
    expect(screen.queryByText("First gate action")).not.toBeInTheDocument();
    expect(screen.queryByText("Legacy first summary")).not.toBeInTheDocument();
    await waitFor(() => expect(mockedFetchJobDetail).toHaveBeenCalledTimes(2));

    act(() => {
      view.queryClient.setQueryData(["jobs", "detail", "42"], jobPayload({
        status: "in_progress", current_step_id: "review_first",
        projection: {
          hero_summary: { headline: "First gate is in progress" },
          next_action: { title: "No action is required right now" },
        },
      }));
      mockJobStream.latestOptions?.onMessage?.({
        type: "approval",
        data: { thread_id: "thread-42", approval_id: "gate-second", step_id: "review_second", review_workspace: { thread_id: "thread-42", approval_id: "gate-second", title: "Second gate" } },
      });
    });
    expect(await screen.findByText("Second gate")).toBeInTheDocument();
    expect(screen.queryByText("First gate is in progress")).not.toBeInTheDocument();
    expect(screen.queryByText("No action is required right now")).not.toBeInTheDocument();
    await waitFor(() => expect(mockedFetchJobDetail).toHaveBeenCalledTimes(3));

    act(() => {
      view.queryClient.setQueryData(["jobs", "detail", "42"], jobPayload({
        status: "awaiting_approval", current_step_id: "review_second",
        lifecycle: {
          status: { code: "awaiting_approval", label: "Waiting for approval" },
          step: { code: "review_second", label: "Review Second" },
        },
        sections: { review_workspace: { thread_id: "thread-42", approval_id: "gate-second", title: "Second gate" } },
        projection: {
          hero_summary: { headline: "Canonical second gate summary" },
          next_action: { title: "Review the second gate", body: "Canonical second gate action" },
        },
      }));
    });
    expect(await screen.findByText("Canonical second gate summary")).toBeInTheDocument();
    expect(screen.getByText("Canonical second gate action")).toBeInTheDocument();
    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "status", data: { status: "awaiting_approval", current_step_id: "review_second" },
      });
      mockJobStream.latestOptions?.onMessage?.({
        type: "approval",
        data: { thread_id: "thread-42", approval_id: "gate-second", step_id: "review_second", review_workspace: { thread_id: "thread-42", approval_id: "gate-second", title: "Second gate" } },
      });
    });
    expect(screen.getByText("Canonical second gate summary")).toBeInTheDocument();
    expect(screen.getByText("Canonical second gate action")).toBeInTheDocument();
    expect(mockedFetchJobDetail).toHaveBeenCalledTimes(3);
  });

  it("shows a fail-closed approval conflict without clearing the active gate", async () => {
    primeApi(
      jobPayload({
        status: "awaiting_approval",
        sections: {
          review_workspace: {
            thread_id: "thread-42", approval_id: "gate-a",
            kind: "document_review",
            title: "Gate A workspace",
          },
        },
      }),
    );
    mockedFetchJobApproval
      .mockResolvedValueOnce({
        job_id: "42",
        thread_id: "thread-42", approval_id: "gate-a",
        payload: {
          thread_id: "thread-42", approval_id: "gate-a",
          review_workspace: { kind: "document_review", title: "Gate A workspace" },
        },
      })
      .mockRejectedValueOnce(new Error("Multiple simultaneous approval gates are not supported"));
    const view = renderPage();
    expect(await screen.findByText("Gate A workspace")).toBeInTheDocument();

    await act(async () => {
      await view.queryClient.invalidateQueries({ queryKey: ["jobs", "detail", "42", "approval"] });
    });

    expect(
      await screen.findByText("Multiple simultaneous approval gates are not supported"),
    ).toBeInTheDocument();
    expect(screen.getByText("Gate A workspace")).toBeInTheDocument();
    expect(
      screen.getByText("Approval decisions are temporarily unavailable. Refresh the gate before deciding."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Viewer mode is read-only/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("approval-approve-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("approval-reject-button")).not.toBeInTheDocument();
  });

  it("shows a new REST approval when its websocket approval frame was missed", async () => {
    primeApi(
      jobPayload({
        status: "awaiting_approval",
        thread_id: "thread-42", approval_id: "gate-a",
        sections: {
          review_workspace: {
            thread_id: "thread-42", approval_id: "gate-a",
            kind: "document_review",
            title: "Gate A workspace",
          },
        },
      }),
    );
    mockedFetchJobApproval.mockResolvedValue({
      job_id: "42",
      thread_id: "thread-42", approval_id: "gate-a",
      payload: {
        thread_id: "thread-42", approval_id: "gate-a",
        review_workspace: { kind: "document_review", title: "Gate A workspace" },
      },
    });
    const view = renderPage();
    expect(await screen.findByText("Gate A workspace")).toBeInTheDocument();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "approval",
        data: {
          thread_id: "thread-42", approval_id: "gate-a",
          review_workspace: { kind: "document_review", title: "Gate A workspace" },
        },
      });
      mockJobStream.latestOptions?.onMessage?.({
        type: "status",
        data: { status: "in_progress" },
      });
    });
    await waitFor(() => expect(screen.queryByText("Gate A workspace")).not.toBeInTheDocument());

    act(() => {
      view.queryClient.setQueryData(["jobs", "detail", "42", "approval"], {
        observedRevision: 2,
        approval: {
          job_id: "42",
          thread_id: "thread-42", approval_id: "gate-b",
          payload: {
            thread_id: "thread-42", approval_id: "gate-b",
            review_workspace: { kind: "document_review", title: "Gate B workspace" },
          },
        },
      });
    });

    expect(await screen.findByText("Gate B workspace")).toBeInTheDocument();
  });

  it("keeps the latest streamed approval when an older detail request finishes later", async () => {
    primeApi(jobPayload({ status: "awaiting_approval" }));
    mockedFetchJobApproval.mockImplementation(() => new Promise(() => undefined));
    const view = renderPage();
    await screen.findByTestId("job-primary-container");
    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "approval",
        data: {
          thread_id: "thread-42", approval_id: "gate-b",
          review_workspace: {
            kind: "document_review",
            title: "Gate B workspace",
          },
        },
      });
    });
    expect(await screen.findByText("Gate B workspace")).toBeInTheDocument();

    act(() => {
      view.queryClient.setQueryData(
        ["jobs", "detail", "42"],
        jobPayload({
          status: "awaiting_approval",
          thread_id: "thread-42", approval_id: "gate-a",
          sections: {
            review_workspace: {
              thread_id: "thread-42", approval_id: "gate-a",
              kind: "document_review",
              title: "Gate A workspace",
            },
          },
        }),
      );
      view.queryClient.setQueryData(["jobs", "detail", "42", "approval"], {
        observedRevision: 0,
        approval: {
          job_id: "42",
          thread_id: "thread-42", approval_id: "gate-a",
          payload: {
            thread_id: "thread-42", approval_id: "gate-a",
            review_workspace: {
              kind: "document_review",
              title: "Gate A workspace",
            },
          },
        },
      });
    });

    expect(screen.getByText("Gate B workspace")).toBeInTheDocument();
    expect(screen.queryByText("Gate A workspace")).not.toBeInTheDocument();
  });

  it("captures the approval revision before a deferred REST response", async () => {
    primeApi(jobPayload({ status: "awaiting_approval" }));
    let resolveApproval: ((value: unknown) => void) | undefined;
    mockedFetchJobApproval.mockImplementation(
      () => new Promise((resolve) => { resolveApproval = resolve; }) as never,
    );
    const view = renderPage();
    await screen.findByTestId("job-primary-container");
    await waitFor(() => expect(mockedFetchJobApproval).toHaveBeenCalled());

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "approval",
        data: {
          thread_id: "thread-42", approval_id: "gate-b",
          review_workspace: { kind: "document_review", title: "Gate B workspace" },
        },
      });
    });
    expect(await screen.findByText("Gate B workspace")).toBeInTheDocument();

    await act(async () => {
      resolveApproval?.({
        job_id: "42",
        thread_id: "thread-42", approval_id: "gate-a",
        payload: {
          thread_id: "thread-42", approval_id: "gate-a",
          review_workspace: { kind: "document_review", title: "Gate A workspace" },
        },
      });
    });
    await waitFor(() =>
      expect(view.queryClient.getQueryData(["jobs", "detail", "42", "approval"])).toMatchObject({
        approval: { approval_id: "gate-a" },
      }),
    );

    expect(screen.getByText("Gate B workspace")).toBeInTheDocument();
    expect(screen.queryByText("Gate A workspace")).not.toBeInTheDocument();
  });

  it("keeps the stream open through done and performs one final terminal refresh", async () => {
    primeApi(jobPayload());
    renderPage();

    await screen.findByTestId("job-workflow-link");
    await waitFor(() => expect(mockedFetchJobDetail).toHaveBeenCalledTimes(1));

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "status",
        data: { status: "completed", finished_at: "2026-04-25T11:11:00Z" },
      });
    });

    await waitFor(() => expect(mockJobStream.latestOptions?.enabled).toBe(true));

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({ type: "done", data: { status: "completed" } });
    });

    await waitFor(() => {
      expect(mockJobStream.latestOptions?.enabled).toBe(false);
      expect(mockedFetchJobDetail.mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(mockedFetchJobDecisionOverrides.mock.calls.length).toBeGreaterThanOrEqual(2);
      expect(mockedFetchJobCostBreakdown.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
  });

  it("keeps the stream open and retries when the final refresh fails", async () => {
    primeApi(jobPayload());
    renderPage();
    await waitFor(() => expect(mockedFetchJobDetail).toHaveBeenCalledTimes(1));
    mockedFetchJobDetail
      .mockRejectedValueOnce(new Error("temporary refresh failure"))
      .mockResolvedValue(jobPayload({ status: "completed" }) as never);

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({ type: "done", data: { status: "completed" } });
    });

    await waitFor(() => expect(mockedFetchJobDetail).toHaveBeenCalledTimes(2));
    expect(mockJobStream.latestOptions?.enabled).toBe(true);
    await waitFor(
      () => {
        expect(mockedFetchJobDetail.mock.calls.length).toBeGreaterThanOrEqual(3);
        expect(mockJobStream.latestOptions?.enabled).toBe(false);
      },
      { timeout: 2_000 },
    );
  });

it("prefers nested job payload over stale top-level job metadata", async () => {
    primeApi(
      jobPayload({
        task_summary: "Stale top-level task summary",
        selected_workflow: "wf_stale",
        selected_workflow_display_name: "Stale Workflow",
        started_at: "2026-04-25T09:00:00Z",
        finished_at: "2026-04-25T09:05:00Z",
        invocation: {
          channel: "manual_api",
          request_id: "stale-request-id",
        },
        failure_summary: {
          present: true,
          title: "Stale failure summary",
          summary: "This should not win.",
        },
        hero_summary: {},
        job: {
          id: 42,
          task_summary: "Nested job task summary",
          selected_workflow: "wf_nested",
          selected_workflow_display_name: "Nested Workflow",
          started_at: "2026-04-25T10:00:00Z",
          finished_at: "2026-04-25T10:02:00Z",
          invocation: {
            channel: "webhook",
            request_id: "nested-request-id",
          },
          failure_summary: {
            present: true,
            title: "Nested failure summary",
            summary: "Nested job failure summary.",
          },
        },
      }),
    );

    renderPage();

    expect((await screen.findAllByText("Nested job task summary")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Stale top-level task summary")).not.toBeInTheDocument();
    expect(screen.getByText("Workflow: Nested Workflow")).toBeInTheDocument();
    expect(screen.queryByText("Workflow: Stale Workflow")).not.toBeInTheDocument();
    expect(screen.queryByText("nested-request-id")).not.toBeInTheDocument();
    expect(screen.queryByText("stale-request-id")).not.toBeInTheDocument();
    expect(screen.getByText("Nested failure summary")).toBeInTheDocument();
    expect(screen.queryByText("Stale failure summary")).not.toBeInTheDocument();
  });

  it("updates workflow metadata when live status frames publish a new workflow selection", async () => {
    primeApi(
      jobPayload({
        selected_workflow: "wf_stale",
        selected_workflow_display_name: "Stale Workflow",
        job: {
          id: 42,
          selected_workflow: "wf_stale",
          selected_workflow_display_name: "Stale Workflow",
        },
      }),
    );

    renderPage();

    expect(await screen.findByText("Workflow: Stale Workflow")).toBeInTheDocument();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "status",
        data: {
          status: "in_progress",
          current_step_id: "analysis",
          selected_workflow: "wf_live",
          selected_workflow_display_name: "Live Workflow",
          selected_workflow_source: "runtime",
        },
      });
    });

    expect(await screen.findByText("Workflow: Live Workflow")).toBeInTheDocument();
    expect(screen.queryByText("Workflow: Stale Workflow")).not.toBeInTheDocument();
  });

  it("updates nested job timestamps when live status frames publish completion metadata", async () => {
    primeApi(
      jobPayload({
        finished_at: "2026-04-25T09:05:00Z",
        job: {
          id: 42,
          started_at: "2026-04-25T10:00:00Z",
          finished_at: "2026-04-25T09:05:00Z",
        },
      }),
    );

    renderPage();

    expect(await screen.findByText(/Finished: Apr 25, 2026, 12:05 PM/i)).toBeInTheDocument();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "status",
        data: {
          status: "completed",
          current_step_id: "complete",
          finished_at: "2026-04-25T11:11:00Z",
        },
      });
    });

    expect(await screen.findByText(/Finished: Apr 25, 2026, 2:11 PM/i)).toBeInTheDocument();
  });

  it("falls back to sections.selected_step when the execution step list is absent", async () => {
    primeApi(
      jobPayload({
        execution_steps: [],
        sections: {
          execution_steps: [],
          selected_step: {
            id: "section-selected-step",
            title: "Section-selected step",
            status: "completed",
            status_label: "Completed",
            group_id: "analysis",
            agent_label: "Planner",
            summary: "The canonical selected step came from sections.",
            result: "Selected-step result",
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByRole("button", { name: /section-selected step/i })).toBeInTheDocument();
    expect(screen.queryByText("No execution steps are available from the runtime feed yet.")).not.toBeInTheDocument();
    expect(screen.getAllByText("The canonical selected step came from sections.").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: /section-selected step/i }));
    expect(await screen.findByText("Selected-step result")).toBeInTheDocument();
  });

  it("keeps sections-backed decision history and diagnostics in sync when approval history frames arrive", async () => {
    primeApi(
      jobPayload({
        previous_decisions: [],
        sections: {
          previous_decisions: [
            {
              id: "stale-decision",
              title: "Stale section decision",
              status: "Recorded",
              summary: "This should be replaced.",
            },
          ],
          diagnostics: {
            tabs: {
              decisions: {
                count: 1,
                rows: [
                  {
                    id: "stale-decision",
                    title: "Stale section decision",
                    status: "Recorded",
                    summary: "This should be replaced.",
                  },
                ],
              },
              overrides: {
                count: 1,
                rows: [
                  {
                    id: "stale-decision",
                    title: "Stale section decision",
                    status: "Recorded",
                    summary: "This should be replaced.",
                  },
                ],
              },
            },
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByText("Stale section decision")).toBeInTheDocument();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "approval_history",
        data: [
          {
            id: "fresh-decision",
            title: "Fresh section decision",
            status: "Approved",
            summary: "The stream updated the canonical decision history.",
            timestamp: "2026-04-25T10:06:00Z",
          },
        ],
      });
    });

    expect(await screen.findByText("Fresh section decision")).toBeInTheDocument();
    expect(screen.queryByText("Stale section decision")).not.toBeInTheDocument();
  });

  it("keeps sections-backed cost totals in sync when cost stream frames arrive", async () => {
    primeApi(
      jobPayload({
        cost: {
          cost_usd: 0.12,
          tokens_total: 12,
        },
        sections: {
          cost_breakdown: {
            total_cost_usd: 0.12,
            total_cost_display: "$0.1200",
            total_tokens: 12,
            rows: [],
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByText("Total: $0.1200")).toBeInTheDocument();
    expect(screen.getByText("Tokens: 12")).toBeInTheDocument();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "cost",
        data: {
          total_cost_usd: 2.5,
          total_cost_display: "$2.5000",
          total_tokens: 900,
        },
      });
    });

    expect(await screen.findByText("Total: $2.5000")).toBeInTheDocument();
    expect(screen.getByText("Tokens: 900")).toBeInTheDocument();
  });

  it("shows a warning when invocation snapshot was truncated", async () => {
    primeApi(
      jobPayload({
        invocation: {
          channel: "webhook",
          trigger_family: "incoming_webhook",
          request_id: "req-truncated",
          truncated: true,
          mapped_inputs: { email: "demo@example.com" },
        },
      }),
    );

    renderPage();

    const invocation = await screen.findByTestId("job-inputs-panel");
    expect(
      within(invocation).getByText(/Some input details were clipped to fit the job metadata size limit/i),
    ).toBeInTheDocument();
  });

  it("renders Workflow not recorded when selected workflow is absent", async () => {
    primeApi(jobPayload({ selected_workflow: null, selected_workflow_display_name: null }));

    renderPage();

    expect(await screen.findByTestId("job-workflow-missing")).toHaveTextContent(
      "Workflow not recorded",
    );
    expect(screen.queryByTestId("job-workflow-link")).not.toBeInTheDocument();
  });

  it("merges websocket message frames into the execution waterfall and inspector", async () => {
    primeApi(jobPayload({ execution_steps: [] }));

    renderPage();

    expect(await screen.findByText("No execution steps are available from the runtime feed yet.")).toBeInTheDocument();
    expect(mockedUseJobStream).toHaveBeenCalled();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "message",
        data: {
          correlation_id: "live-step-1",
          from_agent: "reviewer",
          to_agent: "orchestrator",
          group_id: "design",
          skill: "architecture_review",
          status: "completed",
          summary: "Live websocket step arrived.",
        },
      });
    });

    const liveStep = await screen.findByRole("button", { name: /reviewer/i });
    expect(liveStep).toHaveTextContent("Live websocket step arrived.");
    fireEvent.click(liveStep);

    await waitFor(() => {
      const inspector = screen.getByTestId("job-step-inspector");
      expect(within(inspector).getAllByText("reviewer").length).toBeGreaterThan(0);
      expect(within(inspector).getAllByText("Live websocket step arrived.").length).toBeGreaterThan(0);
      expect(within(inspector).getByText("Group")).toBeInTheDocument();
      expect(within(inspector).getByText("Design")).toBeInTheDocument();
    });
  });

  it("does not duplicate canonical execution steps when matching websocket frames arrive", async () => {
    primeApi(
      jobPayload({
        execution_steps: [
          {
            id: "canonical-design-step",
            title: "Reviewer: Draft Plan",
            group_id: "design",
            agent_id: "reviewer",
            agent_label: "Reviewer",
            skill: "draft_plan",
            tool: "draft_plan",
            status: "completed",
            status_label: "Completed",
            summary: "Architecture designed.",
          },
        ],
      }),
    );

    renderPage();

    expect(await screen.findByRole("button", { name: /Reviewer: Draft Plan/i })).toBeInTheDocument();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "message",
        data: {
          correlation_id: "message-150",
          from_agent: "reviewer",
          to_agent: "orchestrator",
          group_id: "design",
          skill: "draft_plan",
          status: "completed",
          summary: "Architecture designed.",
        },
      });
    });

    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /reviewer/i })).toHaveLength(1);
    });
  });

  it("deduplicates live-shaped websocket rows against minimal canonical execution steps", async () => {
    primeApi(
      jobPayload({
        execution_steps: [
          {
            id: "canonical-writer",
            title: "writer",
            group_id: "analysis",
            status: "completed",
            summary: "Requirements analyzed.",
          },
          {
            id: "canonical-reviewer",
            title: "reviewer",
            group_id: "design",
            status: "completed",
            summary: "Architecture designed.",
          },
        ],
      }),
    );

    renderPage();

    expect(await screen.findByRole("button", { name: /writer/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /reviewer/i })).toBeInTheDocument();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "message",
        data: {
          correlation_id: "message-149",
          from_agent: "writer",
          to_agent: "orchestrator",
          group_id: "analysis",
          skill: "draft_outline",
          status: "completed",
          summary: "Requirements analyzed.",
        },
      });
      mockJobStream.latestOptions?.onMessage?.({
        type: "message",
        data: {
          correlation_id: "message-150",
          from_agent: "reviewer",
          to_agent: "orchestrator",
          group_id: "design",
          skill: "draft_plan",
          status: "completed",
          summary: "Architecture designed.",
        },
      });
    });

    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: /writer/i })).toHaveLength(1);
      expect(screen.getAllByRole("button", { name: /reviewer/i })).toHaveLength(1);
    });
  });
});


it("keeps the selected Pause execution inspector open when the same job completes", async () => {
  const prepare = {
    step_id: "prepare_local_audit_data", step_execution_id: "prepare-1", occurrence: 1,
    title: "Prepare local audit data", status: "completed", status_label: "Completed",
    started_at: "2026-10-18T12:24:00.000Z", finished_at: "2026-10-18T12:24:00.033Z", duration_ms: 33,
  };
  const pause = {
    step_id: "pause", step_execution_id: "pause-execution-1", occurrence: 1, attempt: 1,
    title: "Pause", status: "in_progress", status_label: "In progress",
    started_at: "2026-10-18T12:24:00.033Z", finished_at: null, duration_ms: null,
  };
  primeApi(jobPayload({ status: "awaiting_timer", current_step_id: "pause", execution_steps: [] }));
  mockedFetchJobExecutionStepsPage.mockResolvedValue({
    job_id: "42", run_terminal: false, steps: [prepare, pause],
    page: { count: 2, has_more: false, next_cursor: "" },
  });
  const { queryClient } = renderPage({ embedded: true, jobId: "42" });
  await screen.findByTestId("job-run-overview");
  const waterfall = screen.getByTestId("job-execution-waterfall");
  fireEvent.click(await within(waterfall).findByRole("button", { name: /^Pause(?=\s|In progress\b|Completed\b)/ }));
  const inspector = await screen.findByTestId("job-step-inspector-drawer");
  expect(within(inspector).getByRole("heading", { name: "Pause" })).toBeInTheDocument();
  for (const status of within(inspector).getAllByText("In progress")) expect(status).toBeVisible();

  mockedFetchJobDetail.mockResolvedValue(jobPayload({
    status: "completed", current_step_id: "done", finished_at: "2026-10-18T12:24:30.033Z", execution_steps: [],
  }) as never);
  mockedFetchJobExecutionStepsPage.mockResolvedValue({
    job_id: "42", run_terminal: true,
    steps: [prepare, { ...pause, status: "completed", status_label: "Completed", finished_at: "2026-10-18T12:24:30.033Z", duration_ms: 30000 }],
    page: { count: 2, has_more: false, next_cursor: "" },
  });
  await act(async () => {
    await queryClient.refetchQueries({ queryKey: queryKeys.jobDetail("42"), exact: true });
    await queryClient.refetchQueries({ queryKey: queryKeys.jobExecutionSteps("42", ""), exact: true });
  });

  await waitFor(() => {
    const stillOpen = screen.getByTestId("job-step-inspector-drawer");
    expect(within(stillOpen).getByRole("heading", { name: "Pause" })).toBeInTheDocument();
    for (const status of within(stillOpen).getAllByText("Completed")) expect(status).toBeVisible();
    expect(within(stillOpen).getByText("30s")).toBeInTheDocument();
    expect(within(waterfall).getByRole("button", { name: /^Pause(?=\s|In progress\b|Completed\b)/ })).toHaveAttribute("aria-pressed", "true");
    expect(within(waterfall).getByRole("button", { name: /Prepare local audit data/ })).toHaveAttribute("aria-pressed", "false");
  });
  fireEvent.click(within(screen.getByTestId("job-step-inspector-drawer")).getByRole("button", { name: "Close step details" }));
  await act(async () => { await queryClient.refetchQueries({ queryKey: queryKeys.jobDetail("42"), exact: true }); });
  expect(screen.queryByTestId("job-step-inspector-drawer")).not.toBeInTheDocument();
});

it("keeps the selected Pause occurrence when an older history page is prepended", async () => {
  primeApi(jobPayload({ status: "completed", execution_steps: [] }));
  const latest = {
    step_id: "pause", step_execution_id: "pause-execution-2", occurrence: 2, attempt: 2,
    title: "Pause", status: "completed", status_label: "Completed",
    started_at: "2026-10-18T12:24:30.000Z", finished_at: "2026-10-18T12:25:00.000Z", duration_ms: 30000,
  };
  type HistoryPage = { job_id: string; steps: Array<Record<string, unknown>>; page: { count: number; has_more: boolean; next_cursor: string } };
  let releaseEarlier!: (page: HistoryPage) => void;
  mockedFetchJobExecutionStepsPage.mockImplementation(async (_jobId, cursor) => cursor
    ? new Promise<HistoryPage>((resolve) => { releaseEarlier = resolve; })
    : { job_id: "42", run_terminal: true, steps: [latest], page: { count: 1, has_more: true, next_cursor: "older-pause" } });
  renderPage({ embedded: true, jobId: "42" });
  await screen.findByTestId("job-run-overview");
  const waterfall = screen.getByTestId("job-execution-waterfall");
  fireEvent.click(await within(waterfall).findByRole("button", { name: "Load earlier steps" }));
  fireEvent.click(within(waterfall).getByRole("button", { name: /^Pause(?=\s|In progress\b|Completed\b)/ }));
  const inspector = await screen.findByTestId("job-step-inspector-drawer");
  expect(within(inspector).getByText("2")).toBeInTheDocument();
  expect(within(inspector).getByText("30s")).toBeInTheDocument();

  await act(async () => {
    releaseEarlier({
      job_id: "42", steps: [{ ...latest, step_execution_id: "pause-execution-1", occurrence: 1, attempt: 1,
        started_at: "2026-10-18T12:24:00.000Z", finished_at: "2026-10-18T12:24:00.033Z", duration_ms: 33 }],
      page: { count: 1, has_more: false, next_cursor: "" },
    });
  });

  await waitFor(() => expect(within(waterfall).getAllByRole("button", { name: /^Pause(?=\s|In progress\b|Completed\b)/ })).toHaveLength(2));
  const stillOpen = screen.getByTestId("job-step-inspector-drawer");
  expect(within(stillOpen).getByRole("heading", { name: "Pause" })).toBeInTheDocument();
  expect(within(stillOpen).getByText("2")).toBeInTheDocument();
  expect(within(stillOpen).getByText("30s")).toBeInTheDocument();
  expect(within(stillOpen).queryByText("33ms")).not.toBeInTheDocument();
  expect(within(waterfall).getAllByRole("button", { name: /^Pause(?=\s|In progress\b|Completed\b)/ }).map((button) => button.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
});
