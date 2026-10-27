/* Item 11 (the owner's live test on fs-acc): the variable suggestions did not narrow to
   what was typed -- "project_config." still listed state.resume_stage, state.risk_level,
   ... -- in the If Selector, and every other variable field was a plain input. Now each
   variable field of each step kind is the same filtering combobox. */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  fetchSpy,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowContract,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowValidation,
  postedChangeSets,
  renderPage,
} from "./index.test-helpers";
import { stringFromUnknownValue } from "@/lib/utils/unknown-values";
import { workflowVariablesContext } from "./detail-variable-filter";

const STATE = [
  "current_group_id",
  "current_step_id",
  "project_config",
  "resume_stage",
  "rework_instructions",
  "risk_level",
  "routing_reason",
  "routing_signals",
  "status",
];

const DESCRIPTIONS: Record<string, string> = { current_step_id: "The step that runs now" };

/** An action whose only parameter and output are optional: its mapping starts with one row and no key. */
const LOOKUP_ACTION = {
  operation_id: "lookup_issue",
  display_name: "Look up issue",
  description: "Find an issue",
  parameters: [{ name: "query", type: "string", required: false, description: "Search text" }],
  returns: { name: "issue", type: "json", required: false, description: "The issue found" },
};

function serveOptionalOnlyAction() {
  const fallback = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    if (stringFromUnknownValue(input).includes("/meta/connector-catalog")) {
      return new Response(
        JSON.stringify({
          connectors: [{
            connector_id: "github", display_name: "GitHub", description: "Issue tracker actions", source: "registry_tool",
            tool_id: "github", tool_url: "/registry/tools/github", operations: [LOOKUP_ACTION],
            connections: [{ connection_id: "conn_1", display_name: "Primary GitHub" }],
          }],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return fallback!(input, init);
  });
}

function serveVariables(steps?: unknown[], priorOutputs: unknown[] = []) {
  const fallback = fetchSpy.getMockImplementation();
  fetchSpy.mockImplementation(async (input, init) => {
    const url = stringFromUnknownValue(input);
    if (url.includes("/available-variables")) {
      // A step with no saved place gets the workflow-level answer: position unknown.
      const placed = url.includes("/steps/");
      const inAnalysis = url.includes("for_each_id=analysis") ||
        (url.includes("/steps/step_1/") && steps?.some((row) => {
          const step = row as { step_id?: string; for_each_id?: string };
          return step.step_id === "step_1" && step.for_each_id === "analysis";
        }));
      return new Response(
        JSON.stringify({
          state: STATE.map((name) => ({ name, type: "text", ...(DESCRIPTIONS[name] ? { description: DESCRIPTIONS[name] } : {}) })),
          dotted_path: [{ name: "project_config.stack.language", type: "text" }],
          prior_outputs: priorOutputs,
          loop: inAnalysis
            ? [{ name: "item", type: "json", source: "for_each", for_each_id: "analysis" },
              { name: "index", type: "number", source: "for_each", for_each_id: "analysis" }]
            : [],
          position: placed ? "before" : "unknown",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return fallback!(input, init);
  });
  mockedFetchWorkflowDetail.mockResolvedValue({
    workflow_id: "workflow_contextual",
    display_name: "Contextual Bugfix",
    enabled: false,
    revision: 2,
    steps: steps ?? [
      {
        step_id: "step_1",
        display_name: "Step one",
        step_kind: "agent",
        agent_id: "planner",
        override: {},
      },
    ],
    groups: [],
    for_each: [
      {
        for_each_id: "analysis",
        display_name: "Analysis",
        over: "state.items",
        item_name: "item",
        index_name: "index",
        body: { kind: "sequence" },
      },
    ],
  } as never);
  mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
  mockedFetchWorkflowValidation.mockResolvedValue({
    validation: { valid: true, errors: [], warnings: [] },
  } as never);
}

/** A saved step of the kind under test: a saved step's kind is fixed, so each kind gets its own. */
async function openStep(kind = "agent") {
  serveVariables([
    { step_id: "step_1", display_name: "Step one", step_kind: kind, agent_id: kind === "agent" ? "planner" : "", override: {} },
  ]);
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Step one/i }, { timeout: 5_000 }));
  return screen.findByRole("combobox", { name: "Step kind" });
}

function panelOf(field: HTMLElement): HTMLElement {
  return screen.getByTestId(stringFromUnknownValue(field.getAttribute("aria-controls")));
}

function shown(field: HTMLElement): string[] {
  return within(panelOf(field))
    .queryAllByRole("option")
    .map((option) => stringFromUnknownValue(option.textContent));
}

async function expectItFilters(
  user: ReturnType<typeof userEvent.setup>,
  field: HTMLElement,
  typed = "project_",
) {
  await user.clear(field); // some start with a default, such as the switch's task.mode
  await user.click(field);
  expect(field).toHaveValue("");
  const advanced = within(panelOf(field)).getByRole("button", { name: /Advanced values/ });
  if (!advanced.closest("section")?.querySelector('[role="option"]')) await user.click(advanced);
  await waitFor(() => expect(panelOf(field)).toHaveTextContent("state.risk_level"));

  await user.type(field, typed);
  const matches = shown(field);
  expect(matches.length).toBeGreaterThan(0);
  expect(matches.every((text) => text.toLowerCase().includes("project_"))).toBe(true);
  expect(matches[0]).toMatch(/^state\.project_config/); // it starts with it: first
  expect(panelOf(field)).not.toHaveTextContent("state.risk_level");

  await user.clear(field);
  await user.type(field, "zzz");
  expect(panelOf(field)).toHaveTextContent("No matching variables");
  expect(shown(field)).toEqual([]);

  await user.type(field, "{Backspace}{Backspace}{Backspace}r");
  expect(shown(field).some((text) => text.startsWith("state.risk_level"))).toBe(true); // wider
  expect(field).toHaveFocus();
}

// Each step kind's variable field, by the name it has in the drawer.
const FIELDS: Array<[string, string | RegExp]> = [
  ["if", "Selector"],
  ["switch", "Selector"],
  ["transform", "Transform source"],
  ["http_request", "Response target"],
  ["invoke_workflow", "Input mapping value 1"],
  ["set_variable", "Assignment 1 previous value"],
  ["collect", "Variable to add to"],
  ["batch", /Source path/],
  ["dataset_query", /Result target/],
  ["tabular_feed", /Result target/],
];

describe("WorkflowsDetailPage variable autocomplete", () => {
  it.each(FIELDS)("the %s field filters its suggestions by what is typed", async (kind, name) => {
    const user = userEvent.setup();
    await openStep(kind);
    if (kind === "set_variable") {
      fireEvent.change(screen.getByRole("combobox", { name: "Assignment 1 value mode" }), { target: { value: "source" } });
    }
    if (kind === "invoke_workflow") {
      // A mapping row keeps its value once it names the child input it maps to.
      await user.type(await screen.findByRole("textbox", { name: "Input mapping key 1" }), "city");
    }

    await expectItFilters(user, await screen.findByRole("combobox", { name }));
  }, 15_000);

  it("Enter, Tab and the arrows take a match, and the focus stays", async () => {
    const user = userEvent.setup();
    await openStep("if");
    const selector = await screen.findByRole("combobox", { name: "Selector" });

    await user.click(selector);
    await user.click(within(panelOf(selector)).getByRole("button", { name: /Advanced values/ }));
    await user.type(selector, "project_");
    await user.keyboard("{ArrowDown}{Enter}"); // the second match
    expect(selector).toHaveValue("project_config.stack.language");
    expect(selector).toHaveFocus();

    await user.clear(selector);
    await user.type(selector, "risk");
    await user.keyboard("{Tab}");
    expect(selector).toHaveValue("state.risk_level");
    expect(selector).toHaveFocus();
  }, 15_000);

  it("a template field suggests after {{ and keeps the text around it", async () => {
    const user = userEvent.setup();
    await openStep("http_request");
    const url = await screen.findByRole("combobox", { name: "URL" });

    await user.click(url);
    await user.type(url, "https://api.example.test/");
    expect(url).toHaveAttribute("aria-expanded", "false"); // no open {{: nothing to suggest

    await user.type(url, "{{{{ "); // "{{" typed as two literal braces
    await user.click(within(panelOf(url)).getByRole("button", { name: /Advanced values/ }));
    await user.type(url, "risk");
    expect(shown(url)).toEqual([expect.stringMatching(/^state\.risk_level/)]);
    await user.keyboard("{Enter}");
    expect(url).toHaveValue("https://api.example.test/{{ state.risk_level }}");
    expect(url).toHaveFocus();
  }, 15_000);

  it("the connector's parameter and output fields filter the same way", async () => {
    const user = userEvent.setup();
    serveVariables([
      {
        id: "11111111-1111-1111-1111-111111111011",
        step_id: "create_issue",
        display_name: "Create issue",
        step_kind: "connector",
        connector_id: "github",
        override: { connector: { function_name: "create_issue" } },
        input_mapping_jsonb: { title: "state.issue_title" },
        output_mapping_jsonb: { issue_url: "state.issue_url" },
      },
    ]);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Create issue/i }));

    // Its State path field used to turn into a Raw string on the first letter typed.
    const parameter = await screen.findByRole("combobox", { name: /Parameter value 1/ });
    await user.clear(parameter);
    await user.click(parameter);
    await user.click(within(panelOf(parameter)).getByRole("button", { name: /Advanced values/ }));
    await waitFor(() => expect(panelOf(parameter)).toHaveTextContent("state.project_config"));
    await user.type(parameter, "project_");
    expect(parameter).toHaveValue("state.project_"); // still a state path
    expect(shown(parameter).every((text) => text.toLowerCase().includes("project_"))).toBe(true);
    expect(shown(parameter)[0]).toMatch(/^state\.project_config/);
    expect(parameter).toHaveFocus();
    await expectItFilters(user, await screen.findByRole("combobox", { name: "Output target 1" }));
  }, 15_000);

  it("the For each source filters the same way", async () => {
    const user = userEvent.setup();
    serveVariables([
      { step_id: "step_1", display_name: "Step one", step_kind: "agent", agent_id: "planner", for_each_id: "analysis" },
    ]);
    renderPage({ surface: "builder" });
    await waitFor(() => {
      expect((globalThis as unknown as Window).openGroupDrawer).toBeTypeOf("function");
    });
    act(() => {
      (globalThis as unknown as Window).openGroupDrawer?.("analysis", { defaultTab: "settings" });
    });

    await expectItFilters(user, await screen.findByRole("combobox", { name: "Loop source" }));
  }, 15_000);
});

