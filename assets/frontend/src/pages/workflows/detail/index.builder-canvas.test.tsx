import userEvent from "@testing-library/user-event";
import { loadManualLayout, manualLayoutStorageKey } from "@/pages/workflows/builder/canvas-core";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { JobListItem } from "@/pages/jobs/list/api";
import type { JobDetail } from "@/pages/jobs/detail/api";

import {
  canvasGraphFixture,
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowContract,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowExportPolicy,
  mockedFetchWorkflowValidation,
  mockedFetchWorkflowTestRunState,
  mockedFindWorkflowLinkedProjectId,
  mockedTestRunWorkflow,
  mockedTestRunWorkflowStep,
  dryRunChangeSets,
  postedChangeSets,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";
import {
  firstStringFromUnknownValue,
  stringFromUnknownValue,
} from "@/lib/utils/unknown-values";

const contextualPlayHitTestDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
beforeAll(() => Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [] }));
afterAll(() => {
  if (contextualPlayHitTestDescriptor) Object.defineProperty(document, "elementsFromPoint", contextualPlayHitTestDescriptor);
  else Reflect.deleteProperty(document, "elementsFromPoint");
});

const NEXT = [{ outcome: { name: "success" }, label: "Next" }];

/** A small linear workflow: plan → done, at the given revision. */
function installLinearWorkflow(revision = 5) {
  const steps = [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable", override: { set_variable: { assignments: { "state.plan": true } } } },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ];
  const edges = [
    { source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
  ];
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision, steps, edges }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(
    canvasGraphFixture({
      entry: { kind: "step", id: "plan" },
      steps: [
        { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", config_summary: "state.plan", ports: NEXT },
        { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
      ],
      edges,
    }),
  );
  return { steps, edges };
}

type ChangesResponse = { status: number; body: Record<string, unknown> };

function cleanDryRun(body: Record<string, unknown>): ChangesResponse {
  return {
    status: 200,
    body: {
      workflow_id: "weekly_report",
      revision: body.expected_revision,
      valid: true,
      issues: [],
      blocking_issues: [],
      applied: false,
      workflow: {},
    },
  };
}

/**
 * Answer `POST /changes` that write with the given responses, one per call.
 * Dry runs of the local set are answered by `dryRun` (a clean result by default).
 */
function serveChanges(...responses: ChangesResponse[]) {
  return serveChangesWithDryRun(cleanDryRun, ...responses);
}

function serveChangesWithDryRun(
  dryRun: (body: Record<string, unknown>) => ChangesResponse,
  ...responses: ChangesResponse[]
) {
  const fallback = fetchSpy.getMockImplementation();
  let call = 0;
  fetchSpy.mockImplementation(async (input, init) => {
    if (stringFromUnknownValue(input).endsWith("/changes") && init?.method === "POST") {
      const body = JSON.parse(String(init.body || "{}")) as Record<string, unknown>;
      if (body.dry_run === true) {
        const answer = dryRun(body);
        return new Response(JSON.stringify(answer.body), {
          status: answer.status,
          headers: { "content-type": "application/json" },
        });
      }
      const response = responses[Math.min(call, responses.length - 1)];
      call += 1;
      return new Response(JSON.stringify(response.body), {
        status: response.status,
        headers: { "content-type": "application/json" },
      });
    }
    return fallback ? fallback(input, init) : new Response("{}");
  });
}

function applied(revision: number) {
  return { status: 200, body: { workflow_id: "weekly_report", revision, valid: true, issues: [], workflow: {} } };
}

/** A staged step's Save: "Update new step", or "Updated" for a moment after an update lands. */
const UPDATE_NEW_STEP = /^(Update new step|Updated)$/;

async function addRootStepFromCanvas(label: string, kind: string, discardParameters = false) {
  // The first journey of the file renders the page cold; under gate load that outlasts 1 s.
  fireEvent.click(await screen.findByRole("button", { name: "Add step" }, { timeout: 5_000 }));
  const palette = await screen.findByRole("dialog", { name: "Add step" });
  fireEvent.click(within(palette).getByRole("option", { name: "Set variable" }));

  if (discardParameters) {
    const dialog = await screen.findByRole("dialog", { name: "Discard unsaved parameters?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Discard parameters" }));
  }
  await screen.findByRole("heading", { name: "Step editor" });
  fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: label } });
  expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue(kind);
  fireEvent.click(screen.getByRole("button", { name: "Update new step" }));
}

function openStep(stepId: string) {
  act(() => {
    (globalThis as unknown as Window).editStep?.(stepId);
  });
}


type RoutingKind = "if" | "switch";

const routingCases: Array<{ kind: RoutingKind; label: string; stepId: string }> = [
  { kind: "if", label: "Check plan", stepId: "if" },
  { kind: "switch", label: "Route plan", stepId: "route_plan" },
];

function completedRoutingOverride(kind: RoutingKind) {
  return kind === "if"
    ? { if: { condition: { selector: "state.plan", operator: "equals", value: "ready" } } }
    : { switch: { selector: "state.plan", cases: [{ case_id: "case_1", when: "ready" }] } };
}

function expectBlankRoutingEditorAndFill(kind: RoutingKind) {
  const selector = screen.getByLabelText("Selector", { exact: true });
  expect(selector).toHaveValue("");
  if (kind === "if") {
    expect(screen.queryByText("Unsupported raw IF condition")).not.toBeInTheDocument();
    expect(selector).toHaveAttribute("placeholder", "state.inputs.city");
    expect(screen.getByRole("textbox", { name: "Value" })).toHaveValue("");
  } else {
    expect(selector).toHaveAttribute("placeholder", "task.mode");
    expect(screen.getByRole("textbox", { name: "Case value 1" })).toHaveValue("");
    expect(screen.getByText("case_id: case_1")).toBeInTheDocument();
  }
  fireEvent.change(selector, { target: { value: "state.plan" } });
  if (kind === "if") {
    fireEvent.change(screen.getByRole("textbox", { name: "Value" }), { target: { value: "ready" } });
  } else {
    fireEvent.change(screen.getByRole("textbox", { name: "Case value 1" }), { target: { value: "ready" } });
  }
  expect(selector).toHaveValue("state.plan");
}

function expectReopenedRoutingEditor(kind: RoutingKind) {
  expect(screen.getByLabelText("Selector", { exact: true })).toHaveValue("state.plan");
  if (kind === "if") {
    expect(screen.getByRole("textbox", { name: "Value" })).toHaveValue("ready");
    expect(screen.queryByText("Unsupported raw IF condition")).not.toBeInTheDocument();
  } else {
    expect(screen.getByRole("textbox", { name: "Case value 1" })).toHaveValue("ready");
    expect(screen.getByText("case_id: case_1")).toBeInTheDocument();
  }
}

function expectedRoutingStep(kind: RoutingKind, label: string, stepId: string, forEachId: string | null) {
  return {
    block_key: "",
    display_name: label,
    agent_id: "",
    connector_id: "",
    connector_install_id: "",
    workflow_id: "",
    optional: false,
    override: completedRoutingOverride(kind),
    group_id: null,
    for_each_id: forEachId,
    lane_id: null,
    step_id: stepId,
    step_kind: kind,
  };
}

