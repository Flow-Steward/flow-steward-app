import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";

import { createWorkflowConnectorRenderers } from "@/pages/workflows/detail/detail-connector-renderers";

describe("workflow connector resource picker rendering", () => {
  it("keeps the manual resource field and shows an accessible fail-closed state", () => {
    const fieldName = "file_id";
    const utils = createWorkflowConnectorRenderers({
      connectorOutputRows: [],
      connectorParameterRows: [{
        name: fieldName,
        label: "Provider resource",
        required: true,
        widget_type: "resource_picker",
        external_resource_picker: {
          allowed_script_origins: ["https://picker.example.test"],
          allowed_connect_origins: ["http://api.example.test"],
          allowed_frame_origins: ["https://frame.example.test"],
          bootstrap_action_id: "picker_bootstrap",
          bridge_global: "PartnerResourcePicker",
          button_label: "Choose provider resource",
          scripts: [{ src: "https://picker.example.test/sdk.js" }],
          target_component_id: "workflow_parameter_form",
          selection: {
            maximum: 1,
            fields: [{ source: "id", target: fieldName, required: true, max_length: 1024 }],
          },
        },
      }],
      renderWorkflowVariableInput: vi.fn(),
      replaceStepKindConfigField: vi.fn(),
      selectedStep: { raw: {} },
      selectedConnectorId: "extension.partner.resource",
      selectedExtensionInstallId: "install-partner-resource",
      selectedExtensionReleaseId: "release-partner-resource",
      selectedExtensionDescriptorSha256: "a".repeat(64),
      selectedProjectId: "project-alpha",
      stepKindConfig: {
        connection_binding: { connection_id: "provider-primary" },
        input_mapping: { [fieldName]: "literal:file-current" },
      },
    });

    render(utils.renderConnectorInputMappingEditor());

    expect(screen.getByRole("textbox", { name: "Parameter value 1" })).toHaveValue(
      "file-current",
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "This resource picker is unavailable. Enter or map the resource ID instead.",
    );
    expect(screen.queryByRole("button", { name: "Choose provider resource" }))
      .not.toBeInTheDocument();
  });
});


function MappingPresenceHarness({ initial, owner = "mapping-owner" }: { initial: Record<string, unknown>; owner?: string }) {
  const [mapping, setMapping] = useState(initial);
  const utils = createWorkflowConnectorRenderers({
    connectorOutputRows: [],
    connectorParameterRows: [{ name: "query", required: true }, { name: "filter" }, { name: "note" }, { name: "renamed_filter" }, { name: "renamed_note" }, { name: "connection_ref", required: true }],
    renderWorkflowVariableInput: ({ ariaLabel, value, onChange }: { ariaLabel: string; value: string; onChange: (value: string) => void }) => <input aria-label={ariaLabel} onChange={(event) => onChange(event.target.value)} value={value} />,
    replaceStepKindConfigField: (_kind: string, _field: string, value: Record<string, unknown>) => setMapping(value),
    selectedStep: { runtimeId: owner, raw: {} }, selectedConnectorId: "extension.fixture.mail",
    stepKindConfig: { operation_id: "send_message", function_name: "send_message", input_mapping: mapping },
  });
  return <>{utils.renderConnectorInputMappingEditor()}<output aria-label="Stored mapping">{JSON.stringify(mapping)}</output></>;
}
function storedMapping() {
  return JSON.parse(screen.getByLabelText("Stored mapping").textContent || "{}");
}

describe("Connector row presence through actual mapping controls", () => {
  it("preserves nullable and blank siblings through editing, removal and an owner replacement", () => {
    const view = render(<MappingPresenceHarness initial={{ query: "state.query", filter: null, note: "" }} />);
    fireEvent.change(screen.getByRole("textbox", { name: "Parameter value 1" }), { target: { value: "changed" } });
    expect(storedMapping()).toEqual({ query: "state.changed", filter: null, note: "" });
    fireEvent.click(screen.getByRole("button", { name: "Remove parameter mapping 2" }));
    expect(storedMapping()).toEqual({ query: "state.changed", note: "" });
    fireEvent.change(screen.getByRole("textbox", { name: "Parameter value 2" }), { target: { value: "state.note" } });
    expect(storedMapping()).toEqual({ query: "state.changed", note: "state.note" });
    view.rerender(<MappingPresenceHarness key="other-owner" owner="other-owner" initial={{ query: "state.other" }} />);
    expect(storedMapping()).toEqual({ query: "state.other" });
    expect(screen.queryByRole("combobox", { name: "Parameter 2" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "connection_ref" })).not.toBeInTheDocument();
  });
  it.each([
    { value: null, oldKey: "filter", nextKey: "renamed_filter" },
    { value: "", oldKey: "note", nextKey: "renamed_note" },
  ])("retains $value on an optional key-only rename and consumes provenance on a value edit", ({ value, oldKey, nextKey }) => {
    render(<MappingPresenceHarness initial={{ query: "state.query", [oldKey]: value }} />);
    fireEvent.change(screen.getByRole("combobox", { name: "Parameter 2" }), { target: { value: nextKey } });
    expect(storedMapping()).toEqual({ query: "state.query", [nextKey]: value });
    expect(Object.hasOwn(storedMapping(), oldKey)).toBe(false);
    expect(screen.getByRole("textbox", { name: "Parameter value 2" })).toHaveValue("");
    fireEvent.change(screen.getByRole("textbox", { name: "Parameter value 1" }), { target: { value: "state.updated" } });
    expect(storedMapping()).toEqual({ query: "state.updated", [nextKey]: value });
    fireEvent.change(screen.getByRole("textbox", { name: "Parameter value 2" }), { target: { value: "state.explicit" } });
    expect(storedMapping()).toEqual({ query: "state.updated", [nextKey]: "state.explicit" });
    expect(JSON.stringify(storedMapping())).not.toContain("retainedValue");
  });
  it("replaces a retained null only when the source mode is explicitly edited", () => {
    render(<MappingPresenceHarness initial={{ query: "state.query", filter: null }} />);
    fireEvent.change(screen.getByRole("combobox", { name: "Parameter source 2" }), { target: { value: "literal" } });
    expect(storedMapping()).toEqual({ query: "state.query", filter: "literal:" });
  });
});

