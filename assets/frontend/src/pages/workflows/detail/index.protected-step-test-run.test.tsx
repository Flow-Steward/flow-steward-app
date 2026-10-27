import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  canvasGraphFixture,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowExportPolicy,
  mockedFindWorkflowLinkedProjectId,
  mockedTestRunWorkflow,
  mockedTestRunWorkflowStep,
  postedChangeSets,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

vi.setConfig({ testTimeout: 15_000 });

const contextualPlayHitTestDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
beforeAll(() => Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [] }));
afterAll(() => {
  if (contextualPlayHitTestDescriptor) Object.defineProperty(document, "elementsFromPoint", contextualPlayHitTestDescriptor);
  else Reflect.deleteProperty(document, "elementsFromPoint");
});

function installWorkflow(policy: Record<string, unknown>) {
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
    }),
  );
  mockedFetchWorkflowExportPolicy.mockResolvedValue({ workflow_id: "weekly_report", ...policy } as never);
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
}

async function openStepTestRun() {
  renderPage({ surface: "builder" });
  await waitFor(() => expect((globalThis as unknown as Window).editStep).toBeTypeOf("function"));
  act(() => {
    (globalThis as unknown as Window).editStep?.("plan");
  });
  fireEvent.click(await screen.findByRole("tab", { name: "Test Run" }));
}