/* B7: a step added in the builder lives only in the unapplied change set until Apply.
   It used to get no suggestions at all; it now gets the variables of where it will run,
   and the global variables always. */
describe("a new step that is not applied yet", () => {
  function variableRequests(): string[] {
    return fetchSpy.mock.calls
      .map(([url]) => stringFromUnknownValue(url))
      .filter((url) => url.includes("available-variables"));
  }

  async function addNewStep(kind: string) {
    serveVariables();
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "+ Add Step" }, { timeout: 5_000 }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Step kind" }), { target: { value: kind } });
  }

  async function expectGlobalVariables(user: ReturnType<typeof userEvent.setup>, field: HTMLElement) {
    await user.click(field);
    await waitFor(() => expect(panelOf(field)).toHaveTextContent("Advanced values"));
    if (!within(panelOf(field)).queryByRole("option", { name: /state\.risk_level/ }))
      await user.click(within(panelOf(field)).getByRole("button", { name: /Advanced values/ }));
    expect(panelOf(field)).toHaveTextContent("state.risk_level");
  }

  it.each([
    ["set_variable", "Assignment 1 previous value"],
    ["if", "Selector"],
  ])("a new %s step offers the variables in its %s field before Apply", async (kind, name) => {
    const user = userEvent.setup();
    await addNewStep(kind);

    if (kind === "set_variable") {
      fireEvent.change(screen.getByRole("combobox", { name: "Assignment 1 value mode" }), { target: { value: "source" } });
    }
    const field = await screen.findByRole("combobox", { name });
    await expectGlobalVariables(user, field);
    // Its saved position is unknown; the panel must not assert disconnection.
    expect(within(panelOf(field)).getByTestId("workflow-variables-position-unknown")).toHaveTextContent(
      "position in the saved workflow is not known yet",
    );
    expect(variableRequests()).toContain("/api/workflows/weekly_report/available-variables");
  }, 15_000);

  it("a new action step offers them in its parameter value and output target fields", async () => {
    const user = userEvent.setup();
    await addNewStep("connector");

    await user.click(await screen.findByRole("combobox", { name: "Tool" }));
    await user.click(await within(screen.getByRole("listbox", { name: "Tool options" })).findByRole("option", { name: "GitHub" }));
    await user.click(await screen.findByRole("combobox", { name: "Action" }));
    await user.click(await within(screen.getByRole("listbox", { name: "Action options" })).findByRole("option", { name: /Create issue/ }));
    if (!screen.queryByRole("combobox", { name: /Parameter value 1/ })) {
      fireEvent.click(await screen.findByRole("button", { name: "Add parameter mapping" }));
    }
    if (!screen.queryByRole("combobox", { name: "Output target 1" })) {
      fireEvent.click(await screen.findByRole("button", { name: "Add output mapping" }));
    }

    await expectGlobalVariables(user, await screen.findByRole("combobox", { name: /Parameter value 1/ }));
    await expectGlobalVariables(user, await screen.findByRole("combobox", { name: "Output target 1" }));
  }, 15_000);

  it("a new step in a For each body is offered the For each's item", async () => {
    const user = userEvent.setup();
    await addNewStep("set_variable");

    const placement = await screen.findByRole("combobox", { name: "Add the new step to" });
    await user.click(placement);
    await user.click(within(screen.getByRole("listbox", { name: "Add the new step to options" })).getByRole("option", { name: "Analysis" }));

    fireEvent.change(screen.getByRole("combobox", { name: "Assignment 1 value mode" }), { target: { value: "source" } });
    const field = await screen.findByRole("combobox", { name: "Assignment 1 previous value" });
    await waitFor(() =>
      expect(variableRequests()).toContain("/api/workflows/weekly_report/available-variables?for_each_id=analysis"),
    );
    await expectGlobalVariables(user, field);
    expect(panelOf(field)).toHaveTextContent("state.item");
    expect(within(panelOf(field)).getByTestId("workflow-variables-position-unknown")).not.toHaveTextContent("not connected");
  }, 15_000);
});

