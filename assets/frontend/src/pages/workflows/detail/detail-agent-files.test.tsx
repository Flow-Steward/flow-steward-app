import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { isAgentFileStatePath } from "./detail-agent-file-path";
import { AgentFiles } from "./detail-agent-files";
import { normalizeAvailableWorkflowVariableSuggestions } from "./detail-core";

function renderFiles() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AgentFiles value="{}" onChange={() => undefined} /></QueryClientProvider>);
}
function formats(extensions: string[]) {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ text_extensions: extensions, text_encoding: "utf-8", rtf_mode: "source" }), { status: 200, headers: { "content-type": "application/json" } }));
}
afterEach(() => vi.restoreAllMocks());
it("retains the remaining file control and focus when an earlier row is removed", () => {
  formats([".txt"]);
  function Editor() {
    const [value, setValue] = useState(JSON.stringify({ agent: { file_bindings: [{ name: "first", source: "state.first" }, { name: "second", source: "state.second" }] }, extra: true }));
    return <><AgentFiles value={value} onChange={setValue} /><output aria-label="override">{value}</output></>;
  }
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Editor /></QueryClientProvider>);
  const second = screen.getByRole("textbox", { name: "File label 2" });
  second.focus();
  fireEvent.change(second, { target: { value: "updated" } });
  expect(screen.getByRole("textbox", { name: "File label 2" })).toBe(second);
  fireEvent.click(screen.getByRole("button", { name: "Remove file 1" }));
  expect(screen.getByRole("textbox", { name: "File label 1" })).toBe(second);
  expect(second).toHaveFocus();
  expect(second).toHaveValue("updated");
  expect(JSON.parse(screen.getByLabelText("override").textContent || "{}")).toEqual({ agent: { file_bindings: [{ name: "updated", source: "state.second" }] }, extra: true });
});
it.each([
  { name: "default", extensions: [".txt", ".md", ".json", ".xml", ".rtf"] },
  { name: "custom", extensions: [".custom"] },
])("displays $name server formats without an agent", async ({ extensions }) => {
  formats(extensions); renderFiles();
  expect(await screen.findByText(new RegExp(extensions.map((value) => value.replace(".", "\\.")).join(".*")))).toBeVisible();
  expect(screen.getByText(/UTF-8.*RTF.*source/i)).toBeVisible();
  expect(globalThis.fetch).toHaveBeenCalledWith("/api/workflows/meta/agent-file-formats", expect.anything());
});
it("reports an explicitly empty text format policy", async () => {
  formats([]); renderFiles();
  expect(await screen.findByText("Text file reading is disabled.")).toBeVisible();
});
it("reports unavailable formats without inventing defaults", async () => {
  vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("offline")); renderFiles();
  expect(await screen.findByText("Text file formats are unavailable.")).toBeVisible();
  expect(screen.queryByText(/\.txt/)).not.toBeInTheDocument();
});
it("offers only file-typed state paths, including nested file members", () => {
  const suggestions = normalizeAvailableWorkflowVariableSuggestions({ prior_outputs: [
    { name: "text", type: "string", step_id: "producer" },
    { name: "document", type: "artifact_handle", step_id: "producer" },
    { name: "result", type: "object", step_id: "producer" },
  ] }, undefined, { producerMembers: { "producer:result": [{ name: "file", type: "artifact_handle" }] } });
  vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("offline"));
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AgentFiles value={'{"agent":{"file_bindings":[{"name":"input","source":""}]}}'} onChange={() => undefined} suggestions={suggestions} renderVariableInput={({ accept }) => <output aria-label="accepted">{suggestions.flatMap((group) => group.options).filter((option) => accept(option.value)).map((option) => option.value).sort().join(",")}</output>} /></QueryClientProvider>);
  expect(screen.getByLabelText("accepted")).toHaveTextContent("state.document,state.result.file");
});
it("accepts Unicode decimal array indices but rejects other numeric characters", () => {
  expect(isAgentFileStatePath("state.rows[١]")).toBe(true);
  expect(isAgentFileStatePath("state.rows[²]")).toBe(false);
});
