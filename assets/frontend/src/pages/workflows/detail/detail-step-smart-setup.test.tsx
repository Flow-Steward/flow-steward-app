import { useState } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";

import { inspectWorkflowTabularFeed } from "@/pages/workflows/shared/api";
import { WorkflowStepKindSmartSetup } from "@/pages/workflows/detail/detail-step-smart-setup";
import { useWorkflowStepRendererController } from "@/pages/workflows/detail/detail-step-renderer-controller";
import { editableOverrideForRawStep } from "@/pages/workflows/detail/detail-step-editing";

vi.mock("@/pages/workflows/shared/api", () => ({
  inspectWorkflowTabularFeed: vi.fn(),
}));

function renderVariableInput(props: {
  ariaDescribedBy?: string;
  ariaInvalid?: boolean;
  ariaLabel: string;
  ariaRequired?: boolean;
  className?: string;
  multiline?: boolean;
  name?: string;
  panelId?: string;
  onChange: (value: string) => void;
  placeholder?: string;
  value?: string;
}) {
  const shared = {
    "aria-describedby": props.ariaDescribedBy,
    "aria-invalid": props.ariaInvalid || undefined,
    "aria-label": props.ariaLabel,
    "aria-required": props.ariaRequired || undefined,
    className: props.className,
    name: props.name,
    onChange: (event: { target: { value: string } }) => props.onChange(event.target.value),
    placeholder: props.placeholder,
    value: props.value || "",
  };
  return props.multiline ? <textarea {...shared} /> : <input {...shared} />;
}

function renderTabularSetup(stepKindConfig: Record<string, unknown>, locale = "en") {
  const replaceStepKindConfigField = vi.fn();
  const updateStepKindConfig = vi.fn();
  const renderWorkflowVariableInput = vi.fn(renderVariableInput);
  return {
    replaceStepKindConfigField,
    updateStepKindConfig,
    renderWorkflowVariableInput,
    ...render(
      <I18nContext.Provider value={{ ...fallbackContext, locale, t: (key, text = "", params) => fallbackContext.t(key, locale === "lt" ? ({
        "react.workflows.transform": "transformuoti",
        "react.workflows.validate": "patikrinti",
        "shell.theme.mode.auto": "automatinis",
        "react.workflows.relational_operations": "Reliacinės operacijos",
        "react.workflows.validation_checks": "Patikros",
        "react.workflows.input_format": "Įvesties formatas",
        "react.workflows.display_dataset_handle": "Duomenų rinkinio nuoroda",
      } as Record<string, string>)[key] || text : text, params) }}><WorkflowStepKindSmartSetup
        ctx={{
          renderWorkflowVariableInput,
          replaceStepKindConfigField,
          selectedProjectId: "project-123",
          stepKindConfig,
          stepKindDraft: "tabular_feed",
          transformActionsQuery: {
            data: {
              actions: [
                {
                  action_id: "number.multiply",
                  backends: { polars: true, scalar: true },
                  category: "number",
                  display_name: "Multiply",
                  params_schema: { properties: { factor: { type: "string" } } },
                },
                {
                  action_id: "text.trim",
                  backends: { polars: true, scalar: true },
                  category: "text",
                  display_name: "Trim",
                  params_schema: { properties: {} },
                },
              ],
            },
          },
          updateStepKindConfig,
          workflowId: "workflow-123",
        }}
      /></I18nContext.Provider>,
    ),
  };
}

function renderDatasetSetup(stepKindConfig: Record<string, unknown>) {
  const replaceStepKindConfigField = vi.fn();
  return {
    replaceStepKindConfigField,
    ...render(
      <WorkflowStepKindSmartSetup
        ctx={{
          availableWorkflowVariableSuggestions: [
            {
              label: "Workflow inputs",
              options: [{ label: "Project name", value: "state.inputs.project_name" }],
            },
          ],
          datasetQueryTemplateOptions: [{ label: "Projects", value: "query-1" }],
          datasetQueryTemplatesQuery: { isError: false, isLoading: false },
          renderWorkflowVariableInput: renderVariableInput,
          replaceStepKindConfigField,
          selectedDatasetQueryTemplate: {
            query_template_id: "query-1",
            params_schema_jsonb: {
              type: "object",
              properties: { project_name: { type: "string" } },
              required: ["project_name"],
              additionalProperties: false,
            },
          },
          stepKindConfig,
          stepKindDraft: "dataset_query",
        }}
      />,
    ),
  };
}

function renderBatchSetup(stepKindConfig: Record<string, unknown>) {
  const replaceStepKindConfigField = vi.fn();
  return {
    replaceStepKindConfigField,
    ...render(
      <WorkflowStepKindSmartSetup
        ctx={{
          renderWorkflowVariableInput: renderVariableInput,
          replaceStepKindConfigField,
          stepKindConfig,
          stepKindDraft: "batch",
        }}
      />,
    ),
  };
}

function renderHttpRequestSetup(stepKindConfig: Record<string, unknown>) {
  const replaceStepKindConfigField = vi.fn();
  const updateStepKindConfig = vi.fn();
  return {
    replaceStepKindConfigField,
    updateStepKindConfig,
    ...render(
      <WorkflowStepKindSmartSetup
        ctx={{
          httpResponseTargetListId: "http-response-target-suggestions",
          renderConfigMapEditor: () => null,
          renderWorkflowVariableInput: renderVariableInput,
          replaceStepKindConfigField,
          stepKindConfig,
          stepKindDraft: "http_request",
          updateStepKindConfig,
        }}
      />,
    ),
  };
}

function chooseSetupOption(control: HTMLElement, label: string) {
  if (control.getAttribute("aria-expanded") !== "true") {
    fireEvent.mouseDown(control);
    fireEvent.click(control);
  }
  const options = screen.getByRole("listbox", { name: `${control.getAttribute("aria-label")} options` });
  fireEvent.click(within(options).getByRole("option", { name: label }));
}

