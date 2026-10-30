// eslint-disable-next-line testing-library/no-manual-cleanup -- Unmount before restoring SVG and global descriptors.
import { cleanup, act, fireEvent, screen, waitFor, within } from "@testing-library/react";

import {
  jobPayload,
  longStepId,
  mockJobStream,
  mockedFetchJobApproval,
  mockedFetchJobApprovalHistory,
  mockedFetchJobDefinition,
  mockedFetchJobDetail,
  mockedFetchJobExecutionStepsPage,
  mockedFetchJobIncident,
  primeApi,
  renderEmbeddedPage,
  renderPage,
} from "./index.test-helpers";
import { submitApprovalDecision } from "@/pages/jobs/detail/api";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";

let getBBoxBefore: PropertyDescriptor | undefined;
let resizeObserverBefore: PropertyDescriptor | undefined;
beforeEach(() => {
  getBBoxBefore = Object.getOwnPropertyDescriptor(SVGElement.prototype, "getBBox");
  resizeObserverBefore = Object.getOwnPropertyDescriptor(globalThis, "ResizeObserver");
  Object.defineProperty(SVGElement.prototype, "getBBox", {
    configurable: true, value: () => ({ x: 0, y: 0, width: 120, height: 16 }),
  });
  Object.defineProperty(globalThis, "ResizeObserver", {
    configurable: true, writable: true,
    value: class { observe() {} unobserve() {} disconnect() {} },
  });
});
afterEach(() => {
  cleanup();
  if (getBBoxBefore) Object.defineProperty(SVGElement.prototype, "getBBox", getBBoxBefore);
  else Reflect.deleteProperty(SVGElement.prototype, "getBBox");
  if (resizeObserverBefore) Object.defineProperty(globalThis, "ResizeObserver", resizeObserverBefore);
  else Reflect.deleteProperty(globalThis, "ResizeObserver");
});

