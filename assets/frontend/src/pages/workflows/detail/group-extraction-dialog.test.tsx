import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { GroupExtractionDialog } from "./group-extraction-dialog";

const preview = {
  can_convert: true, issues: [], revision: 7, preview_digest: "verified-plan",
  child_workflow_id: "source_process", display_name: "Process", invocation_step_id: "invoke_process",
  interface: { expected_inputs: [{ name: "source_items", type: "array" }], expected_outputs: [{ name: "result", type: "string" }] },
  input_mapping: { source_items: "state.items" }, output_mapping: { result: "state.result" }, step_count: 2,
};

function setup(extra = {}) {
  const onApplied = vi.fn();
  const onReload = vi.fn().mockResolvedValue(undefined);
  const props = { workflowId: "source", groupId: "process", groupName: "Process", revision: 7, blockedReason: "", onClose: vi.fn(), onApplied, onReload, ...extra };
  const view = render(<GroupExtractionDialog {...props} />);
  return { ...view, props, onApplied, onReload };
}

beforeEach(() => {
  vi.spyOn(HTMLDialogElement.prototype, "showModal").mockImplementation(function (this: HTMLDialogElement) { this.setAttribute("open", ""); });
  vi.spyOn(HTMLDialogElement.prototype, "close").mockImplementation(function (this: HTMLDialogElement) { this.removeAttribute("open"); });
});

it("previews typed mappings before applying the exact reviewed plan and linking the child", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify(preview)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...preview, applied: true, revision: 8, child_revision: 1 })));
  const { onApplied } = setup();
  expect(screen.getByRole("button", { name: "Create child workflow" })).toBeDisabled();
  expect(await screen.findByText("state.items")).toBeInTheDocument();
  expect(screen.getByText("array")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Create child workflow" }));
  expect(await screen.findByRole("link", { name: "Open child workflow" })).toHaveAttribute("href", "/registry/workflows/source_process/builder");
  expect(onApplied).toHaveBeenCalledOnce();
  const body = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body));
  expect(body).toMatchObject({ expected_revision: 7, preview_digest: "verified-plan", dry_run: false });
});

it("checks on open and shows exact boundary blockers as an error, never enabling apply", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ ...preview, can_convert: false, issues: [{ code: "unsafe_stop", path: "steps.stop", message: "Stop would terminate another workflow." }] })));
  setup();
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Can't create the child workflow yet");
  expect(alert).toHaveTextContent("Stop would terminate another workflow.");
  expect(alert).toHaveTextContent("In step stop");
  const create = screen.getByRole("button", { name: "Create child workflow" });
  expect(create).toBeDisabled();
  expect(create).toHaveAttribute("title", "Fix the issues above first.");
});

it("says what to do about an input with no type, in plain words", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ ...preview, can_convert: false, issues: [{
    code: "unknown_input_type", path: "steps.transform.override.transform.input.source",
    message: "No unambiguous transferable type is declared for state.inputs.payload.", subject: "state.inputs.payload",
  }] })));
  setup();
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("The group reads the workflow input “payload”, which has no type. Set its type in Workflow settings → Inputs, then preview again.");
  expect(alert).toHaveTextContent("In step transform");
  expect(alert).not.toHaveTextContent("No unambiguous transferable type");
});

it("invalidates the preview when the child name changes or local drafts appear", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(preview)));
  const { rerender, props } = setup();
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Child workflow name"), { target: { value: "Another child" } });
  expect(screen.getByRole("button", { name: "Create child workflow" })).toBeDisabled();
  rerender(<GroupExtractionDialog {...props} blockedReason="Save group parameters first." />);
  expect(screen.getByText("Save group parameters first.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Preview conversion" })).toBeDisabled();
});

it("clears a conflicting preview and reloads before allowing a new one", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify(preview)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: "workflow_revision_conflict", message: "Workflow changed." } }), { status: 409 }));
  const { onReload } = setup();
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Create child workflow" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Preview the conversion again");
  expect(onReload).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "Create child workflow" })).toBeDisabled();
});

it("names the step and the field that read all inputs, and opens that step", async () => {
  // Owner feedback: "read only the values it needs" read as "read-only"; say what to change and where.
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ ...preview, can_convert: false, issues: [{
    code: "whole_state_input", path: "steps.build_result.override.transform.input.source",
    message: "Select an individual workflow input instead of the entire input object.", subject: "state.inputs",
  }] })));
  const onOpenStep = vi.fn();
  setup({ stepName: (id: string) => (id === "build_result" ? "Build the result" : id), onOpenStep });
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("The step “Build the result” takes all of the workflow’s inputs at once in its “Source” field (state.inputs).");
  expect(alert).not.toHaveTextContent("read only");
  fireEvent.click(screen.getByRole("button", { name: "Open step" }));
  expect(onOpenStep).toHaveBeenCalledWith("build_result");
});