describe("WorkflowStepKindSmartSetup tabular_feed builder", () => {
  it.each(["checksum", "diff"])("uses the existing variable control for %s source checksum", (operation) => {
    const { renderWorkflowVariableInput, updateStepKindConfig } = renderTabularSetup({
      operation, input: { dataset_handle: "state.current", baseline_dataset_handle: "state.baseline" },
      raw_sha256: "state.source.sha256", profile_key: "supplier", key_column: "sku", result_target: "state.checked",
    });
    const control = screen.getByRole("textbox", { name: "Raw SHA-256" });
    expect(renderWorkflowVariableInput).toHaveBeenCalledWith(expect.objectContaining({
      name: "tabular-feed-raw-sha256", value: "state.source.sha256", ariaRequired: operation === "checksum",
    }));
    expect(control).toHaveValue("state.source.sha256");
    if (operation === "checksum") {
      expect(control).toBeRequired();
      expect(screen.getByLabelText(/^Raw SHA-256\s*\*$/)).toBe(control);
    } else expect(control).not.toBeRequired();
    fireEvent.change(control, { target: { value: "state.original_file.sha256" } });
    expect(updateStepKindConfig).toHaveBeenLastCalledWith("tabular_feed", { raw_sha256: "state.original_file.sha256" });
  });

  it("preserves a structured snapshot reference when loading diff inputs", () => {
    const ref = { revision: 7, snapshot_artifact_id: "art_baseline" };
    const { updateStepKindConfig } = renderTabularSetup({
      operation: "read_rows",
      input: { dataset_handle: "state.current", baseline_dataset_handle: "state.baseline", expected_snapshot_ref: ref },
      profile_key: "supplier", key_column: "sku", result_target: "state.diff",
    });
    chooseSetupOption(screen.getByRole("combobox", { name: "Operation" }), "diff");
    expect(updateStepKindConfig).toHaveBeenLastCalledWith("tabular_feed", {
      operation: "diff",
      input: { dataset_handle: "state.current", baseline_dataset_handle: "state.baseline", expected_snapshot_ref: ref },
    });
  });

  it("offers a required business-purpose select without exposing snapshot system fields", () => {
    const { updateStepKindConfig } = renderTabularSetup({
      operation: "diff", input: { dataset_handle: "state.current", baseline_dataset_handle: "state.baseline" },
      profile_key: "supplier", key_column: "sku", result_target: "state.diff",
    });
    expect(screen.queryByRole("textbox", { name: "Expected snapshot reference" })).not.toBeInTheDocument();
    const purpose = screen.getByRole("combobox", { name: "Comparison purpose" });
    expect(purpose).toHaveTextContent("Compare datasets");
    expect(purpose).toBeRequired();
    for (const marker of screen.getAllByText("*")) expect(marker).toHaveClass("fs-required");
    chooseSetupOption(purpose, "Prepare feed for publication");
    expect(updateStepKindConfig).toHaveBeenLastCalledWith("tabular_feed", { snapshot_mode: "publish" });
    for (const name of ["Dataset handle", "Baseline dataset handle", "Result target"]) {
      const control = screen.getByRole("textbox", { name: new RegExp(`^${name}`) });
      expect(control).toBeRequired();
      expect(screen.getByLabelText(new RegExp(`^${name}\\s*\\*$`))).toBe(control);
    }
    for (const name of ["Profile key", "Key column"]) {
      const control = screen.getByRole("textbox", { name: new RegExp(`^${name}`) });
      expect(control).toBeRequired();
      expect(screen.getByLabelText(new RegExp(`^${name}\\s*\\*$`))).toBe(control);
    }
  });

  it.each([
    ["input", { revision: 7, snapshot_artifact_id: "art_baseline" }],
    ["input", "state.checksum.committed_snapshot_ref"],
    ["top", { revision: 7, snapshot_artifact_id: "art_baseline" }],
    ["top", "state.checksum.committed_snapshot_ref"],
  ])("loads prior hidden %s references as publication and clears authority: %j", (location, ref) => {
    const handles = { dataset_handle: "state.current", baseline_dataset_handle: "state.baseline" };
    const config = {
      operation: "diff", input: location === "input" ? { ...handles, expected_snapshot_ref: ref } : handles,
      ...(location === "top" ? { expected_snapshot_ref: ref } : {}),
      profile_key: "supplier", key_column: "sku", result_target: "state.diff",
    };
    const { updateStepKindConfig } = renderTabularSetup(config);
    expect(screen.queryByRole("textbox", { name: "Expected snapshot reference" })).not.toBeInTheDocument();
    const purpose = screen.getByRole("combobox", { name: "Comparison purpose" });
    expect(purpose).toHaveTextContent("Prepare feed for publication");
    chooseSetupOption(purpose, "Compare datasets");
    expect(updateStepKindConfig).toHaveBeenLastCalledWith("tabular_feed", {
      snapshot_mode: "compare", input: handles,
      ...(location === "top" ? { expected_snapshot_ref: undefined } : {}),
    });
    // The existing controller shallow-merges and serializes this exact patch.
    const patch = updateStepKindConfig.mock.lastCall?.[1];
    const saved = JSON.parse(JSON.stringify({ ...config, ...patch }));
    expect(saved.snapshot_mode).toBe("compare");
    expect(saved).not.toHaveProperty("expected_snapshot_ref");
    expect(saved.input).not.toHaveProperty("expected_snapshot_ref");
  });

  it("keeps transform controls visible when operation labels are Lithuanian", () => {
    renderTabularSetup({ operation: "transform", input: { dataset_handle: "state.dataset" }, result_target: "state.output" }, "lt");
    expect(screen.getByRole("textbox", { name: "Duomenų rinkinio nuoroda" })).toHaveValue("state.dataset");
    expect(screen.getByText("Reliacinės operacijos")).toBeVisible();
  });

  it("keeps validate controls visible when operation labels are Lithuanian", () => {
    renderTabularSetup({ operation: "validate", input: { dataset_handle: "state.dataset" }, checks: { unique: ["sku"] }, result_target: "state.checked" }, "lt");
    expect(screen.getByRole("textbox", { name: "Duomenų rinkinio nuoroda" })).toHaveValue("state.dataset");
    expect(screen.getByRole("textbox", { name: "Patikros" })).toBeVisible();
  });

  it("submits canonical auto format from its Lithuanian option using the keyboard", async () => {
    const user = userEvent.setup();
    const { updateStepKindConfig } = renderTabularSetup({ operation: "read", input: { artifact_handle: "state.file" }, options: { format: "csv" }, result_target: "state.dataset" }, "lt");
    screen.getByRole("combobox", { name: "Įvesties formatas" }).focus();
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(screen.getByRole("option", { name: "csv" })).toHaveFocus());
    await user.keyboard("{Home}{Enter}");
    expect(updateStepKindConfig).toHaveBeenLastCalledWith("tabular_feed", { options: { format: "auto" } });
  });

  it("requires and exposes both explicit diff dataset inputs", () => {
    renderTabularSetup({ operation: "diff", input: { dataset_handle: "state.current" }, profile_key: "supplier", key_column: "sku", result_target: "state.diff" });
    expect(screen.getByRole("textbox", { name: "Dataset handle" })).toHaveValue("state.current");
    expect(screen.getByRole("textbox", { name: "Baseline dataset handle" })).toHaveValue("");
    expect(screen.getByText(/Baseline dataset handle is required/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Artifact handle" })).not.toBeInTheDocument();
  });
  it.each([
    ["read", "inspect", { artifact_handle: "state.artifact" }, { artifact_handle: "state.artifact" }],
    ["diff", "write", { dataset_handle: "state.dataset", baseline_dataset_handle: "state.baseline" }, { dataset_handle: "state.dataset" }],
    ["read", "concat", { artifact_handle: "state.artifact" }, {}],
    ["transform", "read", { dataset_handle: "state.dataset" }, {}],
    ["read", "create_dataset", { artifact_handle: "state.artifact" }, {}],
    ["checksum", "commit_snapshot", { profile_key: "supplier", raw_sha256: "abc" }, { profile_key: "supplier" }],
  ])("keeps only compatible inputs when changing %s to %s", (operation, nextOperation, input, expectedInput) => {
    const { updateStepKindConfig } = renderTabularSetup({
      operation,
      input,
      result_target: "state.result",
    });

    chooseSetupOption(screen.getByRole("combobox", { name: "Operation" }), nextOperation);

    expect(updateStepKindConfig).toHaveBeenLastCalledWith("tabular_feed", {
      operation: nextOperation,
      input: expectedInput,
    });
  });

  it("renders dedicated tabular feed controls instead of raw input JSON", () => {
    renderTabularSetup({
      input: { artifact_handle: "state.feed_artifact" },
      operation: "read",
      result_target: "state.feed_dataset",
    });

    expect(screen.queryByText("Input JSON")).not.toBeInTheDocument();
    expect(screen.getByText("Artifact handle")).toBeInTheDocument();
    expect(screen.getByText("Input format")).toBeInTheDocument();
    expect(screen.getByText("Schema")).toBeInTheDocument();
    expect(screen.getByText("10-row preview")).toBeInTheDocument();
  });

  it("exposes the canonical checksum operation with typed inputs", () => {
    renderTabularSetup({
      operation: "checksum",
      raw_sha256: "",
      result_target: "state.feed_checksum",
    });

    fireEvent.click(screen.getByRole("combobox", { name: "Operation" }));
    expect(screen.getByRole("option", { name: "checksum" })).toBeInTheDocument();
    expect(screen.getByText("Profile key")).toBeInTheDocument();
    expect(screen.getByText("Raw SHA-256")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Result target" })).toHaveAttribute(
      "name",
      "tabular-feed-result-target",
    );
    expect(screen.getByRole("textbox", { name: "Raw SHA-256" })).toHaveAttribute(
      "name",
      "tabular-feed-raw-sha256",
    );
    expect(
      screen.getByText(
        (_content, element) =>
          element?.textContent === "raw_sha256: SHA-256 checksum is required.",
      ),
    ).toBeInTheDocument();
  });

  it("exposes action and relational builders with indexed validation errors", async () => {
    const user = userEvent.setup();
    renderTabularSetup({
      actions: [{ action: "money.multiply", params: { factor: "0.9" }, target: "" }],
      input: { dataset_handle: "state.feed_dataset.dataset_handle" },
      operation: "transform",
      relational: [{ op: "filter", when: { column: "supplier" } }],
      result_target: "state.transformed",
    });

    expect(screen.getByText("Actions")).toBeInTheDocument();
    expect(screen.getByText("Relational operations")).toBeInTheDocument();
    expect(screen.getByText("actions[0].target")).toBeInTheDocument();
    expect(
      screen.getAllByText(
        (_content, element) =>
          element?.textContent === "actions[0].target: Target column is required.",
      ).length,
    ).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "Add action" }));

    expect(screen.getAllByText("Action").length).toBeGreaterThan(0);
  });

  it("renders action parameters from the catalog schema", () => {
    renderTabularSetup({
      actions: [{ action: "number.multiply", params: { factor: "0.9" }, target: "price" }],
      input: { dataset_handle: "state.feed_dataset.dataset_handle" },
      operation: "transform",
      result_target: "state.transformed",
    });

    expect(screen.getByText("Parameters")).toBeInTheDocument();
    expect(screen.getByText("factor")).toBeInTheDocument();
    expect(screen.queryByText("Params")).not.toBeInTheDocument();
  });

  it("resets stale params when switching catalog actions", async () => {
    const { updateStepKindConfig } = renderTabularSetup({
      actions: [{ action: "number.multiply", params: { factor: "0.9" }, target: "price" }],
      input: { dataset_handle: "state.feed_dataset.dataset_handle" },
      operation: "transform",
      result_target: "state.transformed",
    });

    await chooseSetupOption(screen.getByRole("combobox", { name: "Action" }), "text / Trim");

    expect(updateStepKindConfig).toHaveBeenLastCalledWith("tabular_feed", {
      actions: [{ action: "text.trim", params: {}, target: "price" }],
    });
  });

  it("resets stale relational fields when switching operations", async () => {
    const { updateStepKindConfig } = renderTabularSetup({
      input: { dataset_handle: "state.feed_dataset.dataset_handle" },
      operation: "transform",
      relational: [{ op: "sort", by: ["qty"], descending: true, nulls_last: false }],
      result_target: "state.transformed",
    });

    await chooseSetupOption(screen.getAllByRole("combobox", { name: "Operation" })[1], "filter");

    expect(updateStepKindConfig).toHaveBeenLastCalledWith("tabular_feed", {
      relational: [{ op: "filter", when: { column: "", equals: "" } }],
    });
  });

  it("edits an optional unnest prefix and restores it on reopen", () => {
    const config = {
      operation: "transform",
      input: { dataset_handle: "state.products.dataset_handle" },
      relational: [{ op: "explode", column: "variants" }, { op: "unnest", column: "variants" }],
      result_target: "state.variants",
    };
    const view = renderTabularSetup(config);
    const prefix = screen.getByRole("textbox", { name: "Prefix (optional)" });
    expect(prefix).toHaveValue("");
    expect(prefix).not.toBeRequired();
    expect(prefix).toHaveAttribute("placeholder", "variant_");

    fireEvent.change(prefix, { target: { value: "variant_" } });
    const saved = view.updateStepKindConfig.mock.lastCall?.[1];
    expect(saved).toEqual({ relational: [{ op: "explode", column: "variants" }, { op: "unnest", column: "variants", prefix: "variant_" }] });
    view.unmount();

    renderTabularSetup({ ...config, ...saved });
    expect(screen.getByRole("textbox", { name: "Prefix (optional)" })).toHaveValue("variant_");
  });

  it("does not show a prefix for explode or keep one when switching away from unnest", () => {
    const view = renderTabularSetup({
      operation: "transform",
      input: { dataset_handle: "state.products.dataset_handle" },
      relational: [{ op: "explode", column: "variants" }],
      result_target: "state.variants",
    });
    expect(screen.queryByRole("textbox", { name: "Prefix (optional)" })).not.toBeInTheDocument();
    view.unmount();

    const { updateStepKindConfig } = renderTabularSetup({
      operation: "transform",
      input: { dataset_handle: "state.products.dataset_handle" },
      relational: [{ op: "unnest", column: "variants", prefix: "variant_" }],
      result_target: "state.variants",
    });
    expect(screen.getByRole("textbox", { name: "Prefix (optional)" })).toHaveValue("variant_");
    chooseSetupOption(screen.getAllByRole("combobox", { name: "Operation" })[1], "explode");
    expect(updateStepKindConfig).toHaveBeenLastCalledWith("tabular_feed", { relational: [{ op: "explode", column: "" }] });
  });

  it("inspects artifacts through the core workflow API", async () => {
    const user = userEvent.setup();
    const mockedInspect = vi.mocked(inspectWorkflowTabularFeed);
    mockedInspect.mockResolvedValueOnce({
      ok: true,
      result: {
        columns: [{ name: "sku", type: "String" }],
        preview_rows: [{ sku: "00123" }],
      },
    });
    const { updateStepKindConfig } = renderTabularSetup({
      input: { artifact_handle: "state.feed_artifact" },
      operation: "read",
      options: { format: "csv" },
      result_target: "state.feed_dataset",
    });

    await user.click(screen.getByRole("button", { name: "Inspect artifact" }));

    expect(mockedInspect).toHaveBeenCalledWith(
      "project-123",
      "workflow-123",
      "state.feed_artifact",
      { format: "csv" },
    );
    expect(updateStepKindConfig).toHaveBeenLastCalledWith("tabular_feed", {
      schema: { columns: [{ name: "sku", type: "String" }] },
      preview_rows: [{ sku: "00123" }],
    });
  });
});