describe("JobsDetailPage canonical rendering", () => {
  it("lets each repeated loop execution open its own recorded inputs", async () => {
    primeApi(jobPayload({
      status: "completed",
      execution_steps: [
        { step_id: "prepare", step_execution_id: "prepare-1", title: "prepare" },
        { step_id: "left_record", step_execution_id: "left-1", title: "left_record", inputs: { item: "alpha" } },
        { step_id: "right_record", step_execution_id: "right-1", title: "right_record", inputs: { item: "alpha" } },
        { step_id: "left_record", step_execution_id: "left-2", title: "left_record", inputs: { item: "beta" } },
        { step_id: "right_record", step_execution_id: "right-2", title: "right_record", inputs: { item: "beta" } },
        { step_id: "done", step_execution_id: "done-1", title: "done" },
      ].map((row) => ({ ...row, status: "completed" })),
    }));
    renderPage();

    await screen.findByRole("button", { name: /prepare/i });
    const leftSteps = screen.getAllByRole("button", { name: /left_record/i });
    expect(leftSteps).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /right_record/i })).toHaveLength(2);
    expect(screen.getByRole("button", { name: /^done/i })).toBeInTheDocument();
    fireEvent.click(leftSteps[0]);
    expect(within(await screen.findByTestId("job-step-inspector")).getByText("alpha")).toBeInTheDocument();
    fireEvent.click(leftSteps[1]);
    const inspector = await screen.findByTestId("job-step-inspector");
    expect(within(inspector).getByText("beta")).toBeInTheDocument();
    expect(within(inspector).queryByText("alpha")).not.toBeInTheDocument();
  });

  it.each([false, true])("keeps a generated tabular CSV visible after reload in embedded=%s", async (embedded) => {
    const detail = jobPayload({
      status: "completed",
      execution_steps: [{
        id: "export-csv",
        title: "Export CSV",
        status: "completed",
        status_label: "Completed",
        summary: "tabular_feed step executed",
        outputs: { row_count: 1 },
        output_artifacts: [{
          artifact_id: "art-export",
          role: "output",
          filename: "catalog.csv",
          content_type: "text/csv",
          size_bytes: 10,
          available: true,
          download_url: "/api/jobs/42/artifacts/art-export",
        }],
      }],
    });
    primeApi({ ...detail, status: "in_progress" });
    const renderExport = () => embedded ? renderEmbeddedPage("42") : renderPage();
    const openExport = async () => {
      if (!screen.queryByTestId("job-step-inspector-drawer")) {
        fireEvent.click(await screen.findByRole("button", { name: /Export CSV/i }));
      }
      const inspector = await screen.findByTestId("job-step-inspector-drawer");
      const files = within(inspector).getByRole("region", { name: "Generated files" });
      expect(within(files).getAllByRole("link")).toHaveLength(1);
      expect(within(files).getByRole("link")).toHaveAttribute("href", "/api/jobs/42/artifacts/art-export");
      expect(within(files).getByText("catalog.csv")).toBeInTheDocument();
      expect(within(inspector).getByText("Row Count", { selector: "dt" })).toBeInTheDocument();
      expect(within(inspector).queryByText("No step outputs were recorded.")).not.toBeInTheDocument();
    };
    const view = renderExport();
    await openExport();
    mockedFetchJobDetail.mockResolvedValue(detail as never);
    act(() => {
      mockJobStream.latestOptions?.onMessage?.({ type: "done", data: { status: "completed" } });
    });
    await waitFor(() => expect(mockedFetchJobDetail.mock.calls.length).toBeGreaterThan(1));
    await waitFor(() => expect(mockJobStream.latestOptions?.enabled).toBe(false));
    await openExport();
    view.unmount();
    renderExport();
    await openExport();
  });

  it.each([false, true])("does not offer a download for an unavailable tabular file in embedded=%s", async (embedded) => {
    primeApi(jobPayload({
      status: "completed",
      execution_steps: [{
        id: "export-csv",
        title: "Export CSV",
        status: "completed",
        outputs: { row_count: 1 },
        output_artifacts: [{
          artifact_id: "art-export",
          role: "output",
          filename: "catalog.csv",
          available: false,
          download_url: null,
        }],
      }],
    }));
    if (embedded) renderEmbeddedPage("42"); else renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Export CSV/i }));
    const inspector = await screen.findByTestId("job-step-inspector-drawer");
    expect(within(inspector).getByText("catalog.csv")).toBeInTheDocument();
    expect(within(inspector).getByText("Download unavailable")).toBeInTheDocument();
    expect(within(inspector).queryByRole("link")).not.toBeInTheDocument();
  });

  it("loads the exact explicit job ID when embedded in the workflow builder", async () => {
    primeApi(jobPayload({ id: "job-explicit-88" }));

    renderEmbeddedPage("job-explicit-88");

    await waitFor(() =>
      expect(mockedFetchJobDetail).toHaveBeenCalledWith("job-explicit-88"),
    );
    expect(screen.getByTestId("workflow-builder-job-details")).toHaveAttribute(
      "data-job-id",
      "job-explicit-88",
    );
    expect(
      screen.getByRole("link", { name: "Open full Job Details" }),
    ).toHaveAttribute("href", "/jobs/job-explicit-88");
  });

  it("carries the settings-portal typography class only when embedded in the workflow builder", async () => {
    primeApi(jobPayload({ id: "job-explicit-88" }));

    renderEmbeddedPage("job-explicit-88");

    await waitFor(() =>
      expect(mockedFetchJobDetail).toHaveBeenCalledWith("job-explicit-88"),
    );
    expect(screen.getByTestId("workflow-builder-job-details")).toHaveClass(
      "fs-settings-portal",
    );
  });

  it("does not carry the settings-portal typography class outside the workflow builder", async () => {
    primeApi(jobPayload({ status: "completed", finished_at: "2026-04-25T10:02:00Z" }));

    renderPage();

    // Anchor on the element under test, not on the job's title: the class depends
    // only on `embedded`, and the title text changes with the product.
    expect(await screen.findByTestId("job-detail-page")).not.toHaveClass(
      "fs-settings-portal",
    );
  });

  it("centers the page on the workflow and a readable run overview instead of the job UUID", async () => {
    primeApi(jobPayload({ status: "completed", finished_at: "2026-04-25T10:02:00Z" }));

    renderPage();

    expect(await screen.findByRole("heading", { name: "Weekly Report" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Job #42 detail/i })).not.toBeInTheDocument();
    expect(screen.getByText("Investigate the migrated job detail page")).toBeInTheDocument();
    const overview = screen.getByTestId("job-run-overview");
    expect(within(overview).getByText("Run overview")).toBeInTheDocument();
    expect(within(overview).getByText("Completed")).toBeInTheDocument();
    expect(screen.getByTestId("job-download-report")).toHaveTextContent("Download full report");
  });

  it("restores durable approval decisions after a compact terminal-job reload", async () => {
    primeApi(jobPayload({ status: "completed", finished_at: "2026-04-25T10:02:00Z" }));
    mockedFetchJobApprovalHistory.mockResolvedValue({
      job_id: "42",
      approvals: [
        {
          action: "reject",
          approved: false,
          stage: "Catalog review",
          comment: "Fix the supplier mapping",
          decided_at: "2026-04-25T10:01:00Z",
        },
      ],
    } as never);

    renderPage();

    expect(await screen.findByText("Fix the supplier mapping")).toBeInTheDocument();
    expect(screen.getByText("Catalog review decision")).toBeInTheDocument();
    expect(mockedFetchJobIncident).not.toHaveBeenCalled();
  });

  it("shows report preparation immediately and blocks duplicate download requests", async () => {
    primeApi(jobPayload({ status: "completed", finished_at: "2026-04-25T10:02:00Z" }));
    const downloadUrls: string[] = [];
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      downloadUrls.push(this.href);
    });

    renderPage();

    const download = await screen.findByRole("button", { name: "Download full report" });
    fireEvent.click(download);
    fireEvent.click(download);

    expect(download).toBeDisabled();
    expect(download).toHaveTextContent("Preparing report…");
    expect(anchorClick).toHaveBeenCalledTimes(1);
    expect(downloadUrls[0]).toMatch(
      /^http:\/\/localhost(?::\d+)?\/api\/jobs\/42\/report\?download_token=[A-Za-z0-9_-]{16,64}$/,
    );
  });

  it("renders launch inputs and files as readable fields without technical invocation details", async () => {
    primeApi(
      jobPayload({
        invocation: {
          channel: "provider_webhook",
          request_id: "req-42",
          source_ip: "203.0.113.10",
          mapped_inputs: {
            email: "[REDACTED]",
            approved: true,
          },
          files: [
            {
              filename: "sample.csv",
              size_bytes: 128,
              download_url: "/api/jobs/42/attachments/7",
            },
          ],
        },
      }),
    );

    renderPage();

    const inputs = await screen.findByTestId("job-inputs-panel");
    expect(within(inputs).getByText("Inputs")).toBeInTheDocument();
    expect(within(inputs).getByText("Email")).toBeInTheDocument();
    expect(within(inputs).getByText("[REDACTED]")).toBeInTheDocument();
    expect(within(inputs).getByText("Approved")).toBeInTheDocument();
    expect(within(inputs).getByText("Yes")).toBeInTheDocument();
    expect(within(inputs).getByText("sample.csv")).toBeInTheDocument();
    expect(within(inputs).getByRole("link", { name: "Download" })).toHaveAttribute(
      "href",
      "/api/jobs/42/attachments/7",
    );
    expect(screen.queryByText("req-42")).not.toBeInTheDocument();
  });

  it("names what started the run and links to the parent run", async () => {
    primeApi(
      jobPayload({
        job: {
          trigger: {
            kind: "parent_workflow",
            label: "Another workflow",
            detail: "workflow wf-parent",
            link: "/jobs/parent-7",
          },
        },
      }),
    );

    renderPage();

    const trigger = await screen.findByTestId("job-trigger");
    expect(trigger).toHaveTextContent("Started by: Another workflow (workflow wf-parent)");
    expect(trigger).toHaveAttribute("href", "/jobs/parent-7");
  });

  it("opens a full step debugger with timing, inputs, outputs, artifacts, and errors", async () => {
    primeApi(
      jobPayload({
        execution_steps: [
          {
            id: "sync-products",
            title: "Sync products",
            status: "failed",
            status_label: "Failed",
            group_id: "import",
            tool: "Acme Shop catalog",
            started_at: "2026-04-25T10:00:00Z",
            finished_at: "2026-04-25T10:00:08Z",
            duration_ms: 8000,
            inputs: { sku: "VINYL-42", quantity: 3 },
            outputs: { imported: 0 },
            artifacts: {
              report: {
                filename: "import-report.csv",
                download_url: "/api/jobs/42/attachments/8",
              },
            },
            error: "Acme Shop returned HTTP 429 rate limit.",
          },
        ],
      }),
    );

    renderPage();

    await screen.findByRole("button", { name: /Sync products/i });
    expect(screen.queryByTestId("job-step-inspector-drawer")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Sync products/i }));

    const inspector = await screen.findByTestId("job-step-inspector-drawer");
    expect(within(inspector).getByText("Step details")).toBeInTheDocument();
    expect(within(inspector).getByText("Inputs")).toBeInTheDocument();
    expect(within(inspector).getByText("VINYL-42")).toBeInTheDocument();
    expect(within(inspector).getByText("Outputs")).toBeInTheDocument();
    expect(within(inspector).getByText("Imported", { selector: "dt" })).toBeInTheDocument();
    expect(within(inspector).getByText("Errors")).toBeInTheDocument();
    expect(within(inspector).getByText("Acme Shop returned HTTP 429 rate limit.")).toBeInTheDocument();
    expect(within(inspector).getByRole("link", { name: /import-report\.csv/i })).toHaveAttribute(
      "href",
      "/api/jobs/42/attachments/8",
    );
    expect(within(inspector).getByText("8s")).toBeInTheDocument();
  });

