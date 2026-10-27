import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { dryRunChangeSets, postedChangeSets, renderPage } from "./index.test-helpers";
import { fetchWorkflowStepContractPreview } from "@/pages/workflows/shared/api";
import { input, install } from "./index.agent-field-test-helpers";

// jsdom has no hit testing; the local/new journey clicks the actual canvas node.
const hitTestDescriptor = Object.getOwnPropertyDescriptor(document, "elementsFromPoint");
beforeAll(() => Object.defineProperty(document, "elementsFromPoint", { configurable: true, value: () => [] }));
afterAll(() => { if (hitTestDescriptor) Object.defineProperty(document, "elementsFromPoint", hitTestDescriptor); else Reflect.deleteProperty(document, "elementsFromPoint"); });

const AGENT_JOURNEY_TIMEOUT = 15_000; // Source allowance only; no duration measured.
const BUILDER_AGENT_JOURNEY_TIMEOUT = 30_000; // Three real Builder journeys take up to ~13s solo; the four-worker gate adds contention.
async function open(surface: "detail" | "builder" = "detail") { const view = renderPage({ surface }); if (surface === "builder") fireEvent.click(await screen.findByRole("group", { name: "Open step Agent A" })); else await userEvent.click(await screen.findByRole("button", { name: /Agent A/i })); if (surface === "builder") await within(await screen.findByRole("dialog", { name: "Step editor" })).findByRole("button", { name: "Save parameters" }); else await screen.findByRole("button", { name: "Save parameters" }); return view; }
async function advanced() { await userEvent.click(screen.getByRole("button", { name: "Advanced mode" })); await userEvent.click(await screen.findByRole("tab", { name: "Advanced" })); }
async function choose(label: string, name: string) { await userEvent.click(screen.getByRole("combobox", { name: label })); await userEvent.click(await within(screen.getByRole("listbox", { name: `${label} options` })).findByRole("option", { name })); }
async function freshRead(boundary: ReturnType<typeof install>, previous: ReturnType<typeof renderPage>) {
  previous.unmount(); boundary.holdRead(); const before = boundary.reads.length; const started = boundary.started();
  const view = renderPage();
  await waitFor(() => expect(boundary.started()).toBeGreaterThan(started));
  await screen.findByText("Preparing the workflow"); expect(boundary.reads).toHaveLength(before);
  await act(async () => boundary.release());
  await userEvent.click(await screen.findByRole("button", { name: /Server Agent A/i }));
  return view;
}
function lastPatch() { const bodies = postedChangeSets(); const operations = bodies.at(-1)?.operations as Array<Record<string, unknown>>; return operations[0].patch as Record<string, unknown>; }

