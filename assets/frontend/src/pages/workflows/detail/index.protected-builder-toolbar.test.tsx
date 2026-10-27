import { fireEvent, screen, within } from "@testing-library/react";

import {
  canvasGraphFixture,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowContract,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowExportPolicy,
  mockedFetchWorkflowValidation,
  mockedFindWorkflowLinkedProjectId,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

vi.setConfig({ testTimeout: 15_000 });

const PROTECTED = {
  extension_owned: true,
  protected: true,
  structure_change_allowed: false,
  step_test_run_allowed: false,
  reference_export_allowed: true,
};

function installProtectedWorkflow(policy: Record<string, unknown> = PROTECTED) {
  const steps = [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable", override: { set_variable: { assignments: { "state.plan": true } } } },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ];
  const edges = [{ source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } }];
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ steps, edges, portability: policy }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(
    canvasGraphFixture({
      entry: { kind: "step", id: "plan" },
      steps: [
        { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", config_summary: "state.plan", ports: [{ outcome: { name: "success" }, label: "Next" }] },
        { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
      ],
      edges,
      protected_workflow: policy.protected === true,
      read_only: true,
    }),
  );
  mockedFetchWorkflowExportPolicy.mockResolvedValue({ workflow_id: "weekly_report", ...policy } as never);
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report",
    interface: { enabled: true, expected_inputs: [{ name: "customer", type: "string", required: true }] },
    definition_issues: [],
  } as never);
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
}

describe("the builder of a protected extension workflow", () => {
  it("keeps the reading tools and the whole-workflow Test Run, and drops the editing ones", async () => {
    installProtectedWorkflow();
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });

    const readOnly = await screen.findByTestId("structure-locked");
    expect(readOnly).toHaveTextContent("Read-only");
    expect(readOnly).toHaveAccessibleDescription(/provided by an extension/i);
    expect(screen.getByRole("note", { name: "Protected extension workflow" })).toHaveTextContent(/cannot be edited/i);
    // B32: the same toolbar as an ordinary workflow, minus Add, Apply and Auto layout.
    fireEvent.click(screen.getByRole("button", { name: /Search canvas/ }));
    expect(screen.getByRole("textbox", { name: "Search workflow canvas" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    expect(screen.getByRole("button", { name: "Show legend" })).toBeInTheDocument();
    // The extension owns the definition: no export, not even a reference bundle.
    expect(screen.queryByText(/Export reference|Export YAML/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Note images are not included in exports/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add step" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Apply changes/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Auto layout/ })).not.toBeInTheDocument();

    // The bottom-centre Test Run opens the whole-workflow Form Dialog
    // with the workflow's own inputs.
    fireEvent.click(screen.getByRole("link", { name: "Test Run" }));
    const dialog = await screen.findByRole("dialog", { name: "Start a test run" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(await within(dialog).findByText("Workflow inputs")).toBeInTheDocument();
    expect(within(dialog).getByText(/customer/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Run workflow test run" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Run step test run" })).not.toBeInTheDocument();
  });
});

describe("the licensed read-only builder of an installed workflow", () => {
  const LICENSED = {
    extension_owned: true,
    protected: false,
    full_export_allowed: true,
    clone_allowed: true,
    structure_change_allowed: false,
    step_test_run_allowed: true,
  };

  it("opens Interface inputs for inspection while keeping source editing and Apply unavailable", async () => {
    installProtectedWorkflow(LICENSED);
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });

    expect(await screen.findByTestId("structure-locked")).toHaveTextContent("Read-only");
    expect(screen.getByRole("note", { name: "Structure read-only (extension)" })).toHaveTextContent(/cannot be edited/i);
    fireEvent.click(await screen.findByRole("button", { name: "Start — workflow inputs" }));
    const drawer = await screen.findByRole("dialog", { name: "Workflow interface" });
    expect(within(drawer).getByRole("tab", { name: "Inputs" })).toHaveAttribute("aria-selected", "true");
    expect(within(drawer).getByText("customer")).toBeVisible();
    expect(within(drawer).getByRole("combobox", { name: "Input mode" })).toBeDisabled();
    expect(within(drawer).queryByRole("button", { name: "Add input" })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Save workflow settings" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add step" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Apply changes/ })).not.toBeInTheDocument();
  });

  it("shows licensed validation and read failures rather than hiding them as protected", async () => {
    installProtectedWorkflow(LICENSED);
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({
      steps: [
        { step_id: "plan", display_name: "Plan work", step_kind: "agent" },
        { step_id: "done", display_name: "Done", step_kind: "stop_success" },
      ],
      portability: LICENSED,
    }));
    mockedFetchWorkflowValidation.mockResolvedValue({ validation: {
      valid: false,
      errors: [{ code: "workflow.interface.input_missing", message: "Customer input is missing", path: "settings.callable.expected_inputs" }],
      warnings: [],
    } } as never);
    renderPage({ surface: "detail", embedded: false, initialEntry: "/registry/workflows/weekly_report?project_id=project_alpha" });

    expect(await screen.findByText(/Validation: 1 errors \/ 0 warnings/)).toBeInTheDocument();
  });

  it("surfaces a licensed validation query error", async () => {
    installProtectedWorkflow(LICENSED);
    mockedFetchWorkflowValidation.mockRejectedValue(new Error("Validation read unavailable"));
    renderPage({ surface: "detail", embedded: false, initialEntry: "/registry/workflows/weekly_report?project_id=project_alpha" });

    expect(await screen.findByText("Workflow detail load failed")).toBeInTheDocument();
    expect(screen.getByText("Validation read unavailable")).toBeInTheDocument();
  });
});

