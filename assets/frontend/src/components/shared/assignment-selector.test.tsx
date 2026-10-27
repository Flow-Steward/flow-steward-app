import { useMemo, useState } from "react";

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AssignmentSelector } from "@/components/shared/assignment-selector";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";

function AssignmentSelectorHarness({
  activeFilter = "all",
  initialSelected,
  saveDisabled = false,
}: Readonly<{
  activeFilter?: string;
  initialSelected: string[];
  saveDisabled?: boolean;
}>) {
  const [selectedIds, setSelectedIds] = useState<string[]>(initialSelected);
  const [searchValue, setSearchValue] = useState("");
  const selectedIdSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  const rows = [
    {
      id: "row-1",
      title: "Alpha",
      selected: selectedIdSet.has("row-1"),
      filterKeys: ["all", selectedIdSet.has("row-1") ? "linked" : "available"],
      searchText: "alpha",
    },
    {
      id: "row-2",
      title: "Beta",
      selected: selectedIdSet.has("row-2"),
      filterKeys: ["all", selectedIdSet.has("row-2") ? "linked" : "available"],
      searchText: "beta",
    },
    {
      id: "row-3",
      title: "Gamma",
      selected: selectedIdSet.has("row-3"),
      disabled: true,
      filterKeys: ["all", "disabled"],
      searchText: "gamma",
    },
  ];

  return (
    <div>
      <AssignmentSelector
        activeFilter={activeFilter}
        emptyState="No rows"
        filters={[
          { key: "all", label: "All", count: 3 },
          { key: "disabled", label: "Disabled", count: 1 },
        ]}
        onFilterChange={() => {}}
        onSave={() => {}}
        onSearchChange={setSearchValue}
        onToggleRow={(rowId) =>
          setSelectedIds((current) =>
            current.includes(rowId)
              ? current.filter((item) => item !== rowId)
              : [...current, rowId].sort(),
          )
        }
        rows={rows}
        saveDisabled={saveDisabled}
        saveLabel="Save"
        searchPlaceholder="Search rows"
        searchValue={searchValue}
        selectedCount={selectedIds.length}
        totalCount={3}
      />
      <div data-testid="selected-ids">{selectedIds.sort().join(",")}</div>
    </div>
  );
}

describe("AssignmentSelector", () => {
  it.each(["lt", "en"])("localizes the selection label in %s while retaining the row title and ID", async (locale) => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<I18nContext.Provider value={{ ...fallbackContext, locale, t: (key, text, params) => fallbackContext.t(key, locale === "lt" && key === "react.shared.toggle_row" ? "Perjungti {title}" : text, params) }}><AssignmentSelectorHarness initialSelected={[]} /></I18nContext.Provider>);
    const toggle = screen.getByRole("checkbox", { name: locale === "lt" ? "Perjungti Alpha" : "Toggle Alpha" });
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    await user.click(toggle);
    expect(screen.getByTestId("selected-ids")).toHaveTextContent("row-1");
    expect(toggle).toBeChecked();
  });

  it("renders whole Lithuanian result counts while retaining the actual filtered rows", async () => {
    const user = userEvent.setup();
    const messages: Record<string, string> = {
      "react.shared.assignment_rows_shown": "Rodoma: {count}",
      "react.shared.assignment_rows_shown_filtered": "Rodoma: {count} iš {total}",
    };
    render(
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, fallback = "", params) => interpolate(messages[key] || fallback || key, params) }}>
        <AssignmentSelectorHarness initialSelected={[]} />
      </I18nContext.Provider>,
    );
    expect(screen.getByText("Rodoma: 3")).toBeInTheDocument();
    await user.type(screen.getByRole("searchbox"), "alpha");
    expect(screen.getByText("Rodoma: 1 iš 3")).toBeInTheDocument();
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.queryByText("Beta")).not.toBeInTheDocument();
  });

  it("selects only currently shown rows and skips disabled rows", async () => {
    // The checkbox contract hides the native input behind its label.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<AssignmentSelectorHarness initialSelected={["row-2"]} />);

    await user.type(screen.getByRole("searchbox"), "alpha");
    await user.click(screen.getByRole("checkbox", { name: "Select visible rows" }));

    expect(screen.getByTestId("selected-ids")).toHaveTextContent("row-1,row-2");
  });

  it("unselects only currently shown rows and preserves hidden selection", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<AssignmentSelectorHarness initialSelected={["row-1", "row-2"]} />);

    await user.type(screen.getByRole("searchbox"), "alpha");
    // Unchecking the table's select-all clears exactly the shown rows.
    await user.click(screen.getByRole("checkbox", { name: "Select visible rows" }));

    expect(screen.getByTestId("selected-ids")).toHaveTextContent("row-2");
  });

  it("explains when the current filter shows only disabled rows", () => {
    render(<AssignmentSelectorHarness activeFilter="disabled" initialSelected={[]} />);

    expect(screen.getByRole("checkbox", { name: "Select visible rows" })).toBeDisabled();
    // A blocked selection is an alert, so the title carries the message.
    expect(screen.getByRole("status")).toHaveTextContent("Nothing here can be selected");
    expect(screen.getByText("The rows shown by the current search or filter are disabled.")).toBeInTheDocument();
  });

  it("disables save when the parent marks the selection invalid", () => {
    render(<AssignmentSelectorHarness initialSelected={[]} saveDisabled />);

    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });
});
