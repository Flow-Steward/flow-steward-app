import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { WorkflowCanvasIssue } from "@/pages/workflows/shared/api";
import { objectOrEmpty } from "./detail-helpers";
import { canvasGraphFixture, fetchSpy, mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail, mockedFetchWorkflowValidation, renderPage, workflowDetailFixture } from "./index.test-helpers";

const message = "Step 'query' is missing override.dataset_query.query_template_id";
const issue: WorkflowCanvasIssue = {
  scope: "step", scope_id: "query", authored_id: "query", step_id: "query", group_id: "", severity: "error",
  code: "workflow.step_dataset_query_template_missing", title: message, message,
  field_path: "workflow.steps.query.override.dataset_query.query_template_id",
};
const response = (value: unknown) => new Response(JSON.stringify(value), {
  status: 200, headers: { "content-type": "application/json" },
});

function installValidationReadback() {
  const fallback = fetchSpy.getMockImplementation();
  let revision = 7;
  let config: Record<string, unknown> = {
    query_template_id: "", params: {}, row_limit: 1, timeout_seconds: 0, result_target: "state.rows",
  };
  let label = "Local query";
  const writes: Array<Record<string, unknown>> = [];
  const reads: number[] = [];
  let release: () => void = () => undefined;
  const held = new Promise<void>((resolve) => { release = resolve; });
  const issues = () => config.query_template_id ? [] : [{ ...issue }];
  const validationErrors = () => issues().map((row) => ({ code: row.code, message: row.title, path: row.field_path }));
  mockedFetchWorkflowDetail.mockImplementation(async () => workflowDetailFixture({
    enabled: false, revision, steps: [{ id: "11111111-1111-1111-1111-111111111099", step_id: "query",
      step_kind: "dataset_query", display_name: label, override: { dataset_query: structuredClone(config) } }],
  }));
  mockedFetchWorkflowValidation.mockImplementation(async () => ({ validation: {
    valid: issues().length === 0, errors: validationErrors(), warnings: [],
  } }) as never);
  mockedFetchWorkflowCanvasGraph.mockImplementation(async () => {
    const graph = canvasGraphFixture({
      workflow: { id: "weekly_report", display_name: "Weekly Report", enabled: false, callable_by_parent: false, revision },
      entry: { kind: "step", id: "query" },
      steps: [{ id: "query", step_id: "query", kind: "dataset_query", display_name: label,
        config_summary: "", ports: [{ outcome: { name: "success" }, label: "Next" }] }],
      validation_issues: issues(),
    });
    reads.push(revision);
    if (revision > 7) await held;
    return graph;
  });
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === "/api/datasets/query-templates") return response([{
      query_template_id: "query-1", dataset_id: "dataset-1", display_name: "Local saved query",
      params_schema_jsonb: { type: "object", properties: {}, additionalProperties: true },
    }]);
    if (url.pathname === "/api/workflows/weekly_report/changes" && init?.method === "POST") {
      const body = objectOrEmpty(JSON.parse(String(init.body)));
      if (body.dry_run === true) return response({ workflow_id: "weekly_report", revision,
        applied: false, valid: issues().length === 0, issues: [], blocking_issues: [], workflow: {} });
      const operations = body.operations as Array<Record<string, unknown>>;
      expect(operations).toHaveLength(1);
      expect(operations[0]).toMatchObject({ op: "update_step_config", step_id: "query" });
      writes.push(structuredClone(body));
      const patch = objectOrEmpty(operations[0].patch);
      config = structuredClone(objectOrEmpty(objectOrEmpty(patch.override).dataset_query));
      label = String(patch.display_name);
      revision += 1;
      return response({ workflow_id: "weekly_report", revision, applied: true,
        valid: issues().length === 0, issues: validationErrors() });
    }
    if (!fallback) throw new Error("Missing ordinary HTTP fixture");
    return fallback(input, init);
  });
  return { writes, reads, release: () => release(), config: () => config };
}