describe("WorkflowsDetailPage canvas builder", { timeout: 30_000 }, () => {
  it("closes a discarded new step editor instead of treating its stale fields as another creation", async () => {
    installLinearWorkflow();
    renderPage({ surface: "builder" });
    await addRootStepFromCanvas("Discard me", "set_variable");
    expect(await screen.findByRole("button", { name: UPDATE_NEW_STEP })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Step editor" })).not.toBeInTheDocument());
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    expect(screen.queryByRole("button", { name: "Add step to changes" })).not.toBeInTheDocument();
    expect(postedChangeSets()).toEqual([]);
  });

  it("closes the step editor and clears the selection when the new step it shows is deleted", async () => {
    installLinearWorkflow();
    renderPage({ surface: "builder" });
    await addRootStepFromCanvas("Tmp", "set_variable");
    expect(await screen.findByRole("button", { name: UPDATE_NEW_STEP })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete step" }));
    // Not a "New step" form still filled with Tmp, whose "Add step to changes" could only fail.
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Step editor" })).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Add step to changes" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Step label" })).not.toBeInTheDocument();
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 2 change(s)");
    expect(postedChangeSets()).toEqual([]);
  });

  it("collects a new step and its connection locally and applies them as one change set", async () => {
    installLinearWorkflow(5);
    serveChanges(applied(6));
    renderPage({ surface: "builder" });

    await addRootStepFromCanvas("Review draft", "set_variable");
    // The new step's Save says ✓ Updated for a moment after the helper's update.
    expect(await screen.findByRole("button", { name: UPDATE_NEW_STEP })).toBeInTheDocument();
    // The editor carries no status chips; the unapplied structure is said by the canvas.
    expect(screen.queryByTestId("step-form-status")).not.toBeInTheDocument();
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
    // Nothing is written yet.
    expect(postedChangeSets()).toEqual([]);

    fireEvent.click(screen.getByRole("combobox", { name: "Next step for Next" }));
    fireEvent.click(await screen.findByRole("option", { name: "Done" }));
    await waitFor(() => {
      expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 2 change(s)");
    });

    fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(postedChangeSets()[0]).toEqual({
      expected_revision: 5,
      operations: [
        {
          op: "create_step",
          step: expect.objectContaining({
            step_id: "set_variable",
            step_kind: "set_variable",
            display_name: "Review draft",
            group_id: null,
            lane_id: null,
          }),
        },
        {
          op: "connect",
          edge: {
            source: { kind: "step", id: "set_variable" },
            outcome: { name: "success" },
            target: { kind: "step", id: "done" },
          },
        },
      ],
    });
    await waitFor(() => {
      expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    });
    expect(mockedFetchWorkflowDetail.mock.calls.length).toBeGreaterThan(1);
  });


  it.each(routingCases)("creates a root $kind with an honest blank editor, then applies and reopens authored values", async ({ kind, label }) => {
    const { steps, edges } = installLinearWorkflow(5);
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ enabled: false, revision: 5, steps, edges }));
    const rootStepId = kind === "if" ? "if" : "switch";
    const authored = expectedRoutingStep(kind, label, rootStepId, null);
    const appliedDetail = workflowDetailFixture({ enabled: false, revision: 6, steps: [...steps, authored], edges });
    serveChanges({ status: 200, body: { ...applied(6).body, workflow: appliedDetail } });
    renderPage({ surface: "builder" });

    const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
    fireEvent.click(within(toolbar).getByRole("button", { name: "Add step" }));
    const palette = await screen.findByRole("listbox", { name: "Add step" });
    fireEvent.click(within(within(palette).getByRole("group", { name: "Flow" })).getByRole("option", { name: kind === "if" ? "If" : "Switch" }));
    await screen.findByRole("heading", { name: "Step editor" });
    expectBlankRoutingEditorAndFill(kind);
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: label } });
    fireEvent.click(screen.getByRole("button", { name: "Update new step" }));
    expect(postedChangeSets()).toEqual([]);

    mockedFetchWorkflowDetail.mockResolvedValue(appliedDetail);
    mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
      entry: { kind: "step", id: "plan" },
      steps: [
        { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", config_summary: "state.plan", ports: NEXT },
        { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
        { id: rootStepId, step_id: rootStepId, kind, display_name: label, config_summary: "", ports: [] },
      ],
      edges,
    }));
    fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(postedChangeSets()[0]).toEqual({
      expected_revision: 5,
      operations: [{ op: "create_step", step: authored }],
    });
    expect(JSON.stringify(postedChangeSets()[0])).not.toContain("task.mode");
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied"));
    expect(mockedFetchWorkflowDetail.mock.calls.length).toBeGreaterThan(1);

    fireEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument());
    openStep(rootStepId);
    await screen.findByRole("heading", { name: "Step editor" });
    expectReopenedRoutingEditor(kind);
  });

  it("keeps the local set on a revision conflict and applies it again only after an explicit reload", async () => {
    installLinearWorkflow(5);
    serveChanges(
      {
        status: 409,
        body: {
          detail: {
            code: "workflow_revision_conflict",
            message: "The workflow changed after it was loaded. Reload before saving.",
            workflow_id: "weekly_report",
            expected_revision: 5,
          },
        },
      },
      applied(8),
    );
    renderPage({ surface: "builder" });

    await addRootStepFromCanvas("Review draft", "set_variable");
    fireEvent.click(await screen.findByRole("button", { name: "Apply changes" }));

    expect(await screen.findByText(/Your unapplied changes are kept/)).toBeInTheDocument();
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Workflow changed elsewhere");
    // A conflict never retries on its own or overwrites anything.
    expect(postedChangeSets()).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Apply changes" })).toBeDisabled();
    expect(screen.getByRole("group", { name: "Open step Review draft" })).toBeInTheDocument();

    installLinearWorkflow(7);
    fireEvent.click(screen.getByRole("button", { name: "Reload workflow" }));
    await waitFor(() => {
      expect(screen.queryByText(/Your unapplied changes are kept/)).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
    fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));

    await waitFor(() => expect(postedChangeSets()).toHaveLength(2));
    expect(postedChangeSets()[1].expected_revision).toBe(7);
  });

  it("blocks Test Run while structure is unapplied and sends the revision on screen", async () => {
    installLinearWorkflow(5);
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    renderPage({ surface: "builder" });

    await addRootStepFromCanvas("Review draft", "set_variable");
    fireEvent.click(screen.getByRole("link", { name: "Test Run" }));

    const blocked = await screen.findByTestId("workflow-test-run-blocked");
    expect(blocked).toHaveTextContent("Apply or discard the structure changes before running a test.");
    expect(screen.getByRole("button", { name: "Run workflow test run" })).toBeDisabled();

    fireEvent.click(within(screen.getByRole("dialog", { name: "Start a test run" })).getByRole("button", { name: "Cancel" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    fireEvent.click(screen.getByRole("link", { name: "Test Run" }));
    await waitFor(() => {
      expect(screen.queryByTestId("workflow-test-run-blocked")).not.toBeInTheDocument();
    });
    fireEvent.change(screen.getByLabelText("Test Run Summary"), { target: { value: "Try it." } });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Run workflow test run" })).toBeEnabled();
    });
    fireEvent.click(screen.getByRole("button", { name: "Run workflow test run" }));
    await waitFor(() => expect(mockedTestRunWorkflow).toHaveBeenCalled());
    expect(mockedTestRunWorkflow).toHaveBeenCalledWith(expect.objectContaining({ expectedRevision: 5 }));
  });

  it("asks to save a step's parameters before testing it, and never tests a step that is not applied", async () => {
    installLinearWorkflow(5);
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
    renderPage({ surface: "builder" });

    await waitFor(() => expect((globalThis as unknown as Window).editStep).toBeTypeOf("function"));
    openStep("plan");
    const label = await screen.findByRole("textbox", { name: "Step label" });
    expect(screen.getByRole("button", { name: "Save parameters" })).toBeDisabled();
    fireEvent.change(label, { target: { value: "Plan the work" } });
    expect(screen.getByRole("button", { name: "Save parameters" })).toBeEnabled();
    fireEvent.click(screen.getByRole("tab", { name: "Test Run" }));
    expect(await screen.findByTestId("step-test-run-blocked")).toHaveTextContent(
      "Save the step parameters before running a test.",
    );
    expect(screen.getByRole("button", { name: "Run step test run" })).toBeDisabled();

    await addRootStepFromCanvas("Review draft", "set_variable", true);
    fireEvent.click(await screen.findByRole("tab", { name: "Test Run" }));
    expect(await screen.findByTestId("step-test-run-blocked")).toHaveTextContent(
      "This step exists only in the unapplied changes. Apply them before testing it.",
    );
  });

  it("routes each named outcome explicitly and replaces a taken one only after confirmation", async () => {
    const steps = [
      { step_id: "check", display_name: "Check city", step_kind: "if", override: { if: { condition: { selector: "state.city", operator: "truthy" } } } },
      { step_id: "yes", display_name: "City given", step_kind: "stop_success" },
      { step_id: "no", display_name: "City missing", step_kind: "stop_fail" },
    ];
    const edges = [
      { source: { kind: "step", id: "check" }, outcome: { name: "true" }, target: { kind: "step", id: "yes" } },
    ];
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 2, steps, edges }));
    serveChanges(applied(3));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Check city/ }));
    const trueSelect = await screen.findByRole("combobox", { name: "Next step for True" });
    const falseSelect = screen.getByRole("combobox", { name: "Next step for False" });
    expect(trueSelect).toHaveTextContent("City given");
    expect(falseSelect).toHaveTextContent("Not connected");
    // An if step has no generic exit to fall back on.
    expect(screen.queryByRole("combobox", { name: "Next step for Next" })).not.toBeInTheDocument();

    fireEvent.click(falseSelect);
    fireEvent.click(await screen.findByRole("option", { name: "City missing" }));
    fireEvent.click(trueSelect);
    fireEvent.click(await screen.findByRole("option", { name: "City missing" }));
    const dialog = await screen.findByRole("dialog", { name: "Replace this connection?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Replace connection" }));

    fireEvent.click(await screen.findByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(postedChangeSets()[0].operations).toEqual([
      {
        op: "connect",
        edge: { source: { kind: "step", id: "check" }, outcome: { name: "false" }, target: { kind: "step", id: "no" } },
      },
      {
        op: "connect",
        edge: { source: { kind: "step", id: "check" }, outcome: { name: "true" }, target: { kind: "step", id: "no" } },
        replace: true,
      },
    ]);
  });

  it("turns a saved step's kind change into a staged change_step_kind with its dropped connections listed", async () => {
    installLinearWorkflow(5);
    serveChanges(applied(6));
    renderPage({ surface: "builder" });

    await waitFor(() => expect((globalThis as unknown as Window).editStep).toBeTypeOf("function"));
    openStep("plan");
    const kind = await screen.findByRole("combobox", { name: "Step kind" });
    expect(kind).toBeEnabled();
    expect(screen.queryByText("Saving replaces this step's settings with the new kind's defaults.")).not.toBeInTheDocument();
    fireEvent.change(kind, { target: { value: "stop_success" } });
    expect(await screen.findByText("Saving replaces this step's settings with the new kind's defaults.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    // The kind change asks inline, in the editor's footer, naming the connections it removes.
    const confirm = await screen.findByRole("group", { name: "Change this step's kind?" });
    expect(screen.queryByRole("dialog", { name: "Change this step's kind?" })).not.toBeInTheDocument();
    expect(within(confirm).getByText("Removed: plan · success → done.")).toBeInTheDocument();
    // Nothing is written by the kind change itself.
    expect(postedChangeSets()).toEqual([]);
    fireEvent.click(within(confirm).getByRole("button", { name: "Change kind" }));

    await waitFor(() => {
      expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
    });
    await waitFor(() => expect(dryRunChangeSets().length).toBeGreaterThan(0));
    expect(dryRunChangeSets().at(-1)?.operations).toEqual([
      { op: "change_step_kind", step_id: "plan", step: expect.objectContaining({ step_kind: "stop_success" }) },
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    const [operation] = postedChangeSets()[0].operations as Array<Record<string, unknown>>;
    expect(operation).toMatchObject({ op: "change_step_kind", step_id: "plan", step: { step_kind: "stop_success" } });
    expect(operation.step).not.toHaveProperty("step_id");
  });

  it("deletes a step with its connections listed and builds no bypass", async () => {
    const steps = [
      { step_id: "first", display_name: "First", step_kind: "set_variable" },
      { step_id: "middle", display_name: "Middle", step_kind: "set_variable" },
      { step_id: "last", display_name: "Last", step_kind: "stop_success" },
    ];
    const edges = [
      { source: { kind: "step", id: "first" }, outcome: { name: "success" }, target: { kind: "step", id: "middle" } },
      { source: { kind: "step", id: "middle" }, outcome: { name: "success" }, target: { kind: "step", id: "last" } },
    ];
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ enabled: false, revision: 4, steps, edges }));
    const gap = "Outcome success of step first has no target.";
    serveChangesWithDryRun(
      (body) => ({
        status: 200,
        body: {
          workflow_id: "weekly_report",
          revision: body.expected_revision,
          valid: false,
          // A disabled workflow keeps its issues: nothing would refuse Apply.
          issues: [{ code: "workflow.outcome_unrouted", message: gap, path: "workflow.steps.first" }],
          blocking_issues: [],
          applied: false,
          workflow: {},
        },
      }),
      applied(5),
    );
    // This no-query workflow has no linked project; the account-scoped GET
    // reports that context explicitly while the exact /changes dry run remains authoritative.
    serveDeleteReactionGets((url) => new Response(JSON.stringify({
      workflow_id: "weekly_report", project_id: null,
      scope_kind: url.searchParams.get("scope_kind") || "workflow",
      scope_id: url.searchParams.get("scope_id") || "",
      reactions: [], available_trigger_sources: [],
    }), { status: 200, headers: { "content-type": "application/json" } }));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Middle/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete step" }));
    // The deleted step's editor closes rather than becoming a new-step form.
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Step editor" })).not.toBeInTheDocument());

    const bar = await screen.findByTestId("workflow-change-set-bar");
    // The gap it leaves is reported by the server's dry run of the set, not bridged.
    const kept = await within(bar).findByTestId("change-check-kept");
    expect(kept).toHaveTextContent(gap);
    expect(within(bar).queryByTestId("change-check-blocking")).not.toBeInTheDocument();
    expect(dryRunChangeSets().at(-1)).toEqual({
      expected_revision: 4,
      operations: [{ op: "delete_step", step_id: "middle" }],
      dry_run: true,
    });
    fireEvent.click(within(bar).getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(postedChangeSets()[0].operations).toEqual([{ op: "delete_step", step_id: "middle" }]);
  });

  it("creates a For each and an Approval step inside it with an explicit body entry", async () => {
    installLinearWorkflow(5);
    serveChanges(applied(6));
    renderPage({ surface: "builder" });

    const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
    fireEvent.click(within(toolbar).getByRole("button", { name: "Add step" }));
    const palette = await screen.findByRole("listbox", { name: "Add step" });
    fireEvent.click(within(within(palette).getByRole("group", { name: "Flow" })).getByRole("option", { name: "For each" }));
    expect(await screen.findByRole("heading", { name: "For each editor" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "Per item" } });
    fireEvent.change(screen.getByLabelText("Loop source", { exact: true }), { target: { value: "state.items" } });
    fireEvent.click(screen.getByRole("button", { name: "Save For each" }));

    fireEvent.click(await screen.findByRole("button", { name: "Add step for each item" }));
    expect(await screen.findByRole("heading", { name: "Step editor" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Add the new step to" })).toHaveTextContent("Per item");
    fireEvent.click(screen.getByRole("checkbox", { name: /Make it the entry of Per item/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Approve item" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: "approval" } });
    expect(await screen.findByText("Approval request")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox", { name: "Message" }), { target: { value: "Ship it?" } });
    fireEvent.click(screen.getByRole("button", { name: "Add step to changes" }));

    // An Approval routes exactly its two decisions.
    expect(await screen.findByRole("combobox", { name: "Next step for Approved" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Next step for Rejected" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    const operations = postedChangeSets()[0].operations as Array<Record<string, unknown>>;
    expect(operations).toHaveLength(4);
    expect(operations[0]).toEqual({
      op: "create_for_each",
      for_each: {
        for_each_id: "for_each",
        group_id: null,
        display_name: "For each",
        body: { kind: "sequence" },
        over: "",
        item_name: "item",
        index_name: null,
      },
    });
    expect(operations[1]).toEqual({
      op: "update_for_each_config",
      for_each_id: "for_each",
      patch: {
        display_name: "Per item",
        description: "",
        over: "state.items",
        item_name: "item",
        index_name: null,
        body: { kind: "sequence" },
      },
    });
    expect(operations[2]).toEqual({
      op: "create_step",
      step: expect.objectContaining({
        step_id: "approve_item",
        step_kind: "approval",
        group_id: null,
        for_each_id: "for_each",
        lane_id: null,
        override: { approval: { message: "Ship it?", show_outputs: [] } },
      }),
    });
    expect(operations[3]).toEqual({
      op: "set_entry",
      target: { kind: "step", id: "approve_item" },
      for_each_id: "for_each",
    });
  });


  it.each(routingCases)("creates a $kind in a For each body with the explicit entry, then applies and reopens it", async ({ kind, label }) => {
    const { steps, edges } = installLinearWorkflow(5);
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ enabled: false, revision: 5, steps, edges }));
    const bodyStepId = kind === "if" ? "check_plan" : "route_plan";
    const authored = expectedRoutingStep(kind, label, bodyStepId, "for_each");
    const loop = {
      for_each_id: "for_each", group_id: null, display_name: "Per item",
      body: { kind: "sequence", entry: { kind: "step", id: bodyStepId } },
      over: "state.items", item_name: "item", index_name: null,
    };
    const appliedDetail = workflowDetailFixture({ enabled: false, revision: 6, steps: [...steps, authored], edges, for_each: [loop] });
    serveChanges({ status: 200, body: { ...applied(6).body, workflow: appliedDetail } });
    renderPage({ surface: "builder" });

    const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
    fireEvent.click(within(toolbar).getByRole("button", { name: "Add step" }));
    const palette = await screen.findByRole("listbox", { name: "Add step" });
    fireEvent.click(within(within(palette).getByRole("group", { name: "Flow" })).getByRole("option", { name: "For each" }));
    await screen.findByRole("heading", { name: "For each editor" });
    fireEvent.change(screen.getByRole("textbox", { name: "Display name" }), { target: { value: "Per item" } });
    fireEvent.change(screen.getByLabelText("Loop source", { exact: true }), { target: { value: "state.items" } });
    fireEvent.click(screen.getByRole("button", { name: "Save For each" }));

    fireEvent.click(await screen.findByRole("button", { name: "Add step for each item" }));
    await screen.findByRole("heading", { name: "Step editor" });
    expect(screen.getByRole("combobox", { name: "Add the new step to" })).toHaveTextContent("Per item");
    fireEvent.click(screen.getByRole("checkbox", { name: /Make it the entry of Per item/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: label } });
    fireEvent.change(screen.getByRole("combobox", { name: "Step kind" }), { target: { value: kind } });
    expectBlankRoutingEditorAndFill(kind);
    fireEvent.click(screen.getByRole("button", { name: "Add step to changes" }));
    expect(postedChangeSets()).toEqual([]);

    mockedFetchWorkflowDetail.mockResolvedValue(appliedDetail);
    mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
      entry: { kind: "step", id: "plan" }, for_each: [{ ...loop, id: "for_each" }],
      steps: [
        { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", config_summary: "state.plan", ports: NEXT },
        { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
        { id: bodyStepId, step_id: bodyStepId, kind, display_name: label, config_summary: "", ports: [] },
      ],
      edges,
    }));
    fireEvent.click(screen.getByRole("button", { name: "Apply changes" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(postedChangeSets()[0]).toEqual({
      expected_revision: 5,
      operations: [
        {
          op: "create_for_each",
          for_each: { for_each_id: "for_each", group_id: null, display_name: "For each",
            body: { kind: "sequence" }, over: "", item_name: "item", index_name: null },
        },
        {
          op: "update_for_each_config",
          for_each_id: "for_each",
          patch: { display_name: "Per item", description: "", over: "state.items",
            item_name: "item", index_name: null, body: { kind: "sequence" } },
        },
        { op: "create_step", step: authored },
        { op: "set_entry", target: { kind: "step", id: bodyStepId }, for_each_id: "for_each" },
      ],
    });
    expect(JSON.stringify(postedChangeSets()[0])).not.toContain("task.mode");
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied"));
    expect(mockedFetchWorkflowDetail.mock.calls.length).toBeGreaterThan(1);

    fireEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument());
    openStep(bodyStepId);
    await screen.findByRole("heading", { name: "Step editor" });
    expectReopenedRoutingEditor(kind);
  });

  it("keeps a saved Switch selector through an unrelated label edit, Apply, and reopen", async () => {
    const saved = {
      step_id: "route", display_name: "Route by tier", step_kind: "switch",
      override: { switch: { selector: "state.tier", cases: [{ case_id: "gold", when: "gold" }] } },
    };
    const appliedStep = { ...saved, display_name: "Route by tier today" };
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 9, steps: [saved], enabled: false }));
    serveChanges({ status: 200, body: { ...applied(10).body,
      workflow: workflowDetailFixture({ revision: 10, steps: [appliedStep], enabled: false }) } });
    renderPage({ surface: "builder" });
    await waitFor(() => expect((globalThis as unknown as Window).editStep).toBeTypeOf("function"));
    openStep("route");
    await screen.findByRole("heading", { name: "Step editor" });
    expect(screen.getByLabelText("Selector", { exact: true })).toHaveValue("state.tier");
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Route by tier today" } });
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 10, steps: [appliedStep], enabled: false }));
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(postedChangeSets()[0]).toMatchObject({
      expected_revision: 9,
      operations: [{ op: "update_step_config", step_id: "route",
        patch: { display_name: "Route by tier today", override: saved.override } }],
    });
    fireEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument());
    openStep("route");
    await screen.findByRole("heading", { name: "Step editor" });
    expect(screen.getByLabelText("Selector", { exact: true })).toHaveValue("state.tier");
    expect(screen.getByRole("textbox", { name: "Case value 1" })).toHaveValue("gold");
  });

  it("stages added switch cases with the structure instead of saving them directly", async () => {
    const steps = [
      {
        step_id: "route",
        display_name: "Route by tier",
        step_kind: "switch",
        override: { switch: { selector: "state.tier", cases: [{ case_id: "gold", when: "gold" }] } },
      },
    ];
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ enabled: false, revision: 9, steps }));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Route by tier/ }));
    expect(await screen.findByRole("combobox", { name: "Next step for gold" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "+ Add" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Case value 2" }), { target: { value: "silver" } });
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    expect(await screen.findByTestId("workflow-change-set-bar")).toHaveTextContent("Structure not applied: 1 change(s)");
    expect(postedChangeSets()).toEqual([]);
    // The new case is a port to route, named by its stable id.
    expect(await screen.findByRole("combobox", { name: "Next step for silver" })).toBeInTheDocument();
  });

  it("marks scaffold-only workflows as scaffold only instead of valid", async () => {
    mockedFetchWorkflowDetail.mockResolvedValueOnce(
      workflowDetailFixture({
        workflow_id: "scaffold_only_workflow",
        display_name: "Scaffold Only Workflow",
        description: "Unsupported AI result",
        enabled: false,
        steps: [
          { step_id: "workflow_start", display_name: "Start workflow", step_kind: "set_variable" },
          { step_id: "workflow_success", display_name: "Complete workflow", step_kind: "stop_success" },
        ],
      }),
    );
    mockedFetchWorkflowExportPolicy.mockResolvedValueOnce({
      protected: false,
      clone_allowed: true,
      full_export_allowed: true,
      reference_export_allowed: false,
      structure_change_allowed: true,
    } as never);
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
      workflow_id: "scaffold_only_workflow",
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    mockedFetchWorkflowDag.mockResolvedValueOnce({
      workflow_id: "scaffold_only_workflow",
      nodes: [{ id: "step:workflow_start" }, { id: "step:workflow_success" }],
      edges: [{ id: "edge:0", source: "step:workflow_start", target: "step:workflow_success" }],
    } as never);
    mockedFetchWorkflowContract.mockResolvedValueOnce({
      trigger_channels: [],
      expected_inputs: [],
      expected_outputs: [],
    } as never);

    renderPage({
      embedded: false,
      surface: "detail",
      initialEntry: "/registry/workflows/scaffold_only_workflow",
    });

    expect(await screen.findByRole("heading", { name: "Scaffold Only Workflow" })).toBeInTheDocument();
    expect(await screen.findByText("Validation: scaffold only")).toBeInTheDocument();
    expect(screen.queryByText("Validation: valid")).not.toBeInTheDocument();
    expect(screen.getByText("2 steps")).toBeInTheDocument();
  });

  it("groups validation duplicates and opens the affected child workflow mapping", async () => {
    mockedFetchWorkflowDetail.mockResolvedValueOnce(
      workflowDetailFixture({
        display_name: "Weekly report (Copy)",
        enabled: false,
        steps: [
          {
            step_id: "plan_work",
            display_name: "Plan work",
            step_kind: "agent",
            context_inject: { summary_doc: "message.step.analysis.artifacts.summary_doc" },
          },
          {
            step_id: "process_items",
            display_name: "Process items",
            step_kind: "invoke_workflow",
            override: {
              invoke_workflow: {
                workflow_id: "child_workflow",
                input_mapping: {
                  task_summary: "state.task_summary",
                  summary_doc: "message.step.analysis.artifacts.summary_doc",
                },
                output_mapping: {},
              },
            },
          },
        ],
      }),
    );
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
      validation: {
        valid: false,
        errors: [
          {
            code: "workflow.context_inject_source_unavailable",
            input_name: "summary_doc",
            message: "context_inject 'summary_doc' source 'message.step.analysis.artifacts.summary_doc' is not produced by an earlier step",
            path: "workflow.steps.plan_work.context_inject.summary_doc",
            source: "message.step.analysis.artifacts.summary_doc",
            step_id: "plan_work",
          },
          {
            code: "invoke.required_input_unmapped",
            child_workflow_id: "child_workflow",
            input_name: "item_summary",
            message: "Required child input 'item_summary' is not mapped in invoke_workflow.input_mapping",
            path: "workflow.steps.process_items.override.invoke_workflow.input_mapping",
            step_id: "process_items",
          },
          {
            code: "invoke.input_mapping_source_unavailable",
            child_workflow_id: "child_workflow",
            input_name: "summary_doc",
            message: "invoke_workflow input 'summary_doc' maps to 'message.step.analysis.artifacts.summary_doc', but this source is not available from earlier steps",
            path: "workflow.steps.process_items.override.invoke_workflow.input_mapping.summary_doc",
            source: "message.step.analysis.artifacts.summary_doc",
            step_id: "process_items",
          },
        ],
        warnings: [],
      },
    } as never);

    renderPage();

    expect(await screen.findByText("Validation overview")).toBeInTheDocument();
    expect(await screen.findByText("2 grouped issues from 3 checks")).toBeInTheDocument();
    expect(screen.getAllByText("Missing source step: analysis")).toHaveLength(1);
    expect(screen.getByText(/checks grouped/)).toBeInTheDocument();
    const childMapping = screen.getByRole("group", { name: "Child input is not mapped: item_summary" });
    fireEvent.click(within(childMapping).getByRole("button", { name: "Open step mapping" }));
    // The affected step's editor opens itself; no notice stays on screen (Panels › Save buttons).
    expect(await screen.findByRole("textbox", { name: "Step label" })).toHaveValue("Process items");
    expect(screen.getByText("process_items", { exact: true })).toBeVisible();
  });

  it("restores invoke workflow variable autosuggestions from the step's position", async () => {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/meta/callable-workflows")) {
        return new Response(
          JSON.stringify({
            workflows: [
              {
                workflow_id: "child_workflow",
                display_name: "Child workflow",
                expected_inputs: [{ name: "city", type: "string", required: true }],
                expected_outputs: [{ name: "summary", type: "string", required: true }],
                returned_files: [{ name: "brief_pdf", type: "file" }],
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/steps/process_items/available-variables")) {
        return new Response(
          JSON.stringify({
            state: [{ name: "task_summary", type: "text" }],
            dotted_path: [{ name: "project_config.stack.language", type: "text" }],
            prior_outputs: [{ name: "outline", step_id: "analysis", group_id: null, type: "document" }],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({}), { status: 200, headers: { "content-type": "application/json" } });
    });
    mockedFetchWorkflowDetail.mockResolvedValueOnce(
      workflowDetailFixture({
        steps: [
          {
            step_id: "process_items",
            display_name: "Process items",
            step_kind: "invoke_workflow",
            override: {
              invoke_workflow: {
                workflow_id: "child_workflow",
                input_mapping: { city: "state.inputs.city" },
                output_mapping: { summary: "state.item_summary" },
                artifact_mapping: { brief_pdf: "state.item_brief_pdf" },
              },
            },
          },
        ],
      }),
    );

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: /Process items/i }));

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/workflows/weekly_report/steps/process_items/available-variables",
        expect.objectContaining({ cache: "no-store" }),
      );
    });
    await waitFor(() => {
      expect(screen.getByDisplayValue("state.inputs.city")).toHaveAttribute(
        "aria-controls",
        expect.stringContaining("workflow-variable-suggestions-input_mapping-city"),
      );
    });
    const cityInput = screen.getByDisplayValue("state.inputs.city");
    fireEvent.focus(cityInput);
    const listId = firstStringFromUnknownValue([cityInput.getAttribute("aria-controls"), ""]);
    const suggestionPanel = await screen.findByTestId(listId);
    expect(suggestionPanel).toHaveTextContent("Advanced values");
    expect(suggestionPanel).toHaveTextContent("Workflow variables");
    expect(suggestionPanel).toHaveTextContent("Previous steps");
    expect(suggestionPanel).toHaveTextContent("analysis");
    expect(suggestionPanel).toHaveTextContent("State aliases");
    expect(suggestionPanel).toHaveTextContent("Step artifacts");
    fireEvent.mouseDown(within(suggestionPanel).getByRole("button", { name: /Advanced values/ }));
    expect(suggestionPanel).toHaveTextContent("state.task_summary");
    expect(suggestionPanel).toHaveTextContent("state.outline");
    expect(suggestionPanel).toHaveTextContent("message.step.analysis.artifacts.outline");
  });

  it("offers a step added after a saved step what runs after that step, its outputs included", async () => {
    const steps = [
      { step_id: "plan", display_name: "Plan work", step_kind: "set_variable", override: { set_variable: { assignments: { "state.plan": true } } } },
      { step_id: "done", display_name: "Done", step_kind: "stop_success" },
    ];
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps, edges: [] }));
    mockedFetchWorkflowCanvasGraph.mockResolvedValue(
      canvasGraphFixture({
        entry: { kind: "step", id: "plan" },
        steps: [
          { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", config_summary: "state.plan", ports: NEXT },
          { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
        ],
        edges: [],
      }),
    );
    const fallback = fetchSpy.getMockImplementation();
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/available-variables")) {
        const after = url.endsWith("/steps/plan/available-variables?position=after");
        return new Response(
          JSON.stringify({
            state: [{ name: "task_summary", type: "text" }],
            dotted_path: [],
            prior_outputs: after ? [{ name: "plan", step_id: "plan", group_id: null, type: "boolean" }] : [],
            loop: [],
            position: after ? "after" : "unknown",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return fallback ? fallback(input, init) : new Response("{}");
    });
    renderPage({ surface: "builder" });

    fireEvent.click(await screen.findByRole("button", { name: "Add a step after Plan work · Next, not connected" }));
    const palette = await screen.findByRole("dialog", { name: "Add after Plan work · Next" });
    fireEvent.click(within(palette).getByRole("option", { name: "If" }));
    await screen.findByRole("heading", { name: "Step editor" });
    expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("if");
    const selector = await screen.findByRole("combobox", { name: "Selector" });
    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/workflows/weekly_report/steps/plan/available-variables?position=after",
        expect.objectContaining({ cache: "no-store" }),
      ),
    );
    fireEvent.focus(selector);
    const panel = await screen.findByTestId(firstStringFromUnknownValue([selector.getAttribute("aria-controls"), ""]));
    await waitFor(() => expect(panel).toHaveTextContent("state.plan"));
    expect(panel).toHaveTextContent("Advanced values");
    fireEvent.mouseDown(within(panel).getByRole("button", { name: /Advanced values/ }));
    expect(panel).toHaveTextContent("state.task_summary");
    expect(within(panel).queryByTestId("workflow-variables-position-unknown")).not.toBeInTheDocument();
  });
});


