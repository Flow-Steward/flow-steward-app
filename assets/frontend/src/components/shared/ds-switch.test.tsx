import type * as SwitchAdapter from "./ds-switch";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

const adapters = import.meta.glob<typeof SwitchAdapter>("./ds-switch.tsx");

it("reports controlled checked state, helper description, keyboard changes and disabled semantics", async () => {
  expect(adapters["./ds-switch.tsx"], "published switch adapter").toBeDefined();
  const { DsSwitch } = await adapters["./ds-switch.tsx"]();
  const change = vi.fn();
  const { rerender } = render(<><p id="reason">Only project owners can change this.</p><DsSwitch checked={false} onCheckedChange={change} label="Run automation" describedBy="reason" /></>);
  const control = screen.getByRole("switch", { name: "Run automation" });
  expect(control.tagName).toBe("INPUT");
  expect(control).not.toBeChecked();
  expect(control).toHaveAccessibleDescription("Only project owners can change this.");
  const user = userEvent.setup();
  await user.tab();
  expect(control).toHaveFocus();
  await user.keyboard(" ");
  expect(change).toHaveBeenLastCalledWith(true);
  expect(control).not.toBeChecked();
  rerender(<DsSwitch checked onCheckedChange={change} label="Run automation" disabled />);
  const disabledControl = screen.getByRole("switch", { name: "Run automation" });
  expect(disabledControl).toBeChecked();
  expect(disabledControl).toBeDisabled();
  await user.click(disabledControl);
  expect(change).toHaveBeenCalledTimes(1);
});