describe("WorkflowStepKindSmartSetup batch builder", () => {
  it("caps batch size at the runtime maximum", () => {
    const { replaceStepKindConfigField } = renderBatchSetup({ batch_size: 10 });
    const input = screen.getByRole("spinbutton", { name: "Batch size" });

    expect(input).toHaveAttribute("min", "1");
    expect(input).toHaveAttribute("max", "1000");
    fireEvent.change(input, { target: { value: "5000" } });

    expect(replaceStepKindConfigField).toHaveBeenLastCalledWith(
      "batch",
      "batch_size",
      1000,
    );
  });
});

describe("WorkflowStepKindSmartSetup http_request builder", () => {
  it("shows structured JSON bodies and keeps them structured when edited", () => {
    const { replaceStepKindConfigField } = renderHttpRequestSetup({
      body_template: {
        line_items: [{ sku: "{{state.sku}}", quantity: 2 }],
      },
      expected_statuses: [200],
      method: "POST",
      response_target: "state.api_response",
      url: "https://api.example.test/items",
    });

    const body = screen.getByRole("textbox", { name: "Body" });
    expect(body).toHaveValue(
      '{\n  "line_items": [\n    {\n      "sku": "{{state.sku}}",\n      "quantity": 2\n    }\n  ]\n}',
    );

    fireEvent.change(body, {
      target: { value: '{"sku":"{{state.sku}}","quantity":3}' },
    });

    expect(replaceStepKindConfigField).toHaveBeenLastCalledWith(
      "http_request",
      "body_template",
      { sku: "{{state.sku}}", quantity: 3 },
    );
  });

  it("writes URL, response target, statuses, and body to canonical runtime keys", () => {
    const replaceStepKindConfigField = vi.fn();
    render(<NumericIntentSetup kind="http_request" initialConfig={{
      expected_statuses: [200],
      method: "GET",
      response_target: "",
      url: "",
    }} onReplace={replaceStepKindConfigField} />);

    fireEvent.change(screen.getByRole("textbox", { name: "URL" }), {
      target: { value: "https://api.example.test/items" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Response target" }), {
      target: { value: "state.api_response" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Expected statuses" }), {
      target: { value: "200, 202" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Timeout seconds" }), {
      target: { value: "12.5" },
    });
    chooseSetupOption(screen.getByRole("combobox", { name: "Body value format" }), "JSON");
    expect(screen.getByRole("combobox", { name: "Body value format" })).toHaveTextContent("JSON");
    fireEvent.change(screen.getByRole("textbox", { name: "Body" }), {
      target: { value: '{"sku":"{{state.sku}}"}' },
    });
    expect(screen.getByRole("combobox", { name: "Body value format" })).toHaveTextContent("JSON");
    expect(screen.getByRole("textbox", { name: "Body" })).toHaveValue('{"sku":"{{state.sku}}"}');

    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request",
      "url",
      "https://api.example.test/items",
    );
    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request",
      "response_target",
      "state.api_response",
    );
    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request",
      "expected_statuses",
      [200, 202],
    );
    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request",
      "timeout_seconds",
      12.5,
    );
    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request",
      "body_template",
      { sku: "{{state.sku}}" },
    );
  });

  it("writes method, response storage, and authentication fields to runtime keys", () => {
    const { replaceStepKindConfigField, updateStepKindConfig } =
      renderHttpRequestSetup({
        auth_mode: "none",
        auth_ref: "",
        expected_statuses: [200],
        max_response_bytes: 100 * 1024 * 1024,
        method: "GET",
        response_mode: "artifact",
      });

    chooseSetupOption(screen.getByRole("combobox", { name: "Method" }), "POST");
    chooseSetupOption(screen.getByRole("combobox", { name: "Response mode" }), "inline");
    fireEvent.change(screen.getByRole("textbox", { name: "Max response bytes" }), {
      target: { value: "4096" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Artifact filename" }), {
      target: { value: "response.json" },
    });
    chooseSetupOption(screen.getByRole("combobox", { name: "Auth mode" }), "bearer");
    fireEvent.change(screen.getByRole("textbox", { name: "Auth reference" }), {
      target: { value: "secret:http-token" },
    });

    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request",
      "method",
      "POST",
    );
    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request", "response_mode", "inline",
    );
    expect(updateStepKindConfig).not.toHaveBeenCalled();
    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request",
      "max_response_bytes",
      4096,
    );
    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request",
      "artifact_filename",
      "response.json",
    );
    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request",
      "auth_mode",
      "bearer",
    );
    expect(replaceStepKindConfigField).toHaveBeenCalledWith(
      "http_request",
      "auth_ref",
      "secret:http-token",
    );
  });
});

describe("WorkflowStepKindSmartSetup dataset_query builder", () => {
  it("renders declared saved-query parameters as workflow mappings", async () => {
    const { replaceStepKindConfigField } = renderDatasetSetup({
      query_template_id: "query-1",
      params: {},
    });

    const input = screen.getByRole("textbox", {
      name: "Query parameter project_name",
    });
    expect(screen.getByText("required")).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "state.inputs.project_name" } });

    expect(replaceStepKindConfigField).toHaveBeenLastCalledWith(
      "dataset_query",
      "params",
      { project_name: "state.inputs.project_name" },
    );
  });
});

// The actual setup component owns these events; this fixture keeps its draft
// updates real rather than asserting only on a replacement callback.
function NumericIntentSetup({ kind, initialConfig, onReplace }: Readonly<{
  kind: string;
  initialConfig: Record<string, unknown>;
  onReplace?: (kind: string, field: string, value: unknown) => void;
}>) {
  const [config, setConfig] = useState(initialConfig);
  return <WorkflowStepKindSmartSetup ctx={{
    stepKindDraft: kind,
    stepKindConfig: config,
    replaceStepKindConfigField: (stepKind: string, field: string, value: unknown) => {
      onReplace?.(stepKind, field, value);
      setConfig((current) => JSON.parse(JSON.stringify({ ...current, [field]: value })) as Record<string, unknown>);
    },
    updateStepKindConfig: (_kind: string, patch: Record<string, unknown>) => {
      setConfig((current) => JSON.parse(JSON.stringify({ ...current, ...patch })) as Record<string, unknown>);
    },
    renderConfigMapEditor: () => null,
    renderWorkflowVariableInput: renderVariableInput,
    datasetQueryTemplateOptions: [{ label: "Projects", value: "query-1" }],
    datasetQueryTemplatesQuery: { isError: false, isLoading: false },
    selectedDatasetQueryTemplate: { params_schema_jsonb: { properties: {} } },
  }} />;
}

function expectNumericFieldError(input: HTMLElement) {
  expect(input).toHaveAttribute("aria-invalid", "true");
  const descriptionId = input.getAttribute("aria-describedby");
  expect(descriptionId).toBeTruthy();
  const error = screen.getByRole("alert");
  expect(error).toHaveAttribute("id", descriptionId);
  expect(error).toBeVisible();
  expect(error).toHaveAttribute("role", "alert");
  expect(error?.textContent?.trim()).not.toBe("");
}

