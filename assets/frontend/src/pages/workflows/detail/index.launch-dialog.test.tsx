import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpError } from "@/lib/http/errors";

import {
  canvasGraphFixture,
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowContract,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowTestRunState,
  mockedFindWorkflowLinkedProjectId,
  mockedTestRunWorkflow,
  mockedTestRunWorkflowStep,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

vi.setConfig({ testTimeout: 15_000 });

const builderEntry = "/registry/workflows/weekly_report/builder?project_id=project_alpha";

async function openFromToolbar() {
  const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
  const trigger = within(toolbar).getByRole("link", { name: "Test Run" });
  trigger.focus();
  fireEvent.click(trigger);
  return { dialog: await screen.findByRole("dialog", { name: /^Start a test run$/ }), trigger };
}

function installStepWorkflow() {
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  const edges = [{ source: { kind: "step", id: "wait" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } }];
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({
    revision: 7,
    steps: [
      { step_id: "wait", display_name: "Wait", step_kind: "wait", override: { wait: { duration_ms: 15000 } } },
      { step_id: "done", display_name: "Done", step_kind: "stop_success" },
    ],
    edges,
  }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "step", id: "wait" },
    steps: [
      { id: "wait", step_id: "wait", kind: "wait", display_name: "Wait", config_summary: "15 seconds", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
    ],
    edges,
  }));
}

