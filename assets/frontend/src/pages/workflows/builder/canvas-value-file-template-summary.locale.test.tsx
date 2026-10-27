import { render, screen, within } from "@testing-library/react";
import { ReactFlowProvider } from "@xyflow/react";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { GenericStepNode } from "@/pages/workflows/builder/canvas-step-nodes";
import type { StepNodeData } from "@/pages/workflows/builder/layout";

const cases = [
  {
    "kind": "set_variable",
    "raw": "variable = null",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "null"
    },
    "lt": "kintamasis = null"
  },
  {
    "kind": "set_variable",
    "raw": "variable = true",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "true"
    },
    "lt": "kintamasis = true"
  },
  {
    "kind": "set_variable",
    "raw": "variable = false",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "false"
    },
    "lt": "kintamasis = false"
  },
  {
    "kind": "set_variable",
    "raw": "variable = 12.5",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "12.5"
    },
    "lt": "kintamasis = 12.5"
  },
  {
    "kind": "set_variable",
    "raw": "variable = owner value",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "owner value"
    },
    "lt": "kintamasis = owner value"
  },
  {
    "kind": "set_variable",
    "raw": "variable = \"\"",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "\"\""
    },
    "lt": "kintamasis = \"\""
  },
  {
    "kind": "set_variable",
    "raw": "variable = [true, null, owner, ...]",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "[true, null, owner, ...]"
    },
    "lt": "kintamasis = [true, null, owner, ...]"
  },
  {
    "kind": "set_variable",
    "raw": "variable = {owner: true, selector: state.source, nested: [null, owner]}",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "{owner: true, selector: state.source, nested: [null, owner]}"
    },
    "lt": "kintamasis = {owner: true, selector: state.source, nested: [null, owner]}"
  },
  {
    "kind": "set_variable",
    "raw": "variable = true",
    "key": null,
    "params": null
  },
  {
    "kind": "set_variable",
    "raw": "state.owner = {source: state.inputs.owner}",
    "key": null,
    "params": null
  },
  {
    "kind": "set_variable",
    "raw": "owner_first = true",
    "key": null,
    "params": null
  },
  {
    "kind": "set_variable",
    "raw": "owner_first = true; owner_second = null; owner_third = [1, 2, 3, ...]",
    "key": null,
    "params": null
  },
  {
    "kind": "set_variable",
    "raw": "owner_first = true; owner_second = null; owner_third = {source: state.owner}; +1 more",
    "key": "react.workflows.set_variable_more_summary",
    "params": {
      "assignments": "owner_first = true; owner_second = null; owner_third = {source: state.owner}",
      "extra_count": 1
    },
    "lt": "owner_first = true; owner_second = null; owner_third = {source: state.owner}; +1 papildomų"
  },
  {
    "kind": "set_variable",
    "raw": "z_last = first; a_first = false; variable = more; +3 more",
    "key": "react.workflows.set_variable_more_summary",
    "params": {
      "assignments": "z_last = first; a_first = false; variable = more",
      "extra_count": 3
    },
    "lt": "z_last = first; a_first = false; variable = more; +3 papildomų"
  },
  {
    "kind": "set_variable",
    "raw": "state.owner = 0",
    "key": null,
    "params": null
  },
  {
    "kind": "set_variable",
    "raw": "variable = owner",
    "key": "react.workflows.set_variable_default_summary",
    "params": {
      "value": "owner"
    },
    "lt": "kintamasis = owner"
  },
  {
    "kind": "file_from_base64",
    "raw": "state.owner -> file owner-report.pdf",
    "key": "react.workflows.base64_file_named_summary",
    "params": {
      "source": "state.owner",
      "filename": "owner-report.pdf"
    },
    "lt": "state.owner -> failas owner-report.pdf"
  },
  {
    "kind": "file_from_base64",
    "raw": "state.owner -> file",
    "key": "react.workflows.base64_file_summary",
    "params": {
      "source": "state.owner",
      "filename": ""
    },
    "lt": "state.owner -> failas"
  },
  {
    "kind": "file_from_base64",
    "raw": "base64 -> file file",
    "key": "react.workflows.base64_file_named_summary",
    "params": {
      "source": "base64",
      "filename": "file"
    },
    "lt": "base64 -> failas file"
  },
  {
    "kind": "file_from_base64",
    "raw": "base64 -> file",
    "key": "react.workflows.base64_file_summary",
    "params": {
      "source": "base64",
      "filename": ""
    },
    "lt": "base64 -> failas"
  },
  {
    "kind": "file_from_base64",
    "raw": "base64 -> file {{ owner.filename }}.txt",
    "key": "react.workflows.base64_file_named_summary",
    "params": {
      "source": "base64",
      "filename": "{{ owner.filename }}.txt"
    },
    "lt": "base64 -> failas {{ owner.filename }}.txt"
  },
  {
    "kind": "template",
    "raw": "markdown template -> state.owner",
    "key": "react.workflows.template_summary",
    "params": {
      "format": "markdown",
      "target": "state.owner"
    },
    "lt": "markdown šablonas -> state.owner"
  },
  {
    "kind": "template",
    "raw": "html template -> text",
    "key": "react.workflows.template_summary",
    "params": {
      "format": "html",
      "target": "text"
    },
    "lt": "html šablonas -> text"
  },
  {
    "kind": "template",
    "raw": "text template -> state.owner",
    "key": "react.workflows.template_summary",
    "params": {
      "format": "text",
      "target": "state.owner"
    },
    "lt": "text šablonas -> state.owner"
  },
  {
    "kind": "template",
    "raw": "text template -> text",
    "key": "react.workflows.template_default_target_summary",
    "params": {
      "format": "text",
      "target": ""
    },
    "lt": "text šablonas -> tekstas"
  },
  {
    "kind": "template",
    "raw": " owner-format  template -> text",
    "key": "react.workflows.template_default_target_summary",
    "params": {
      "format": " owner-format ",
      "target": ""
    },
    "lt": " owner-format  šablonas -> tekstas"
  },
  {
    "kind": "template",
    "raw": "text template -> template",
    "key": "react.workflows.template_summary",
    "params": {
      "format": "text",
      "target": "template"
    },
    "lt": "text šablonas -> template"
  }
];
const enMessages: Record<string, string> = {
  "react.workflows.set_variable_default_summary": "variable = {value}",
  "react.workflows.set_variable_more_summary": "{assignments}; +{extra_count} more",
  "react.workflows.base64_file_named_summary": "{source} -> file {filename}",
  "react.workflows.base64_file_summary": "{source} -> file",
  "react.workflows.template_summary": "{format} template -> {target}",
  "react.workflows.template_default_target_summary": "{format} template -> text"
};
const ltMessages: Record<string, string> = {
  "react.workflows.set_variable_default_summary": "kintamasis = {value}",
  "react.workflows.set_variable_more_summary": "{assignments}; +{extra_count} papildomų",
  "react.workflows.base64_file_named_summary": "{source} -> failas {filename}",
  "react.workflows.base64_file_summary": "{source} -> failas",
  "react.workflows.template_summary": "{format} šablonas -> {target}",
  "react.workflows.template_default_target_summary": "{format} šablonas -> tekstas"
};

