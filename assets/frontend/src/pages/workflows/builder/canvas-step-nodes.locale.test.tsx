import { render, screen, within } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { GenericStepNode, IfStepNode, SwitchStepNode, TerminalStepNode } from "@/pages/workflows/builder/canvas-step-nodes";
import type { StepNodeData } from "@/pages/workflows/builder/layout";
import type { TranslateParams } from "@/lib/i18n/context";

const genericSummaryCases: Array<{ kind: string; raw: string; key: string; lt: string; params?: TranslateParams }> = [
  { kind: "agent", raw: "agent: owner.participant", key: "react.workflows.agent_summary", params: { label: "owner.participant" }, lt: "agentas: owner.participant" },
  { kind: "agent", raw: "agent: owner.agent", key: "react.workflows.agent_summary", params: { label: "owner.agent" }, lt: "agentas: owner.agent" },
  { kind: "agent", raw: "agent step", key: "react.workflows.agent_default_summary", lt: "agento žingsnis" },
  { kind: "invoke_workflow", raw: "invoke owner.configured_child", key: "react.workflows.invoke_workflow_summary", params: { workflow_id: "owner.configured_child" }, lt: "Vykdyti owner.configured_child" },
  { kind: "invoke_workflow", raw: "invoke owner.step_fallback", key: "react.workflows.invoke_workflow_summary", params: { workflow_id: "owner.step_fallback" }, lt: "Vykdyti owner.step_fallback" },
  { kind: "invoke_workflow", raw: "invoke from owner_parent_workflow", key: "react.workflows.invoke_workflow_default_summary", params: { workflow_id: "owner_parent_workflow" }, lt: "Vykdyti iš owner_parent_workflow" },
  { kind: "batch", raw: "state.owner_items in batches of 1", key: "react.workflows.batch_summary", params: { source: "state.owner_items", batch_size: 1 }, lt: "state.owner_items paketais po 1" },
  { kind: "batch", raw: "state.owner_items in batches of 3", key: "react.workflows.batch_summary", params: { source: "state.owner_items", batch_size: 3 }, lt: "state.owner_items paketais po 3" },
  { kind: "batch", raw: "items in batches of 7", key: "react.workflows.batch_default_summary", params: { source: "", batch_size: 7 }, lt: "Elementai paketais po 7" },
];

describe.each(["en", "lt"] as const)("%s generated agent/invoke/batch summaries", (locale) => {
  it.each(genericSummaryCases)("renders $kind $key while preserving identical unkeyed owner text", ({ kind, raw, key, lt, params }) => {
    const messages: Record<string, string> = locale === "lt" ? {
      "react.workflows.agent_summary": "agentas: {label}",
      "react.workflows.agent_default_summary": "agento žingsnis",
      "react.workflows.invoke_workflow_summary": "Vykdyti {workflow_id}",
      "react.workflows.invoke_workflow_default_summary": "Vykdyti iš {workflow_id}",
      "react.workflows.batch_summary": "{source} paketais po {batch_size}",
      "react.workflows.batch_default_summary": "Elementai paketais po {batch_size}",
    } : {
      "react.workflows.agent_summary": "agent: {label}",
      "react.workflows.agent_default_summary": "agent step",
      "react.workflows.invoke_workflow_summary": "invoke {workflow_id}",
      "react.workflows.invoke_workflow_default_summary": "invoke from {workflow_id}",
      "react.workflows.batch_summary": "{source} in batches of {batch_size}",
      "react.workflows.batch_default_summary": "items in batches of {batch_size}",
    };
    const steps = [true, false].map((keyed, index) => ({
      id: `owner-${kind}-${index}`, step_id: `owner-${kind}-key`, display_name: raw,
      kind, config_summary: raw,
      ...(keyed ? { config_summary_key: key, config_summary_params: params } : {}),
    }));
    const original = JSON.stringify(steps);
    render(<I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (messageKey, text = "", values) => interpolate(messages[messageKey] || text, values) }}>
      <ReactFlowProvider>{steps.map((step) => {
        const data: StepNodeData = { step, automationRules: [], validationIssues: [], isEntry: false, local: false, unreachable: false, onAutomation: () => undefined, onOpenStep: () => undefined, onEditStep: () => undefined };
        return <GenericStepNode key={step.id} id={step.id} data={data} type={kind === "batch" ? "batch" : "action"} selected={false} dragging={false} isConnectable={false} zIndex={1} selectable draggable deletable positionAbsoluteX={0} positionAbsoluteY={0} />;
      })}</ReactFlowProvider>
    </I18nContext.Provider>);
    const nodes = screen.getAllByRole("group", { name: `Open step ${raw}` });
    expect(within(nodes[0]).getAllByText(locale === "lt" ? lt : raw)).toHaveLength(locale === "lt" ? 1 : 2);
    expect(within(nodes[0]).queryByText(`owner-${kind}-key`)).not.toBeInTheDocument();
    expect(within(nodes[0]).getByRole("img", { name: kind === "agent" ? "Agent" : kind === "invoke_workflow" ? "Invoke workflow" : "Batch" })).toBeInTheDocument();
    expect(within(nodes[1]).getAllByText(raw)).toHaveLength(2);
    if (locale === "lt") expect(within(nodes[1]).queryByText(lt)).not.toBeInTheDocument();
    expect(JSON.stringify(steps)).toBe(original);
  });
});