describe("Canvas controls beside the contextual drawer", () => {
  it("follows the real drawer width through open, keyboard resize and close", async () => {
    installLinearWorkflow();
    renderPage({ surface: "builder" });
    const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
    const controls = screen.getByTestId("workflow-canvas-controls");
    expect(controls).toHaveStyle({ right: "0px" });

    openStep("plan");
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    expect(drawer).toHaveStyle({ width: "450px" });
    expect(controls).toHaveStyle({ right: drawer.style.width });

    const resize = within(drawer).getByRole("button", { name: "Resize builder inspector" });
    fireEvent.keyDown(resize, { key: "ArrowLeft" });
    await waitFor(() => expect(drawer).toHaveStyle({ width: "474px" }));
    expect(controls).toHaveStyle({ right: drawer.style.width });
    fireEvent.keyDown(resize, { key: "ArrowRight" });
    await waitFor(() => expect(drawer).toHaveStyle({ width: "450px" }));
    expect(controls).toHaveStyle({ right: drawer.style.width });

    expect(within(toolbar).getByRole("link", { name: "Test Run" })).toBeInTheDocument();
    expect(within(toolbar).getByRole("button", { name: "Zoom out (−)" })).toBeEnabled();
    expect(within(toolbar).getByRole("button", { name: "Zoom in (+)" })).toBeEnabled();
    expect(within(toolbar).getByRole("button", { name: "Fit to screen (Shift+1)" })).toBeEnabled();
    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument());
    expect(controls).toHaveStyle({ right: "0px" });
    expect(within(toolbar).getByRole("link", { name: "Test Run" })).toBeInTheDocument();
    expect(postedChangeSets()).toEqual([]);
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  });
});


it("keeps actual ordinary-step marks updating with Results closed and resets them for a new job", async () => {
  installLinearWorkflow(5);
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  const firstJob = "ordinary-runtime-1";
  const secondJob = "ordinary-runtime-2";
  let finished = false;
  const run = (jobId: string, terminal: boolean) => ({
    workflow_id: "weekly_report", project_id: "project_alpha", job_id: jobId, thread_id: `thread-${jobId}`,
    status: terminal ? "completed" : "in_progress", final_status: terminal ? "success" : "in_progress", needs_user_approval: false,
  });
  mockedTestRunWorkflow.mockResolvedValueOnce(run(firstJob, false)).mockResolvedValueOnce(run(secondJob, false));
  mockedFetchWorkflowTestRunState.mockImplementation(async (threadId) => run(threadId === `thread-${secondJob}` ? secondJob : firstJob, threadId === `thread-${firstJob}` && finished));
  const fallback = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const match = /^\/api(?:\/v2)?\/jobs\/(ordinary-runtime-[12])(?:\/(.*))?$/.exec(url.pathname);
    if (!match) return fallback ? fallback(input, init) : new Response("{}");
    const jobId = match[1];
    const terminal = jobId === firstJob && finished;
    const first = jobId === firstJob;
    const started = "2026-10-18T12:24:00.000Z";
    const plan = {
      step_id: "plan", step_execution_id: `${jobId}-plan`, occurrence: 1, attempt: 1, title: "Plan work",
      status: first ? "completed" : "in_progress", started_at: started,
      finished_at: first ? "2026-10-18T12:24:00.033Z" : null, duration_ms: first ? 33 : null,
    };
    let payload: unknown;
    if (match[2] === "definition") payload = {
      job_id: jobId, available: true, workflow_id: "weekly_report", revision: 5, digest: "archive-revision-five", schema_version: 1,
      steps: [{ step_id: "plan", display_name: "Plan work", step_kind: "set_variable" }, { step_id: "done", display_name: "Done", step_kind: "stop_success" }],
      groups: [], for_each: [], execution_graph: { schema_version: 1, entry: { kind: "step", id: "plan" }, edges: [{ source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } }] },
    };
    else if (match[2] === "execution-steps") payload = {
      job_id: jobId, run_terminal: terminal, child_invocations: [],
      steps: first ? [plan, { step_id: "done", step_execution_id: `${jobId}-done`, occurrence: 1, attempt: 1, title: "Done",
        status: terminal ? "completed" : "in_progress", started_at: "2026-10-18T12:24:00.033Z",
        finished_at: terminal ? "2026-10-18T12:24:00.033Z" : null, duration_ms: terminal ? 0 : null }] : [plan],
      page: { count: first ? 2 : 1, has_more: false, next_cursor: "" },
    };
    else if (!match[2]) payload = {
      id: jobId, status: terminal ? "completed" : "in_progress", selected_workflow: "weekly_report", project_id: "project_alpha",
      current_step_id: first ? "done" : "plan", started_at: started, finished_at: terminal ? "2026-10-18T12:24:00.033Z" : null,
      execution_steps: [], task_summary: "Run ordinary workflow from the builder.",
    };
    else if (match[2] === "cost-breakdown") payload = { breakdown: [], total_cost_usd: 0 };
    else if (match[2] === "approval-history") payload = { job_id: jobId, approvals: [] };
    else if (match[2] === "decision-overrides") payload = { job_id: jobId, overrides: [] };
    else payload = { job_id: jobId };
    return new Response(JSON.stringify(payload), { status: 200, headers: { "content-type": "application/json" } });
  });
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  const planBeforeRun = await screen.findByRole("group", { name: "Open step Plan work" });
  const doneBeforeRun = screen.getByRole("group", { name: "Open step Done" });
  const canvasTools = screen.getByRole("toolbar", { name: "Canvas tools" });
  fireEvent.click(within(canvasTools).getByRole("link", { name: "Test Run" }));
  fireEvent.click(await screen.findByRole("button", { name: "Run workflow test run" }));
  await screen.findByTestId("workflow-test-run-results");
  await waitFor(() => expect(within(planBeforeRun).getByTestId("workflow-runtime-mark")).toHaveAttribute("data-run", "done"));
  const firstPlanMark = within(planBeforeRun).getByTestId("workflow-runtime-mark");
  expect(firstPlanMark).toHaveAttribute("data-job-id", firstJob);
  // 33 ms recorded: the mark says the time, rounded as the design says it.
  expect(firstPlanMark).toHaveTextContent("< 0.1 s");
  fireEvent.click(within(screen.getByRole("dialog", { name: "Test Run results" })).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument());
  finished = true;
  await waitFor(() => {
    const mark = within(doneBeforeRun).getByTestId("workflow-runtime-mark");
    expect(mark).toHaveAttribute("data-run", "done");
    expect(within(mark).getByText("< 0.1 s", { exact: true })).toBeVisible();
  }, { timeout: 5_000 });
  const terminalDoneMark = within(doneBeforeRun).getByTestId("workflow-runtime-mark");
  expect(terminalDoneMark).toHaveAttribute("data-job-id", firstJob);
  expect(within(terminalDoneMark).getByText("< 0.1 s", { exact: true })).toBeVisible();
  expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();

  // Execution pages poll independently of compact JobDetail. View Results is
  // the same guarded existing-run route whether JobDetail has caught up or not.
  fireEvent.click(within(await selectedRunBar(firstJob)).getByRole("button", { name: "Results" }));
  const completedResults = await screen.findByTestId("workflow-test-run-results");
  expect(completedResults).toHaveAttribute("data-job-id", firstJob);
  expect(await within(completedResults).findByRole("heading", { name: /^Latest test run/ })).toHaveTextContent("Finished");
  expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
  fireEvent.click(within(screen.getByRole("dialog", { name: "Test Run results" })).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument());

  fireEvent.click(await within(canvasTools).findByRole("link", { name: "Test Run" }));
  const submitAgain = await screen.findByRole("button", { name: "Run workflow test run" });
  await waitFor(() => expect(submitAgain).toBeEnabled());
  fireEvent.click(submitAgain);
  await waitFor(() => expect(within(planBeforeRun).getByTestId("workflow-runtime-mark")).toHaveAttribute("data-job-id", secondJob));
  const secondPlanMark = within(planBeforeRun).getByTestId("workflow-runtime-mark");
  expect(secondPlanMark).toHaveAttribute("data-job-id", secondJob);
  expect(secondPlanMark).toHaveTextContent(/in progress|running/i);
  expect(secondPlanMark).not.toHaveTextContent(/< 0\.1 s/);
  expect(within(doneBeforeRun).queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open full Job Details" })).toHaveAttribute("href", `/jobs/${secondJob}`);
  expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(2);
});