it("renders the selected workflow link and keeps raw payloads out of primary UI", async () => {
    primeApi(jobPayload());

    renderPage();

    const workflowLink = await screen.findByTestId("job-workflow-link");
    expect(workflowLink).toHaveAttribute("href", "/registry/workflows/wf_weekly_report");
    expect(workflowLink).toHaveTextContent("Workflow: Weekly Report");

    const primary = screen.getByTestId("job-primary-container");
    expect(within(primary).queryByText("Raw step payload")).not.toBeInTheDocument();
    expect(within(primary).queryByText("No additional message payload.")).not.toBeInTheDocument();
    expect(within(primary).queryByText("super-raw-json-hidden")).not.toBeInTheDocument();
    expect(within(primary).queryByText(longStepId)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Review: Verification/i }));
    const inspector = await screen.findByTestId("job-step-inspector");
    expect(inspector).toBeInTheDocument();
    expect(within(inspector).getByRole("heading", { name: "Review: Verification" })).toBeInTheDocument();
    expect(within(inspector).getAllByText("Verification is running.").length).toBeGreaterThan(0);
  });

  it("renders fenced phase result output as user-readable review evidence", async () => {
    const reviewPhaseResult = [
      "```json",
      JSON.stringify({
        schema_version: "phase_result_v2",
        status: "pass",
        summary: "Automated review completed successfully with all checks passing.",
        artifacts: {
          review_verdict: "pass",
          findings: "tone, structure, and citation checks passed.",
        },
      }),
      "```",
    ].map((value) => stringFromUnknownValue(value)).join("\n");

    primeApi(
      jobPayload({
        execution_steps: [
          {
            id: "review-step",
            title: "Review: Check draft",
            status: "completed",
            status_label: "Completed",
            group_id: "review",
            agent_label: "Reviewer",
            tool: "Check draft",
            summary: "Review completed.",
            result: reviewPhaseResult,
            artifacts: {
              raw_output: reviewPhaseResult,
              review_verdict: "pass",
              findings: "tone, structure, and citation checks passed.",
            },
          },
        ],
      }),
    );

    renderPage();

    const stepButton = await screen.findByRole("button", { name: /Review: Check draft/i });
    fireEvent.click(stepButton);
    const inspector = await screen.findByTestId("job-step-inspector");
    expect(
      within(inspector).getAllByText(
        "Automated review completed successfully with all checks passing.",
      ).length,
    ).toBeGreaterThan(0);
    expect(
      within(inspector).getAllByText("tone, structure, and citation checks passed.").length,
    ).toBeGreaterThan(0);
    expect(within(inspector).getByText("Structured result")).toBeInTheDocument();
    expect(within(inspector).queryByText("Raw Output")).not.toBeInTheDocument();
    expect(within(inspector).queryByText(/schema_version/)).not.toBeInTheDocument();
    expect(within(inspector).queryByText(/```json/)).not.toBeInTheDocument();
  });

  it("hides invocation panel when the job has no invocation snapshot", async () => {
    primeApi(jobPayload({ invocation: undefined }));

    renderPage();

    await screen.findByText("Run overview");
    expect(screen.queryByTestId("job-inputs-panel")).not.toBeInTheDocument();
  });

  it("prefers lifecycle status and step labels over stale top-level fields", async () => {
    primeApi(
      jobPayload({
        status: "failed",
        current_step_id: "legacy_step",
        lifecycle: {
          status: {
            code: "awaiting_approval",
            label: "Waiting for approval",
          },
          step: {
            code: "approval_review",
            label: "Approval Review",
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByText(/Status: Waiting for approval/i)).toBeInTheDocument();
    expect(screen.getByText(/Step: Approval Review/i)).toBeInTheDocument();
  });

  it("prefers nested job status and step over stale top-level fields when lifecycle is absent", async () => {
    primeApi(
      jobPayload({
        status: "failed",
        current_step_id: "legacy_step",
        job: {
          id: 42,
          status: "awaiting_approval",
          current_step_id: "approval_review",
        },
        lifecycle: undefined,
      }),
    );

    renderPage();

    // The status group is the headline; its detailed label adds nothing here.
    expect(await screen.findByText("Status: Waiting for approval")).toBeInTheDocument();
    expect(screen.getByText(/Step: Approval Review/i)).toBeInTheDocument();
  });

  it("keeps lifecycle-backed header state in sync when approval stream frames arrive", async () => {
    primeApi(
      jobPayload({
        status: "in_progress",
        current_step_id: "analysis",
        lifecycle: {
          status: {
            code: "in_progress",
            label: "In progress",
            terminal: false,
          },
          step: {
            code: "analysis",
            label: "Analysis",
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByText(/Status: In progress/i)).toBeInTheDocument();
    expect(screen.getByText(/Step: Analysis/i)).toBeInTheDocument();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "approval",
        data: {
          step_id: "approval_review",
          review_workspace: {
            kind: "document_review",
            title: "Review workspace",
          },
        },
      });
    });

    expect(screen.getByText(/Status: Waiting for approval/i)).toBeInTheDocument();
    expect(screen.getByText(/Step: Approval Review/i)).toBeInTheDocument();
  });

  it("keeps sections-backed review workspace in sync when approval stream frames arrive", async () => {
    primeApi(
      jobPayload({
        status: "in_progress",
        current_step_id: "analysis",
        sections: {
          review_workspace: {
            kind: "document_review",
            approval_id: "gate-section-old",
            thread_id: "thread-ready",
            title: "Stale section workspace",
            intro: "This should be replaced by the stream patch.",
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByText("Stale section workspace")).toBeInTheDocument();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "approval",
        data: {
          step_id: "approval_review",
          review_workspace: {
            kind: "document_review",
            approval_id: "gate-section-live",
            thread_id: "thread-ready",
            title: "Live review workspace",
            intro: "The approval stream updated the canonical sections payload.",
          },
        },
      });
    });

    expect(screen.getByText("Live review workspace")).toBeInTheDocument();
    expect(screen.queryByText("Stale section workspace")).not.toBeInTheDocument();
  });

  it("clears gate-scoped review workspace when the next approval has none", async () => {
    primeApi(
      jobPayload({
        status: "awaiting_approval",
        sections: {
          review_workspace: {
            kind: "document_review",
            approval_id: "gate-a",
            thread_id: "thread-ready",
            title: "Gate A workspace",
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByText("Gate A workspace")).toBeInTheDocument();

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "approval",
        data: {
          approval_id: "gate-b",
          interrupt_id: "gate-b",
          step_id: "approval_review",
        },
      });
    });

    expect(screen.queryByText("Gate A workspace")).not.toBeInTheDocument();
  });

  it("submits a sequential stream gate with the stable thread from canonical job detail", async () => {
    primeApi(
      jobPayload({
        status: "awaiting_approval",
        job: {
          id: 42,
          status: "awaiting_approval",
          thread_id: "thread-stable",
        },
        lifecycle: {
          status: { code: "awaiting_approval", label: "Waiting for approval" },
          approval: { state: "awaiting_approval" },
        },
      }),
    );
    vi.mocked(submitApprovalDecision).mockResolvedValue({ status: "in_progress" } as never);

    renderPage();
    await screen.findByText(/Status: Waiting for approval/i);

    act(() => {
      mockJobStream.latestOptions?.onMessage?.({
        type: "approval",
        data: {
          approval_id: "gate-next",
          interrupt_id: "gate-next",
          gate_kind: "autonomy_gate",
          stage: "Next gate",
        },
      });
    });

    fireEvent.click(screen.getByRole("button", { name: "Approve and continue" }));

    await waitFor(() =>
      expect(submitApprovalDecision).toHaveBeenCalledWith("thread-stable", {
        approval_id: "gate-next",
        approved: true,
        comment: "",
      }),
    );
    expect(screen.queryByText(/no thread ID recorded/i)).not.toBeInTheDocument();
  });

  it("uses nested job metadata for fallback review workspace content", async () => {
    primeApi(
      jobPayload({
        status: "awaiting_approval",
        task_summary: "Stale top-level summary",
        review_workspace: undefined,
        job: {
          id: 42,
          thread_id: "thread-nested",
          task_summary: "Nested job review summary",
        },
        lifecycle: {
          status: {
            code: "awaiting_approval",
            label: "Waiting for approval",
          },
          step: {
            code: "approval_review",
            label: "Approval Review",
          },
          approval: {
            state: "awaiting_approval",
            gate: { approval_id: "gate-nested" },
          },
        },
      }),
    );

    renderPage();

    const reviewWorkspace = await screen.findByTestId("job-review-workspace");
    expect(reviewWorkspace).toBeInTheDocument();
    expect(within(reviewWorkspace).getByText("Nested job review summary")).toBeInTheDocument();
    expect(screen.queryByText("Stale top-level summary")).not.toBeInTheDocument();
  });

  it("prefers lifecycle sections for execution steps and review workspace when top-level sections are absent", async () => {
    primeApi(
      jobPayload({
        execution_steps: [],
        review_workspace: undefined,
        lifecycle: {
          status: {
            code: "awaiting_approval",
            label: "Waiting for approval",
          },
          step: {
            code: "approval_review",
            label: "Approval Review",
          },
          approval: {
            state: "awaiting_approval",
            review_workspace: {
              kind: "document_review",
              approval_id: "gate-lifecycle",
              thread_id: "thread-ready",
              title: "Lifecycle review workspace",
              intro: "Lifecycle-backed review workspace.",
            },
          },
          steps: [
            {
              id: "lifecycle-step-1",
              title: "Lifecycle Step",
              status: "completed",
              status_label: "Completed",
              summary: "Step sourced from lifecycle.",
            },
          ],
        },
      }),
    );

    renderPage();

    expect(await screen.findByRole("button", { name: /Lifecycle Step/i })).toBeInTheDocument();
    expect(screen.getByText("Lifecycle review workspace")).toBeInTheDocument();
  });

  it("prefers explicit sections payload over stale top-level projection aliases", async () => {
    primeApi(
      jobPayload({
        execution_steps: [
          {
            id: "stale-top-level-step",
            title: "Stale Top Level Step",
            status: "completed",
            status_label: "Completed",
            summary: "This should not win.",
          },
        ],
        review_workspace: {
          kind: "document_review",
          title: "Stale top-level workspace",
          intro: "This should not win.",
        },
        previous_decisions: [
          {
            id: "stale-decision",
            title: "Stale decision",
            status: "Recorded",
            summary: "This should not win.",
          },
        ],
        sections: {
          execution_steps: [
            {
              id: "section-step-1",
              title: "Section Step",
              status: "completed",
              status_label: "Completed",
              summary: "Step sourced from sections.",
            },
          ],
          review_workspace: {
            kind: "document_review",
            approval_id: "gate-section",
            thread_id: "thread-ready",
            title: "Section review workspace",
            intro: "Review workspace sourced from sections.",
          },
          previous_decisions: [
            {
              id: "section-decision",
              title: "Section decision",
              status: "Recorded",
              summary: "Decision sourced from sections.",
            },
          ],
          cost_breakdown: {
            rows: [
              {
                agent_label: "Planner",
                model: "gpt-5",
                tokens: 200,
                calls: 1,
                cost_usd: 0.12,
                cost_display: "$0.1200",
              },
            ],
            total_cost_usd: 0.12,
            total_cost_display: "$0.12",
          },
          diagnostics: {
            tabs: {
              decisions: {
                count: 1,
                rows: [
                  {
                    id: "section-decision",
                    title: "Section decision",
                    status: "Recorded",
                    summary: "Decision sourced from sections.",
                  },
                ],
              },
            },
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByRole("button", { name: /Section Step/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Stale Top Level Step/i })).not.toBeInTheDocument();
    expect(screen.getByText("Section review workspace")).toBeInTheDocument();
  });

  it("prefers nested projection payload over stale top-level projection aliases", async () => {
    primeApi(
      jobPayload({
        hero_summary: {
          summary: "Stale top-level hero summary",
          facts: [{ label: "Current step", value: "Stale step" }],
        },
        next_action: {
          title: "Stale next action",
          body: "This should not win.",
        },
        job_actions: {
          can_open_diagnostics: false,
        },
        projection: {
          hero_summary: {
            headline: "Projection-backed hero summary",
            facts: [{ label: "Current step", value: "Projection step" }],
          },
          next_action: {
            title: "Projection next action",
            body: "Projection-backed next action body.",
          },
          job_actions: {
            can_open_diagnostics: true,
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByText("Projection-backed hero summary")).toBeInTheDocument();
    expect(screen.queryByText("Stale top-level hero summary")).not.toBeInTheDocument();
    expect(screen.getByText("Projection step")).toBeInTheDocument();
    expect(screen.getByText("Projection next action")).toBeInTheDocument();
    expect(screen.getByText("Projection-backed next action body.")).toBeInTheDocument();
    expect(screen.queryByTestId("job-advanced-diagnostics-trigger")).not.toBeInTheDocument();
  });

  it("keeps the cancel action available while an approved job is queued for resume", async () => {
    primeApi(
      jobPayload({
        status: "resume_pending",
        current_step_id: "approval_resume",
        lifecycle: {
          status: {
            code: "resume_pending",
            label: "Resume queued",
            terminal: false,
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByTestId("job-cancel-button")).toHaveTextContent("Cancel job");
  });

  it("prefers nested projection section data over stale top-level aliases when sections are absent", async () => {
    primeApi(
      jobPayload({
        execution_steps: [
          {
            id: "stale-top-level-step",
            title: "Stale Top Level Step",
            status: "completed",
            status_label: "Completed",
            summary: "This should not win.",
          },
        ],
        review_workspace: {
          kind: "document_review",
          title: "Stale top-level workspace",
          intro: "This should not win.",
        },
        previous_decisions: [
          {
            id: "stale-decision",
            title: "Stale decision",
            status: "Recorded",
            summary: "This should not win.",
          },
        ],
        diagnostics: {
          tabs: {
            decisions: {
              count: 1,
              rows: [
                {
                  id: "stale-decision",
                  title: "Stale decision",
                  status: "Recorded",
                  summary: "This should not win.",
                },
              ],
            },
          },
        },
        sections: undefined,
        projection: {
          execution_steps: [
            {
              id: "projection-step-1",
              title: "Projection Step",
              status: "completed",
              status_label: "Completed",
              summary: "Step sourced from nested projection.",
            },
          ],
          selected_step: {
            id: "projection-step-1",
            title: "Projection Step",
            status: "completed",
            status_label: "Completed",
            summary: "Step sourced from nested projection.",
          },
          review_workspace: {
            kind: "document_review",
            approval_id: "gate-projection",
            thread_id: "thread-ready",
            title: "Projection review workspace",
            intro: "Review workspace sourced from nested projection.",
          },
          previous_decisions: [
            {
              id: "projection-decision",
              title: "Projection decision",
              status: "Recorded",
              summary: "Decision sourced from nested projection.",
            },
          ],
          cost_breakdown: {
            rows: [
              {
                agent_label: "Planner",
                model: "gpt-5",
                tokens: 200,
                calls: 1,
                cost_usd: 0.12,
                cost_display: "$0.1200",
              },
            ],
            total_cost_usd: 0.12,
            total_cost_display: "$0.12",
          },
          diagnostics: {
            tabs: {
              decisions: {
                count: 1,
                rows: [
                  {
                    id: "projection-decision",
                    title: "Projection decision",
                    status: "Recorded",
                    summary: "Decision sourced from nested projection.",
                  },
                ],
              },
            },
          },
        },
      }),
    );

    renderPage();

    expect(await screen.findByRole("button", { name: /Projection Step/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Stale Top Level Step/i })).not.toBeInTheDocument();
    expect(screen.getByText("Projection review workspace")).toBeInTheDocument();
  });

  it("loads earlier execution steps without replacing the current page", async () => {
    primeApi(
      jobPayload({
        execution_steps: [],
        sections: {
          execution_steps: [],
        },
      }),
    );
    mockedFetchJobExecutionStepsPage
      .mockResolvedValueOnce({
        job_id: "42",
        steps: [
          {
            id: "recent-step",
            title: "Recent batch",
            status: "completed",
            timestamp: "2026-04-25T10:02:00Z",
          },
        ],
        page: { count: 1, has_more: true, next_cursor: "50:message-50" },
      } as never)
      .mockResolvedValueOnce({
        job_id: "42",
        steps: [
          {
            id: "older-step",
            title: "Earlier batch",
            status: "completed",
            timestamp: "2026-04-25T10:01:00Z",
          },
        ],
        page: { count: 1, has_more: false, next_cursor: "" },
      } as never);

    renderPage();

    expect(await screen.findByRole("button", { name: /Recent batch/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Load earlier steps/i }));

    expect(await screen.findByRole("button", { name: /Earlier batch/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Recent batch/i })).toBeInTheDocument();
    expect(mockedFetchJobExecutionStepsPage).toHaveBeenCalledWith("42", "50:message-50");
  });
});

describe("compact declared workflow result", () => {
  it.each([false, true])("shows the persisted strict result in embedded Test Run=%s", async (embedded) => {
    primeApi({
      ...jobPayload({ status: "completed", execution_steps: [] }),
      workflow_run: null,
      workflow_result: { outputs: { repository: "Hello-World", accepted: false, count: 0, optional: null }, artifacts: {} },
    });
    if (embedded) renderEmbeddedPage("42"); else renderPage();
    const panel = await screen.findByTestId("job-result-panel");
    expect(within(panel).getByText("Hello-World")).toBeInTheDocument();
    expect(within(panel).getByText("No")).toBeInTheDocument();
    expect(within(panel).getByText("0")).toBeInTheDocument();
    expect(within(panel).getByText("—")).toBeInTheDocument();
  });
});


describe("Approval Review public context parity", () => {
  const archivedDefinition = {
    job_id: "42", available: true, snapshot_id: "review-snapshot-3", workflow_id: "approval-workflow", revision: 3,
    digest: "archived-review-digest", schema_version: 1,
    steps: [
      { step_id: "review", step_kind: "approval", display_name: "Review", override: { approval: { message: "Approve this synthetic audit item?" } } },
      { step_id: "accepted", step_kind: "stop_success", display_name: "Archived approved destination" },
      { step_id: "declined", step_kind: "stop_fail", display_name: "Archived rejected destination" },
    ], groups: [], for_each: [],
    execution_graph: { schema_version: 1, entry: { kind: "step", id: "review" }, edges: [
      { source: { kind: "step", id: "review" }, outcome: { name: "approved" }, target: { kind: "step", id: "accepted" }, rework: false },
      { source: { kind: "step", id: "review" }, outcome: { name: "rejected" }, target: { kind: "step", id: "declined" }, rework: false },
    ] },
  };
  function primeReview(mode: string, content: unknown, definition: Record<string, unknown> = archivedDefinition) {
    const gate = {
      approval_id: "review-gate-42", job_id: "42", thread_id: "review-thread-42", gate_kind: "approval", stage: "Review",
      step_id: "review", step_execution_id: "review-execution-42", message: "Approve this synthetic audit item?", content,
      allowed_actions: ["approve", "reject"],
    };
    primeApi(jobPayload({
      status: "awaiting_approval", selected_workflow: "approval-workflow", thread_id: "review-thread-42", current_step_id: "review",
      approval_gate: gate,
      review_workspace: mode === "canonical" ? {
        kind: "document_review", title: "Review", stage_label: "Review", intro: gate.message,
        approval_id: gate.approval_id, thread_id: gate.thread_id, content, content_is_present: true,
        comment_required_on_reject: true, read_only: false,
      } : undefined,
    }));
    mockedFetchJobApproval.mockResolvedValue({ job_id: "42", approval_id: gate.approval_id, payload: gate } as never);
    mockedFetchJobDefinition.mockResolvedValue(definition as never);
    renderPage();
  }

  it.each(["raw", "canonical"])("shows every domain field and this run's destinations in the %s Review consumer", async (mode) => {
    primeReview(mode, JSON.stringify({ sku: "AUDIT-1", amount: 42, arbitrary_business_code: "PUBLIC-7", zero: 0, enabled: false,
      nested: { public_values: ["retained", 0, false, null], credential: "[REDACTED]" } }));
    const body = await screen.findByTestId("job-review-workspace-body");
    await waitFor(() => expect(body).toHaveTextContent("AUDIT-1"));
    for (const summary of within(body).queryAllByText(/./, { selector: "summary" })) fireEvent.click(summary);
    expect(body).toHaveTextContent("42");
    expect(body).toHaveTextContent("PUBLIC-7");
    expect(body).toHaveTextContent("0");
    expect(body).toHaveTextContent("false");
    expect(body).toHaveTextContent("null");
    expect(body).toHaveTextContent("[REDACTED]");
    expect(body).not.toHaveTextContent("_flow_steward_projection");
    expect(within(body).getByText("Approve this synthetic audit item?")).toBeInTheDocument();
    expect(await within(body).findByText("Archived approved destination")).toBeVisible();
    expect(within(body).getByText("Archived rejected destination")).toBeVisible();
    expect(within(body).getByText("Approved")).toBeInTheDocument();
    expect(within(body).getByText("Rejected")).toBeInTheDocument();
    expect(mockedFetchJobDefinition).toHaveBeenCalledWith("42");
    expect(within(body).queryByRole("textbox", { name: /destination/i })).not.toBeInTheDocument();
    expect(submitApprovalDecision).not.toHaveBeenCalled();
  });

  it.each(["raw", "canonical"])("keeps a public JSON array with arbitrary nested values in the %s Review consumer", async (mode) => {
    primeReview(mode, JSON.stringify(["arbitrary-owner-value", 0, false, null, { custom_domain_key: ["deep-value"] }]));
    const body = await screen.findByTestId("job-review-workspace-body");
    await waitFor(() => expect(body).toHaveTextContent("arbitrary-owner-value"));
    for (const summary of within(body).queryAllByText(/./, { selector: "summary" })) fireEvent.click(summary);
    expect(body).toHaveTextContent("custom_domain_key");
    expect(body).toHaveTextContent("deep-value");
    expect(body).toHaveTextContent("false");
    expect(body).toHaveTextContent("null");
  });

  it.each([
    { reason: "foreign job", patch: { job_id: "another-run" } },
    { reason: "foreign workflow", patch: { workflow_id: "another-workflow" } },
    { reason: "missing archive", patch: { available: false } },
    { reason: "unsupported archive", patch: { schema_version: 99 } },
  ])("does not invent Review destinations from a $reason", async ({ patch }) => {
    primeReview("canonical", "Owner review document", { ...archivedDefinition, ...patch });
    const body = await screen.findByTestId("job-review-workspace-body");
    await waitFor(() => expect(mockedFetchJobDefinition).toHaveBeenCalledWith("42"));
    expect(await within(body).findAllByText("Not recorded")).toHaveLength(2);
    expect(within(body).queryByText("Archived approved destination")).not.toBeInTheDocument();
    expect(within(body).queryByText("Archived rejected destination")).not.toBeInTheDocument();
    expect(within(body).getByText("Owner review document")).toBeVisible();
  });

  it("does not choose an arbitrary target when the pinned outcome has two destinations", async () => {
    primeReview("canonical", "Owner review document", { ...archivedDefinition, execution_graph: { ...archivedDefinition.execution_graph,
      edges: [...archivedDefinition.execution_graph.edges, { source: { kind: "step", id: "review" }, outcome: { name: "approved" }, target: { kind: "step", id: "declined" }, rework: false }],
    } });
    const body = await screen.findByTestId("job-review-workspace-body");
    expect(await within(body).findByText("Not recorded")).toBeVisible();
    expect(within(body).queryByText("Archived approved destination")).not.toBeInTheDocument();
    expect(within(body).getByText("Archived rejected destination")).toBeVisible();
  });

  it("keeps required rejection comments and the immutable gate identity beside structured Review context", async () => {
    primeReview("raw", JSON.stringify({ owner_field: "Review value" }));
    const body = await screen.findByTestId("job-review-workspace-body");
    await waitFor(() => expect(body).toHaveTextContent("Review value"));
    fireEvent.click(within(body).getByTestId("approval-reject-button"));
    expect(await within(body).findByRole("alert")).toHaveTextContent(/comment/i);
    expect(submitApprovalDecision).not.toHaveBeenCalled();
    fireEvent.change(within(body).getByTestId("approval-comment-input"), { target: { value: "Please check this item" } });
    fireEvent.click(within(body).getByTestId("approval-reject-button"));
    await waitFor(() => expect(submitApprovalDecision).toHaveBeenCalledExactlyOnceWith("review-thread-42", {
      approval_id: "review-gate-42", approved: false, comment: "Please check this item",
    }));
  });
});


describe("canonical workflow-level result artifacts", () => {
  it.each([false, true])("preserves validated artifact metadata with artifact-only=%s without step attribution or invented links", async (artifactOnly) => {
    primeApi({ ...jobPayload({ status: "completed", execution_steps: [] }), workflow_run: null,
      workflow_result: {
        outputs: artifactOnly ? {} : { repository: "Hello-World", accepted: false, count: 0, optional: null },
        artifacts: { report: { artifact_id: "artifact-1", filename: "canonical-report.pdf", content_type: "application/pdf", size_bytes: 42,
          sha256: "canonical-sha256", retention_class: "workflow-output" } },
      },
    });
    renderPage();
    const panel = await screen.findByTestId("job-result-panel");
    const artifacts = within(panel).getByRole("region", { name: "Final artifacts" });
    expect(within(artifacts).getByText("Report")).toBeVisible();
    for (const expected of ["artifact_id: artifact-1", "filename: canonical-report.pdf", "content_type: application/pdf", "size_bytes: 42", "sha256: canonical-sha256", "retention_class: workflow-output"]) {
      expect(within(artifacts).getAllByRole("listitem").some((item) => item.textContent === expected)).toBe(true);
    }
    expect(within(artifacts).queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByTestId("job-step-inspector-drawer")).not.toBeInTheDocument();
    if (artifactOnly) expect(within(panel).queryByText("Workflow outputs")).not.toBeInTheDocument();
    else {
      expect(within(panel).getByText("Workflow outputs")).toBeVisible();
      expect(within(panel).getByText("Hello-World")).toBeVisible();
      expect(within(panel).getByText("No")).toBeVisible();
      expect(within(panel).getByText("0")).toBeVisible();
      expect(within(panel).getByText("—")).toBeVisible();
    }
  });
});

describe("designed Stop Fail overview presentation", () => {
  const configuredReason = "Orders over 1000 need manual review";
  const genericFailureMessage = "Workflow execution did not complete successfully";

  function primePagedRunError(runError: Record<string, unknown>) {
    mockedFetchJobExecutionStepsPage.mockResolvedValue({
      job_id: "42",
      steps: [],
      page: { has_more: false, next_cursor: "" },
      run_error: runError,
    } as never);
  }

  it("lets semantic workflow_stopped show the configured reason over a generic public failure", async () => {
    primeApi(jobPayload({
      status: "failed",
      finished_at: "2026-04-25T10:05:00Z",
      lifecycle: { status: { code: "failed", label: "Failed", terminal: true } },
      execution_steps: [],
      failure_summary: {
        present: true,
        kind: "workflow_stopped",
        title: "Stopped by the workflow",
        summary: `Stopped by the workflow: ${configuredReason}`,
        detail: configuredReason,
      },
    }));
    primePagedRunError({ code: "job_failed", message: genericFailureMessage, step_id: "stop" });
    renderPage();

    const overview = await screen.findByTestId("job-run-overview");
    expect(overview).toHaveTextContent("StatusFailed");
    const alert = await within(overview).findByTestId("job-step-failure");
    expect(within(alert).getByText("Stopped by the workflow")).toBeInTheDocument();
    expect(within(alert).getByText(configuredReason)).toBeInTheDocument();
    expect(within(alert).queryByText(genericFailureMessage)).not.toBeInTheDocument();
    expect(within(alert).queryByText("job_failed")).not.toBeInTheDocument();
    expect(within(alert).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("recognizes authoritative workflow_stop_fail public metadata without a semantic summary", async () => {
    primeApi(jobPayload({
      status: "failed",
      finished_at: "2026-04-25T10:05:00Z",
      execution_steps: [],
    }));
    primePagedRunError({
      code: "workflow_stop_fail",
      message: configuredReason,
      step_id: "stop",
      step_execution_id: "stop-1",
    });
    renderPage();

    const alert = await screen.findByTestId("job-step-failure");
    expect(within(alert).getByText("Stopped")).toBeInTheDocument();
    expect(within(alert).getByText(configuredReason)).toBeInTheDocument();
    expect(within(alert).queryByText("Step “Stop” failed")).not.toBeInTheDocument();
  });

  it("uses authoritative detail Stop metadata when the paged error is generic", async () => {
    primeApi(jobPayload({
      status: "failed",
      finished_at: "2026-04-25T10:05:00Z",
      execution_steps: [],
      workflow_run: {
        error: {
          code: "workflow_stop_fail",
          message: configuredReason,
          step_id: "stop",
          step_execution_id: "stop-1",
        },
      },
    }));
    primePagedRunError({ code: "job_failed", message: genericFailureMessage, step_id: "stop" });
    renderPage();

    const overview = await screen.findByTestId("job-run-overview");
    expect(overview).toHaveTextContent("StatusFailed");
    const alert = await within(overview).findByTestId("job-step-failure");
    expect(within(alert).getByText("Stopped")).toBeInTheDocument();
    expect(within(alert).getByText(configuredReason)).toBeInTheDocument();
    expect(within(alert).queryByText(genericFailureMessage)).not.toBeInTheDocument();
    expect(within(alert).queryByText("job_failed")).not.toBeInTheDocument();
    expect(within(alert).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps ordinary runtime errors in the danger treatment", async () => {
    primeApi(jobPayload({
      status: "failed",
      finished_at: "2026-04-25T10:05:00Z",
      execution_steps: [
        { id: "query", step_id: "query", title: "Query saved stock levels", status: "failed" },
      ],
    }));
    primePagedRunError({
      code: "projection_unavailable",
      message: "Saved query projection is unavailable for new runs",
      step_id: "query",
    });
    renderPage();

    const alert = await screen.findByTestId("job-step-failure");
    expect(within(alert).getByText("Step “Query saved stock levels” failed")).toBeInTheDocument();
    expect(within(alert).getByText("projection_unavailable")).toBeInTheDocument();
    expect(within(alert).getByText("Saved query projection is unavailable for new runs")).toBeInTheDocument();
    expect(within(alert).getByRole("alert")).toBeInTheDocument();
  });
});