describe("whole-workflow Test Run launch dialog", () => {
  it.each(["test-run", "workflow-test-run"])("opens the same modal from the %s URL alias without submitting", async (alias) => {
    const { router } = renderPage({ surface: "builder", initialEntry: `${builderEntry}&drawer=${alias}` });

    const dialog = await screen.findByRole("dialog", { name: /^Start a test run$/ });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(within(dialog).getByLabelText("Test Run Summary")).toBeInTheDocument();
    expect(within(dialog).getByRole("note", { name: "Test Run effects" })).toHaveTextContent(/not an external-effect sandbox/i);
    expect(within(dialog).queryByRole("button", { name: "Run step test run" })).not.toBeInTheDocument();
    await waitFor(() => expect(router.state.location.search).toBe("?project_id=project_alpha"));
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
  });

  it("opens from the lower toolbar and retains its input draft after Cancel with focus returned", async () => {
    renderPage({ surface: "builder", initialEntry: builderEntry });
    const { dialog, trigger } = await openFromToolbar();
    fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Keep this safe launch draft" } });

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    const reopened = await openFromToolbar();
    expect(within(reopened.dialog).getByLabelText("Test Run Summary")).toHaveValue("Keep this safe launch draft");
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  });

  it("keeps the ordinary Step Test Run contextual after opening and cancelling whole launch", async () => {
    installStepWorkflow();
    renderPage({ surface: "builder", initialEntry: builderEntry });
    await waitFor(() => expect(window.editStep).toBeTypeOf("function"));
    act(() => window.editStep?.("wait"));
    fireEvent.click(await screen.findByRole("tab", { name: "Test Run" }));
    fireEvent.change(await screen.findByLabelText("Step Test Run Summary"), { target: { value: "Retain the isolated step draft" } });

    const { dialog } = await openFromToolbar();
    expect(within(dialog).queryByLabelText("Step Test Run Summary")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(await screen.findByRole("tab", { name: "Test Run", selected: true })).toBeInTheDocument();
    expect(screen.getByLabelText("Step Test Run Summary")).toHaveValue("Retain the isolated step draft");
    expect(screen.getByRole("button", { name: "Run step test run" })).toBeEnabled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
  });

  it("preserves unsaved step parameters and the launch guard when Cancel returns to the editor", async () => {
    installStepWorkflow();
    renderPage({ surface: "builder", initialEntry: builderEntry });
    await waitFor(() => expect(window.editStep).toBeTypeOf("function"));
    act(() => window.editStep?.("wait"));
    fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "Unsaved wait label" } });

    const { dialog } = await openFromToolbar();
    expect(within(dialog).getByTestId("workflow-test-run-blocked")).toHaveTextContent(/save.*parameters/i);
    expect(within(dialog).getByRole("button", { name: "Run workflow test run" })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(await screen.findByRole("textbox", { name: "Step label" })).toHaveValue("Unsaved wait label");
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  });

  it("keeps a rejected submission in the dialog with the entered values", async () => {
    mockedTestRunWorkflow.mockRejectedValueOnce(new Error("The applied workflow cannot be launched."));
    renderPage({ surface: "builder", initialEntry: builderEntry });
    const { dialog } = await openFromToolbar();
    fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Keep values after refusal" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));

    expect(await within(dialog).findByText("The applied workflow cannot be launched.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Test Run Summary")).toHaveValue("Keep values after refusal");
    expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
  });

  it("submits one applied revision and opens that acknowledged job in Results", async () => {
    const fallback = fetchSpy.getMockImplementation()!;
    fetchSpy.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/jobs/job-launch-ack/definition" && (!init?.method || init.method === "GET")) {
        return new Response(JSON.stringify({ job_id: "job-launch-ack", available: false, unavailable_reason: "definition_snapshot_unavailable" }), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      if (url.pathname === "/api/v2/jobs/job-launch-ack" && (!init?.method || init.method === "GET")) {
        const job = { id: "job-launch-ack", project_id: "project_alpha", selected_workflow: "weekly_report", status: "completed" };
        return new Response(JSON.stringify({ ok: true, kind: "detail", data: { ...job, job, execution_steps: [] }, error: null, meta: { resource: "jobs" } }), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      return fallback(input, init);
    });
    let acknowledge: ((value: Awaited<ReturnType<typeof mockedTestRunWorkflow>>) => void) | undefined;
    mockedTestRunWorkflow.mockImplementationOnce(() => new Promise((resolve) => { acknowledge = resolve; }));
    mockedFetchWorkflowContract.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      interface: { enabled: true, expected_inputs: [{ name: "count", type: "number", required: true }, { name: "enabled", type: "boolean", required: true }] },
      definition_issues: [],
    } as never);
    renderPage({ surface: "builder", initialEntry: builderEntry });
    const { dialog } = await openFromToolbar();
    fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Run the applied snapshot" } });
    fireEvent.change(await within(dialog).findByLabelText(/^count/), { target: { value: "0" } });
    fireEvent.change(within(dialog).getByLabelText(/^enabled/), { target: { value: "false" } });
    const start = within(dialog).getByRole("button", { name: "Run workflow test run" });
    fireEvent.click(start);
    fireEvent.click(start);

    await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1));
    expect(mockedTestRunWorkflow).toHaveBeenCalledWith({ workflowId: "weekly_report", projectId: "project_alpha", taskSummary: "Run the applied snapshot", dryRun: false, expectedRevision: 7, inputs: { count: 0, enabled: false } });
    expect(start).toBeDisabled();
    expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
    await act(async () => {
      acknowledge?.({ workflow_id: "weekly_report", project_id: "project_alpha", job_id: "job-launch-ack", status: "completed", final_status: "success", needs_user_approval: false, trace: [] });
    });

    expect(await screen.findByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "job-launch-ack");
    expect(await screen.findByRole("link", { name: "Open full Job Details" })).toHaveAttribute("href", "/jobs/job-launch-ack");
    expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument();
    // The next start does not carry the last run's outcome: the run bar and Results say it.
    const reopened = await openFromToolbar();
    expect(within(reopened.dialog).queryByText(/test run completed/i)).not.toBeInTheDocument();
  });

  it("keeps the invalid-input Results confirmation accessible and preserves both drafts when navigation is refused", async () => {
    const user = userEvent.setup();
    const threadId = "thread-invalid-input-results";
    const jobId = "job-invalid-input-results";
    const storageKey = "fs.workflow-test-run.weekly_report";
    globalThis.localStorage.setItem(storageKey, threadId);
    globalThis.localStorage.setItem("fs.workflow-run-job.weekly_report", jobId);
    mockedFetchWorkflowTestRunState.mockResolvedValue({ workflow_id: "weekly_report", project_id: "project_alpha", job_id: jobId, thread_id: threadId, execution_id: "exec-remembered", status: "completed", final_status: "success", trace: [] } as never);
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    const fallback = fetchSpy.getMockImplementation()!;
    fetchSpy.mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/v2/jobs" && (!init?.method || init.method === "GET")) {
        const scoped = url.searchParams.get("workflow_id") === "weekly_report" && url.searchParams.get("project_id") === "project_alpha"
          && url.searchParams.get("task_source") === "workflow_test_run" && url.searchParams.get("page") === "1" && url.searchParams.get("per_page") === "8";
        const job = { id: jobId, project_id: "project_alpha", selected_workflow: "weekly_report", task_source: "workflow_test_run", status: "completed" };
        return new Response(JSON.stringify(scoped ? { ok: true, kind: "list", data: { jobs: [job], page: 1, per_page: 8, total: 1, total_pages: 1 }, error: null } : { error: "Unexpected history scope" }), {
          status: scoped ? 200 : 422, headers: { "content-type": "application/json" },
        });
      }
      if (url.pathname === `/api/v2/jobs/${jobId}` && (!init?.method || init.method === "GET")) {
        const job = { id: jobId, project_id: "project_alpha", selected_workflow: "weekly_report", status: "completed" };
        return new Response(JSON.stringify({ ok: true, kind: "detail", data: { ...job, job, execution_steps: [] }, error: null, meta: { resource: "jobs" } }), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      return fallback(input, init);
    });
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({
      revision: 7,
      steps: [{ step_id: "feed_rows", display_name: "Feed rows", step_kind: "tabular_feed", override: { tabular_feed: { operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.feed_dataset" } } }],
      edges: [],
    }));
    mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
      entry: { kind: "step", id: "feed_rows" },
      steps: [{ id: "feed_rows", step_id: "feed_rows", kind: "tabular_feed", display_name: "Feed rows", config_summary: "Create dataset", ports: [] }],
      edges: [],
    }));
    renderPage({ surface: "builder", initialEntry: builderEntry });
    await waitFor(() => expect(mockedFetchWorkflowTestRunState).toHaveBeenCalledWith(threadId));
    const initial = await openFromToolbar();
    // The launcher has no history: the remembered finished run is shown in the run bar.
    expect(within(initial.dialog).queryByRole("combobox", { name: "Test Run history" })).not.toBeInTheDocument();
    fireEvent.click(within(initial.dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(initial.dialog).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("workflow-run-bar")).toHaveAttribute("data-job-id", jobId));
    await waitFor(() => expect(window.editStep).toBeTypeOf("function"));
    act(() => window.editStep?.("feed_rows"));
    const rows = await screen.findByRole("textbox", { name: "Rows" });
    fireEvent.change(rows, { target: { value: "[{" } });
    expect(rows).toHaveAttribute("aria-invalid", "true");
    // A launch draft typed meanwhile survives the refused Results navigation too.
    await user.click(within(screen.getByRole("toolbar", { name: "Canvas tools" })).getByRole("link", { name: "Test Run" }));
    const initialConfirmation = await screen.findByRole("dialog", { name: "Discard invalid input?" });
    await user.click(within(initialConfirmation).getByRole("button", { name: "Discard input" }));
    const launch = await screen.findByRole("dialog", { name: /^Start a test run$/ });
    fireEvent.change(within(launch).getByLabelText("Test Run Summary"), { target: { value: "Retain the launch summary" } });
    await user.click(within(launch).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(launch).not.toBeInTheDocument());

    const opener = within(screen.getByTestId("workflow-run-bar")).getByRole("button", { name: "Results" });
    opener.focus();
    fireEvent.click(opener);
    const resultsConfirmation = await screen.findByRole("dialog", { name: "Discard invalid input?" });
    const refuse = within(resultsConfirmation).getByRole("button", { name: "Cancel" });
    await user.click(refuse);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Discard invalid input?" })).not.toBeInTheDocument());
    await waitFor(() => expect(opener).toHaveFocus());
    expect(screen.queryByRole("dialog", { name: "Test Run results" })).not.toBeInTheDocument();
    const retainedRows = await screen.findByRole("textbox", { name: "Rows" });
    expect(retainedRows).toBe(rows);
    expect(retainedRows).toBeVisible();
    expect(retainedRows).toHaveValue("[{");
    expect(rows).toHaveAttribute("aria-invalid", "true");
    const validationError = screen.getByRole("alert");
    expect(validationError).not.toBeEmptyDOMElement();
    expect(retainedRows).toHaveAccessibleDescription(validationError.textContent || "");
    await user.click(within(screen.getByRole("toolbar", { name: "Canvas tools" })).getByRole("link", { name: "Test Run" }));
    const launchConfirmation = await screen.findByRole("dialog", { name: "Discard invalid input?" });
    await user.click(within(launchConfirmation).getByRole("button", { name: "Discard input" }));
    const retainedLaunch = await screen.findByRole("dialog", { name: /^Start a test run$/ });
    expect(within(retainedLaunch).getByLabelText("Test Run Summary")).toHaveValue("Retain the launch summary");
    expect(globalThis.localStorage.getItem(storageKey)).toBe(threadId);
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
  });
});


