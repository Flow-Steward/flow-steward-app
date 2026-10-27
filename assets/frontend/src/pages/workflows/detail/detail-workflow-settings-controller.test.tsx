import { act, renderHook, waitFor } from "@testing-library/react";

import { useWorkflowSettingsController, useWorkflowSettingsHydration } from "./detail-workflow-settings-controller";

const commonDefault = { type: "object", additionalProperties: false };

afterEach(() => vi.restoreAllMocks());

it.each([
  { label: "an unchanged empty schema", stored: {}, edit: undefined, expected: {} },
  { label: "an authored common default", stored: commonDefault, edit: undefined, expected: commonDefault },
  { label: "a deliberate common-default edit", stored: {}, edit: commonDefault, expected: commonDefault },
  { label: "a deliberate field edit", stored: {}, edit: { type: "object", properties: { count: { type: "integer" } } }, expected: { type: "object", properties: { count: { type: "integer" } } } },
])("saves $label from the settings draft without adopting a public synthesized schema", async ({ stored, edit, expected }) => {
  const workflow = { workflow_id: "shipping-summary", display_name: "Shipping summary", description: "Original", enabled: true, revision: 7, settings: {
    callable: { enabled: false, trigger_channels: ["ui"], input_mode: "json_schema", input_schema: stored, expected_inputs: [], expected_outputs: [] },
  } };
  const contract = { data: { interface: { ...workflow.settings.callable, input_schema: Object.keys(stored).length ? stored : commonDefault } } };
  const posted: Array<Record<string, unknown>> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (String(input) !== "/api/workflows/shipping-summary" || init?.method !== "PATCH") throw new Error("Unexpected schema fixture request");
    posted.push(JSON.parse(String(init.body)) as Record<string, unknown>);
    return new Response(JSON.stringify({ revision: 8 }), { headers: { "content-type": "application/json" } });
  });
  const { result } = renderHook(() => {
    const settings = useWorkflowSettingsController({ workflowId: "shipping-summary", workflowRecord: workflow, drawerMode: "workflow-settings",
      workflowInterfaceControlIdPrefix: "shipping-summary", setDrawerTab: vi.fn(), setSaveError: vi.fn(), setSaveNotice: vi.fn(),
      setSavingWorkflowSurface: vi.fn(), refetchWorkflowSurfaces: async () => undefined });
    useWorkflowSettingsHydration({ ...settings, workflow, workflowContractQuery: contract, workflowId: "shipping-summary", savingWorkflowSurface: "" });
    return settings;
  });
  await waitFor(() => expect(result.current.workflowInterfaceInputModeDraft).toBe("json_schema"));
  act(() => {
    result.current.setWorkflowDescriptionDraft("Changed description");
    if (edit) result.current.setWorkflowInterfaceInputSchemaDraft(JSON.stringify(edit));
  });
  await act(async () => result.current.handleSaveWorkflowSettings());
  expect(posted).toHaveLength(1);
  expect(posted[0]).toMatchObject({ expected_revision: 7, description: "Changed description", settings: { callable: {
    enabled: false, trigger_channels: ["ui"], input_mode: "json_schema", input_schema: expected,
  } } });
  const saved = posted[0].settings as { callable: { input_schema: unknown } };
  expect(saved.callable.input_schema).toEqual(expected);
});

it.each([
  { label: "absent", traits: { retained: "keep" } },
  { label: "unsupported", traits: { retained: "keep", expected_failure: "legacy" } },
])("preserves an $label failure flag and other traits until explicit checkbox edit", async ({ traits }) => {
  const workflow = { workflow_id: "shipping-summary", display_name: "Shipping summary", description: "Original",
    enabled: true, revision: 7, tags: ["billing", "expected-failure"], settings: {
      traits, retained_setting: { keep: true }, callable: { enabled: false, trigger_channels: ["ui"],
        input_mode: "fields", expected_inputs: [], expected_outputs: [] },
    } };
  const posted: Array<Record<string, unknown>> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (String(input) !== "/api/workflows/shipping-summary" || init?.method !== "PATCH") throw new Error("Unexpected settings request");
    posted.push(JSON.parse(String(init.body)) as Record<string, unknown>);
    return new Response(JSON.stringify({ revision: 7 + posted.length }), { headers: { "content-type": "application/json" } });
  });
  const contract = { data: { interface: workflow.settings.callable } };
  const { result } = renderHook(() => {
    const settings = useWorkflowSettingsController({ workflowId: "shipping-summary", workflowRecord: workflow,
      drawerMode: "workflow-settings", workflowInterfaceControlIdPrefix: "shipping-summary", setDrawerTab: vi.fn(),
      setSaveError: vi.fn(), setSaveNotice: vi.fn(), setSavingWorkflowSurface: vi.fn(),
      refetchWorkflowSurfaces: async () => undefined });
    useWorkflowSettingsHydration({ ...settings, workflow, workflowContractQuery: contract,
      workflowId: "shipping-summary", savingWorkflowSurface: "" });
    return settings;
  });
  await waitFor(() => expect(result.current.workflowExpectedFailureDraft).toBe(false));
  act(() => result.current.setWorkflowDescriptionDraft("Ordinary change"));
  await act(async () => result.current.handleSaveWorkflowSettings());
  expect(posted[0]).toMatchObject({ expected_revision: 7, settings: { traits, retained_setting: { keep: true } } });
  expect(posted[0]).not.toHaveProperty("tags");
  workflow.revision = 8;
  act(() => result.current.setWorkflowExpectedFailureDraft(true));
  await act(async () => result.current.handleSaveWorkflowSettings());
  expect(posted[1]).toMatchObject({ expected_revision: 8, settings: {
    traits: { retained: "keep", expected_failure: true }, retained_setting: { keep: true },
  } });
  expect(workflow.tags).toEqual(["billing", "expected-failure"]);
});
