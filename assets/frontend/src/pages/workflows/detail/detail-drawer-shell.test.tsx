import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState, type ReactNode } from "react";
import { vi } from "vitest";

import { DsDialog, DsSelect } from "@/components/shared/design-system";
import { WorkflowDrawerShell } from "./detail-drawer-shell";
import { useWorkflowDrawerController } from "./detail-drawer-controller";
import { useWorkflowBuilderDrawerResize } from "./detail-builder-drawer-resize";
import { BUILDER_DRAWER_WIDTH_STORAGE_KEY } from "./detail-core";

vi.mock("@/lib/auth/use-is-admin", () => ({ useIsAdmin: () => true }));

function DrawerHarness({ mode = "workflow-settings", locked = false, forEach = false, hook = false, openerState = "visible", context = "first", isolatedAllowed = false, advanced = false, channels, interfaceEnabled = true, children }: { mode?: string; locked?: boolean; forEach?: boolean; hook?: boolean; openerState?: "visible" | "removed" | "hidden" | "disabled" | "nonfocusable"; context?: string; isolatedAllowed?: boolean; advanced?: boolean; channels?: string[]; interfaceEnabled?: boolean; children?: ReactNode }) {
  const [drawerMode, setDrawerMode] = useState("");
  const [drawerTab, setDrawerTab] = useState(mode === "group" ? "settings" : mode === "workflow-interface" ? "inputs" : "smart-setup");
  const [draft, setDraft] = useState("");
  const [confirmation, setConfirmation] = useState<((accept: boolean) => void) | null>(null);
  const fallbackRef = useRef<HTMLButtonElement>(null);
  const resize = useWorkflowBuilderDrawerResize(true);
  const controller = useWorkflowDrawerController({
    drawerMode, setDrawerMode, drawerTab, setDrawerTab,
    definitionRows: { stepRowsById: new Map(), groupRows: [] },
    groupDraft: { groupId: "", containerKind: forEach ? "for_each" : "group" },
    selectedGroupId: "", selectedStepId: "", searchParams: new URLSearchParams(),
    hasInvalidStepInput: Boolean(draft),
    confirmDiscardInvalidInput: () => new Promise<boolean>((resolve) => setConfirmation(() => resolve)),
    setSelectedCanvasIssue: () => undefined,
  });
  const answer = (accept: boolean) => { confirmation?.(accept); setConfirmation(null); };
  return <>
    <button ref={fallbackRef} type="button">Canvas tools</button>
    {openerState === "nonfocusable" ? <span>Inspect node</span> : openerState !== "removed" ? <button disabled={openerState === "disabled"} hidden={openerState === "hidden"} onClick={() => setDrawerMode(mode)} type="button">Inspect node</button> : null}
    <WorkflowDrawerShell allowStepTestRun={isolatedAllowed} ctx={{ ...resize, ...controller, advancedMode: advanced, drawerMode, drawerTab, drawerOpen: Boolean(drawerMode), builderSurface: true, structureLocked: locked, groupDraft: { containerKind: forEach ? "for_each" : "group" }, selectedStepHookId: hook ? "hook-a" : "", drawerFocusContext: `${drawerMode}:${context}`, drawerReturnFocusOwner: context, drawerFocusFallback: () => fallbackRef.current, ...(channels ? { workflowInterfaceTriggerChannelsDraft: channels, workflowInterfaceEnabledDraft: interfaceEnabled } : {}) }}>
      <input aria-label="Parent draft" value={draft} onChange={(event) => setDraft(event.target.value)} />
      {mode === "workflow-interface" ? <div role="tabpanel" aria-labelledby={`workflow-interface-tab-${drawerTab}`} id={`workflow-contract-section-${drawerTab}`}>{drawerTab}</div> : null}
      {children}
    </WorkflowDrawerShell>
    <DsDialog open={Boolean(confirmation)} title="Discard invalid input?" description="Your draft is still available." eyebrow="Unsaved changes" onClose={() => answer(false)} footer={<><button onClick={() => answer(false)} type="button">Keep editing</button><button onClick={() => answer(true)} type="button">Discard</button></>} />
  </>;
}