describe("the builder of an extension workflow whose required extension is missing", () => {
  const POSTGRES_ID = "acme.data-connector-postgres";
  const SENTENCE = `Needs the extension ${POSTGRES_ID}, which this account does not have active. Install it from Extensions > Catalog, or turn it on if it is installed.`;
  const UNAVAILABLE = {
    availability_status: "unavailable",
    availability_reason_code: "required_extension_uninstalled",
    availability_reason_text: SENTENCE,
    new_runs_blocked: true,
    missing_extension_details: [
      { extension_id: POSTGRES_ID, display_name: "PostgreSQL (Google MCP Toolbox)", state: "not_installed" },
    ],
    viewer_can_manage_extensions: true,
  };

  it("says it once, worded for a person, with Install and the id only under Details", async () => {
    installProtectedWorkflow();
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({
      enabled: false,
      ...UNAVAILABLE,
      portability: { ...PROTECTED, ...UNAVAILABLE },
    }));
    mockedFetchWorkflowExportPolicy.mockResolvedValue({ workflow_id: "weekly_report", ...PROTECTED, ...UNAVAILABLE } as never);
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });

    const headline = "This workflow can't run yet: it needs PostgreSQL (Google MCP Toolbox).";
    const notice = await screen.findByTestId("workflow-missing-extension-notice");
    expect(within(notice).getByText(headline)).toBeInTheDocument();
    // One notice: no page-level "Workflow unavailable" card above the canvas.
    expect(screen.getAllByTestId("workflow-missing-extension-notice")).toHaveLength(1);
    expect(screen.getAllByText(headline)).toHaveLength(1);
    expect(screen.queryByText("Workflow unavailable")).not.toBeInTheDocument();
    expect(notice).toContainHTML('class="fs-alert warning"');
    expect(within(notice).getByRole("link", { name: "Install" })).toHaveAttribute(
      "href",
      `/extensions?find=${encodeURIComponent(POSTGRES_ID)}#marketplace`,
    );
    const details = within(notice).getByRole("group");
    expect(within(details).getByText(POSTGRES_ID)).toBeInTheDocument();
    expect(within(details).getByText(SENTENCE)).toBeInTheDocument();
    expect(screen.getAllByText(SENTENCE)).toHaveLength(1);
  });
});
