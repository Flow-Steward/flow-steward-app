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

it("reports a rename into a taken name as a conflict and keeps the draft for the user's choice", async () => {
  const workflow = { workflow_id: "shipping-summary", display_name: "Shipping summary", description: "Original",
    enabled: false, revision: 7, settings: { callable: { enabled: false, trigger_channels: ["ui"],
      input_mode: "fields", expected_inputs: [], expected_outputs: [] } } };
  const requests: string[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    requests.push(`${init?.method} ${String(input)}`);
    return new Response(JSON.stringify({ detail: {
      code: "registry_name_conflict",
      message: "Workflow name 'Order Sync' is already used. Choose another name.",
      matches: [{ id: "order_sync", display_name: "Order Sync", enabled: true, used_by: [] }],
      suggested_display_name: "Order Sync 2",
    } }), { status: 409, headers: { "content-type": "application/json" } });
  });
  const setSaveError = vi.fn();
  const setSaveConflict = vi.fn();
  const contract = { data: { interface: workflow.settings.callable } };
  const { result } = renderHook(() => {
    const settings = useWorkflowSettingsController({ workflowId: "shipping-summary", workflowRecord: workflow,
      drawerMode: "workflow-settings", workflowInterfaceControlIdPrefix: "shipping-summary", setDrawerTab: vi.fn(),
      setSaveError, setSaveConflict, setSaveNotice: vi.fn(), setSavingWorkflowSurface: vi.fn(),
      refetchWorkflowSurfaces: async () => undefined });
    useWorkflowSettingsHydration({ ...settings, workflow, workflowContractQuery: contract,
      workflowId: "shipping-summary", savingWorkflowSurface: "" });
    return settings;
  });
  await waitFor(() => expect(result.current.workflowDisplayNameDraft).toBe("Shipping summary"));
  act(() => result.current.setWorkflowDisplayNameDraft("order sync"));
  await act(async () => result.current.handleSaveWorkflowSettings());

  expect(requests).toEqual(["PATCH /api/workflows/shipping-summary"]);
  expect(setSaveError).toHaveBeenLastCalledWith("Workflow name 'Order Sync' is already used. Choose another name.");
  expect(setSaveConflict).toHaveBeenLastCalledWith(expect.objectContaining({
    code: "registry_name_conflict",
    suggestedDisplayName: "Order Sync 2",
    matches: [expect.objectContaining({ id: "order_sync", display_name: "Order Sync" })],
  }));
  expect(result.current.workflowDisplayNameDraft).toBe("order sync");
});

it("keeps, clears and refuses a stale report output selection", async () => {
  const workflow = { workflow_id: "weekly", display_name: "Weekly", description: "", enabled: false, revision: 3, settings: {
    callable: { enabled: true, trigger_channels: ["mcp"], input_mode: "fields", expected_inputs: [],
      expected_outputs: [{ name: "sales_report", type: "object", required: true }], report_output: "sales_report" },
  } };
  const posted: Array<{ settings: { callable: Record<string, unknown> } }> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (String(input) !== "/api/workflows/weekly" || init?.method !== "PATCH") throw new Error("Unexpected request");
    posted.push(JSON.parse(String(init.body)) as { settings: { callable: Record<string, unknown> } });
    return new Response(JSON.stringify({ revision: 3 + posted.length }), { headers: { "content-type": "application/json" } });
  });
  const setSaveError = vi.fn();
  const contract = { data: { interface: workflow.settings.callable } };
  const { result } = renderHook(() => {
    const settings = useWorkflowSettingsController({ workflowId: "weekly", workflowRecord: workflow, drawerMode: "workflow-interface",
      workflowInterfaceControlIdPrefix: "weekly", setDrawerTab: vi.fn(), setSaveError, setSaveNotice: vi.fn(),
      setSavingWorkflowSurface: vi.fn(), refetchWorkflowSurfaces: async () => undefined });
    useWorkflowSettingsHydration({ ...settings, workflow, workflowContractQuery: contract, workflowId: "weekly", savingWorkflowSurface: "" });
    return settings;
  });
  await waitFor(() => expect(result.current.workflowInterfaceReportOutputDraft).toBe("sales_report"));

  await act(async () => result.current.handleSaveWorkflowSettings());
  expect(posted[0].settings.callable.report_output).toBe("sales_report");

  workflow.revision = 4;
  act(() => result.current.setWorkflowInterfaceReportOutputDraft(""));
  await act(async () => result.current.handleSaveWorkflowSettings());
  expect(posted[1].settings.callable).not.toHaveProperty("report_output");

  workflow.revision = 5;
  act(() => {
    result.current.setWorkflowInterfaceReportOutputDraft("sales_report");
    result.current.setWorkflowInterfaceOutputsDraft(
      result.current.workflowInterfaceOutputsDraft.map((row) => ({ ...row, name: "weekly_report" })),
    );
  });
  await act(async () => result.current.handleSaveWorkflowSettings());
  expect(posted).toHaveLength(2);
  expect(setSaveError).toHaveBeenLastCalledWith(expect.stringContaining("is no longer a required output of type object"));
});

