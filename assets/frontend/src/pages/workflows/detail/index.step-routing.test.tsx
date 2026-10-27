import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";

import {
  canvasGraphFixture,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowValidation,
  postedChangeSets,
  renderPage,
} from "./index.test-helpers";

function lastPatch(): Record<string, unknown> {
  const sets = postedChangeSets();
  const operations = (sets.at(-1)?.operations || []) as Array<Record<string, unknown>>;
  expect(operations).toHaveLength(1);
  return (operations[0].patch || {}) as Record<string, unknown>;
}

describe("WorkflowsDetailPage step routing and loops", () => {
  it("keeps a supported structured blank selector editable without inventing a selector", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "weekly_report", enabled: false, revision: 1,
      steps: [{
        id: "11111111-1111-1111-1111-111111111021", step_id: "check", display_name: "Incomplete condition", step_kind: "if",
        override: { if: { condition: { operator: "equals", selector: "", value: "" } } },
      }], groups: [],
    } as never);
    renderPage({ initialEntry: "/registry/workflows/weekly_report/builder?drawer=step&step_id=check" });
    expect(await screen.findByRole("heading", { name: "Step editor" })).toBeInTheDocument();
    expect(screen.queryByText("Unsupported raw IF condition")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply fix" })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Selector" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "Value" })).toHaveValue("");
  });
  it("shows and clears a saved blank Switch selector issue in its editor", async () => {
    const message = "Step 'route' is missing override.switch.selector";
    mockedFetchWorkflowDetail.mockResolvedValue({ workflow_id: "weekly_report", enabled: false, revision: 1,
      steps: [{ id: "11111111-1111-1111-1111-111111111022", step_id: "route", display_name: "Incomplete route", step_kind: "switch",
        override: { switch: { selector: "", cases: [{ case_id: "case_1", when: "" }] } } }], groups: [],
    } as never);
    mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({
      enabled: false,
      entry: { kind: "step", id: "route" },
      steps: [{ id: "route", step_id: "route", kind: "switch", display_name: "Incomplete route", config_summary: "",
        ports: [{ outcome: { name: "case", case_id: "case_1" }, label: "Case 1" }] }],
      validation_issues: [{ scope: "step", scope_id: "route", authored_id: "route", step_id: "route", group_id: "",
        severity: "error", code: "workflow.step_switch_selector_missing", title: message, message,
        field_path: "workflow.steps.route.override.switch.selector" }],
    }) as never);
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder" });
    fireEvent.click(await screen.findByRole("button", { name: /^Diagnostics/ }));
    fireEvent.click(within(await screen.findByRole("region", { name: "Validation issues" }))
      .getByRole("button", { name: new RegExp("missing override.switch.selector") }));
    const drawer = await screen.findByRole("dialog", { name: "Step editor" });
    expect(within(drawer).getByText("Validation issue")).toBeVisible();
    expect(within(drawer).getByText(message)).toBeVisible();
    const selector = within(drawer).getByRole("textbox", { name: "Selector" });
    expect(selector).toHaveValue("");
    fireEvent.change(selector, { target: { value: "state.plan" } });
    await waitFor(() => expect(within(drawer).queryByText("Validation issue")).not.toBeInTheDocument());
    expect(within(drawer).queryByText(message)).not.toBeInTheDocument();
  });