it.each([true, false])("reconciles the focused issue from current saved validation; repaired=%s", async (repaired) => {
  const boundary = installValidationReadback();
  const view = renderPage({ surface: "builder" });
  try {
    fireEvent.click(await screen.findByRole("button", { name: /^Diagnostics/ }));
    const diagnostics = await screen.findByRole("region", { name: "Validation issues" });
    fireEvent.click(within(diagnostics).getByRole("button", { name: new RegExp("missing override.dataset_query.query_template_id") }));
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    expect(within(drawer).getByText("Validation issue")).toBeVisible();
    expect(within(drawer).getByText(message)).toBeVisible();
    if (repaired) {
      fireEvent.click(within(drawer).getByRole("combobox", { name: "Saved query" }));
      fireEvent.click(await screen.findByRole("option", { name: "Local saved query" }));
      // Validation follows the draft: the chosen query fixes the issue before anything is saved.
      await waitFor(() => expect(within(drawer).queryByText("Validation issue")).not.toBeInTheDocument());
    } else {
      fireEvent.change(within(drawer).getByRole("textbox", { name: "Step label" }), { target: { value: "Still missing query" } });
    }
    fireEvent.click(within(drawer).getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(boundary.writes).toHaveLength(1));
    expect(boundary.writes[0]).toMatchObject({ expected_revision: 7 });
    expect(boundary.config()).toEqual({ query_template_id: repaired ? "query-1" : "", params: {},
      row_limit: 1, timeout_seconds: 0, result_target: "state.rows" });
    await waitFor(() => expect(boundary.reads).toContain(8));
    // The accepted write is not a fresh canvas validation response yet: an unrepaired issue stays.
    if (repaired) expect(within(drawer).queryByText(message)).not.toBeInTheDocument();
    else expect(within(drawer).getByText(message)).toBeVisible();
    await act(async () => boundary.release());
    if (repaired) {
      await waitFor(() => expect(within(drawer).queryByText("Validation issue")).not.toBeInTheDocument());
      expect(within(drawer).queryByText(message)).not.toBeInTheDocument();
    } else {
      await waitFor(() => expect(within(drawer).queryByRole("button", { name: "Saving parameters…" })).not.toBeInTheDocument());
      expect(within(drawer).getByText(message)).toBeVisible();
    }
    expect(within(drawer).getByLabelText("Result target")).toHaveValue("state.rows");
    expect(within(drawer).getByLabelText("Row limit")).toHaveDisplayValue("1");
    expect(boundary.writes).toHaveLength(1);
  } finally {
    boundary.release();
    view.unmount();
  }
}, 15_000);


it("retires the focused issue across project A→B→A until Diagnostics is explicitly selected again", async () => {
  const boundary = installValidationReadback();
  const route = (project: string) => `/registry/workflows/weekly_report/builder?project_id=${project}`;
  const view = renderPage({ surface: "builder", initialEntry: route("project_alpha") });
  try {
    fireEvent.click(await screen.findByRole("button", { name: /^Diagnostics/ }));
    const diagnostics = await screen.findByRole("region", { name: "Validation issues" });
    fireEvent.click(within(diagnostics).getByRole("button", { name: new RegExp("missing override.dataset_query.query_template_id") }));
    expect(await screen.findByText("Validation issue")).toBeVisible();
    await act(async () => { await view.router.navigate(route("project_beta")); });
    expect(screen.queryByText("Validation issue")).not.toBeInTheDocument();
    await act(async () => { await view.router.navigate(route("project_alpha")); });
    expect(screen.queryByText("Validation issue")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Diagnostics/ }));
    const currentDiagnostics = await screen.findByRole("region", { name: "Validation issues" });
    fireEvent.click(within(currentDiagnostics).getByRole("button", { name: new RegExp("missing override.dataset_query.query_template_id") }));
    expect(await screen.findByText("Validation issue")).toBeVisible();
    expect(boundary.writes).toEqual([]);
  } finally {
    boundary.release();
    view.unmount();
  }
}, 15_000);

