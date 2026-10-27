import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { postedChangeSets, renderPage } from "./index.test-helpers";
import { fetchWorkflowStepContractPreview } from "@/pages/workflows/shared/api";
import { input, output, install } from "./index.agent-field-test-helpers";

// jsdom has no hit testing; the local/new journey clicks the actual canvas node.
const hitTestDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
beforeAll(() => Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [] }));
afterAll(() => { if (hitTestDescriptor) Object.defineProperty(document, "elementsFromPoint", hitTestDescriptor); else Reflect.deleteProperty(document, "elementsFromPoint"); });

const AGENT_JOURNEY_TIMEOUT = 15_000; // Source allowance only; no duration measured.
async function open(surface: "detail" | "builder" = "detail") { const view = renderPage({ surface }); if (surface === "builder") fireEvent.click(await screen.findByRole("group", { name: "Open step Agent A" })); else await userEvent.click(await screen.findByRole("button", { name: /Agent A/i })); await screen.findByRole("button", { name: "Save parameters" }); return view; }
async function choose(label: string, name: string) { await userEvent.click(screen.getByRole("combobox", { name: label })); await userEvent.click(await within(screen.getByRole("listbox", { name: `${label} options` })).findByRole("option", { name })); }
function lastPatch() { const bodies = postedChangeSets(); const operations = bodies.at(-1)?.operations as Array<Record<string, unknown>>; return operations[0].patch as Record<string, unknown>; }

it("maps the selected Agent contract in normal mode and retains canonical values after readback", async () => {
  const boundary = install();
  vi.mocked(fetchWorkflowStepContractPreview).mockResolvedValue({
    workflow_id: "weekly_report", agent_id: "writer", skill: { id: "compose", label: "Compose report" },
    locked_required: [], unresolved_required: [{ name: "request", type: "text", required: true, source: "" }],
    optional: [], custom: [], outputs: [{ name: "result", type: "text" }],
  });
  const view = await open(); let fresh: ReturnType<typeof renderPage> | undefined;
  try {
    expect(screen.queryByRole("tab", { name: "Advanced" })).not.toBeInTheDocument();
    const request = await screen.findByRole("textbox", { name: "Input source: request" });
    expect(request).toHaveValue("state.inputs.request");
    fireEvent.change(request, { target: { value: "state.inputs.changed" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Output target: result" }), { target: { value: "state.outputs.answer" } });
    await choose("Agent binding", "Workflow participant"); await choose("Workflow participant", "reviewer");
    await userEvent.click(screen.getByRole("button", { name: "Save parameters" })); await screen.findByText("Parameters saved");
    expect(lastPatch()).toMatchObject({ participant_key: "reviewer", agent_id: "reviewer", input_mapping_jsonb: { ...input, request: "state.inputs.changed" }, output_mapping_jsonb: { ...output, result: "state.outputs.answer" } });
    view.unmount(); fresh = await open();
    expect(await screen.findByRole("textbox", { name: "Input source: request" })).toHaveValue("state.inputs.changed");
    expect(screen.getByRole("textbox", { name: "Output target: result" })).toHaveValue("state.outputs.answer");
    expect(screen.getByRole("combobox", { name: "Agent" })).toHaveTextContent("Reviewer");
    expect(boundary.row().input_mapping_jsonb).toEqual({ ...input, request: "state.inputs.changed" });
  } finally { fresh?.unmount(); view.unmount(); }
}, AGENT_JOURNEY_TIMEOUT);

it("shows the selected Agent skill label in normal setup", async () => {
  install();
  vi.mocked(fetchWorkflowStepContractPreview).mockResolvedValue({ workflow_id: "weekly_report", skill: { id: "compose", label: "Compose report" }, locked_required: [], unresolved_required: [], optional: [], custom: [], outputs: [] });
  const view = await open();
  try { expect(await screen.findByText("Compose report")).toBeVisible(); } finally { view.unmount(); }
}, AGENT_JOURNEY_TIMEOUT);