it("opens the requested step drawer from builder URL search params", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "sample_weather_city_brief",
      display_name: "Demo Weather",
      enabled: true,
      revision: 3,
      steps: [{
              id: "11111111-1111-1111-1111-111111111031",
              step_id: "set_default_city",
              display_name: "Default to Berlin",
              step_kind: "set_variable",
              override: {
                set_variable: {
                  assignments: {
                    "state.inputs.city": "Berlin",
                  },
                },
              },
            }, {
              id: "11111111-1111-1111-1111-111111111032",
              step_id: "set_berlin",
              display_name: "Set Berlin coordinates",
              step_kind: "set_variable",
              override: {
                set_variable: {
                  assignments: {
                    "state.weather_query.city": "Berlin",
                  },
                },
              },
            },
            {
              id: "11111111-1111-1111-1111-111111111033",
              step_id: "berlin_continue",
              display_name: "Continue to request",
              step_kind: "if",
              override: {
                if: {
                  condition: "state.weather_query",
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage({
      initialEntry:
        "/registry/workflows/weekly_report/builder?drawer=step&step_id=berlin_continue",
    });

    expect(
      await screen.findByRole("heading", { name: "Step editor" }),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue("Continue to request")).toBeInTheDocument();
    expect(screen.getByDisplayValue("state.weather_query")).toHaveValue(
      "state.weather_query",
    );
  });

  it("shows unsupported raw if conditions as remediation context", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "sample_weather_city_brief",
      display_name: "Demo Weather",
      enabled: true,
      revision: 3,
      steps: [{
              id: "11111111-1111-1111-1111-111111111021",
              step_id: "check_city_input",
              display_name: "Check city input",
              step_kind: "if",
              override: {
                if: {
                  condition: true,
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: false, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /Check city input/i }),
    );
    expect(
      await screen.findByRole("heading", { name: "Step editor" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Unsupported raw IF condition")).toBeInTheDocument();
    expect(
      screen.getByText(
        "This raw IF condition uses a constant value.",
        { exact: false },
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("true")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Selector" })).not.toBeInTheDocument();
    const operator = screen.getByRole("combobox", { name: "Operator" });
    expect(operator).toHaveTextContent("always true");
    fireEvent.click(operator);
    expect(within(screen.getByRole("listbox", { name: "Operator options" }))
      .getByRole("option", { name: "always true" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(operator, { key: "Escape" });
    fireEvent.click(screen.getByRole("button", { name: "Apply fix" }));
    await waitFor(() => {
      expect(
        screen.queryByText("Unsupported raw IF condition"),
      ).not.toBeInTheDocument();
    });
    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(postedChangeSets()[0]).toMatchObject({
      expected_revision: 3,
      operations: [{ op: "update_step_config", step_id: "check_city_input" }],
    });
    expect(lastPatch().override).toEqual({
      if: { condition: { operator: "always_true" } },
    });
  });

  it("hydrates retired set_variable target/value steps into visible assignment rows", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "sample_weather_city_brief",
      display_name: "Demo Weather",
      enabled: true,
      revision: 3,
      steps: [{
              id: "11111111-1111-1111-1111-111111111012",
              step_id: "set_default_city",
              display_name: "Default to Berlin",
              step_kind: "set_variable",
              override: {
                set_variable: { target: "state.inputs.city", value: "Berlin" },
              },
            }, {
              id: "11111111-1111-1111-1111-111111111013",
              step_id: "set_berlin",
              display_name: "Set Berlin coordinates",
              step_kind: "set_variable",
              override: {
                set_variable: {
                  target: "state.weather_query",
                  value: { city: "Berlin", latitude: 52.52, longitude: 13.41 },
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /Default to Berlin/i }),
    );
    expect(
      await screen.findByRole("heading", { name: "Step editor" }),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue("state.inputs.city")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Berlin")).toBeInTheDocument();

    fireEvent.click(
      await screen.findByRole("button", { name: /Set Berlin coordinates/i }),
    );
    expect(
      await screen.findByRole("textbox", { name: "Assignment 1 target" }),
    ).toHaveValue("Weather Query");
    const value = await screen.findByRole("textbox", { name: "Assignment 1 value" }) as HTMLInputElement;
    expect(JSON.parse(value.value)).toEqual({ city: "Berlin", latitude: 52.52, longitude: 13.41 });
    fireEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
    fireEvent.click(screen.getByRole("tab", { name: "Advanced" }));
    const override = JSON.parse((screen.getByRole("textbox", { name: "Override JSON" }) as HTMLTextAreaElement).value);
    expect(override.set_variable.target).toBe("state.weather_query");
  });

  it("hydrates set_variable source-copy assignments into visible assignment rows", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "sample_product_catalog_enrichment",
      display_name: "Demo Product Catalog",
      enabled: true,
      revision: 3,
      steps: [{
              id: "11111111-1111-1111-1111-111111111014",
              step_id: "copy_current_batch",
              display_name: "Copy current batch",
              step_kind: "set_variable",
              override: {
                set_variable: {
                  assignments: {
                    "state.current_batch_copy": {
                      source: "state.current_batch",
                    },
                  },
                },
              },
            }], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    fireEvent.click(
      await screen.findByRole("button", { name: /Copy current batch/i }),
    );
    expect(
      await screen.findByRole("heading", { name: "Step editor" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByDisplayValue("[object Object]"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Assignment 1 target" })).toHaveValue("Current Batch Copy");
    expect(screen.getByRole("combobox", { name: "Assignment 1 value mode" })).toHaveValue("source");
    expect(screen.getByRole("textbox", { name: "Assignment 1 previous value" })).toHaveValue("state.current_batch");
    expect(screen.queryByText(/plain state path is saved as literal text/i)).not.toBeInTheDocument();

    // Save rests until something changes; a label edit sends the hydrated assignments.
    fireEvent.change(screen.getByRole("textbox", { name: "Step label" }), { target: { value: "Copy the current batch" } });
    fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));

    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(lastPatch().override).toMatchObject({
      set_variable: {
        assignments: {
          "state.current_batch_copy": { source: "state.current_batch" },
        },
      },
    });
  });

  it("explains and saves a For each source from its editor", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "sample_product_catalog_enrichment",
      display_name: "Demo Product Catalog",
      enabled: true,
      revision: 3,
      steps: [{
        step_id: "measure_current_batch",
        display_name: "Measure current batch",
        step_kind: "connector",
        for_each_id: "inspect_batches_loop",
      }],
      groups: [],
      for_each: [{
        for_each_id: "inspect_batches_loop",
        display_name: "Inspect batches",
        over: "catalog_batches",
        item_name: "current_batch",
        body: { kind: "sequence", entry: { kind: "step", id: "measure_current_batch" } },
      }],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage({ surface: "builder" });

    await waitFor(() => {
      expect((globalThis as unknown as Window).openGroupDrawer).toBeTypeOf("function");
    });
    act(() => {
      (globalThis as unknown as Window).openGroupDrawer?.("inspect_batches_loop", { defaultTab: "settings" });
    });

    expect(
      await screen.findByRole("heading", { name: "For each editor" }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("for-each-settings")).toBeInTheDocument();
    const loopSource = screen.getByRole("textbox", { name: "Loop source" });
    expect(loopSource).toHaveValue("catalog_batches");
    expect(screen.getByRole("textbox", { name: "Item name" })).toHaveValue("current_batch");
    expect(screen.getByRole("combobox", { name: "Body entry" })).toHaveValue("measure_current_batch");

    fireEvent.change(loopSource, { target: { value: "state.catalog_batches" } });
    fireEvent.click(screen.getByRole("button", { name: "Save For each" }));

    await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
    expect(postedChangeSets()[0]).toMatchObject({
      expected_revision: 3,
      operations: [
        {
          op: "update_for_each_config",
          for_each_id: "inspect_batches_loop",
          patch: {
            display_name: "Inspect batches",
            over: "state.catalog_batches",
            item_name: "current_batch",
            index_name: null,
          },
        },
      ],
    });
    // The source and names are the For each's own settings: they never carry its id or body.
    const patch = (postedChangeSets()[0].operations as Array<{ patch: Record<string, unknown> }>)[0].patch;
    expect(patch).not.toHaveProperty("body");
    expect(patch).not.toHaveProperty("kind");
  });
  it("shows a For each as the body entry of the Group it starts", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "orders",
      display_name: "Orders",
      enabled: true,
      revision: 2,
      steps: [{ step_id: "keep", display_name: "Keep", step_kind: "collect", for_each_id: "each_order" }],
      groups: [{
        group_id: "process",
        display_name: "Process orders",
        body: { kind: "sequence", entry: { kind: "for_each", id: "each_order" } },
      }],
      for_each: [{
        for_each_id: "each_order",
        display_name: "Each order",
        group_id: "process",
        over: "state.orders",
        item_name: "order",
        body: { kind: "sequence", entry: { kind: "step", id: "keep" } },
      }],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage({ surface: "builder" });

    await waitFor(() => {
      expect((globalThis as unknown as Window).openGroupDrawer).toBeTypeOf("function");
    });
    act(() => {
      (globalThis as unknown as Window).openGroupDrawer?.("process", { defaultTab: "settings" });
    });

    const entry = await screen.findByRole("combobox", { name: "Body entry" });
    expect(entry).toHaveValue("for_each:each_order");
    expect(within(entry).getByRole("option", { name: "Each order" })).toBeInTheDocument();
  });
});