describe.each(["en", "lt"] as const)("%s variable/file/template display prose", (locale) => {
  it.each(cases)("renders $kind $raw with explicit provenance only", ({ kind, key, raw, lt, params }) => {
    const messages = locale === "lt" ? ltMessages : enMessages;
    const steps = [true, false].map((keyed, index) => ({
      id: `owner-${kind}-${index}`, step_id: "owner-step-key", display_name: raw,
      kind, config_summary: raw,
      ...(keyed && key && params ? { config_summary_key: key, config_summary_params: params } : {}),
    }));
    const original = JSON.stringify(steps);
    render(<I18nContext.Provider value={{ ...fallbackContext, locale, messages, t: (messageKey, text = "", values) => interpolate(messages[messageKey] || text, values) }}>
      <ReactFlowProvider>{steps.map((step) => {
        const data: StepNodeData = { step, automationRules: [], validationIssues: [], isEntry: false, local: false, unreachable: false, onAutomation: () => undefined, onOpenStep: () => undefined, onEditStep: () => undefined };
        return <GenericStepNode key={step.id} id={step.id} data={data} type="action" selected={false} dragging={false} isConnectable={false} zIndex={1} selectable draggable deletable positionAbsoluteX={0} positionAbsoluteY={0} />;
      })}</ReactFlowProvider>
    </I18nContext.Provider>);
    const nodes = screen.getAllByRole("group", { name: /^Open step / });
    expect(nodes).toHaveLength(2);
    const translated = locale === "lt" && key && lt ? lt : raw;
    expect(within(nodes[0]).getAllByText(translated, { exact: true, normalizer: (text) => text })).toHaveLength(translated === raw ? 2 : 1);
    expect(within(nodes[0]).queryByText("owner-step-key")).not.toBeInTheDocument();
    expect(within(nodes[0]).getByRole("img", { name: kind === "set_variable" ? "Set variable" : kind === "file_from_base64" ? "File from base64" : "Template" })).toBeInTheDocument();
    expect(within(nodes[1]).getAllByText(raw, { exact: true, normalizer: (text) => text })).toHaveLength(2);
    if (translated !== raw) expect(within(nodes[1]).queryByText(translated, {exact: true, normalizer: (text) => text})).not.toBeInTheDocument();
    expect(JSON.stringify(steps)).toBe(original);
  });
});