it("presents the three exact diagnostics with business targets, raw correlation and supported corrections", async () => {
  const switchRaw = "Step 'route' has no edge for its case 'c_email' outcome";
  const httpRaw = "http_request requires override.http_request.url";
  const intentRaw = "A workflow whose only terminal is stop_fail must declare explicit expected-failure intent in settings.traits.expected_failure or tags";
  const switchIssue = { scope: "step", scope_id: "route", authored_id: "route", step_id: "route",
    severity: "error", code: "workflow.graph.outcome_missing", title: switchRaw, message: switchRaw,
    field_path: "workflow.steps.route", suggested_action: "Open Connections and connect the missing outcome.",
    suggested_action_key: "react.workflows.diagnostic_action_missing_outcome",
    issues: [{ path: "workflow.steps.route", message: switchRaw,
      outcome_name: "case", outcome_case_id: "c_email" }] };
  const httpIssue = { scope: "step", scope_id: "request", authored_id: "request", step_id: "request",
    severity: "error", code: "workflow.step_http_request_url_missing", title: httpRaw, message: httpRaw,
    field_path: "workflow.steps.request.override.http_request.url",
    suggested_action: "Open URL in Smart setup and enter the request address.",
    suggested_action_key: "react.workflows.diagnostic_action_http_url" };
  const intentIssue = { scope: "workflow", scope_id: "workflow", authored_id: "workflow",
    severity: "error", code: "workflow.terminal_fail_intent_missing", title: intentRaw, message: intentRaw,
    field_path: "workflow.settings.traits.expected_failure",
    suggested_action: "Open workflow settings and declare an expected failure.",
    suggested_action_key: "react.workflows.diagnostic_action_expected_failure" };
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 7, steps: [
    { step_id: "route", step_kind: "switch", display_name: "Route invoice", override: { switch: {
      selector: "state.channel", cases: [{ case_id: "c_email", when: "email", label: "Email" }],
    } } },
    { step_id: "request", step_kind: "http_request", display_name: "Send request",
      override: { http_request: { method: "POST", url: "", response_target: "state.reply" } } },
    { step_id: "failed", step_kind: "stop_fail", display_name: "Stop with failure" },
  ], settings: { traits: { retained: "yes" }, tags: ["billing"] } }) as never);
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    workflow: { id: "weekly_report", display_name: "Weekly Report", revision: 7, enabled: false, callable_by_parent: false },
    steps: [
      { id: "route", step_id: "route", kind: "switch", display_name: "Route invoice", config_summary: "",
        ports: [{ outcome: { name: "case", case_id: "c_email" }, label: "Email" }] },
      { id: "request", step_id: "request", kind: "http_request", display_name: "Send request", config_summary: "",
        ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "failed", step_id: "failed", kind: "stop_fail", display_name: "Stop with failure", config_summary: "", ports: [] },
    ], validation_issues: [switchIssue, httpIssue, intentIssue] as never,
  }) as never);
  renderPage({ surface: "builder" });
  fireEvent.click(await screen.findByRole("button", { name: /^Diagnostics/ }));
  const panel = await screen.findByRole("region", { name: "Validation issues" });
  const switchCard = within(panel).getByRole("button", { name: /has no edge for its case/ });
  const httpCard = within(panel).getByRole("button", { name: /http_request requires/ });
  const intentCard = within(panel).getByRole("button", { name: /only terminal is stop_fail/ });
  expect.soft(switchCard).toHaveTextContent("Route invoice");
  expect.soft(switchCard).toHaveTextContent("Email");
  expect.soft(switchCard).toHaveTextContent("Connections");
  expect.soft(switchCard).toHaveTextContent("workflow.graph.outcome_missing");
  expect.soft(httpCard).toHaveTextContent("Send request");
  expect.soft(httpCard).toHaveTextContent("URL");
  expect.soft(httpCard).toHaveTextContent("workflow.step_http_request_url_missing");
  expect.soft(httpCard).toHaveTextContent("workflow.steps.request.override.http_request.url");
  expect.soft(intentCard).toHaveTextContent(/declare an expected failure/i);
  expect.soft(intentCard).toHaveTextContent("workflow.terminal_fail_intent_missing");
  expect.soft(intentCard).toHaveTextContent("workflow.settings.traits.expected_failure");
  fireEvent.click(switchCard);
  let drawer = await screen.findByRole("dialog", { name: "Step editor" });
  expect.soft(within(drawer).getByText("Connections")).toBeVisible();
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument());
  if (!screen.queryByRole("region", { name: "Validation issues" })) fireEvent.click(screen.getByRole("button", { name: /^Diagnostics/ }));
  fireEvent.click(within(await screen.findByRole("region", { name: "Validation issues" }))
    .getByRole("button", { name: /http_request requires/ }));
  drawer = await screen.findByRole("dialog", { name: "Step editor" });
  await waitFor(() => expect(within(drawer).getByRole("textbox", { name: "Step label" })).toHaveValue("Send request"));
  expect.soft(within(drawer).getByLabelText("URL")).toBeVisible();
  fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument());
  if (!screen.queryByRole("region", { name: "Validation issues" })) fireEvent.click(screen.getByRole("button", { name: /^Diagnostics/ }));
  fireEvent.click(within(await screen.findByRole("region", { name: "Validation issues" }))
    .getByRole("button", { name: /only terminal is stop_fail/ }));
  const settings = await screen.findByRole("dialog", { name: "Workflow settings" });
  expect.soft(within(settings).queryByRole("checkbox", { name: /Declare an expected failure/ })).toBeInTheDocument();
}, 20_000);


