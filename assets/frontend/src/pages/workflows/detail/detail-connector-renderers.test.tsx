import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";

import { createWorkflowConnectorRenderers } from "@/pages/workflows/detail/detail-connector-renderers";
import { invalidConnectorLiteralNames } from "@/pages/workflows/detail/detail-connector-literal";

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

describe("connector literal controls", () => {
  function LiteralHarness() {
    const [mapping, setMapping] = useState<Record<string, unknown>>({
      query: "literal:hello", data: "literal:{}", count: "literal:2", enabled: "literal:false",
    });
    const utils = createWorkflowConnectorRenderers({
      connectorOutputRows: [],
      connectorParameterRows: [
        { name: "query", label: "Question", widget_type: "textarea", required: true, help_text: "Write the question." },
        { name: "data", label: "Data", widget_type: "json", required: true },
        { name: "count", label: "Count", widget_type: "integer", required: true },
        { name: "enabled", label: "Enabled", widget_type: "boolean", required: true },
      ],
      renderWorkflowVariableInput: vi.fn(),
      replaceStepKindConfigField: (_kind: string, _field: string, value: Record<string, unknown>) => setMapping(value),
      selectedStep: { runtimeId: "literal-step", raw: {} }, selectedConnectorId: "extension.fixture",
      stepKindConfig: { function_name: "action", input_mapping: mapping },
    });
    return <>{utils.renderConnectorInputMappingEditor()}<output aria-label="Stored mapping">{JSON.stringify(mapping)}</output></>;
  }

  it("renders declared controls and preserves typed literal mappings", () => {
    render(<LiteralHarness />);
    expect(screen.getByRole("textbox", { name: "Parameter value 1" }).tagName).toBe("TEXTAREA");
    expect(screen.getByText("Write the question.")).toBeInTheDocument();
    const query = screen.getByRole("textbox", { name: "Parameter value 1" });
    for (const value of ["Hello", "Hello ", "Hello world", "Hello world\n", "Hello world\nNext"]) {
      fireEvent.change(query, { target: { value } });
      expect(query).toHaveValue(value);
    }
    expect(storedMapping()).toMatchObject({ query: 'literal:"Hello world\\nNext"' });
    fireEvent.change(query, { target: { value: "true" } });
    expect(storedMapping()).toMatchObject({ query: 'literal:"true"' });
    expect(screen.getByRole("textbox", { name: "Parameter value 1" })).toHaveValue("true");
    expect(screen.getByRole("textbox", { name: "Parameter value 2" }).tagName).toBe("TEXTAREA");
    expect(screen.getByRole("spinbutton", { name: "Parameter value 3" })).toHaveValue(2);
    const enabled = screen.getByRole("combobox", { name: "Parameter value 4" });
    expect(enabled).toHaveValue("false");
    fireEvent.change(enabled, { target: { value: "true" } });
    expect(storedMapping()).toMatchObject({ enabled: "literal:true" });
    fireEvent.change(enabled, { target: { value: "" } });
    expect(enabled).toHaveAttribute("aria-invalid", "true");
    expect(enabled).toHaveAttribute("aria-describedby", "connector-input-error-3");
  });

  it("identifies malformed JSON and invalid integers with recoverable errors", () => {
    render(<LiteralHarness />);
    const data = screen.getByRole("textbox", { name: "Parameter value 2" });
    fireEvent.change(data, { target: { value: "{" } });
    expect(data).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/valid JSON/i)).toBeInTheDocument();
    fireEvent.change(data, { target: { value: '{"ok":true}' } });
    expect(data).not.toHaveAttribute("aria-invalid", "true");
    fireEvent.change(screen.getByRole("spinbutton", { name: "Parameter value 3" }), { target: { value: "1.5" } });
    expect(screen.getByText(/whole number/i)).toBeInTheDocument();
  });

  it("explains why decimal and exponent integer spellings are rejected", () => {
    render(<LiteralHarness />);
    const count = screen.getByRole("spinbutton", { name: "Parameter value 3" });
    for (const value of ["1.0", "1e1"]) {
      fireEvent.change(count, { target: { value } });
      expect(count).toHaveAttribute("aria-invalid", "true");
      expect(screen.getByText(/without a decimal point or exponent/i)).toBeInTheDocument();
    }
    fireEvent.change(count, { target: { value: "10" } });
    expect(count).not.toHaveAttribute("aria-invalid", "true");
    expect(storedMapping()).toMatchObject({ count: "literal:10" });
  });
});

