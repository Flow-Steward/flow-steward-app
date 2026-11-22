import type * as SwitchAdapter from "./ds-switch";
import type * as AccordionAdapter from "./ds-accordion";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import accordionStyles from "./ds-accordion.css?raw";

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

it("sizes the accessory slot for what it holds, so two icon buttons stay clear of the chevron", async () => {
  const { DsAccordion } = await accordions["./ds-accordion.tsx"]();
  render(<DsAccordion openId={null} onOpenChange={() => undefined} items={[
    { id: "switch", header: "Switch row", panel: "", accessory: <span /> },
    { id: "pair", header: "Pair row", panel: "", accessory: <span />, accessorySlot: "icon-pair" },
    { id: "plain", header: "Plain row", panel: "" },
  ]} />);
  const header = (name: string) => screen.getByRole("button", { name }).parentElement;
  expect(header("Switch row")).toHaveAttribute("data-accessory-slot", "switch");
  expect(header("Pair row")).toHaveAttribute("data-accessory-slot", "icon-pair");
  expect(header("Plain row")).not.toHaveAttribute("data-accessory-slot");
  // The trigger reserves the same width the accessory is given.
  const css = accordionStyles.replace(/\s+/g, " ");
  expect(css).toMatch(/\[data-accessory-slot="icon-pair"\] \{ --fs-accordion-accessory-width: calc\(var\(--fs-icon-button-size-sm\) \* 2 \+ 4px\); \}/);
  expect(css).toMatch(/grid-template-columns: minmax\(0, 1fr\) 10px var\(--fs-accordion-accessory-width\) 12px 15px;/);
  expect(css).toMatch(/\.fs-accordion-accessory \{[^}]*width: var\(--fs-accordion-accessory-width\);/);
  // The list lines up with the panel's other content: it adds no inset of its own.
  expect(css).toMatch(/\.fs-accordion-list \{ display: grid; gap: 6px; \}/);
});