it("edits complete canonical Agent maps without coercing typed siblings and reads them from a fresh page", async () => {
  const boundary = install(); const view = await open(); let fresh: ReturnType<typeof renderPage> | undefined;
  try {
    await advanced();
    const expected = { ...input, request: "state.inputs.changed" };
    fireEvent.change(screen.getByRole("textbox", { name: "Agent input mapping JSON" }), { target: { value: JSON.stringify(expected) } });
    fireEvent.change(screen.getByRole("textbox", { name: "Agent output mapping JSON" }), { target: { value: JSON.stringify({ result: "state.changed", summary: "state.summary" }) } });
    await userEvent.click(screen.getByRole("button", { name: "Save parameters" }));
    await screen.findByText("Parameters saved");
    expect(lastPatch().input_mapping_jsonb).toEqual(expected);
    expect(lastPatch().output_mapping_jsonb).toEqual({ result: "state.changed", summary: "state.summary" });
    expect(boundary.row().input_mapping_jsonb).toEqual(expected);
    view.unmount(); boundary.holdRead(); const before = boundary.reads.length; const started = boundary.started(); fresh = renderPage();
    await waitFor(() => expect(boundary.started()).toBeGreaterThan(started));
    await screen.findByText("Preparing the workflow"); expect(boundary.reads).toHaveLength(before);
    await act(async () => boundary.release());
    await userEvent.click(await screen.findByRole("button", { name: /Server Agent A/i })); await advanced();
    expect(JSON.parse((screen.getByRole("textbox", { name: "Agent input mapping JSON" }) as HTMLTextAreaElement).value)).toEqual(expected);
    expect(JSON.parse((screen.getByRole("textbox", { name: "Agent output mapping JSON" }) as HTMLTextAreaElement).value)).toEqual(boundary.row().output_mapping_jsonb);
    expect(postedChangeSets()).toHaveLength(1);
  } finally { boundary.release(); fresh?.unmount(); view.unmount(); }
}, AGENT_JOURNEY_TIMEOUT);
it("preserves untouched canonical maps and aliases, and explicit canonical empty stays empty", async () => {
  const boundary = install(); const view = await open();
  try {
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Changed label" } });
    await userEvent.click(screen.getByRole("button", { name: "Save parameters" })); await screen.findByText("Parameters saved");
    expect(lastPatch()).not.toHaveProperty("input_mapping_jsonb"); expect(lastPatch()).not.toHaveProperty("output_mapping_jsonb");
    expect(boundary.row().input_mapping_jsonb).toEqual(input);
    await advanced(); fireEvent.change(screen.getByRole("textbox", { name: "Agent input mapping JSON" }), { target: { value: "{}" } });
    await userEvent.click(screen.getByRole("button", { name: "Save parameters" })); await waitFor(() => expect(postedChangeSets()).toHaveLength(2));
    expect(lastPatch().input_mapping_jsonb).toEqual({});
    expect((lastPatch().override as { agent: unknown }).agent).toEqual({ input_mapping: { request: "state.shadowed" } });
  } finally { view.unmount(); }
}, AGENT_JOURNEY_TIMEOUT);
it("retains invalid map text without a hidden-last-valid Save and honors tab Cancel", async () => {
  install(); const view = await open();
  try {
    await advanced(); const map = screen.getByRole("textbox", { name: "Agent input mapping JSON" });
    fireEvent.change(map, { target: { value: '{"request":' } });
    await userEvent.click(screen.getByRole("button", { name: "Save parameters" })); expect(map).toHaveFocus(); expect(postedChangeSets()).toEqual([]);
    await userEvent.click(screen.getByRole("tab", { name: "Smart setup" }));
    const dialog = await screen.findByRole("dialog", { name: "Discard invalid input?" }); await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("tab", { name: "Advanced" })).toHaveAttribute("aria-selected", "true");
    expect(map).toHaveValue('{"request":'); expect(postedChangeSets()).toEqual([]);
  } finally { view.unmount(); }
}, AGENT_JOURNEY_TIMEOUT);
it("binds effective block aliases and explicit Auto instead of the legacy preview column", async () => {
  const boundary = install({ block_key: "legacy-preview", override: { published_block_key: "block.a", block_key: "fallback", extra_prompt: "Keep me" } }); const view = await open(); let fresh: ReturnType<typeof renderPage> | undefined;
  try {
    await advanced(); await waitFor(() => expect(screen.getByRole("combobox", { name: "Agent contract" })).toHaveTextContent("Block A"));
    await choose("Agent contract", "Block B"); await userEvent.click(screen.getByRole("button", { name: "Save parameters" })); await screen.findByText("Parameters saved");
    expect(lastPatch().override).toEqual({ published_block_key: "block.b", extra_prompt: "Keep me" }); expect(lastPatch().block_key).toBe("block.b");
    await choose("Agent contract", "Auto from selected agent"); await userEvent.click(screen.getByRole("button", { name: "Save parameters" })); await waitFor(() => expect(postedChangeSets()).toHaveLength(2));
    expect(lastPatch().override).toEqual({ extra_prompt: "Keep me" }); expect(lastPatch().block_key).toBe("");
    await waitFor(() => expect(vi.mocked(fetchWorkflowStepContractPreview).mock.calls.at(-1)?.[1]).toMatchObject({ stepId: "agent_a", blockBindingMode: "auto" }));
    fresh = await freshRead(boundary, view); await advanced();
    expect(screen.getByRole("combobox", { name: "Agent contract" })).toHaveTextContent("Auto from selected agent");
    expect(boundary.row().override).toEqual({ extra_prompt: "Keep me" }); expect(postedChangeSets()).toHaveLength(2);
  } finally { boundary.release(); fresh?.unmount(); view.unmount(); }
}, AGENT_JOURNEY_TIMEOUT);
it("preserves untouched orphan participant but explicit Direct clears and explicit Participant supplies its compatible agent", async () => {
  const boundary = install({ participant_key: "orphan", agent_id: "writer" }); const view = await open(); let fresh: ReturnType<typeof renderPage> | undefined;
  try {
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Unrelated label" } });
    await userEvent.click(screen.getByRole("button", { name: "Save parameters" })); await screen.findByText("Parameters saved");
    expect(lastPatch()).not.toHaveProperty("participant_key");
    await choose("Agent binding", "Direct agent"); await userEvent.click(screen.getByRole("button", { name: "Save parameters" })); await waitFor(() => expect(postedChangeSets()).toHaveLength(2));
    expect(lastPatch()).toMatchObject({ participant_key: "", agent_id: "writer" });
    await choose("Agent binding", "Workflow participant"); await choose("Workflow participant", "reviewer");
    await userEvent.click(screen.getByRole("button", { name: "Save parameters" })); await waitFor(() => expect(postedChangeSets()).toHaveLength(3));
    expect(lastPatch()).toMatchObject({ participant_key: "reviewer", agent_id: "reviewer" });
    expect(screen.getByRole("combobox", { name: "Agent" })).toBeDisabled();
    fresh = await freshRead(boundary, view);
    expect(screen.getByRole("combobox", { name: "Agent binding" })).toHaveTextContent("Workflow participant");
    expect(screen.getByRole("combobox", { name: "Workflow participant" })).toHaveTextContent("reviewer");
    expect(screen.getByRole("combobox", { name: "Agent" })).toBeDisabled(); expect(screen.getByRole("combobox", { name: "Agent" })).toHaveTextContent("Reviewer");
    expect(boundary.row()).toMatchObject({ participant_key: "reviewer", agent_id: "reviewer" }); expect(postedChangeSets()).toHaveLength(3);
  } finally { boundary.release(); fresh?.unmount(); view.unmount(); }
}, AGENT_JOURNEY_TIMEOUT);