describe("numeric setup preserves author intent", () => {
  it.each([
    { kind: "dataset_query", config: { row_limit: 100, timeout_seconds: 30 }, label: "Row limit" },
    { kind: "http_request", config: { timeout_seconds: 30, max_response_bytes: 4096 }, label: "Max response bytes" },
  ])("retains keyboard-entered invalid $label instead of coercing it or treating it as an optional blank", async ({ kind, config, label }) => {
    const user = userEvent.setup();
    render(<NumericIntentSetup kind={kind} initialConfig={config} />);
    const input = screen.getByLabelText(label);
    for (const raw of ["1e", "1.", "1.5", "0x10"]) {
      await user.clear(input);
      await user.type(input, raw);
      expect(input).toHaveValue(raw);
      expectNumericFieldError(input);
      expect(input).not.toBeRequired();
    }
    await user.clear(input);
    expect(input).toHaveValue("");
    expect(input).not.toHaveAttribute("aria-invalid", "true");
    expect(input).not.toHaveAttribute("aria-describedby");
  });

  it.each([
    { raw: "", displayed: null },
    { raw: "-1", displayed: -1 },
    { raw: "1.5", displayed: 1.5 },
    { raw: "31536000001", displayed: 31536000001 },
  ])("keeps invalid fixed Wait input $raw visible instead of repairing it", ({ raw, displayed }) => {
    render(<NumericIntentSetup kind="wait" initialConfig={{ duration_ms: 60000 }} />);
    const input = screen.getByRole("spinbutton", { name: "Duration, milliseconds" });
    fireEvent.change(input, { target: { value: raw } });
    expect(input).toHaveValue(displayed);
    expectNumericFieldError(input);
    expect(input).toBeRequired();
    expect(screen.getByText("Duration, milliseconds")).toHaveTextContent("*");
  });

  it.each([0, 31536000000])("accepts fixed Wait duration %s and clears its field error", (value) => {
    render(<NumericIntentSetup kind="wait" initialConfig={{ duration_ms: 1.5 }} />);
    const input = screen.getByRole("spinbutton", { name: "Duration, milliseconds" });
    expectNumericFieldError(input);
    fireEvent.change(input, { target: { value: String(value) } });
    expect(input).toHaveValue(value);
    expect(input).not.toHaveAttribute("aria-invalid", "true");
    expect(input).not.toHaveAttribute("aria-describedby");
  });

  it.each([undefined, true, "0"])("exposes invalid loaded fixed Wait type %s without fabricating a valid zero", (duration) => {
    render(<NumericIntentSetup kind="wait" initialConfig={duration === undefined ? {} : { duration_ms: duration }} />);
    const input = screen.getByRole("spinbutton", { name: "Duration, milliseconds" });
    expectNumericFieldError(input);
    if (duration === undefined || duration === true) expect(input).toHaveValue(null);
  });

  it.each([0, 1.5, -1])("keeps invalid Dataset row limit %s for local feedback", (value) => {
    render(<NumericIntentSetup kind="dataset_query" initialConfig={{ row_limit: 100, timeout_seconds: 30 }} />);
    const input = screen.getByRole("textbox", { name: "Row limit" });
    fireEvent.change(input, { target: { value: String(value) } });
    expect(input).toHaveValue(String(value));
    expectNumericFieldError(input);
    expect(input).not.toBeRequired();
  });

  it("leaves a blank Dataset row limit optional instead of replacing it with 100", () => {
    render(<NumericIntentSetup kind="dataset_query" initialConfig={{ row_limit: 100, timeout_seconds: 30 }} />);
    const input = screen.getByRole("textbox", { name: "Row limit" });
    fireEvent.change(input, { target: { value: "" } });
    expect(input).toHaveValue("");
    expect(input).not.toBeRequired();
    expect(input).not.toHaveAttribute("aria-invalid", "true");
  });

  it.each([
    { raw: "", displayed: null },
    { raw: "0", displayed: 0 },
    { raw: "-0.5", displayed: -0.5 },
  ])("retains invalid HTTP timeout $raw with required local feedback", ({ raw, displayed }) => {
    render(<NumericIntentSetup kind="http_request" initialConfig={{ timeout_seconds: 30 }} />);
    const input = screen.getByRole("spinbutton", { name: "Timeout seconds" });
    fireEvent.change(input, { target: { value: raw } });
    expect(input).toHaveValue(displayed);
    expectNumericFieldError(input);
    expect(input).toBeRequired();
    expect(screen.getByText("Timeout seconds")).toHaveTextContent("*");
  });

  it.each([0.01, 12.5])("accepts HTTP timeout %s without the old step/min restriction", (value) => {
    render(<NumericIntentSetup kind="http_request" initialConfig={{ timeout_seconds: 0 }} />);
    const input = screen.getByRole("spinbutton", { name: "Timeout seconds" });
    fireEvent.change(input, { target: { value: String(value) } });
    expect(input).toHaveValue(value);
    expect(input).not.toHaveAttribute("aria-invalid", "true");
    expect((input as HTMLInputElement).validity.valid).toBe(true);
  });

  it.each([
    { mode: "inline", value: 0 },
    { mode: "inline", value: 1.5 },
    { mode: "inline", value: 5242881 },
    { mode: "artifact", value: 209715201 },
  ])("retains invalid $mode HTTP byte limit $value", ({ mode, value }) => {
    render(<NumericIntentSetup kind="http_request" initialConfig={{ timeout_seconds: 30, response_mode: mode, max_response_bytes: 4096 }} />);
    const input = screen.getByRole("textbox", { name: "Max response bytes" });
    fireEvent.change(input, { target: { value: String(value) } });
    expect(input).toHaveValue(String(value));
    expectNumericFieldError(input);
    expect(input).not.toBeRequired();
  });

  it.each(["inline", "artifact"])("leaves a blank %s HTTP byte limit optional", (mode) => {
    render(<NumericIntentSetup kind="http_request" initialConfig={{ timeout_seconds: 30, response_mode: mode, max_response_bytes: 4096 }} />);
    const input = screen.getByRole("textbox", { name: "Max response bytes" });
    fireEvent.change(input, { target: { value: "" } });
    expect(input).toHaveValue("");
    expect(input).not.toBeRequired();
    expect(input).not.toHaveAttribute("aria-invalid", "true");
  });
});


// DEVHOOK03 exercises the existing dispatcher, never a missing future import.
// This is serialization/form coverage, not execution or persistence evidence.
const declarationFixture = {
  hook_id: "before_finish", contract_version: 2, display_name: "Before finishing", description: "Adjust the customer.",
  input_schema: { type: "object", properties: { customer: { type: "string", minLength: 1 } }, required: ["customer"], additionalProperties: false, $defs: { label: { type: "string" } } },
  input_mapping: { customer: "state.inputs.customer" }, mutable_fields: ["customer"],
  additional_outputs: { result: { schema: { type: ["string", "number", "boolean", "null"] }, optional: true } },
  stage_contract: { summary: "Before finishing the order", effects_before: ["order.read"], effects_after: ["order.finish"] },
};
function renderDeclarationSetup({ identityLocked = false, readOnly = false, declaration = declarationFixture, issues = [] }: {
  identityLocked?: boolean;
  readOnly?: boolean;
  declaration?: Record<string, unknown>;
  issues?: readonly { code?: string; path?: string; message?: string }[];
} = {}) {
  const onReplace = vi.fn();
  const onVariableInput = vi.fn(renderVariableInput);
  const onStepInputValidationChange = vi.fn();
  function Harness() {
    const [config, setConfig] = useState<Record<string, unknown>>({ hook: structuredClone(declaration) });
    return <WorkflowStepKindSmartSetup ctx={{
      stepKindDraft: "invoke_workflow", stepKindConfig: config,
      hookDeclarationIdentityLocked: identityLocked, hookDeclarationReadOnly: readOnly, hookDeclarationIssues: issues,
      onStepInputValidationChange,
      callableWorkflowOptions: [], selectedCallableWorkflow: {}, availableWorkflowVariableSuggestions: [],
      renderWorkflowVariableInput: onVariableInput, renderWorkflowContractMappingEditor: () => null,
      replaceStepKindConfigField: (kind: string, field: string, value: unknown) => { onReplace(kind, field, value); setConfig((current) => ({ ...current, [field]: value })); },
      updateStepKindConfig: vi.fn(),
    }} />;
  }
  return { ...render(<Harness />), onReplace, onVariableInput, onStepInputValidationChange };
}

it("edits the declaration contract through named fields without showing a direct child target", () => {
  const view = renderDeclarationSetup();
  expect(screen.getByRole("textbox", { name: "Hook ID" })).toHaveValue("before_finish");
  expect(screen.getByRole("textbox", { name: "Contract version" })).toHaveValue("2");
  expect(screen.getByRole("textbox", { name: "Hook name" })).toHaveValue("Before finishing");
  expect(screen.getByRole("textbox", { name: /^Hook description/ })).toHaveValue("Adjust the customer.");
  expect(screen.queryByRole("combobox", { name: "Workflow" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "Hook name" }), { target: { value: "Customer review" } });
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", { ...declarationFixture, display_name: "Customer review" });
});

it("edits a basic input's type/nullability visually while preserving schema constraints", () => {
  const view = renderDeclarationSetup();
  const input = within(screen.getByRole("group", { name: "Input customer" }));
  expect(input.getByRole("combobox", { name: "Type" })).toHaveTextContent(/^string$/);
  expect(input.getByRole("checkbox", { name: "Allow null" })).not.toBeChecked();
  fireEvent.click(input.getByRole("checkbox", { name: "Allow null" }));
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", { ...declarationFixture, input_schema: { ...declarationFixture.input_schema, properties: { customer: { type: ["string", "null"], minLength: 1 } } } });
});

it("changes a typed input source and mutability without replacing its JSON Schema constraints", () => {
  const view = renderDeclarationSetup();
  const input = within(screen.getByRole("group", { name: "Input customer" }));
  expect(input.getByRole("checkbox", { name: "Required" })).toBeChecked();
  fireEvent.click(input.getByRole("button", { name: "Schema details" }));
  expect(input.getByRole("textbox", { name: "Input schema" })).toHaveValue(JSON.stringify(declarationFixture.input_schema.properties.customer, null, 2));
  fireEvent.change(input.getByRole("textbox", { name: "Mapped from" }), { target: { value: "state.normalized.customer" } });
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", { ...declarationFixture, input_mapping: { customer: "state.normalized.customer" } });
  fireEvent.click(input.getByRole("checkbox", { name: "Allow override" }));
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", { ...declarationFixture, input_mapping: { customer: "state.normalized.customer" }, mutable_fields: [] });
});

it.each([
  { value: false, text: "false" }, { value: 0, text: "0" }, { value: "", text: '""' }, { value: null, text: "null" },
])("preserves an explicitly enabled output default $text by presence", ({ value, text }) => {
  const view = renderDeclarationSetup();
  const output = within(screen.getByRole("group", { name: "Output result" }));
  expect(output.getByRole("checkbox", { name: "Use default" })).not.toBeChecked();
  fireEvent.click(output.getByRole("checkbox", { name: "Use default" }));
  fireEvent.change(output.getByRole("textbox", { name: "Default value" }), { target: { value: text } });
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", { ...declarationFixture, additional_outputs: { result: { ...declarationFixture.additional_outputs.result, default: value } } });
  fireEvent.click(output.getByRole("checkbox", { name: "Use default" }));
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", declarationFixture);
  expect(declarationFixture.additional_outputs.result).not.toHaveProperty("default");
});