function mountBranchSummary(kind: "if" | "switch", raw: string, locale: "en" | "lt", key?: string, params?: TranslateParams) {
  const messages: Record<string, string> = locale === "lt" ? {
    "react.workflows.if_default_summary": "sąlyginis šakojimas",
    "react.workflows.switch_selector_summary": "{selector} (atvejų skaičius: {case_count})",
    "react.workflows.switch_cases_summary": "Pasirinkimo atvejų skaičius: {case_count}",
  } : {
    "react.workflows.if_default_summary": "conditional branch",
    "react.workflows.switch_selector_summary": "{selector} ({case_count} cases)",
    "react.workflows.switch_cases_summary": "{case_count} switch cases",
  };
  const steps = [true, false].map((keyed, index) => ({
    id: `owner-${kind}-${index}`, step_id: `owner-${kind}-key`, display_name: raw,
    kind, config_summary: raw, ports: [],
    ...(keyed && key ? { config_summary_key: key, config_summary_params: params } : {}),
  }));
  const original = JSON.stringify(steps);
  const Component = kind === "if" ? IfStepNode : SwitchStepNode;
  render(<I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (messageKey, text = "", values) => interpolate(messages[messageKey] || text, values) }}>
    <ReactFlowProvider>{steps.map((step) => {
      const data: StepNodeData = { step, automationRules: [], validationIssues: [], isEntry: false, local: false, unreachable: false, onAutomation: () => undefined, onOpenStep: () => undefined, onEditStep: () => undefined };
      return <Component key={step.id} id={step.id} data={data} type={kind === "if" ? "ifStep" : "switchStep"} selected={false} dragging={false} isConnectable={false} zIndex={1} selectable draggable deletable positionAbsoluteX={0} positionAbsoluteY={0} />;
    })}</ReactFlowProvider>
  </I18nContext.Provider>);
  return { steps, original, nodes: screen.getAllByRole("group", { name: `Open step ${raw}` }) };
}

describe.each(["en", "lt"] as const)("%s IF and SWITCH generated summaries", (locale) => {
  it("localizes only explicit generated IF defaults and keeps identical unkeyed text raw", () => {
    const raw = "conditional branch";
    const { steps, original, nodes } = mountBranchSummary("if", raw, locale, "react.workflows.if_default_summary");
    expect(within(nodes[0]).getAllByText(locale === "lt" ? "sąlyginis šakojimas" : raw)).toHaveLength(locale === "lt" ? 1 : 2);
    expect(within(nodes[1]).getAllByText(raw)).toHaveLength(2);
    expect(within(nodes[0]).queryByText("owner-if-key")).not.toBeInTheDocument();
    expect(within(nodes[1]).getByRole("img", { name: "If" })).toBeInTheDocument();
    expect(JSON.stringify(steps)).toBe(original);
  });

  it.each(["state.owner_flag", "conditional branch", "{'operator': 'always_true'}"])("preserves unkeyed authored IF condition %s", (raw) => {
    const { steps, original, nodes } = mountBranchSummary("if", raw, locale);
    expect(within(nodes[0]).getAllByText(raw)).toHaveLength(2);
    expect(within(nodes[1]).getAllByText(raw)).toHaveLength(2);
    expect(JSON.stringify(steps)).toBe(original);
  });

  it.each([
    { selector: "state.owner_choice", count: 0, raw: "state.owner_choice (0 cases)", lt: "state.owner_choice (atvejų skaičius: 0)", key: "react.workflows.switch_selector_summary" },
    { selector: "state.owner_choice", count: 1, raw: "state.owner_choice (1 cases)", lt: "state.owner_choice (atvejų skaičius: 1)", key: "react.workflows.switch_selector_summary" },
    { selector: "state.owner_choice", count: 3, raw: "state.owner_choice (3 cases)", lt: "state.owner_choice (atvejų skaičius: 3)", key: "react.workflows.switch_selector_summary" },
    { selector: "", count: 0, raw: "0 switch cases", lt: "Pasirinkimo atvejų skaičius: 0", key: "react.workflows.switch_cases_summary" },
    { selector: "", count: 1, raw: "1 switch cases", lt: "Pasirinkimo atvejų skaičius: 1", key: "react.workflows.switch_cases_summary" },
    { selector: "", count: 3, raw: "3 switch cases", lt: "Pasirinkimo atvejų skaičius: 3", key: "react.workflows.switch_cases_summary" },
  ])("localizes explicit SWITCH $key count $count without rewriting identical owner summaries", ({ selector, count, raw, lt, key }) => {
    const { steps, original, nodes } = mountBranchSummary("switch", raw, locale, key, { selector, case_count: count });
    expect(within(nodes[0]).getAllByText(locale === "lt" ? lt : raw)).toHaveLength(locale === "lt" ? 1 : 2);
    expect(within(nodes[1]).getAllByText(raw)).toHaveLength(2);
    expect(within(nodes[0]).queryByText("owner-switch-key")).not.toBeInTheDocument();
    expect(within(nodes[1]).getByRole("img", { name: "Switch" })).toBeInTheDocument();
    expect(JSON.stringify(steps)).toBe(original);
  });
});