describe("contextual Play uses the existing isolated Step Test Run", () => {
  function clickCanvasCard(card: HTMLElement) {
    // Vitest aliases window/defaultView to its global; d3 needs the existing JSDOM Window.
    const nativeRealm = (globalThis as typeof globalThis & { jsdom?: { window: Window } }).jsdom?.window;
    if (!nativeRealm) throw new Error("Selected-card mouse fixture requires Vitest's existing JSDOM Window.");
    fireEvent.mouseDown(card, { view: nativeRealm, button: 0, buttons: 1 });
    fireEvent.mouseUp(card, { view: nativeRealm, button: 0, buttons: 0 });
    fireEvent.click(card, { view: nativeRealm, button: 0, buttons: 0 });
  }
  async function playFor(name: string) {
    const user = userEvent.setup();
    const card = await screen.findByRole("group", { name: `Open step ${name}` });
    await user.hover(card);
    return within(card).findByRole("button", { name: "Test step" });
  }

  function permitStepPlay() {
    mockedFetchWorkflowExportPolicy.mockResolvedValue({ workflow_id: "weekly_report", structure_change_allowed: true, step_test_run_allowed: true } as never);
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  }

  it("opens the author step tab without submitting and runs only through its existing explicit Run action", async () => {
    installLinearWorkflow(5);
    permitStepPlay();
    mockedTestRunWorkflowStep.mockResolvedValue({ workflow_id: "weekly_report", step_id: "plan", step_kind: "set_variable", status: "completed", final_status: "success" } as never);
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
    fireEvent.click(await playFor("Plan work"));
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    expect(within(drawer).getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
    // The drawer's subtitle names the step it edits.
    expect(within(drawer).getByText("plan", { exact: true })).toBeVisible();
    expect(screen.queryByRole("dialog", { name: /^Start a test run$/ })).not.toBeInTheDocument();
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(postedChangeSets()).toEqual([]);
    fireEvent.change(within(drawer).getByRole("textbox", { name: "Step Test Run Summary" }), { target: { value: "Verify the plan step only." } });
    const run = within(drawer).getByRole("button", { name: "Run step test run" });
    await waitFor(() => expect(run).toBeEnabled());
    fireEvent.click(run);
    await waitFor(() => expect(mockedTestRunWorkflowStep).toHaveBeenCalledTimes(1));
    expect(mockedTestRunWorkflowStep).toHaveBeenCalledWith({ workflowId: "weekly_report", expectedRevision: 5, stepId: "plan", projectId: "project_alpha", taskSummary: "Verify the plan step only.", inputs: {}, inputBindings: [] });
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(postedChangeSets()).toEqual([]);
  });

  it("opens terminal Play on its isolated tab while ordinary terminal opening still targets Interface Outputs", async () => {
    installLinearWorkflow(5);
    permitStepPlay();
    const user = userEvent.setup();
    renderPage({ surface: "builder" });
    await user.click(await playFor("Done"));
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    expect(within(drawer).getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
    // The drawer's subtitle names the step it edits.
    expect(within(drawer).getByText("done", { exact: true })).toBeVisible();
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    await user.click(within(drawer).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument());
    clickCanvasCard(screen.getByRole("group", { name: "Open step Done" }));
    const contract = await screen.findByRole("dialog", { name: "Workflow interface" });
    expect(within(contract).getByRole("tab", { name: "Outputs" })).toHaveAttribute("aria-selected", "true");
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  });

  it("keeps a different step's unsaved parameters and router state when contextual navigation is cancelled", async () => {
    installLinearWorkflow(5);
    permitStepPlay();
    const user = userEvent.setup();
    const { router } = renderPage({ surface: "builder" });
    clickCanvasCard(await screen.findByRole("group", { name: "Open step Plan work" }));
    const label = await screen.findByRole("textbox", { name: "Step label" });
    fireEvent.change(label, { target: { value: "Unfinished plan label" } });
    const before = router.state.location;
    const play = await playFor("Done");
    await user.click(play);
    const confirmation = await screen.findByRole("dialog", { name: "Discard unsaved parameters?" });
    await user.click(within(confirmation).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Discard unsaved parameters?" })).not.toBeInTheDocument());
    expect(label).toBeInTheDocument();
    expect(label).toHaveValue("Unfinished plan label");
    expect(screen.getByRole("tab", { name: "Smart setup" })).toHaveAttribute("aria-selected", "true");
    expect(router.state.location).toEqual(before);
    expect(play).toHaveFocus();
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(postedChangeSets()).toEqual([]);
  });

  it("lets the same dirty saved step inspect its test tab without saving or running its parameters", async () => {
    installLinearWorkflow(5);
    permitStepPlay();
    const user = userEvent.setup();
    renderPage({ surface: "builder" });
    clickCanvasCard(await screen.findByRole("group", { name: "Open step Plan work" }));
    fireEvent.change(await screen.findByRole("textbox", { name: "Step label" }), { target: { value: "Unfinished plan label" } });
    await user.click(await playFor("Plan work"));
    expect(await screen.findByTestId("step-test-run-blocked")).toHaveTextContent("Save the step parameters before running a test.");
    expect(screen.getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: "Run step test run" })).toBeDisabled();
    await user.click(screen.getByRole("tab", { name: "Smart setup" }));
    expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Unfinished plan label");
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(postedChangeSets()).toEqual([]);
  });

  it("shows the existing not-applied refusal when Play opens a newly staged step", async () => {
    installLinearWorkflow(5);
    permitStepPlay();
    renderPage({ surface: "builder" });
    await addRootStepFromCanvas("Unapplied draft", "set_variable");
    fireEvent.click(await playFor("Unapplied draft"));
    expect(await screen.findByTestId("step-test-run-blocked")).toHaveTextContent("This step exists only in the unapplied changes. Apply them before testing it.");
    expect(screen.getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: "Run step test run" })).toBeDisabled();
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(postedChangeSets()).toEqual([]);
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)");
  });

  it("preserves the same step's invalid Rows control and active tab when the tab-leave guard is cancelled", async () => {
    permitStepPlay();
    mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps: [{ step_id: "feed", display_name: "Feed rows", step_kind: "tabular_feed", override: { tabular_feed: { operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.feed_dataset" } } }] }));
    mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ entry: { kind: "step", id: "feed" }, steps: [{ id: "feed", step_id: "feed", display_name: "Feed rows", kind: "tabular_feed", config_summary: "Create dataset", ports: NEXT }] }));
    const user = userEvent.setup();
    const { router } = renderPage({ surface: "builder" });
    clickCanvasCard(await screen.findByRole("group", { name: "Open step Feed rows" }));
    const rows = await screen.findByRole("textbox", { name: "Rows" });
    fireEvent.change(rows, { target: { value: "[{" } });
    await user.click(screen.getByRole("button", { name: "Save parameters" }));
    const before = router.state.location;
    const play = await playFor("Feed rows");
    await user.click(play);
    const confirmation = await screen.findByRole("dialog", { name: "Discard invalid input?" });
    await user.click(within(confirmation).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Discard invalid input?" })).not.toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Rows" })).toBe(rows);
    expect(rows).toHaveValue("[{");
    expect(rows).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("tab", { name: "Smart setup" })).toHaveAttribute("aria-selected", "true");
    expect(router.state.location).toEqual(before);
    expect(play).toHaveFocus();
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(postedChangeSets()).toEqual([]);
  });
});

/** The actual Automation GET boundary, kept distinct from the canvas summary. */
function serveDeleteReactionGets(reply: (url: URL) => Response | Promise<Response>) {
  const previous = fetchSpy.getMockImplementation();
  const requests: URL[] = [];
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(stringFromUnknownValue(input), "http://localhost");
    if (url.pathname.endsWith("/reactions") && (!init?.method || init.method === "GET")) {
      requests.push(url);
      return reply(url);
    }
    return previous ? previous(input, init) : new Response("{}");
  });
  return requests;
}

function stepRuleReply(url: URL, reactions: Array<Record<string, unknown>>, status = 200) {
  return new Response(JSON.stringify({
    workflow_id: "weekly_report",
    project_id: "project_alpha",
    scope_kind: url.searchParams.get("scope_kind") || "workflow",
    scope_id: url.searchParams.get("scope_id") || "",
    reactions,
    available_trigger_sources: [],
  }), { status, headers: { "content-type": "application/json" } });
}

function serveProtectedPlanDryRuns() {
  serveChangesWithDryRun((body) => ({
    status: 200,
    body: {
      workflow_id: "weekly_report",
      revision: body.expected_revision,
      valid: false,
      applied: false,
      issues: [],
      blocking_issues: [{ code: "workflow.reaction.scope_missing", path: "reactions.scope::step::plan::protect_plan", message: "A saved rule protects step plan.", severity: "error" }],
      workflow: {},
    },
  }));
}

function expectOnlyPlanDeleteDryRuns() {
  for (const body of dryRunChangeSets()) {
    expect(body.expected_revision).toBe(5);
    expect(body.dry_run).toBe(true);
    expect(body.operations).toEqual([{ op: "delete_step", step_id: "plan" }]);
  }
}

it("refuses editor Delete before staging when a saved disabled step Automation rule exists", async () => {
  installLinearWorkflow();
  serveProtectedPlanDryRuns();
  const requests = serveDeleteReactionGets((url) => stepRuleReply(url,
    url.searchParams.get("scope_kind") === "step" && url.searchParams.get("scope_id") === "plan"
      ? [{ reaction_key: "protect_plan", display_name: "Guard plan", event_type: "job.step_failed", conditions: {}, action: { type: "notify" }, enabled: false, scope_kind: "step", scope_id: "plan" }]
      : [],
  ));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });

  fireEvent.click(await screen.findByRole("group", { name: "Open step Plan work" }));
  expect(await screen.findByRole("heading", { name: "Step editor" })).toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "Delete step" }));

  expect(await screen.findByText(/Guard plan/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open Automation" })).toBeInTheDocument();
  expect(requests.some((url) => url.pathname === "/api/workflows/weekly_report/reactions")).toBe(true);
  expect(requests.some((url) => url.searchParams.get("project_id") === "project_alpha"
    && url.searchParams.get("scope_kind") === "step"
    && url.searchParams.get("scope_id") === "plan")).toBe(true);
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expectOnlyPlanDeleteDryRuns();
  expect(postedChangeSets()).toEqual([]);
  expect(screen.getByRole("heading", { name: "Step editor" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Open Automation" }));
  await waitFor(() => expect(screen.getByRole("tab", { name: "Automation" })).toHaveAttribute("aria-selected", "true"));
});

it("keeps editor Delete unstaged while Automation is loading and after a failed read, then retries", async () => {
  installLinearWorkflow();
  serveProtectedPlanDryRuns();
  let finishFirst: ((response: Response) => void) | undefined;
  const first = new Promise<Response>((resolve) => { finishFirst = resolve; });
  let attempt = 0;
  const requests = serveDeleteReactionGets((url) => {
    if (url.searchParams.get("scope_kind") !== "step" || url.searchParams.get("scope_id") !== "plan") {
      return stepRuleReply(url, []);
    }
    attempt += 1;
    if (attempt === 1) return first;
    return stepRuleReply(url, [{ reaction_key: "protect_plan", display_name: "Guard plan", event_type: "job.step_failed", conditions: {}, action: { type: "notify" }, enabled: false, scope_kind: "step", scope_id: "plan" }]);
  });
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });

  fireEvent.click(await screen.findByRole("group", { name: "Open step Plan work" }));
  expect(await screen.findByRole("heading", { name: "Step editor" })).toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "Delete step" }));
  await waitFor(() => expect(requests.some((url) => url.searchParams.get("scope_id") === "plan")).toBe(true));
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expectOnlyPlanDeleteDryRuns();
  await act(async () => {
    finishFirst?.(new Response(JSON.stringify({ detail: "Automation unavailable" }), { status: 503, headers: { "content-type": "application/json" } }));
  });
  const retry = await screen.findByRole("button", { name: "Retry" });
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expectOnlyPlanDeleteDryRuns();
  fireEvent.click(retry);
  await waitFor(() => expect(attempt).toBe(2));
  expect(await screen.findByText(/Guard plan/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open Automation" })).toBeInTheDocument();
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expectOnlyPlanDeleteDryRuns();
  expect(postedChangeSets()).toEqual([]);
});

it("uses the exact-operation dry run to refuse a foreign-account saved rule without exposing its key", async () => {
  installLinearWorkflow(5);
  const foreignKey = "scope::step::plan::other_account_secret";
  serveChangesWithDryRun((body) => ({
    status: 200,
    body: {
      workflow_id: "weekly_report",
      revision: body.expected_revision,
      valid: false,
      applied: false,
      issues: [],
      blocking_issues: [{
        code: "workflow.reaction.scope_missing",
        path: `reactions.${foreignKey}`,
        message: `Reaction '${foreignKey}' refers to missing step scope 'plan'.`,
        severity: "error",
      }],
      workflow: {},
    },
  }));
  const ownAccountGets = serveDeleteReactionGets((url) => stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });

  fireEvent.click(await screen.findByRole("group", { name: "Open step Plan work" }));
  expect(await screen.findByRole("heading", { name: "Step editor" })).toBeInTheDocument();
  fireEvent.click(await screen.findByRole("button", { name: "Delete step" }));

  expect(await screen.findByText(/saved Automation rule/i)).toBeInTheDocument();
  expect(ownAccountGets.some((url) => url.pathname === "/api/workflows/weekly_report/reactions"
    && url.searchParams.get("project_id") === "project_alpha"
    && url.searchParams.get("scope_kind") === "step"
    && url.searchParams.get("scope_id") === "plan")).toBe(true);
  await waitFor(() => expect(dryRunChangeSets()).toContainEqual(expect.objectContaining({
    expected_revision: 5,
    dry_run: true,
    operations: [{ op: "delete_step", step_id: "plan" }],
  })));
  expect(screen.queryByText(/other_account_secret/)).not.toBeInTheDocument();
  expect(screen.queryByText(/scope::step::plan/)).not.toBeInTheDocument();
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expect(postedChangeSets()).toEqual([]);
  expect(screen.getByRole("heading", { name: "Step editor" })).toBeInTheDocument();
});


it.each(["workflow.connection_required", "workflow.connection_unavailable", "workflow.execution_bindings_missing", "workflow.graph.outcome_missing"])("keeps an exact guarded Delete as one editable local draft when %s blocks Apply", async (code) => {
  installLinearWorkflow(5);
  const unrelated = { code, path: "workflow.steps.done", message: "A connection is unavailable for a later run.", severity: "error" };
  serveChangesWithDryRun((body) => ({ status: 200, body: {
    workflow_id: "weekly_report", revision: body.expected_revision, valid: false, applied: false,
    issues: [unrelated], blocking_issues: [unrelated], workflow: {},
  } }));
  const gets = serveDeleteReactionGets((url) => stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  fireEvent.click(await screen.findByRole("group", { name: "Open step Plan work" }));
  fireEvent.click(await screen.findByRole("button", { name: "Delete step" }));

  await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)"));
  expect(gets.some((url) => url.pathname === "/api/workflows/weekly_report/reactions"
    && url.searchParams.get("project_id") === "project_alpha"
    && url.searchParams.get("scope_kind") === "step"
    && url.searchParams.get("scope_id") === "plan")).toBe(true);
  const exact = dryRunChangeSets().filter((body) => JSON.stringify(body.operations) === JSON.stringify([{ op: "delete_step", step_id: "plan" }]));
  expect(exact).toHaveLength(1);
  expect(exact[0]).toMatchObject({ expected_revision: 5, dry_run: true });
  expect(postedChangeSets()).toEqual([]);
  expect(screen.queryByRole("group", { name: "Open step Plan work" })).not.toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Open step Done" })).toBeInTheDocument();
});

