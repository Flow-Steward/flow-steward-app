import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import {
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowContract,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowExportPolicy,
  mockedFetchWorkflowValidation,
  mockedFindWorkflowLinkedProjectId,
  mockedTestRunWorkflow,
  mockedTestRunWorkflowStep,
  renderPage,
} from "./index.test-helpers";
import { firstStringFromUnknownValue } from "@/lib/utils/unknown-values";
import { fetchWorkflowStepContractPreview } from "@/pages/workflows/shared/api";

function expectTestRunEffectWarning() {
  const warnings = screen.getAllByRole("note", { name: "Test Run effects" });
  for (const warning of warnings) {
    expect(warning).toHaveTextContent(/not an external-effect sandbox/i);
    expect(warning).toHaveTextContent(/HTTP steps.*real requests.*writes/i);
    expect(warning).toHaveTextContent(/model calls.*provider costs/i);
    expect(warning).toHaveTextContent(/extension effects.*host.*suppression/i);
    expect(warning).toHaveTextContent(/review.*steps.*target URLs.*credentials.*inputs.*before running/i);
    expect(warning).not.toHaveTextContent(/other external changes are suppressed/i);
  }
}

describe("WorkflowsDetailPage test runs and header", () => {
  it("keeps the active project binding when cloning from the builder", async () => {
    vi.stubGlobal("prompt", vi.fn(() => "weekly_report_copy"));

    renderPage({
      embedded: false,
      surface: "detail",
      initialEntry:
        "/registry/workflows/weekly_report?project_id=project_alpha",
    });

    fireEvent.click(
      await screen.findByRole("button", { name: "Workflow actions" }),
    );
    const menu = await screen.findByRole("menu", { name: "Workflow actions" });
    fireEvent.click(
      within(menu).getByRole("menuitem", { name: /clone workflow/i }),
    );

    await waitFor(() => {
      const cloneCall = fetchSpy.mock.calls.find((call) =>
        firstStringFromUnknownValue([call[0], ""]).includes(
          "/api/workflows/weekly_report/clone",
        ),
      );
      expect(cloneCall).toBeDefined();
      expect(
        JSON.parse(firstStringFromUnknownValue([cloneCall?.[1]?.body, "{}"])),
      ).toEqual({
        project_id: "project_alpha",
        workflow_id: "weekly_report_copy",
      });
    });
    await waitFor(() => {
      expect(screen.getByRole("link", { name: /open builder/i })).toHaveAttribute(
        "href",
        "/registry/workflows/weekly_report_copy/builder?project_id=project_alpha",
      );
    });
  });

  it("shows the exact failed step test run error outside the successful result panel", async () => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValueOnce("project_alpha");
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
          id: "11111111-1111-1111-1111-111111111099",
          step_id: "draft_email",
          display_name: "Draft email",
          step_kind: "connector",
          override: {},
        }], groups: [],
    } as never);
    mockedTestRunWorkflowStep.mockResolvedValueOnce({
      status: "failed",
      final_status: "failed",
      error: "Acme Workspace Developer Preview enrollment is required.",
      final_report: "The connector invocation failed.",
      workflow_id: "weekly_report",
      step_id: "draft_email",
      step_kind: "connector",
      inputs: {},
      result: {},
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Draft email/i }));
    fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
    // A tab change is guarded (unsaved automation asks first), so the tab opens a tick later.
    await screen.findAllByRole("note", { name: "Test Run effects" });
    expectTestRunEffectWarning();
    fireEvent.change(await screen.findByLabelText("Step Test Run Summary"), {
      target: { value: "Create a draft email." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run step test run" }));

    expect(
      await screen.findByText(
        "Acme Workspace Developer Preview enrollment is required.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("The connector invocation failed.")).not.toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Step test run result" }),
    ).not.toHaveTextContent("Acme Workspace Developer Preview enrollment is required.");
  });

  it("names the project of a step Test Run instead of listing its id among the inputs", async () => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValueOnce("project_alpha");
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
          id: "11111111-1111-1111-1111-111111111095",
          step_id: "compose_post",
          display_name: "Compose post",
          step_kind: "connector",
          override: {},
        }], groups: [],
    } as never);
    mockedTestRunWorkflowStep.mockResolvedValueOnce({
      status: "completed",
      final_status: "completed",
      workflow_id: "weekly_report",
      step_id: "compose_post",
      step_kind: "connector",
      project_name: "Template check",
      inputs: { project_id: "54ee3a1b-9fcf-4daa-8039-e5f8c96e3184", header: "Spring" },
      result: {},
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Compose post/i }));
    fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
    fireEvent.change(await screen.findByLabelText("Step Test Run Summary"), {
      target: { value: "Compose the post." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run step test run" }));

    expect(await screen.findByText("Template check")).toBeInTheDocument();
    expect(screen.getByText("Spring")).toBeInTheDocument();
    expect(screen.queryByText("54ee3a1b-9fcf-4daa-8039-e5f8c96e3184")).not.toBeInTheDocument();
  });

  it("does not show one step's Test Run result under another step", async () => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValueOnce("project_alpha");
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
            id: "11111111-1111-1111-1111-111111111097",
            step_id: "first_step",
            display_name: "First step",
            step_kind: "connector",
            override: {},
          },
          {
            id: "11111111-1111-1111-1111-111111111096",
            step_id: "second_step",
            display_name: "Second step",
            step_kind: "connector",
            override: {},
          }], groups: [],
    } as never);
    mockedTestRunWorkflowStep.mockResolvedValueOnce({
      status: "failed",
      final_status: "failed",
      error: "the repeated value would be too large",
      workflow_id: "weekly_report",
      step_id: "first_step",
      step_kind: "connector",
      inputs: {},
      result: {},
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /First step/i }));
    fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
    fireEvent.change(await screen.findByLabelText("Step Test Run Summary"), {
      target: { value: "Try the first step." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run step test run" }));
    expect(await screen.findByText("the repeated value would be too large")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Second step/i }));
    fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
    await screen.findByLabelText("Step Test Run Summary");
    expect(screen.queryByText("the repeated value would be too large")).not.toBeInTheDocument();
    expect(screen.queryByText(/Step: first_step/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /First step/i }));
    fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
    expect(await screen.findByText("the repeated value would be too large")).toBeInTheDocument();
  });

  it("labels an optional step input as optional and states a failure once", async () => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValueOnce("project_alpha");
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [{
          id: "11111111-1111-1111-1111-111111111098",
          step_id: "compose_post",
          display_name: "Compose post",
          step_kind: "connector",
          override: {},
        }], groups: [],
    } as never);
    const preview = vi.mocked(fetchWorkflowStepContractPreview);
    const defaultPreview = preview.getMockImplementation();
    onTestFinished(() => {
      if (defaultPreview) preview.mockImplementation(defaultPreview);
    });
    preview.mockResolvedValue({
      locked_required: [],
      unresolved_required: [
        { name: "header", required: true, resolved: false, type: "text", source: "state.outputs.header" },
      ],
      optional: [
        { name: "footer", required: false, resolved: false, type: "text", source: "state.outputs.footer" },
      ],
      custom: [],
      outputs: [],
    } as never);
    const failure = "Template failed: Input 'items': expected array, got object";
    mockedTestRunWorkflowStep.mockResolvedValueOnce({
      status: "failed",
      final_status: "failed",
      error: failure,
      summary: failure,
      workflow_id: "weekly_report",
      step_id: "compose_post",
      step_kind: "connector",
      inputs: {},
      result: {},
    });

    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Compose post/i }));
    fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));

    expect(await screen.findByText(/Optional · text/)).toBeInTheDocument();
    expect(screen.getByText(/Required by step · text/)).toBeInTheDocument();

    fireEvent.change(await screen.findByLabelText("Step Test Run Summary"), {
      target: { value: "Compose the post." },
    });
    fireEvent.change(screen.getByLabelText(/^header/), { target: { value: "Spring" } });
    fireEvent.click(screen.getByRole("button", { name: "Run step test run" }));

    expect(await screen.findByText(failure)).toBeInTheDocument();
    expect(screen.getAllByText(failure)).toHaveLength(1);
  });

  it("blocks workflow Test Run while a required contract input is blank", async () => {
    mockedFetchWorkflowContract.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      interface: {
        enabled: true,
        expected_inputs: [
          { name: "project_name", type: "string", required: true },
        ],
      },
      definition_issues: [],
    } as never);

    renderPage({
      initialEntry:
        "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha",
      surface: "builder",
    });

    expect(await screen.findByText(/project_name/i)).toBeInTheDocument();
    expectTestRunEffectWarning();
    expect(
      screen.getByRole("button", { name: "Run workflow test run" }),
    ).toBeDisabled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  });