/* Item 6: Shopify -> Get the store showed a Parameters block whose only row's select
   offered just "Choose parameter", because the action takes no parameters. */
describe("an action's Parameters and Output mapping blocks", () => {
  function renderBlocks({
    parameters,
    returns,
    inputMapping = {},
    outputMapping = { shop: "state.shop" },
  }: {
    parameters: Array<Record<string, unknown>>;
    returns: Array<Record<string, unknown>>;
    inputMapping?: Record<string, unknown>;
    outputMapping?: Record<string, unknown>;
  }) {
    const utils = createWorkflowConnectorRenderers({
      connectorOutputRows: returns,
      connectorParameterRows: parameters,
      renderWorkflowVariableInput: ({ ariaLabel, className, value }: { ariaLabel: string; className?: string; value: string }) => <input aria-label={ariaLabel} className={className} onChange={vi.fn()} value={value} />,
      replaceStepKindConfigField: vi.fn(),
      selectedStep: { runtimeId: "get-store", raw: {} },
      selectedConnectorId: "extension.shopify",
      stepKindConfig: { operation_id: "get_shop", input_mapping: inputMapping, output_mapping: outputMapping },
    });
    return render(<>{utils.renderConnectorInputMappingEditor()}{utils.renderConnectorOutputMappingEditor()}</>);
  }

  it("shows no Parameters block for an action that takes none, and its outputs", () => {
    renderBlocks({ parameters: [], returns: [{ name: "shop", type: "object" }] });

    expect(screen.queryByText("Parameters")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Parameter 1" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add parameter mapping" })).not.toBeInTheDocument();
    expect(screen.getByText("Output mapping")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Returned field 1" })).toHaveValue("shop");
  });

  it("shows no Output mapping block for an action that returns nothing", () => {
    const utils = createWorkflowConnectorRenderers({
      connectorOutputRows: [],
      connectorParameterRows: [],
      renderWorkflowVariableInput: vi.fn(),
      replaceStepKindConfigField: vi.fn(),
      selectedStep: { runtimeId: "fire", raw: {} },
      selectedConnectorId: "extension.shopify",
      stepKindConfig: { operation_id: "ping", input_mapping: {}, output_mapping: {} },
    });
    render(<>{utils.renderConnectorInputMappingEditor()}{utils.renderConnectorOutputMappingEditor()}</>);

    expect(screen.queryByText("Parameters")).not.toBeInTheDocument();
    expect(screen.queryByText("Output mapping")).not.toBeInTheDocument();
  });

  it("shows the parameters an action declares, with a quiet Remove", () => {
    renderBlocks({
      parameters: [{ name: "product_id", required: true }, { name: "fields" }],
      returns: [{ name: "shop", type: "object" }],
      inputMapping: { product_id: "state.product_id" },
      outputMapping: {},
    });

    expect(screen.getByText("Parameters")).toBeInTheDocument();
    const parameter = screen.getByRole("combobox", { name: "Parameter 1" });
    expect(parameter).toHaveValue("product_id");
    expect(within(parameter).getAllByRole("option").map((option) => (option as HTMLOptionElement).value)).toEqual(["", "product_id", "fields"]);
    const remove = screen.getByRole("button", { name: "Remove parameter mapping 1" });
    expect(remove).toHaveClass("fs-button", "ghost-danger", "sm");
    expect(remove).not.toHaveClass("danger");
    // The empty output row has nothing to remove: no disabled box that looks like another button.
    expect(screen.queryByRole("button", { name: "Remove output mapping 1" })).not.toBeInTheDocument();
  });

  it("draws the state-path fields as ordinary editable inputs, not grey read-only boxes", () => {
    renderBlocks({
      parameters: [{ name: "product_id", required: true }],
      returns: [{ name: "shop", type: "object" }],
      inputMapping: { product_id: "state.product_id" },
      outputMapping: { shop: "state.shop" },
    });

    for (const field of [screen.getByDisplayValue("state.product_id"), screen.getByDisplayValue("state.shop")]) {
      expect(field).toHaveClass("fs-input");
      expect(field).not.toHaveClass("fs-surface-inset");
      expect(field).toBeEnabled();
    }
    expect(screen.getByRole("button", { name: "Remove output mapping 1" })).toHaveClass("fs-button", "ghost-danger", "sm");
  });

  it("keeps a saved parameter the action no longer declares editable", () => {
    renderBlocks({ parameters: [], returns: [], inputMapping: { legacy_filter: "state.filter" } });

    expect(screen.getByText("Parameters")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Parameter 1" })).toHaveValue("legacy_filter");
  });
});
