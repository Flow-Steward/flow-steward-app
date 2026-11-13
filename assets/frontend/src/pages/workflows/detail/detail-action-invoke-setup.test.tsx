import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { fetchJson } from "@/lib/http/client";
import { stepExtensionId } from "@/pages/workflows/shared/workflow-step-extension";

import { actionInvokeConfigForOperation, actionInvokeRef } from "./detail-action-invoke";
import { ActionInvokeSetup } from "./detail-action-invoke-setup";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));

const ACTION = { extension_id: "acme.amazon-pro", install_id: "pi_addon", action_id: "orders.create_full" };

function operation(connections: Array<Record<string, unknown>>) {
  return {
    operation_id: "orders.create_full",
    action_id: "orders.create_full",
    display_name: "Create order with shipment",
    parameters: [{ name: "order", type: "object", required: true }],
    returns: [{ name: "order_id", type: "string" }],
    implementation: {
      kind: "workflow",
      workflow_id: "extension.pi_addon.create_full",
      installs: { "acme.amazon": "pi_base" },
      slots: [{ slot: "amazon", extension_id: "acme.amazon", install_id: "pi_base", connections }],
    },
  };
}

const TWO_ACCOUNTS = [
  { connection_id: "conn-1", display_name: "UK store" },
  { connection_id: "conn-2", display_name: "DE store", verified_email: "de@example.test" },
];

function renderSetup(stepKindConfig: Record<string, unknown>, connections = TWO_ACCOUNTS) {
  vi.mocked(fetchJson).mockResolvedValue({ install_id: "pi_addon", operations: [operation(connections)] });
  const updateStepKindConfig = vi.fn();
  const onChooseAnotherAction = vi.fn();
  const mappingEditors: string[] = [];
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ActionInvokeSetup ctx={{
        stepKindConfig,
        selectedProjectId: "project-1",
        availableWorkflowVariableSuggestions: [],
        updateStepKindConfig,
        onChooseAnotherAction,
        renderWorkflowContractMappingEditor: (field, _title, rows) => {
          mappingEditors.push(`${field}:${rows.map((row) => String(row.name)).join(",")}`);
          return null;
        },
      }} />
    </QueryClientProvider>,
  );
  return { updateStepKindConfig, onChooseAnotherAction, mappingEditors };
}

function chooseOption(control: HTMLElement, label: string) {
  if (control.getAttribute("aria-expanded") !== "true") {
    fireEvent.mouseDown(control);
    fireEvent.click(control);
  }
  const options = screen.getByRole("listbox", { name: `${control.getAttribute("aria-label")} options` });
  fireEvent.click(within(options).getByRole("option", { name: label }));
}

afterEach(() => vi.mocked(fetchJson).mockReset());

it("reads an action Invoke as its action and a plain Invoke as none", () => {
  expect(actionInvokeRef({ workflow_id: "report" })).toBeNull();
  expect(actionInvokeRef({ action: ACTION })).toEqual({
    extensionId: "acme.amazon-pro",
    installId: "pi_addon",
    actionId: "orders.create_full",
  });
  expect(stepExtensionId({ step_kind: "invoke_workflow", override: { invoke_workflow: { action: ACTION } } }))
    .toBe("acme.amazon-pro");
  expect(stepExtensionId({ step_kind: "invoke_workflow", override: { invoke_workflow: { workflow_id: "report" } } }))
    .toBe("");
});

it("saves a composite action as an Invoke of its workflow", () => {
  const config = actionInvokeConfigForOperation(
    operation([TWO_ACCOUNTS[0]]),
    "acme.amazon-pro",
    "pi_addon",
    { order_id: "state.order_id" },
  );

  expect(config).toEqual({
    workflow_id: "extension.pi_addon.create_full",
    action: ACTION,
    installs: { "acme.amazon": "pi_base" },
    // The only fitting connection is chosen for the person.
    connections: { amazon: { connection_id: "conn-1" } },
    input_mapping: {},
    output_mapping: { order_id: "state.order_id" },
  });
  expect(actionInvokeConfigForOperation(operation(TWO_ACCOUNTS), "acme.amazon-pro", "pi_addon"))
    .toMatchObject({ connections: {} });
  // An ordinary action stays a connector step.
  expect(actionInvokeConfigForOperation({ operation_id: "orders.note" }, "acme.amazon-pro", "pi_addon")).toBeNull();
});

