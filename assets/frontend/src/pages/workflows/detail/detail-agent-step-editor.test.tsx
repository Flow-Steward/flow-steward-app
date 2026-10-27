import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { AgentAdvancedFields, AgentBindingFields } from "./detail-agent-step-editor";
import { agentParticipants } from "./detail-agent-step-fields";
import { useAgentStepDrafts } from "./detail-agent-step-drafts";

const workflow = { settings: { participants: [{ participant_key: "author", agent_id: "writer" }] } };
function Harness({ owner = "A" }: { owner?: string }) {
  const [agent, setAgent] = useState("writer"); const [override, setOverride] = useState('{"block_key":"block.old","extra_prompt":"Keep"}');
  const [captured, setCaptured] = useState<unknown>(null);
  const drafts = useAgentStepDrafts({ owner, raw: { agent_id: "writer", input_mapping_jsonb: { value: null, flag: false }, override: { block_key: "block.old", extra_prompt: "Keep" } }, createContext: undefined });
  const ctx = { agentDrafts: drafts, agentWorkflowRecord: workflow, stepAgentId: agent, setStepAgentId: setAgent, stepAgentLocked: false, stepAgentOptions: [{ value: "writer", label: "Writer" }], agentCatalogQuery: {}, agentBlockCatalogQuery: {}, agentBlockOptions: [{ value: "block.old", label: "Old block" }, { value: "block.new", label: "New block" }], stepOverrideDraft: override, setStepOverrideDraft: setOverride, setStepBlockKey: () => undefined };
  return <><AgentBindingFields ctx={ctx} key={`binding:${drafts.editorKey}`} /><AgentAdvancedFields ctx={ctx} key={`advanced:${drafts.editorKey}`} /><button onClick={() => setCaptured(drafts.snapshot(agent, JSON.parse(override), agentParticipants(workflow)))}>Capture</button><output aria-label="Captured">{JSON.stringify(captured)}</output><output aria-label="Override">{override}</output></>;
}
async function choose(label: string, option: string) { await userEvent.click(screen.getByRole("combobox", { name: label })); await userEvent.click(within(screen.getByRole("listbox", { name: `${label} options` })).getByRole("option", { name: option })); }
it("uses real controls to preserve typed map data and explicit participant/direct intention", async () => {
  render(<Harness />);
  fireEvent.change(screen.getByRole("textbox", { name: "Agent input mapping JSON" }), { target: { value: '{"value":null,"flag":false,"text":"false"}' } });
  await choose("Agent binding", "Workflow participant"); await choose("Workflow participant", "author");
  expect(screen.getByRole("combobox", { name: "Agent" })).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Capture" }));
  expect(JSON.parse(screen.getByLabelText("Captured").textContent || "{}")).toEqual({ agent_id: "writer", participant_key: "author", input_mapping_jsonb: { value: null, flag: false, text: "false" } });
  await choose("Agent binding", "Direct agent"); await userEvent.click(screen.getByRole("button", { name: "Capture" }));
  expect(JSON.parse(screen.getByLabelText("Captured").textContent || "{}").participant_key).toBe("");
});
it("uses effective alias and explicit Auto while preserving unrelated override data", async () => {
  render(<Harness />);
  expect(screen.getByRole("combobox", { name: "Agent contract" })).toHaveTextContent("Old block");
  await choose("Agent contract", "New block");
  expect(JSON.parse(screen.getByLabelText("Override").textContent || "{}")).toEqual({ published_block_key: "block.new", extra_prompt: "Keep" });
  await choose("Agent contract", "Auto from selected agent");
  expect(JSON.parse(screen.getByLabelText("Override").textContent || "{}")).toEqual({ extra_prompt: "Keep" });
});
it("associates raw map errors and never carries raw invalid draft to a different owner", () => {
  const view = render(<Harness />); const input = screen.getByRole("textbox", { name: "Agent input mapping JSON" });
  fireEvent.change(input, { target: { value: '{"value":' } });
  expect(input).toHaveAttribute("aria-invalid", "true");
  const error = screen.getByText("Enter a JSON object", { exact: false });
  expect(error).toHaveTextContent("Enter a JSON object");
  expect(error).toHaveAttribute("id", input.getAttribute("aria-describedby"));
  expect(input).toHaveAccessibleDescription("Enter a JSON object with finite numbers. The incomplete draft has not been saved.");
  view.rerender(<Harness owner="B" />);
  expect(screen.getByRole("textbox", { name: "Agent input mapping JSON" })).not.toHaveValue('{"value":');
});