it("keeps saved For each entry and sibling scopes, and distinguishes an unapplied first body step", () => {
  const loop = { for_each_id: "analysis", display_name: "Analysis", item_name: "item", index_name: "index",
    body: { entry: { kind: "step", id: "saved_entry" } } };
  const preview = { groups: [], forEach: [loop], steps: [
    { step_id: "saved_entry", for_each_id: "analysis" },
    { step_id: "saved_sibling", for_each_id: "analysis" },
    { step_id: "draft_first", for_each_id: "analysis" },
  ], graph: { edges: [] } } as never;
  const common = { drawerMode: "step", preview, localForEachIds: new Set<string>(),
    localStepIds: new Set(["draft_first"]) };
  expect(workflowVariablesContext({ ...common, selectedStep: { runtimeId: "saved_entry" } })).toMatchObject({
    stepId: "saved_entry", position: "before", forEachId: "",
  });
  expect(workflowVariablesContext({ ...common, selectedStep: { runtimeId: "saved_sibling" } })).toMatchObject({
    stepId: "saved_sibling", position: "before", forEachId: "",
  });
  expect(workflowVariablesContext({ ...common, selectedStep: { runtimeId: "draft_first", local: true },
    createContext: { forEachId: "analysis" } })).toMatchObject({
    stepId: "saved_entry", position: "before", forEachId: "",
  });
  expect(workflowVariablesContext({ ...common, preview: { groups: [], forEach: [loop], steps: [
    { step_id: "draft_first", for_each_id: "analysis" },
  ], graph: { edges: [] } } as never, selectedStep: { runtimeId: "draft_first", local: true },
    createContext: { forEachId: "analysis" } })).toMatchObject({
    stepId: "", position: "unknown", forEachId: "analysis",
  });
});

