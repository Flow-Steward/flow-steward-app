import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import type { WorkflowTestRunEvidence } from "./detail-test-run-evidence";
import type { RunPass } from "./detail-test-run-results-model";
import { MemoryRouter } from "react-router";
import { WorkflowTestRunResults as Results } from "./detail-test-run-results";
import { StepTestRunLatest } from "./detail-step-test-run-latest";

function WorkflowTestRunResults(props: Parameters<typeof Results>[0]) {
  return <MemoryRouter><Results {...props} /></MemoryRouter>;
}

function pass(overrides: Partial<RunPass>): RunPass {
  return {
    key: "p", stepId: "fetch", stepExecutionId: "p", label: "Fetch order", state: "done", durationMs: 400, place: "Workflow", itemHeader: "",
    kindLabel: "", stepKind: "http_request", inputs: { "inputs.order_id": "A-1001", "inputs.api_token": "[redacted]" },
    outputs: { "state.order": { id: "A-1001", total: 128.4 } }, message: "", error: "", decision: "", groupId: "", forEachId: "", laneId: "", iteration: null, outputsCut: false,
    ...overrides,
  };
}

function evidence(overrides: Partial<WorkflowTestRunEvidence> = {}): WorkflowTestRunEvidence {
  return {
    workflowId: "order_fulfilment", jobId: "run-2", verified: true, loading: false, error: "", isLatest: true, runNumber: 2,
    statusWord: "Finished", statusTone: "success", terminal: true,
    runs: [
      { jobId: "run-2", number: 2, status: "Finished", tone: "success", meta: "14 s · 15:07:43", label: "Run #2 · Finished · 14 s · 15:07:43" },
      { jobId: "run-1", number: 1, status: "Failed", tone: "danger", meta: "8.5 s · 15:07:36", label: "Run #1 · Failed · 8.5 s · 15:07:36" },
    ],
    runsLoading: false, runsDisabled: false, selectRun: vi.fn(),
    inputs: [{ name: "order_id", value: "A-1001", hidden: false }, { name: "api_token", value: "[redacted]", hidden: true }],
    passes: [
      pass({}),
      pass({ key: "c", stepId: "ship", stepExecutionId: "c", label: "Create shipment", state: "failed", place: "Fulfilment · item 1 of 2", itemHeader: "Fulfilment: item 1 of 2",
        forEachId: "fulfil", iteration: 0, error: "Needs a connection binding.", stepKind: "connector", outputs: {} }),
    ],
    counts: { done: 1, skipped: 0, failed: 1 }, duration: "14 s", approval: null, runOutputs: {}, runArtifacts: {}, executedRevision: 7, executedDigest: "abcdef0123456789",
    kept: new Map(), keptPending: false, keepOutput: vi.fn(async () => ""), clearOutput: vi.fn(async () => ""),
    openStep: vi.fn(), lightStep: vi.fn(), litPassKey: "", changeInputs: vi.fn(), runAgain: { onInvoke: vi.fn() }, refresh: vi.fn(),
    ...overrides,
  };
}

