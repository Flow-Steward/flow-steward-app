import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { AppShellMenu } from "@/components/layout/app-shell-menu";

function MenuHarness() {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <AppShellMenu
        label="Workspace"
        menuLabel="Workspace menu"
        onOpenChange={setOpen}
        open={open}
        triggerContent="Atlas"
      >
        <button role="menuitem" type="button">
          Atlas workspace
        </button>
        <button disabled role="menuitem" type="button">
          Disabled workspace
        </button>
        <button role="menuitem" type="button">
          Workspace settings
        </button>
      </AppShellMenu>
      <button type="button">Outside</button>
    </div>
  );
}

function SelectionOrderHarness({
  events,
  throwOnAccount = false,
}: Readonly<{ events: string[]; throwOnAccount?: boolean }>) {
  const [open, setOpen] = useState(false);
  return (
    <AppShellMenu
      label="Account menu"
      menuLabel="Account menu"
      onOpenChange={(nextOpen) => {
        events.push(nextOpen ? "open" : "close");
        setOpen(nextOpen);
      }}
      open={open}
      triggerContent="Tatiana"
    >
      <button
        aria-checked="false"
        onClick={() => {
          events.push("account");
          if (throwOnAccount) throw new Error("synchronous account failure");
        }}
        role="menuitemradio"
        type="button"
      >
        Switch account
      </button>
      <button
        onClick={() => events.push("logout")}
        role="menuitem"
        type="button"
      >
        Log out
      </button>
    </AppShellMenu>
  );
}

describe("AppShellMenu", () => {
  it.each([
    ["account", "Switch account"],
    ["logout", "Log out"],
  ])("runs the %s product callback before closing", (eventName, itemName) => {
    const events: string[] = [];
    render(<SelectionOrderHarness events={events} />);
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));
    events.length = 0;

    fireEvent.click(screen.getByText(itemName));

    expect(events).toEqual([eventName, "close"]);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("closes after a synchronous product-handler failure", () => {
    const events: string[] = [];
    render(<SelectionOrderHarness events={events} throwOnAccount />);
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));
    events.length = 0;
    const errors: Error[] = [];
    const preventUnhandledError = (event: ErrorEvent) => {
      errors.push(event.error as Error);
      event.preventDefault();
    };
    window.addEventListener("error", preventUnhandledError);

    fireEvent.click(
      screen.getByRole("menuitemradio", { name: "Switch account" }),
    );
    window.removeEventListener("error", preventUnhandledError);

    expect(errors[0]).toEqual(new Error("synchronous account failure"));
    expect(events).toEqual(["account", "close"]);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("opens from its trigger and focuses the directional boundary item", async () => {
    const user = userEvent.setup();
    render(<MenuHarness />);
    const trigger = screen.getByRole("button", { name: "Workspace" });

    trigger.focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Atlas workspace" })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(
      screen.getByRole("menuitem", { name: "Workspace settings" }),
    ).toHaveFocus();
  });

  it("dismisses on Escape and returns focus to its trigger", async () => {
    const user = userEvent.setup();
    render(<MenuHarness />);
    const trigger = screen.getByRole("button", { name: "Workspace" });
    await user.click(trigger);
    expect(screen.getByRole("menu")).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("dismisses after selecting an enabled action and returns focus", async () => {
    const user = userEvent.setup();
    render(<MenuHarness />);
    const trigger = screen.getByRole("button", { name: "Workspace" });

    await user.click(trigger);
    await user.click(screen.getByRole("menuitem", { name: "Atlas workspace" }));

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("returns focus after outside pointer focus transfer", async () => {
    const user = userEvent.setup();
    render(<MenuHarness />);
    const trigger = screen.getByRole("button", { name: "Workspace" });
    const outside = screen.getByRole("button", { name: "Outside" });

    await user.click(trigger);
    await user.click(outside);

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("moves focus through enabled menu actions with ArrowDown and ArrowUp", async () => {
    const user = userEvent.setup();
    render(<MenuHarness />);
    await user.click(screen.getByRole("button", { name: "Workspace" }));
    const first = screen.getByRole("menuitem", { name: "Atlas workspace" });
    const last = screen.getByRole("menuitem", { name: "Workspace settings" });

    await user.keyboard("{ArrowDown}");
    expect(first).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(last).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(first).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(last).toHaveFocus();
  });
});