it("shows saved For each entry item, index and prior outputs without a disconnected claim", async () => {
  const user = userEvent.setup();
  serveVariables([{ step_id: "step_1", display_name: "Step one", step_kind: "if", for_each_id: "analysis",
    override: { if: { selector: "" } } }], [{ name: "outline", step_id: "prepare", type: "json" }]);
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Step one/i }));
  const field = await screen.findByRole("combobox", { name: "Selector" });
  await user.click(field);
  await waitFor(() => expect(panelOf(field)).toHaveTextContent("state.item"));
  expect(panelOf(field)).toHaveTextContent("state.index");
  expect(panelOf(field)).toHaveTextContent("state.outline");
  expect(within(panelOf(field)).queryByTestId("workflow-variables-position-unknown")).not.toBeInTheDocument();
}, 15_000);

/* B24: in an action step's Parameters row (and its Output mapping row) with no parameter
   chosen yet, the Value field showed the variables on click, but what was typed vanished
   on every keystroke -- the mapping is keyed by the parameter, so a keyless row could not
   hold a value -- and the list never filtered. */
/** The variable paths a field's open list offers, in order (an option also shows its description and type). */
function paths(field: HTMLElement): string[] {
  return within(panelOf(field))
    .queryAllByRole("option")
    .map((option) => stringFromUnknownValue(option.querySelector("span span")?.textContent));
}