it.each(["malformed_get", "failed_get", "malformed_dry_run", "failed_dry_run", "stale_revision"])(
  "fails closed on %s while checking a Delete, without staging",
  async (caseName) => {
    installLinearWorkflow(5);
    serveChangesWithDryRun((body) => {
      if (caseName === "failed_dry_run") return { status: 503, body: { detail: "Changes unavailable" } };
      if (caseName === "stale_revision") return { status: 409, body: { detail: { code: "workflow_revision_conflict", message: "Workflow changed." } } };
      if (caseName === "malformed_dry_run") return { status: 200, body: { workflow_id: "weekly_report", revision: body.expected_revision, blocking_issues: "not-an-array" } };
      return cleanDryRun(body);
    });
    const gets = serveDeleteReactionGets((url) => {
      if (caseName === "failed_get") return new Response(JSON.stringify({ detail: "Automation unavailable" }), { status: 503, headers: { "content-type": "application/json" } });
      if (caseName === "malformed_get") return new Response(JSON.stringify({ workflow_id: "weekly_report", project_id: "project_alpha", scope_kind: "step", scope_id: "plan", reactions: {} }), { status: 200, headers: { "content-type": "application/json" } });
      return stepRuleReply(url, []);
    });
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
    fireEvent.click(await screen.findByRole("group", { name: "Open step Plan work" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete step" }));

    expect(await screen.findByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(gets.some((url) => url.pathname === "/api/workflows/weekly_report/reactions"
      && url.searchParams.get("project_id") === "project_alpha"
      && url.searchParams.get("scope_kind") === "step"
      && url.searchParams.get("scope_id") === "plan")).toBe(true);
    if (!caseName.endsWith("_get")) {
      await waitFor(() => expect(dryRunChangeSets()).not.toHaveLength(0));
    }
    expectOnlyPlanDeleteDryRuns();
    expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
    expect(screen.getByRole("heading", { name: "Step editor" })).toBeInTheDocument();
    expect(postedChangeSets()).toEqual([]);
  },
);

it("drops a held exact Delete dry-run after the selected project changes", async () => {
  installLinearWorkflow(5);
  const previous = fetchSpy.getMockImplementation();
  const scopedGets: URL[] = [];
  const heldBodies: Array<Record<string, unknown>> = [];
  let releaseDryRun: ((response: Response) => void) | undefined;
  const held = new Promise<Response>((resolve) => { releaseDryRun = resolve; });
  let parsedDryRun: (() => void) | undefined;
  const parsed = new Promise<void>((resolve) => { parsedDryRun = resolve; });
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(stringFromUnknownValue(input), "http://localhost");
    if (url.pathname === "/api/workflows/weekly_report/reactions" && (!init?.method || init.method === "GET")) {
      scopedGets.push(url);
      return stepRuleReply(url, []);
    }
    if (url.pathname === "/api/workflows/weekly_report/changes" && init?.method === "POST") {
      const body = JSON.parse(String(init.body || "{}")) as Record<string, unknown>;
      if (body.dry_run === true) { heldBodies.push(body); return held; }
    }
    return previous ? previous(input, init) : new Response("{}");
  });
  const view = renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  fireEvent.click(await screen.findByRole("group", { name: "Open step Plan work" }));
  fireEvent.click(await screen.findByRole("button", { name: "Delete step" }));
  await waitFor(() => expect(heldBodies).toHaveLength(1));
  expect(heldBodies[0]).toMatchObject({ expected_revision: 5, dry_run: true, operations: [{ op: "delete_step", step_id: "plan" }] });
  expect(scopedGets.some((url) => url.searchParams.get("project_id") === "project_alpha"
    && url.searchParams.get("scope_kind") === "step" && url.searchParams.get("scope_id") === "plan")).toBe(true);
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");

  await act(async () => { await view.router.navigate("/registry/workflows/weekly_report/builder?project_id=project_beta"); });
  expect(view.router.state.location.search).toContain("project_id=project_beta");
  const response = new Response(JSON.stringify({ workflow_id: "weekly_report", revision: 5, valid: true, applied: false, issues: [], blocking_issues: [], workflow: {} }), { status: 200, headers: { "content-type": "application/json" } });
  const originalJson = response.json.bind(response);
  response.json = async () => { const body = await originalJson(); parsedDryRun?.(); return body; };
  await act(async () => { releaseDryRun?.(response); await parsed; });
  expect(postedChangeSets()).toEqual([]);
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
});

/** Public selected-card ingress, with the same saved-rule fence as editor Delete. */
it("routes selected Step More Delete through the exact saved-rule guard", async () => {
  installLinearWorkflow(5);
  serveProtectedPlanDryRuns();
  const gets = serveDeleteReactionGets((url) => stepRuleReply(url,
    url.searchParams.get("scope_kind") === "step" && url.searchParams.get("scope_id") === "plan"
      ? [{ reaction_key: "protect_plan", display_name: "Guard plan", event_type: "job.step_failed", conditions: {}, action: { type: "notify" }, enabled: false, scope_kind: "step", scope_id: "plan" }]
      : [],
  ));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  fireEvent.click(await screen.findByRole("group", { name: "Open step Plan work" }));
  const editor = await screen.findByRole("dialog", { name: "Step editor" });
  fireEvent.click(within(editor).getByRole("button", { name: "Close" }));
  fireEvent.click(await screen.findByRole("button", { name: "More actions for Plan work" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));

  expect(await screen.findByText(/Guard plan/)).toBeInTheDocument();
  expect(gets.some((url) => url.pathname === "/api/workflows/weekly_report/reactions"
    && url.searchParams.get("project_id") === "project_alpha"
    && url.searchParams.get("scope_kind") === "step"
    && url.searchParams.get("scope_id") === "plan")).toBe(true);
  expectOnlyPlanDeleteDryRuns();
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expect(postedChangeSets()).toEqual([]);
  expect(screen.getByRole("group", { name: "Open step Plan work" })).toBeInTheDocument();
});

/** Canvas key ingress must own native React Flow removal once, and ignore editing focus. */
it.each(["Delete", "Backspace"])("routes one canvas-targeted %s to one guarded local Step Delete", async (key) => {
  installLinearWorkflow(5);
  serveChangesWithDryRun(cleanDryRun);
  let releaseGuardGet: ((response: Response) => void) | undefined;
  const heldGuardGet = new Promise<Response>((resolve) => { releaseGuardGet = resolve; });
  const gets = serveDeleteReactionGets((url) =>
    url.searchParams.get("scope_kind") === "step" && url.searchParams.get("scope_id") === "plan"
      ? heldGuardGet
      : stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  const card = await screen.findByRole("group", { name: "Open step Plan work" });
  fireEvent.click(card);
  const editor = await screen.findByRole("dialog", { name: "Step editor" });
  const label = within(editor).getByRole("textbox", { name: "Step label" });
  fireEvent.keyDown(label, { key, code: key });
  expect(screen.queryByRole("dialog", { name: "Delete this step?" })).not.toBeInTheDocument();
  expect(dryRunChangeSets()).toEqual([]);
  fireEvent.click(within(editor).getByRole("button", { name: "Close" }));
  const node = canvasNodeForStep("Plan work");
  expect(node).not.toBeNull();
  if (!node) throw new Error("Selected React Flow node was not rendered");
  node.focus();
  expect(node).toHaveFocus();
  fireEvent.keyDown(node, { key, code: key });

  // Before any local stage, exactly one guard request is held. Later the
  // change-set controller may legitimately validate the staged draft again.
  await waitFor(() => expect(gets.filter((url) => url.pathname === "/api/workflows/weekly_report/reactions"
    && url.searchParams.get("scope_kind") === "step" && url.searchParams.get("scope_id") === "plan")).toHaveLength(1));
  expect(dryRunChangeSets()).toEqual([]);
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  await act(async () => { releaseGuardGet?.(stepRuleReply(gets.at(-1)!, [])); });
  await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)"));
  expect(dryRunChangeSets()).toContainEqual(expect.objectContaining({
    expected_revision: 5, dry_run: true, operations: [{ op: "delete_step", step_id: "plan" }],
  }));
  expect(postedChangeSets()).toEqual([]);
  expect(screen.queryByRole("group", { name: "Open step Plan work" })).not.toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Open step Done" })).toBeInTheDocument();
});

it("does not turn a composing canvas Delete into a structural command", async () => {
  installLinearWorkflow(5);
  serveChangesWithDryRun(cleanDryRun);
  serveDeleteReactionGets((url) => stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  const card = await screen.findByRole("group", { name: "Open step Plan work" });
  fireEvent.click(card);
  fireEvent.click(within(await screen.findByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
  const node = canvasNodeForStep("Plan work");
  expect(node).not.toBeNull();
  if (!node) throw new Error("Selected React Flow node was not rendered");
  node.focus();
  expect(node).toHaveFocus();
  fireEvent.keyDown(node, { key: "Delete", code: "Delete", isComposing: true });
  expect(screen.queryByRole("dialog", { name: "Delete this step?" })).not.toBeInTheDocument();
  expect(dryRunChangeSets()).toEqual([]);
  expect(postedChangeSets()).toEqual([]);
  expect(screen.getByRole("group", { name: "Open step Plan work" })).toBeInTheDocument();
});


/** The contextual drawer stays mounted while the canvas More action is used. */
it("closes only the deleted Step editor after accepted More Delete, without showing New step", async () => {
  installLinearWorkflow(5);
  serveChangesWithDryRun(cleanDryRun);
  serveDeleteReactionGets((url) => stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  fireEvent.click(await screen.findByRole("group", { name: "Open step Plan work" }));
  expect(await screen.findByRole("dialog", { name: "Step editor" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "More actions for Plan work" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
  await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)"));
  expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "New step" })).not.toBeInTheDocument();
  expect(dryRunChangeSets()).toContainEqual(expect.objectContaining({
    expected_revision: 5, dry_run: true, operations: [{ op: "delete_step", step_id: "plan" }],
  }));
  expect(postedChangeSets()).toEqual([]);
  expect(screen.getByRole("group", { name: "Open step Done" })).toBeInTheDocument();
});


it("leaves a focused plaintext-only canvas editing host alone on Delete", async () => {
  installLinearWorkflow(5);
  serveChangesWithDryRun(cleanDryRun);
  serveDeleteReactionGets((url) => stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  const card = await screen.findByRole("group", { name: "Open step Plan work" });
  fireEvent.click(card);
  fireEvent.click(within(await screen.findByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
  const node = canvasNodeForStep("Plan work");
  expect(node).not.toBeNull();
  if (!node) throw new Error("Selected React Flow node was not rendered");
  const editable = document.createElement("div");
  editable.setAttribute("contenteditable", "plaintext-only");
  editable.tabIndex = 0;
  node.append(editable);
  editable.focus();
  expect(editable).toHaveFocus();
  fireEvent.keyDown(editable, { key: "Delete", code: "Delete" });
  expect(screen.queryByRole("dialog", { name: "Delete this step?" })).not.toBeInTheDocument();
  expect(dryRunChangeSets()).toEqual([]);
  expect(postedChangeSets()).toEqual([]);
  expect(screen.getByRole("group", { name: "Open step Plan work" })).toBeInTheDocument();
  editable.remove();
});


/** One guard message lives in the open editor, or in the canvas when the editor is closed. */
it.each(["open", "closed"])("shows one saved-rule refusal host with the %s Step editor", async (host) => {
  installLinearWorkflow(5);
  serveChangesWithDryRun(cleanDryRun);
  serveDeleteReactionGets((url) => stepRuleReply(url,
    url.searchParams.get("scope_kind") === "step" && url.searchParams.get("scope_id") === "plan"
      ? [{ reaction_key: "protect_plan", display_name: "Guard plan", event_type: "job.step_failed",
        conditions: {}, action: { type: "notify" }, enabled: false, scope_kind: "step", scope_id: "plan" }]
      : [],
  ));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  const card = await screen.findByRole("group", { name: "Open step Plan work" });
  fireEvent.click(card);
  const editor = await screen.findByRole("dialog", { name: "Step editor" });
  if (host === "open") {
    fireEvent.click(within(editor).getByRole("button", { name: "Delete step" }));
  } else {
    fireEvent.click(within(editor).getByRole("button", { name: "Close" }));
    const node = canvasNodeForStep("Plan work");
    if (!node) throw new Error("Selected React Flow node was not rendered");
    node.focus();
    expect(node).toHaveFocus();
    fireEvent.keyDown(node, { key: "Delete", code: "Delete" });
  }
  await waitFor(() => expect(screen.getAllByText(/Saved Automation rule: Guard plan/)).toHaveLength(1));
  expect(screen.getAllByRole("button", { name: "Open Automation" })).toHaveLength(1);
  expect(screen.getByRole("group", { name: "Open step Plan work" })).toBeInTheDocument();
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expect(postedChangeSets()).toEqual([]);
  if (host === "open") expect(screen.getByRole("dialog", { name: "Step editor" })).toBeInTheDocument();
  else expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument();
});

/** A successful guarded container deletion must not leave its old Group draft open. */
it("closes the affected Group editor only after its guarded delete stages", async () => {
  const steps = [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable", group_id: "checks" },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ];
  const groups = [{ group_id: "checks", display_name: "Checks", body: { kind: "sequence", entry: { kind: "step", id: "plan" } } }];
  const edges = [
    { source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "checks", lane_id: null } },
    { source: { kind: "group", id: "checks" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
  ];
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps, groups, edges, entry: { kind: "group", id: "checks" } }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "group", id: "checks" },
    groups: [{ id: "checks", group_id: "checks", display_name: "Checks", body: { kind: "sequence", entry: { kind: "step", id: "plan" } } }],
    steps: [
      { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", group_id: "checks", config_summary: "", ports: NEXT },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
    ], edges,
  }));
  serveChangesWithDryRun(cleanDryRun);
  serveDeleteReactionGets((url) => stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  const container = await screen.findByText((_, element) => element?.getAttribute("data-group-id") === "checks");
  fireEvent.click(within(container).getAllByRole("button", { name: /Checks/ })[0]);
  const editor = await screen.findByRole("dialog", { name: "Group editor" });
  fireEvent.click(within(editor).getByRole("button", { name: "Delete group" }));
  await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)"));
  expect(screen.queryByRole("dialog", { name: "Group editor" })).not.toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Group editor" })).not.toBeInTheDocument();
  expect(dryRunChangeSets()).toContainEqual(expect.objectContaining({ expected_revision: 5, dry_run: true,
    operations: [{ op: "delete_group", group_id: "checks", with_contents: true }],
  }));
  expect(postedChangeSets()).toEqual([]);
});

/** Four saved cards leave an ordinary Step available to select after a two-root deletion. */
function installBulkDeleteWorkflow() {
  const steps = [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable" },
    { step_id: "review", display_name: "Review work", step_kind: "set_variable" },
    { step_id: "archive", display_name: "Archive work", step_kind: "set_variable" },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ];
  const edges = [
    { source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "step", id: "review" } },
    { source: { kind: "step", id: "review" }, outcome: { name: "success" }, target: { kind: "step", id: "archive" } },
    { source: { kind: "step", id: "archive" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
  ];
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps, edges }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "step", id: "plan" },
    steps: [
      { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", config_summary: "", ports: NEXT },
      { id: "review", step_id: "review", kind: "set_variable", display_name: "Review work", config_summary: "", ports: NEXT },
      { id: "archive", step_id: "archive", kind: "set_variable", display_name: "Archive work", config_summary: "", ports: NEXT },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
    ], edges,
  }));
}

/** React Flow's focusable node wrapper has no unique accessible name. */
function canvasNodeForStep(name: string) {
  return screen.getAllByRole("group").find((candidate) =>
    candidate.getAttribute("aria-roledescription") === "node" &&
    within(candidate).queryByRole("group", { name: `Open step ${name}` }));
}
function bulkCanvasNode(name: string) {
  const node = canvasNodeForStep(name);
  if (!node) throw new Error(`Missing React Flow node for ${name}`);
  return node;
}

async function selectBulkCards(first: string, second: string) {
  fireEvent.click(await screen.findByRole("group", { name: `Open step ${first}` }));
  const firstEditor = await screen.findByRole("dialog", { name: "Step editor" });
  fireEvent.click(within(firstEditor).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(bulkCanvasNode(first)).toHaveClass("selected"));
  // Shift+click adds a node to the selection without opening its editor (Canvas › Selecting).
  const additive = { key: "Shift", code: "ShiftLeft", shiftKey: true };
  fireEvent.keyDown(window, additive);
  fireEvent.click(await screen.findByRole("group", { name: `Open step ${second}` }), additive);
  fireEvent.keyUp(window, additive);
  expect(screen.queryByRole("dialog", { name: "Step editor" })).not.toBeInTheDocument();
  await waitFor(() => {
    expect(bulkCanvasNode(first)).toHaveClass("selected");
    expect(bulkCanvasNode(second)).toHaveClass("selected");
  });
  return screen.getByRole("toolbar", { name: "Selected items" });
}

/** Delete on the selection bar acts at once: no confirmation, the toast offers Undo. */
function bulkDelete() {
  fireEvent.click(within(screen.getByRole("toolbar", { name: "Selected items" })).getByRole("button", { name: "Delete" }));
  expect(screen.queryByRole("dialog", { name: /Delete/ })).not.toBeInTheDocument();
}

it("proves two selected saved roots in one dry-run and stages one Undo intent", async () => {
  installBulkDeleteWorkflow();
  serveChangesWithDryRun(cleanDryRun);
  let releaseDeleteProof: ((reply: Response) => void) | undefined;
  const heldDeleteProof = new Promise<Response>((resolve) => { releaseDeleteProof = resolve; });
  const deleteProofs: Array<Record<string, unknown>> = [];
  const prior = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    if (stringFromUnknownValue(input).endsWith("/changes") && init?.method === "POST") {
      const body = JSON.parse(String(init.body || "{}")) as Record<string, unknown>;
      if (body.dry_run === true && JSON.stringify(body.operations).includes('"delete_step"')) {
        deleteProofs.push(body);
        if (deleteProofs.length === 1) return heldDeleteProof;
      }
    }
    return prior ? prior(input, init) : new Response("{}");
  });
  serveDeleteReactionGets((url) => stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  const selected = await selectBulkCards("Plan work", "Review work");
  expect(within(selected).getByText("2 selected")).toBeVisible(); // First Task4 semantic RED: bar absent.
  bulkDelete();
  await waitFor(() => expect(deleteProofs).toHaveLength(1));
  const [candidate] = deleteProofs;
  expect(candidate).toEqual({
    expected_revision: 5, dry_run: true,
    operations: [{ op: "delete_step", step_id: "plan" }, { op: "delete_step", step_id: "review" }],
  });
  expect(postedChangeSets()).toEqual([]);
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  const proof = cleanDryRun(candidate);
  await act(async () => { releaseDeleteProof?.(new Response(JSON.stringify(proof.body), { status: proof.status, headers: { "content-type": "application/json" } })); await heldDeleteProof; });
  await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 2 change(s)"));
  const done = bulkCanvasNode("Done");
  act(() => done.focus());
  expect(done).toHaveFocus();
  fireEvent.keyDown(done, { key: "z", code: "KeyZ", ctrlKey: true });
  expect(await screen.findByRole("group", { name: "Open step Plan work" })).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Open step Review work" })).toBeInTheDocument();
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expect(postedChangeSets()).toEqual([]);
});

it("deduplicates a selected Group and its child Step into one parent delete candidate", async () => {
  const steps = [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable", group_id: "checks" },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ];
  const groups = [{ group_id: "checks", display_name: "Checks", body: { kind: "sequence", entry: { kind: "step", id: "plan" } } }];
  const edges = [{ source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "checks", lane_id: null } }];
  const successor = { source: { kind: "group", id: "checks" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } };
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps, groups, entry: { kind: "group", id: "checks" }, edges: [...edges, successor] }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "group", id: "checks" },
    groups: [{ id: "checks", group_id: "checks", display_name: "Checks", body: { kind: "sequence", entry: { kind: "step", id: "plan" } } }],
    steps: [
      { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", group_id: "checks", config_summary: "", ports: NEXT },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
    ], edges: [...edges, successor],
  }));
  serveChangesWithDryRun(cleanDryRun);
  const gets = serveDeleteReactionGets((url) => stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  await screen.findByRole("button", { name: /^Checks\b/ });
  const groupNode = screen.getAllByRole("group").find((candidate) =>
    candidate.getAttribute("aria-roledescription") === "node" &&
    within(candidate).queryByRole("button", { name: /^Checks\b/ }));
  if (!groupNode) throw new Error("Missing React Flow Group wrapper");
  fireEvent.pointerDown(groupNode);
  fireEvent.pointerUp(groupNode);
  fireEvent.click(groupNode); // Header clicks open the editor; the RF wrapper selects the Group.
  await waitFor(() => expect(groupNode).toHaveClass("selected"));
  const additive = { key: "Shift", code: "ShiftLeft", shiftKey: true };
  fireEvent.keyDown(window, additive);
  fireEvent.click(screen.getByRole("group", { name: "Open step Plan work" }), additive);
  fireEvent.keyUp(window, additive);
  await waitFor(() => {
    expect(groupNode).toHaveClass("selected");
    expect(bulkCanvasNode("Plan work")).toHaveClass("selected");
  });
  expect(within(screen.getByRole("toolbar", { name: "Selected items" })).getByText("2 selected")).toBeVisible();
  bulkDelete();
  await waitFor(() => expect(dryRunChangeSets().some((body) => JSON.stringify(body.operations).includes('"delete_group"'))).toBe(true));
  const candidates = dryRunChangeSets().filter((body) => JSON.stringify(body.operations).includes('"delete_group"'));
  expect(candidates[0]).toEqual({ expected_revision: 5, dry_run: true, operations: [{ op: "delete_group", group_id: "checks", with_contents: true }] });
  expect(candidates.every((body) => JSON.stringify(body.operations) === JSON.stringify(candidates[0].operations))).toBe(true);
  expect(gets.map((url) => `${url.searchParams.get("scope_kind")}:${url.searchParams.get("scope_id")}`)).toEqual(expect.arrayContaining(["group:checks", "step:plan"]));
  expect(postedChangeSets()).toEqual([]);
});

