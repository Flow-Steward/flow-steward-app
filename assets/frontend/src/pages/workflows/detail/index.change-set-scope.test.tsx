import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import {
  canvasGraphFixture,
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail,
  renderPage,
  workflowDetailFixture,
} from "./index.test-helpers";

async function prepareStopFromCanvas(label: string) {
  fireEvent.click(await screen.findByRole("button", { name: "Add step" }));
  const palette = await screen.findByRole("dialog", { name: "Add step" });
  fireEvent.click(within(palette).getByRole("option", { name: "Stop · success" }));
  await screen.findByRole("heading", { name: "Step editor" });
  fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: label } });
  expect(screen.getByRole("combobox", { name: "Step kind" })).toHaveValue("stop_success");
}

it("discards source-only structure when the author confirms navigation to another workflow", async () => {
  mockedFetchWorkflowDetail.mockImplementation(async (workflowId) => workflowDetailFixture({
    workflow_id: workflowId,
    display_name: workflowId === "weekly_report" ? "Source workflow" : "Destination workflow",
    enabled: false,
    revision: workflowId === "weekly_report" ? 7 : 21,
  }));
  mockedFetchWorkflowCanvasGraph.mockImplementation(async (workflowId) => canvasGraphFixture({
    workflow: { id: workflowId, display_name: workflowId === "weekly_report" ? "Source workflow" : "Destination workflow", enabled: false, callable_by_parent: false },
  }));
  const fallback = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation(async (input, init) => {
    if (String(input).endsWith("/changes")) {
      const body = JSON.parse(String(init?.body));
      return new Response(JSON.stringify({ revision: body.expected_revision, valid: true, applied: false, issues: [], blocking_issues: [], workflow: {} }), { status: 200, headers: { "content-type": "application/json" } });
    }
    return fallback(input, init);
  });
  const { router } = renderPage({ surface: "builder" });
  await prepareStopFromCanvas("Source only draft");
  fireEvent.click(screen.getByRole("button", { name: "Update new step" }));
  await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure not applied: 1 change(s)"));

  await act(async () => { await router.navigate("/registry/workflows/destination/builder"); });
  fireEvent.click(within(await screen.findByRole("dialog", { name: "Leave the builder?" })).getByRole("button", { name: "Leave and discard" }));
  await waitFor(() => expect(router.state.location.pathname).toBe("/registry/workflows/destination/builder"));
  await screen.findAllByText("Destination workflow");
  expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied");
  expect(screen.queryByRole("group", { name: "Open step Source only draft" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Apply changes" })).toBeDisabled();

  await prepareStopFromCanvas("Destination only draft");
  fireEvent.click(screen.getByRole("button", { name: "Update new step" }));
  await waitFor(() => {
    const requests = fetchSpy.mock.calls.filter(([url]) => String(url) === "/api/workflows/destination/changes");
    expect(requests.length).toBeGreaterThan(0);
    for (const [, init] of requests) {
      expect(JSON.parse(String(init?.body))).toMatchObject({
        expected_revision: 21,
        operations: [{ op: "create_step", step: { step_id: "stop_success", step_kind: "stop_success" } }],
      });
      expect(JSON.parse(String(init?.body)).operations).toHaveLength(1);
      expect(String(init?.body)).not.toContain("Source only draft");
    }
    // Palette selection creates immediately; its later parameter save keeps that same id.
    expect(JSON.parse(String(requests.at(-1)?.[1]?.body))).toMatchObject({
      expected_revision: 21,
      operations: [{ op: "create_step", step: { step_id: "stop_success", display_name: "Destination only draft" } }],
    });
  });
}, 10_000);

it.each(["Apply changes", "Discard changes"])("retires the owned new-step parameter notice after %s", async (action) => {
  let applied = false;
  mockedFetchWorkflowDetail.mockImplementation(async () => workflowDetailFixture({ revision: applied ? 8 : 7,
    steps: applied ? [{ step_id: "stop_success", step_kind: "stop_success", display_name: "Local Stop" }] : [] }));
  mockedFetchWorkflowCanvasGraph.mockImplementation(async () => canvasGraphFixture({ steps: applied
    ? [{ id: "stop_success", step_id: "stop_success", kind: "stop_success", display_name: "Local Stop", config_summary: "", ports: [] }] : [] }));
  const fallback = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation(async (input, init) => {
    if (String(input).endsWith("/changes")) {
      const body = JSON.parse(String(init?.body));
      if (!body.dry_run) applied = true;
      return new Response(JSON.stringify({ workflow_id: "weekly_report", revision: applied ? 8 : 7, valid: true,
        applied: !body.dry_run, issues: [], blocking_issues: [], workflow: {} }), { status: 200, headers: { "content-type": "application/json" } });
    }
    return fallback(input, init);
  });
  renderPage({ surface: "builder" });
  await prepareStopFromCanvas("Local Stop");
  fireEvent.click(await screen.findByRole("button", { name: "Update new step" }));
  const stale = "The new step's parameters are kept with the unapplied structure.";
  expect(await screen.findAllByText(stale)).not.toHaveLength(0);
  const button = await screen.findByRole("button", { name: action });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  await waitFor(() => expect(screen.getByTestId("structure-status")).toHaveTextContent("Structure applied"));
  // The drawer shows no persistent notice; the Save button's ✓ Updated announcement lasts a moment.
  await waitFor(() => expect(screen.queryByText(stale)).not.toBeInTheDocument(), { timeout: 3_000 });
  if (action === "Apply changes") {
    expect(applied).toBe(true);
    expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Local Stop");
  }
}, 15_000);
