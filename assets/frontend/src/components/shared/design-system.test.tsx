import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { IconButton } from "@/components/ui/icon-button";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useCallback, useEffect, useRef, useState } from "react";
import { vi } from "vitest";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { beginRequest } from "@/lib/http/request-activity";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { useConfirmDialog } from "@/components/shared/use-confirm-dialog";
import { RequestLoadingIndicator } from "@/components/shared/request-loading-indicator";

import settingsStyles from "@/components/shared/design-system.css?raw";
import shellStyles from "@/styles/design-system-shell.css?raw";

import {
  AdvancedTableFilters,
  BulkActionBar,
  DsAlert,
  DsDrawer,
  DsProgress,
  DsBadge,
  DsDialog,
  DsSelect,
  OptionalLabel,
  OptionalMarker,
  DsTable,
  DsTabs,
  MultiCombobox,
  SingleCombobox,
  type DsTableSort,
} from "@/components/shared/design-system";

const options = [
  { value: "salesforce", label: "Salesforce" },
  { value: "spreadsheet", label: "Spreadsheet" },
  { value: "hubspot", label: "HubSpot" },
];

function SelectHarness() {
  const [value, setValue] = useState("");
  return <DsSelect ariaLabel="Source" onChange={setValue} options={options} required value={value} />;
}

function ComboboxHarness() {
  const [selected, setSelected] = useState<string[]>([]);
  return <section aria-label="Combobox host"><MultiCombobox ariaLabel="Account memberships" onChange={setSelected} options={options} placeholder="Search accounts" required selected={selected} /></section>;
}

function SingleComboboxHarness() {
  const [value, setValue] = useState("salesforce");
  return <SingleCombobox ariaLabel="Timezone" onChange={setValue} options={options} value={value} />;
}

function DialogHarness() {
  const [open, setOpen] = useState(false);
  return <><button onClick={() => setOpen(true)} type="button">Open dialog</button><DsDialog closeLabel="Close edit settings dialog" description="Dialog contract" eyebrow="Settings" footer={<button type="button">Save</button>} onClose={() => setOpen(false)} open={open} title="Edit settings"><input aria-label="Display name" /></DsDialog></>;
}

function StatefulDialogHarness() {
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  return <><button onClick={() => setOpen(true)} type="button">Create token</button><DsDialog description="Dialog contract" eyebrow="Personal tokens" footer={<button type="button">Create</button>} onClose={() => setOpen(false)} open={open} title="Create personal token"><input aria-label="Label" onChange={(event) => setLabel(event.target.value)} value={label} /></DsDialog></>;
}

function ShadowDialogHarness() {
  const [open, setOpen] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const openDialog = useCallback(() => setOpen(true), []);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const shadowRoot = host.attachShadow({ mode: "open" });
    const opener = document.createElement("button");
    opener.textContent = "Open shadow dialog";
    opener.type = "button";
    opener.addEventListener("click", openDialog);
    shadowRoot.append(opener);
    return () => opener.removeEventListener("click", openDialog);
  }, [openDialog]);

  return (
    <>
      <div data-testid="shadow-dialog-host" ref={hostRef} />
      <DsDialog
        description="Shadow dialog contract"
        eyebrow="Settings"
        onClose={() => setOpen(false)}
        open={open}
        title="Shadow settings"
      />
    </>
  );
}

const overflowTabItems = [
  { key: "smart", label: "Smart setup" },
  { key: "test", label: "Test Run" },
  { key: "automation", label: "Automation" },
  { key: "advanced", label: "Advanced" },
];

function OverflowTabsHarness({ revealActiveTab = false }: { revealActiveTab?: boolean }) {
  const [active, setActive] = useState("smart");
  const props = { active, ariaLabel: "Overflowing sections", idPrefix: "overflow", items: overflowTabItems, onSelect: setActive, revealActiveTab };
  return <section data-testid="tabs-outer"><div data-testid="tabs-body" /><DsTabs {...props} /></section>;
}

function installTabStripLayout(width = 421) {
  // eslint-disable-next-line testing-library/no-node-access -- The real scrollport has no extra semantic role; this is a JSDOM layout witness only.
  const scrollport = screen.getByRole("tablist", { name: "Overflowing sections" }).parentElement;
  if (!(scrollport instanceof HTMLElement)) throw new Error("Expected the actual DsTabs scrollport");
  Object.defineProperty(scrollport, "clientWidth", { configurable: true, value: width });
  Object.defineProperty(scrollport, "scrollWidth", { configurable: true, value: 540 });
  vi.spyOn(scrollport, "getBoundingClientRect").mockImplementation(() => new DOMRect(1026, 152, width, 38));
  for (const [index, item] of overflowTabItems.entries()) {
    const tab = screen.getByRole("tab", { name: item.label });
    vi.spyOn(tab, "getBoundingClientRect").mockImplementation(() => new DOMRect(1026 + index * 136 - scrollport.scrollLeft, 152, 132, 34));
  }
  return scrollport;
}