function installDeclaredLaunchInput(type: "object" | "array", enabled: boolean, label = "") {
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report", display_name: "Design audit · f03 wait", enabled: false,
    interface: {
      enabled, trigger_channels: [], input_mode: "fields", strict_inputs: true,
      expected_inputs: [{ name: "audit_data", label, type, required: true }],
      output_mode: "none", expected_outputs: [], returned_files: [],
    },
    definition_issues: [],
  });
}

describe("native Test Run form contract regressions", () => {
  it("shows the real workflow title and saved human label while submitting the original wire key", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ display_name: "Design audit · f03 wait", enabled: false }));
    installDeclaredLaunchInput("object", false, "Audit input");
    renderPage({ surface: "builder", initialEntry: builderEntry });
    const { dialog } = await openFromToolbar();

    expect(within(dialog).getByText(/Runs Design audit · f03 wait with these inputs/i)).toBeVisible();
    const input = within(dialog).getByRole("textbox", { name: /^Audit input\b/ });
    fireEvent.change(input, { target: { value: '{"audit_ready":true}' } });
    fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Run the saved labelled input" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));

    await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1));
    expect(mockedTestRunWorkflow).toHaveBeenCalledWith({
      workflowId: "weekly_report", projectId: "project_alpha", taskSummary: "Run the saved labelled input",
      dryRun: false, expectedRevision: 7, inputs: { audit_data: { audit_ready: true } },
    });
  });

  it("uses the workflow ID and field name when human display values are absent", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ display_name: "" }));
    installDeclaredLaunchInput("object", true);
    renderPage({ surface: "builder", initialEntry: builderEntry });
    const { dialog } = await openFromToolbar();

    expect(within(dialog).getByText(/Runs weekly_report with these inputs/i)).toBeVisible();
    expect(within(dialog).getByRole("textbox", { name: /^audit_data\b/ })).toBeVisible();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  });

  it.each([
    { enabled: false, type: "object" as const, raw: "{", valid: "{}", value: {} },
    { enabled: true, type: "object" as const, raw: "{", valid: "{}", value: {} },
    { enabled: false, type: "array" as const, raw: "[", valid: "[]", value: [] },
    { enabled: true, type: "array" as const, raw: "[", valid: "[]", value: [] },
  ])("shows the $type error before POST with interface enabled=$enabled and recovers after editing", async ({ enabled, type, raw, valid, value }) => {
    installDeclaredLaunchInput(type, enabled);
    renderPage({ surface: "builder", initialEntry: builderEntry });
    const { dialog } = await openFromToolbar();
    const input = within(dialog).getByRole("textbox", { name: /^audit_data\b/ });
    fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "A valid nonempty audit Summary" } });
    fireEvent.change(input, { target: { value: raw } });
    fireEvent.blur(input);

    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription(/\S/);
    expect(input).toHaveValue(raw);
    const start = within(dialog).getByRole("button", { name: "Run workflow test run" });
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: valid } });
    expect(input).not.toHaveAttribute("aria-invalid", "true");
    expect(start).toBeEnabled();
    fireEvent.click(start);
    await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1));
    expect(mockedTestRunWorkflow).toHaveBeenCalledWith(expect.objectContaining({
      taskSummary: "A valid nonempty audit Summary", expectedRevision: 7, inputs: { audit_data: value },
    }));
  });

  it("explains an empty Summary inline and preserves its draft and input through Cancel before recovery", async () => {
    installDeclaredLaunchInput("object", false);
    renderPage({ surface: "builder", initialEntry: builderEntry });
    const { dialog, trigger } = await openFromToolbar();
    fireEvent.change(within(dialog).getByRole("textbox", { name: /^audit_data\b/ }), { target: { value: '{"retain":true}' } });
    const summary = within(dialog).getByLabelText("Test Run Summary");
    fireEvent.change(summary, { target: { value: "" } });
    fireEvent.blur(summary);

    expect(summary).toHaveAttribute("aria-invalid", "true");
    expect(summary).toHaveAccessibleDescription(/required/i);
    expect(within(dialog).getByRole("button", { name: "Run workflow test run" })).toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    const reopened = (await openFromToolbar()).dialog;
    const retainedSummary = within(reopened).getByLabelText("Test Run Summary");
    expect(retainedSummary).toHaveValue("");
    expect(retainedSummary).toHaveAttribute("aria-invalid", "true");
    expect(retainedSummary).toHaveAccessibleDescription(/required/i);
    expect(within(reopened).getByRole("textbox", { name: /^audit_data\b/ })).toHaveValue('{"retain":true}');
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();

    fireEvent.change(retainedSummary, { target: { value: "Recovered audit Summary" } });
    expect(retainedSummary).not.toHaveAttribute("aria-invalid", "true");
    expect(within(reopened).getByRole("button", { name: "Run workflow test run" })).toBeEnabled();
  });

  it("associates a real structured HTTP422 field error with its input and retains both drafts", async () => {
    installDeclaredLaunchInput("object", true);
    const message = "Workflow field 'audit_data' must be an object";
    mockedTestRunWorkflow.mockRejectedValueOnce(new HttpError({
      status: 422, url: "/api/workflows/weekly_report/test-run",
      payload: { detail: { code: "workflow_input_validation_failed", errors: [
        { code: "workflow.input.invalid_type", message, path: "inputs.audit_data", severity: "error" },
      ] } },
    }));
    renderPage({ surface: "builder", initialEntry: builderEntry });
    const { dialog } = await openFromToolbar();
    const input = within(dialog).getByRole("textbox", { name: /^audit_data\b/ });
    fireEvent.change(input, { target: { value: '{"saved":true}' } });
    fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Preserve after server refusal" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));

    await waitFor(() => expect(input).toHaveAttribute("aria-invalid", "true"));
    expect(input).toHaveAccessibleDescription(message);
    expect(input).toHaveValue('{"saved":true}');
    expect(within(dialog).getByLabelText("Test Run Summary")).toHaveValue("Preserve after server refusal");
    expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
  });
});