describe("an action's Value and Store in workflow state fields keep what is typed (B24)", () => {
  async function typeAndExpectCurrent(user: ReturnType<typeof userEvent.setup>, field: HTMLElement, prefix: string) {
    await user.clear(field);
    await user.click(field);
    if (!within(panelOf(field)).queryByRole("option", { name: /state\.risk_level/ }))
      await user.click(within(panelOf(field)).getByRole("button", { name: /Advanced values/ }));
    await waitFor(() => expect(panelOf(field)).toHaveTextContent("state.risk_level"));
    await user.type(field, "current");
    expect(field).toHaveValue(`${prefix}current`);
    expect(paths(field)).toEqual(["state.current_group_id", "state.current_step_id"]);
    await user.clear(field);
    await user.type(field, "rrent_st"); // a fragment from the middle of a name
    expect(field).toHaveValue(`${prefix}rrent_st`);
    expect(paths(field)).toEqual(["state.current_step_id"]);
    expect(field).toHaveFocus();
    await user.click(within(panelOf(field)).getByRole("option", { name: /state\.current_step_id/ }));
    expect(field).toHaveValue("state.current_step_id");
  }

  async function chooseAction(user: ReturnType<typeof userEvent.setup>, action: RegExp) {
    await user.click(await screen.findByRole("combobox", { name: "Tool" }));
    await user.click(await within(screen.getByRole("listbox", { name: "Tool options" })).findByRole("option", { name: "GitHub" }));
    await user.click(await screen.findByRole("combobox", { name: "Action" }));
    await user.click(await within(screen.getByRole("listbox", { name: "Action options" })).findByRole("option", { name: action }));
  }

  async function addNewActionStep(user: ReturnType<typeof userEvent.setup>, optionalOnly: boolean) {
    serveVariables();
    if (optionalOnly) serveOptionalOnlyAction();
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "+ Add Step" }, { timeout: 5_000 }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Step kind" }), { target: { value: "connector" } });
    await chooseAction(user, optionalOnly ? /Look up issue/ : /Create issue/);
  }

  it("a new unapplied step with no parameter chosen", async () => {
    const user = userEvent.setup();
    await addNewActionStep(user, true);
    expect(await screen.findByRole("combobox", { name: "Parameter 1" })).toHaveValue("");

    await typeAndExpectCurrent(user, await screen.findByRole("combobox", { name: /Parameter value 1/ }), "state.");
    // Choosing the parameter afterwards keeps the value.
    fireEvent.change(screen.getByRole("combobox", { name: "Parameter 1" }), { target: { value: "query" } });
    expect(await screen.findByRole("combobox", { name: /Parameter value 1/ })).toHaveValue("state.current_step_id");

    await typeAndExpectCurrent(user, await screen.findByRole("combobox", { name: "Output target 1" }), "");
    fireEvent.change(screen.getByRole("combobox", { name: "Returned field 1" }), { target: { value: "issue" } });
    expect(await screen.findByRole("combobox", { name: "Output target 1" })).toHaveValue("state.current_step_id");
  }, 20_000);

  it("a new unapplied step with its parameter chosen", async () => {
    const user = userEvent.setup();
    await addNewActionStep(user, false);
    expect(await screen.findByRole("combobox", { name: "Parameter 1" })).toHaveValue("title");

    await typeAndExpectCurrent(user, await screen.findByRole("combobox", { name: /Parameter value 1/ }), "state.");
  }, 20_000);

  it.each([
    ["no parameter chosen", {}, "Parameter value 1"],
    ["its parameter chosen", { query: "state.risk_level" }, "Parameter value 1"],
  ])("a saved step with %s", async (_case, mapping, name) => {
    const user = userEvent.setup();
    serveVariables([
      {
        id: "11111111-1111-1111-1111-111111111012",
        step_id: "look_up",
        display_name: "Look up",
        step_kind: "connector",
        connector_id: "github",
        override: { connector: { function_name: "lookup_issue" } },
        input_mapping_jsonb: mapping,
        output_mapping_jsonb: {},
      },
    ]);
    serveOptionalOnlyAction();
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Look up/i }, { timeout: 5_000 }));

    await typeAndExpectCurrent(user, await screen.findByRole("combobox", { name: new RegExp(name) }), "state.");
    await typeAndExpectCurrent(user, await screen.findByRole("combobox", { name: "Output target 1" }), "");
  }, 20_000);
});

