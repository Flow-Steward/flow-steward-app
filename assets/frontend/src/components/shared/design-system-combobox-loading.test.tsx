import { act, fireEvent, render, screen, within } from "@testing-library/react";

import { MultiCombobox, SingleCombobox, type DsOption } from "./design-system-selects";
import { LOADER_DELAY_MS, LOADER_MIN_SHOWN_MS } from "@/components/shared/use-loader-delay";

const projects: DsOption[] = [
  { value: "alpha", label: "Alpha" },
  { value: "beta", label: "Beta" },
];

function advance(ms: number) {
  act(() => { vi.advanceTimersByTime(ms); });
}

/** The visible loading row inside the list: not an option, so it is presentational. */
function loadingRow(listbox: HTMLElement) {
  return within(listbox).queryByRole("presentation");
}

/** The field's polite live region announces the loading text exactly once. */
function announced(text: string) {
  return screen.queryAllByText(text).filter((node) => node.matches('[aria-live="polite"]'));
}

function SingleField({ loading, options = [] }: Readonly<{ loading: boolean; options?: DsOption[] }>) {
  return <SingleCombobox ariaLabel="Project" loading={loading} loadingLabel="Loading projects…" onChange={() => undefined} options={options} placeholder="Search projects" value="" />;
}

function MultiField({ loading }: Readonly<{ loading: boolean }>) {
  return <MultiCombobox ariaLabel="Project restrictions" loading={loading} loadingLabel="Loading projects…" onChange={() => undefined} options={projects} placeholder="Search projects" selected={["alpha"]} />;
}

describe("Combobox loading row (designer rule 3.2)", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("keeps the single field usable and leads the open list with a named, non-selectable row after 400 ms", () => {
    const { rerender } = render(<SingleField loading />);
    const input = screen.getByRole("combobox", { name: "Project" });
    expect(input).toBeEnabled();
    expect(input).toHaveAttribute("placeholder", "Search projects");
    act(() => { input.focus(); });

    // Nothing paints during the delay: no loader and no false "No results".
    advance(LOADER_DELAY_MS - 1);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.queryByText("No results")).not.toBeInTheDocument();
    expect(screen.queryByText("Loading projects…")).not.toBeInTheDocument();

    advance(1);
    const listbox = screen.getByRole("listbox", { name: "Project options" });
    expect(input).toHaveAttribute("aria-expanded", "true");
    expect(listbox).toHaveAttribute("aria-busy", "true");
    const row = loadingRow(listbox);
    expect(row).toHaveClass("fs-combobox-loading");
    expect(row).toHaveTextContent("Loading projects…");
    if (!row) throw new Error("no loading row");
    expect(within(row).getByTestId("loader-mark")).toHaveClass("fs-loader-orbit");
    expect(within(listbox).queryByRole("option")).not.toBeInTheDocument();
    expect(screen.queryByText("No results")).not.toBeInTheDocument();
    // Announced once, through the field's live region, not a nested status.
    expect(announced("Loading projects…")).toHaveLength(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    // The options arrive under the row, which holds for its minimum time.
    rerender(<SingleField loading={false} options={projects} />);
    advance(LOADER_MIN_SHOWN_MS - 1);
    expect(loadingRow(listbox)).toBeInTheDocument();
    const [firstOption] = within(listbox).getAllByRole("option");
    expect(within(listbox).getAllByRole("option")).toHaveLength(2);
    // The row leads the list: the options follow it.
    expect(row.compareDocumentPosition(firstOption) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    advance(1);
    expect(loadingRow(listbox)).not.toBeInTheDocument();
    expect(listbox).not.toHaveAttribute("aria-busy");
    expect(screen.queryByText("Loading projects…")).not.toBeInTheDocument();
  });

  it("never paints the row for a load shorter than the delay", () => {
    const { rerender } = render(<SingleField loading />);
    act(() => { screen.getByRole("combobox", { name: "Project" }).focus(); });
    advance(LOADER_DELAY_MS / 2);
    rerender(<SingleField loading={false} options={projects} />);
    advance(LOADER_DELAY_MS + LOADER_MIN_SHOWN_MS);
    const listbox = screen.getByRole("listbox", { name: "Project options" });
    expect(loadingRow(listbox)).not.toBeInTheDocument();
    expect(listbox).not.toHaveAttribute("aria-busy");
    expect(within(listbox).getAllByRole("option")).toHaveLength(2);
  });

  it("skips the loading row in keyboard navigation", () => {
    render(<SingleField loading options={projects} />);
    const input = screen.getByRole("combobox", { name: "Project" });
    act(() => { input.focus(); });
    advance(LOADER_DELAY_MS);
    const listbox = screen.getByRole("listbox", { name: "Project options" });
    expect(loadingRow(listbox)).toHaveClass("fs-combobox-loading");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    const alpha = within(listbox).getByRole("option", { name: "Alpha" });
    expect(input).toHaveAttribute("aria-activedescendant", alpha.id);
  });

  it("keeps chosen values visible in the multiple field and takes Clear away while the row shows", () => {
    const { rerender } = render(<MultiField loading />);
    const input = screen.getByRole("combobox", { name: "Project restrictions" });
    expect(input).toBeEnabled();
    expect(screen.getByRole("button", { name: "Remove Alpha" })).toBeVisible();
    act(() => { input.focus(); });
    expect(announced("1 selected")).toHaveLength(1);

    advance(LOADER_DELAY_MS);
    const listbox = screen.getByRole("listbox", { name: "Project restrictions options" });
    expect(listbox).toHaveAttribute("aria-busy", "true");
    expect(loadingRow(listbox)).toHaveClass("fs-combobox-loading");
    // The row is not an option: only the two projects are.
    expect(within(listbox).getAllByRole("option")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Remove Alpha" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Clear project restrictions" })).not.toBeInTheDocument();
    expect(announced("Loading projects…")).toHaveLength(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    rerender(<MultiField loading={false} />);
    advance(LOADER_MIN_SHOWN_MS);
    expect(loadingRow(listbox)).not.toBeInTheDocument();
    expect(listbox).not.toHaveAttribute("aria-busy");
    expect(screen.getByRole("button", { name: "Clear project restrictions" })).toBeInTheDocument();
    expect(announced("1 selected")).toHaveLength(1);
  });
});