it("shows the server-projected If expression on the Builder card", () => {
  const summary = "state.line.qty ≤ 0";
  const step = { id: "route", step_id: "route", display_name: "Check line quantity", kind: "if", config_summary: summary, ports: [] };
  const data: StepNodeData = { step, automationRules: [], validationIssues: [], isEntry: false, local: false, unreachable: false, onAutomation: () => undefined, onOpenStep: () => undefined, onEditStep: () => undefined };
  render(<ReactFlowProvider><IfStepNode id="route" data={data} type="ifStep" selected={false} dragging={false} isConnectable={false} zIndex={1} selectable draggable deletable positionAbsoluteX={0} positionAbsoluteY={0} /></ReactFlowProvider>);
  const card = screen.getByRole("group", { name: "Open step Check line quantity" });
  expect(within(card).getByText(summary, { exact: true })).toBeVisible();
  expect(card).not.toHaveTextContent("{'value': 0");
});

it.each([
  { kind: "stop_success", raw: "stop workflow (success)", key: "react.workflows.stop_workflow_success_summary", translated: "Užbaigti darbo eigą sėkmingai" },
  { kind: "stop_fail", raw: "stop workflow (failure)", key: "react.workflows.stop_workflow_failure_summary", translated: "Užbaigti darbo eigą nesėkmingai" },
])("localizes explicitly generated $kind summary while retaining identical unkeyed owner text and IDs", ({ kind, raw, key, translated }) => {
  const messages: Record<string, string> = { [key]: translated };
  const mountNode = (id: string, keyed: boolean) => {
    const step = {
      id,
      step_id: "owner-step-key",
      display_name: raw,
      kind,
      config_summary: raw,
      ...(keyed ? { config_summary_key: key } : {}),
    };
    const data: StepNodeData = {
      step,
      automationRules: [],
      validationIssues: [],
      isEntry: false,
      local: false,
      unreachable: false,
      onAutomation: () => undefined,
      onOpenStep: () => undefined,
      onEditStep: () => undefined,
    };
    return <TerminalStepNode id={id} data={data} type="end" selected={false} dragging={false} isConnectable={false} zIndex={1} selectable draggable deletable positionAbsoluteX={0} positionAbsoluteY={0} />;
  };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (messageKey, text = "", params) => interpolate(messages[messageKey] || text, params) }}>
    <ReactFlowProvider>{mountNode("generated-stop-id", true)}{mountNode("owner-stop-id", false)}</ReactFlowProvider>
  </I18nContext.Provider>);

  const nodes = screen.getAllByRole("group", { name: `Open step ${raw}` });
  expect(within(nodes[0]).getByText(translated)).toBeInTheDocument();
  expect(within(nodes[0]).getByText(raw)).toBeInTheDocument();
  expect(within(nodes[0]).getByRole("img", { name: kind === "stop_success" ? "Stop · success" : "Stop · fail" })).toBeInTheDocument();
  expect(within(nodes[1]).queryByText(translated)).not.toBeInTheDocument();
  expect(within(nodes[1]).getAllByText(raw)).toHaveLength(2);
  expect(within(nodes[0]).queryByText("owner-step-key")).not.toBeInTheDocument();
  expect(within(nodes[1]).queryByText("owner-step-key")).not.toBeInTheDocument();
});