/* A new Set variable row keeps its destination draft while the value mode
   explicitly chooses a previous runtime value. */
describe("a Set variable assignment row added with + Add", () => {
  it("keeps its blank target while selecting and clearing a previous value", async () => {
    const user = userEvent.setup();
    serveVariables([{ step_id: "step_1", display_name: "Step one", step_kind: "set_variable",
      override: { set_variable: { assignments: { "state.plan": true } } } }]);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Step one/i }, { timeout: 5_000 }));
    fireEvent.click(await screen.findByRole("button", { name: "Add assignment" }));
    const target = await screen.findByRole("textbox", { name: "Assignment 2 target" });
    expect(target).toHaveValue("");
    fireEvent.change(screen.getByRole("combobox", { name: "Assignment 2 value mode" }), { target: { value: "source" } });
    const previous = await screen.findByRole("combobox", { name: "Assignment 2 previous value" });
    await user.click(previous);
    await user.click(within(panelOf(previous)).getByRole("button", { name: /Advanced values/ }));
    expect(panelOf(previous)).toHaveTextContent("state.risk_level");
    await user.type(previous, "risk");
    expect(shown(previous)).toEqual([expect.stringMatching(/^state\.risk_level/)]);
    await user.keyboard("{Enter}");
    expect(previous).toHaveValue("state.risk_level");
    await user.clear(previous);
    expect(screen.getByRole("textbox", { name: "Assignment 2 target" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "Assignment 1 target" })).toHaveValue("Plan");
  }, 15_000);
});


it("offers a saved Customer Name member with business labels and keeps its exact source token", async () => {
  serveVariables([{ step_id: "step_1", display_name: "Normalize customer", step_kind: "transform",
    override: { transform: { input: { source: "" }, result_target: "state.normalized_customer" } } }]);
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report", enabled: true, display_name: "Weekly Report",
    interface: { enabled: true, input_mode: "json_schema", expected_inputs: [],
      input_schema: { type: "object", required: ["customer"], properties: {
        customer: { type: "object", title: "Customer", required: ["name"], properties: {
          name: { type: "string", title: "Name" },
        } },
      } }, expected_outputs: [] }, definition_issues: [],
  } as never);
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Normalize customer/i }));
  const source = await screen.findByRole("combobox", { name: "Transform source" });
  fireEvent.click(source);
  const panel = panelOf(source);
  const name = await within(panel).findByRole("option", { name: /Customer.*Name/ });
  expect(name).toBeVisible();
  expect(panel).not.toHaveTextContent("current_step_id");
  fireEvent.mouseDown(name);
  expect(source).toHaveValue("Customer → Name");
  expect(source).not.toHaveValue("state.inputs.customer.name");
});