describe("native numeric draft and nested field error regressions", () => {
  it("retains an optional incomplete numeric draft through Cancel and refuses Start until it is corrected", async () => {
    const user = userEvent.setup();
    mockedFetchWorkflowContract.mockResolvedValue({
      workflow_id: "weekly_report", enabled: false,
      interface: { enabled: false, input_mode: "fields", strict_inputs: true, trigger_channels: [],
        expected_inputs: [{ name: "audit_quantity", label: "Audit quantity", type: "number", required: false }],
        output_mode: "none", expected_outputs: [], returned_files: [] },
      definition_issues: [],
    });
    renderPage({ surface: "builder", initialEntry: builderEntry });
    const { dialog, trigger } = await openFromToolbar();
    const quantity = within(dialog).getByLabelText(/^Audit quantity/);
    fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Preserve the incomplete quantity" } });
    await user.type(quantity, "1e");
    await user.tab();

    expect(quantity).toHaveValue("1e");
    expect(quantity).toHaveAttribute("aria-invalid", "true");
    expect(quantity).toHaveAccessibleDescription(/number/i);
    const start = within(dialog).getByRole("button", { name: "Run workflow test run" });
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();

    const reopened = (await openFromToolbar()).dialog;
    const retained = within(reopened).getByLabelText(/^Audit quantity/);
    expect(retained).toHaveValue("1e");
    expect(retained).toHaveAttribute("aria-invalid", "true");
    expect(retained).toHaveAccessibleDescription(/number/i);
    expect(within(reopened).getByRole("button", { name: "Run workflow test run" })).toBeDisabled();
    expect(within(reopened).getByLabelText("Test Run Summary")).toHaveValue("Preserve the incomplete quantity");
    await user.clear(retained);
    expect(retained).not.toHaveAttribute("aria-invalid", "true");
    expect(within(reopened).getByRole("button", { name: "Run workflow test run" })).toBeEnabled();
    await user.type(retained, "0");
    await user.click(within(reopened).getByRole("button", { name: "Run workflow test run" }));

    await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1));
    expect(mockedTestRunWorkflow).toHaveBeenCalledWith(expect.objectContaining({
      taskSummary: "Preserve the incomplete quantity", expectedRevision: 7, inputs: { audit_quantity: 0 },
    }));
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
  });

  it.each([
    { type: "object", raw: '{"count":0}', path: "inputs.audit_data.count", schema: { type: "object", properties: { count: { type: "number", minimum: 1 } } } },
    { type: "array", raw: "[0]", path: "inputs.audit_data.0", schema: { type: "array", items: { type: "number", minimum: 1 } } },
  ])("associates a nested $type schema error with the declared input and keeps its raw draft", async ({ type, raw, path, schema }) => {
    const message = "Workflow input schema validation failed: 0 is less than the minimum of 1";
    mockedFetchWorkflowContract.mockResolvedValue({
      workflow_id: "weekly_report", enabled: true,
      interface: { enabled: true, input_mode: "json_schema", strict_inputs: true, trigger_channels: [],
        input_schema: { type: "object", properties: { audit_data: schema } },
        expected_inputs: [{ name: "audit_data", label: "Audit input", type, required: true }],
        output_mode: "none", expected_outputs: [], returned_files: [] },
      definition_issues: [],
    });
    mockedTestRunWorkflow.mockRejectedValueOnce(new HttpError({ status: 422, url: "/api/workflows/weekly_report/test-run", payload: {
      detail: { code: "workflow_input_validation_failed", errors: [{ code: "workflow.input.schema_validation_failed", message, path, severity: "error" }] },
    } }));
    renderPage({ surface: "builder", initialEntry: builderEntry });
    const { dialog } = await openFromToolbar();
    const input = within(dialog).getByRole("textbox", { name: /^Audit input/ });
    fireEvent.change(input, { target: { value: raw } });
    fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Retain the nested schema refusal" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));

    await waitFor(() => expect(input).toHaveAttribute("aria-invalid", "true"));
    expect(input).toHaveAccessibleDescription(message);
    expect(within(dialog).getByRole("textbox", { name: /^Audit input/ })).toBe(input);
    expect(input).toHaveValue(raw);
    expect(within(dialog).getByLabelText("Test Run Summary")).toHaveValue("Retain the nested schema refusal");
    expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
  });
});