it("refuses the whole selected set when one member has a saved disabled Automation rule", async () => {
  installBulkDeleteWorkflow();
  serveChangesWithDryRun(cleanDryRun);
  const gets = serveDeleteReactionGets((url) => stepRuleReply(url,
    url.searchParams.get("scope_kind") === "step" && url.searchParams.get("scope_id") === "review"
      ? [{ reaction_key: "saved_review", display_name: "Guard review", event_type: "job.step_failed", conditions: {}, action: { type: "notify" }, enabled: false, scope_kind: "step", scope_id: "review" }]
      : [],
  ));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  await selectBulkCards("Plan work", "Review work");
  bulkDelete();
  expect(await screen.findByText(/Guard review/)).toBeVisible();
  expect(gets.map((url) => url.searchParams.get("scope_id"))).toEqual(expect.arrayContaining(["plan", "review"]));
  expect(dryRunChangeSets().filter((body) => JSON.stringify(body.operations).includes('"delete_step"'))).toEqual([]);
  expect(screen.getByRole("group", { name: "Open step Plan work" })).toBeInTheDocument();
  expect(screen.getByRole("group", { name: "Open step Review work" })).toBeInTheDocument();
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expect(postedChangeSets()).toEqual([]);
});

it("retires a held combined guard when the selected card set changes", async () => {
  installBulkDeleteWorkflow();
  serveChangesWithDryRun(cleanDryRun);
  let releaseGet: ((reply: Response) => void) | undefined;
  const heldGet = new Promise<Response>((resolve) => { releaseGet = resolve; });
  const gets = serveDeleteReactionGets((url) => url.searchParams.get("scope_id") === "plan" ? heldGet : stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  await selectBulkCards("Plan work", "Review work");
  bulkDelete();
  await waitFor(() => expect(gets.some((url) => url.searchParams.get("scope_id") === "plan")).toBe(true));
  fireEvent.click(screen.getByRole("group", { name: "Open step Archive work" }));
  const archiveEditor = await screen.findByRole("dialog", { name: "Step editor" });
  fireEvent.click(within(archiveEditor).getByRole("button", { name: "Close" }));
  await waitFor(() => {
    expect(bulkCanvasNode("Plan work")).not.toHaveClass("selected");
    expect(bulkCanvasNode("Review work")).not.toHaveClass("selected");
    expect(bulkCanvasNode("Archive work")).toHaveClass("selected");
  });
  await act(async () => { releaseGet?.(stepRuleReply(gets.find((url) => url.searchParams.get("scope_id") === "plan")!, [])); await heldGet; });
  expect(dryRunChangeSets().filter((body) => JSON.stringify(body.operations).includes('"delete_step"'))).toEqual([]);
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expect(postedChangeSets()).toEqual([]);
});

it("keeps a newly opened unrelated Group editor when a held Step delete completes", async () => {
  const steps = [
    { step_id: "plan", display_name: "Plan work", step_kind: "set_variable" },
    { step_id: "review", display_name: "Review work", step_kind: "set_variable", group_id: "checks" },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
  ];
  const groups = [{ group_id: "checks", display_name: "Checks", body: { kind: "sequence", entry: { kind: "step", id: "review" } } }];
  const edges = [
    { source: { kind: "step", id: "plan" }, outcome: { name: "success" }, target: { kind: "group", id: "checks" } },
    { source: { kind: "step", id: "review" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "checks", lane_id: null } },
    { source: { kind: "group", id: "checks" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
  ];
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps, groups, edges, entry: { kind: "step", id: "plan" } }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "step", id: "plan" },
    groups: [{ id: "checks", group_id: "checks", display_name: "Checks", body: { kind: "sequence", entry: { kind: "step", id: "review" } } }],
    steps: [
      { id: "plan", step_id: "plan", kind: "set_variable", display_name: "Plan work", config_summary: "", ports: NEXT },
      { id: "review", step_id: "review", kind: "set_variable", display_name: "Review work", group_id: "checks", config_summary: "", ports: NEXT },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
    ], edges,
  }));
  serveChangesWithDryRun(cleanDryRun);
  let releaseGet: ((reply: Response) => void) | undefined;
  const heldGet = new Promise<Response>((resolve) => { releaseGet = resolve; });
  const gets = serveDeleteReactionGets((url) => url.searchParams.get("scope_id") === "plan" ? heldGet : stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  fireEvent.click(await screen.findByRole("group", { name: "Open step Plan work" }));
  expect(await screen.findByRole("dialog", { name: "Step editor" })).toBeInTheDocument();
  await waitFor(() => expect(bulkCanvasNode("Plan work")).toHaveClass("selected"));
  // One selected node shows no selection bar; Delete on the canvas acts on it at once.
  fireEvent.keyDown(bulkCanvasNode("Plan work"), { key: "Delete", code: "Delete" });
  await waitFor(() => expect(gets.some((url) => url.searchParams.get("scope_id") === "plan")).toBe(true));
  const container = await screen.findByText((_, element) => element?.getAttribute("data-group-id") === "checks");
  fireEvent.click(within(container).getAllByRole("button", { name: /Checks/ })[0]);
  const groupEditor = await screen.findByRole("dialog", { name: "Group editor" });
  expect(groupEditor).toBeVisible();
  expect(bulkCanvasNode("Plan work")).toHaveClass("selected"); // Header opens the drawer without replacing the Delete selection.
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  await act(async () => { releaseGet?.(stepRuleReply(gets.find((url) => url.searchParams.get("scope_id") === "plan")!, [])); await heldGet; });
  await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)"));
  expect(dryRunChangeSets()).toContainEqual({ expected_revision: 5, dry_run: true, operations: [{ op: "delete_step", step_id: "plan" }] });
  expect(screen.getByRole("dialog", { name: "Group editor" })).toBeVisible();
  expect(postedChangeSets()).toEqual([]);
});


// SOURCE PROPOSAL 09A: additions below keep every pre-existing journey unchanged.
type RunBarHistoryRow = JobListItem & {
  task_source: "workflow_test_run"; duration_ms: number | null;
};
type RunBarDetail = JobDetail;
const runBarAckKey = "fs.workflow-run-job.weekly_report";
const runBarThreadKey = "fs.workflow-test-run.weekly_report";
const runBarEntry = "/registry/workflows/weekly_report/builder?project_id=project_alpha";
function deferredRunBarResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  return { promise, resolve };
}
function runBarJson(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}
function runBarRow(id: string, extra: Partial<RunBarHistoryRow> = {}): RunBarHistoryRow {
  return { id, job_id: id, project_id: "project_alpha", selected_workflow: "weekly_report",
    task_source: "workflow_test_run", status: "completed", started_at: "2026-10-18T12:00:00.000Z",
    finished_at: "2026-10-18T12:00:00.033Z", duration_ms: 33, ...extra };
}
function runBarHistory(rows: RunBarHistoryRow[]) {
  return runBarJson({ ok: true, kind: "list", data: { jobs: rows, page: 1, per_page: 8, total: rows.length, total_pages: 1 }, error: null });
}
function installRunBarFixture() {
  installLinearWorkflow(5);
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  const rows = [runBarRow("bar-latest"), runBarRow("bar-older", { status: "failed" })];
  const details = new Map<string, RunBarDetail>();
  const revisions = new Map<string, number>();
  const historyRequests: URL[] = [];
  const detailRequests: string[] = [];
  let history: (url: URL) => Response | Promise<Response> = () => runBarHistory(rows);
  let detail: ((id: string) => Response | Promise<Response> | undefined) | undefined;
  const add = (row: RunBarHistoryRow) => {
    details.set(String(row.id), { id: String(row.id), status: row.status || "completed",
      project_id: row.project_id, selected_workflow: row.selected_workflow,
      started_at: row.started_at || undefined, finished_at: row.finished_at || undefined,
      elapsed_seconds: row.duration_ms === null ? undefined : row.duration_ms / 1000,
      job: { id: String(row.id), project_id: row.project_id, selected_workflow: row.selected_workflow, status: row.status }, execution_steps: [] });
  };
  rows.forEach(add);
  const fallback = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === "/api/v2/jobs") {
      historyRequests.push(url);
      // A legacy/unfiltered list must never become successful fake history.
      if (url.searchParams.get("workflow_id") !== "weekly_report" ||
        !["project_alpha", "project_beta"].includes(url.searchParams.get("project_id") || "") ||
        url.searchParams.get("task_source") !== "workflow_test_run" ||
        url.searchParams.get("page") !== "1" || url.searchParams.get("per_page") !== "8") return runBarJson({ error: "Incorrect history scope" }, 422);
      return history(url);
    }
    if (url.pathname === "/api/jobs" || /\/projects\/[^/]+\/jobs$/.test(url.pathname)) return runBarJson({ error: "Wrong history endpoint" }, 422);
    const match = /^\/api(?:\/v2)?\/jobs\/(bar-[^/]+)(?:\/(.*))?$/.exec(url.pathname);
    if (!match) return fallback ? fallback(input, init) : runBarJson({});
    const id = match[1];
    const real = details.get(id);
    if (!real) return runBarJson({ error: "Unknown job" }, 404);
    const suffix = match[2];
    if (!suffix) { detailRequests.push(id); return detail?.(id) || runBarJson(real); }
    if (suffix === "definition") return runBarJson({ job_id: id, available: true, workflow_id: real.selected_workflow,
      revision: revisions.get(id) ?? 5, digest: `archive-${id}`, schema_version: 1,
      steps: [{ step_id: "plan", display_name: "Plan work", step_kind: "set_variable" }], groups: [], for_each: [],
      execution_graph: { schema_version: 1, entry: { kind: "step", id: "plan" }, edges: [] } });
    if (suffix === "execution-steps") return runBarJson({ job_id: id, run_terminal: true, child_invocations: [],
      steps: [{ step_id: "plan", step_execution_id: `${id}-plan`, occurrence: 1, attempt: 1,
        status: real.status, started_at: "2026-10-18T12:00:00.000Z", finished_at: "2026-10-18T12:00:00.017Z", duration_ms: 17 }],
      page: { count: 1, has_more: false, next_cursor: "" } });
    if (suffix === "cost-breakdown") return runBarJson({ breakdown: [], total_cost_usd: 0 });
    if (suffix === "approval-history") return runBarJson({ job_id: id, approvals: [] });
    if (suffix === "decision-overrides") return runBarJson({ job_id: id, overrides: [] });
    return runBarJson({ job_id: id });
  });
  return { rows, details, revisions, historyRequests, detailRequests, add,
    history: (handler: typeof history) => { history = handler; },
    detail: (handler: NonNullable<typeof detail>) => { detail = handler; } };
}
/**
 * After a reload the server's latest finished run is shown by itself; an earlier
 * run is chosen in Results › Test runs. `verify: false` only waits for the read.
 */
async function chooseInitialRunBarHistory(id = "bar-latest", verify = true) {
  if (!verify) {
    await waitFor(() => expect(fetchSpy.mock.calls.some(([input]) =>
      new URL(String(input), "http://localhost").pathname === `/api/v2/jobs/${id}`)).toBe(true));
    return;
  }
  const bar = await screen.findByTestId("workflow-run-bar");
  await waitFor(() => expect(bar.getAttribute("data-job-id")).toBeTruthy());
  if (bar.getAttribute("data-job-id") === id) return;
  await selectRunBarHistory(id);
  await selectedRunBar(id);
  fireEvent.click(within(screen.getByRole("dialog", { name: "Test Run results" })).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Test Run results" })).not.toBeInTheDocument());
}
async function selectedRunBar(id = "bar-latest") {
  return waitFor(() => {
    const bar = screen.getByTestId("workflow-run-bar");
    expect(bar).toHaveAttribute("data-job-id", id);
    return bar;
  });
}
function selectedRunBarId() {
  return screen.queryByTestId("workflow-run-bar")?.getAttribute("data-job-id") || "";
}
/** Results › Test runs: opens Results from the run bar first when it is closed. */
async function selectRunBarHistory(id: string) {
  if (!screen.queryByRole("dialog", { name: "Test Run results" })) {
    fireEvent.click(within(screen.getByTestId("workflow-run-bar")).getByRole("button", { name: "Results" }));
  }
  fireEvent.click(await screen.findByTestId(`workflow-test-run-row:${id}`));
}
async function openSelectedRunBarResults(id: string) {
  fireEvent.click(within(await selectedRunBar(id)).getByRole("button", { name: "Results" }));
  expect(await screen.findByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", id);
  expect(screen.getByRole("link", { name: "Open full Job Details" })).toHaveAttribute("href", `/jobs/${id}`);
}
function expectRunBarReadOnly() {
  expect(postedChangeSets()).toEqual([]);
  expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
  expect(fetchSpy.mock.calls.filter(([, init]) => String(init?.method || "GET").toUpperCase() === "POST")).toEqual([]);
}
function installRunBarInvalidRows() {
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5,
    steps: [{ step_id: "feed_rows", display_name: "Feed rows", step_kind: "tabular_feed", override: {
      tabular_feed: { operation: "create_dataset", rows: [{ sku: "A1" }], result_target: "state.feed_dataset" } } }], edges: [] }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ entry: { kind: "step", id: "feed_rows" },
    steps: [{ id: "feed_rows", step_id: "feed_rows", kind: "tabular_feed", display_name: "Feed rows", config_summary: "Create dataset", ports: [] }], edges: [] }));
}