it("finds Client name by its business label and saves the exact source after keyboard choice", async () => {
  const user = userEvent.setup();
  serveVariables([{ step_id: "step_1", display_name: "Normalize customer", step_kind: "transform",
    override: { transform: { input: { source: "state.inputs.customer" }, result_target: "state.summary" } } }]);
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report", enabled: true, display_name: "Weekly Report",
    interface: { enabled: true, input_mode: "json_schema", expected_inputs: [],
      input_schema: { type: "object", properties: { customer: { type: "object", title: "Customer",
        properties: { name: { type: "string", title: "Client name" } } } } }, expected_outputs: [] },
    definition_issues: [],
  } as never);
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Normalize customer/i }));
  const source = await screen.findByRole("combobox", { name: "Transform source" });
  await user.clear(source);
  await user.type(source, "Client");
  expect(shown(source)).toEqual(expect.arrayContaining([expect.stringMatching(/Customer.*Client name/)]));
  await user.keyboard("{Enter}");
  expect(source).toHaveValue("Customer → Client name");
  fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
  expect(postedChangeSets()[0]).toMatchObject({
    operations: [{ patch: { override: { transform: { input: { source: "state.inputs.customer.name" } } } } }],
  });
}, 15_000);

it("writes a declared Text result through the destination picker and accepts a new variable name", async () => {
  const user = userEvent.setup();
  serveVariables([{ step_id: "step_1", display_name: "Normalize customer", step_kind: "transform",
    override: { transform: { input: { source: "state.inputs.customer" }, result_target: "state.custom.legacy" } } }]);
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report", enabled: true, display_name: "Weekly Report",
    interface: { enabled: true, input_mode: "fields", expected_inputs: [],
      expected_outputs: [{ name: "text", type: "string", label: "Text" }] }, definition_issues: [],
  } as never);
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Normalize customer/i }));
  const target = await screen.findByRole("combobox", { name: "Transform result target" });
  expect(target).toHaveValue("state.custom.legacy");
  await user.click(target);
  const option = await within(panelOf(target)).findByRole("option", { name: /Text/ });
  fireEvent.mouseDown(option);
  expect(target).toHaveValue("Text");
  await user.click(target);
  await user.clear(target);
  await user.type(target, "audit_note");
  expect(target).toHaveValue("audit_note");
  fireEvent.blur(target);
  expect(target).toHaveValue("Audit Note");
});



it("types and saves a new destination exactly when no workflow outputs are declared", async () => {
  const user = userEvent.setup();
  serveVariables([{ step_id: "step_1", display_name: "Normalize customer", step_kind: "transform",
    override: { transform: { input: { source: "state.inputs.customer" }, result_target: "state.legacy" } } }]);
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report", enabled: true, display_name: "Weekly Report",
    interface: { enabled: true, input_mode: "fields", expected_inputs: [], expected_outputs: [] }, definition_issues: [],
  } as never);
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Normalize customer/i }));
  const target = await screen.findByRole("textbox", { name: "Transform result target" });
  await user.clear(target);
  await user.type(target, "audit_note");
  expect(target).toHaveValue("audit_note");
  fireEvent.blur(target);
  expect(target).toHaveValue("Audit Note");
  fireEvent.click(screen.getByRole("button", { name: "Save parameters" }));
  await waitFor(() => expect(postedChangeSets()).toHaveLength(1));
  expect(postedChangeSets()[0]).toMatchObject({
    operations: [{ patch: { override: { transform: { result_target: "state.audit_note" } } } }],
  });
}, 15_000);


it("keeps engine globals out of search and keyboard choices until Advanced opens", async () => {
  const user = userEvent.setup();
  await openStep("if");
  const selector = await screen.findByRole("combobox", { name: "Selector" });
  await user.clear(selector);
  await user.click(selector);
  await user.type(selector, "current");
  expect(shown(selector)).toEqual([]);
  expect(panelOf(selector)).toHaveTextContent("No matching variables");
  await user.keyboard("{ArrowDown}{Enter}");
  expect(selector).toHaveValue("current");

  await user.clear(selector);
  await user.click(selector);
  await user.click(within(panelOf(selector)).getByRole("button", { name: /Advanced values/ }));
  await user.type(selector, "current");
  expect(shown(selector)).toEqual(expect.arrayContaining([
    expect.stringMatching(/state\.current_group_id/),
    expect.stringMatching(/state\.current_step_id/),
  ]));
  await user.keyboard("{Enter}");
  expect(selector).toHaveValue("state.current_group_id");
}, 15_000);

