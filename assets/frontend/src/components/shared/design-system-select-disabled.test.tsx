import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { DsSelect, type DsOption } from "./design-system-selects";

const connectionOptions: Array<DsOption & { disabled?: boolean }> = [
  { value: "saved", label: "Saved account (unavailable)", disabled: true },
  { value: "first", label: "First available account" },
  { value: "blocked", label: "Account without required scope", disabled: true },
  { value: "last", label: "Last available account" },
];

function ConnectionSelect({ onChange, options = connectionOptions }: Readonly<{
  onChange: (value: string) => void;
  options?: Array<DsOption & { disabled?: boolean }>;
}>) {
  const [value, setValue] = useState("saved");
  return <DsSelect ariaLabel="Connection" onChange={(next) => { setValue(next); onChange(next); }} options={options} value={value} />;
}

it("keeps the saved unavailable label visible without allowing an unavailable connection to be chosen", async () => {
  const user = userEvent.setup();
  const onChange = vi.fn();
  render(<ConnectionSelect onChange={onChange} />);
  const trigger = screen.getByRole("combobox", { name: "Connection" });
  expect(trigger).toHaveTextContent("Saved account (unavailable)");
  await user.click(trigger);
  const unavailable = screen.getByRole("option", { name: "Saved account (unavailable)" });
  expect(unavailable).toBeDisabled();
  expect(unavailable).toHaveAttribute("aria-selected", "true");
  await user.click(unavailable);
  await user.click(screen.getByRole("option", { name: "Account without required scope" }));
  expect(onChange).not.toHaveBeenCalled();
  await user.click(screen.getByRole("option", { name: "First available account" }));
  expect(onChange).toHaveBeenCalledExactlyOnceWith("first");
  expect(trigger).toHaveTextContent("First available account");
});

it("skips disabled connections in keyboard navigation and returns focus after choosing or dismissing", async () => {
  const user = userEvent.setup();
  const onChange = vi.fn();
  render(<ConnectionSelect onChange={onChange} />);
  const trigger = screen.getByRole("combobox", { name: "Connection" });
  trigger.focus();
  await user.keyboard("{ArrowDown}");
  await waitFor(() => expect(screen.getByRole("option", { name: "First available account" })).toHaveFocus());
  await user.keyboard("{ArrowDown}");
  await waitFor(() => expect(screen.getByRole("option", { name: "Last available account" })).toHaveFocus());
  await user.keyboard("{ArrowDown}");
  await waitFor(() => expect(screen.getByRole("option", { name: "First available account" })).toHaveFocus());
  await user.keyboard("{ArrowUp}");
  await waitFor(() => expect(screen.getByRole("option", { name: "Last available account" })).toHaveFocus());
  await user.keyboard("{Home}");
  await waitFor(() => expect(screen.getByRole("option", { name: "First available account" })).toHaveFocus());
  await user.keyboard("{End}{Enter}");
  expect(onChange).toHaveBeenCalledExactlyOnceWith("last");
  expect(trigger).toHaveTextContent("Last available account");
  await waitFor(() => expect(trigger).toHaveFocus());
  await user.keyboard("{ArrowDown}{Escape}");
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  await waitFor(() => expect(trigger).toHaveFocus());
});

it("leaves an all-unavailable connection list readable without changing the saved value", async () => {
  const user = userEvent.setup();
  const onChange = vi.fn();
  render(<ConnectionSelect onChange={onChange} options={connectionOptions.filter((option) => option.disabled)} />);
  const trigger = screen.getByRole("combobox", { name: "Connection" });
  trigger.focus();
  await user.keyboard("{ArrowDown}{ArrowUp}{Home}{End}");
  await waitFor(() => expect(trigger).toHaveFocus());
  expect(trigger).toHaveTextContent("Saved account (unavailable)");
  for (const option of screen.getAllByRole("option")) expect(option).toBeDisabled();
  expect(onChange).not.toHaveBeenCalled();
  await user.keyboard("{Escape}");
  expect(trigger).toHaveAttribute("aria-expanded", "false");
});

it("dismisses an open list when disabled and does not revive it on re-enable", async () => {
  const user = userEvent.setup();
  const onChange = vi.fn();
  const props = { ariaLabel: "Connection", onChange, options: connectionOptions, value: "first" };
  const { rerender } = render(<DsSelect {...props} />);
  screen.getByRole("combobox", { name: "Connection" }).focus();
  await user.keyboard("{ArrowDown}");
  await waitFor(() => expect(screen.getByRole("option", { name: "First available account" })).toHaveFocus());
  rerender(<DsSelect {...props} disabled />);
  expect(screen.getByRole("combobox", { name: "Connection" })).toBeDisabled();
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  await user.keyboard("{ArrowDown}{Enter}");
  expect(onChange).not.toHaveBeenCalled();
  rerender(<DsSelect {...props} />);
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  await user.click(screen.getByRole("combobox", { name: "Connection" }));
  await user.click(screen.getByRole("option", { name: "Last available account" }));
  expect(onChange).toHaveBeenCalledExactlyOnceWith("last");
});