it("asks before discarding an edited identity and keeps it when canceled", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(preview)));
  const { props } = setup();
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.change(screen.getByRole("textbox", { name: "Child workflow name" }), { target: { value: "Edited child" } });
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(screen.getByRole("dialog", { name: "Discard child workflow draft?" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Discard child workflow draft?" })).not.toBeInTheDocument());
  expect(screen.getByRole("textbox", { name: "Child workflow name" })).toHaveValue("Edited child");
  expect(props.onClose).not.toHaveBeenCalled();
});

it("announces pending then a valid zero-field preview in a stable region", async () => {
  let resolvePreview!: (response: Response) => void;
  vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>((resolve) => { resolvePreview = resolve; }));
  setup();
  expect(screen.getByRole("status", { name: "Conversion preview status" })).toHaveTextContent("Checking conversion");
  const region = screen.getByRole("region", { name: "Conversion preview" });
  expect(region).toBeInTheDocument();
  resolvePreview(new Response(JSON.stringify({ ...preview, interface: { expected_inputs: [], expected_outputs: [] }, input_mapping: {}, output_mapping: {} })));
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  expect(screen.getByRole("status", { name: "Conversion preview status" })).toHaveTextContent("Preview ready");
  expect(region).toHaveTextContent("Inputs");
  expect(region).toHaveTextContent("Outputs");
});

it("names each exact refused nested target and callable output without inventing a writer", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ ...preview, can_convert: false, issues: [
    { code: "partial_state_write", path: "steps.build_result.outputs.customer.response", message: "Nested write refused." },
    { code: "partial_state_write", path: "steps.build_result.outputs.order.total", message: "Nested write refused." },
    { code: "parent_output_boundary", path: "settings.callable.expected_outputs.summary", message: "Output boundary." },
    { code: "parent_output_boundary", path: "settings.callable.expected_outputs.report", message: "Output boundary." },
  ] })));
  setup({ stepName: (id: string) => id === "build_result" ? "Build the result" : id });
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("state.customer.response");
  expect(alert).toHaveTextContent("state.order.total");
  expect(alert).toHaveTextContent("summary");
  expect(alert).toHaveTextContent("report");
  expect(alert).toHaveTextContent("In step Build the result");
  expect(alert).not.toHaveTextContent("In step summary");
  expect(alert).not.toHaveTextContent("In step report");
  expect(screen.getByRole("button", { name: "Create child workflow" })).toBeDisabled();
});

it.each(["footer", "header", "backdrop", "Escape"] as const)("confirms dirty %s dismissal, preserving the identity on cancel and closing once on discard", async (path) => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(preview)));
  const { props } = setup();
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  const previewCalls = fetchMock.mock.calls.length;
  fireEvent.change(screen.getByRole("textbox", { name: "Child workflow ID" }), { target: { value: "manual_child" } });
  const attempt = () => {
    if (path === "Escape") fireEvent.keyDown(document, { key: "Escape" });
    else fireEvent.click(screen.getByRole("button", { name: path === "footer" ? "Close" : path === "header" ? "Close Convert group to child workflow" : "Dismiss Convert group to child workflow" }));
  };
  attempt();
  expect(screen.getByRole("dialog", { name: "Discard child workflow draft?" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Discard child workflow draft?" })).not.toBeInTheDocument());
  expect(screen.getByRole("textbox", { name: "Child workflow ID" })).toHaveValue("manual_child");
  expect(screen.getByRole("button", { name: "Create child workflow" })).toBeDisabled();
  expect(props.onClose).not.toHaveBeenCalled();
  attempt();
  fireEvent.click(screen.getByRole("button", { name: "Discard draft" }));
  await waitFor(() => expect(props.onClose).toHaveBeenCalledOnce());
  expect(fetchMock).toHaveBeenCalledTimes(previewCalls);
});

it("confirms Open step with dirty identity and navigates only after discard", async () => {
  const refusal = { ...preview, can_convert: false, issues: [{ code: "whole_state_input", path: "steps.build_result.override.transform.input.source", subject: "state.inputs", message: "Whole input refused." }] };
  vi.spyOn(globalThis, "fetch").mockImplementation(() => Promise.resolve(new Response(JSON.stringify(refusal))));
  const onOpenStep = vi.fn();
  const { props } = setup({ onOpenStep });
  await screen.findByRole("alert");
  fireEvent.change(screen.getByRole("textbox", { name: "Child workflow name" }), { target: { value: "Changed" } });
  fireEvent.click(screen.getByRole("button", { name: "Preview conversion" }));
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Open step" }));
  expect(screen.getByRole("dialog", { name: "Discard child workflow draft?" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Discard child workflow draft?" })).not.toBeInTheDocument());
  expect(onOpenStep).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Open step" }));
  fireEvent.click(screen.getByRole("button", { name: "Discard draft" }));
  await waitFor(() => expect(onOpenStep).toHaveBeenCalledOnce());
  expect(onOpenStep).toHaveBeenCalledWith("build_result");
  expect(props.onClose).not.toHaveBeenCalled();
});

it.each([
  { workflowId: "other" },
  { groupId: "other" },
  { revision: 8 },
])("does not dismiss a different current owner after deferred confirmation: %o", async (change) => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(preview)));
  const { rerender, props } = setup();
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.change(screen.getByRole("textbox", { name: "Child workflow name" }), { target: { value: "Changed" } });
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  rerender(<GroupExtractionDialog {...props} {...change} />);
  fireEvent.click(screen.getByRole("button", { name: "Discard draft" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Discard child workflow draft?" })).not.toBeInTheDocument());
  expect(props.onClose).not.toHaveBeenCalled();
});

it("closes untouched and completed dialogs without a discard prompt", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(preview)));
  const { props } = setup();
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(props.onClose).toHaveBeenCalledOnce();
  expect(screen.queryByRole("dialog", { name: "Discard child workflow draft?" })).not.toBeInTheDocument();
});

it("keeps in-flight conversion protected from close and Escape", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation(() => new Promise<Response>(() => {}));
  const { props } = setup();
  expect(screen.getByRole("button", { name: "Close" })).toBeDisabled();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(props.onClose).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog", { name: "Discard child workflow draft?" })).not.toBeInTheDocument();
  expect(screen.getByText("Wait for the current request to finish.")).toBeInTheDocument();
});