it("shows workflow test run outputs artifacts and per-step payload details", async () => {
    const jobId = "job-workflow-result-123";
    const contentBrief = { subject: "Launch email ready", preview_text: "Buyers preview ready", body: "Generated email body", audience: "buyers", cta: "Open dashboard" };
    const workflowResult = {
      outputs: { channel_summary: "email branch ready for launch", content_brief: contentBrief },
      artifacts: { brief_pdf: { artifact_id: "art_123", filename: "brief.pdf", content_type: "application/pdf", size_bytes: 42, retention_class: "workflow-output" } },
    };
    const job = { id: jobId, project_id: "project_alpha", selected_workflow: "weekly_report", task_source: "workflow_test_run", status: "completed" };
    const steps = [
      { step_id: "compose_email_connector", step_execution_id: "compose-pass", title: "Compose Email", status: "completed", occurrence: 1,
        inputs: { channel: "email", audience: "buyers", request: "Draft launch email for buyers" },
        outputs: { result: "Generated email body", content_brief: contentBrief }, message: "Created email content." },
      { step_id: "set_channel_summary_email", step_execution_id: "summary-pass", title: "Set channel summary", status: "completed", occurrence: 1,
        outputs: { channel_summary: "email branch ready for launch" }, message: "set_variable wrote 2 top-level key(s)" },
      { step_id: "end_stop_success", step_execution_id: "end-pass", title: "End workflow", status: "completed", occurrence: 1,
        message: "stop_success terminated workflow execution" },
    ];
    mockedTestRunWorkflow.mockResolvedValueOnce({ workflow_id: "weekly_report", project_id: "project_alpha", job_id: jobId,
      status: "completed", final_status: "success", needs_user_approval: false });
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    const fallback = fetchSpy.getMockImplementation()!;
    fetchSpy.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
      if (url.pathname === "/api/v2/jobs" && (!init?.method || init.method === "GET")) {
        const scoped = url.searchParams.get("workflow_id") === "weekly_report" && url.searchParams.get("project_id") === "project_alpha"
          && url.searchParams.get("task_source") === "workflow_test_run" && url.searchParams.get("page") === "1" && url.searchParams.get("per_page") === "8";
        return scoped ? json({ ok: true, kind: "list", data: { jobs: [job], page: 1, per_page: 8, total: 1, total_pages: 1 }, error: null }) : json({ error: "Unexpected history scope" }, 422);
      }
      if (url.pathname === `/api/v2/jobs/${jobId}` && (!init?.method || init.method === "GET")) return json({ ok: true, kind: "detail",
        data: { ...job, job, execution_steps: [], workflow_run: null, workflow_result: workflowResult,
          outputs: { legacy_output: "ignored because workflow_outputs wins" }, artifacts: { legacy_artifact: { artifact_id: "art_legacy" } } }, error: null });
      if (url.pathname === `/api/jobs/${jobId}/definition`) return json({ job_id: jobId, available: false, workflow_id: "weekly_report" });
      if (url.pathname === `/api/v2/jobs/${jobId}/execution-steps`) return json({ job_id: jobId, run_terminal: true, child_invocations: [], steps,
        page: { count: steps.length, has_more: false, next_cursor: "" } });
      if (url.pathname === `/api/jobs/${jobId}/cost-breakdown`) return json({ breakdown: [], total_cost_usd: 0 });
      if (url.pathname === `/api/jobs/${jobId}/approval`) return json({ job_id: jobId });
      if (url.pathname === `/api/jobs/${jobId}/approvals`) return json({ job_id: jobId, approvals: [] });
      if (url.pathname === `/api/jobs/${jobId}/decision-overrides`) return json({ job_id: jobId, overrides: [] });
      return fallback(input, init);
    });
    const originalRect = HTMLElement.prototype.getBoundingClientRect;
    const measure = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (!this.matches("dialog.workflow-builder-drawer")) return originalRect.call(this);
      const width = Number.parseFloat(this.style.width);
      return new DOMRect(window.innerWidth - width, 64, width, window.innerHeight - 64);
    });
    let view: ReturnType<typeof renderPage> | undefined;
    try {
      view = renderPage({ initialEntry: "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha", surface: "builder" });
      const summary = await screen.findByLabelText("Test Run Summary");
      fireEvent.change(summary, { target: { value: "Run the workflow and return generated content." } });
      fireEvent.click(screen.getByRole("button", { name: "Run workflow test run" }));
      await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledWith(expect.objectContaining({
        projectId: "project_alpha", taskSummary: "Run the workflow and return generated content.", workflowId: "weekly_report",
      })));
      const results = await screen.findByTestId("workflow-test-run-results");
      expect(results).toHaveAttribute("data-job-id", jobId);
      expect(within(results).getByRole("link", { name: "Open full Job Details" })).toHaveAttribute("href", `/jobs/${jobId}`);
      // What the run published: its workflow result, never the legacy top-level copies.
      const finalResult = await within(results).findByTestId("workflow-test-run-outputs");
      expect(within(finalResult).getByText("channel_summary")).toBeVisible();
      expect(within(finalResult).getByText("\"email branch ready for launch\"")).toBeVisible();
      expect(within(finalResult).getByText("content_brief")).toBeVisible();
      expect(finalResult).toHaveTextContent("\"subject\": \"Launch email ready\"");
      expect(within(finalResult).getByText("brief_pdf")).toBeVisible();
      expect(finalResult).toHaveTextContent("\"filename\": \"brief.pdf\"");
      expect(finalResult).toHaveTextContent("art_123");
      expect(within(finalResult).queryByRole("link")).not.toBeInTheDocument();
      expect(results).not.toHaveTextContent(/legacy_output|legacy_artifact|art_legacy|ignored because workflow_outputs wins/);
      // One row per pass, each opening in place to what it read and wrote.
      const passes = within(results).getAllByTestId("workflow-run-pass");
      expect(passes.map((pass) => within(pass).getAllByRole("button")[0].textContent)).toEqual([
        expect.stringContaining("Compose Email"), expect.stringContaining("Set channel summary"), expect.stringContaining("End workflow")]);
      fireEvent.click(within(passes[0]).getByRole("button", { name: /Compose Email/ }));
      expect(within(passes[0]).getByText("request")).toBeVisible();
      expect(within(passes[0]).getByText("\"Draft launch email for buyers\"")).toBeVisible();
      expect(within(passes[0]).getByText("result")).toBeVisible();
      expect(within(passes[0]).getByText("\"Generated email body\"")).toBeVisible();
      expect(passes[0]).toHaveTextContent("\"subject\": \"Launch email ready\"");
      fireEvent.click(within(passes[1]).getByRole("button", { name: /Set channel summary/ }));
      expect(within(passes[1]).getByText("\"email branch ready for launch\"")).toBeVisible();
      expect(fetchSpy.mock.calls.filter(([, init]) => ["POST", "PATCH", "PUT", "DELETE"].includes(String(init?.method || "GET").toUpperCase()))).toEqual([]);
    } finally { view?.unmount(); measure.mockRestore(); }
  });

  it("opens the exact job returned by the workflow test run inside the builder", async () => {
    const job = { id: "job-exact-test-run-123", project_id: "project_alpha", selected_workflow: "weekly_report", task_source: "workflow_test_run", status: "completed" };
    const fallback = fetchSpy.getMockImplementation()!;
    fetchSpy.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
      if (url.pathname === "/api/v2/jobs" && (!init?.method || init.method === "GET")) {
        const scoped = url.searchParams.get("workflow_id") === "weekly_report" && url.searchParams.get("project_id") === "project_alpha"
          && url.searchParams.get("task_source") === "workflow_test_run" && url.searchParams.get("page") === "1" && url.searchParams.get("per_page") === "8";
        return scoped ? json({ ok: true, kind: "list", data: { jobs: [job], page: 1, per_page: 8, total: 1, total_pages: 1 }, error: null }) : json({ error: "Unexpected history scope" }, 422);
      }
      if (url.pathname === `/api/v2/jobs/${job.id}` && (!init?.method || init.method === "GET")) return json({ ok: true, kind: "detail", data: { ...job, job, execution_steps: [] }, error: null });
      if (url.pathname === `/api/jobs/${job.id}/definition`) return json({ job_id: job.id, available: false, workflow_id: "weekly_report" });
      if (url.pathname === `/api/v2/jobs/${job.id}/execution-steps`) return json({ job_id: job.id, run_terminal: true, child_invocations: [], steps: [], page: { count: 0, has_more: false, next_cursor: "" } });
      return fallback(input, init);
    });
    mockedTestRunWorkflow.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      project_id: "project_alpha",
      job_id: "job-exact-test-run-123",
      status: "completed",
      final_status: "success",
      final_report: "Completed successfully.",
      needs_user_approval: false,
      workflow_outputs: { outputs: {}, artifacts: {} },
      trace: [],
    });

    renderPage({
      initialEntry:
        "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha",
      surface: "builder",
    });

    const start = await screen.findByRole("button", { name: "Run workflow test run" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1));
    expect(mockedTestRunWorkflow).toHaveBeenCalledWith(expect.objectContaining({
      workflowId: "weekly_report", projectId: "project_alpha",
    }));

    expect(fetchSpy.mock.calls.some(([url]) => String(url).endsWith("/run"))).toBe(false);
    expect(
      await screen.findByRole("link", { name: "Open full Job Details" }),
    ).toHaveAttribute("href", "/jobs/job-exact-test-run-123");
    expect(
      screen.getByTestId("workflow-test-run-results"),
    ).toHaveAttribute("data-job-id", "job-exact-test-run-123");
  });

  it("offers an acknowledged ordinary workflow run alongside the default Test Run", async () => {
    renderPage({
      initialEntry: "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha",
      surface: "builder",
    });
    expect(await screen.findByRole("button", { name: "Run workflow test run" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run workflow" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: /create external changes and use live credentials/i })).not.toBeChecked();
  });

  it("launches an ordinary job through the real browser adapter without changing test history", async () => {
    mockedFetchWorkflowContract.mockResolvedValue({ workflow_id: "weekly_report", enabled: true,
      interface: { enabled: true, trigger_channels: ["ui"], input_mode: "fields", strict_inputs: true,
        expected_inputs: [{ name: "supplier", type: "string", required: true }], output_mode: "none" },
    } as never);
    const fallback = fetchSpy.getMockImplementation()!;
    fetchSpy.mockImplementation(async (input, init) => {
      if (String(input) === "/api/workflows/weekly_report/run") return new Response(JSON.stringify({ job_id: "ordinary-job-42" }), { headers: { "content-type": "application/json" } });
      return fallback(input, init);
    });
    renderPage({ initialEntry: "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha", surface: "builder" });
    fireEvent.change(await screen.findByLabelText(/supplier/), { target: { value: "acme" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /create external changes and use live credentials/i }));
    const start = screen.getByRole("button", { name: "Run workflow" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    expect(await screen.findByRole("link", { name: "Open workflow Job Details" })).toHaveAttribute("href", "/jobs/ordinary-job-42");
    const calls = fetchSpy.mock.calls.filter(([url]) => String(url).endsWith("/run"));
    expect(calls).toHaveLength(1);
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual(expect.objectContaining({ project_id: "project_alpha", expected_revision: 7, acknowledged: true, inputs: { supplier: "acme" }, files: [] }));
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
    expect(localStorage.getItem("fs.workflow-run-job.weekly_report")).toBeNull();
  });

  it("restores the exact builder job after a page reload", async () => {
    globalThis.localStorage.setItem(
      "fs.workflow-run-job.weekly_report",
      "job-restored-789",
    );

    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    const fallback = fetchSpy.getMockImplementation()!;
    const job = { id: "job-restored-789", project_id: "project_alpha", selected_workflow: "weekly_report", task_source: "workflow_test_run", status: "completed" };
    fetchSpy.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
      if (url.pathname === "/api/v2/jobs" && (!init?.method || init.method === "GET")) {
        const scoped = url.searchParams.get("workflow_id") === "weekly_report" && url.searchParams.get("project_id") === "project_alpha"
          && url.searchParams.get("task_source") === "workflow_test_run" && url.searchParams.get("page") === "1" && url.searchParams.get("per_page") === "8";
        return scoped ? json({ ok: true, kind: "list", data: { jobs: [job], page: 1, per_page: 8, total: 1, total_pages: 1 }, error: null }) : json({ error: "Unexpected history scope" }, 422);
      }
      if (url.pathname === `/api/v2/jobs/${job.id}` && (!init?.method || init.method === "GET")) return json({ ok: true, kind: "detail", data: { ...job, job, execution_steps: [] }, error: null });
      if (url.pathname === `/api/jobs/${job.id}/definition`) return json({ job_id: job.id, available: false, workflow_id: "weekly_report" });
      if (url.pathname === `/api/v2/jobs/${job.id}/execution-steps`) return json({ job_id: job.id, run_terminal: true, child_invocations: [], steps: [], page: { count: 0, has_more: false, next_cursor: "" } });
      return fallback(input, init);
    });

    renderPage({
      initialEntry:
        "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha",
      surface: "builder",
    });

    const form = await screen.findByRole("dialog", { name: /^Start a test run$/ });
    expect(within(form).queryByRole("combobox", { name: "Test Run history" })).not.toBeInTheDocument();
    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    // The latest finished run comes back in the run bar after the reload; Results open from there.
    const runBar = await screen.findByTestId("workflow-run-bar");
    await waitFor(() => expect(runBar).toHaveAttribute("data-job-id", "job-restored-789"));
    fireEvent.click(within(runBar).getByRole("button", { name: "Results" }));

    expect(
      await screen.findByRole("link", { name: "Open full Job Details" }),
    ).toHaveAttribute("href", "/jobs/job-restored-789");
    expect(screen.getByTestId("workflow-test-run-results")).toHaveAttribute(
      "data-job-id",
      "job-restored-789",
    );
  });

  it("allows workflow test run even when the workflow itself is disabled", async () => {
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: false,
      availability_status: "unavailable",
      availability_reason_code: "workflow_disabled",
      availability_reason_text: "Workflow is disabled.",
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowCanvasGraph.mockResolvedValueOnce({
      workflow: {
        id: "weekly_report",
        display_name: "Weekly Report",
        enabled: false,
        callable_by_parent: false,
      },
      availability_status: "unavailable",
      availability_reason_code: "workflow_disabled",
      availability_reason_text: "Workflow is disabled.",
      interface_contract: {
        trigger_channels: [],
        input_mode: "fields",
        expected_inputs: [],
        strict_inputs: true,
      },
      output_contract: {
        output_mode: "none",
        expected_outputs: [],
        returned_files: [],
      },
      groups: [],
      steps: [],
      edges: [],
      validation_issues: [],
      automation_rules: [],
    } as never);

    renderPage({
      initialEntry:
        "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha",
      surface: "builder",
    });

    expect(await screen.findByLabelText("Test Run Summary")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Run workflow test run" }),
    ).toBeEnabled();
    expect(screen.queryByText("Workflow is disabled.")).not.toBeInTheDocument();
  });

  it("continues approval-gated workflow test runs from the drawer", async () => {
    const jobId = "job-approval-test-run-123";
    const gate = { approval_id: "gate-test-run", job_id: jobId, thread_id: "thread_123", gate_kind: "approval",
      message: "Review the generated content before continuing.", content: "Review the generated content before continuing." };
    let approved = false;
    let pendingDetailReads = 0;
    let completedDetailReads = 0;
    const decisions: unknown[] = [];
    const fallback = fetchSpy.getMockImplementation()!;
    const job = () => ({ id: jobId, project_id: "project_alpha", selected_workflow: "weekly_report", task_source: "workflow_test_run",
      thread_id: "thread_123", status: approved ? "completed" : "awaiting_approval" });
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    mockedTestRunWorkflow.mockResolvedValueOnce({ workflow_id: "weekly_report", project_id: "project_alpha", job_id: jobId,
      status: "awaiting_approval", final_status: "awaiting_approval", needs_user_approval: true,
      approval_id: "gate-test-run", thread_id: "thread_123", execution_id: "exec_pending" } as never);
    fetchSpy.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
      if (url.pathname === "/api/approvals/thread_123" && init?.method === "POST") {
        decisions.push(JSON.parse(String(init.body)));
        approved = true;
        return json({ status: "in_progress" });
      }
      if (url.pathname === "/api/v2/jobs" && (!init?.method || init.method === "GET")) {
        const scoped = url.searchParams.get("workflow_id") === "weekly_report" && url.searchParams.get("project_id") === "project_alpha"
          && url.searchParams.get("task_source") === "workflow_test_run" && url.searchParams.get("page") === "1" && url.searchParams.get("per_page") === "8";
        return scoped ? json({ ok: true, kind: "list", data: { jobs: [], page: 1, per_page: 8, total: 0, total_pages: 0 }, error: null }) : json({ error: "Unexpected history scope" }, 422);
      }
      if (url.pathname === `/api/v2/jobs/${jobId}` && (!init?.method || init.method === "GET")) {
        if (approved) completedDetailReads += 1; else pendingDetailReads += 1;
        const ownedJob = job();
        return json({ ok: true, kind: "detail", data: { ...ownedJob, job: ownedJob, execution_steps: [],
          lifecycle: { status: { code: ownedJob.status, label: approved ? "Completed" : "Waiting for approval", terminal: approved },
            approval: approved ? { state: "none" } : { state: "awaiting_approval", gate } },
          workflow_result: approved ? { outputs: { content_brief: "Approved brief" }, artifacts: {} } : null }, error: null });
      }
      if (url.pathname === `/api/jobs/${jobId}/approval`) return approved
        ? json({ code: "approval_not_awaiting" }, 409) : json({ job_id: jobId, approval_id: gate.approval_id, payload: gate });
      if (url.pathname === `/api/jobs/${jobId}/approvals`) return json({ job_id: jobId, approvals: [] });
      if (url.pathname === `/api/jobs/${jobId}/definition`) return json({ job_id: jobId, available: false, workflow_id: "weekly_report" });
      if (url.pathname === `/api/v2/jobs/${jobId}/execution-steps`) return json({ job_id: jobId, run_terminal: approved, child_invocations: [], steps: [], page: { count: 0, has_more: false, next_cursor: "" } });
      if (url.pathname === `/api/jobs/${jobId}/cost-breakdown`) return json({ breakdown: [], total_cost_usd: 0 });
      if (url.pathname === `/api/jobs/${jobId}/decision-overrides`) return json({ job_id: jobId, overrides: [] });
      return fallback(input, init);
    });

    renderPage({ initialEntry: "/registry/workflows/weekly_report/builder?drawer=workflow-test-run&project_id=project_alpha", surface: "builder" });
    fireEvent.change(await screen.findByLabelText("Test Run Summary"), { target: { value: "Run the workflow and pause at approval." } });
    const start = screen.getByRole("button", { name: "Run workflow test run" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      workflowId: "weekly_report", projectId: "project_alpha", taskSummary: "Run the workflow and pause at approval.",
    })));
    const results = await screen.findByTestId("workflow-test-run-results");
    expect(results).toHaveAttribute("data-job-id", jobId);
    expect(within(results).getByRole("link", { name: "Open full Job Details" })).toHaveAttribute("href", `/jobs/${jobId}`);
    const workspace = await within(results).findByTestId("workflow-approval-decision");
    await waitFor(() => expect(workspace).toHaveTextContent("Review the generated content before continuing."));
    fireEvent.change(within(workspace).getByRole("textbox", { name: "Comment for rejection" }), { target: { value: "Looks good to continue." } });
    fireEvent.click(within(workspace).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(decisions).toEqual([{ approval_id: "gate-test-run", approved: true, comment: "Looks good to continue." }]));
    const decisionCalls = fetchSpy.mock.calls.filter(([input, init]) => new URL(String(input), "http://localhost").pathname.startsWith("/api/approvals/") && init?.method === "POST");
    expect(decisionCalls).toHaveLength(1);
    expect(new URL(String(decisionCalls[0][0]), "http://localhost").pathname).toBe("/api/approvals/thread_123");
    await waitFor(() => expect(completedDetailReads).toBeGreaterThan(0));
    expect(pendingDetailReads).toBeGreaterThan(0);
    await waitFor(() => expect(within(screen.getByTestId("workflow-test-run-results")).getByRole("heading", { name: /^Latest test run/ })).toHaveTextContent("Finished"));
    const finalResult = await within(screen.getByTestId("workflow-test-run-results")).findByTestId("workflow-test-run-outputs");
    expect(within(finalResult).getByText("content_brief")).toBeVisible();
    expect(within(finalResult).getByText("\"Approved brief\"")).toBeVisible();
    expect(screen.getByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", jobId);
    await waitFor(() => expect(screen.queryByTestId("workflow-approval-decision")).not.toBeInTheDocument());
  });

  it("renders the workflow detail header description from workflow metadata", async () => {
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValueOnce({
      nodes: [],
      edges: [],
    } as never);
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
      validation: {
        valid: true,
        errors: [],
        warnings: [],
      },
    } as never);

    renderPage({ embedded: false });

    expect(await screen.findByText("Weekly Report")).toBeInTheDocument();
    expect(
      screen.getByText("Workflow-level description", { selector: "p" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/React workflow detail consumes workflow metadata/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Save project bindings" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Detail" })).not.toBeInTheDocument();
    // Export is a secondary command, so it lives in the header's action menu
    // rather than as one more button in the row; the href is what matters here.
    fireEvent.click(await screen.findByRole("button", { name: "Workflow actions" }));
    expect(await screen.findByRole("menuitem", { name: "Export YAML" })).toHaveAttribute(
      "href",
      "/api/workflows/weekly_report/export.yaml",
    );
  });

  it("exposes the canvas builder from the workflow detail page", async () => {
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValueOnce({
      nodes: [],
      edges: [],
    } as never);
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
      validation: {
        valid: true,
        errors: [],
        warnings: [],
      },
    } as never);

    renderPage({ embedded: false });

    expect(await screen.findByText("Canvas builder beta")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open canvas builder" }),
    ).toHaveAttribute(
      "href",
      "/registry/workflows/weekly_report/builder",
    );
  });

  it("hides full export and clone for protected extension workflows and opens their canvas read-only", async () => {
    mockedFetchWorkflowExportPolicy.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      extension_owned: true,
      protected: true,
      full_export_allowed: false,
      reference_export_allowed: true,
      clone_allowed: false,
      structure_change_allowed: false,
      source_install_id: "inst-ext",
      source_release_id: "rel-1",
      source_extension_id: "acme.reports",
      source_extension_name: "Acme Reports",
      source_extension_version: "2.1.0",
      reason: "Workflow is provided by an extension and protected by default.",
    } as never);
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Extension-owned workflow",
      enabled: true,
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValueOnce({
      nodes: [],
      edges: [],
    } as never);
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
      validation: {
        valid: true,
        errors: [],
        warnings: [],
      },
    } as never);

    renderPage({ embedded: false });

    // The badge names the extension and links to it; the install id is not a name.
    expect(await screen.findByRole("link", { name: "Acme Reports 2.1.0" })).toHaveAttribute("href", "/extensions/acme.reports");
    expect(screen.queryByText(/inst-ext/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Workflow actions" }));
    await screen.findByRole("menu", { name: "Workflow actions" });
    // A protected extension workflow offers no export at all (not even a reference
    // bundle) and no clone; its canvas opens read-only (structure visible, hooks connectable).
    expect(screen.queryByText("Export YAML")).not.toBeInTheDocument();
    expect(screen.queryByText("Export reference")).not.toBeInTheDocument();
    expect(screen.queryByText("Clone Workflow")).not.toBeInTheDocument();
    expect(screen.queryByText("Delete Workflow")).not.toBeInTheDocument();
    expect(screen.queryByText("Open builder")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open canvas builder" })).toHaveAttribute(
      "href",
      "/registry/workflows/weekly_report/builder",
    );
    expect(screen.queryByText("Build with AI")).not.toBeInTheDocument();
  });

  it("opens a protected workflow builder URL on a read-only canvas", async () => {
    mockedFetchWorkflowExportPolicy.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      extension_owned: true,
      protected: true,
      full_export_allowed: false,
      reference_export_allowed: true,
      clone_allowed: false,
      structure_change_allowed: false,
      source_install_id: "inst-ext",
      reason: "Workflow is provided by an extension and protected by default.",
    } as never);
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      source_install_id: "inst-ext",
      portability: {
        protected: true,
        structure_change_allowed: false,
        reason: "Workflow is provided by an extension and protected by default.",
      },
      steps: [], groups: [],
    } as never);
    // A protected author graph is sent with its real structure (sensitive values redacted).
    mockedFetchWorkflowCanvasGraph.mockResolvedValueOnce({
      workflow: { id: "weekly_report", display_name: "Weekly Report", enabled: true, callable_by_parent: false },
      protected_workflow: true,
      source_install_id: "inst-ext",
      interface_contract: { trigger_channels: [], input_mode: "fields", expected_inputs: [], strict_inputs: true },
      output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
      entry: null,
      groups: [],
      for_each: [],
      steps: [],
      edges: [],
      validation_issues: [],
      automation_rules: [],
    } as never);
    mockedFetchWorkflowDag.mockRejectedValueOnce(new Error("protected structure"));
    mockedFetchWorkflowValidation.mockRejectedValueOnce(new Error("protected structure"));

    renderPage({
      initialEntry: "/registry/workflows/weekly_report/builder",
      surface: "builder",
    });

    expect(await screen.findByText("Protected extension workflow")).toBeInTheDocument();
    expect(screen.getByText(/provided by an extension and protected/i)).toBeInTheDocument();
    expect(screen.queryByText("Workflow detail load failed")).not.toBeInTheDocument();
    // Read-only: no Add, no structure status or Apply; the lock is said once.
    expect(await screen.findByTestId("structure-locked")).toHaveTextContent("Read-only");
    expect(screen.queryByRole("button", { name: "Add step" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply changes" })).not.toBeInTheDocument();
  });
});