describe("09A persistent selected Test Run identity (SOURCE proposal)", () => {
  it("keeps the selected runbar and real marks after Results closes, then reopens the same job", async () => {
    const fixture = installRunBarFixture();
    renderPage({ surface: "builder", initialEntry: runBarEntry });
    await chooseInitialRunBarHistory();
    await openSelectedRunBarResults("bar-latest");
    fireEvent.click(within(screen.getByRole("dialog", { name: "Test Run results" })).getByRole("button", { name: "Close" }));
    expect(await selectedRunBar()).toBeVisible();
    expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
    const plan = screen.getByRole("group", { name: "Open step Plan work" });
    await waitFor(() => expect(within(plan).getByTestId("workflow-runtime-mark")).toHaveAttribute("data-job-id", "bar-latest"));
    await openSelectedRunBarResults("bar-latest");
    expect(fixture.historyRequests.length).toBeGreaterThan(0);
    expectRunBarReadOnly();
  });
  it("selects a historical run and returns to actual server latest without rewriting ACK or pending storage", async () => {
    installRunBarFixture();
    localStorage.setItem(runBarAckKey, "bar-older");
    const view = renderPage({ surface: "builder", initialEntry: runBarEntry });
    await chooseInitialRunBarHistory("bar-older");
    await selectedRunBar("bar-older");
    const before = [localStorage.getItem(runBarAckKey), localStorage.getItem(runBarThreadKey)];
    // A verified restored ACK is selected independently of actual server Latest.
    // Keep a genuine history transition rather than selecting the current row.
    await selectRunBarHistory("bar-latest");
    await selectedRunBar("bar-latest");
    await selectRunBarHistory("bar-older");
    await selectedRunBar("bar-older");
    await openSelectedRunBarResults("bar-older");
    fireEvent.click(within(screen.getByRole("dialog", { name: "Test Run results" })).getByRole("button", { name: "Close" }));
    fireEvent.click(within(await selectedRunBar("bar-older")).getByRole("button", { name: "Back to latest" }));
    await openSelectedRunBarResults("bar-latest");
    expect([localStorage.getItem(runBarAckKey), localStorage.getItem(runBarThreadKey)]).toEqual(before);
    expectRunBarReadOnly();
    view.unmount();
  });
  it.each(["task_source", "project_id", "selected_workflow"] as const)("excludes a history row with a foreign %s", async (field) => {
    const fixture = installRunBarFixture();
    const bad = { ...runBarRow("bar-foreign"), [field]: field === "task_source" ? "step_test_run" : "foreign-owner" };
    fixture.history(() => runBarJson({ ok: true, kind: "list", data: { jobs: [bad, ...fixture.rows], page: 1, per_page: 8, total: 3, total_pages: 1 }, error: null }));
    renderPage({ surface: "builder", initialEntry: runBarEntry });
    // The foreign row is neither the shown latest run nor listed among the test runs.
    await selectedRunBar("bar-latest");
    await openSelectedRunBarResults("bar-latest");
    expect(await screen.findByTestId("workflow-test-run-row:bar-older")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-test-run-row:bar-latest")).toHaveAttribute("aria-current", "true");
    expect(screen.queryByTestId("workflow-test-run-row:bar-foreign")).not.toBeInTheDocument();
    expect(fixture.detailRequests).not.toContain("bar-foreign");
    expectRunBarReadOnly();
  });
  it.each(["id", "project_id", "selected_workflow"] as const)("refuses a selected JobDetail with a foreign %s before exposing Results or marks", async (field) => {
    const fixture = installRunBarFixture();
    const real = fixture.details.get("bar-latest")!;
    fixture.details.set("bar-latest", { ...real, [field]: "foreign-owner", job: { ...real.job, [field]: "foreign-owner" } });
    const pending = deferredRunBarResponse();
    fixture.detail((id) => id === "bar-latest" ? pending.promise : undefined);
    renderPage({ surface: "builder", initialEntry: runBarEntry });
    await chooseInitialRunBarHistory("bar-latest", false);
    await waitFor(() => expect(fixture.detailRequests).toContain("bar-latest"));
    const response = runBarJson(fixture.details.get("bar-latest"));
    const parseBody = vi.spyOn(response, "json");
    await act(async () => { pending.resolve(response); await pending.promise; });
    await waitFor(() => expect(parseBody).toHaveBeenCalledTimes(1));
    await act(async () => {
      await parseBody.mock.results[0].value;
      // Flush the existing query notification after the actual response body settles.
      await new Promise<void>((resolve) => { setTimeout(resolve, 0); });
    });
    // The foreign detail never becomes the selected run: no run bar, Results or marks.
    await waitFor(() => expect(selectedRunBarId()).not.toBe("bar-latest"));
    expect(screen.queryByRole("button", { name: "Results" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
    expectRunBarReadOnly();
  });
  it("keeps archived revision identity in Results and refuses to paint it onto the current revision", async () => {
    const fixture = installRunBarFixture();
    fixture.revisions.set("bar-older", 4);
    renderPage({ surface: "builder", initialEntry: runBarEntry });
    await chooseInitialRunBarHistory();
    await selectedRunBar();
    await selectRunBarHistory("bar-older");
    await openSelectedRunBarResults("bar-older");
    expect(screen.queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
    expectRunBarReadOnly();
  });
  it("displays a persisted real zero run duration", async () => {
    const fixture = installRunBarFixture();
    fixture.rows[0] = runBarRow("bar-latest", { duration_ms: 0 });
    fixture.add(fixture.rows[0]);
    renderPage({ surface: "builder", initialEntry: runBarEntry });
    await chooseInitialRunBarHistory();
    // A persisted zero is a real duration, not an unknown one.
    expect(await selectedRunBar()).toHaveTextContent("Test Run finished in < 0.1 s");
    expectRunBarReadOnly();
  });
  it("keeps the actual failed status and does not invent a missing aggregate duration from terminal steps", async () => {
    const fixture = installRunBarFixture();
    fixture.rows[0] = runBarRow("bar-latest", { status: "failed", duration_ms: null, started_at: undefined, finished_at: undefined });
    fixture.add(fixture.rows[0]);
    renderPage({ surface: "builder", initialEntry: runBarEntry });
    await chooseInitialRunBarHistory();
    const bar = await selectedRunBar();
    expect(bar).toHaveTextContent(/failed/i);
    expect(bar).not.toHaveTextContent(/completed|\b(?:0|17)\s*(?:ms|s)\b/i);
    expectRunBarReadOnly();
  });
  it("retains a valid dirty step draft when Open Results performs read-only navigation", async () => {
    installRunBarFixture();
    renderPage({ surface: "builder", initialEntry: runBarEntry });
    await chooseInitialRunBarHistory();
    await selectedRunBar();
    openStep("plan");
    const label = await screen.findByRole("textbox", { name: "Step label" });
    fireEvent.change(label, { target: { value: "Unsaved plan label" } });
    await openSelectedRunBarResults("bar-latest");
    fireEvent.click(within(screen.getByRole("dialog", { name: "Test Run results" })).getByRole("button", { name: "Close" }));
    openStep("plan");
    expect(await screen.findByRole("textbox", { name: "Step label" })).toHaveValue("Unsaved plan label");
    expectRunBarReadOnly();
  });
  // Earlier runs are chosen in Results › Test runs, which a step editor with invalid input
// cannot share the drawer with; the selected-run hook test covers a cancelled history choice.
it.each(["Results", "Back to latest"] as const)("preserves invalid input and selection when %s navigation is cancelled", async (action) => {
    installRunBarFixture();
    installRunBarInvalidRows();
    renderPage({ surface: "builder", initialEntry: runBarEntry });
    await chooseInitialRunBarHistory();
    await selectedRunBar();
    await selectRunBarHistory("bar-older");
    await selectedRunBar("bar-older");
    openStep("feed_rows");
    const rows = await screen.findByRole("textbox", { name: "Rows" });
    fireEvent.change(rows, { target: { value: "[{" } });
    expect(rows).toHaveAttribute("aria-invalid", "true");
    const initiatingControl = within(await selectedRunBar("bar-older")).getByRole("button", { name: action });
    initiatingControl.focus();
    fireEvent.click(initiatingControl);
    const confirmation = await screen.findByRole("dialog", { name: "Discard invalid input?" });
    fireEvent.click(within(confirmation).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(initiatingControl).toHaveFocus());
    expect(await screen.findByRole("textbox", { name: "Rows" })).toBe(rows);
    expect(rows).toHaveValue("[{");
    expect(rows).toHaveAttribute("aria-invalid", "true");
    await selectedRunBar("bar-older");
    expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
    expectRunBarReadOnly();
  });
  it("ignores a replaced historical detail request after Back to latest", async () => {
    const fixture = installRunBarFixture();
    const initial = deferredRunBarResponse();
    const pending = deferredRunBarResponse();
    let olderReads = 0;
    let lateBodiesParsed = 0;
    fixture.detail((id) => {
      if (id !== "bar-older") return undefined;
      olderReads += 1;
      // Each real HTTP reader consumes its own clone; the saved base Response
      // remains usable across the compact-to-Jobs query handoff.
      const late = olderReads > 1;
      return (late ? pending.promise : initial.promise).then((response) => {
        const copy = response.clone();
        if (late) {
          const readJson = copy.json.bind(copy);
          vi.spyOn(copy, "json").mockImplementation(async () => {
            const body = await readJson();
            lateBodiesParsed += 1;
            return body;
          });
        }
        return copy;
      });
    });
    renderPage({ surface: "builder", initialEntry: runBarEntry });
    await chooseInitialRunBarHistory();
    await selectedRunBar();
    const pointers = [localStorage.getItem(runBarAckKey), localStorage.getItem(runBarThreadKey)];
    await selectRunBarHistory("bar-older");
    await waitFor(() => expect(fixture.detailRequests).toContain("bar-older"));
    expect(selectedRunBarId()).not.toBe("bar-older");
    expect(screen.queryByRole("button", { name: "Back to latest" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-test-run-results")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-runtime-mark")).not.toBeInTheDocument();
    await act(async () => { initial.resolve(runBarJson(fixture.details.get("bar-older"))); await initial.promise; });
    await selectedRunBar("bar-older");
    // Mount the actual Jobs consumer over verified cached detail, then witness
    // its held real revalidation before replacing this selected owner.
    await openSelectedRunBarResults("bar-older");
    await waitFor(() => expect(olderReads).toBeGreaterThanOrEqual(2));
    const back = within(await selectedRunBar("bar-older")).getByRole("button", { name: "Back to latest" });
    await waitFor(() => expect(back).toBeEnabled());
    fireEvent.click(back);
    await selectedRunBar();
    await waitFor(() => expect(screen.getByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "bar-latest"));
    await act(async () => { pending.resolve(runBarJson(fixture.details.get("bar-older"))); await pending.promise; });
    await waitFor(() => expect(lateBodiesParsed).toBeGreaterThan(0));
    await act(async () => {
      // Flush query notification after the actual late response body settles.
      await new Promise<void>((resolve) => { setTimeout(resolve, 0); });
    });
    await openSelectedRunBarResults("bar-latest");
    await waitFor(() => expect(within(screen.getByRole("group", { name: "Open step Plan work" })).getByTestId("workflow-runtime-mark")).toHaveAttribute("data-job-id", "bar-latest"));
    expect([localStorage.getItem(runBarAckKey), localStorage.getItem(runBarThreadKey)]).toEqual(pointers);
    expectRunBarReadOnly();
  });
  it("does not roll a new ACK back when an older pending history response arrives", async () => {
    const fixture = installRunBarFixture();
    const pending = deferredRunBarResponse();
    let calls = 0;
    fixture.history(() => ++calls === 1 ? pending.promise : runBarHistory([runBarRow("bar-new"), ...fixture.rows]));
    fixture.add(runBarRow("bar-new", { status: "in_progress", duration_ms: null }));
    const ack = { workflow_id: "weekly_report", project_id: "project_alpha", job_id: "bar-new", thread_id: "thread-bar-new", status: "in_progress", final_status: "in_progress", needs_user_approval: false };
    mockedTestRunWorkflow.mockResolvedValue(ack);
    mockedFetchWorkflowTestRunState.mockResolvedValue(ack);
    renderPage({ surface: "builder", initialEntry: runBarEntry });
    await waitFor(() => expect(fixture.historyRequests).toHaveLength(1));
    fireEvent.click(within(await screen.findByRole("toolbar", { name: "Canvas tools" })).getByRole("link", { name: "Test Run" }));
    fireEvent.click(await screen.findByRole("button", { name: "Run workflow test run" }));
    await selectedRunBar("bar-new");
    await waitFor(() => expect(fixture.historyRequests.length).toBeGreaterThan(1));
    await act(async () => { pending.resolve(runBarHistory(fixture.rows)); await pending.promise; });
    await selectedRunBar("bar-new");
    expect(localStorage.getItem(runBarAckKey)).toBe("bar-new");
    expect(screen.getByTestId("workflow-test-run-results")).toHaveAttribute("data-job-id", "bar-new");
    expect(mockedTestRunWorkflow).toHaveBeenCalledTimes(1);
    expect(postedChangeSets()).toEqual([]);
  });
  it("rejects a late history response and stored ACK from another project of the same workflow", async () => {
    const fixture = installRunBarFixture();
    const pending = deferredRunBarResponse();
    const beta = runBarRow("bar-beta", { project_id: "project_beta" });
    fixture.add(beta);
    fixture.history((url) => url.searchParams.get("project_id") === "project_alpha" ? pending.promise : runBarHistory([beta]));
    localStorage.setItem(runBarAckKey, "bar-latest");
    const view = renderPage({ surface: "builder", initialEntry: runBarEntry });
    await waitFor(() => expect(fixture.historyRequests.some((url) => url.searchParams.get("project_id") === "project_alpha")).toBe(true));
    await act(async () => { await view.router.navigate("/registry/workflows/weekly_report/builder?project_id=project_beta"); });
    await chooseInitialRunBarHistory("bar-beta");
    await selectedRunBar("bar-beta");
    await act(async () => { pending.resolve(runBarHistory(fixture.rows)); await pending.promise; });
    await selectedRunBar("bar-beta");
    expect(selectedRunBarId()).toBe("bar-beta");
    await openSelectedRunBarResults("bar-beta");
    expect(localStorage.getItem(runBarAckKey)).toBe("bar-latest");
    expectRunBarReadOnly();
  });
});

describe("header popups above an open contextual inspector", () => {
  it("portals More outside the canvas stack, toggles the real Advanced action and preserves the editor draft", async () => {
    installLinearWorkflow(5);
    const user = userEvent.setup();
    const view = renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
    try {
      await waitFor(() => expect(window.editStep).toBeTypeOf("function"));
      openStep("plan");
      const editor = await screen.findByRole("dialog", { name: "Step editor" });
      const label = within(editor).getByRole("textbox", { name: "Step label" });
      fireEvent.change(label, { target: { value: "Keep the open editor draft" } });
      const more = screen.getByRole("button", { name: "More" });
      await user.click(more);
      const advanced = await screen.findByRole("button", { name: /^Advanced mode (on|off)$/ });
      expect(within(screen.getByRole("region", { name: "Weekly Report" })).queryByRole("button", { name: /^Advanced mode (on|off)$/ })).not.toBeInTheDocument();
      expect(editor).not.toContainElement(advanced);
      const before = advanced.textContent;
      await user.click(advanced);
      const settings = await screen.findByRole("dialog", { name: "Workflow settings" });
      expect(within(settings).getByRole("tab", { name: "Advanced" })).toHaveAttribute("aria-selected", "true");
      openStep("plan");
      const returnedEditor = await screen.findByRole("dialog", { name: "Step editor" });
      expect(within(returnedEditor).getByRole("textbox", { name: "Step label" })).toHaveValue("Keep the open editor draft");
      await user.click(more);
      const reopenedAdvanced = await screen.findByRole("button", { name: /^Advanced mode (on|off)$/ });
      const reopenedText = reopenedAdvanced.textContent;
      expect(reopenedText).not.toBe(before);
      await user.keyboard("{Escape}");
      expect(screen.queryByRole("button", { name: /^Advanced mode (on|off)$/ })).not.toBeInTheDocument();
      expect(more).toHaveFocus();
      expect(screen.getByRole("dialog", { name: "Step editor" })).toBe(returnedEditor);
      expect(within(returnedEditor).getByRole("textbox", { name: "Step label" })).toHaveValue("Keep the open editor draft");
      expect(postedChangeSets()).toEqual([]);
      expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
      expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    } finally { view.unmount(); }
  });

  it("closes only More on Escape and preserves invalid input when the real Diagnostics close guard is cancelled", async () => {
    installRunBarInvalidRows();
    const user = userEvent.setup();
    const view = renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
    try {
      await waitFor(() => expect(window.editStep).toBeTypeOf("function"));
      openStep("feed_rows");
      const editor = await screen.findByRole("dialog", { name: "Step editor" });
      const rows = within(editor).getByRole("textbox", { name: "Rows" });
      fireEvent.change(rows, { target: { value: "[{" } });
      expect(rows).toHaveAttribute("aria-invalid", "true");
      const more = screen.getByRole("button", { name: "More" });
      await user.click(more);
      await screen.findByRole("button", { name: /^Advanced mode (on|off)$/ });
      expect(within(screen.getByRole("region", { name: "Weekly Report" })).queryByRole("button", { name: /^Advanced mode (on|off)$/ })).not.toBeInTheDocument();
      await user.keyboard("{Escape}");
      expect(screen.queryByRole("dialog", { name: "Discard invalid input?" })).not.toBeInTheDocument();
      expect(screen.getByRole("dialog", { name: "Step editor" })).toBe(editor);
      expect(rows).toHaveValue("[{");
      expect(more).toHaveFocus();
      await user.click(screen.getByRole("button", { name: /^Diagnostics/ }));
      const confirmation = await screen.findByRole("dialog", { name: "Discard invalid input?" });
      await user.click(within(confirmation).getByRole("button", { name: "Cancel" }));
      expect(screen.getByRole("dialog", { name: "Step editor" })).toBe(editor);
      expect(within(editor).getByRole("textbox", { name: "Rows" })).toBe(rows);
      expect(rows).toHaveValue("[{");
      expect(rows).toHaveAttribute("aria-invalid", "true");
      expect(postedChangeSets()).toEqual([]);
      expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
      expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    } finally { view.unmount(); }
  });
});


it.each(["bar-latest", "bar-older"])("keeps %s Builder Results compact while preserving executed provenance and step details", async (id) => {
  const fixture = installRunBarFixture();
  fixture.revisions.set("bar-older", 4);
  const fallback = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname === `/api/v2/jobs/${id}/execution-steps`) return runBarJson({
      job_id: id, run_terminal: true, child_invocations: [],
      steps: [{ step_id: "plan", step_execution_id: `${id}-plan`, occurrence: 1, attempt: 1,
        title: "Recorded Plan step", status: id === "bar-older" ? "failed" : "completed",
        inputs: { request: "Recorded input" }, outputs: { receipt: "Recorded output" }, message: "Recorded execution message" }],
      page: { count: 1, has_more: false, next_cursor: "" },
    });
    return fallback ? fallback(input, init) : runBarJson({});
  });
  const originalRect = HTMLElement.prototype.getBoundingClientRect;
  const measure = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (!this.matches("dialog.workflow-builder-drawer")) return originalRect.call(this);
    // JSDOM has no layout: measure only the real Results drawer at its configured width.
    const width = Number.parseFloat(this.style.width);
    return new DOMRect(window.innerWidth - width, 64, width, window.innerHeight - 64);
  });
  let view: ReturnType<typeof renderPage> | undefined;
  try {
    view = renderPage({ surface: "builder", initialEntry: runBarEntry });
    await chooseInitialRunBarHistory();
    await selectedRunBar();
    if (id === "bar-older") await selectRunBarHistory(id);
    await openSelectedRunBarResults(id);
    const results = screen.getByTestId("workflow-test-run-results");
    const provenance = await within(results).findByTestId("workflow-test-run-revision");
    expect(provenance).toHaveTextContent(`Executed definition: revision ${id === "bar-older" ? 4 : 5}`);
    expect(provenance).toHaveTextContent(`archive-${id}`.slice(0, 12));
    expect(within(results).queryByTestId("job-definition-canvas")).not.toBeInTheDocument();
    const [pass] = await within(results).findAllByTestId("workflow-run-pass");
    fireEvent.click(within(pass).getByRole("button", { name: /Plan work|Recorded Plan step/ }));
    expect(within(pass).getByText("\"Recorded input\"")).toBeVisible();
    if (id === "bar-older") expect(within(pass).getByText("Failed")).toBeVisible();
    else {
      expect(within(pass).getByText("\"Recorded output\"")).toBeVisible();
      expect(within(pass).getByText("Recorded execution message")).toBeVisible();
    }
    expectRunBarReadOnly();
  } finally {
    view?.unmount();
    measure.mockRestore();
  }
});


it("offers a plain Group and Parallel lanes under the toolbar's Group", async () => {
  installLinearWorkflow(5);
  serveChangesWithDryRun(cleanDryRun);
  renderPage({ surface: "builder" });
  const toolbar = await screen.findByRole("toolbar", { name: "Canvas tools" });
  fireEvent.click(within(toolbar).getByRole("button", { name: "Add group" }));
  expect(within(toolbar).getByRole("menuitem", { name: "Group" })).toBeEnabled();
  expect(within(toolbar).queryByRole("menuitem", { name: "For each" })).not.toBeInTheDocument();
  const group = within(toolbar).getByRole("menuitem", { name: "Parallel lanes" });
  expect(group).toBeEnabled();
  fireEvent.click(group);
  expect(await screen.findByRole("dialog", { name: "Group editor" })).toBeVisible();
  expect(screen.queryByRole("dialog", { name: "For each editor" })).not.toBeInTheDocument();
  expect(postedChangeSets()).toEqual([]);
});

it("switches a saved Group between One after another and Parallel lanes, keeping its steps and their connection", async () => {
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, enabled: false,
    groups: [{ group_id: "prepare", display_name: "Prepare", body: { kind: "sequence", entry: { kind: "step", id: "tidy" } } }],
    steps: [
      { step_id: "tidy", step_kind: "set_variable", display_name: "Tidy", group_id: "prepare", override: { set_variable: { assignments: { x: "1" } } } },
      { step_id: "check", step_kind: "set_variable", display_name: "Check", group_id: "prepare", override: { set_variable: { assignments: { y: "2" } } } },
      { step_id: "done", step_kind: "stop_success" },
    ],
    entry: { kind: "group", id: "prepare" },
    edges: [
      { source: { kind: "step", id: "tidy" }, outcome: { name: "success" }, target: { kind: "step", id: "check" } },
      { source: { kind: "step", id: "check" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "prepare", lane_id: null } },
      { source: { kind: "group", id: "prepare" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
    ],
  }));
  serveChangesWithDryRun(cleanDryRun);
  renderPage({ surface: "builder" });
  await waitFor(() => expect(window.openGroupDrawer).toBeTypeOf("function"));
  act(() => window.openGroupDrawer?.("prepare"));
  const runs = await screen.findByRole("combobox", { name: "Steps run" });
  expect(runs).toHaveValue("sequence");
  expect(within(runs).getAllByRole("option").map((option) => option.textContent)).toEqual(["One after another", "Parallel lanes"]);
  fireEvent.change(runs, { target: { value: "lanes" } });
  // One connected chain: one lane.
  expect(screen.getByRole("textbox", { name: "Lane IDs (comma separated)" })).toHaveValue("lane_a");
  fireEvent.click(screen.getByRole("button", { name: "Save group" }));
  await waitFor(() => expect(dryRunChangeSets().length).toBeGreaterThan(0));
  expect(dryRunChangeSets().at(-1)?.operations).toEqual(expect.arrayContaining([
    { op: "update_group_config", group_id: "prepare", patch: { body: { kind: "lanes", lanes: [{ lane_id: "lane_a" }] } } },
    { op: "move_step", step_id: "tidy", group_id: "prepare", for_each_id: null, lane_id: "lane_a" },
    { op: "move_step", step_id: "check", group_id: "prepare", for_each_id: null, lane_id: "lane_a" },
    { op: "connect", edge: { source: { kind: "step", id: "tidy" }, outcome: { name: "success" }, target: { kind: "step", id: "check" } } },
    { op: "connect", edge: { source: { kind: "step", id: "check" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "prepare", lane_id: "lane_a" } } },
    { op: "set_entry", target: { kind: "step", id: "tidy" }, group_id: "prepare", lane_id: "lane_a" },
  ]));
  expect(postedChangeSets()).toEqual([]);
});


