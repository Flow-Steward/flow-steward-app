import { render, screen, within } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { GenericStepNode, IfStepNode, SwitchStepNode, TerminalStepNode } from "@/pages/workflows/builder/canvas-step-nodes";
import type { StepNodeData } from "@/pages/workflows/builder/layout";

// Literal reviewed labels; not computed using the production kind mapping.
const cases = [
  {
    "kind": "agent",
    "key": "react.workflows.step_kind_agent",
    "en": "Agent",
    "lt": "Agentas"
  },
  {
    "kind": "connector",
    "key": "react.workflows.step_kind_connector",
    "en": "Action step",
    "lt": "Veiksmo žingsnis"
  },
  {
    "kind": "invoke_workflow",
    "key": "react.workflows.step_kind_invoke_workflow",
    "en": "Invoke workflow",
    "lt": "Iškviesti darbo eigą"
  },
  {
    "kind": "if",
    "key": "react.workflows.step_kind_if",
    "en": "If",
    "lt": "Jei"
  },
  {
    "kind": "switch",
    "key": "react.workflows.step_kind_switch",
    "en": "Switch",
    "lt": "Pasirinkimas"
  },
  {
    "kind": "stop_success",
    "key": "react.workflows.step_kind_stop_success",
    "en": "Stop · success",
    "lt": "Baigti · sėkmingai"
  },
  {
    "kind": "stop_fail",
    "key": "react.workflows.step_kind_stop_fail",
    "en": "Stop · fail",
    "lt": "Baigti · nesėkmingai"
  },
  {
    "kind": "set_variable",
    "key": "react.workflows.step_kind_set_variable",
    "en": "Set variable",
    "lt": "Nustatyti kintamąjį"
  },
  {
    "kind": "collect",
    "key": "react.workflows.step_kind_collect",
    "en": "Collect",
    "lt": "Rinkti"
  },
  {
    "kind": "continue",
    "key": "react.workflows.step_kind_continue",
    "en": "Continue",
    "lt": "Tęsti"
  },
  {
    "kind": "break",
    "key": "react.workflows.step_kind_break",
    "en": "Break",
    "lt": "Nutraukti"
  },
  {
    "kind": "batch",
    "key": "react.workflows.step_kind_batch",
    "en": "Batch",
    "lt": "Paketas"
  },
  {
    "kind": "http_request",
    "key": "react.workflows.step_kind_http_request",
    "en": "HTTP request",
    "lt": "HTTP užklausa"
  },
  {
    "kind": "dataset_query",
    "key": "react.workflows.step_kind_dataset_query",
    "en": "DataSet",
    "lt": "Duomenų rinkinys"
  },
  {
    "kind": "transform",
    "key": "react.workflows.step_kind_transform",
    "en": "Transform",
    "lt": "Transformuoti"
  },
  {
    "kind": "tabular_feed",
    "key": "react.workflows.step_kind_tabular_feed",
    "en": "Tabular feed",
    "lt": "Lentelės duomenų srautas"
  },
  {
    "kind": "file_from_base64",
    "key": "react.workflows.step_kind_file_from_base64",
    "en": "File from base64",
    "lt": "Failas iš base64"
  },
  {
    "kind": "template",
    "key": "react.workflows.step_kind_template",
    "en": "Template",
    "lt": "Šablonas"
  }
];

function mount(kind: string, name: string, locale: "en" | "lt") {
  const errorPort = { outcome: { name: "error" as const }, label: "owner.error" };
  const step: StepNodeData["step"] = {
    id: "owner-node-id", step_id: name, display_name: name,
    kind, config_summary: "state.owner_input -> state.owner_output",
    ports: kind === "if" ? [
      { outcome: { name: "true" }, label: "owner.true" },
      { outcome: { name: "false" }, label: "owner.false" },
      errorPort,
    ] : kind === "switch" ? [
      { outcome: { name: "case", case_id: "case_0" }, label: "owner.case" },
      errorPort,
    ] : ["stop_success", "stop_fail"].includes(kind) ? [] : [
      { outcome: { name: "success" }, label: "owner.success" },
      errorPort,
    ],
  };
  const data: StepNodeData = {
    step, automationRules: [], validationIssues: [], isEntry: false, local: false, unreachable: false,
    onAutomation: () => undefined, onOpenStep: () => undefined, onEditStep: () => undefined,
  };
  const original = JSON.stringify(data);
  const messages = Object.fromEntries(cases.map((row) => [row.key, row[locale]]));
  const Component = kind === "if" ? IfStepNode : kind === "switch" ? SwitchStepNode
    : ["stop_success", "stop_fail"].includes(kind) ? TerminalStepNode : GenericStepNode;
  render(<I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (key, text = "", params) => interpolate(messages[key] ?? text, params) }}>
    <ReactFlowProvider><Component id={step.id} data={data} type="action" selected={false}
      dragging={false} isConnectable={false} zIndex={1} selectable draggable deletable
      positionAbsoluteX={0} positionAbsoluteY={0} /></ReactFlowProvider>
  </I18nContext.Provider>);
  return { data, original, node: screen.getByRole("group", { name: `Open step ${name}` }) };
}

