import type * as SwitchAdapter from "./ds-switch";
import type * as AccordionAdapter from "./ds-accordion";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

const accordions = import.meta.glob<typeof AccordionAdapter>("./ds-accordion.tsx");
const switches = import.meta.glob<typeof SwitchAdapter>("./ds-switch.tsx");

it("keeps disclosure single-open and the sibling switch independent by mouse and keyboard", async () => {
  expect(accordions["./ds-accordion.tsx"], "published accordion adapter").toBeDefined();
  expect(switches["./ds-switch.tsx"], "published switch adapter").toBeDefined();
  const { DsAccordion } = await accordions["./ds-accordion.tsx"]();
  const { DsSwitch } = await switches["./ds-switch.tsx"]();
  function Harness() {
    const [openId, setOpenId] = useState<string | null>(null);
    const [checked, setChecked] = useState(false);
    return <DsAccordion openId={openId} onOpenChange={setOpenId} items={[
      { id: "first", header: "First", panel: "First content", accessory: <DsSwitch checked={checked} onCheckedChange={setChecked} label="Run first automation" /> },
      { id: "second", header: "Second", panel: "Second content" },
    ]} />;
  }
  render(<Harness />);
  const user = userEvent.setup();
  const first = screen.getByRole("button", { name: "First" });
  const second = screen.getByRole("button", { name: "Second" });
  const setting = screen.getByRole("switch", { name: "Run first automation" });
  await user.click(first);
  expect(first).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("region", { name: "First" })).toHaveAttribute("id", first.getAttribute("aria-controls"));
  await user.click(setting);
  expect(setting).toBeChecked();
  expect(first).toHaveAttribute("aria-expanded", "true");
  expect(first).not.toContainElement(setting);
  await user.click(second);
  expect(first).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("region", { name: "First" })).not.toBeInTheDocument();
  expect(second).toHaveAttribute("aria-expanded", "true");
  await user.keyboard("{Enter}");
  expect(second).toHaveAttribute("aria-expanded", "false");
  await user.click(setting);
  await user.keyboard(" ");
  expect(setting).toBeChecked();
  expect(second).toHaveAttribute("aria-expanded", "false");
});
