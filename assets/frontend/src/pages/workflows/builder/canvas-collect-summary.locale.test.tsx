import { render, screen, within } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { GenericStepNode } from "@/pages/workflows/builder/canvas-step-nodes";
import type { StepNodeData } from "@/pages/workflows/builder/layout";

// The server sends a Collect summary key with its parameters; the raw text is the owner's fallback.
const cases = [
  {
    key: "react.workflows.collect_summary_append",
    raw: "state.order.email → emails",
    lt: "state.order.email → emails",
    params: { source: "state.order.email", target: "emails" },
  },
  {
    key: "react.workflows.collect_summary_set_key",
    raw: "by_email[key] = state.order",
    lt: "by_email[raktas] = state.order",
    params: { source: "state.order", target: "by_email" },
  },
];

const enMessages: Record<string, string> = {
  "react.workflows.collect_summary_append": "{source} → {target}",
  "react.workflows.collect_summary_set_key": "{target}[key] = {source}",
};
const ltMessages: Record<string, string> = {
  "react.workflows.collect_summary_append": "{source} → {target}",
  "react.workflows.collect_summary_set_key": "{target}[raktas] = {source}",
  "react.workflows.step_kind_collect": "Rinkti",
};

describe.each(["en", "lt"] as const)("%s Collect summary on the canvas", (locale) => {
  it.each(cases)("renders $key without changing the owner's step", ({ key, raw, lt, params }) => {
    const messages = locale === "lt" ? ltMessages : enMessages;
    const steps = [true, false].map((keyed, index) => ({
      id: `owner-collect-${index}`, step_id: "owner-collect-key", display_name: `Collect ${index}`,
      kind: "collect", config_summary: raw,
      ...(keyed ? { config_summary_key: key, config_summary_params: params } : {}),
    }));
    const original = JSON.stringify(steps);
    render(<I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (messageKey, text = "", values) => interpolate(messages[messageKey] || text, values) }}>
      <ReactFlowProvider>{steps.map((step) => {
        const data: StepNodeData = { step, automationRules: [], validationIssues: [], isEntry: false, local: false, unreachable: false, onAutomation: () => undefined, onOpenStep: () => undefined, onEditStep: () => undefined };
        return <GenericStepNode key={step.id} id={step.id} data={data} type="action" selected={false} dragging={false} isConnectable={false} zIndex={1} selectable draggable deletable positionAbsoluteX={0} positionAbsoluteY={0} />;
      })}</ReactFlowProvider>
    </I18nContext.Provider>);
    const keyed = screen.getByRole("group", { name: "Open step Collect 0" });
    const unkeyed = screen.getByRole("group", { name: "Open step Collect 1" });
    expect(within(keyed).getByText(locale === "lt" ? lt : raw)).toBeInTheDocument();
    expect(within(unkeyed).getByText(raw)).toBeInTheDocument();
    expect(JSON.stringify(steps)).toBe(original);
  });
});