describe("the step Test Run of a protected extension workflow", () => {
  it("keeps a licensed installed source read-only while allowing isolated step tests", async () => {
    installWorkflow({
      extension_owned: true,
      protected: false,
      full_export_allowed: true,
      clone_allowed: true,
      structure_change_allowed: false,
      step_test_run_allowed: true,
    });
    const user = userEvent.setup();
    renderPage({ surface: "builder" });

    expect(await screen.findByTestId("structure-locked")).toHaveTextContent("Read-only");
    expect(screen.getByRole("note", { name: "Structure read-only (extension)" })).not.toHaveTextContent(/cannot be edited or run one by one/i);
    expect(screen.queryByRole("button", { name: "Add step" })).not.toBeInTheDocument();
    const card = await screen.findByRole("group", { name: "Open step Plan work" });
    await user.hover(card);
    expect(within(card).getByRole("button", { name: "Test step" })).toBeEnabled();
    expect(postedChangeSets()).toEqual([]);
  });

  const protectedPolicy = {
    extension_owned: true,
    protected: true,
    structure_change_allowed: false,
    step_test_run_allowed: false,
    reference_export_allowed: true,
  };

  it("is not offered in the workflow Test Run drawer, which keeps its own Test Run", async () => {
    installWorkflow(protectedPolicy);
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?drawer=test-run" });

    // B28: "Run step test run" sat in this drawer's DOM, and the server answered 403.
    expect(await screen.findByRole("button", { name: "Run workflow test run" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Run step test run" })).not.toBeInTheDocument();
  });

  it("is disabled with the reason wherever the policy forbids it", async () => {
    // Structure that may change but steps that may not run alone: the reason
    // is the policy's, not the read-only canvas's.
    installWorkflow({ structure_change_allowed: true, step_test_run_allowed: false });
    await openStepTestRun();

    expect(await screen.findByTestId("step-test-run-blocked")).toHaveTextContent(
      "A step of a protected workflow does not run on its own. Run the whole workflow with Test Run, or test your own workflow in the hook panel.",
    );
    expect(screen.getByRole("button", { name: "Run step test run" })).toBeDisabled();
  });

  it("stays available on an ordinary workflow", async () => {
    installWorkflow({ structure_change_allowed: true, step_test_run_allowed: true, full_export_allowed: true });
    await openStepTestRun();

    expect(await screen.findByRole("button", { name: "Run step test run" })).toBeInTheDocument();
    expect(screen.queryByTestId("step-test-run-blocked")).not.toBeInTheDocument();
  });
});


describe("contextual Play follows public isolated permission separately from authoring", () => {
  it.each([
    { name: "protected read-only author", policy: { extension_owned: true, protected: true, structure_change_allowed: false, step_test_run_allowed: false } },
    { name: "editable structure with isolated execution forbidden", policy: { structure_change_allowed: true, step_test_run_allowed: false } },
  ])("offers no enabled contextual execution for $name", async ({ policy }) => {
    installWorkflow(policy);
    const user = userEvent.setup();
    renderPage({ surface: "builder" });
    const card = await screen.findByRole("group", { name: "Open step Plan work" });
    await user.hover(card);
    const play = within(card).queryByRole("button", { name: "Test step" });
    if (play) expect(play).toBeDisabled();
    // Locked inspection stays read-only; editable-but-denied inspection keeps its refusal explanation.
    // Vitest aliases window/defaultView to its global; d3 needs the existing JSDOM Window.
    const nativeRealm = (globalThis as typeof globalThis & { jsdom?: { window: Window } }).jsdom?.window;
    if (!nativeRealm) throw new Error("Selected-card mouse fixture requires Vitest's existing JSDOM Window.");
    fireEvent.mouseDown(card, { view: nativeRealm });
    fireEvent.mouseUp(card, { view: nativeRealm });
    fireEvent.click(card, { view: nativeRealm });
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    if (policy.structure_change_allowed === false) {
      expect(within(card).queryByRole("button", { name: "Test step" })).not.toBeInTheDocument();
      expect(within(drawer).queryByRole("tab", { name: "Test Run" })).not.toBeInTheDocument();
      expect(within(drawer).getByRole("tab", { name: "Smart setup" })).toHaveAttribute("aria-selected", "true");
      expect(within(drawer).getByTestId("protected-step-view")).toBeVisible();
      expect(within(drawer).queryByRole("textbox", { name: "Step label" })).not.toBeInTheDocument();
      expect(within(drawer).queryByRole("button", { name: "Save parameters" })).not.toBeInTheDocument();
    } else {
      await user.click(within(drawer).getByRole("tab", { name: "Test Run" }));
      expect(await within(drawer).findByTestId("step-test-run-blocked")).toHaveTextContent("A step of a protected workflow does not run on its own.");
      expect(within(drawer).getByRole("button", { name: "Run step test run" })).toBeDisabled();
    }
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(postedChangeSets()).toEqual([]);
  });

  it("uses an explicit published isolated permission on a live read-only author graph without exposing editing", async () => {
    installWorkflow({ extension_owned: true, protected: true, structure_change_allowed: false, step_test_run_allowed: true, reference_export_allowed: true });
    const user = userEvent.setup();
    renderPage({ surface: "builder" });
    const card = await screen.findByRole("group", { name: "Open step Plan work" });
    await user.hover(card);
    const play = await within(card).findByRole("button", { name: "Test step" });
    expect(play).toBeEnabled();
    expect(within(card).queryByRole("button", { name: "More actions for Plan work" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add step" })).not.toBeInTheDocument();
    // Keep this real permission-test control focused while user-event moves its pointer.
    act(() => play.focus());
    expect(play).toHaveFocus();
    await user.click(play);
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    expect(within(drawer).getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
    expect(within(drawer).queryByTestId("step-test-run-blocked")).not.toBeInTheDocument();
    expect(within(drawer).getByRole("button", { name: "Run step test run" })).toBeEnabled();
    await user.click(within(drawer).getByRole("tab", { name: "Smart setup" }));
    expect(within(drawer).getByTestId("protected-step-view")).toBeVisible();
    expect(within(drawer).queryByRole("textbox", { name: "Step label" })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Save parameters" })).not.toBeInTheDocument();
    expect(mockedTestRunWorkflowStep).not.toHaveBeenCalled();
    expect(mockedTestRunWorkflow).not.toHaveBeenCalled();
    expect(postedChangeSets()).toEqual([]);
  });
});