it("saves, keeps and clears the workflow's run title in settings", async () => {
  const workflow = { workflow_id: "scrape", display_name: "Scrape a site", description: "", enabled: false, revision: 3, settings: {
    callable: { enabled: true, trigger_channels: ["ui"], input_mode: "fields", expected_inputs: [{ name: "site_url", type: "string", required: true }] },
    run_title: "{{ inputs.site_url }}",
  } as Record<string, unknown> };
  const posted: Array<{ settings: Record<string, unknown> }> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (String(input) !== "/api/workflows/scrape" || init?.method !== "PATCH") throw new Error("Unexpected request");
    posted.push(JSON.parse(String(init.body)) as { settings: Record<string, unknown> });
    return new Response(JSON.stringify({ revision: 3 + posted.length }), { headers: { "content-type": "application/json" } });
  });
  const contract = { data: { interface: (workflow.settings as { callable: unknown }).callable } };
  const { result } = renderHook(() => {
    const settings = useWorkflowSettingsController({ workflowId: "scrape", workflowRecord: workflow, drawerMode: "workflow-settings",
      workflowInterfaceControlIdPrefix: "scrape", setDrawerTab: vi.fn(), setSaveError: vi.fn(), setSaveNotice: vi.fn(),
      setSavingWorkflowSurface: vi.fn(), refetchWorkflowSurfaces: async () => undefined });
    useWorkflowSettingsHydration({ ...settings, workflow, workflowContractQuery: contract, workflowId: "scrape", savingWorkflowSurface: "" });
    return settings;
  });
  await waitFor(() => expect(result.current.workflowRunTitleDraft).toBe("{{ inputs.site_url }}"));

  act(() => result.current.setWorkflowRunTitleDraft("  Scrape {{ inputs.site_url }}  "));
  await act(async () => result.current.handleSaveWorkflowSettings());
  expect(posted[0].settings.run_title).toBe("Scrape {{ inputs.site_url }}");

  workflow.revision = 4;
  act(() => result.current.setWorkflowRunTitleDraft(""));
  await act(async () => result.current.handleSaveWorkflowSettings());
  // Settings merge on the server, so a cleared title is sent empty rather than left out.
  expect(posted[1].settings.run_title).toBe("");
});

it("leaves run_title out of the settings of a workflow that never had one", async () => {
  const workflow = { workflow_id: "plain", display_name: "Plain", description: "", enabled: false, revision: 2, settings: {
    callable: { enabled: false, trigger_channels: [], input_mode: "fields", expected_inputs: [] },
  } };
  const posted: Array<{ settings: Record<string, unknown> }> = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
    posted.push(JSON.parse(String(init?.body)) as { settings: Record<string, unknown> });
    return new Response(JSON.stringify({ revision: 3 }), { headers: { "content-type": "application/json" } });
  });
  const contract = { data: { interface: workflow.settings.callable } };
  const { result } = renderHook(() => {
    const settings = useWorkflowSettingsController({ workflowId: "plain", workflowRecord: workflow, drawerMode: "workflow-settings",
      workflowInterfaceControlIdPrefix: "plain", setDrawerTab: vi.fn(), setSaveError: vi.fn(), setSaveNotice: vi.fn(),
      setSavingWorkflowSurface: vi.fn(), refetchWorkflowSurfaces: async () => undefined });
    useWorkflowSettingsHydration({ ...settings, workflow, workflowContractQuery: contract, workflowId: "plain", savingWorkflowSurface: "" });
    return settings;
  });
  await waitFor(() => expect(result.current.workflowDisplayNameDraft).toBe("Plain"));
  await act(async () => result.current.handleSaveWorkflowSettings());
  expect(posted[0].settings).not.toHaveProperty("run_title");
});
