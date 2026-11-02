import { render, screen } from "@testing-library/react";
import { isAgentFileStatePath } from "./detail-agent-file-path";
import { AgentFiles } from "./detail-agent-files";
import { normalizeAvailableWorkflowVariableSuggestions } from "./detail-core";

it("offers only file-typed state paths, including nested file members", () => {
  const suggestions = normalizeAvailableWorkflowVariableSuggestions({ prior_outputs: [
    { name: "text", type: "string", step_id: "producer" },
    { name: "document", type: "artifact_handle", step_id: "producer" },
    { name: "result", type: "object", step_id: "producer" },
  ] }, undefined, { producerMembers: { "producer:result": [{ name: "file", type: "artifact_handle" }] } });
  render(<AgentFiles value={'{"agent":{"file_bindings":[{"name":"input","source":""}]}}'} onChange={() => undefined} suggestions={suggestions} renderVariableInput={({ accept }) => <output aria-label="accepted">{suggestions.flatMap((group) => group.options).filter((option) => accept(option.value)).map((option) => option.value).sort().join(",")}</output>} />);
  expect(screen.getByLabelText("accepted")).toHaveTextContent("state.document,state.result.file");
});
it("accepts Unicode decimal array indices but rejects other numeric characters", () => {
  expect(isAgentFileStatePath("state.rows[١]")).toBe(true);
  expect(isAgentFileStatePath("state.rows[²]")).toBe(false);
});