it("refuses the published missing-channel issue before POST and accepts an explicitly repaired API-only contract", async () => {
  const contract = { workflow_id: "weekly_report", enabled: false,
    interface: { enabled: true, trigger_channels: [] as string[], input_mode: "fields", expected_inputs: [], output_mode: "none" },
    definition_issues: [{ code: "workflow.interface.trigger_channels_missing", message: "Callable workflow must declare trigger_channels", path: "workflow.callable.trigger_channels", severity: "error" }] };
  mockedFetchWorkflowContract.mockResolvedValue(contract as never);
  const view = renderPage({ surface: "builder", initialEntry: builderEntry });
  try {
    const { dialog } = await openFromToolbar();
    expect(await within(dialog).findByText("Choose at least one trigger channel before running a Test Run.")).toBeVisible();
    const start = within(dialog).getByRole("button", { name: "Run workflow test run" });
    expect(start).toBeDisabled();
    fireEvent.click(start);
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  } finally { view.unmount(); }
  // Read the repaired published contract through a new real page/query owner;
  // no direct mutation of controller state or admission result is used.
  mockedFetchWorkflowContract.mockResolvedValue({ ...contract,
    interface: { ...contract.interface, trigger_channels: ["api"] }, definition_issues: [] } as never);
  const utils = renderPage({ surface: "builder", initialEntry: builderEntry });
  try {
    const { dialog } = await openFromToolbar();
    fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Explicit API-only contract" } });
    const start = within(dialog).getByRole("button", { name: "Run workflow test run" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1));
    expect(mockedTestRunWorkflow).toHaveBeenCalledWith(expect.objectContaining({ workflowId: "weekly_report", projectId: "project_alpha", expectedRevision: 7, taskSummary: "Explicit API-only contract", inputs: {} }));
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
  } finally { utils.unmount(); }
});

