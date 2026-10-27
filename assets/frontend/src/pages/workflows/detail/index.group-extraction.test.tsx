import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { fetchSpy, mockedFetchWorkflowDetail, renderPage, workflowDetailFixture } from "./index.test-helpers";

it("opens conversion for a saved group with its revision, but blocks unsaved group settings", async () => {
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5,
    groups: [{ group_id: "items", display_name: "Items", body: { kind: "sequence" } }],
    steps: [{ step_id: "done", step_kind: "stop_success" }],
  }));
  const fallback = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation((input, init) => {
    if (String(input).endsWith("/groups/items/convert-to-child")) {
      const body = JSON.parse(String(init?.body));
      expect(body).toMatchObject({ expected_revision: 5, dry_run: true, display_name: "Items" });
      return Promise.resolve(new Response(JSON.stringify({ can_convert: false, issues: [{ code: "empty_group", path: "groups.items", message: "The group has no steps." }], revision: 5, preview_digest: "digest", child_workflow_id: body.child_workflow_id, display_name: "Items", interface: {}, input_mapping: {}, output_mapping: {}, step_count: 0 })));
    }
    return fallback(input, init);
  });
  renderPage({ surface: "builder" });
  await waitFor(() => expect(window.openGroupDrawer).toBeTypeOf("function"));
  act(() => window.openGroupDrawer?.("items"));
  const name = await screen.findByRole("textbox", { name: "Display name" });
  await waitFor(() => expect(name).toHaveValue("Items"));
  const convert = screen.getByRole("button", { name: "Convert group to child workflow" });
  expect(convert).toBeEnabled();
  fireEvent.change(name, { target: { value: "Unsaved" } });
  expect(convert).toBeDisabled();
  fireEvent.change(name, { target: { value: "Items" } });
  expect(convert).toBeEnabled();
  fireEvent.click(convert);
  const dialog = await screen.findByRole("dialog", { name: "Convert group to child workflow" });
  // The dialog checks the conversion as it opens.
  expect(await within(dialog).findByText("The group has no steps.")).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Create child workflow" })).toBeDisabled();
  fireEvent.change(within(dialog).getByRole("textbox", { name: "Child workflow name" }), { target: { value: "Edited Items" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
  expect(screen.getByRole("dialog", { name: "Discard child workflow draft?" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
  expect(within(dialog).getByRole("textbox", { name: "Child workflow name" })).toHaveValue("Edited Items");
});

it("converts a plain Group from its right-click menu: preview, then create the child and reload the parent", async () => {
  mockedFetchWorkflowDetail.mockResolvedValue(workflowDetailFixture({ revision: 5,
    groups: [{ group_id: "prepare", display_name: "Prepare order", body: { kind: "sequence", entry: { kind: "step", id: "tidy" } } }],
    steps: [
      { step_id: "tidy", step_kind: "set_variable", display_name: "Tidy fields", group_id: "prepare", override: { set_variable: { assignments: { x: "1" } } } },
      { step_id: "done", step_kind: "stop_success" },
    ],
    entry: { kind: "group", id: "prepare" },
    edges: [
      { source: { kind: "step", id: "tidy" }, outcome: { name: "success" }, target: { kind: "group_exit", group_id: "prepare", lane_id: null } },
      { source: { kind: "group", id: "prepare" }, outcome: { name: "success" }, target: { kind: "step", id: "done" } },
    ],
  }));
  const bodies: Array<Record<string, unknown>> = [];
  const fallback = fetchSpy.getMockImplementation()!;
  fetchSpy.mockImplementation((input, init) => {
    if (String(input).endsWith("/groups/prepare/convert-to-child")) {
      const body = JSON.parse(String(init?.body));
      bodies.push(body);
      const result = { can_convert: true, issues: [], revision: body.dry_run ? 5 : 6, preview_digest: "digest-1", child_workflow_id: body.child_workflow_id, display_name: body.display_name, invocation_step_id: "invoke_prepare", interface: { expected_inputs: [], expected_outputs: [{ name: "x", type: "string" }] }, input_mapping: {}, output_mapping: { x: "state.x" }, step_count: 1 };
      return Promise.resolve(new Response(JSON.stringify(body.dry_run ? result : { ...result, applied: true, child_revision: 1 })));
    }
    return fallback(input, init);
  });
  renderPage({ surface: "builder" });
  const card = await screen.findByText("Prepare order", { selector: ".workflow-container-title span" });
  fireEvent.contextMenu(card);
  fireEvent.click(await screen.findByRole("menuitem", { name: "Convert to child workflow…" }));
  const dialog = await screen.findByRole("dialog", { name: "Convert group to child workflow" });
  expect(within(dialog).getByRole("textbox", { name: "Child workflow name" })).toHaveValue("Prepare order");
  // The dialog checks the conversion as it opens.
  expect(await within(dialog).findByText("state.x")).toBeInTheDocument();
  const detailCalls = mockedFetchWorkflowDetail.mock.calls.length;
  fireEvent.click(within(dialog).getByRole("button", { name: "Create child workflow" }));
  expect(await within(dialog).findByText("Child workflow created.")).toBeInTheDocument();
  expect(bodies).toEqual([
    { child_workflow_id: "weekly_report_prepare", display_name: "Prepare order", expected_revision: 5, dry_run: true },
    { child_workflow_id: "weekly_report_prepare", display_name: "Prepare order", expected_revision: 5, dry_run: false, preview_digest: "digest-1" },
  ]);
  await waitFor(() => expect(mockedFetchWorkflowDetail.mock.calls.length).toBeGreaterThan(detailCalls));
  expect(within(dialog).getByRole("link", { name: "Open child workflow" })).toHaveAttribute("href", expect.stringContaining("/registry/workflows/weekly_report_prepare/builder"));
});
