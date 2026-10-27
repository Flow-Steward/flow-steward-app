import { fireEvent, render, screen, within } from "@testing-library/react";
import { JobDefinitionCanvas } from "./job-definition-canvas";
import type { JobDefinitionPayload } from "./api";
import { archivedDefinitionCanvasGraph } from "./job-definition-graph";
import { buildCanvasLayout } from "@/pages/workflows/builder/layout";

const liveSchedule = vi.hoisted(() => vi.fn(() => <p>LIVE_SCHEDULE_MUST_NOT_APPEAR</p>));
vi.mock("@/pages/workflows/builder/canvas-schedule-summary", () => ({ CanvasScheduleSummary: liveSchedule }));

type ArchivedContractSummary = {
  input_mode: string; output_mode: string;
  inputs: Array<{ name: string; required: boolean }>;
  outputs: string[]; artifacts: string[]; trigger_channels: string[];
};
type ArchivedFixture = JobDefinitionPayload & { contract_summary?: ArchivedContractSummary | null };
const summary: ArchivedContractSummary = {
  input_mode: "fields", output_mode: "variables",
  inputs: [{ name: "historical_input", required: true }], outputs: ["historical_output"],
  artifacts: ["historical.csv"], trigger_channels: ["schedule"],
};
const snapshot: ArchivedFixture = {
  job_id: "pinned-run", available: true, snapshot_id: "pinned-archive", schema_version: 1,
  workflow_id: "changed-live-workflow", revision: 4,
  steps: [{ step_id: "success", display_name: "Historical success", step_kind: "stop_success" }], groups: [], for_each: [],
  execution_graph: { schema_version: 1, entry: { kind: "step", id: "success" }, edges: [] },
  contract_summary: summary,
};

beforeEach(() => {
  liveSchedule.mockClear();
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});
function archived(overrides: Partial<ArchivedFixture>): ArchivedFixture { return { ...snapshot, ...overrides }; }
function canvas() { return screen.getByTestId("job-definition-canvas"); }
it.each([
  [{ selector: "state.line.qty", operator: "lte", value: 0 }, "state.line.qty ≤ 0"],
  [{ selector: "state.a", operator: "equals", value: "state.b" }, "state.a = state.b"],
  [{ selector: "state.a", operator: "equals", value: "state" }, "state.a = state"],
  [{ equals: { left: "state.a", right: "state.b" } }, "state.a = state.b"],
  [{ equals: { left: "state.a", right: "state" } }, "state.a = state"],
  [{ selector: "state.value", operator: "equals", value: false }, "state.value = false"],
  [{ selector: "state.value", operator: "not_equals", value: null }, "state.value ≠ null"],
  [{ selector: "state.value", operator: "gt", value: 1 }, "state.value > 1"],
  [{ selector: "state.value", operator: "gte", value: 1 }, "state.value ≥ 1"],
  [{ selector: "state.value", operator: "lt", value: 1 }, "state.value < 1"],
  [{ selector: "state.text", operator: "contains", value: "x" }, 'state.text contains "x"'],
  [{ selector: "state.text", operator: "in", value: ["x"] }, 'state.text in ["x"]'],
  [{ selector: "state.text", operator: "not_in", value: ["x"] }, 'state.text not in ["x"]'],
  [{ selector: "state.ready", operator: "exists" }, "state.ready exists"],
  [{ selector: "state.ready", operator: "truthy" }, "state.ready truthy"],
  [{ selector: "state.ready", operator: "always_true" }, "always true"],
  [{ selector: "state.text", operator: "equals", value: "" }, 'state.text = ""'],
  [{ selector: "state.text", operator: "equals", value: 'a"\\b\n' }, String.raw`state.text = "a\"\\b\n"`],
  [{ lte: { left: "state.line.qty", right: 0 } }, "state.line.qty ≤ 0"],
  [{ truthy: "state.ready" }, "state.ready truthy"],
  [{ operator: "always_false" }, "always false"],
  [{ selector: "state.x", operator: "eq", value: 0 }, "conditional branch"],
  [{ selector: "state.x", operator: "constructor", value: 0 }, "conditional branch"],
  [{ toString: { left: "state.x", right: 0 } }, "conditional branch"],
  [{ selector: "state.x", operator: "lte", value: 0, nested: true }, "conditional branch"],
  [{ selector: "state.x", operator: "lte" }, "conditional branch"],
  [{ selector: "state.x", operator: "always_true", value: 0 }, "conditional branch"],
  [{ lte: { left: "state.x", right: 0, extra: true } }, "conditional branch"],
  [{ and: [{ truthy: "state.x" }] }, "conditional branch"],
  [{ always_false: null }, "conditional branch"],
  [{ selector: 0, operator: "always_false" }, "conditional branch"],
  [false, "conditional branch"],
])("formats the archived If summary from its pinned condition (%o)", (condition, expected) => {
  const pinned = archived({
    steps: [{ step_id: "route", display_name: "Historical route", step_kind: "if", override: { if: { condition } } }],
    execution_graph: { schema_version: 1, entry: { kind: "step", id: "route" }, edges: [] },
  });
  render(<JobDefinitionCanvas definition={pinned} />);
  expect(within(canvas()).getByText(expected, { exact: true })).toBeVisible();
  expect(liveSchedule).not.toHaveBeenCalled();
});