function configuredLaunchField(dialog: HTMLElement, label: string) {
  const caption = within(dialog).getByText(label, { exact: true });
  // Fixed previews have no input role; assert badge ownership in their nearest actual field wrapper.

  return caption.closest(".fs-field")!;
}

it.each([false, true])("marks configured values active only when the interface is enabled=%s without changing launch values", async (enabled) => {
  mockedFetchWorkflowContract.mockResolvedValue({ workflow_id: "weekly_report", enabled: false,
    interface: { enabled, trigger_channels: enabled ? ["api"] : [], input_mode: "fields", expected_inputs: [
      { name: "reference", label: "Reference", type: "string", default_configured: true, default: "server-fixed" },
      { name: "fixed_zero", label: "Fixed zero", type: "number", default_configured: true, default: 0 },
      { name: "fixed_false", label: "Fixed false", type: "boolean", default_configured: true, default: false },
      { name: "secret", label: "Configured secret", type: "secret", default_configured: true },
      { name: "file", label: "Sample file", type: "file", required: true, sample_file: { filename: "sample.json" } },
      { name: "quantity", label: "Quantity", type: "number", required: true },
      { name: "flag", label: "Flag", type: "boolean", required: true },
    ], output_mode: "none" }, definition_issues: [] } as never);
  const view = renderPage({ surface: "builder", initialEntry: builderEntry });
  try {
    const { dialog } = await openFromToolbar();
    for (const label of ["Reference", "Fixed zero", "Fixed false", "Configured secret"]) {
      const field = configuredLaunchField(dialog, label);
      expect(field).toHaveTextContent(enabled ? "Fixed" : "Configured, inactive");
    }
    const sample = configuredLaunchField(dialog, "Sample file");
    expect(sample).toHaveTextContent(enabled ? "Test file" : "Configured, inactive");
    expect(sample).toHaveTextContent("sample.json");
    const notice = "Configured defaults and test files are not applied while the workflow interface is disabled.";
    if (enabled) expect(within(dialog).queryByText(notice)).not.toBeInTheDocument();
    else expect(within(dialog).getByText(notice)).toBeVisible();
    expect(within(dialog).getByText("server-fixed")).toBeVisible();
    expect(within(dialog).getByText("0", { exact: true })).toBeVisible();
    expect(within(dialog).getByText("false", { exact: true, selector: "p" })).toBeVisible();
    fireEvent.change(within(dialog).getByLabelText(/^Quantity/), { target: { value: "0" } });
    fireEvent.change(within(dialog).getByLabelText(/^Flag/), { target: { value: "false" } });
    fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Preserve entered public values" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));
    await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1));
    expect(mockedTestRunWorkflow).toHaveBeenCalledWith(expect.objectContaining({ expectedRevision: 7, inputs: { quantity: 0, flag: false } }));
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
  } finally { view.unmount(); }
});


