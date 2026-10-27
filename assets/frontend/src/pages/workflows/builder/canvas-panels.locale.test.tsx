import { fireEvent, render, screen, within } from "@testing-library/react";

import { fallbackContext, I18nContext } from "@/lib/i18n/context";
import { interpolate } from "@/lib/i18n/interpolate";
import { ContractHeader, ValidationPanel } from "@/pages/workflows/builder/canvas-panels";
import type { WorkflowCanvasGraph, WorkflowCanvasIssue } from "@/pages/workflows/shared/api";
import { WorkflowDrawerShell } from "@/pages/workflows/detail/detail-drawer-shell";

const messages: Record<string, string> = {
  "react.workflows.validation_error_one": "Klaidų: {count}",
  "react.workflows.validation_errors_count": "Klaidų: {count}",
  "react.workflows.validation_warning_one": "Įspėjimų: {count}",
  "react.workflows.validation_warnings_count": "Įspėjimų: {count}",
  "events.severity.error": "Klaida",
  "events.severity.warning": "Įspėjimas",
  "react.workflows.execution_bindings_missing": "Prieš įjungiant arba paleidžiant darbo eigą, joje turi būti bent vienas tikras vykdymo žingsnis (agentas, jungtis, kita darbo eiga, HTTP užklausa, duomenų rinkinio užklausa arba transformacija).",
};

it.each([
  { errors: 1, warnings: 0, expected: "Klaidų: 1" },
  { errors: 2, warnings: 0, expected: "Klaidų: 2" },
  { errors: 0, warnings: 1, expected: "Įspėjimų: 1" },
  { errors: 11, warnings: 2, expected: "Klaidų: 11 · Įspėjimų: 2" },
])("localizes the validation toolbar with $errors errors and $warnings warnings", ({ errors, warnings, expected }) => {
  const issues: WorkflowCanvasIssue[] = [
    ...Array.from({ length: errors }, (_, index): WorkflowCanvasIssue => ({ scope: "workflow", scope_id: "fixture", severity: "error", title: `Fixture error ${index}` })),
    ...Array.from({ length: warnings }, (_, index): WorkflowCanvasIssue => ({ scope: "workflow", scope_id: "fixture", severity: "warning", title: `Fixture warning ${index}` })),
  ];
  const graph: WorkflowCanvasGraph = { workflow: { id: "fixture", display_name: "Owner-authored workflow", enabled: true, callable_by_parent: false }, interface_contract: {}, output_contract: {}, groups: [], steps: [], edges: [], validation_issues: issues, automation_rules: [] };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><ContractHeader graph={graph} onOpenWorkflowAutomation={() => undefined} onOpenWorkflowSettings={() => undefined} onSearchChange={() => undefined} onSearchKeyDown={() => undefined} onValidationClick={() => undefined} searchQuery="" showSearchResults={false} /></I18nContext.Provider>);
  expect(screen.getByRole("button", { name: expected })).toBeInTheDocument();
  expect(screen.getByText("Owner-authored workflow")).toBeInTheDocument();
});

it.each(["panel", "drawer"] as const)("localizes explicit execution-readiness message metadata in the %s and preserves identical unkeyed text", (surface) => {
  const raw = "Workflow must include at least one real execution step (agent, connector, child workflow, HTTP request, dataset query, or transform) before it can be enabled or run";
  const keyed = { scope: "workflow", scope_id: "workflow", severity: "error", code: "workflow.execution_bindings_missing", title: raw, message: raw, title_key: "react.workflows.execution_bindings_missing", title_params: {}, message_key: "react.workflows.execution_bindings_missing", message_params: {} } as WorkflowCanvasIssue;
  const owner = { scope: "workflow", scope_id: "owner-workflow", severity: "error", code: keyed.code, title: raw, message: raw } as WorkflowCanvasIssue;
  const graph: WorkflowCanvasGraph = { workflow: { id: "owner-workflow", display_name: "Owner workflow", enabled: true, callable_by_parent: false }, interface_contract: {}, output_contract: {}, groups: [], steps: [], edges: [], validation_issues: [keyed, owner], automation_rules: [] };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}>
    {surface === "panel" ? <ValidationPanel graph={graph} issues={[keyed, owner]} onIssueClick={() => undefined} /> : <><WorkflowDrawerShell ctx={{ drawerOpen: true, drawerMode: "workflow-settings", selectedCanvasIssue: keyed }}><span>Keyed content</span></WorkflowDrawerShell><WorkflowDrawerShell ctx={{ drawerOpen: true, drawerMode: "workflow-settings", selectedCanvasIssue: owner }}><span>Owner content</span></WorkflowDrawerShell></>}
  </I18nContext.Provider>);
  expect(screen.getAllByText(messages["react.workflows.execution_bindings_missing"])).toHaveLength(surface === "panel" ? 2 : 1);
  expect(surface === "panel" ? screen.getAllByText(raw) : [screen.getByText(raw)]).toHaveLength(surface === "panel" ? 2 : 1);
  expect(keyed.message).toBe(raw);
  expect(owner).not.toHaveProperty("message_key");
});