it("keeps child ID independent, marks both edits stale, and applies only the re-previewed digest and identity", async () => {
  const revised = { ...preview, preview_digest: "reviewed-again", child_workflow_id: "manual_child", display_name: "New name" };
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify(preview)))
    .mockResolvedValueOnce(new Response(JSON.stringify(revised)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...revised, applied: true, revision: 8 })));
  setup();
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  const initialCalls = fetchMock.mock.calls.length;
  const childId = screen.getByRole("textbox", { name: "Child workflow ID" });
  expect(childId).toHaveValue("source_process");
  expect(screen.getByText(/independent of the child name/)).toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Child workflow name" }), { target: { value: "New name" } });
  expect(childId).toHaveValue("source_process");
  fireEvent.change(childId, { target: { value: "manual_child" } });
  expect(screen.getByRole("status", { name: "Conversion preview status" })).toHaveTextContent("Preview out of date");
  expect(screen.getByText("The preview is out of date. Preview the conversion again.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Create child workflow" })).toBeDisabled();
  expect(fetchMock).toHaveBeenCalledTimes(initialCalls);
  fireEvent.click(screen.getByRole("button", { name: "Preview conversion" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Create child workflow" }));
  await screen.findByRole("link", { name: "Open child workflow" });
  expect(JSON.parse(String(fetchMock.mock.calls.at(-2)?.[1]?.body))).toMatchObject({ child_workflow_id: "manual_child", display_name: "New name", expected_revision: 7, dry_run: true });
  expect(JSON.parse(String(fetchMock.mock.calls.at(-1)?.[1]?.body))).toMatchObject({ child_workflow_id: "manual_child", display_name: "New name", expected_revision: 7, dry_run: false, preview_digest: "reviewed-again" });
});

it("invalidates the digest on server revision change and re-previews at the new revision", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify(preview)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...preview, revision: 8, preview_digest: "revision-eight" })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...preview, revision: 9, applied: true })));
  const { rerender, props } = setup();
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  rerender(<GroupExtractionDialog {...props} revision={8} />);
  expect(screen.getByRole("button", { name: "Create child workflow" })).toBeDisabled();
  expect(screen.getByRole("status", { name: "Conversion preview status" })).toHaveTextContent("Preview out of date");
  fireEvent.click(screen.getByRole("button", { name: "Preview conversion" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Create child workflow" }));
  await screen.findByRole("link", { name: "Open child workflow" });
  expect(JSON.parse(String(fetchMock.mock.calls.at(-2)?.[1]?.body))).toMatchObject({ expected_revision: 8, dry_run: true });
  expect(JSON.parse(String(fetchMock.mock.calls.at(-1)?.[1]?.body))).toMatchObject({ expected_revision: 8, dry_run: false, preview_digest: "revision-eight" });
});

it("shows the actual Create blocker for invalid name, ID and revision", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(preview)));
  const { rerender, props } = setup();
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.change(screen.getByRole("textbox", { name: "Child workflow name" }), { target: { value: "" } });
  expect(screen.getByText("Enter a child workflow name of 1–200 characters.")).toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Child workflow name" }), { target: { value: "Valid name" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Child workflow ID" }), { target: { value: "Invalid-ID" } });
  expect(screen.getByText(/Use an ID starting with a lowercase letter/)).toBeInTheDocument();
  rerender(<GroupExtractionDialog {...props} revision={0} />);
  expect(screen.getByText("Reload the workflow before creating a child.")).toBeInTheDocument();
});