describe("workflow artifact inputs", () => {
  it("uses workflow state for artifacts and explains the limitation", () => {
    const utils = createWorkflowConnectorRenderers({
      connectorOutputRows: [], connectorParameterRows: [{ name: "file", label: "Input file", widget_type: "artifact_picker", required: true }],
      renderWorkflowVariableInput: ({ ariaLabel, value }: { ariaLabel: string; value: string }) => <input aria-label={ariaLabel} value={value} onChange={vi.fn()} />,
      replaceStepKindConfigField: vi.fn(), selectedStep: { raw: {} }, selectedConnectorId: "extension.fixture",
      stepKindConfig: { input_mapping: { file: "state.input_file" } },
    });
    render(utils.renderConnectorInputMappingEditor());
    expect(screen.getByText(/map a file artifact supplied or uploaded/i)).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Literal value" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Parameter value 1" })).toHaveValue("state.input_file");
  });
});

describe("string-valued select literals", () => {
  const doclingFormatPattern = String.raw`^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*(?:docx|doc|rtf|pptx|ppt|html|mhtml|image|pdf|asciidoc|md|csv|xlsx|xls|odt|ods|odp|xml_uspto|xml_jats|xml_xbrl|xml_doclang|dclx|mets_gbs|json_docling|audio|video|vtt|latex|email|epub|boxnote|iwork_pages|iwork_keynote|iwork_numbers|ebcdic|afp)?[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$`;
  it.each([
    { value: "  pdf\t", schema: { type: "string", pattern: doclingFormatPattern }, invalid: false },
    { value: "pdfx", schema: { type: "string", pattern: doclingFormatPattern }, invalid: false },
    { value: "unknown", schema: { type: "string", enum: ["pdf", "docx"] }, invalid: true },
  ])("displays a saved value outside the declared options: $value", ({ value, schema, invalid }) => {
    const mapping = { from_format: `literal:${JSON.stringify(value)}` };
    const replace = vi.fn();
    const utils = createWorkflowConnectorRenderers({
      connectorOutputRows: [], connectorParameterRows: [{ name: "from_format", value_type: "text", widget_type: "select", schema, options: [{ value: "pdf", label: "PDF" }, { value: "docx", label: "DOCX" }] }],
      renderWorkflowVariableInput: vi.fn(), replaceStepKindConfigField: replace,
      selectedStep: { raw: {} }, selectedConnectorId: "extension.fixture", stepKindConfig: { input_mapping: mapping },
    });
    render(utils.renderConnectorInputMappingEditor());
    const select = screen.getByRole("combobox", { name: "Parameter value 1" });
    expect(select).toHaveValue(value);
    expect(within(select).getByRole("option", { name: value.trim() })).toBeDisabled();
    if (invalid) {
      expect(select).toHaveAttribute("aria-invalid", "true");
      expect(screen.getByText(/matching the field's type and limits/i)).toBeInTheDocument();
    } else {
      expect(select).not.toHaveAttribute("aria-invalid");
    }
    expect(replace).not.toHaveBeenCalled();
    expect(invalidConnectorLiteralNames(mapping, [{ name: "from_format", value_type: "text", schema }])).toEqual(invalid ? ["from_format"] : []);
    fireEvent.change(select, { target: { value: "docx" } });
    expect(replace).toHaveBeenCalledWith("connector", "input_mapping", { from_format: 'literal:"docx"' });
  });

  it("keeps numeric-looking options as strings", () => {
    function SelectHarness() {
      const [mapping, setMapping] = useState<Record<string, unknown>>({ size: "literal:256" });
      const utils = createWorkflowConnectorRenderers({
        connectorOutputRows: [], connectorParameterRows: [{ name: "size", value_type: "text", widget_type: "select", schema: { type: "string", enum: ["256"] }, options: [{ value: "256", label: "256" }, { value: "512", label: "512" }] }],
        renderWorkflowVariableInput: vi.fn(), replaceStepKindConfigField: (_kind: string, _field: string, value: Record<string, unknown>) => setMapping(value),
        selectedStep: { raw: {} }, selectedConnectorId: "extension.fixture", stepKindConfig: { input_mapping: mapping },
      });
      return <>{utils.renderConnectorInputMappingEditor()}<output aria-label="Stored mapping">{JSON.stringify(mapping)}</output></>;
    }
    render(<SelectHarness />);
    fireEvent.change(screen.getByRole("combobox", { name: "Parameter value 1" }), { target: { value: "512" } });
    expect(storedMapping()).toEqual({ size: 'literal:"512"' });
    const select = screen.getByRole("combobox", { name: "Parameter value 1" });
    expect(select).toHaveValue("512");
    expect(select).toHaveAttribute("aria-invalid", "true");
    expect(select).toHaveAttribute("aria-describedby", "connector-input-error-0");
  });
});

describe("optional typed literal defaults", () => {
  function DefaultsHarness() {
    const [mapping, setMapping] = useState<Record<string, unknown>>({
      choice: 'literal:"256"', count: "literal:3", json: 'literal:["a"]', enabled: "literal:true", note: 'literal:"hello"', legacy: "literal:",
    });
    const utils = createWorkflowConnectorRenderers({
      connectorOutputRows: [], connectorParameterRows: [
        { name: "choice", widget_type: "select", value_type: "text", schema: { type: "string", enum: ["256"] }, options: [{ value: "256", label: "256" }] },
        { name: "count", widget_type: "number" }, { name: "json", widget_type: "json" },
        { name: "enabled", widget_type: "boolean" }, { name: "note", widget_type: "text", value_type: "text" },
        { name: "legacy", widget_type: "number" },
      ],
      renderWorkflowVariableInput: vi.fn(),
      replaceStepKindConfigField: (_kind: string, _field: string, value: Record<string, unknown>) => setMapping(value),
      selectedStep: { runtimeId: "defaults", raw: {} }, selectedConnectorId: "extension.fixture",
      stepKindConfig: { input_mapping: mapping },
    });
    return <>{utils.renderConnectorInputMappingEditor()}<output aria-label="Stored mapping">{JSON.stringify(mapping)}</output></>;
  }
  it("omits cleared optional typed values, holds their rows and preserves explicit empty text and legacy blanks", () => {
    render(<DefaultsHarness />);
    const choice = screen.getByRole("combobox", { name: "Parameter value 1" });
    fireEvent.change(choice, { target: { value: "" } });
    expect(choice).not.toHaveAttribute("aria-invalid", "true");
    expect(screen.queryByText(/matching the field's type and limits/i)).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("spinbutton", { name: "Parameter value 2" }), { target: { value: "" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Parameter value 3" }), { target: { value: "" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Parameter value 4" }), { target: { value: "" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Parameter value 5" }), { target: { value: "" } });
    expect(storedMapping()).toEqual({ note: 'literal:""', legacy: "literal:" });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Parameter value 2" }), { target: { value: "4" } });
    expect(storedMapping()).toEqual({ count: "literal:4", note: 'literal:""', legacy: "literal:" });
  });
});

describe("nullable connector literals", () => {
  function NullableHarness({ parameter, initial }: { parameter: Record<string, unknown>; initial: string }) {
    const [mapping, setMapping] = useState<Record<string, unknown>>({ value: initial });
    const utils = createWorkflowConnectorRenderers({
      connectorOutputRows: [], connectorParameterRows: [{ name: "value", required: true, ...parameter }],
      renderWorkflowVariableInput: vi.fn(),
      replaceStepKindConfigField: (_kind: string, _field: string, value: Record<string, unknown>) => setMapping(value),
      selectedStep: { runtimeId: "nullable", raw: {} }, selectedConnectorId: "extension.fixture",
      stepKindConfig: { input_mapping: mapping },
    });
    return <>{utils.renderConnectorInputMappingEditor()}<output aria-label="Stored mapping">{JSON.stringify(mapping)}</output></>;
  }

  it.each([
    { widget_type: "number", value_type: "number", schema: { type: ["number", "null"] }, nonNull: "2.5" },
    { widget_type: "integer", value_type: "integer", schema: { type: ["integer", "null"] }, nonNull: "2" },
    { widget_type: "boolean", value_type: "boolean", schema: { type: ["boolean", "null"] }, nonNull: "false" },
    { widget_type: "number", value_type: "number", schema: { anyOf: [{ type: "number" }, { type: "null" }] }, nonNull: "2.5" },
    { widget_type: "integer", value_type: "integer", schema: { oneOf: [{ type: "integer" }, { type: "null" }] }, nonNull: "2" },
  ])("displays and round-trips explicit null for $widget_type", ({ nonNull, ...parameter }) => {
    const view = render(<NullableHarness parameter={parameter} initial="literal:null" />);
    let source = screen.getByRole("combobox", { name: "Parameter source 1" });
    const nullOption = within(source).getByRole("option", { name: "Null value" });
    expect(nullOption).toBeEnabled();
    expect(nullOption).toHaveProperty("selected", true);
    expect(screen.queryByText(/enter a valid|matching the field|choose true or false/i)).not.toBeInTheDocument();
    expect(storedMapping()).toEqual({ value: "literal:null" });

    fireEvent.change(source, { target: { value: (within(source).getByRole("option", { name: "Literal value" }) as HTMLOptionElement).value } });
    const valueControl = screen.getByLabelText("Parameter value 1");
    fireEvent.change(valueControl, { target: { value: nonNull } });
    expect(storedMapping()).toEqual({ value: `literal:${nonNull}` });
    source = screen.getByRole("combobox", { name: "Parameter source 1" });
    fireEvent.change(source, { target: { value: (within(source).getByRole("option", { name: "Null value" }) as HTMLOptionElement).value } });
    expect(storedMapping()).toEqual({ value: "literal:null" });
    const saved = storedMapping().value as string;
    view.unmount();
    render(<NullableHarness parameter={parameter} initial={saved} />);
    expect(within(screen.getByRole("combobox", { name: "Parameter source 1" })).getByRole("option", { name: "Null value" })).toHaveProperty("selected", true);
    expect(storedMapping()).toEqual({ value: "literal:null" });
  });

  it.each([
    { widget_type: "number", value_type: "number", schema: { type: ["number", "null"] } },
    { widget_type: "text", value_type: "text", schema: { type: ["string", "null"] } },
  ])("hydrates whitespace around a JSON null for $widget_type", (parameter) => {
    const view = render(<NullableHarness parameter={parameter} initial="literal: null " />);
    const source = screen.getByRole("combobox", { name: "Parameter source 1" });
    expect(within(source).getByRole("option", { name: "Null value" })).toHaveProperty("selected", true);
    expect(screen.queryByText(/enter a valid|matching the field|choose true or false/i)).not.toBeInTheDocument();
    expect(JSON.parse((storedMapping().value as string).slice("literal:".length))).toBeNull();
    fireEvent.change(source, { target: { value: (within(source).getByRole("option", { name: "Literal value" }) as HTMLOptionElement).value } });
    fireEvent.change(source, { target: { value: (within(source).getByRole("option", { name: "Null value" }) as HTMLOptionElement).value } });
    const saved = storedMapping().value as string;
    expect(JSON.parse(saved.slice("literal:".length))).toBeNull();
    view.unmount();
    render(<NullableHarness parameter={parameter} initial={saved} />);
    expect(within(screen.getByRole("combobox", { name: "Parameter source 1" })).getByRole("option", { name: "Null value" })).toHaveProperty("selected", true);
  });

  it.each([
    { widget_type: "number", value_type: "number", schema: { type: ["number", "null"] }, payload: "\u0085null\u0085" },
    { widget_type: "text", value_type: "text", schema: { type: ["string", "null"] }, payload: "\u0085null\u0085" },
    { widget_type: "number", value_type: "number", schema: { type: ["number", "null"] }, payload: "\u001cnull\u001f" },
  ])("hydrates Python whitespace around null for $widget_type", ({ payload, ...parameter }) => {
    const view = render(<NullableHarness parameter={parameter} initial={`literal:${payload}`} />);
    const source = screen.getByRole("combobox", { name: "Parameter source 1" });
    expect(within(source).getByRole("option", { name: "Null value" })).toHaveProperty("selected", true);
    expect(screen.queryByText(/enter a valid|matching the field|choose true or false/i)).not.toBeInTheDocument();
    fireEvent.change(source, { target: { value: (within(source).getByRole("option", { name: "Literal value" }) as HTMLOptionElement).value } });
    fireEvent.change(source, { target: { value: (within(source).getByRole("option", { name: "Null value" }) as HTMLOptionElement).value } });
    expect(storedMapping()).toEqual({ value: "literal:null" });
    view.unmount();
    render(<NullableHarness parameter={parameter} initial="literal:null" />);
    expect(within(screen.getByRole("combobox", { name: "Parameter source 1" })).getByRole("option", { name: "Null value" })).toHaveProperty("selected", true);
  });

  it.each(["literal:\ufeffnull", 'literal:"\ufeffnull"'])("keeps a BOM in the text literal %s", (initial) => {
    const parameter = { widget_type: "text", value_type: "text", schema: { type: ["string", "null"] } };
    render(<NullableHarness parameter={parameter} initial={initial} />);
    const source = screen.getByRole("combobox", { name: "Parameter source 1" });
    expect(within(source).getByRole("option", { name: "Literal value" })).toHaveProperty("selected", true);
    expect(screen.getByRole("textbox", { name: "Parameter value 1" })).toHaveValue("\ufeffnull");
    expect(storedMapping()).toEqual({ value: initial });
  });

  it("keeps a whitespace-padded quoted null as text until the author selects null", () => {
    const parameter = { widget_type: "text", value_type: "text", schema: { type: ["string", "null"] } };
    render(<NullableHarness parameter={parameter} initial={'literal: "null"'} />);
    const source = screen.getByRole("combobox", { name: "Parameter source 1" });
    expect(within(source).getByRole("option", { name: "Literal value" })).toHaveProperty("selected", true);
    expect(screen.getByRole("textbox", { name: "Parameter value 1" })).toHaveValue("null");
    expect(JSON.parse((storedMapping().value as string).slice("literal:".length))).toBe("null");
    fireEvent.change(source, { target: { value: (within(source).getByRole("option", { name: "Null value" }) as HTMLOptionElement).value } });
    expect(storedMapping()).toEqual({ value: "literal:null" });
    expect(within(source).getByRole("option", { name: "Null value" })).toHaveProperty("selected", true);
  });

  it.each([
    { type: ["string", "null"] },
    { anyOf: [{ type: "string" }, { type: "null" }] },
    { oneOf: [{ type: "string" }, { type: "null" }] },
  ])("distinguishes null from the text 'null' for a nullable string schema %o", (schema) => {
    const parameter = { widget_type: "text", value_type: "text", schema };
    const view = render(<NullableHarness parameter={parameter} initial="literal:null" />);
    const source = screen.getByRole("combobox", { name: "Parameter source 1" });
    expect(within(source).getByRole("option", { name: "Null value" })).toHaveProperty("selected", true);
    fireEvent.change(source, { target: { value: (within(source).getByRole("option", { name: "Literal value" }) as HTMLOptionElement).value } });
    fireEvent.change(screen.getByRole("textbox", { name: "Parameter value 1" }), { target: { value: "null" } });
    expect(storedMapping()).toEqual({ value: 'literal:"null"' });
    view.unmount();
    render(<NullableHarness parameter={parameter} initial={'literal:"null"'} />);
    expect(within(screen.getByRole("combobox", { name: "Parameter source 1" })).getByRole("option", { name: "Literal value" })).toHaveProperty("selected", true);
    expect(screen.getByRole("textbox", { name: "Parameter value 1" })).toHaveValue("null");
    const reopenedSource = screen.getByRole("combobox", { name: "Parameter source 1" });
    fireEvent.change(reopenedSource, { target: { value: (within(reopenedSource).getByRole("option", { name: "Null value" }) as HTMLOptionElement).value } });
    expect(storedMapping()).toEqual({ value: "literal:null" });
    expect(within(reopenedSource).getByRole("option", { name: "Null value" })).toHaveProperty("selected", true);
  });

  it.each([
    { schema: { type: "number" }, value: "literal:null", invalid: true },
    { schema: { type: ["integer", "null"] }, value: "literal:1.5", invalid: true },
    { schema: { type: ["integer", "number", "null"] }, value: "literal:1.5", invalid: false },
    { schema: { type: ["number", "null"], enum: [1, null] }, value: "literal:2", invalid: true },
    { schema: { type: ["number", "null"], const: 1 }, value: "literal:null", invalid: true },
    { schema: { anyOf: [{ type: "number" }, { type: "null" }], enum: [1] }, value: "literal:null", invalid: true },
    { schema: { oneOf: [{ type: "number" }, { type: "null" }], const: 1 }, value: "literal:null", invalid: true },
  ])("enforces the schema for $value", ({ schema, value, invalid }) => {
    render(<NullableHarness parameter={{ widget_type: "number", value_type: "number", schema }} initial={value} />);
    if (invalid) {
      expect(screen.getByText(/valid number|matching the field/i)).toBeInTheDocument();
    } else {
      expect(screen.getByRole("spinbutton", { name: "Parameter value 1" })).not.toHaveAttribute("aria-invalid", "true");
    }
  });
});

describe("optional explicit text literal validation", () => {
  it("validates a cleared text value while preserving the saved empty string and omitting a cleared number", () => {
    function OptionalHarness() {
      const [mapping, setMapping] = useState<Record<string, unknown>>({ note: 'literal:"ok"', count: "literal:2" });
      const utils = createWorkflowConnectorRenderers({
        connectorOutputRows: [], connectorParameterRows: [
          { name: "note", widget_type: "text", value_type: "text", schema: { type: "string", minLength: 1 } },
          { name: "count", widget_type: "number", value_type: "number", schema: { type: "number" } },
        ],
        renderWorkflowVariableInput: vi.fn(),
        replaceStepKindConfigField: (_kind: string, _field: string, value: Record<string, unknown>) => setMapping(value),
        selectedStep: { runtimeId: "optional-validation", raw: {} }, selectedConnectorId: "extension.fixture",
        stepKindConfig: { input_mapping: mapping },
      });
      return <>{utils.renderConnectorInputMappingEditor()}<output aria-label="Stored mapping">{JSON.stringify(mapping)}</output></>;
    }
    render(<OptionalHarness />);
    const note = screen.getByRole("textbox", { name: "Parameter value 1" });
    fireEvent.change(note, { target: { value: "" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Parameter value 2" }), { target: { value: "" } });
    expect(storedMapping()).toEqual({ note: 'literal:""' });
    expect(note).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(note, { target: { value: " " } });
    expect(storedMapping()).toEqual({ note: 'literal:" "' });
    expect(note).not.toHaveAttribute("aria-invalid", "true");
  });
});
