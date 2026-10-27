import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { canvasGraphFixture, mockedFetchWorkflowCanvasGraph, mockedFetchWorkflowDetail, mockedFindWorkflowLinkedProjectId, renderPage, workflowDetailFixture } from "./index.test-helpers";

// These page journeys take about 6 s when the quality gate shares the machine,
// over vitest's 5 s default. Per-test timeouts set below still take precedence.
vi.setConfig({ testTimeout: 10_000 });

function installWorkflow() {
  mockedFindWorkflowLinkedProjectId.mockResolvedValue("project_alpha");
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({
    revision: 5,
    steps: [{ step_id: "wait", display_name: "Wait", step_kind: "wait", override: { wait: { duration_ms: 15000 } } }, { step_id: "done", display_name: "Done", step_kind: "stop_success" }],
    edges: [{ source: { kind: "step", id: "wait" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } }],
  }));
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
    entry: { kind: "step", id: "wait" },
    steps: [
      { id: "wait", step_id: "wait", kind: "wait", display_name: "Wait", config_summary: "15 seconds", ports: [{ outcome: { name: "success" }, label: "Next" }] },
      { id: "done", step_id: "done", kind: "stop_success", display_name: "Done", config_summary: "", ports: [] },
    ],
    edges: [{ source: { kind: "step", id: "wait" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } }],
  }));
}

describe("unsaved builder forms", () => {
  it("keeps step parameters after Close and reopening, and still blocks workflow Test Run", async () => {
    installWorkflow();
    renderPage({ surface: "builder" });
    await screen.findByRole("button", { name: "Weekly Report" });
    act(() => window.editStep?.("wait"));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Wait"));
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Unsaved wait" } });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    act(() => window.editStep?.("wait"));
    expect(await screen.findByRole("textbox", { name: "Step label" })).toHaveValue("Unsaved wait");
    act(() => window.editStep?.("done"));
    const discard = await screen.findByRole("dialog", { name: "Discard unsaved parameters?" });
    fireEvent.click(within(discard).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Unsaved wait");
    fireEvent.click(screen.getByRole("link", { name: "Test Run" }));
    expect(await screen.findByTestId("workflow-test-run-blocked")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run workflow test run" })).toBeDisabled();
  }, 10_000);
  it("asks before discarding extra edits to a new step already in the structural batch", async () => {
    installWorkflow();
    renderPage({ surface: "builder" });
    await screen.findByRole("button", { name: "Weekly Report" });
    fireEvent.click(await screen.findByRole("button", { name: "Add step" }));
    const palette = await screen.findByRole("dialog", { name: "Add step" });
    fireEvent.click(within(palette).getByRole("option", { name: "Stop · success" }));
    await screen.findByRole("heading", { name: "Step editor" });
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Local draft" } });
    expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("stop_success");
    fireEvent.click(screen.getByRole("button", { name: "Update new step" }));
    await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied"));
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Edited local draft" } });
    act(() => window.editStep?.("wait"));
    const discard = await screen.findByRole("dialog", { name: "Discard unsaved parameters?" });
    fireEvent.click(within(discard).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Edited local draft");
  });

});