describe.each(["en", "lt"] as const)("%s canonical kind display", (locale) => {
  it.each(cases)("localizes $kind heading without translating identical owner data", (row) => {
    const { node, data, original } = mount(row.kind, row.kind, locale);
    const texts = within(node);
    // Connector type icons are redundant beside the actual visible card name.
    // Other kinds retain their established accessible icon semantics.
    if (row.kind === "connector") expect(texts.queryByRole("img")).not.toBeInTheDocument();
    else expect(texts.getByRole("img", { name: row[locale] })).toBeInTheDocument();
    expect(texts.getAllByText(row.kind, { exact: true })).toHaveLength(1);
    expect(texts.getByText("state.owner_input -> state.owner_output")).toBeInTheDocument();
    expect(JSON.stringify(data)).toBe(original);
    // Hidden graph ports have no accessible text; verify the actual React Flow port boundary.
    // eslint-disable-next-line testing-library/no-node-access
    const handles = [...node.querySelectorAll(".react-flow__handle")].map((element) => element.getAttribute("data-handleid"));
    expect(handles).toEqual(row.kind === "if" ? ["in", "true", "false"]
      : row.kind === "switch" ? ["in", "case:case_0"]
      : ["stop_success", "stop_fail"].includes(row.kind) ? ["in"] : ["in", "success"]);
    if (row.kind === "if") {
      expect(texts.getByText("owner.true")).toBeInTheDocument();
      expect(texts.getByText("owner.false")).toBeInTheDocument();
    }
    if (row.kind === "switch") expect(texts.getByText("owner.case")).toBeInTheDocument();
  });

  it.each(cases)("preserves owner title/ID identical to translated $kind display text", (row) => {
    const { node, data, original } = mount(row.kind, row[locale], locale);
    expect(within(node).getAllByText(row[locale], { exact: true })).toHaveLength(1);
    if (row.kind === "connector") expect(within(node).queryByRole("img")).not.toBeInTheDocument();
    else expect(within(node).getByRole("img", { name: row[locale] })).toBeInTheDocument();
    expect(JSON.stringify(data)).toBe(original);
  });

  it.each(["owner_custom_kind", "__proto__", "constructor", "toString", "SET_VARIABLE", " set_variable "])(
    "keeps unknown/noncanonical kind %s raw", (kind) => {
      const { node, data, original } = mount(kind, "Owner title", locale);
      expect(within(node).getByRole("img", { name: kind.trim() })).toBeInTheDocument();
      expect(within(node).getAllByText("Owner title")).toHaveLength(1);
      expect(JSON.stringify(data)).toBe(original);
    },
  );
});


it.each(["en", "lt"] as const)("keeps the connector card's real name without an obsolete %s generic icon tooltip", (locale) => {
  const { node, data, original } = mount("connector", "Merchant lookup", locale);
  expect(node).toHaveAccessibleName("Open step Merchant lookup");
  expect(within(node).getByText("Merchant lookup", { exact: true })).toBeVisible();
  expect(within(node).getByText("state.owner_input -> state.owner_output")).toBeVisible();
  expect(within(node).queryByRole("img")).not.toBeInTheDocument();
  expect(screen.queryByTitle(locale === "lt" ? "Veiksmo žingsnis" : "Action step")).not.toBeInTheDocument();
  expect(JSON.stringify(data)).toBe(original);
});