it("preserves an archived legacy If string and its stored condition", () => {
  const condition = "state.ready == true";
  const pinned = archived({ steps: [{ step_id: "route", display_name: "Historical route", step_kind: "if", override: { if: { condition } } }], execution_graph: { schema_version: 1, entry: { kind: "step", id: "route" }, edges: [] } });
  render(<JobDefinitionCanvas definition={pinned} />);
  expect(within(canvas()).getByText(condition, { exact: true })).toBeVisible();
  expect(pinned.steps?.[0]?.override).toEqual({ if: { condition } });
});

it("preserves the archived legacy If selector summary", () => {
  const pinned = archived({
    steps: [{ step_id: "route", display_name: "Historical route", step_kind: "if", override: { if: { selector: "state.ready" } } }],
    execution_graph: { schema_version: 1, entry: { kind: "step", id: "route" }, edges: [] },
  });
  render(<JobDefinitionCanvas definition={pinned} />);
  expect(within(canvas()).getByText("state.ready", { exact: true })).toBeVisible();
});

it("marks the projected Start read-only as well as every archived executable node", () => {
  const graph = archivedDefinitionCanvasGraph(snapshot)!;
  const layout = buildCanvasLayout({ canvasGraph: graph, readOnly: true, onOpenStep: vi.fn(), onOpenGroup: vi.fn(), onOpenStepAutomation: vi.fn(), onOpenGroupAutomation: vi.fn() });
  expect(layout.nodes.find((node) => node.id === "visual:start")?.data).toMatchObject({ readOnly: true });
  expect(layout.nodes.every((node) => node.draggable === false)).toBe(true);
});

it("renders pinned contract names and required markers without mounting current schedule queries", () => {
  render(<JobDefinitionCanvas definition={snapshot} />);
  const inputName = within(canvas()).getByText("historical_input", { exact: true });
  expect(inputName).toBeVisible();
  expect(within(inputName).getByLabelText("Required")).toBeVisible();
  expect(within(canvas()).getByText("* Required", { exact: true })).toBeVisible();
  expect(within(canvas()).getByText("historical_output")).toBeVisible();
  expect(within(canvas()).getByText("historical.csv")).toBeVisible();
  expect(within(canvas()).getByText("Schedule")).toBeVisible();
  expect(liveSchedule).not.toHaveBeenCalled();
  expect(within(canvas()).queryByText("Workflow disabled")).not.toBeInTheDocument();
  expect(within(canvas()).queryByText("LIVE_SCHEDULE_MUST_NOT_APPEAR")).not.toBeInTheDocument();
});

it.each(["body", "button"])("opens pinned Inputs from the archived Start %s without treating it as a step", async (target) => {
  render(<JobDefinitionCanvas definition={snapshot} />);
  fireEvent.click(target === "body" ? within(canvas()).getByText("Inputs", { exact: true }) : within(canvas()).getByRole("button", { name: "Start — workflow inputs" }));
  const drawer = await screen.findByRole("dialog", { name: "Archived configuration" });
  expect(drawer).toHaveTextContent("historical_input");
  expect(within(drawer).queryByRole("textbox")).not.toBeInTheDocument();
  expect(within(drawer).queryByRole("button", { name: /save|apply/i })).not.toBeInTheDocument();
});