it("names a returned-file step but leaves an unproved nested target on its server text", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ ...preview, can_convert: false, issues: [
    { code: "parent_output_boundary", path: "steps.build_result.override.returned_files", message: "File refused." },
    { code: "partial_state_write", path: "settings.unknown", message: "Unknown target refused." },
  ] })));
  setup({ stepName: (id: string) => id === "build_result" ? "Build the result" : id });
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("The step “Build the result” returns a file");
  expect(alert).toHaveTextContent("Unknown target refused.");
  expect(alert).not.toHaveTextContent("state.unknown");
});


it("keeps completed feedback truthful after onApplied refreshes the workflow revision", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify(preview)))
    .mockResolvedValueOnce(new Response(JSON.stringify({ ...preview, applied: true, revision: 8, child_revision: 1 })));
  let refreshRevision = () => {};
  const onApplied = vi.fn(async () => { refreshRevision(); });
  const { rerender, props } = setup({ onApplied });
  refreshRevision = () => rerender(<GroupExtractionDialog {...props} revision={8} />);
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  const beforeApply = fetchMock.mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: "Create child workflow" }));
  expect(await screen.findByRole("link", { name: "Open child workflow" })).toBeInTheDocument();
  expect(onApplied).toHaveBeenCalledOnce();
  expect(fetchMock).toHaveBeenCalledTimes(beforeApply + 1);
  expect(screen.queryByRole("region", { name: "Conversion preview" })).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Child workflow created.");
  expect(screen.getByRole("status")).not.toHaveTextContent("Preview out of date");
  fireEvent.click(screen.getByRole("button", { name: "Close" }));
  expect(props.onClose).toHaveBeenCalledOnce();
  expect(screen.queryByRole("dialog", { name: "Discard child workflow draft?" })).not.toBeInTheDocument();
});


it("reconciles a confirmed apply when the same workflow and group revision advances while awaiting its response", async () => {
  let resolveApply!: (response: Response) => void;
  const fetchMock = vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify(preview)))
    .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveApply = resolve; }));
  const onApplied = vi.fn().mockResolvedValue(undefined);
  const { rerender, props } = setup({ onApplied });
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Create child workflow" }));
  expect(screen.getByRole("status", { name: "Conversion preview status" })).toHaveTextContent("Creating child workflow");
  rerender(<GroupExtractionDialog {...props} revision={8} />);
  resolveApply(new Response(JSON.stringify({ ...preview, applied: true, revision: 8, child_revision: 1 })));
  expect(await screen.findByRole("link", { name: "Open child workflow" })).toBeInTheDocument();
  expect(onApplied).toHaveBeenCalledOnce();
  expect(screen.queryByRole("region", { name: "Conversion preview" })).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Child workflow created.");
  expect(JSON.parse(String(fetchMock.mock.calls.at(-1)?.[1]?.body))).toMatchObject({ expected_revision: 7, dry_run: false, preview_digest: "verified-plan" });
});

it.each([{ workflowId: "other" }, { groupId: "other" }])("does not reconcile a confirmed apply into a different current owner: %o", async (change) => {
  let resolveApply!: (response: Response) => void;
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify(preview)))
    .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveApply = resolve; }));
  const onApplied = vi.fn().mockResolvedValue(undefined);
  const { rerender, props } = setup({ onApplied });
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Create child workflow" }));
  rerender(<GroupExtractionDialog {...props} {...change} />);
  resolveApply(new Response(JSON.stringify({ ...preview, applied: true, revision: 8, child_revision: 1 })));
  await waitFor(() => expect(screen.getByRole("button", { name: "Preview conversion" })).toBeEnabled());
  expect(onApplied).not.toHaveBeenCalled();
  expect(screen.queryByRole("link", { name: "Open child workflow" })).not.toBeInTheDocument();
});

it("does not reconcile a confirmed apply after this dialog unmounts", async () => {
  let resolveApply!: (response: Response) => void;
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(new Response(JSON.stringify(preview)))
    .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveApply = resolve; }));
  const onApplied = vi.fn().mockResolvedValue(undefined);
  const { unmount } = setup({ onApplied });
  await waitFor(() => expect(screen.getByRole("button", { name: "Create child workflow" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "Create child workflow" }));
  unmount();
  resolveApply(new Response(JSON.stringify({ ...preview, applied: true, revision: 8, child_revision: 1 })));
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(onApplied).not.toHaveBeenCalled();
});