it("opens and saves an expected-failure issue on the child workflow's own page", async () => {
  const writes: Array<{ url: string; body: Record<string, unknown> }> = [];
  const originalFetch = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    const url = String(input);
    if (init?.method === "PATCH" && url.startsWith("/api/workflows/")) {
      writes.push({ url, body: JSON.parse(String(init.body)) as Record<string, unknown> });
      return response({ revision: 4 });
    }
    if (!originalFetch) throw new Error("Missing child workflow fixture");
    return originalFetch(input, init);
  });
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({
    workflow_id: "client_fix", display_name: "Fix the name", revision: 3,
    settings: { traits: { retained: "keep" }, callable: { enabled: false, trigger_channels: ["ui"],
      input_mode: "fields", expected_inputs: [], expected_outputs: [] } },
  }) as never);
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    workflow: { id: "client_fix", display_name: "Fix the name", revision: 3, enabled: false, callable_by_parent: true },
    validation_issues: [{ scope: "workflow", scope_id: "workflow", authored_id: "workflow",
      severity: "error", code: "workflow.terminal_fail_intent_missing", title: "Declare expected failure intent",
      message: "Declare expected failure intent", field_path: "workflow.settings.traits.expected_failure",
      suggested_action: "Open workflow settings and declare an expected failure.",
      suggested_action_key: "react.workflows.diagnostic_action_expected_failure" }],
  }) as never);
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/client_fix/builder?project_id=project_alpha" });
  fireEvent.click(await screen.findByRole("button", { name: /^Diagnostics/ }));
  fireEvent.click(within(await screen.findByRole("region", { name: "Validation issues" }))
    .getByRole("button", { name: /Declare expected failure intent/ }));
  const settings = await screen.findByRole("dialog", { name: "Workflow settings" });
  expect(within(settings).getByLabelText("Display name")).toHaveValue("Fix the name");
  fireEvent.click(within(settings).getByRole("checkbox", { name: /Declare an expected failure/ }));
  fireEvent.click(within(settings).getByRole("button", { name: "Save workflow settings" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0].url).toBe("/api/workflows/client_fix");
  expect(writes[0].body).toMatchObject({ expected_revision: 3, settings: { traits: {
    retained: "keep", expected_failure: true,
  } } });
});