it("does not suggest a false nested path for a literal dotted schema property", async () => {
  serveVariables([{ step_id: "step_1", display_name: "Read input", step_kind: "transform",
    override: { transform: { input: { source: "" }, result_target: "state.result" } } }]);
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report", enabled: true, display_name: "Weekly Report",
    interface: { enabled: true, input_mode: "json_schema", expected_inputs: [],
      input_schema: { type: "object", properties: { "a.b": { type: "string", title: "Literal dotted key" } } },
      expected_outputs: [] }, definition_issues: [],
  } as never);
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Read input/i }));
  const source = await screen.findByRole("combobox", { name: "Transform source" });
  fireEvent.click(source);
  await waitFor(() => expect(mockedFetchWorkflowContract).toHaveBeenCalled());
  expect(within(panelOf(source)).queryByRole("option", { name: /state\.inputs\.a\.b/ })).not.toBeInTheDocument();
});


it("offers declared Transform members only from a reachable producer", async () => {
  serveVariables([
    { step_id: "step_1", display_name: "Normalize customer", step_kind: "transform",
      override: { transform: { input: { source: "state.inputs.customer" }, result_target: "state.normalized_customer",
        fields: [{ from: "state.inputs.customer.name", to: "name", type: "text" }] } } },
    { step_id: "step_2", display_name: "Write summary", step_kind: "transform",
      override: { transform: { input: { source: "" }, result_target: "state.summary" } } },
    { step_id: "step_3", display_name: "Object without declared members", step_kind: "transform",
      override: { transform: { input: { source: "" }, result_target: "state.untyped" } } },
  ], [
    { step_id: "step_1", name: "normalized_customer", type: "object", source: "transform" },
    { step_id: "step_3", name: "untyped", type: "object", source: "transform" },
  ]);
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Write summary/i }));
  const source = await screen.findByRole("combobox", { name: "Transform source" });
  fireEvent.click(source);
  const panel = panelOf(source);
  const name = await within(panel).findByRole("option", { name: /Normalize customer.*Name/ });
  expect(within(panel).queryByRole("option", { name: /Object without declared members.*Name/ })).not.toBeInTheDocument();
  fireEvent.mouseDown(name);
  expect(source).toHaveValue("Normalize customer → Name");
});


it("offers known copied input and literal object members from reachable Set variable steps", async () => {
  serveVariables([
    { step_id: "step_1", display_name: "Copy customer", step_kind: "set_variable",
      override: { set_variable: { assignments: { "state.customer_copy": { source: " state.inputs.customer " } } } } },
    { step_id: "step_2", display_name: "Save details", step_kind: "set_variable",
      override: { set_variable: { assignments: { "state.details": { status: "ready", count: 0 } } } } },
    { step_id: "step_3", display_name: "Use details", step_kind: "transform",
      override: { transform: { input: { source: "" }, result_target: "state.result" } } },
  ], [
    { step_id: "step_1", name: "customer_copy", type: "object", source: "set_variable" },
    { step_id: "step_2", name: "details", type: "object", source: "set_variable" },
  ]);
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report", enabled: true, display_name: "Weekly Report",
    interface: { enabled: true, input_mode: "json_schema", expected_inputs: [],
      input_schema: { type: "object", properties: { customer: { type: "object", title: "Customer",
        properties: { name: { type: "string", title: "Name" } } } } }, expected_outputs: [] },
    definition_issues: [],
  } as never);
  renderPage();
  fireEvent.click(await screen.findByRole("button", { name: /Use details/i }));
  const source = await screen.findByRole("combobox", { name: "Transform source" });
  fireEvent.click(source);
  const panel = panelOf(source);
  expect(await within(panel).findByRole("option", { name: /Copy customer.*Name/ })).toBeVisible();
  expect(within(panel).queryByRole("option", { name: /Copy customer.*Source/ })).not.toBeInTheDocument();
  const status = within(panel).getByRole("option", { name: /Save details.*Status/ });
  expect(within(panel).getByRole("option", { name: /Save details.*Count/ })).toBeVisible();
  fireEvent.mouseDown(status);
  expect(source).toHaveValue("Save details → Status");
});
