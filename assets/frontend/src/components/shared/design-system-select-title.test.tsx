import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DsSelect, SingleCombobox } from "./design-system-selects";

const longLabel = "Export every matching WooCommerce product with its variations";
const options = [{ value: "test", label: "Test the connection" }, { value: "export", label: longLabel }];

it("shows the whole label of a truncated option and of the chosen value on hover", async () => {
  const user = userEvent.setup();
  render(<DsSelect ariaLabel="Action" onChange={vi.fn()} options={options} value="export" />);
  const trigger = screen.getByRole("combobox", { name: "Action" });
  expect(within(trigger).getByTitle(longLabel)).toHaveTextContent(longLabel);
  await user.click(trigger);
  expect(within(screen.getByRole("option", { name: longLabel })).getByTitle(longLabel)).toHaveTextContent(longLabel);
});

it("gives combobox options their whole label on hover", async () => {
  const user = userEvent.setup();
  render(<SingleCombobox ariaLabel="Action" onChange={vi.fn()} options={options} value="" />);
  await user.click(screen.getByRole("combobox", { name: "Action" }));
  expect(within(screen.getByRole("option", { name: longLabel })).getByTitle(longLabel)).toHaveTextContent(longLabel);
});