it("adds a named typed input using the backend-supported property name without imposing output ID rules", () => {
  const view = renderDeclarationSetup();
  fireEvent.click(screen.getByRole("button", { name: "Add input" }));
  const row = within(screen.getByRole("group", { name: "New input" }));
  fireEvent.change(row.getByRole("textbox", { name: "Input name" }), { target: { value: "display-name" } });
  const named = within(screen.getByRole("group", { name: "Input display-name" }));
  fireEvent.click(named.getByRole("button", { name: "Schema details" }));
  fireEvent.change(named.getByRole("textbox", { name: "Input schema" }), { target: { value: '{"type":["string","null"]}' } });
  fireEvent.change(named.getByRole("textbox", { name: "Mapped from" }), { target: { value: "state.inputs.display_name" } });
  fireEvent.click(named.getByRole("checkbox", { name: "Required" }));
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", { ...declarationFixture,
    input_schema: { ...declarationFixture.input_schema, properties: { ...declarationFixture.input_schema.properties, "display-name": { type: ["string", "null"] } }, required: ["customer", "display-name"] },
    input_mapping: { ...declarationFixture.input_mapping, "display-name": "state.inputs.display_name" },
  });
});

it("removes an input from schema, sources and writable fields as one draft change", () => {
  const view = renderDeclarationSetup();
  fireEvent.click(within(screen.getByRole("group", { name: "Input customer" })).getByRole("button", { name: "Remove input" }));
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", { ...declarationFixture, input_schema: { ...declarationFixture.input_schema, properties: {}, required: [] }, input_mapping: {}, mutable_fields: [] });
});

it("marks a required output without a default incomplete instead of inventing a value", () => {
  const view = renderDeclarationSetup();
  const output = within(screen.getByRole("group", { name: "Output result" }));
  fireEvent.click(output.getByRole("checkbox", { name: "Optional output" }));
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", { ...declarationFixture, additional_outputs: { result: { ...declarationFixture.additional_outputs.result, optional: false } } });
  expect(output.getByRole("checkbox", { name: "Use default" })).not.toBeChecked();
  expect(output.getByRole("alert")).toHaveTextContent(/default.*required|required.*default/i);
});

it("edits the stage summary and effects using only supported stage keys", () => {
  const view = renderDeclarationSetup();
  fireEvent.change(screen.getByRole("textbox", { name: "Stage summary" }), { target: { value: "After loading the order" } });
  fireEvent.change(screen.getByRole("textbox", { name: /^Effects before/ }), { target: { value: "order.read\ncustomer.read" } });
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", { ...declarationFixture, stage_contract: { ...declarationFixture.stage_contract, summary: "After loading the order", effects_before: ["order.read", "customer.read"] } });
});

it("keeps invalid output JSON visible without replacing the last parsed default", () => {
  const view = renderDeclarationSetup();
  const output = within(screen.getByRole("group", { name: "Output result" }));
  fireEvent.click(output.getByRole("checkbox", { name: "Use default" }));
  fireEvent.change(output.getByRole("textbox", { name: "Default value" }), { target: { value: "false" } });
  const count = view.onReplace.mock.calls.length;
  fireEvent.change(output.getByRole("textbox", { name: "Default value" }), { target: { value: "{" } });
  expect(output.getByRole("textbox", { name: "Default value" })).toHaveValue("{");
  expect(output.getByRole("alert")).toHaveTextContent(/valid JSON/i);
  expect(view.onReplace).toHaveBeenCalledTimes(count);
});

it("locks an existing Production identity while leaving its personal contract editable", () => {
  renderDeclarationSetup({ identityLocked: true });
  expect(screen.getByRole("textbox", { name: "Hook ID" })).toHaveAttribute("readonly");
  expect(screen.getByRole("textbox", { name: "Hook name" })).toBeEnabled();
  expect(screen.getByRole("textbox", { name: "Contract version" })).toBeEnabled();
});


// DEVHOOK03 numeric amendment: one mounted existing dispatcher/harness keeps the
// raw buffer separate from the last valid canonical number and reports errors
// through the existing step-input callback, not a new numeric primitive.
it("keeps invalid and blank Contract version drafts raw until a valid correction", async () => {
  const view = renderDeclarationSetup();
  const input = screen.getByRole("textbox", { name: "Contract version" });
  expect(input).toHaveAttribute("type", "text");
  expect(input).toHaveAttribute("inputmode", "numeric");
  expect(input).toBeRequired();
  expect(input).toHaveValue("2");
  view.onReplace.mockClear();
  view.onStepInputValidationChange.mockClear();
  const lastVersionError = () => view.onStepInputValidationChange.mock.calls.filter(([path]) => path === "hook.contract_version").at(-1)?.[1];
  for (const raw of ["1e", ""]) {
    fireEvent.change(input, { target: { value: raw } });
    expect(input).toHaveValue(raw);
    expectNumericFieldError(input);
    await waitFor(() => expect(lastVersionError()).toEqual(expect.stringMatching(/\S/)));
    expect(view.onReplace).not.toHaveBeenCalled();
  }
  fireEvent.change(input, { target: { value: "3" } });
  expect(input).toHaveValue("3");
  expect(input).not.toHaveAttribute("aria-invalid", "true");
  expect(input).not.toHaveAttribute("aria-describedby");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await waitFor(() => expect(lastVersionError()).toBe(""));
  expect(view.onReplace).toHaveBeenCalledTimes(1);
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", { ...declarationFixture, contract_version: 3 });
});


// DH-S1/S2: exercise the real Hook declaration editor through Smart Setup.
// Backend-shaped diagnostics are inputs, not a parser or a replacement router.
it("omits a cleared optional source while retaining its schema and override permission", () => {
  const optionalDeclaration = {
    ...declarationFixture,
    input_schema: { ...declarationFixture.input_schema, required: [] },
  };
  const view = renderDeclarationSetup({ declaration: optionalDeclaration });
  const input = within(screen.getByRole("group", { name: "Input customer" }));
  expect(input.getByRole("checkbox", { name: "Required" })).not.toBeChecked();
  expect(input.getByRole("textbox", { name: "Mapped from" })).toHaveValue("state.inputs.customer");
  fireEvent.change(input.getByRole("textbox", { name: "Mapped from" }), { target: { value: "" } });
  expect(view.onReplace).toHaveBeenLastCalledWith("invoke_workflow", "hook", {
    ...optionalDeclaration,
    input_mapping: {},
  });
  expect(input.getByRole("checkbox", { name: "Allow override" })).toBeChecked();
  fireEvent.click(input.getByRole("button", { name: "Schema details" }));
  expect(input.getByRole("textbox", { name: "Input schema" })).toHaveValue('{\n  "type": "string",\n  "minLength": 1\n}');
  expect(input.getByRole("textbox", { name: "Mapped from" })).toHaveValue("");
});