it("opens the pinned Outputs contract from Success Stop without opening a live editor", async () => {
  render(<JobDefinitionCanvas definition={snapshot} />);
  fireEvent.click(within(canvas()).getByText("Historical success"));
  const drawer = await screen.findByRole("dialog", { name: "Archived configuration" });
  expect(drawer).toHaveTextContent("historical_output");
  expect(drawer).toHaveTextContent("historical.csv");
  expect(drawer).toHaveTextContent("Returned Files");
  expect(drawer).not.toHaveTextContent(/External Changes/i);
  expect(within(canvas()).queryByText("Edit")).not.toBeInTheDocument();
  expect(within(drawer).queryByRole("textbox")).not.toBeInTheDocument();
});

it.each([undefined, null])("distinguishes unavailable archived contracts (%s) from an empty declaration", (contract_summary) => {
  render(<JobDefinitionCanvas definition={archived({ contract_summary })} />);
  expect(within(canvas()).getAllByText(/unavailable/i).length).toBeGreaterThan(0);
  expect(within(canvas()).queryByText("None declared")).not.toBeInTheDocument();
  expect(within(canvas()).queryByText("No inputs declared", { exact: true })).not.toBeInTheDocument();
});

it("does not describe scalar or array JSON Schema inputs as no declared fields", () => {
  render(<JobDefinitionCanvas definition={archived({ contract_summary: { ...summary, input_mode: "json_schema", inputs: [] } })} />);
  expect(within(canvas()).getByText(/JSON Schema/i)).toBeVisible();
  expect(within(canvas()).queryByText("None declared")).not.toBeInTheDocument();
  expect(within(canvas()).queryByText("No inputs declared", { exact: true })).not.toBeInTheDocument();
});

it("keeps authoritative empty contracts distinct and an empty archived graph non-editable", () => {
  render(<JobDefinitionCanvas definition={archived({ steps: [], execution_graph: { schema_version: 1, entry: null, edges: [] }, contract_summary: { ...summary, inputs: [], outputs: [], trigger_channels: [] } })} />);
  expect(within(canvas()).getByText("No inputs declared", { exact: true })).toBeVisible();
  expect(within(canvas()).getByText("No triggers configured", { exact: true })).toBeVisible();
  expect(within(canvas()).queryByText(/unavailable/i)).not.toBeInTheDocument();
  expect(within(canvas()).queryByRole("button", { name: "Add first step" })).not.toBeInTheDocument();
});

it("does not disclose arbitrary extra summary data through either cards or the archived drawer", async () => {
  const extraSummary = { ...summary, defaults: { historical_input: "SECRET_DEFAULT" }, descriptions: "PRIVATE_DESCRIPTION", input_schema: { default: "RAW_SCHEMA_SECRET" } };
  render(<JobDefinitionCanvas definition={archived({ contract_summary: extraSummary })} />);
  fireEvent.click(within(canvas()).getByRole("button", { name: "Start — workflow inputs" }));
  await screen.findByRole("dialog", { name: "Archived configuration" });
  expect(document.body).not.toHaveTextContent(/SECRET_DEFAULT|PRIVATE_DESCRIPTION|RAW_SCHEMA_SECRET/);
});

it("keeps protected definitions closed even when the transport supplies a contract summary", () => {
  render(<JobDefinitionCanvas definition={{ ...snapshot, available: false, unavailable_reason: "protected" }} />);
  expect(screen.queryByTestId("job-definition-canvas")).not.toBeInTheDocument();
  expect(document.body).not.toHaveTextContent(/historical_input|historical_output|historical.csv/);
});

it("accepts canonical passthrough inputs and describes the raw payload instead of an absent contract", () => {
  render(<JobDefinitionCanvas definition={archived({ contract_summary: { ...summary, input_mode: "passthrough", inputs: [] } })} />);
  expect(within(canvas()).getByText("Raw JSON payload")).toBeVisible();
  expect(within(canvas()).getByText("historical_output")).toBeVisible();
  expect(within(canvas()).queryByText(/unavailable/i)).not.toBeInTheDocument();
  expect(within(canvas()).queryByText("None declared")).not.toBeInTheDocument();
  expect(within(canvas()).queryByText("No inputs declared", { exact: true })).not.toBeInTheDocument();
});