it("keeps a newer raw map and Step label when its real held Agent Save commits", async () => {
  const boundary = install(); boundary.holdSave(); const view = await open();
  try {
    // Save parameters rests until something differs from the saved step.
    expect(screen.getByRole("button", { name: "Save parameters" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Held label" } });
    await userEvent.click(screen.getByRole("button", { name: "Save parameters" })); await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Newer local label" } });
    await advanced(); const field = screen.getByRole("textbox", { name: "Agent input mapping JSON" });
    fireEvent.change(field, { target: { value: '{"newer":' } });
    const reads = boundary.reads.length;
    await act(async () => boundary.acceptSave());
    await waitFor(() => expect(boundary.reads.length).toBeGreaterThan(reads));
    expect(field).toHaveValue('{"newer":');
    fireEvent.change(field, { target: { value: '{"newer":null}' } });
    await userEvent.click(screen.getByRole("tab", { name: "Smart setup" }));
    expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Newer local label");
    expect(screen.getByRole("button", { name: "Save parameters" })).toBeEnabled(); expect(postedChangeSets()).toHaveLength(1);
  } finally { await act(async () => boundary.acceptSave()); view.unmount(); }
}, AGENT_JOURNEY_TIMEOUT);

it.each(["acknowledged", "newer valid edit", "newer malformed edit"])("keeps local Agent values and guards only genuine edits after Update: %s", async (scenario) => {
  install(); const view = await open("builder");
  try {
    // Builder owns Advanced mode in More; its real command opens Workflow settings.
    await userEvent.click(screen.getByRole("button", { name: "More" }));
    await userEvent.click(await screen.findByRole("button", { name: "Advanced mode off" }));
    const settings = await screen.findByRole("dialog", { name: "Workflow settings" });
    expect(within(settings).getByRole("tab", { name: "Advanced" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("group", { name: "Open step Agent A" }));
    const editor = await screen.findByRole("dialog", { name: "Step editor" });
    await userEvent.click(within(editor).getByRole("tab", { name: "Advanced" }));
    const expected = { request: "literal:", nil: null, flag: false, zero: 0 };
    fireEvent.change(screen.getByRole("textbox", { name: "Agent input mapping JSON" }), { target: { value: JSON.stringify(expected) } });
    await userEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
    fireEvent.click(await screen.findByRole("group", { name: "Open step Agent A" }));
    await userEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    expect(JSON.parse((screen.getByRole("textbox", { name: "Agent input mapping JSON" }) as HTMLTextAreaElement).value)).toEqual(expected);
    // Save first, so the separate new-step journey is not relying on a discard bypass.
    await userEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Save parameters" })); await screen.findByText("Parameters saved");
    await userEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
    await userEvent.click(screen.getByRole("button", { name: "Add step" }));
    const palette = await screen.findByRole("listbox", { name: "Add step" });
    await userEvent.click(within(palette).getByRole("option", { name: "Agent" }));
    await within(await screen.findByRole("dialog", { name: "Step editor" })).findByRole("button", { name: /Add step to changes|Update new step/ });
    const agent = await screen.findByRole("combobox", { name: "Agent" });
    await waitFor(() => expect(agent).toBeEnabled());
    await choose("Agent", "Writer");
    expect(agent).toHaveTextContent("Writer");
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Staged Agent" } });
    await userEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    expect(screen.getByRole("textbox", { name: "Agent input mapping JSON" })).toHaveValue("{}");
    const stagedInput = { request: "state.inputs.request", flag: false, zero: 0, nullable: null, code: "001" };
    const stagedOutput = { result: "state.result" };
    fireEvent.change(screen.getByRole("textbox", { name: "Agent input mapping JSON" }), { target: { value: JSON.stringify(stagedInput) } });
    fireEvent.change(screen.getByRole("textbox", { name: "Agent output mapping JSON" }), { target: { value: JSON.stringify(stagedOutput) } });
    await userEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Update new step" }));
    await within(await screen.findByRole("dialog", { name: "Step editor" })).findByRole("button", { name: "Updated" });
    const expectedAgent = { step_kind: "agent", agent_id: "writer", display_name: "Staged Agent", input_mapping_jsonb: stagedInput, output_mapping_jsonb: stagedOutput };
    await waitFor(() => expect(dryRunChangeSets().at(-1)?.operations).toEqual(expect.arrayContaining([
      expect.objectContaining({ op: "create_step", step: expect.objectContaining(expectedAgent) }),
    ])));
    expect(postedChangeSets()).toHaveLength(1);
    const newer = scenario === "newer malformed edit" ? '{"newer":' : '{"newer":null}';
    if (scenario !== "acknowledged") fireEvent.change(screen.getByRole("textbox", { name: "Agent input mapping JSON" }), { target: { value: newer } });
    await userEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
    await userEvent.click(screen.getByRole("button", { name: "Add step" }));
    const nextPalette = await screen.findByRole("listbox", { name: "Add step" });
    await userEvent.click(within(nextPalette).getByRole("option", { name: "Stop · success" }));
    if (scenario === "acknowledged") {
      await screen.findByRole("group", { name: "Open step Stop · success" });
      expect(screen.queryByRole("dialog", { name: "Discard unsaved parameters?" })).not.toBeInTheDocument();
      await waitFor(() => expect(dryRunChangeSets().at(-1)?.operations).toEqual(expect.arrayContaining([
        expect.objectContaining({ op: "create_step", step: expect.objectContaining(expectedAgent) }),
        expect.objectContaining({ op: "create_step", step: expect.objectContaining({ step_kind: "stop_success" }) }),
      ])));
      await userEvent.click(within(screen.getByRole("dialog", { name: "Step editor" })).getByRole("button", { name: "Close" }));
      fireEvent.click(screen.getByRole("group", { name: "Open step Staged Agent" }));
      await screen.findByRole("dialog", { name: "Step editor" });
      expect(screen.getByRole("textbox", { name: "Step label" })).toHaveValue("Staged Agent");
      expect(screen.getByRole("combobox", { name: "Agent" })).toHaveTextContent("Writer");
      await userEvent.click(screen.getByRole("tab", { name: "Advanced" }));
      expect(JSON.parse((screen.getByRole("textbox", { name: "Agent input mapping JSON" }) as HTMLTextAreaElement).value)).toEqual(stagedInput);
      expect(JSON.parse((screen.getByRole("textbox", { name: "Agent output mapping JSON" }) as HTMLTextAreaElement).value)).toEqual(stagedOutput);
    } else {
      const discard = await screen.findByRole("dialog", { name: "Discard unsaved parameters?" });
      await userEvent.click(within(discard).getByRole("button", { name: "Cancel" }));
      expect(screen.queryByRole("group", { name: "Open step Stop · success" })).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("group", { name: "Open step Staged Agent" }));
      await screen.findByRole("dialog", { name: "Step editor" });
      await userEvent.click(screen.getByRole("tab", { name: "Advanced" }));
      expect(screen.getByRole("textbox", { name: "Agent input mapping JSON" })).toHaveValue(newer);
      expect((dryRunChangeSets().at(-1)?.operations as Array<{ op: string }>).filter(({ op }) => op === "create_step")).toHaveLength(1);
    }
    expect(postedChangeSets()).toHaveLength(1);
  } finally { view.unmount(); }
}, BUILDER_AGENT_JOURNEY_TIMEOUT);