describe("shared design-system adapters", () => {
  it.each([
    ["en", "Description", "Project", "Allowed workflows", "(optional)"],
    ["lt", "Aprašymas", "Projektas", "Leidžiamos darbo eigos", "(nebūtina)"],
  ])("includes the separate localized optional note in field and group names (%s)", (locale, description, project, workflows, marker) => {
    render(<I18nContext.Provider value={{ ...fallbackContext, locale, t: (key, fallback = "") => key === "auth.common.optional" ? marker : fallback }}>
      <label><OptionalLabel>{description}</OptionalLabel><input /></label>
      <OptionalLabel id="optional-project-label">{project}</OptionalLabel>
      <DsSelect ariaLabel={project} labelledBy="optional-project-label" onChange={() => undefined} options={options} value="salesforce" />
      <fieldset><legend>{workflows}{" "}<OptionalMarker /></legend><input type="checkbox" aria-label="Example workflow" /></fieldset>
    </I18nContext.Provider>);
    expect(screen.getByRole("textbox", { name: `${description} ${marker}` })).not.toBeRequired();
    expect(screen.getByRole("combobox", { name: `${project} ${marker}` })).toHaveTextContent("Salesforce");
    const group = screen.getByRole("group", { name: `${workflows} ${marker}` });
    expect(within(group).getByText(marker)).not.toHaveAttribute("aria-hidden");
    expect(screen.getByRole("checkbox", { name: "Example workflow" })).not.toBeChecked();
    expect(screen.getByText(project, { selector: "#optional-project-label" })).toHaveTextContent(`${project} ${marker}`, { normalizeWhitespace: false });
  });
  it("keeps the filter menu out of the clipping table card", async () => {
    // `.fs-table-card` is `overflow: hidden`, so a menu rendered inside it is
    // cut off at the card's bottom edge — canon says a table popup "must never
    // be clipped by the table".
    function Harness() {
      const [values, setValues] = useState<Record<string, string>>({});
      return (
        <article className="fs-card fs-table-card" data-testid="table-card">
          <div className="fs-table-toolbar">
            <AdvancedTableFilters
              fields={[
                { key: "workspace", label: "Workspace", options },
                { key: "health", label: "Health", options },
              ]}
              onChange={(key, value) => setValues((current) => ({ ...current, [key]: value }))}
              onClear={() => setValues({})}
              values={values}
            />
          </div>
        </article>
      );
    }
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("button", { name: "Add filter" }));
    const menu = await screen.findByRole("menu", { name: "Choose a filter" });

    expect(screen.getByTestId("table-card").contains(menu)).toBe(false);
    expect(document.body.contains(menu)).toBe(true);
  });

  describe("filter menu with many options", () => {
    const manyOptions = (count: number) => Array.from({ length: count }, (_, index) => ({
      value: `wf-${index + 1}`,
      label: index === 3 ? "Classify support requests with a very long workflow name that will not fit" : `Workflow ${index + 1}`,
      description: index === 3 ? "Support project" : undefined,
    }));

    function Harness({ count, onChange = () => undefined }: Readonly<{ count: number; onChange?: (key: string, value: string) => void }>) {
      const [values, setValues] = useState<Record<string, string>>({});
      return (
        <AdvancedTableFilters
          fields={[{ key: "workflow", label: "Workflow", options: manyOptions(count) }]}
          onChange={(key, value) => { onChange(key, value); setValues((current) => ({ ...current, [key]: value })); }}
          onClear={() => setValues({})}
          values={values}
        />
      );
    }

    async function openWorkflowField(user: ReturnType<typeof userEvent.setup>) {
      await user.click(screen.getByRole("button", { name: "Add filter" }));
      await user.click(await screen.findByRole("menuitem", { name: "Workflow" }));
      return screen.findByRole("menu", { name: "Choose a filter" });
    }

    it("shows no search box at ten options", async () => {
      const user = userEvent.setup();
      render(<Harness count={10} />);
      const menu = await openWorkflowField(user);
      expect(within(menu).queryByRole("searchbox")).not.toBeInTheDocument();
      expect(within(menu).getAllByRole("menuitem")).toHaveLength(11);
    });

    it("searches above ten options, focused on open and case-insensitive", async () => {
      const user = userEvent.setup();
      render(<Harness count={11} />);
      const menu = await openWorkflowField(user);
      const search = within(menu).getByRole("searchbox", { name: "Search workflow" });
      await waitFor(() => expect(search).toHaveFocus());

      await user.type(search, "CLASSIFY");
      const rows = within(menu).getAllByRole("menuitem").filter((item) => item.textContent !== "All fields");
      expect(rows).toHaveLength(1);
      // One line per label; the full text and the description stay in the title.
      expect(rows[0]).toHaveAttribute("title", "Classify support requests with a very long workflow name that will not fit · Support project");

      await user.type(search, "zzz");
      expect(within(menu).getByText("No results")).toBeInTheDocument();
    });

    it("walks the search box and the options with the arrow keys and selects with Enter", async () => {
      const onChange = vi.fn();
      const user = userEvent.setup();
      render(<Harness count={12} onChange={onChange} />);
      const menu = await openWorkflowField(user);
      const search = within(menu).getByRole("searchbox", { name: "Search workflow" });
      await waitFor(() => expect(search).toHaveFocus());

      await user.keyboard("{ArrowDown}");
      expect(within(menu).getByRole("menuitem", { name: "Workflow 1" })).toHaveFocus();
      await user.keyboard("{ArrowDown}");
      expect(within(menu).getByRole("menuitem", { name: "Workflow 2" })).toHaveFocus();
      await user.keyboard("{ArrowUp}{ArrowUp}");
      expect(search).toHaveFocus();
      await user.keyboard("{ArrowUp}");
      expect(within(menu).getByRole("menuitem", { name: "All fields" })).toHaveFocus();
      await user.keyboard("{End}");
      expect(within(menu).getByRole("menuitem", { name: "Workflow 12" })).toHaveFocus();

      await user.keyboard("{Enter}");
      expect(onChange).toHaveBeenCalledWith("workflow", "wf-12");
      expect(screen.queryByRole("menu", { name: "Choose a filter" })).not.toBeInTheDocument();
    });

    it("keeps the menu wide, capped to the viewport, with a scrolling one-line list", () => {
      expect(settingsStyles).toMatch(/\.fs-filter-menu \{[^}]*width:\s*min\(340px,\s*calc\(100vw - \(2 \* var\(--fs-space-200\)\)\)\)/s);
      expect(settingsStyles).toMatch(/\.fs-filter-menu-options \{[^}]*max-height:[^;]+;[^}]*overflow-y:\s*auto/s);
      expect(settingsStyles).toMatch(/\.fs-filter-menu-label,\s*\.fs-filter-menu-description \{[^}]*text-overflow:\s*ellipsis;[^}]*white-space:\s*nowrap/s);
    });
  });

  it("keeps the Card as the single top boundary for the Personal tokens table", () => {
    expect(settingsStyles).toMatch(
      /\.fs-token-table-card\s*>\s*\.fs-table-scroll\s*\{[^}]*border-top:\s*0;/s,
    );
  });

  it("clips the Personal tokens scroll area to the Card top corners", () => {
    expect(settingsStyles).toMatch(
      /\.fs-token-table-card\s*>\s*\.fs-table-scroll\s*\{[^}]*border-radius:\s*var\(--fs-card-radius\)\s+var\(--fs-card-radius\)\s+0\s+0;/s,
    );
  });

  it("layers Dialog Combobox menus above the modal", () => {
    expect(settingsStyles).toMatch(
      /\.fs-multi-menu-layer\s*\{[^}]*z-index:\s*var\(--fs-z-modal-popup\)/s,
    );
    expect(settingsStyles).toContain('[data-has-selection="true"][data-has-query="false"]');
    expect(settingsStyles).toContain("min-width: var(--fs-space-025)");
  });

  it("supports Select arrow navigation, selection and trigger focus return", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<SelectHarness />);
    const trigger = screen.getByRole("combobox", { name: "Source" });
    expect(trigger).toBeRequired();
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    expect(await screen.findByRole("option", { name: "Salesforce" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Enter}");
    expect(trigger).toHaveTextContent("Spreadsheet");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByRole("listbox", { name: "Source options" })).not.toBeInTheDocument();
  });

  it("gives a Select option label the truncating title element", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<SelectHarness />);
    await user.click(screen.getByRole("combobox", { name: "Source" }));
    // The label carried no class at all, so canon's ellipsis rule had nothing to
    // bind to and a label wider than its column ran under the check instead of
    // truncating. `select-preview.html:433`.
    const option = await screen.findByRole("option", { name: "Salesforce" });
    expect(within(option).getByText("Salesforce")).toHaveClass("fs-select-option-title");
  });

  it("keeps multi-Combobox focus in the query while toggling options", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<ComboboxHarness />);
    const input = screen.getByRole("combobox", { name: "Account memberships" });
    expect(input).toBeRequired();
    await user.click(input);
    await user.keyboard("{ArrowDown}{Enter}");
    expect(input).toHaveFocus();
    expect(input).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByText("Salesforce").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "Remove Salesforce" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Remove Spreadsheet" })).toBeVisible();
    expect(within(screen.getByRole("region", { name: "Combobox host" })).queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.getByRole("listbox", { name: "Account memberships options" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(input).toHaveFocus();
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Remove Salesforce" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    await user.keyboard("{Backspace}");
    expect(screen.getByText("0 selected")).toBeInTheDocument();
  });

  it("anchors an above-opening multi-Combobox to its field on the first open", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const rectSpy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const top = this.classList.contains("fs-multi-field") ? 700 : 0;
      const left = this.classList.contains("fs-multi-field") ? 100 : 0;
      const width = this.classList.contains("fs-multi-field") ? 300 : 0;
      const height = this.classList.contains("fs-multi-field") ? 32 : 0;
      return { x: left, y: top, top, left, right: left + width, bottom: top + height, width, height, toJSON: () => ({}) } as DOMRect;
    });
    const scrollHeightSpy = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("fs-multi-menu") ? 128 : 0;
    });

    try {
      render(<ComboboxHarness />);
      await user.click(screen.getByRole("combobox", { name: "Account memberships" }));
      const listbox = await screen.findByRole("listbox", { name: "Account memberships options" });

      await waitFor(() => {
        expect(listbox).toHaveStyle({
          top: "auto",
          bottom: `${window.innerHeight - 700 + 6}px`,
          left: "100px",
          width: "300px",
        });
      });
    } finally {
      rectSpy.mockRestore();
      scrollHeightSpy.mockRestore();
    }
  });

  it("flips a Select menu above when the dialog footer leaves no room below", async () => {
    // The agent dialog's Model select sits just above the footer. Its inline menu
    // was clipped there and showed one option; it now shares the multi-Combobox
    // placement and opens above the trigger in the viewport layer.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const rectSpy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const box = (top: number, left: number, width: number, height: number) =>
        ({ x: left, y: top, top, left, right: left + width, bottom: top + height, width, height, toJSON: () => ({}) }) as DOMRect;
      if (this.classList.contains("fs-select-trigger")) return box(500, 120, 280, 32);
      if (this.classList.contains("fs-dialog-footer")) return box(560, 0, 600, 64);
      return box(0, 0, 0, 0);
    });
    const scrollHeightSpy = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("fs-select-menu") ? 120 : 0;
    });

    try {
      render(
        <section aria-label="Select host" className="fs-dialog">
          <div className="fs-dialog-body"><SelectHarness /></div>
          <footer className="fs-dialog-footer"><button type="button">Create</button></footer>
        </section>,
      );
      await user.click(screen.getByRole("combobox", { name: "Source" }));
      const listbox = await screen.findByRole("listbox", { name: "Source options" });

      expect(within(screen.getByRole("region", { name: "Select host" })).queryByRole("listbox")).not.toBeInTheDocument();
      await waitFor(() => {
        expect(listbox).toHaveStyle({ top: "auto", bottom: `${window.innerHeight - 500 + 6}px`, left: "120px", width: "280px", maxHeight: "120px" });
      });
      // A press inside the portalled menu is not an outside press: the option still selects.
      await user.click(within(listbox).getByRole("option", { name: "HubSpot" }));
      expect(screen.getByRole("combobox", { name: "Source" })).toHaveTextContent("HubSpot");
      expect(screen.queryByRole("listbox", { name: "Source options" })).not.toBeInTheDocument();
    } finally {
      rectSpy.mockRestore();
      scrollHeightSpy.mockRestore();
    }
  });

  it("opens a Select menu below its trigger when there is room, and closes it on an outside press", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const rectSpy = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const isTrigger = this.classList.contains("fs-select-trigger");
      const [top, left, width, height] = isTrigger ? [100, 40, 240, 32] : [0, 0, 0, 0];
      return { x: left, y: top, top, left, right: left + width, bottom: top + height, width, height, toJSON: () => ({}) } as DOMRect;
    });
    const scrollHeightSpy = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
      return this.classList.contains("fs-select-menu") ? 120 : 0;
    });

    try {
      render(<><SelectHarness /><button type="button">Elsewhere</button></>);
      await user.click(screen.getByRole("combobox", { name: "Source" }));
      const listbox = await screen.findByRole("listbox", { name: "Source options" });
      await waitFor(() => {
        expect(listbox).toHaveStyle({ top: "138px", bottom: "auto", left: "40px", width: "240px" });
      });
      await user.click(screen.getByRole("button", { name: "Elsewhere" }));
      expect(screen.queryByRole("listbox", { name: "Source options" })).not.toBeInTheDocument();
    } finally {
      rectSpy.mockRestore();
      scrollHeightSpy.mockRestore();
    }
  });

  it("filters and selects a single Combobox option with the keyboard", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<SingleComboboxHarness />);
    const input = screen.getByRole("combobox", { name: "Timezone" });
    await user.click(input);
    await user.clear(input);
    await user.type(input, "hub");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(input).toHaveValue("HubSpot");
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(input).toHaveFocus();
  });

  it("closes a dialog with Escape and returns focus to its trigger", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<DialogHarness />);
    const trigger = screen.getByRole("button", { name: "Open dialog" });
    await user.click(trigger);
    expect(await screen.findByRole("dialog", { name: "Edit settings" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Close edit settings dialog" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Edit settings" })).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  /* eslint-disable testing-library/no-node-access -- This regression must inspect focus inside a real shadow root. */
  it("returns dialog focus to its trigger inside a shadow root", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<ShadowDialogHarness />);
    const host = screen.getByTestId("shadow-dialog-host");
    const trigger = host.shadowRoot?.querySelector<HTMLButtonElement>("button");
    expect(trigger).toBeTruthy();

    trigger?.focus();
    await user.click(trigger as HTMLButtonElement);
    await screen.findByRole("dialog", { name: "Shadow settings" });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Close Shadow settings" })).toHaveFocus(),
    );
    await user.keyboard("{Escape}");

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Shadow settings" })).not.toBeInTheDocument(),
    );
    await waitFor(() => expect(host.shadowRoot?.activeElement).toBe(trigger));
  });
  /* eslint-enable testing-library/no-node-access */

  it("keeps focus while controlled dialog fields rerender", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<StatefulDialogHarness />);
    await user.click(screen.getByRole("button", { name: "Create token" }));
    const input = await screen.findByRole("textbox", { name: "Label" });
    await user.click(input);
    await user.type(input, "Local CLI");
    expect(input).toHaveValue("Local CLI");
    expect(input).toHaveFocus();
  });

  it("renders a semantic table and reports sort state through aria-sort", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onSortChange = vi.fn();
    const rows = [
      { id: "a", name: "Flow Steward App", jobs: 4 },
      { id: "b", name: "Test", jobs: 0 },
    ];

    render(
      <DsTable
        ariaLabel="Projects"
        columns={[
          { key: "name", header: "Name", sortable: true, cell: (row) => row.name },
          { key: "jobs", header: "Jobs", sortable: true, align: "right", cell: (row) => row.jobs },
          { key: "actions", header: "", ariaLabel: "Actions", cell: () => null },
        ]}
        onSortChange={onSortChange}
        rowKey={(row) => row.id}
        rows={rows}
        sort={{ key: "name", direction: "asc" } satisfies DsTableSort}
      />,
    );

    // A real table, not a div grid: the row action column is reachable by name.
    const table = screen.getByRole("table", { name: "Projects" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    // No row handler here, so no row may claim to be an action: the pointer
    // cursor hangs off this attribute.
    expect(within(table).getAllByRole("row")[1]).not.toHaveAttribute("data-clickable");
    expect(screen.getByRole("columnheader", { name: "Name" })).toHaveAttribute("aria-sort", "ascending");
    expect(screen.getByRole("columnheader", { name: "Jobs" })).toHaveAttribute("aria-sort", "none");
    expect(screen.getByRole("columnheader", { name: "Actions" })).not.toHaveAttribute("aria-sort");

    await user.click(screen.getByRole("button", { name: "Name" }));
    expect(onSortChange).toHaveBeenCalledWith({ key: "name", direction: "desc" });
  });

  it("truncates a cell while keeping the full value reachable, and paints badge tones", () => {
    const rows = [{ id: "a", task: "A very long task summary that will not fit", status: "awaiting approval" }];

    render(
      <DsTable
        ariaLabel="Processes"
        columns={[
          {
            key: "task",
            header: "Task",
            truncate: "20rem",
            tooltip: (row) => row.task,
            cell: (row) => row.task,
          },
          { key: "status", header: "Status", cell: (row) => <DsBadge tone="danger">{row.status}</DsBadge> },
        ]}
        rowKey={(row) => row.id}
        rows={rows}
      />,
    );

    // Truncation is visual only: the whole string stays in the DOM and in the
    // cell title, which is what the bridge's tooltip_key did.
    const cell = screen.getByTitle("A very long task summary that will not fit");
    expect(within(cell).getByText("A very long task summary that will not fit")).toBeInTheDocument();
    expect(within(cell).getByText(/A very long task/)).toHaveClass("fs-table-truncate");
    expect(screen.getByText("awaiting approval")).toHaveClass("fs-badge", "danger");
  });

  it("selects rows and drives the bulk action bar", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onAction = vi.fn();
    const rows = [{ id: "a" }, { id: "b" }, { id: "c" }];

    function SelectionHarness() {
      const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
      return (
        <>
          <DsTable
            ariaLabel="Processes"
            columns={[{ key: "id", header: "ID", cell: (row: { id: string }) => row.id }]}
            onSelectionChange={setSelected}
            rowKey={(row) => row.id}
            rows={rows}
            selectable
            selectedKeys={selected}
          />
          <BulkActionBar
            actions={[{ id: "kill", label: "Kill Selected", variant: "danger" }]}
            count={selected.size}
            onAction={onAction}
            onClear={() => setSelected(new Set())}
          />
        </>
      );
    }

    render(<SelectionHarness />);

    // No selection, no bar. It stays mounted so the exit motion has something to
    // run on, and `aria-hidden` is what keeps it out of the accessibility tree.
    expect(screen.queryByRole("region", { name: "Bulk actions" })).not.toBeInTheDocument();
    // Queried without a name filter: an aria-hidden element computes an empty
    // accessible name, which is the whole point of the attribute here.
    const [dormant] = screen.getAllByRole("region", { hidden: true });
    expect(dormant).toHaveAttribute("aria-label", "Bulk actions");
    expect(dormant).toHaveAttribute("data-state", "hidden");

    await user.click(screen.getByRole("checkbox", { name: "Select b" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    // Partial selection reads as indeterminate, not as "all".
    const selectAll = screen.getByRole("checkbox", { name: "Select visible rows" }) as HTMLInputElement;
    expect(selectAll).not.toBeChecked();
    expect(selectAll.indeterminate).toBe(true);

    await user.click(selectAll);
    expect(screen.getByText("3 selected")).toBeInTheDocument();
    expect(selectAll.indeterminate).toBe(false);

    await user.click(screen.getByRole("button", { name: "Kill Selected" }));
    expect(onAction).toHaveBeenCalledWith("kill");
    // The published contract keeps the selection until it is cleared explicitly.
    expect(screen.getByText("3 selected")).toBeInTheDocument();

    // The bar owns no action paint: a destructive batch action takes the quiet
    // Ghost danger lane, the neutral ones are Secondary.
    expect(screen.getByRole("button", { name: "Kill Selected" })).toHaveClass("fs-button", "ghost-danger");

    // The dismiss is a Ghost IconButton whose glyph carries no text, so the
    // accessible name states the act. It replaced the "Clear" text label.
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByRole("region", { name: "Bulk actions" })).not.toBeInTheDocument();
    expect(document.body.dataset.bulkActionBar).toBeUndefined();
  });

  it("stacks a second live selection in the shared dock instead of on top of the first", () => {
    render(
      <>
        <BulkActionBar actions={[{ id: "a", label: "Pause" }]} count={2} onAction={() => undefined} onClear={() => undefined} />
        <BulkActionBar actions={[{ id: "b", label: "Restart" }]} count={5} onAction={() => undefined} onClear={() => undefined} />
      </>,
    );

    const bars = screen.getAllByRole("region", { name: "Bulk actions" });
    expect(bars).toHaveLength(2);
    // The dock is a placement device with no accessible role of its own, so
    // there is no Testing Library query for it: both bars sharing one parent is
    // what "one dock" means.
    // eslint-disable-next-line testing-library/no-node-access
    expect(bars[0].parentElement).toBe(bars[1].parentElement);
    // eslint-disable-next-line testing-library/no-node-access
    expect(bars[0].parentElement).toHaveClass("fs-bulk-action-bar-dock");
    // The page owes the dock room for both, not for one.
    expect(document.body.dataset.bulkActionBar).toBe("2");
  });

  it("clears the selection on Escape and reserves room for the floating bar", async () => {
    const user = userEvent.setup();

    function EscapeHarness() {
      const [selected, setSelected] = useState(new Set<string>(["a", "b"]));
      return (
        <BulkActionBar
          actions={[{ id: "purge", label: "Purge", variant: "danger" }]}
          count={selected.size}
          onAction={() => undefined}
          onClear={() => setSelected(new Set())}
        />
      );
    }

    render(<EscapeHarness />);
    // While a selection lives, the page reserves the bar's strip so the floating
    // surface never covers the last row of the collection.
    expect(document.body.dataset.bulkActionBar).toBe("1");

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("region", { name: "Bulk actions" })).not.toBeInTheDocument();
    expect(document.body.dataset.bulkActionBar).toBeUndefined();
  });

  it("closes the frozen column zone with a divider that carries a shadow only while columns pass under it", async () => {
    const user = userEvent.setup();
    const rows = [{ id: "a", name: "Alpha" }, { id: "b", name: "Beta" }];

    render(
      <DsTable
        ariaLabel="Wide"
        columns={[{ key: "name", header: "Name", cell: (row) => row.name }]}
        rowKey={(row) => row.id}
        rows={rows}
      />,
    );

    // The flag is evaluated at startup, not only when a scroll event arrives -
    // without that the state rule is dead and the shadow never appears.
    // eslint-disable-next-line testing-library/no-node-access
    const scroller = screen.getByRole("table", { name: "Wide" }).parentElement as HTMLElement;
    expect(scroller).toHaveAttribute("data-scrolled", "false");

    scroller.scrollLeft = 40;
    await user.pointer({ target: scroller });
    scroller.dispatchEvent(new Event("scroll"));
    expect(scroller).toHaveAttribute("data-scrolled", "true");

    scroller.scrollLeft = 0;
    scroller.dispatchEvent(new Event("scroll"));
    expect(scroller).toHaveAttribute("data-scrolled", "false");
  });

  it("draws the frozen boundary as a pseudo-element, because a collapsed cell never paints a shadow", () => {
    // `.fs-table` sets border-collapse: collapse, under which a table cell
    // reports a box-shadow in its computed style and paints nothing. The
    // divider must therefore live on ::after, and the shadow with it.
    expect(settingsStyles).toMatch(
      /\.fs-table:not\(\.embedded\) td:first-child::after\s*\{[^}]*background:\s*var\(--fs-table-sticky-divider\);[^}]*box-shadow:\s*var\(--fs-table-sticky-shadow-rest\);/s,
    );
    expect(settingsStyles).toMatch(
      /\.fs-table-scroll\[data-scrolled="true"\][^{]*::after\s*\{\s*box-shadow:\s*var\(--fs-table-sticky-shadow\);/s,
    );
  });

  it("sizes every tab row to its labels, with no opt-in left to forget", () => {
    const items = [
      { key: "overview", label: "Overview" },
      { key: "policy", label: "Policy" },
    ];

    render(
      <DsTabs
        active="overview"
        ariaLabel="Sections"
        idPrefix="cfg"
        items={items}
        onSelect={() => undefined}
      />,
    );

    // Retired with the canon change of 2026-09-10: the `fit` opt-in and its
    // paired classes are gone, because content width is what the component does
    // now. Filling the surface was the behaviour the published rule removed.
    const tablist = screen.getByRole("tablist", { name: "Sections" });
    expect(tablist).toHaveClass("fs-tabs-list");
    expect(tablist).not.toHaveClass("fit");
  });

  it("sizes a tab from the published floor and padding rather than from its siblings", () => {
    // Count-independent by contract: the same tab renders identically with two
    // siblings or eight, so the rule may not reach for flex growth or a share of
    // the row. `width: max-content` with `min-width: 100%` is what leaves the
    // empty space to the right of a short row, which the contract calls correct.
    expect(settingsStyles).toMatch(
      /\.fs-tab\s*\{[^}]*flex:\s*none;[^}]*min-width:\s*var\(--fs-tabs-tab-min-width\);/s,
    );
    expect(settingsStyles).toMatch(
      /\.fs-tab\s*\{[^}]*padding:\s*0 var\(--fs-tabs-tab-padding-x\);/s,
    );
    expect(settingsStyles).toMatch(
      /\.fs-tabs-list\s*\{[^}]*width:\s*max-content;[^}]*min-width:\s*100%;/s,
    );
    // The escape hatch this replaces must not survive anywhere.
    expect(settingsStyles).not.toMatch(/\.fs-tabs-(list|scroll)\.fit/);
  });

  it("exposes in-page tabs as a real tablist with arrow-key movement", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });

    function TabsHarness() {
      const [active, setActive] = useState("overview");
      return (
        <DsTabs
          active={active}
          ariaLabel="Sections"
          idPrefix="cfg"
          items={[
            { key: "overview", label: "Overview" },
            { key: "policy", label: "Policy" },
            { key: "advanced", label: "Advanced" },
          ]}
          onSelect={setActive}
        />
      );
    }

    render(<TabsHarness />);

    const tablist = screen.getByRole("tablist", { name: "Sections" });
    expect(within(tablist).getAllByRole("tab")).toHaveLength(3);

    const overview = screen.getByRole("tab", { name: "Overview" });
    expect(overview).toHaveAttribute("aria-selected", "true");
    // Roving tabindex: only the selected tab is in the tab order.
    expect(overview).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: "Policy" })).toHaveAttribute("tabindex", "-1");
    expect(overview).toHaveAttribute("aria-controls", "cfg-panel-overview");

    overview.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Policy" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Policy" })).toHaveFocus();

    // Wraps from the first tab backwards to the last.
    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Advanced" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Advanced" })).toHaveFocus();
  });

  it("reveals an opted-in keyboard tab within its strip without moving outer or body scroll", async () => {
    const user = userEvent.setup();
    render(<OverflowTabsHarness revealActiveTab />);
    const strip = installTabStripLayout();
    const outer = screen.getByTestId("tabs-outer");
    const body = screen.getByTestId("tabs-body");
    outer.scrollTop = 73;
    outer.scrollLeft = 7;
    body.scrollTop = 91;
    screen.getByRole("tab", { name: "Smart setup" }).focus();
    await user.keyboard("{ArrowRight}{ArrowRight}{ArrowRight}");
    const advanced = screen.getByRole("tab", { name: "Advanced" });
    expect(advanced).toHaveFocus();
    expect(advanced).toHaveAttribute("aria-selected", "true");
    expect(strip.scrollLeft).toBe(119);
    expect(advanced.getBoundingClientRect().right).toBeLessThanOrEqual(strip.getBoundingClientRect().right);
    expect(outer.scrollTop).toBe(73);
    expect(outer.scrollLeft).toBe(7);
    expect(body.scrollTop).toBe(91);
    await user.keyboard("{ArrowLeft}{ArrowLeft}{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "Smart setup" })).toHaveFocus();
    expect(strip.scrollLeft).toBe(0);
  });

  it("preserves the default tab strip behavior when reveal is not opted in", async () => {
    const user = userEvent.setup();
    render(<OverflowTabsHarness />);
    const strip = installTabStripLayout();
    screen.getByRole("tab", { name: "Smart setup" }).focus();
    await user.keyboard("{ArrowRight}{ArrowRight}{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Advanced" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Advanced" })).toHaveAttribute("aria-selected", "true");
    expect(strip.scrollLeft).toBe(0);
  });

  it("leaves an opted-in strip still when its keyboard tab already fits", async () => {
    const user = userEvent.setup();
    render(<OverflowTabsHarness revealActiveTab />);
    const strip = installTabStripLayout(700);
    screen.getByRole("tab", { name: "Smart setup" }).focus();
    await user.keyboard("{ArrowRight}{ArrowRight}{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Advanced" })).toHaveFocus();
    expect(strip.scrollLeft).toBe(0);
  });

  it("shows the state block instead of inventing empty rows", () => {
    render(
      <DsTable
        ariaLabel="Projects"
        columns={[{ key: "name", header: "Name", cell: (row: { id: string }) => row.id }]}
        rowKey={(row) => row.id}
        rows={[]}
        state={<div className="fs-table-state"><strong>No projects yet</strong></div>}
      />,
    );

    expect(within(screen.getByRole("table")).queryAllByRole("row")).toHaveLength(1);
    expect(screen.getByText("No projects yet")).toBeInTheDocument();
  });

  it("lets a row whose cell holds its link be clicked without a second tab stop", async () => {
    const onRowClick = vi.fn();
    render(
      <DsTable
        ariaLabel="Entries"
        columns={[
          { key: "name", header: "Name", cell: (row: { id: string }) => <a href={`#${row.id}`}>{row.id}</a> },
          { key: "kind", header: "Kind", cell: () => "Plain" },
        ]}
        onRowClick={onRowClick}
        rowClickFocusable={false}
        rowKey={(row) => row.id}
        rows={[{ id: "alpha" }]}
      />,
    );
    const row = within(screen.getByRole("table")).getAllByRole("row")[1];
    expect(row).not.toHaveAttribute("tabindex");

    await userEvent.click(within(row).getByText("Plain"));
    expect(onRowClick).toHaveBeenCalledTimes(1);
    // The link's click is the link's own.
    await userEvent.click(within(row).getByRole("link", { name: "alpha" }));
    expect(onRowClick).toHaveBeenCalledTimes(1);
  });

  it("centres the bulk action dock on the content area, not the viewport", () => {
    expect(settingsStyles).toMatch(
      /\.fs-bulk-action-bar-dock\s*\{[^}]*left:\s*calc\(50% \+ \(var\(--fs-shell-sidebar-width\) \+ 1px\) \/ 2\)/,
    );
  });

  it("resolves the rail width at root, because the dock lives outside the shell", () => {
    // bulkActionBarDock() appends to document.body, so a variable scoped to
    // .fs-shell-layout would be undefined there and the calc() would be dropped.
    expect(shellStyles).toMatch(/:root\s*\{[^}]*--fs-shell-sidebar-width:\s*var\(--fs-sidebar-width-expanded\)/);
    expect(shellStyles).toMatch(
      /:root:has\(\.fs-shell-layout\[data-rail-state="collapsed"\]\)\s*\{\s*--fs-shell-sidebar-width:\s*var\(--fs-sidebar-width-collapsed\)/,
    );
    const declarations = [...shellStyles.matchAll(/--fs-shell-sidebar-width:/g)];
    expect(declarations, "declared once per state, never duplicated").toHaveLength(2);
  });

  it("keeps the bar's bottom reservation on the shell's main element", () => {
    // The reservation targets .fs-shell-main deliberately: the page container
    // is a child, and moving the padding onto it would let the bar cover the
    // collection's last row.
    expect(settingsStyles).toMatch(/body\[data-bulk-action-bar\] \.fs-shell-main\s*\{\s*padding-bottom:/);
    expect(settingsStyles).not.toMatch(/body\[data-bulk-action-bar\] \.fs-page\s*\{/);
  });
});


describe("native feedback and overlay semantics", () => {
  it("exposes rich status details without nesting block content in output", () => {
    render(<DsAlert title="Saved" tone="success"><p>Changes are available.</p><p>Continue editing.</p></DsAlert>);
    const status = screen.getByRole("status");
    expect(status.tagName).toBe("OUTPUT");
    expect(status).toHaveTextContent("Saved");
    expect(status).toHaveAccessibleDescription("Changes are available. Continue editing.");
    expect(within(status).queryByText("Changes are available.")).not.toBeInTheDocument();
  });

  it("keeps drawers non-modal using an open native dialog", () => {
    render(<DsDrawer open title="Details" onClose={vi.fn()}>Panel content</DsDrawer>);
    const dialog = screen.getByRole("dialog", { name: "Details" });
    expect(dialog.tagName).toBe("DIALOG");
    expect(dialog).toHaveAttribute("open");
    expect(dialog).not.toHaveAttribute("aria-modal");
  });

  it("keeps contextual drawer layout hooks opt-in and restores default geometry", () => {
    const close = vi.fn();
    const { rerender } = render(<DsDrawer open title="Inspector" onClose={close} className="fixture-inspector" style={{ width: "480px", top: "104px" }} titleId="fixture-inspector-title" dataAttributes={{ "data-width": 480 }}>Detail</DsDrawer>);
    const panel = screen.getByRole("dialog", { name: "Inspector" });
    expect(panel).toHaveClass("fs-drawer", "fixture-inspector");
    expect(panel).toHaveStyle({ width: "480px", top: "104px", height: "auto" });
    expect(panel).toHaveAttribute("aria-labelledby", "fixture-inspector-title");
    expect(panel).toHaveAttribute("data-width", "480");
    rerender(<DsDrawer open title="Inspector" onClose={close}>Detail</DsDrawer>);
    expect(panel).toHaveClass("fs-drawer", { exact: true });
    expect(panel).toHaveStyle({ height: "auto" });
    expect(panel).not.toHaveStyle({ width: "480px" });
    expect(panel).not.toHaveAttribute("data-width");
    expect(panel).not.toHaveAttribute("aria-modal");
  });

  it("preserves backdrop dismissal and prevents it while busy", async () => {
    const user = userEvent.setup();
    const close = vi.fn();
    const { rerender } = render(<DsDialog open title="Edit" description="Details" eyebrow="Settings" onClose={close}>Content</DsDialog>);
    expect(screen.getByRole("dialog", { name: "Edit" }).tagName).toBe("DIALOG");
    await user.click(screen.getByRole("button", { name: "Dismiss Edit" }));
    expect(close).toHaveBeenCalledTimes(1);
    rerender(<DsDialog busy open title="Edit" description="Details" eyebrow="Settings" onClose={close}>Content</DsDialog>);
    await user.click(screen.getByRole("button", { name: "Dismiss Edit" }));
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("uses native determinate and indeterminate progress", () => {
    const { rerender } = render(<DsProgress label="Import" value={25} max={50} valueText="25 of 50" />);
    const progress = screen.getByRole("progressbar", { name: "Import" });
    expect(progress.tagName).toBe("PROGRESS");
    expect(progress).toHaveValue(25);
    expect(progress).toHaveAttribute("max", "50");
    expect(progress).toHaveAttribute("aria-valuetext", "25 of 50");
    expect(progress).toHaveAccessibleDescription("25 of 50");
    rerender(<DsProgress label="Import" />);
    expect(screen.getByRole("progressbar", { name: "Import" })).not.toHaveAttribute("value");
  });
});


describe("IMPL-01 foundation resource contracts", () => {
  it("preserves local font delivery while routing the UI and code families through semantic roles", async () => {
    const { default: fonts } = await import("@/design-system/01_tokens/fonts.css?raw");
    const { default: base } = await import("@/design-system/01_tokens/base.css?raw");
    const { default: styles } = await import("@/styles/index.css?raw");
    const urls = [...fonts.matchAll(/src: url\("([^"\n]+)"\)/g)].map((match) => match[1]);
    expect(urls).toHaveLength(8);
    for (const url of urls) expect(existsSync(resolve("src/design-system/01_tokens", url)), url).toBe(true);
    expect(fonts).not.toMatch(/https?:|@import/);
    expect(base).toMatch(/--ff-body:\s*'Google Sans Flex'/);
    expect(base).toMatch(/--ff-data:\s*'DM Sans'/);
    expect(base).toMatch(/--ff-code:\s*'Google Sans Code'/);
    expect(base).toMatch(/--ff-logo:\s*'Alan Sans'/);
    expect(base).toMatch(/--fs-type-weight-regular:\s*400/);
    expect(base).toMatch(/--fs-type-weight-medium:\s*500/);
    expect(base).toMatch(/font-variation-settings:\s*'GRAD' -50/);
    expect(base).toContain("font-variant-numeric: tabular-nums");
    expect(styles).toMatch(/body\s*\{[^}]*font-family:\s*var\(--ff-body\)/);
    expect(styles).toMatch(/code\s*\{[^}]*font-family:\s*var\(--ff-code\)/);
  });

  it("provides still assets, canonical dimensions and independent coarse-pointer hit targets", async () => {
    const { default: themes } = await import("@/design-system/01_tokens/themes.css?raw");
    const { default: base } = await import("@/design-system/01_tokens/base.css?raw");
    expect(themes).toMatch(/--fs-loader-height-lg:\s*40px/);
    expect(themes).toMatch(/--fs-loader-height-md:\s*24px/);
    expect(themes).toMatch(/--fs-loader-orbit-size-sm:\s*16px/);
    expect(themes).toMatch(/--fs-loader-orbit-size-md:\s*24px/);
    expect(themes).toContain("aspect-ratio: 578 / 199");
    expect(themes).toContain('flow-steward-loader-still.svg');
    expect(themes).toContain('flow-steward-loader-orbit-still.svg');
    expect(themes).toContain('flow-steward-loader-orbit-mono-still.svg');
    expect(themes).toContain("@media (pointer: coarse)");
    expect(base).toMatch(/--fs-hit-target-touch:\s*44px/);
    expect(base).toMatch(/--fs-hit-target-touch-gap:\s*8px/);
    expect(settingsStyles).toContain(".fs-button::after");
    expect(settingsStyles).toContain("var(--fs-hit-target)");
  });
});


describe("foundation control feedback", () => {
  it("keeps an external reason available with and without the transient tooltip", async () => {
    render(<><p id="reason">Only project owners can change this.</p><IconButton aria-describedby="reason" icon={Search} label="Search" /></>);
    const control = screen.getByRole("button", { name: "Search" });
    expect(control).toHaveAccessibleDescription("Only project owners can change this.");
    await userEvent.hover(control);
    expect(control).toHaveAccessibleDescription("Only project owners can change this. Search");
    await userEvent.unhover(control);
    expect(control).toHaveAccessibleDescription("Only project owners can change this.");
  });

  it("ignores repeat button presses while busy without fading its content", async () => {
    const action = vi.fn();
    render(<Button aria-busy onClick={action}>Running…</Button>);
    const control = screen.getByRole("button", { name: "Running…" });
    await userEvent.click(control);
    expect(action).not.toHaveBeenCalled();
    expect(control).toHaveAttribute("aria-busy", "true");
    expect(control).toHaveAttribute("aria-disabled", "true");
    expect(control).toBeEnabled();
  });

  it("guards the busy IconButton immediately and preserves its name before the glyph delay", async () => {
    const action = vi.fn();
    render(<IconButton aria-busy icon={Search} label="Run workflow" onClick={action} />);
    const control = screen.getByRole("button", { name: "Run workflow" });
    expect(within(control).queryByTestId("loader-mark")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    await userEvent.click(control);
    expect(action).not.toHaveBeenCalled();
  });
});


function BusyDrawerOpenerHarness({ busy = true, context = "run-a" }: { busy?: boolean; context?: string }) {
  const [open, setOpen] = useState(false);
  const fallbackRef = useRef<HTMLButtonElement>(null);
  return <>
    <button ref={fallbackRef} type="button">Drawer fallback</button>
    <button disabled={open && busy} onClick={() => setOpen(true)} type="button">Open results</button>
    <DsDrawer open={open} title="Captured results" focusContext={context} focusFallback={() => fallbackRef.current} onClose={() => setOpen(false)}>
      <input aria-label="Drawer detail" />
    </DsDrawer>
  </>;
}

it.each([false, true])("retains a temporarily disabled drawer opener only while its context is current (changed=%s)", async (changedContext) => {
  const user = userEvent.setup();
  const view = render(<BusyDrawerOpenerHarness />);
  const opener = screen.getByRole("button", { name: "Open results" });
  await user.click(opener);
  expect(opener).toBeDisabled();
  await user.click(screen.getByRole("textbox", { name: "Drawer detail" }));
  view.rerender(<BusyDrawerOpenerHarness busy={false} context={changedContext ? "run-b" : "run-a"} />);
  expect(opener).toBeEnabled();
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Captured results" })).not.toBeInTheDocument());
  await waitFor(() => expect(changedContext ? screen.getByRole("button", { name: "Drawer fallback" }) : opener).toHaveFocus());
});


function QueuedDrawerReturnHarness({ open, context = "run-a", openerState = "visible", mounted = true }: {
  open: boolean; context?: string; openerState?: "visible" | "disabled" | "hidden" | "removed"; mounted?: boolean;
}) {
  const fallbackRef = useRef<HTMLButtonElement>(null);
  return <>
    <button ref={fallbackRef} type="button">Queued fallback</button>
    <button type="button">External focus owner</button>
    {openerState !== "removed" ? <button disabled={openerState === "disabled"} hidden={openerState === "hidden"} type="button">Queued opener</button> : null}
    {mounted ? <DsDrawer open={open} title="Queued results" focusContext={context} focusFallback={() => fallbackRef.current} onClose={() => {}}><input aria-label="Queued detail" /></DsDrawer> : null}
    <RequestLoadingIndicator />
  </>;
}

it.each(["context", "context-aba", "disabled", "hidden", "removed", "reopen", "unmount", "external"] as const)("rechecks queued drawer return focus safely after %s", async (change) => {
  const user = userEvent.setup();
  const view = render(<QueuedDrawerReturnHarness open={false} />);
  let finishRequest: (() => void) | undefined;
  try {
    const opener = screen.getByRole("button", { name: "Queued opener" });
    await user.click(opener);
    view.rerender(<QueuedDrawerReturnHarness open />);
    const drawer = screen.getByRole("dialog", { name: "Queued results" });
    await waitFor(() => expect(within(drawer).getByRole("button", { name: "Close" })).toHaveFocus());
    act(() => { finishRequest = beginRequest(); });
    const loader = screen.getByRole("dialog", { name: "Loading requests" });
    expect(loader).toHaveFocus();
    view.rerender(<QueuedDrawerReturnHarness open={false} />);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Queued results" })).not.toBeInTheDocument());
    expect(loader).toHaveFocus();

    if (change === "context" || change === "context-aba") {
      view.rerender(<QueuedDrawerReturnHarness open={false} context="run-b" />);
      if (change === "context-aba") view.rerender(<QueuedDrawerReturnHarness open={false} context="run-a" />);
    } else if (change === "disabled" || change === "hidden" || change === "removed") {
      view.rerender(<QueuedDrawerReturnHarness open={false} openerState={change} />);
    } else if (change === "reopen") {
      view.rerender(<QueuedDrawerReturnHarness open />);
    } else if (change === "unmount") {
      view.rerender(<QueuedDrawerReturnHarness open={false} mounted={false} />);
    } else {
      await user.click(screen.getByRole("button", { name: "External focus owner" }));
    }
    act(() => { finishRequest?.(); });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument());
    expect(opener).not.toHaveFocus();
    if (change === "disabled" || change === "hidden" || change === "removed") expect(screen.getByRole("button", { name: "Queued fallback" })).toHaveFocus();
    if (change === "external") expect(screen.getByRole("button", { name: "External focus owner" })).toHaveFocus();
    if (change === "reopen") expect(screen.getByRole("dialog", { name: "Queued results" })).toBeInTheDocument();
  } finally {
    act(() => { finishRequest?.(); });
    view.unmount();
  }
});


it.each([false, true])("retires the original owner when closing and replacing context in the same commit (loading=%s)", async (loading) => {
  const user = userEvent.setup();
  const view = render(<QueuedDrawerReturnHarness open={false} />);
  let finishRequest: (() => void) | undefined;
  try {
    const opener = screen.getByRole("button", { name: "Queued opener" });
    const fallback = screen.getByRole("button", { name: "Queued fallback" });
    await user.click(opener);
    view.rerender(<QueuedDrawerReturnHarness open />);
    const drawer = screen.getByRole("dialog", { name: "Queued results" });
    await waitFor(() => expect(within(drawer).getByRole("button", { name: "Close" })).toHaveFocus());
    if (loading) act(() => { finishRequest = beginRequest(); });
    view.rerender(<QueuedDrawerReturnHarness open={false} context="run-b" />);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Queued results" })).not.toBeInTheDocument());
    act(() => { finishRequest?.(); });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Loading requests" })).not.toBeInTheDocument());
    expect(opener).not.toHaveFocus();
    expect(fallback).not.toHaveFocus();
  } finally {
    act(() => { finishRequest?.(); });
    view.unmount();
  }
});

function NestedConfirmationHarness({ onParentClose }: { onParentClose: () => void }) {
  const [open, setOpen] = useState(true);
  const { confirmDialog, requestConfirmation } = useConfirmDialog();
  return <DsDialog description="Parent content" eyebrow="Settings" onClose={() => { onParentClose(); setOpen(false); }} open={open} title="Parent settings">
    <button onClick={() => { void requestConfirmation({ title: "Clear nested hook?", confirmLabel: "Clear" }); }} type="button">Request clear</button>
    <button type="button">Parent last control</button>
    <ConfirmDialog {...confirmDialog} />
  </DsDialog>;
}

it("gives only the top modal Tab and Escape ownership, then restores the surviving parent", async () => {
  const user = userEvent.setup();
  const onParentClose = vi.fn();
  render(<NestedConfirmationHarness onParentClose={onParentClose} />);
  const trigger = screen.getByRole("button", { name: "Request clear" });
  await user.click(trigger);
  const confirmation = screen.getByRole("dialog", { name: "Clear nested hook?" });
  await waitFor(() => expect(within(confirmation).getByRole("button", { name: "Cancel" })).toHaveFocus());
  // A focus move by another surface must not hand keyboard ownership to the parent.
  screen.getByRole("button", { name: "Parent last control" }).focus();
  await user.tab();
  expect(within(confirmation).getByRole("button", { name: "Close Clear nested hook?" })).toHaveFocus();
  await user.tab({ shift: true });
  expect(within(confirmation).getByRole("button", { name: "Clear" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(onParentClose).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog", { name: "Clear nested hook?" })).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "Parent settings" })).toBeInTheDocument();
  await waitFor(() => expect(trigger).toHaveFocus());
  expect(document.body).toHaveStyle({overflow:"hidden"});
  await user.keyboard("{Escape}");
  expect(onParentClose).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("dialog", { name: "Parent settings" })).not.toBeInTheDocument();
  expect(document.body).not.toHaveStyle({overflow:"hidden"});
});

it.each(["busy", "handled popup"])("keeps a parent modal open when the top modal has %s Escape ownership", async (mode) => {
  const user = userEvent.setup();
  const parentClose = vi.fn();
  const childClose = vi.fn();
  render(<>
    <DsDialog description="Parent" eyebrow="Settings" onClose={parentClose} open title="Parent" />
    <DsDialog busy={mode === "busy"} description="Child" eyebrow="Settings" onClose={childClose} open title="Child" variant="confirm">
      <input aria-label="Popup field" onKeyDown={(event) => { if (mode === "handled popup" && event.key === "Escape") event.preventDefault(); }} />
    </DsDialog>
  </>);
  expect(screen.getByRole("alertdialog", { name: "Child" })).toHaveAttribute("aria-modal", "true");
  await user.click(screen.getByRole("textbox", { name: "Popup field" }));
  await user.keyboard("{Escape}");
  expect(childClose).not.toHaveBeenCalled();
  expect(parentClose).not.toHaveBeenCalled();
});
