/* eslint-disable testing-library/no-node-access */
import { fireEvent, render, screen, within } from "@testing-library/react";

import { ConnectorStepSetup } from "./detail-connection-step-setup";

function renderSetup(required: boolean, source = "extension") {
  return render(<ConnectorStepSetup ctx={{
    connectorConnectionRequired: required,
    connectorConnectionSetupHref: "",
    connectorConnectionOptions: [{ value: "conn-1", label: "Account" }],
    connectorConnectionRows: [{ connection_id: "conn-1" }],
    connectorConnectionsRefreshing: false,
    connectorOperationOptions: [{ value: "action", label: "Action" }],
    connectorOperationRows: [], connectorOptions: [{ value: "extension.test", label: "Test" }], connectorRows: [],
    connectorOutputRows: [], connectorParameterRows: [],
    selectedConnectorId: "extension.test", selectedConnectorOperationId: "action",
    selectedConnectorRow: { source }, selectedConnectorOperation: {}, selectedConnectionTypeLabel: "account",
    stepKindConfig: { connection_binding: { connection_id: "conn-1" } }, stepToolCategory: "extension_tools",
    renderConnectorInputMappingEditor: () => null, renderConnectorOutputMappingEditor: () => null,
    renderContractChips: () => null, updateStepKindConfig: vi.fn(), replaceStepKindConfigField: vi.fn(),
    refreshConnectorConnections: vi.fn(),
  }} />);
}

it.each([true, false])("marks only mandatory connection selectors (required=%s)", (required) => {
  renderSetup(required);
  for (const name of ["Action source", "Action"]) {
    const field = screen.getByRole("combobox", { name });
    expect(field).toBeRequired();
    expect(field.closest("label")?.querySelector(".fs-required")).toHaveTextContent("*");
  }
  const connection = screen.getByRole("combobox", { name: "Connection" });
  if (required) expect(connection).toBeRequired();
  else expect(connection).not.toBeRequired();
  expect(Boolean(connection.closest(".block")?.querySelector(".fs-required"))).toBe(required);
});

it("also marks the Tool selector required for tool actions", () => {
  renderSetup(false, "mcp_connector");
  const tool = screen.getByRole("combobox", { name: "Tool" });
  expect(tool).toBeRequired();
  expect(tool.closest("label")?.querySelector(".fs-required")).toHaveTextContent("*");
});

describe("changing a step's action", () => {
  function chooseOption(control: HTMLElement, label: string) {
    if (control.getAttribute("aria-expanded") !== "true") {
      fireEvent.mouseDown(control);
      fireEvent.click(control);
    }
    const options = screen.getByRole("listbox", { name: `${control.getAttribute("aria-label")} options` });
    fireEvent.click(within(options).getByRole("option", { name: label }));
  }

  function renderWithMapping() {
    const updateStepKindConfig = vi.fn();
    const ctx = {
      connectorConnectionRequired: false,
      connectorConnectionSetupHref: "",
      connectorConnectionOptions: [],
      connectorConnectionRows: [],
      connectorConnectionsRefreshing: false,
      connectorOperationOptions: [
        { value: "get_rates", label: "Get shipping rates" },
        { value: "create_plan", label: "Create inbound plan" },
      ],
      connectorOperationRows: [
        { operation_id: "get_rates", parameters: [{ name: "ship_from", type: "object", required: true }, { name: "amazon_order_id", value_type: "text", required: true }, { name: "marketplace", value_type: "text" }] },
        { operation_id: "create_plan", parameters: [{ name: "items", type: "array", required: true }, { name: "marketplace", value_type: "text" }] },
      ],
      connectorOptions: [{ value: "extension.test", label: "Test" }, { value: "extension.other", label: "Other" }],
      connectorRows: [],
      connectorOutputRows: [],
      connectorParameterRows: [],
      selectedConnectorId: "extension.test",
      selectedConnectorOperationId: "get_rates",
      selectedConnectorRow: { source: "extension" },
      selectedConnectorOperation: {},
      selectedConnectionTypeLabel: "account",
      stepKindConfig: {
        input_mapping: { ship_from: "state.warehouse", amazon_order_id: "state.order_id", marketplace: 'literal:"UK"' },
      },
      stepToolCategory: "extension_tools",
      renderConnectorInputMappingEditor: () => null,
      renderConnectorOutputMappingEditor: () => null,
      renderContractChips: () => null,
      updateStepKindConfig,
      replaceStepKindConfigField: vi.fn(),
      refreshConnectorConnections: vi.fn(),
    };
    const view = render(<ConnectorStepSetup ctx={ctx} />);
    return { ctx, updateStepKindConfig, view };
  }

  it("keeps the inputs both actions share and says which filled ones were removed", () => {
    const { ctx, updateStepKindConfig, view } = renderWithMapping();
    chooseOption(screen.getByRole("combobox", { name: "Action" }), "Create inbound plan");
    expect(updateStepKindConfig).toHaveBeenCalledWith("connector", expect.objectContaining({
      operation_id: "create_plan",
      input_mapping: { marketplace: 'literal:"UK"' },
    }));
    view.rerender(<ConnectorStepSetup ctx={{ ...ctx, selectedConnectorOperationId: "create_plan" }} />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "The new action does not accept these mapped inputs, so they were removed: ship_from, amazon_order_id.",
    );
  });

  it("says what a new action source removed and keeps nothing from the old one", () => {
    const { ctx, updateStepKindConfig, view } = renderWithMapping();
    chooseOption(screen.getByRole("combobox", { name: "Action source" }), "Other");
    expect(updateStepKindConfig).toHaveBeenCalledWith("connector", expect.objectContaining({
      connector_id: "extension.other",
      operation_id: "",
      input_mapping: {},
    }));
    view.rerender(<ConnectorStepSetup ctx={{ ...ctx, selectedConnectorId: "extension.other", selectedConnectorOperationId: "" }} />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Changing the action source removed these mapped inputs: ship_from, amazon_order_id, marketplace.",
    );
  });

  it("shows no notice when nothing filled had to go", () => {
    const { ctx, view } = renderWithMapping();
    view.rerender(<ConnectorStepSetup ctx={{ ...ctx, stepKindConfig: { input_mapping: { marketplace: 'literal:"UK"' } } }} />);
    chooseOption(screen.getByRole("combobox", { name: "Action" }), "Create inbound plan");
    view.rerender(<ConnectorStepSetup ctx={{ ...ctx, stepKindConfig: { input_mapping: { marketplace: 'literal:"UK"' } }, selectedConnectorOperationId: "create_plan" }} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
