/* eslint-disable testing-library/no-node-access */
import { render, screen } from "@testing-library/react";

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