it("lists the kept test runs, the inputs with secrets hidden, the counts and one row per pass", () => {
  const value = evidence();
  render(<WorkflowTestRunResults evidence={value} />);
  expect(screen.getByRole("button", { name: "Run #2 · Finished · 14 s · 15:07:43" })).toHaveAttribute("aria-current", "true");
  fireEvent.click(screen.getByRole("button", { name: "Run #1 · Failed · 8.5 s · 15:07:36" }));
  expect(value.selectRun).toHaveBeenCalledWith("run-1");
  expect(screen.getByRole("heading", { name: /Latest test run/ })).toHaveTextContent("Finished");
  expect(screen.getByText(/Inputs:/)).toHaveTextContent("Inputs: order_id = A-1001 · api_token = hidden");
  expect(screen.getByText("Fulfilment: item 1 of 2")).toBeVisible();
  const rows = screen.getAllByTestId("workflow-run-pass");
  expect(within(rows[1]).getByText("Needs a connection binding.")).toBeVisible();
  expect(within(rows[1]).getByText("Failed")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Change inputs" }));
  fireEvent.click(screen.getByRole("button", { name: "Run again" }));
  expect(value.changeInputs).toHaveBeenCalledOnce();
  expect(value.runAgain?.onInvoke).toHaveBeenCalledOnce();
});

it("opens a row in place to its input and output, lights the step and keeps its output", async () => {
  const value = evidence();
  render(<WorkflowTestRunResults evidence={value} />);
  const [row] = screen.getAllByTestId("workflow-run-pass");
  fireEvent.click(within(row).getByRole("button", { name: /Fetch order/ }));
  expect(value.lightStep).toHaveBeenCalledWith(expect.objectContaining({ stepId: "fetch" }));
  expect(within(row).getByText("inputs.order_id")).toBeVisible();
  expect(within(row).getByText("hidden")).toBeVisible();
  expect(within(row).getByText("state.order")).toBeVisible();
  fireEvent.click(within(row).getByRole("button", { name: "Open step" }));
  expect(value.openStep).toHaveBeenCalledWith("fetch");
  fireEvent.click(within(row).getByRole("button", { name: "Keep as test output" }));
  await waitFor(() => expect(value.keepOutput).toHaveBeenCalled());
  expect(value.keepOutput).toHaveBeenCalledWith("fetch", { "state.order": { id: "A-1001", total: 128.4 } }, { kind: "run", jobId: "run-2" });
  expect(await within(row).findByText(/Kept as test output/)).toBeVisible();
  fireEvent.click(within(row).getByRole("button", { name: /Fetch order/ }));
  expect(value.lightStep).toHaveBeenLastCalledWith(null);
});

it("an earlier run is named by its number", () => {
  render(<WorkflowTestRunResults evidence={evidence({ isLatest: false, runNumber: 1, jobId: "run-1", statusWord: "Failed", statusTone: "danger" })} />);
  expect(screen.getByRole("heading", { name: /Test run #1/ })).toHaveTextContent("Failed");
});

it("a long value shows its start and Show all until Show less", () => {
  const long = { line_items: Array.from({ length: 8 }, (_, index) => ({ sku: `SKU-${index}`, qty: index })) };
  render(<StepTestRunLatest evidence={evidence({ passes: [pass({ outputs: { "state.order": long } })] })}
    step={{ stepId: "fetch", label: "Fetch order", kind: "http_request", local: false, declaredOutputs: ["state.order"] }} />);
  const toggle = screen.getByRole("button", { name: /^Show all · \d+ lines$/ });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(toggle);
  expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute("aria-expanded", "true");
});

it("the step's Test Run tab shows the latest pass and offers a kept test output", async () => {
  const value = evidence({ passes: [pass({ key: "1", durationMs: 300 }), pass({ key: "2", durationMs: 600 })] });
  render(<StepTestRunLatest evidence={value} step={{ stepId: "fetch", label: "Fetch order", kind: "http_request", local: false, declaredOutputs: ["state.order"] }} />);
  expect(screen.getByText("Done in 0.6 s")).toBeVisible();
  expect(screen.getByText("The latest of 2 passes in this run.")).toBeVisible();
  const kept = screen.getByTestId("workflow-kept-test-output");
  expect(within(kept).getByText(/In test runs this step does its work/)).toBeVisible();
  fireEvent.click(within(kept).getByRole("button", { name: "Keep the latest output" }));
  await waitFor(() => expect(value.keepOutput).toHaveBeenCalled());
  expect(value.keepOutput).toHaveBeenCalledWith("fetch", { "state.order": { id: "A-1001", total: 128.4 } }, { kind: "run", jobId: "run-2" });
  fireEvent.click(within(kept).getByRole("button", { name: "Write it yourself" }));
  const json = within(kept).getByRole("textbox", { name: "Values, as JSON" });
  expect(json).toHaveValue(JSON.stringify({ "state.order": null }, null, 2));
  fireEvent.change(json, { target: { value: "{" } });
  fireEvent.click(within(kept).getByRole("button", { name: "Save" }));
  expect(await within(kept).findByRole("alert")).toHaveTextContent("This is not valid JSON.");
  fireEvent.change(json, { target: { value: "{\"state.order\": {\"id\": \"A-2\"}}" } });
  fireEvent.click(within(kept).getByRole("button", { name: "Save" }));
  await waitFor(() => expect(value.keepOutput).toHaveBeenCalledTimes(2));
  expect(value.keepOutput).toHaveBeenLastCalledWith("fetch", { "state.order": { id: "A-2" } }, { kind: "manual" });
});

it("a kept output says it stands in, with Stop using it; control steps always run", async () => {
  const value = evidence({ kept: new Map([["fetch", { step_id: "fetch", outputs: { "state.order": { id: "A-9" } }, source_kind: "manual", source_job_id: "" }]]) });
  const view = render(<StepTestRunLatest evidence={value} step={{ stepId: "fetch", label: "Fetch order", kind: "http_request", local: false, declaredOutputs: [] }} />);
  const kept = screen.getByTestId("workflow-kept-test-output");
  expect(within(kept).getByText("Edited")).toBeVisible();
  expect(within(kept).getByText(/does not do its work/)).toBeVisible();
  fireEvent.click(within(kept).getByRole("button", { name: "Stop using it" }));
  await waitFor(() => expect(value.clearOutput).toHaveBeenCalled());
  expect(value.clearOutput).toHaveBeenCalledWith("fetch");
  view.rerender(<StepTestRunLatest evidence={value} step={{ stepId: "route", label: "Route", kind: "switch", local: false, declaredOutputs: [] }} />);
  expect(screen.getByText("This kind of step always runs in tests.")).toBeVisible();
});

it("an approval waiting in the run is decided with its request in view, focus on the request", async () => {
  const decide = vi.fn();
  render(<StepTestRunLatest evidence={evidence({ approval: { stepId: "approve", identity: "run-2:gate", stepLabel: "Approve order", message: "Approve this order for fulfilment?",
    shown: [{ name: "state.review", value: { decision: "approved" } }], approvedTo: "Fulfilment", rejectedTo: "", pending: false, error: "", decide } })}
    step={{ stepId: "approve", label: "Approve order", kind: "approval", local: false, declaredOutputs: [] }} />);
  const decision = screen.getByTestId("workflow-approval-decision");
  await waitFor(() => expect(within(decision).getByText("Approve this order for fulfilment?")).toHaveFocus());
  expect(within(decision).getByText("state.review")).toBeVisible();
  expect(within(decision).getByText(/Fulfilment/)).toBeVisible();
  expect(within(decision).getByText(/the run ends/)).toBeVisible();
  expect(within(decision).getAllByRole("button").map((button) => button.textContent)).toEqual(["Reject", "Approve"]);
  fireEvent.change(within(decision).getByRole("textbox", { name: "Comment for rejection" }), { target: { value: "Checked" } });
  fireEvent.click(within(decision).getByRole("button", { name: "Approve" }));
  expect(decide).toHaveBeenCalledWith(true, "Checked");
});

it.each([
  ["", "the run ends"],
  ["Revise report", "Revise report"],
])("keeps the authored Approval rejected outcome for destination %s", (rejectedTo, expected) => {
  render(<StepTestRunLatest evidence={evidence({ approval: {
    stepId: "review", identity: "run-2:report-review", stepLabel: "Review report", message: "Send this report?",
    shown: [], approvedTo: "Send report", rejectedTo, pending: false, error: "", decide: vi.fn(),
  } })} step={{ stepId: "review", label: "Review report", kind: "approval", local: false, declaredOutputs: [] }} />);

  const decision = screen.getByTestId("workflow-approval-decision");
  const outcomes = within(decision).getAllByRole("listitem");
  expect(outcomes[0]).toHaveTextContent("Send report");
  expect(outcomes[1]).toHaveTextContent(`Rejected → ${expected}`);
  expect(within(decision).getByRole("button", { name: "Reject" })).toBeEnabled();
  expect(within(decision).getByRole("button", { name: "Approve" })).toBeEnabled();
  expect(within(decision).queryByRole("button", { name: "Cancel run" })).not.toBeInTheDocument();
});

it("names the executed definition, the run's published outputs and the full Job Details", () => {
  render(<WorkflowTestRunResults evidence={evidence({ runOutputs: { receipt: "Approved" }, runArtifacts: { orders_csv: { filename: "orders.csv" } } })} />);
  expect(screen.getByTestId("workflow-test-run-revision")).toHaveTextContent("Executed definition: revision 7 · abcdef012345");
  const outputs = screen.getByTestId("workflow-test-run-outputs");
  expect(within(outputs).getByText("receipt")).toBeVisible();
  expect(within(outputs).getByText("\"Approved\"")).toBeVisible();
  expect(within(outputs).getByText("orders_csv")).toBeVisible();
  expect(screen.getByRole("link", { name: "Open full Job Details" })).toHaveAttribute("href", "/jobs/run-2");
});

it("shows a waiting approval in Results too, with a comment kept by the page's draft owner", () => {
  const decide = vi.fn();
  const onChange = vi.fn();
  render(<WorkflowTestRunResults commentDraft={{ identity: "run-2:gate", value: "Kept comment", onChange }} evidence={evidence({ statusTone: "attention",
    approval: { stepId: "", identity: "run-2:gate", stepLabel: "", message: "Approve this?", shown: [], approvedTo: "", rejectedTo: "", pending: false, error: "", decide } })} />);
  const decision = screen.getByTestId("workflow-approval-decision");
  const comment = within(decision).getByRole("textbox", { name: "Comment for rejection" });
  expect(comment).toHaveValue("Kept comment");
  fireEvent.change(comment, { target: { value: "Changed" } });
  expect(onChange).toHaveBeenCalledWith("run-2:gate", "Changed");
  fireEvent.click(within(decision).getByRole("button", { name: "Reject" }));
  expect(decide).toHaveBeenCalledWith(false, "Kept comment");
});

it("shows no counts and no 'No step has run yet' while a finished run's steps are still loading", () => {
  render(<WorkflowTestRunResults evidence={evidence({ loading: true, passes: [], counts: { done: 0, skipped: 0, failed: 0 } })} />);
  expect(screen.queryByText("done")).not.toBeInTheDocument();
  expect(screen.queryByText("No step has run yet.")).not.toBeInTheDocument();
});