it("shows the action, its connection slot and its mappings", async () => {
  const { updateStepKindConfig, onChooseAnotherAction, mappingEditors } = renderSetup({
    workflow_id: "extension.pi_addon.create_full",
    action: ACTION,
    installs: { "acme.amazon": "pi_base" },
    connections: {},
  });

  await waitFor(() => expect(screen.getByText("Create order with shipment")).toBeInTheDocument());
  expect(vi.mocked(fetchJson).mock.calls[0][0]).toContain("install_id=pi_addon&project_id=project-1");
  const connection = screen.getByRole("combobox", { name: "Connection: amazon" });
  expect(connection).toBeRequired();
  expect(connection).toHaveAttribute("aria-invalid", "true");
  chooseOption(connection, "DE store (de@example.test)");
  expect(updateStepKindConfig).toHaveBeenCalledWith("invoke_workflow", {
    connections: { amazon: { connection_id: "conn-2" } },
  });
  await waitFor(() => expect(mappingEditors).toContain("input_mapping:order"));
  expect(mappingEditors).toContain("output_mapping:order_id");
  fireEvent.click(screen.getByRole("button", { name: "Choose action" }));
  expect(onChooseAnotherAction).toHaveBeenCalledTimes(1);
});

it("follows the release when it moves the workflow or the dependency installs", async () => {
  const { updateStepKindConfig } = renderSetup({
    workflow_id: "extension.pi_addon.old",
    action: ACTION,
    installs: {},
    connections: { amazon: { connection_id: "conn-1" } },
  });

  await waitFor(() => expect(updateStepKindConfig).toHaveBeenCalledWith("invoke_workflow", {
    installs: { "acme.amazon": "pi_base" },
    workflow_id: "extension.pi_addon.create_full",
  }));
});

it("says when the action is gone", async () => {
  vi.mocked(fetchJson).mockResolvedValue({ install_id: "pi_addon", operations: [] });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ActionInvokeSetup ctx={{
        stepKindConfig: { workflow_id: "extension.pi_addon.create_full", action: ACTION },
        availableWorkflowVariableSuggestions: [],
        updateStepKindConfig: vi.fn(),
        renderWorkflowContractMappingEditor: () => null,
      }} />
    </QueryClientProvider>,
  );

  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("This extension action is no longer available."));
});

it("names a slot's connection by its extension, and by the slot when two share one", async () => {
  const twoSlots = {
    ...operation(TWO_ACCOUNTS),
    implementation: {
      kind: "workflow",
      workflow_id: "extension.pi_addon.create_full",
      installs: { "acme.amazon": "pi_base", "acme.books": "pi_books" },
      slots: [
        { slot: "source", extension_id: "acme.amazon", extension_name: "Amazon", connections: TWO_ACCOUNTS },
        { slot: "target", extension_id: "acme.amazon", extension_name: "Amazon", connections: TWO_ACCOUNTS },
        { slot: "ledger", extension_id: "acme.books", extension_name: "Books", connections: [] },
      ],
    },
  };
  vi.mocked(fetchJson).mockResolvedValue({ install_id: "pi_addon", operations: [twoSlots] });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ActionInvokeSetup ctx={{
        stepKindConfig: { workflow_id: "extension.pi_addon.create_full", action: ACTION, installs: twoSlots.implementation.installs },
        availableWorkflowVariableSuggestions: [],
        updateStepKindConfig: vi.fn(),
        renderWorkflowContractMappingEditor: () => null,
      }} />
    </QueryClientProvider>,
  );

  await waitFor(() => expect(screen.getByRole("combobox", { name: "Connection: Amazon (source)" })).toBeInTheDocument());
  expect(screen.getByRole("combobox", { name: "Connection: Amazon (target)" })).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Connection: Books" })).toBeInTheDocument();
});