it("places a Default Append at its named ghost after deleting a card whose ID is reused", async () => {
  const steps = [
    { step_id: "set_priority", display_name: "Set priority", step_kind: "set_variable" },
    { step_id: "route_priority", display_name: "Route priority", step_kind: "switch", override: { switch: { selector: "state.priority", cases: [{ case_id: "high", when: "high" }, { case_id: "low", when: "low" }] } } },
    { step_id: "done", display_name: "Done", step_kind: "stop_success" },
    { step_id: "set_variable", display_name: "Mark High", step_kind: "set_variable" },
    { step_id: "set_variable_2", display_name: "Mark Low", step_kind: "set_variable" },
    { step_id: "set_variable_3", display_name: "Mark Default", step_kind: "set_variable" },
  ];
  const ports = [{ outcome: { name: "case" as const, case_id: "high" }, label: "High" },
    { outcome: { name: "case" as const, case_id: "low" }, label: "Low" },
    { outcome: { name: "default" as const }, label: "Default" }];
  const edges = [
    { source: { kind: "step" as const, id: "set_priority" }, outcome: { name: "success" as const }, target: { kind: "step" as const, id: "route_priority" } },
    { source: { kind: "step" as const, id: "route_priority" }, outcome: { name: "case" as const, case_id: "high" }, target: { kind: "step" as const, id: "set_variable" } },
    { source: { kind: "step" as const, id: "route_priority" }, outcome: { name: "case" as const, case_id: "low" }, target: { kind: "step" as const, id: "set_variable_2" } },
    { source: { kind: "step" as const, id: "route_priority" }, outcome: { name: "default" as const }, target: { kind: "step" as const, id: "set_variable_3" } },
  ];
  const oldManual = {
    set_priority: { x: 448, y: 64 }, route_priority: { x: 768, y: 64 }, done: { x: 1136, y: 256 },
    set_variable: { x: 1136, y: 112 }, set_variable_2: { x: 1136, y: 432 }, set_variable_3: { x: 1136, y: -32 },
  };
  localStorage.setItem(manualLayoutStorageKey("weekly_report"), JSON.stringify(oldManual));
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5, steps, edges }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "step", id: "set_priority" },
    steps: [
      { id: "set_priority", step_id: "set_priority", kind: "set_variable", display_name: "Set priority", config_summary: "", ports: NEXT },
      { id: "route_priority", step_id: "route_priority", kind: "switch", display_name: "Route priority", config_summary: "state.priority", ports },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
      ...["set_variable", "set_variable_2", "set_variable_3"].map((id, index) => ({ id, step_id: id, kind: "set_variable", display_name: ["Mark High", "Mark Low", "Mark Default"][index], config_summary: "", ports: NEXT })),
    ], edges,
  }));
  serveChangesWithDryRun(cleanDryRun);
  serveDeleteReactionGets((url) => stepRuleReply(url, []));
  renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?project_id=project_alpha" });
  const canvasRegion = await screen.findByRole("region", { name: "Workflow canvas" });
  const canvasQueries = within(canvasRegion);
  const canvas = () => {
    expect(canvasRegion).toBeInTheDocument();
    return canvasQueries;
  };

  fireEvent.click(await canvas().findByRole("group", { name: "Open step Mark Default" }));
  const deleteEditor = await screen.findByRole("dialog", { name: "Step editor" });
  fireEvent.click(await within(deleteEditor).findByRole("button", { name: "Delete step" }));
  await waitFor(() => expect(canvas().queryByRole("group", { name: "Open step Mark Default" })).not.toBeInTheDocument());
  expect(loadManualLayout("weekly_report").set_variable_3).toEqual(oldManual.set_variable_3); // The retired XY still exists.
  fireEvent.click(await canvas().findByRole("group", { name: "Open step Route priority" }));
  const ghost = await canvas().findByRole("button", { name: "Default: add step" });
  const requested = { x: Number.parseFloat(ghost.style.left), y: Number.parseFloat(ghost.style.top) };
  expect(requested.y).toBeGreaterThan(432);
  fireEvent.click(ghost);
  const palette = await screen.findByRole("dialog", { name: /Add after Route priority · Default/ });
  fireEvent.click(within(palette).getByRole("option", { name: "Set variable" }));
  const newCard = await canvas().findByRole("group", { name: "Open step Set variable" });
  await waitFor(() => expect(loadManualLayout("weekly_report").set_variable_3).toEqual(requested));
  expect(newCard).toBeVisible();
  const manual = loadManualLayout("weekly_report");
  for (const [id, position] of Object.entries(oldManual)) if (id !== "set_variable_3") expect(manual[id]).toEqual(position);
  await waitFor(() => expect(dryRunChangeSets().at(-1)?.operations).toEqual(expect.arrayContaining([
    expect.objectContaining({ op: "delete_step", step_id: "set_variable_3" }),
    expect.objectContaining({ op: "create_step", step: expect.objectContaining({ step_id: "set_variable_3" }) }),
    expect.objectContaining({ op: "connect", edge: expect.objectContaining({ source: { kind: "step", id: "route_priority" }, outcome: { name: "default" }, target: { kind: "step", id: "set_variable_3" } }) }),
  ])));
  // A later selection cannot consume the old palette point and move an existing card.
  const editor = await screen.findByRole("dialog", { name: "Step editor" });
  fireEvent.click(within(editor).getByRole("button", { name: "Close" }));
  fireEvent.click(await canvas().findByRole("group", { name: "Open step Mark High" }));
  expect(loadManualLayout("weekly_report")).toEqual(manual);

  const history = (redo: boolean) => {
    expect(canvasRegion).toBeInTheDocument();
    return fireEvent.keyDown(canvasRegion,
      { key: "z", code: "KeyZ", ctrlKey: true, shiftKey: redo });
  };
  const unchanged = () => {
    const positions = loadManualLayout("weekly_report");
    for (const [id, position] of Object.entries(oldManual)) if (id !== "set_variable_3") expect(positions[id]).toEqual(position);
  };
  history(false); // Undo creation: the retired ID still keeps its former XY for restoring the old card.
  await waitFor(() => expect(canvas().queryByRole("group", { name: "Open step Set variable" })).not.toBeInTheDocument());
  await waitFor(() => expect(loadManualLayout("weekly_report").set_variable_3).toEqual(oldManual.set_variable_3));
  unchanged();
  history(false); // Undo deletion: the old card returns at its own XY.
  await canvas().findByRole("group", { name: "Open step Mark Default" });
  expect(loadManualLayout("weekly_report").set_variable_3).toEqual(oldManual.set_variable_3);
  unchanged();
  history(true); // Redo deletion.
  await waitFor(() => expect(canvas().queryByRole("group", { name: "Open step Mark Default" })).not.toBeInTheDocument());
  expect(loadManualLayout("weekly_report").set_variable_3).toEqual(oldManual.set_variable_3);
  unchanged();
  history(true); // Redo creation: the new point returns with the same graph item.
  await canvas().findByRole("group", { name: "Open step Set variable" });
  await waitFor(() => expect(loadManualLayout("weekly_report").set_variable_3).toEqual(requested));
  unchanged();
}, 20_000);
