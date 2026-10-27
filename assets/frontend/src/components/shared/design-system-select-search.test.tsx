import { useState } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { DsSelect } from "./design-system-selects";

function SearchHarness() {
  const [value, setValue] = useState("");
  return <DsSelect {...{ searchable: true }} ariaLabel="Action" onChange={setValue} options={[{ value: "list_products", label: "List products" }, { value: "create_product", label: "Create product" }, { value: "disabled", label: "Disabled product", disabled: true }]} value={value} />;
}

it("filters a portalled menu and selects with the keyboard while retaining the selected label", async () => {
  const user = userEvent.setup();
  render(<SearchHarness />);
  const trigger = screen.getByRole("combobox", { name: "Action" });
  await user.click(trigger);
  const search = screen.getByRole("searchbox", { name: "Action Search" });
  expect(search).toHaveFocus();
  await user.type(search, "CREATE");
  expect(screen.queryByRole("option", { name: "List products" })).not.toBeInTheDocument();
  await user.keyboard("{ArrowDown}{Enter}");
  expect(trigger).toHaveTextContent("Create product");
  await waitFor(() => expect(trigger).toHaveFocus());
  await user.click(trigger);
  expect(screen.getByRole("searchbox", { name: "Action Search" })).toHaveValue("");
  expect(within(screen.getByRole("listbox")).getAllByRole("option")).toHaveLength(3);
  await user.type(screen.getByRole("searchbox", { name: "Action Search" }), "unknown");
  expect(screen.getByText("No results")).toBeInTheDocument();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  await waitFor(() => expect(trigger).toHaveFocus());
});

it("returns Tab to the field's native order and returns to the trigger with Shift+Tab", async () => {
  const user = userEvent.setup();
  render(<><SearchHarness /><input aria-label="Next field" /></>);
  const trigger = screen.getByRole("combobox", { name: "Action" });
  await user.click(trigger);
  // user-event calculates Tab's destination from the original event target,
  // even after our handler changes focus. Exercise that handoff separately;
  // the browser scenario checks a single Tab reaches the next field.
  fireEvent.keyDown(screen.getByRole("searchbox", { name: "Action Search" }), { key: "Tab" });
  expect(trigger).toHaveFocus();
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  await user.tab();
  expect(screen.getByRole("textbox", { name: "Next field" })).toHaveFocus();
  await user.click(trigger);
  await user.tab({ shift: true });
  await waitFor(() => expect(trigger).toHaveFocus());
});

it("restores the menu height when clearing a search expands the inner scrolling list", async () => {
  const user = userEvent.setup();
  let expanded = false;
  const scrollHeight = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    if (this.classList.contains("fs-select-menu")) return 80;
    if (this.classList.contains("fs-select-options")) return expanded ? 800 : 32;
    return 0;
  });
  const clientHeight = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("fs-select-options") ? 32 : 0;
  });
  try {
    render(<SearchHarness />);
    await user.click(screen.getByRole("combobox", { name: "Action" }));
    const search = screen.getByRole("searchbox", { name: "Action Search" });
    await user.type(search, "Create");
    expanded = true;
    await user.clear(search);
    // Geometry belongs to the popup wrapper; the semantic listbox scrolls inside it.
    // eslint-disable-next-line testing-library/no-node-access
    const popup = screen.getByRole("listbox").parentElement;
    await waitFor(() => expect(popup).toHaveStyle({ maxHeight: "188px" }));
  } finally {
    scrollHeight.mockRestore();
    clientHeight.mockRestore();
  }
});