describe("workflow contextual inspector shell", () => {
  it("removes the closed panel, close action and editors from DOM and accessibility tree", () => {
    render(<DrawerHarness />);
    expect(screen.queryByRole("button", { name: "Close", hidden: true })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Parent draft")).not.toBeInTheDocument();
    expect(screen.queryByRole("complementary", { name: "builder drawer", hidden: true })).not.toBeInTheDocument();
  });

  it("opens a named non-modal shared drawer and returns focus to its visible opener", async () => {
    const user = userEvent.setup();
    render(<DrawerHarness />);
    const opener = screen.getByRole("button", { name: "Inspect node" });
    await user.click(opener);
    const panel = screen.getByRole("dialog", { name: "Workflow settings" });
    expect(panel).toHaveClass("fs-drawer", "workflow-builder-drawer");
    expect(panel).not.toHaveAttribute("aria-modal");
    expect(document.body).not.toHaveStyle({overflow:"hidden"});
    await waitFor(() => expect(within(panel).getByRole("button", { name: "Close" })).toHaveFocus());
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("keeps parent draft and panel mounted when the actual async close guard is cancelled", async () => {
    const user = userEvent.setup();
    render(<DrawerHarness />);
    await user.click(screen.getByRole("button", { name: "Inspect node" }));
    await user.type(screen.getByRole("textbox", { name: "Parent draft" }), "unsaved draft");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByRole("dialog", { name: "Discard invalid input?" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Parent draft" })).toHaveValue("unsaved draft");
    await user.keyboard("{Escape}");
    expect(screen.getByRole("dialog", { name: "Workflow settings" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Discard invalid input?" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByRole("dialog", { name: "Workflow settings" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.queryByLabelText("Parent draft")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Inspect node" }));
    expect(screen.getByRole("textbox", { name: "Parent draft" })).toHaveValue("unsaved draft");
  });

  it("lets a nested popup consume Escape before dismissing the inspector", async () => {
    const user = userEvent.setup();
    render(<DrawerHarness><DsSelect ariaLabel="Source" value="one" options={[{ value: "one", label: "One" }, { value: "two", label: "Two" }]} onChange={() => undefined} /></DrawerHarness>);
    await user.click(screen.getByRole("button", { name: "Inspect node" }));
    await user.click(screen.getByRole("combobox", { name: "Source" }));
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Workflow settings" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  // R2-16: a channel's tab appears only once that channel can start the workflow.
  it.each([
    { channels: ["ui", "api"], interfaceEnabled: true, names: ["Smart setup", "Automation", "History"] },
    { channels: ["webhook"], interfaceEnabled: true, names: ["Smart setup", "Webhook", "Automation", "History"] },
    { channels: ["schedule"], interfaceEnabled: true, names: ["Smart setup", "Schedules", "Automation", "History"] },
    { channels: ["webhook", "schedule"], interfaceEnabled: true, names: ["Smart setup", "Webhook", "Schedules", "Automation", "History"] },
    { channels: ["webhook", "schedule"], interfaceEnabled: false, names: ["Smart setup", "Automation", "History"] },
  ])("shows workflow settings tabs for the channels that are on: $channels (interface $interfaceEnabled)", async ({ channels, interfaceEnabled, names }) => {
    const user = userEvent.setup();
    render(<DrawerHarness channels={channels} interfaceEnabled={interfaceEnabled} />);
    await user.click(screen.getByRole("button", { name: "Inspect node" }));
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual(names);
  });

  it("goes back to Smart setup when the open tab's channel is turned off", async () => {
    const user = userEvent.setup();
    const view = render(<DrawerHarness channels={["webhook"]} />);
    await user.click(screen.getByRole("button", { name: "Inspect node" }));
    await user.click(screen.getByRole("tab", { name: "Webhook" }));
    expect(screen.getByRole("tab", { name: "Webhook" })).toHaveAttribute("aria-selected", "true");
    view.rerender(<DrawerHarness channels={[]} />);
    await waitFor(() => expect(screen.getByRole("tab", { name: "Smart setup" })).toHaveAttribute("aria-selected", "true"));
    expect(screen.queryByRole("tab", { name: "Webhook" })).not.toBeInTheDocument();
  });

  it("keeps the actual tab guard in charge of selection and exposes only existing controlled panels", async () => {
    const user = userEvent.setup();
    render(<DrawerHarness />);
    await user.click(screen.getByRole("button", { name: "Inspect node" }));
    const tabs = screen.getAllByRole("tab");
    expect(tabs.filter((tab) => tab.tabIndex === 0)).toHaveLength(1);
    for (const tab of tabs) expect(tab).toHaveAttribute("aria-controls", screen.getByRole("tabpanel").id);
    await user.type(screen.getByRole("textbox", { name: "Parent draft" }), "draft");
    await user.click(screen.getByRole("tab", { name: "Webhook" }));
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByRole("tab", { name: "Smart setup" })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("tab", { name: "Webhook" }));
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", screen.getByRole("tab", { name: "Webhook" }).id);
  });

  it.each([
    { mode: "group", locked: true, names: ["Settings"] },
    { mode: "group", forEach: true, names: ["Settings"] },
    { mode: "step", locked: true, names: ["Smart setup"] },
    { mode: "step", hook: true, names: ["Smart setup", "Test Run", "Automation"] },
    { mode: "library", names: ["Smart setup"] },
    { mode: "workflow-test-run", names: [] },
  ])("preserves allowed tabs for $mode locked=$locked forEach=$forEach hook=$hook", async ({ names, ...props }) => {
    render(<DrawerHarness {...props} />);
    await userEvent.click(screen.getByRole("button", { name: "Inspect node" }));
    expect(screen.queryAllByRole("tab").map((tab) => tab.textContent)).toEqual(names);
  });

  it("retains every fixed interface ID without duplicates as its actual child switches panels", async () => {
    render(<DrawerHarness mode="workflow-interface" />);
    await userEvent.click(screen.getByRole("button", { name: "Inspect node" }));
    for (const name of ["Inputs", "Outputs"]) {
      await userEvent.click(screen.getByRole("tab", { name }));
      const panels = screen.getAllByRole("tabpanel", { hidden: true });
      const ids = panels.map((panel) => panel.id);
      expect(panels).toHaveLength(2);
      expect(new Set(ids).size).toBe(2);
      for (const tab of screen.getAllByRole("tab")) expect(ids).toContain(tab.getAttribute("aria-controls"));
      const activeTab = screen.getByRole("tab", { name });
      expect(screen.getByRole("tabpanel")).toHaveAttribute("id", activeTab.getAttribute("aria-controls"));
      expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", activeTab.id);
    }
  });

  it.each(["removed", "hidden", "disabled", "nonfocusable"] as const)("uses its explicit visible page fallback when the opener becomes %s", async (openerState) => {
    const user = userEvent.setup();
    const view = render(<DrawerHarness />);
    await user.click(screen.getByRole("button", { name: "Inspect node" }));
    view.rerender(<DrawerHarness openerState={openerState} />);
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Canvas tools" })).toHaveFocus();
  });

  it("does not return focus to the old opener after an internal context switch", async () => {
    const user = userEvent.setup();
    const view = render(<DrawerHarness />);
    await user.click(screen.getByRole("button", { name: "Inspect node" }));
    screen.getByRole("textbox", { name: "Parent draft" }).focus();
    view.rerender(<DrawerHarness context="second" />);
    expect(screen.getByRole("textbox", { name: "Parent draft" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.getByRole("button", { name: "Canvas tools" })).toHaveFocus();
  });

  it("keeps page-controlled width and keyboard resize persistence", async () => {
    localStorage.clear();
    render(<DrawerHarness />);
    await userEvent.click(screen.getByRole("button", { name: "Inspect node" }));
    const panel = screen.getByRole("dialog");
    const previous = Number(panel.dataset.width);
    const handle = screen.getByRole("button", { name: "Resize builder inspector" });
    handle.focus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(Number(panel.dataset.width)).toBe(previous + 24);
    expect(panel).toHaveStyle({width:`${previous + 24}px`});
    expect(localStorage.getItem(BUILDER_DRAWER_WIDTH_STORAGE_KEY)).toBe(String(previous + 24));
  });
});


describe("explicit isolated permission on a locked Step inspector", () => {
  it.each([true, false])("keeps editing tabs closed while isolated permission=%s controls Test Run", async (allowed) => {
    const user = userEvent.setup();
    render(<DrawerHarness mode="step" locked isolatedAllowed={allowed} advanced />);
    await user.click(screen.getByRole("button", { name: "Inspect node" }));
    const drawer = screen.getByRole("dialog", { name: "Step editor" });
    expect(within(drawer).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(allowed ? ["Smart setup", "Test Run"] : ["Smart setup"]);
    expect(within(drawer).queryByRole("tab", { name: "Automation" })).not.toBeInTheDocument();
    expect(within(drawer).queryByRole("tab", { name: "Advanced" })).not.toBeInTheDocument();
    if (allowed) {
      await user.click(within(drawer).getByRole("tab", { name: "Test Run" }));
      expect(within(drawer).getByRole("tab", { name: "Test Run" })).toHaveAttribute("aria-selected", "true");
    }
  });
});


function ReturnOwnerDrawerHarness({ open, owner }: { open: boolean; owner: string }) {
  const fallbackRef = useRef<HTMLButtonElement>(null);
  const drawerMode = open ? "workflow-settings" : "";
  return <>
    <button ref={fallbackRef} type="button">Owner fallback</button>
    <button type="button">Owner opener</button>
    <WorkflowDrawerShell ctx={{
      builderSurface: true, drawerMode, drawerOpen: open, drawerTab: "smart-setup", drawerTitle: "Owned settings",
      drawerFocusContext: `${drawerMode}:${owner}`, drawerReturnFocusOwner: owner,
      drawerFocusFallback: () => fallbackRef.current, closeDrawer: () => {}, changeDrawerTab: () => {},
    }}><input aria-label="Owned settings detail" /></WorkflowDrawerShell>
  </>;
}

it.each([false, true])("keeps close-only mode normalization separate from a replacement return owner (changed=%s)", async (changedOwner) => {
  const user = userEvent.setup();
  const view = render(<ReturnOwnerDrawerHarness open={false} owner="workflow-a:run-a" />);
  const opener = screen.getByRole("button", { name: "Owner opener" });
  const fallback = screen.getByRole("button", { name: "Owner fallback" });
  await user.click(opener);
  view.rerender(<ReturnOwnerDrawerHarness open owner="workflow-a:run-a" />);
  const drawer = screen.getByRole("dialog", { name: "Owned settings" });
  await waitFor(() => expect(within(drawer).getByRole("button", { name: "Close" })).toHaveFocus());
  view.rerender(<ReturnOwnerDrawerHarness open={false} owner={changedOwner ? "workflow-b:run-b" : "workflow-a:run-a"} />);
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Owned settings" })).not.toBeInTheDocument());
  if (changedOwner) {
    expect(opener).not.toHaveFocus();
    expect(fallback).not.toHaveFocus();
  } else {
    await waitFor(() => expect(opener).toHaveFocus());
  }
});

it("names what the drawer edits under its title and keeps the footer actions out of the scrolling body", async () => {
  const { DrawerFooter } = await import("./detail-drawer-footer");
  render(
    <WorkflowDrawerShell ctx={{ drawerMode: "step", drawerTab: "smart-setup", drawerOpen: true, drawerTitle: "Step editor", drawerSubtitle: "fulfil · create_shipment", builderSurface: true }}>
      <p>Step fields</p>
      <DrawerFooter><button type="button">Save parameters</button></DrawerFooter>
    </WorkflowDrawerShell>,
  );
  const drawer = screen.getByRole("dialog", { name: "Step editor" });
  expect(within(drawer).getByText("fulfil · create_shipment")).toBeInTheDocument();
  const footer = within(drawer).getByTestId("builder-drawer-footer");
  await waitFor(() => expect(within(footer).getByRole("button", { name: "Save parameters" })).toBeInTheDocument());
  expect(within(screen.getByTestId("builder-drawer-body")).queryByRole("button", { name: "Save parameters" })).not.toBeInTheDocument();
});