it("launches a saved schema-only Customer input without duplicate expected-input rows", async () => {
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report", enabled: true, display_name: "Weekly Report",
    interface: { enabled: true, trigger_channels: ["ui"], input_mode: "json_schema",
      expected_inputs: [], input_schema: { type: "object", required: ["customer"], properties: {
        customer: { type: "object", title: "Customer", required: ["name"], properties: {
          name: { type: "string", title: "Name" },
        } },
      } }, output_mode: "none", expected_outputs: [], returned_files: [] },
    definition_issues: [],
  } as never);
  renderPage({ surface: "builder", initialEntry: builderEntry });
  const { dialog } = await openFromToolbar();
  const customer = within(dialog).getByRole("textbox", { name: /^Customer/ });
  fireEvent.change(customer, { target: { value: '{"name":"Ada"}' } });
  fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Welcome Ada" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));
  await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledWith(expect.objectContaining({
    workflowId: "weekly_report", inputs: { customer: { name: "Ada" } }, taskSummary: "Welcome Ada",
  })));
  expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
});


it("launches a required schema-only integer input with zero", async () => {
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report", enabled: true, display_name: "Weekly Report",
    interface: { enabled: true, trigger_channels: ["ui"], input_mode: "json_schema",
      expected_inputs: [], input_schema: { type: "object", required: ["quantity"], properties: {
        quantity: { type: "integer", title: "Quantity" },
      } }, output_mode: "none", expected_outputs: [], returned_files: [] },
    definition_issues: [],
  } as never);
  renderPage({ surface: "builder", initialEntry: builderEntry });
  const { dialog } = await openFromToolbar();
  fireEvent.change(within(dialog).getByRole("textbox", { name: /^Quantity/ }), { target: { value: "0" } });
  fireEvent.change(within(dialog).getByLabelText("Test Run Summary"), { target: { value: "Count zero" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Run workflow test run" }));
  await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalledWith(expect.objectContaining({
    inputs: { quantity: 0 }, taskSummary: "Count zero",
  })));
});
