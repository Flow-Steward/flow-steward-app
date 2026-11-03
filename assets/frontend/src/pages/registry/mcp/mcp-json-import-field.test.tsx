import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { McpJsonImportField } from "./mcp-json-import-field";

it("clears pasted credentials while retaining the server selector", async () => {
  const user = userEvent.setup();
  const onApply = vi.fn();
  render(<McpJsonImportField onApply={onApply} />);
  const input = screen.getByRole("textbox", { name: "Paste JSON configuration" });
  await user.click(input);
  await user.paste(JSON.stringify({ mcpServers: {
    first: { url: "https://first.example/mcp", headers: { Authorization: "Bearer secret" } },
    second: { url: "https://second.example/mcp" },
  } }));
  await waitFor(() => expect(input).toHaveValue(""));
  await user.click(screen.getByRole("combobox", { name: "Server from the JSON" }));
  await user.click(screen.getByRole("option", { name: "second" }));
  expect(onApply).toHaveBeenLastCalledWith(expect.objectContaining({ name: "second" }));
});
