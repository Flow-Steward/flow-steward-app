import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ResourceSelectionList } from "./resource-selection-list";
import { resourceScopeFromSelection } from "./dataset-page-helpers";
import { useResourceSelection } from "./use-resource-selection";

const snapshot = {
  resources: [
    { resource_id: "orders", resource_type: "table", columns: [{ name: "id" }, { name: "amount" }] },
    { resource_id: "customers", resource_type: "table", columns: [{ name: "name" }] },
  ],
};

function ResourceEditor() {
  const selection = useResourceSelection(snapshot);
  return (
    <>
      <input aria-label="Search resources" onChange={(event) => selection.setResourceSearch(event.target.value)} value={selection.resourceSearch} />
      <ResourceSelectionList
        onSetAllColumns={selection.setAllColumns}
        onToggleColumn={selection.toggleColumn}
        onToggleResource={selection.toggleResource}
        resources={selection.filteredResources}
        selectedResources={selection.selectedResources}
      />
      <output aria-label="Resource scope">{JSON.stringify(resourceScopeFromSelection(snapshot, selection.selectedResources))}</output>
    </>
  );
}

describe("dataset resource selection", () => {
  it("keeps explicit column choices distinct from all columns and removes deselected resources", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<ResourceEditor />);

    await user.click(screen.getByText("orders"));
    await user.click(screen.getByText(/table · 2 columns/));
    expect(screen.getByRole("checkbox", { name: "All columns" })).toBeChecked();
    await user.click(screen.getByText("All columns"));
    await user.click(screen.getByText("amount"));
    expect(screen.getByLabelText("Resource scope")).toHaveTextContent('{"version":1,"resources":[{"resource_id":"orders","columns":["id"]}]}');
    await user.click(screen.getByText("amount"));
    expect(screen.getByLabelText("Resource scope")).toHaveTextContent('{"version":1,"resources":[{"resource_id":"orders","columns":["id","amount"]}]}');
    await user.click(screen.getByText("All columns"));
    expect(screen.getByLabelText("Resource scope")).toHaveTextContent('{"version":1,"resources":[{"resource_id":"orders","columns":null}]}');
    await user.click(screen.getByText("orders"));
    expect(screen.getByLabelText("Resource scope")).toHaveTextContent('{"version":1,"resources":[]}');
  });

  it("searches resource names and column names without clearing the selected scope", async () => {
    const user = userEvent.setup();
    render(<ResourceEditor />);
    await user.click(screen.getByText("orders"));
    const search = screen.getByRole("textbox", { name: "Search resources" });

    await user.type(search, "  AMOUNT ");
    expect(screen.getByText("orders")).toBeInTheDocument();
    expect(screen.queryByText("customers")).not.toBeInTheDocument();
    await user.clear(search);
    await user.type(search, "CUSTOMERS");
    expect(screen.getByText("customers")).toBeInTheDocument();
    expect(screen.queryByText("orders")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Resource scope")).toHaveTextContent('{"version":1,"resources":[{"resource_id":"orders","columns":null}]}');
    await user.clear(search);
    expect(screen.getByRole("checkbox", { name: "orders" })).toBeChecked();
  });
});