const transformCases = [
  { source: "state.owner_input", target: "state.owner_result", count: 0, key: "react.workflows.transform_summary", raw: "state.owner_input -> state.owner_result (0 fields)", lt: "state.owner_input -> state.owner_result (laukų skaičius: 0)" },
  { source: "state.owner_input", target: "state.owner_result", count: 1, key: "react.workflows.transform_summary", raw: "state.owner_input -> state.owner_result (1 fields)", lt: "state.owner_input -> state.owner_result (laukų skaičius: 1)" },
  { source: "state.owner_input", target: "state.owner_result", count: 3, key: "react.workflows.transform_summary", raw: "state.owner_input -> state.owner_result (3 fields)", lt: "state.owner_input -> state.owner_result (laukų skaičius: 3)" },
  { source: "", target: "state.owner_result", count: 1, key: "react.workflows.transform_summary_default_source", raw: "source -> state.owner_result (1 fields)", lt: "šaltinis -> state.owner_result (laukų skaičius: 1)" },
  { source: "state.owner_input", target: "", count: 3, key: "react.workflows.transform_summary_default_target", raw: "state.owner_input -> result (3 fields)", lt: "state.owner_input -> rezultatas (laukų skaičius: 3)" },
  { source: "", target: "", count: 0, key: "react.workflows.transform_summary_defaults", raw: "source -> result (0 fields)", lt: "šaltinis -> rezultatas (laukų skaičius: 0)" },
];

describe.each(["en", "lt"] as const)("%s generated transform canvas summary", (locale) => {
  it.each(transformCases)("renders $key with $count fields and preserves unkeyed owner content", ({ source, target, count, key, raw, lt }) => {
    const messages: Record<string, string> = locale === "lt" ? {
      "react.workflows.transform_summary": "{source} -> {target} (laukų skaičius: {field_count})",
      "react.workflows.transform_summary_default_source": "šaltinis -> {target} (laukų skaičius: {field_count})",
      "react.workflows.transform_summary_default_target": "{source} -> rezultatas (laukų skaičius: {field_count})",
      "react.workflows.transform_summary_defaults": "šaltinis -> rezultatas (laukų skaičius: {field_count})",
    } : {
      "react.workflows.transform_summary": "{source} -> {target} ({field_count} fields)",
      "react.workflows.transform_summary_default_source": "source -> {target} ({field_count} fields)",
      "react.workflows.transform_summary_default_target": "{source} -> result ({field_count} fields)",
      "react.workflows.transform_summary_defaults": "source -> result ({field_count} fields)",
    };
    const steps = [true, false].map((keyed, index) => ({
      id: `owner-transform-${index}`, step_id: "owner-transform-key", display_name: raw,
      kind: "transform", config_summary: raw,
      ...(keyed ? { config_summary_key: key, config_summary_params: { source, target, field_count: count } } : {}),
    }));
    const original = JSON.stringify(steps);
    render(<I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (messageKey, text = "", params) => interpolate(messages[messageKey] || text, params) }}>
      <ReactFlowProvider>{steps.map((step) => {
        const data: StepNodeData = { step, automationRules: [], validationIssues: [], isEntry: false, local: false, unreachable: false, onAutomation: () => undefined, onOpenStep: () => undefined, onEditStep: () => undefined };
        return <GenericStepNode key={step.id} id={step.id} data={data} type="action" selected={false} dragging={false} isConnectable={false} zIndex={1} selectable draggable deletable positionAbsoluteX={0} positionAbsoluteY={0} />;
      })}</ReactFlowProvider>
    </I18nContext.Provider>);

    const nodes = screen.getAllByRole("group", { name: `Open step ${raw}` });
    expect(within(nodes[0]).getAllByText(locale === "lt" ? lt : raw)).toHaveLength(locale === "lt" ? 1 : 2);
    expect(within(nodes[0]).queryByText("owner-transform-key")).not.toBeInTheDocument();
    expect(within(nodes[0]).getByRole("img", { name: "Transform" })).toBeInTheDocument();
    expect(within(nodes[1]).getAllByText(raw)).toHaveLength(2);
    if (locale === "lt") expect(within(nodes[1]).queryByText(lt)).not.toBeInTheDocument();
    expect(within(nodes[1]).queryByText("owner-transform-key")).not.toBeInTheDocument();
    expect(JSON.stringify(steps)).toBe(original);
  });
});