it.each([
  {
    field: "required input source",
    declaration: { ...declarationFixture, input_mapping: {} },
    issue: {
      code: "workflow.hook.input_mapping_missing",
      path: "steps.hook_step.override.invoke_workflow.hook.input_mapping.customer",
      message: "The required input 'customer' has no source in the author state",
    },
    group: "Input customer",
    control: "Mapped from",
    expandSchema: false,
  },
  {
    field: "output schema",
    declaration: {
      ...declarationFixture,
      additional_outputs: { result: { schema: { type: 7 }, optional: true } },
    },
    issue: {
      code: "workflow.hook.output_schema_invalid",
      path: "steps.hook_step.override.invoke_workflow.hook.additional_outputs.result.schema",
      message: "Invalid JSON Schema: 7 is not valid under any of the given schemas",
    },
    group: "Output result",
    control: "Output schema",
    expandSchema: true,
  },
  {
    field: "output default",
    declaration: {
      ...declarationFixture,
      additional_outputs: { result: { schema: { type: "boolean" }, optional: true, default: "wrong" } },
    },
    issue: {
      code: "workflow.hook.output_default_invalid",
      path: "steps.hook_step.override.invoke_workflow.hook.additional_outputs.result.default",
      message: "The default of 'result' does not match its schema: 'wrong' is not of type 'boolean'",
    },
    group: "Output result",
    control: "Default value",
    expandSchema: false,
  },
  {
    field: "stage summary",
    declaration: { ...declarationFixture, stage_contract: { ...declarationFixture.stage_contract, summary: "" } },
    issue: {
      code: "workflow.hook.stage_contract_invalid",
      path: "steps.hook_step.override.invoke_workflow.hook.stage_contract",
      message: "stage_contract states where the hook runs (summary) and the effects before and after it",
    },
    group: "",
    control: "Stage summary",
    expandSchema: false,
  },
])("associates the canonical $field issue with its visible input", ({ declaration, issue, group, control, expandSchema }) => {
  renderDeclarationSetup({ declaration, issues: [issue] });
  const row = group ? within(screen.getByRole("group", { name: group })) : screen;
  if (expandSchema) fireEvent.click(row.getByRole("button", { name: "Schema details" }));
  const input = row.getByRole("textbox", { name: control });
  expect(input).toHaveAttribute("aria-invalid", "true");
  expect(input).toHaveAttribute("aria-describedby");
  const description = new RegExp(issue.message.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  expect(input).toHaveAccessibleDescription(description);
  expect(screen.getByRole("textbox", { name: "Hook name" })).not.toHaveAttribute("aria-invalid", "true");
  expect(screen.getByRole("textbox", { name: "Hook name" })).not.toHaveAttribute("aria-describedby");
});


it("keeps mapped-source selector panel identities distinct and stable across edits and rename", () => {
  const declaration = {
    ...declarationFixture,
    input_schema: { ...declarationFixture.input_schema, properties: {
      ...declarationFixture.input_schema.properties, order: { type: "string" },
    } },
    input_mapping: { ...declarationFixture.input_mapping, order: "state.inputs.order" },
  };
  const view = renderDeclarationSetup({ declaration });
  const latestInputs = () => view.onVariableInput.mock.calls.slice(-2).map(([props]) => props);
  const before = latestInputs();
  expect(before.map((props) => props.placeholder)).toEqual(["state.inputs.customer", "state.inputs.order"]);
  expect(before).toHaveLength(2);
  for (const props of before) expect(props.panelId).toEqual(expect.stringMatching(/\S/));
  expect(new Set(before.map((props) => props.panelId)).size).toBe(2);
  const input = within(screen.getByRole("group", { name: "Input customer" }));
  fireEvent.change(input.getByRole("textbox", { name: "Mapped from" }), { target: { value: "state.customer" } });
  expect(latestInputs().map((props) => props.panelId)).toEqual(before.map((props) => props.panelId));
  fireEvent.change(input.getByRole("textbox", { name: "Input name" }), { target: { value: "buyer" } });
  expect(latestInputs().map((props) => props.panelId)).toEqual(before.map((props) => props.panelId));
  expect(latestInputs().map((props) => props.placeholder)).toEqual(["state.inputs.buyer", "state.inputs.order"]);
});

// DEVHOOK field fidelity: real DS markers and existing variable-input ARIA port.
it("includes the separate shared optional marker in all three Hook field names", () => {
  renderDeclarationSetup();
  for (const name of ["Hook description", "Effects before", "Effects after"]) {
    const label = screen.getByText(name, { selector: ".fs-field-label" });
    expect(within(label).getByText("(optional)")).toHaveClass("fs-field-optional");
    expect(within(label).getByText("(optional)")).not.toHaveAttribute("aria-hidden");
    const input = screen.getByRole("textbox", { name: `${name} (optional)` });
    expect(input).not.toHaveAttribute("aria-label");
    expect(input).not.toBeRequired();
  }
});

it("updates mapped-source required semantics without losing its canonical error association", () => {
  const message = "The author input source is missing";
  renderDeclarationSetup({
    declaration: { ...declarationFixture, input_mapping: {} },
    issues: [{ path: "steps.hook_step.override.invoke_workflow.hook.input_mapping.customer", message }],
  });
  const input = within(screen.getByRole("group", { name: "Input customer" }));
  const source = input.getByRole("textbox", { name: "Mapped from" });
  expect(source).toBeRequired();
  expect(source).toHaveAttribute("aria-invalid", "true");
  expect(source).toHaveAccessibleDescription(new RegExp(message));
  fireEvent.click(input.getByRole("checkbox", { name: "Required" }));
  expect(source).not.toBeRequired();
  expect(source).toHaveAttribute("aria-invalid", "true");
  expect(source).toHaveAccessibleDescription(message);
  expect(source).toHaveValue("");
  fireEvent.click(input.getByRole("checkbox", { name: "Required" }));
  expect(source).toBeRequired();
  expect(source).toHaveAccessibleDescription(new RegExp(message));
});


// Dataset timeout repair source: these use the real public setup component and
// its established callback port. They do not claim a full page Save/readback.
describe("Dataset timeout draft preservation", () => {
  it.each([
    { raw: "", expected: "" },
    { raw: "0", expected: 0 },
    { raw: "1.5", expected: 1.5 },
    { raw: "1e2", expected: 100 },
  ])("keeps an edited numeric draft $raw without parseInt/default rewriting", ({ raw, expected }) => {
    const { replaceStepKindConfigField } = renderDatasetSetup({
      query_template_id: "query-1", params: {}, timeout_seconds: 45,
    });
    fireEvent.change(screen.getByLabelText("Timeout seconds"), { target: { value: raw } });
    expect(replaceStepKindConfigField).toHaveBeenLastCalledWith(
      "dataset_query", "timeout_seconds", expected,
    );
  });

  it("keeps a cleared timeout blank after the actual controlled draft rerender", () => {
    render(<NumericIntentSetup kind="dataset_query" initialConfig={{
      query_template_id: "query-1", row_limit: 100, timeout_seconds: 45,
    }} />);
    fireEvent.change(screen.getByLabelText("Timeout seconds"), { target: { value: "" } });
    expect(screen.getByLabelText("Timeout seconds")).toHaveDisplayValue("");
  });

  it.each([{}, { timeout_seconds: null }, { timeout_seconds: false }, { timeout_seconds: "abc" }])(
    "does not write an imported timeout merely by opening its editor: %j",
    (stored) => {
      const { replaceStepKindConfigField } = renderDatasetSetup({ query_template_id: "query-1", ...stored });
      expect(screen.getByLabelText("Timeout seconds")).toBeVisible();
      expect(replaceStepKindConfigField).not.toHaveBeenCalled();
    },
  );
});


// Exercise the real renderer's owner identity, not a test-computed editor key.
function DeclarationOwnerHarness({ projectId = "project-a", workflowId = "workflow-a", stepId = "step-a", override }: Readonly<{
  projectId?: string; workflowId?: string; stepId?: string; override?: string;
}>) {
  const [raw, setRaw] = useState(() => JSON.stringify({ invoke_workflow: { hook: declarationFixture } }));
  const current = override ?? raw;
  const config = JSON.parse(current) as { invoke_workflow: Record<string, unknown> };
  const view = useWorkflowStepRendererController({
    stepKindDraft: "invoke_workflow", stepKindConfig: config.invoke_workflow, stepOverrideDraft: current,
    selectedStep: { runtimeId: stepId }, selectedProjectId: projectId, workflowId,
    setStepOverrideDraft: setRaw, renderWorkflowVariableInput: renderVariableInput,
  });
  return view.renderStepKindSmartSetup();
}

it.each([
  { owner: "project", next: { projectId: "project-b" } },
  { owner: "workflow", next: { workflowId: "workflow-b" } },
  { owner: "step", next: { stepId: "step-b" } },
])("clears invalid Hook drafts on a same-shaped $owner switch while preserving unrelated edits", ({ next }) => {
  const view = render(<DeclarationOwnerHarness />);
  const input = within(screen.getByRole("group", { name: "Input customer" }));
  fireEvent.click(input.getByRole("button", { name: "Schema details" }));
  const schema = input.getByRole("textbox", { name: "Input schema" });
  fireEvent.change(schema, { target: { value: "{" } });
  const version = screen.getByRole("textbox", { name: "Contract version" });
  fireEvent.change(version, { target: { value: "1e" } });
  const output = within(screen.getByRole("group", { name: "Output result" }));
  fireEvent.click(output.getByRole("checkbox", { name: "Use default" }));
  const defaultValue = output.getByRole("textbox", { name: "Default value" });
  fireEvent.change(defaultValue, { target: { value: "{" } });
  fireEvent.change(screen.getByRole("textbox", { name: /^Effects before/ }), { target: { value: "order.read\ncustomer.read\n" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Hook name" }), { target: { value: "Updated name" } });
  expect(input.getByRole("textbox", { name: "Input schema" })).toBe(schema);
  expect(schema).toHaveValue("{");
  expect(schema).toHaveAttribute("aria-invalid", "true");
  expect(version).toHaveValue("1e");
  expect(defaultValue).toHaveValue("{");
  expect(defaultValue).toHaveAttribute("aria-invalid", "true");
  expect(screen.getByRole("textbox", { name: /^Effects before/ })).toHaveValue("order.read\ncustomer.read\n");
  view.rerender(<DeclarationOwnerHarness {...next} />);
  expect(screen.getByRole("textbox", { name: "Contract version" })).toHaveValue("2");
  expect(screen.getByRole("textbox", { name: "Contract version" })).not.toHaveAttribute("aria-invalid", "true");
  expect(screen.queryByRole("textbox", { name: "Input schema" })).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Default value" })).not.toBeInTheDocument();
  expect(within(screen.getByRole("group", { name: "Output result" })).getByRole("checkbox", { name: "Use default" })).not.toBeChecked();
  expect(screen.getByRole("textbox", { name: /^Effects before/ })).toHaveValue("order.read\ncustomer.read");
});

it("retains a renamed row's invalid schema without giving a reintroduced canonical name its identity", () => {
  const view = render(<DeclarationOwnerHarness />);
  const original = screen.getByRole("group", { name: "Input customer" });
  fireEvent.click(within(original).getByRole("button", { name: "Schema details" }));
  const schema = within(original).getByRole("textbox", { name: "Input schema" });
  fireEvent.change(schema, { target: { value: "{" } });
  fireEvent.change(within(original).getByRole("textbox", { name: "Input name" }), { target: { value: "buyer" } });
  expect(screen.getByRole("group", { name: "Input buyer" })).toBe(original);
  expect(schema).toHaveValue("{");
  const declaration = {
    ...declarationFixture,
    input_schema: { ...declarationFixture.input_schema, properties: {
      buyer: { type: "string", minLength: 1 }, customer: { type: "boolean" },
    }, required: ["buyer"] },
    input_mapping: { buyer: "state.inputs.customer" }, mutable_fields: ["buyer"],
  };
  view.rerender(<DeclarationOwnerHarness override={JSON.stringify({ invoke_workflow: { hook: declaration } })} />);
  expect(screen.getByRole("group", { name: "Input buyer" })).toBe(original);
  expect(within(original).getByRole("textbox", { name: "Input schema" })).toBe(schema);
  expect(schema).toHaveValue("{");
  const reintroduced = screen.getByRole("group", { name: "Input customer" });
  expect(reintroduced).not.toBe(original);
  expect(within(reintroduced).getByRole("textbox", { name: "Input name" })).toHaveValue("customer");
  expect(within(reintroduced).getByRole("combobox", { name: "Type" })).toHaveTextContent(/^boolean$/);
  expect(within(reintroduced).queryByRole("textbox", { name: "Input schema" })).not.toBeInTheDocument();
});

it("resets an invalid JSON buffer on an external schema change and does not revive it when the schema returns", () => {
  const view = render(<DeclarationOwnerHarness />);
  const input = within(screen.getByRole("group", { name: "Input customer" }));
  fireEvent.click(input.getByRole("button", { name: "Schema details" }));
  const schema = input.getByRole("textbox", { name: "Input schema" });
  fireEvent.change(schema, { target: { value: "{" } });
  const declaration = { ...declarationFixture, input_schema: {
    ...declarationFixture.input_schema, properties: { customer: { type: "number" } },
  } };
  view.rerender(<DeclarationOwnerHarness override={JSON.stringify({ invoke_workflow: { hook: declaration } })} />);
  expect(input.getByRole("textbox", { name: "Input schema" })).toBe(schema);
  expect(schema).toHaveValue('{\n  "type": "number"\n}');
  expect(schema).not.toHaveAttribute("aria-invalid", "true");
  view.rerender(<DeclarationOwnerHarness />);
  expect(schema).toHaveValue('{\n  "type": "string",\n  "minLength": 1\n}');
  expect(schema).not.toHaveAttribute("aria-invalid", "true");
});


it("exposes the required Saved query while preserving controlled selection through clear, choose and rerender", async () => {
  const user = userEvent.setup();
  const onReplace = vi.fn();
  const initialConfig = { query_template_id: "query-1", row_limit: 7, timeout_seconds: 0, result_target: "state.rows" };
  const view = render(<NumericIntentSetup kind="dataset_query" initialConfig={initialConfig} onReplace={onReplace} />);
  const select = screen.getByRole("combobox", { name: "Saved query" });
  expect(select).toHaveAccessibleName("Saved query");
  const requiredAttribute = select.getAttribute("aria-required");
  expect(requiredAttribute).toBe("true");
  const caption = screen.getByText("Saved query", { selector: ".fs-required-label" });
  expect(within(caption).getByText("*")).toBeVisible();
  expect(select).toHaveTextContent("Projects");

  // Required semantics do not add a new veto on incomplete authoring drafts.
  await user.click(select);
  await user.click(screen.getByRole("option", { name: "Choose saved query" }));
  expect(select).toHaveTextContent("Choose saved query");
  await user.click(select);
  await user.click(screen.getByRole("option", { name: "Projects" }));
  expect(select).toHaveTextContent("Projects");
  expect(onReplace.mock.calls).toEqual([
    ["dataset_query", "query_template_id", ""],
    ["dataset_query", "query_template_id", "query-1"],
  ]);

  view.rerender(<NumericIntentSetup kind="dataset_query" initialConfig={initialConfig} onReplace={onReplace} />);
  expect(screen.getByRole("combobox", { name: "Saved query" })).toHaveTextContent("Projects");
  expect(screen.getByRole("textbox", { name: "Result target" })).toHaveValue("state.rows");
  expect(screen.getByRole("textbox", { name: "Row limit" })).toHaveValue("7");
  expect(screen.getByRole("textbox", { name: "Timeout seconds" })).toHaveValue("0");
  expect(onReplace).toHaveBeenCalledTimes(2);
  expect(initialConfig).toEqual({ query_template_id: "query-1", row_limit: 7, timeout_seconds: 0, result_target: "state.rows" });
});


// "Return from workflow" through the real renderer: the step override is the only store.
function ReturnedFileHarness({ initial, returnedFiles = [], onDraft }: Readonly<{
  initial: Record<string, unknown>;
  returnedFiles?: Array<Record<string, unknown>>;
  onDraft: (override: Record<string, unknown>) => void;
}>) {
  const [raw, setRaw] = useState(() => JSON.stringify(initial));
  const config = JSON.parse(raw) as Record<string, Record<string, unknown>>;
  const view = useWorkflowStepRendererController({
    stepKindDraft: "file_from_base64", stepKindConfig: config.file_from_base64, stepOverrideDraft: raw,
    selectedStep: { runtimeId: "render_invoice" }, selectedProjectId: "project-a", workflowId: "workflow-a",
    workflowInterfaceReturnedFiles: returnedFiles, workflowInterfaceOutputsDraft: [{ name: "invoice_pdf" }],
    setStepOverrideDraft: (next: string | ((current: string) => string)) => setRaw((current) => {
      const value = typeof next === "function" ? next(current) : next;
      onDraft(JSON.parse(value) as Record<string, unknown>);
      return value;
    }),
    renderWorkflowVariableInput: renderVariableInput,
  });
  return view.renderStepKindSmartSetup();
}

describe("Return from workflow on a step's file result", () => {
  const saveFile = { file_from_base64: { source: "state.inputs.pdf", result_target: "state.invoice_pdf" } };

  it("is off by default and, ticked, saves a stable key that avoids other outputs", () => {
    const onDraft = vi.fn();
    render(<ReturnedFileHarness initial={saveFile} onDraft={onDraft} />);
    const box = screen.getByRole("checkbox", { name: "Return from workflow" });
    expect(box).not.toBeChecked();
    expect(box).toHaveAccessibleDescription("Make the result available to the calling workflow and in the run results");
    fireEvent.click(box);
    expect(screen.getByRole("checkbox", { name: "Return from workflow" })).toBeChecked();
    // "invoice_pdf" is already an output name: the new key does not repeat it.
    expect(onDraft).toHaveBeenLastCalledWith(expect.objectContaining({
      ...saveFile, returned_files: [{ result: "file", key: "invoice_pdf_2" }],
    }));
    expect(screen.getByText("Output: invoice_pdf_2")).toBeVisible();
  });

  it("unticked, removes the result from what the step returns", () => {
    const onDraft = vi.fn();
    render(<ReturnedFileHarness initial={{ ...saveFile, returned_files: [{ result: "file", key: "invoice" }] }} onDraft={onDraft} />);
    const box = screen.getByRole("checkbox", { name: "Return from workflow" });
    expect(box).toBeChecked();
    fireEvent.click(box);
    expect(onDraft).toHaveBeenLastCalledWith(expect.objectContaining({ returned_files: [] }));
  });

  it("shows a result linked from an older declaration as ticked and keeps its key on the first change", () => {
    const onDraft = vi.fn();
    const legacy = [{ name: "invoice", type: "file", required: true, origin: "legacy", source: { step_id: "render_invoice", step_name: "Render", result: "file", target: "state.invoice_pdf" } }];
    render(<ReturnedFileHarness initial={saveFile} returnedFiles={legacy} onDraft={onDraft} />);
    expect(screen.getByRole("checkbox", { name: "Return from workflow" })).toBeChecked();
    expect(screen.getByText("Output: invoice")).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox", { name: "Return from workflow" }));
    expect(onDraft).toHaveBeenLastCalledWith(expect.objectContaining({ returned_files: [] }));
  });

  it("is not offered for a step that saves no file", () => {
    render(<ReturnedFileHarness initial={{ file_from_base64: { source: "state.inputs.pdf" } }} onDraft={vi.fn()} />);
    expect(screen.queryByRole("checkbox", { name: "Return from workflow" })).not.toBeInTheDocument();
  });
});


function renderSetAssignments(initial: Record<string, unknown>) {
  function Harness() {
    const [config, setConfig] = useState<Record<string, unknown>>(initial);
    return <>
      <WorkflowStepKindSmartSetup ctx={{
        stepKindDraft: "set_variable", stepKindConfig: config,
        renderWorkflowVariableInput: renderVariableInput,
        renderWorkflowDestinationInput: renderVariableInput,
        replaceStepKindConfigField: (_kind: string, field: string, value: unknown) =>
          setConfig((current) => ({ ...current, [field]: value })),
        updateStepKindConfig: (_kind: string, patch: Record<string, unknown>) =>
          setConfig((current) => JSON.parse(JSON.stringify({ ...current, ...patch })) as Record<string, unknown>),
      }} />
      <output data-testid="set-wire">{JSON.stringify(config)}</output>
    </>;
  }
  return render(<Harness />);
}

it("uses typed Set variable rows and preserves untouched literals while explicitly choosing a previous value", () => {
  const assignments = { "state.note": "true", "state.zero": 0, "state.false": false, "state.none": null,
    "state.object": { source: "state.inputs.name", extra: 0 }, "state.copy": { source: "state.inputs.old" } };
  renderSetAssignments({ assignments, output_types: { "state.note": "text" } });
  expect(screen.getByRole("textbox", { name: "Assignment 1 value" })).toHaveValue("true");
  expect(screen.getByRole("combobox", { name: "Assignment 1 value mode" })).toHaveValue("literal");
  expect(screen.getByRole("combobox", { name: "Assignment 6 value mode" })).toHaveValue("source");
  fireEvent.change(screen.getByRole("combobox", { name: "Assignment 1 value mode" }), { target: { value: "source" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Assignment 1 previous value" }), { target: { value: "state.inputs.customer.name" } });
  const wire = JSON.parse(screen.getByTestId("set-wire").textContent || "{}") as Record<string, unknown>;
  expect(wire).toEqual({ assignments: { ...assignments, "state.note": { source: "state.inputs.customer.name" } },
    output_types: { "state.note": "text" } });
  fireEvent.click(screen.getByRole("button", { name: "Add assignment" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Assignment 7 target" }), { target: { value: "state.added" } });
  fireEvent.change(screen.getByRole("combobox", { name: "Assignment 7 literal type" }), { target: { value: "number" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Assignment 7 value" }), { target: { value: "0" } });
  expect(JSON.parse(screen.getByTestId("set-wire").textContent || "{}")).toMatchObject({ assignments: { "state.added": 0 } });
});

it("keeps legacy Set variable target/value presence, including null, while another field changes", () => {
  renderSetAssignments({ target: "state.legacy", value: null, output_types: { "state.legacy": "json" } });
  expect(screen.getByRole("textbox", { name: "Assignment 1 target" })).toHaveValue("state.legacy");
  expect(JSON.parse(screen.getByTestId("set-wire").textContent || "{}")).toEqual({
    target: "state.legacy", value: null, output_types: { "state.legacy": "json" },
  });
});


it("removes a final legacy Set assignment without retaining target or value", () => {
  renderSetAssignments({ target: "state.legacy", value: null, output_types: { retained: "json" } });
  fireEvent.click(screen.getByRole("button", { name: "Remove assignment" }));
  expect(JSON.parse(screen.getByTestId("set-wire").textContent || "{}")).toEqual({
    assignments: {}, output_types: { retained: "json" },
  });
  expect(screen.queryByRole("textbox", { name: "Assignment 1 target" })).not.toBeInTheDocument();
});

it("can add a Set row and remove all rows from a legacy assignment", () => {
  renderSetAssignments({ target: "state.legacy", value: 0 });
  fireEvent.click(screen.getByRole("button", { name: "Add assignment" }));
  fireEvent.click(screen.getAllByRole("button", { name: "Remove assignment" })[0]);
  const last = screen.getByRole("button", { name: "Remove assignment" });
  expect(last).toBeEnabled();
  fireEvent.click(last);
  expect(JSON.parse(screen.getByTestId("set-wire").textContent || "{}")).toEqual({ assignments: {} });
});

it("keeps an untouched assignment key exactly when a neighboring value changes", () => {
  renderSetAssignments({ assignments: { " state.keep ": false, "state.edit": "before" } });
  fireEvent.change(screen.getByRole("textbox", { name: "Assignment 2 value" }), { target: { value: "after" } });
  expect(JSON.parse(screen.getByTestId("set-wire").textContent || "{}")).toEqual({
    assignments: { " state.keep ": false, "state.edit": "after" },
  });
});

it.each([null, 7, " state.keep "])("keeps the legacy raw target %s on a value-only edit", (target) => {
  renderSetAssignments({ target, value: 0 });
  fireEvent.change(screen.getByRole("textbox", { name: "Assignment 1 value" }), { target: { value: "2" } });
  expect(JSON.parse(screen.getByTestId("set-wire").textContent || "{}")).toEqual({ target, value: 2 });
});

it("keeps a single retained Set target and wire while its edited destination is blank", () => {
  renderSetAssignments({ assignments: { "state.keep": "hello" }, opaque: true });
  fireEvent.change(screen.getByRole("textbox", { name: "Assignment 1 target" }), { target: { value: "   " } });
  expect(screen.getByRole("alert")).toHaveTextContent("Give each assignment a target.");
  expect(JSON.parse(screen.getByTestId("set-wire").textContent || "{}")).toEqual({
    assignments: { "state.keep": "hello" }, opaque: true,
  });
  fireEvent.change(screen.getByRole("textbox", { name: "Assignment 1 target" }), { target: { value: "state.fixed" } });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(JSON.parse(screen.getByTestId("set-wire").textContent || "{}")).toEqual({
    assignments: { "state.fixed": "hello" }, opaque: true,
  });
});

it("blocks an explicit literal JSON edit that would execute as a source copy", () => {
  const original = { source: "other", extra: 1 };
  renderSetAssignments({ assignments: { "state.value": original } });
  fireEvent.change(screen.getByRole("textbox", { name: "Assignment 1 value" }),
    { target: { value: '{"source":"state.inputs.customer"}' } });
  expect(screen.getByRole("alert")).toHaveTextContent("Use Previous value");
  expect(JSON.parse(screen.getByTestId("set-wire").textContent || "{}")).toEqual({
    assignments: { "state.value": original },
  });
});

it.each([
  { target: "state.legacy", value: null },
  { target: "state.zero", value: 0 },
  { target: "state.false", value: false },
  { target: "state.empty" },
])("keeps legacy Set variable property presence and $value during step hydration", (config) => {
  const override = { set_variable: { ...config, output_types: { retained: "json" } } };
  expect(editableOverrideForRawStep({ step_kind: "set_variable", override })).toEqual(override);
});


it("edits Approval reviewer outputs as ordered picker rows without rewriting custom neighbours", () => {
  function Harness() {
    const [config, setConfig] = useState<Record<string, unknown>>({
      message: "Review the text", show_outputs: ["state.custom.legacy", "state.outputs.text", "state.custom.legacy"],
    });
    return <>
      <WorkflowStepKindSmartSetup ctx={{ stepKindDraft: "approval", stepKindConfig: config,
        renderWorkflowVariableInput: renderVariableInput,
        replaceStepKindConfigField: (_kind: string, field: string, value: unknown) =>
          setConfig((current) => ({ ...current, [field]: value })),
      }} />
      <output data-testid="approval-wire">{JSON.stringify(config)}</output>
    </>;
  }
  render(<Harness />);
  expect(screen.getByRole("textbox", { name: "Reviewer output 1" })).toHaveValue("state.custom.legacy");
  expect(screen.getByRole("textbox", { name: "Reviewer output 2" })).toHaveValue("state.outputs.text");
  fireEvent.click(screen.getByRole("button", { name: "Add reviewer output" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Reviewer output 4" }), { target: { value: "state.inputs.customer.name" } });
  expect(JSON.parse(screen.getByTestId("approval-wire").textContent || "{}")).toEqual({
    message: "Review the text", show_outputs: ["state.custom.legacy", "state.outputs.text", "state.custom.legacy", "state.inputs.customer.name"],
  });
  fireEvent.click(screen.getByRole("button", { name: "Remove reviewer output 2" }));
  expect(JSON.parse(screen.getByTestId("approval-wire").textContent || "{}")).toEqual({
    message: "Review the text", show_outputs: ["state.custom.legacy", "state.custom.legacy", "state.inputs.customer.name"],
  });
});


it("retains untouched whitespace reviewer outputs when another Approval row is added", () => {
  function Harness() {
    const [config, setConfig] = useState<Record<string, unknown>>({ show_outputs: [" ", "state.keep"], opaque: true });
    return <>
      <WorkflowStepKindSmartSetup ctx={{ stepKindDraft: "approval", stepKindConfig: config,
        renderWorkflowVariableInput: renderVariableInput,
        replaceStepKindConfigField: (_kind: string, field: string, value: unknown) =>
          setConfig((current) => ({ ...current, [field]: value })),
      }} />
      <output data-testid="approval-whitespace-wire">{JSON.stringify(config)}</output>
    </>;
  }
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Add reviewer output" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Reviewer output 3" }),
    { target: { value: "state.inputs.customer.name" } });
  expect(JSON.parse(screen.getByTestId("approval-whitespace-wire").textContent || "{}")).toEqual({
    show_outputs: [" ", "state.keep", "state.inputs.customer.name"], opaque: true,
  });
});

it("keeps Set and Approval row inputs mounted through unrelated parent rerenders", () => {
  function Harness() {
    const [tick, setTick] = useState(0);
    return <>
      <button onClick={() => setTick((value) => value + 1)} type="button">Rerender {tick}</button>
      <WorkflowStepKindSmartSetup ctx={{ stepKindDraft: "set_variable",
        stepKindConfig: { assignments: { "state.note": "hello" } },
        renderWorkflowVariableInput: renderVariableInput, renderWorkflowDestinationInput: renderVariableInput,
        replaceStepKindConfigField: vi.fn(),
      }} />
      <WorkflowStepKindSmartSetup ctx={{ stepKindDraft: "approval",
        stepKindConfig: { message: "Review", show_outputs: ["state.note"] },
        renderWorkflowVariableInput: renderVariableInput, replaceStepKindConfigField: vi.fn(),
      }} />
    </>;
  }
  render(<Harness />);
  const setValue = screen.getByRole("textbox", { name: "Assignment 1 value" });
  const approval = screen.getByRole("textbox", { name: "Reviewer output 1" });
  fireEvent.click(screen.getByRole("button", { name: "Rerender 0" }));
  expect(screen.getByRole("textbox", { name: "Assignment 1 value" })).toBe(setValue);
  expect(screen.getByRole("textbox", { name: "Reviewer output 1" })).toBe(approval);
});

it("keeps a target-only legacy Set row mounted and leaves value absent when target changes", () => {
  function Harness() {
    const [config, setConfig] = useState<Record<string, unknown>>({ target: "state.old" });
    return <>
      <WorkflowStepKindSmartSetup ctx={{ stepKindDraft: "set_variable", stepKindConfig: config,
        renderWorkflowVariableInput: renderVariableInput, renderWorkflowDestinationInput: renderVariableInput,
        replaceStepKindConfigField: (_kind: string, field: string, value: unknown) =>
          setConfig((current) => ({ ...current, [field]: value })),
      }} />
      <output data-testid="target-only-wire">{JSON.stringify(config)}</output>
    </>;
  }
  render(<Harness />);
  const target = screen.getByRole("textbox", { name: "Assignment 1 target" });
  fireEvent.change(target, { target: { value: "state.new" } });
  expect(screen.getByRole("textbox", { name: "Assignment 1 target" })).toBe(target);
  expect(JSON.parse(screen.getByTestId("target-only-wire").textContent || "{}")).toEqual({ target: "state.new" });
});


function SetOwnerHarness({ projectId = "project-a", workflowId = "workflow-a" }: Readonly<{ projectId?: string; workflowId?: string }>) {
  const raw = JSON.stringify({ set_variable: { assignments: { "state.note": "hello" } } });
  const view = useWorkflowStepRendererController({
    stepKindDraft: "set_variable", stepOverrideDraft: raw,
    stepKindConfig: { assignments: { "state.note": "hello" } },
    selectedStep: { runtimeId: "shared-step" }, selectedProjectId: projectId, workflowId,
    setStepOverrideDraft: vi.fn(), renderWorkflowVariableInput: renderVariableInput,
    renderWorkflowDestinationInput: renderVariableInput,
  });
  return view.renderStepKindSmartSetup();
}

it.each([
  { projectId: "project-b", workflowId: "workflow-a" },
  { projectId: "project-a", workflowId: "hook-child-b" },
])("resets a Set variable's invalid local mode when its actual authoring owner changes to $projectId/$workflowId", (next) => {
  const view = render(<SetOwnerHarness />);
  fireEvent.change(screen.getByRole("combobox", { name: "Assignment 1 value mode" }), { target: { value: "source" } });
  expect(screen.getByRole("combobox", { name: "Assignment 1 value mode" })).toHaveValue("source");
  view.rerender(<SetOwnerHarness {...next} />);
  expect(screen.getByRole("combobox", { name: "Assignment 1 value mode" })).toHaveValue("literal");
});