it.each([
  { severity: "error" as const, expected: "Klaida" },
  { severity: "warning" as const, expected: "Įspėjimas" },
])("localizes the $severity severity badge without rewriting the diagnostic payload", ({ severity, expected }) => {
  const issue: WorkflowCanvasIssue = { scope: "workflow", scope_id: "owner-workflow", severity, title: "Owner diagnostic", message: severity };
  const graph: WorkflowCanvasGraph = { workflow: { id: "owner-workflow", display_name: "Owner workflow", enabled: true, callable_by_parent: false }, interface_contract: {}, output_contract: {}, groups: [], steps: [], edges: [], validation_issues: [issue], automation_rules: [] };
  const onIssueClick = vi.fn();
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages, t: (key, text = "", params) => interpolate(messages[key] || text, params) }}><ValidationPanel graph={graph} issues={[issue]} onIssueClick={onIssueClick} /></I18nContext.Provider>);
  const button = screen.getByRole("button", { name: /Owner diagnostic/ });
  expect(within(button).getByText(expected)).toBeInTheDocument();
  expect(within(button).getByText(severity)).toBeInTheDocument();
  fireEvent.click(button);
  expect(onIssueClick).toHaveBeenCalledWith(issue);
  expect(issue).toMatchObject({ severity, title: "Owner diagnostic", message: severity });
});

it.each([
  { key: "react.workflows.diagnostic_action_workflow_contract", fallback: "Open workflow settings and update the workflow contract.", translated: "Atverkite darbo eigos nustatymus ir atnaujinkite jos sutartį." },
  { key: "react.workflows.diagnostic_action_io_contract", fallback: "Open this phase or step and align its input/output contract.", translated: "Atverkite šį etapą arba žingsnį ir suderinkite jo įvesties bei išvesties sutartį." },
  { key: "react.workflows.diagnostic_action_branch_condition", fallback: "Open the affected router step and fix the branch condition or target.", translated: "Atverkite susijusį maršrutizavimo žingsnį ir pataisykite šakos sąlygą arba tikslą." },
  { key: "react.workflows.diagnostic_action_configuration", fallback: "Open the affected object and update the highlighted configuration.", translated: "Atverkite susijusį objektą ir atnaujinkite paryškintą konfigūraciją." },
])("localizes explicit generated action metadata $key without translating unkeyed diagnostics", ({ key, fallback, translated }) => {
  const issues = [
    { scope: "workflow", scope_id: "workflow", severity: "error", title: "Owner generated diagnostic", message: "Owner generated message", suggested_action: fallback, suggested_action_key: key },
    { scope: "workflow", scope_id: "workflow", severity: "warning", title: "Owner unkeyed diagnostic", message: "Owner raw message", suggested_action: fallback },
  ] as WorkflowCanvasIssue[];
  const graph: WorkflowCanvasGraph = { workflow: { id: "owner-workflow", display_name: "Owner workflow", enabled: true, callable_by_parent: false }, interface_contract: {}, output_contract: {}, groups: [], steps: [], edges: [], validation_issues: issues, automation_rules: [] };
  const actionMessages = { ...messages, [key]: translated };
  render(<I18nContext.Provider value={{ ...fallbackContext, locale: "lt", messages: actionMessages, t: (messageKey, text = "", params) => interpolate(actionMessages[messageKey] || text, params) }}><ValidationPanel graph={graph} issues={issues} onIssueClick={() => undefined} /></I18nContext.Provider>);
  const generated = screen.getByRole("button", { name: /Owner generated diagnostic/ });
  expect(within(generated).getByText(translated)).toBeInTheDocument();
  expect(within(generated).getByText("Owner generated message")).toBeInTheDocument();
  const unkeyed = screen.getByRole("button", { name: /Owner unkeyed diagnostic/ });
  expect(within(unkeyed).getByText(fallback)).toBeInTheDocument();
  expect(within(unkeyed).getByText("Owner raw message")).toBeInTheDocument();
});
