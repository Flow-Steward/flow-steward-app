import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";

import {
  canvasGraphFixture,
  fetchSpy,
  mockedFetchWorkflowCanvasGraph,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowContract,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowValidation,
  renderPage,
} from "./index.test-helpers";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

// These page journeys take about 6 s when the quality gate shares the machine,
// over vitest's 5 s default. Per-test timeouts set below still take precedence.
vi.setConfig({ testTimeout: 10_000 });

describe("WorkflowsDetailPage workflow settings", () => {
  it.each([
    { source: "contract", channels: [] },
    { source: "settings", channels: [] },
    { source: "settings", channels: undefined },
    { source: "contract", channels: ["webhook", "schedule"] },
  ])("hydrates $source trigger channels without inventing defaults ($channels)", async ({ source, channels }) => {
    const callable = { enabled: false, trigger_channels: channels };
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: false,
      settings: { callable },
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowContract.mockResolvedValue({
      workflow_id: "weekly_report",
      enabled: false,
      interface: source === "contract" ? callable : {},
      definition_issues: [],
    } as never);

    renderPage();
    await screen.findByText("Workflow steps");
    act(() => {
      globalThis.dispatchEvent(new CustomEvent("platform:workflow-builder-action", {
        detail: { action: "workflow-settings" },
      }));
    });
    await screen.findByRole("heading", { name: "Workflow settings" });
    expect(screen.getByLabelText("Enable workflow interface")).not.toBeChecked();
    expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("disabled");
    const selected = channels || [];
    for (const [channel, label] of [
      ["ui", "UI"], ["api", "API"], ["mcp", "MCP"],
      ["webhook", "Incoming Webhook"], ["schedule", "Schedule"],
      ["parent_workflow", "Parent workflow"],
    ]) {
      if (selected.includes(channel)) {
        expect(screen.getByLabelText(label)).toBeChecked();
      } else {
        expect(screen.getByLabelText(label)).not.toBeChecked();
      }
    }
  });

  it("keeps the last trigger unchecked after save, refetch and reopening the builder", async () => {
    let stored = {
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Disabled workflow",
      enabled: false,
      revision: 12,
      settings: {
        retry_policy: { max_attempts: 3 },
        callable: {
          enabled: false,
          trigger_channels: ["webhook"],
          input_mode: "fields",
          strict_inputs: true,
          expected_inputs: [],
          input_schema: {},
          output_mode: "none",
          expected_outputs: [],
          returned_files: [],
        },
      },
      steps: [], groups: [],
    };
    mockedFetchWorkflowDetail.mockImplementation(async () => structuredClone(stored) as never);
    mockedFetchWorkflowContract.mockImplementation(async () => ({
      workflow_id: stored.workflow_id,
      display_name: stored.display_name,
      enabled: stored.enabled,
      interface: structuredClone(stored.settings.callable),
      definition_issues: [],
    }) as never);
    mockedFetchWorkflowCanvasGraph.mockImplementation(async () => ({
      workflow: {
        id: stored.workflow_id, display_name: stored.display_name,
        enabled: stored.enabled, callable_by_parent: false,
      },
      interface_contract: structuredClone(stored.settings.callable),
      output_contract: { output_mode: "none", expected_outputs: [], returned_files: [] },
      groups: [], steps: [], edges: [], validation_issues: [], automation_rules: [],
    }) as never);
    const originalFetch = fetchSpy.getMockImplementation()!;
    fetchSpy.mockImplementation(async (input, init) => {
      if (input === "/api/workflows/weekly_report" && init?.method === "PATCH") {
        const body = JSON.parse(firstStringFromUnknownValue([init.body, "{}"]));
        expect(body.expected_revision).toBe(12);
        expect(body.enabled).toBe(false);
        expect(body.settings).toMatchObject({
          retry_policy: { max_attempts: 3 },
          callable: { enabled: false, trigger_channels: [] },
        });
        stored = { ...stored, ...body, revision: 13 };
        return new Response(JSON.stringify(stored), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      return originalFetch(input, init);
    });

    const { unmount } = renderPage({ surface: "builder" });
    // Workflow settings open from the workflow name in the builder header.
    fireEvent.click(await screen.findByRole("button", { name: "Weekly Report" }));
    const webhook = await screen.findByLabelText("Incoming Webhook");
    expect(webhook).toBeChecked();
    fireEvent.click(webhook);
    fireEvent.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await screen.findByText("Workflow settings saved.");
    expect(stored.revision).toBe(13);
    expect(stored.settings.callable.trigger_channels).toEqual([]);
    expect(screen.queryByText("no triggers")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Incoming Webhook")).not.toBeChecked();

    unmount();
    renderPage({ surface: "builder" });
    // Workflow settings open from the workflow name in the builder header.
    fireEvent.click(await screen.findByRole("button", { name: "Weekly Report" }));
    await screen.findByRole("heading", { name: "Workflow settings" });
    expect(screen.queryByText("no triggers")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Enable workflow interface")).not.toBeChecked();
    expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("disabled");
    for (const label of ["UI", "API", "MCP", "Incoming Webhook", "Schedule", "Parent workflow"]) {
      expect(screen.getByLabelText(label)).not.toBeChecked();
    }
  });

  it("ignores retired AI drawer links and events without requesting an AI session", async () => {
    renderPage({ surface: "builder", initialEntry: "/registry/workflows/weekly_report/builder?drawer=build-ai" });
    expect(await screen.findByRole("button", { name: "Start — workflow inputs" })).toBeInTheDocument();
    act(() => {
      globalThis.dispatchEvent(new CustomEvent("platform:workflow-builder-action", {
        detail: { action: "build-ai" },
      }));
    });
    expect(screen.queryByRole("heading", { name: "Build with AI" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Describe.*workflow/i })).not.toBeInTheDocument();
    expect(fetchSpy.mock.calls.map(([url]) => String(url)).filter((url) => url.includes("/ai-builder/"))).toEqual([]);

    act(() => {
      globalThis.dispatchEvent(new CustomEvent("platform:workflow-builder-action", {
        detail: { action: "workflow-settings" },
      }));
    });
    expect(await screen.findByRole("heading", { name: "Workflow settings" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Smart setup" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("Weekly Report");
    fireEvent.click(screen.getByRole("link", { name: "Test Run" }));
    expect(await screen.findByLabelText("Test Run Summary")).toBeInTheDocument();
    expect(fetchSpy.mock.calls.map(([url]) => String(url)).filter((url) => url.includes("/ai-builder/"))).toEqual([]);
  });

  it("keeps discovery metadata editable in Advanced without an AI Builder tab", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      builder_metadata_jsonb: {
        purpose: "Resolve contextual bugs",
        use_cases: ["Fix a reported regression"],
        anti_use_cases: ["Build an unrelated feature"],
        composition_role: "reasoning_step",
        determinism_level: "hybrid",
        safe_for_auto_use: false,
      },
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);

    renderPage();

    expect(await screen.findByText("Workflow steps")).toBeInTheDocument();
    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("platform:workflow-builder-action", {
          detail: { action: "workflow-settings" },
        }),
      );
    });

    expect(
      await screen.findByRole("heading", { name: "Workflow settings" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "AI Builder" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: "Purpose" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Advanced mode" }));
    expect(screen.queryByRole("button", { name: "AI Builder" })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("tab", { name: "Advanced" }));

    expect(screen.getByRole("textbox", { name: "Purpose" })).toHaveValue(
      "Resolve contextual bugs",
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Purpose" }), {
      target: { value: "Resolve contextual workflow bugs" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Save workflow settings" }),
    );

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/workflows/weekly_report",
        expect.objectContaining({ method: "PATCH" }),
      );
    });
    const workflowCall = fetchSpy.mock.calls.find(
      (call) =>
        call[0] === "/api/workflows/weekly_report" &&
        call[1]?.method === "PATCH",
    );
    expect(
      JSON.parse(firstStringFromUnknownValue([workflowCall?.[1]?.body, "{}"]))
        .builder_metadata_jsonb,
    ).toMatchObject({ purpose: "Resolve contextual workflow bugs" });

    fireEvent.click(screen.getByRole("button", { name: "Advanced mode on" }));

    expect(
      screen.queryByRole("button", { name: "AI Builder" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: "Purpose" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Display name" })).toBeInTheDocument();
  });

it("hydrates workflow interface controls from the normalized contract endpoint", async () => {
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "sample_weather_summary_child",
      display_name: "Demo Child: Weather Summary",
      description: "Callable child workflow",
      enabled: true,
      settings: {
        callable: {
          enabled: true,
          expected_outputs: [{ name: "weather_summary", type: "text" }],
        },
      },
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowContract.mockResolvedValueOnce({
      workflow_id: "sample_weather_summary_child",
      display_name: "Demo Child: Weather Summary",
      enabled: true,
      interface: {
        enabled: true,
        trigger_channels: ["parent_workflow"],
        input_mode: "fields",
        strict_inputs: true,
        expected_inputs: [],
        output_mode: "variables",
        expected_outputs: [{ name: "weather_summary", type: "string" }],
        returned_files: [],
      },
      definition_issues: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValueOnce({
      nodes: [],
      edges: [],
    } as never);
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
      validation: {
        valid: true,
        errors: [],
        warnings: [],
      },
    } as never);

    renderPage();

    expect(await screen.findByText("Workflow steps")).toBeInTheDocument();
    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("platform:workflow-builder-action", {
          detail: { action: "workflow-settings" },
        }),
      );
    });

    expect(
      await screen.findByRole("heading", { name: "Workflow settings" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Parent workflow")).toBeChecked();
    // Smart setup carries the input mode and the contract itself; what the
    // workflow returns follows from its outputs, so there is no Output mode.
    expect(await screen.findByTestId("workflow-contract-returned-files")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Output mode" })).not.toBeInTheDocument();
  });

  it("keeps no External changes or Artifacts editor and lists returned files in Smart setup", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      settings: {
        callable: {
          enabled: true,
          // Declared before external changes were removed: ignored and never shown.
          side_effects: [{ kind: "file_write", target: "artifact_storage" }],
        },
      },
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowContract.mockResolvedValue({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      interface: {
        enabled: true,
        trigger_channels: ["ui"],
        input_mode: "fields",
        strict_inputs: true,
        expected_inputs: [],
        output_mode: "artifacts",
        expected_outputs: [],
        returned_files: [{ name: "weekly_pdf", type: "file", required: false, source: { step_id: "render", step_name: "Render PDF", result: "file", target: "state.weekly_pdf" } }],
      },
      definition_issues: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({ validation: { valid: true, errors: [], warnings: [] } } as never);

    renderPage();

    expect(await screen.findByText("Workflow steps")).toBeInTheDocument();
    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("platform:workflow-builder-action", {
          detail: { action: "workflow-settings" },
        }),
      );
    });

    const returned = within(await screen.findByTestId("workflow-contract-returned-files"));
    expect(returned.getByText("weekly_pdf")).toBeInTheDocument();
    expect(returned.getByText("Render PDF → file")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-contract-external-changes")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-contract-artifacts")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add external change|Add artifact/ })).not.toBeInTheDocument();

    fireEvent.change(screen.getByRole("textbox", { name: "Description" }), {
      target: { value: "Edited description" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/workflows/weekly_report",
        expect.objectContaining({ method: "PATCH" }),
      );
    });
    const workflowCall = fetchSpy.mock.calls.find(
      (call) => call[0] === "/api/workflows/weekly_report" && call[1]?.method === "PATCH",
    );
    const callable = JSON.parse(firstStringFromUnknownValue([workflowCall?.[1]?.body, "{}"])).settings.callable;
    expect(callable).not.toHaveProperty("returned_files");
    expect(callable).not.toHaveProperty("output_mode");
  });

  it("persists workflow settings through the workflow API", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "weekly_report",
      revision: 12,
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      
      settings: {
        callable: {
          enabled: true,
          trigger_channels: ["ui"],
          input_mode: "fields",
          strict_inputs: true,
          expected_inputs: [
            { name: "ticket_id", type: "string", required: true },
          ],
          output_mode: "variables",
          expected_outputs: [{ name: "summary", type: "string" }],
          returned_files: [],
        },
      },
      steps: [],
      groups: [
        {
          group_id: "analysis",
          display_name: "Context Analysis",
          description: "Group-level description",
          body: { kind: "sequence" },
        },
      ],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({
      nodes: [],
      edges: [],
    } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: {
        valid: true,
        errors: [],
        warnings: [],
      },
    } as never);
    mockedFetchWorkflowContract.mockResolvedValue({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      interface: {
        enabled: true,
        trigger_channels: ["ui"],
        input_mode: "fields",
        strict_inputs: true,
        expected_inputs: [
          { name: "ticket_id", type: "string", required: true },
        ],
        output_mode: "variables",
        expected_outputs: [{ name: "summary", type: "string" }],
        returned_files: [],
      },
      definition_issues: [],
    } as never);

    renderPage();

    expect(await screen.findByText("Workflow steps")).toBeInTheDocument();

    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("platform:workflow-builder-action", {
          detail: { action: "workflow-settings" },
        }),
      );
    });

    fireEvent.change(
      await screen.findByRole("textbox", { name: "Display name" }),
      {
        target: { value: "Weekly Report Edited" },
      },
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Description" }), {
      target: { value: "Updated description" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Status" }), {
      target: { value: "disabled" },
    });
    fireEvent.click(screen.getByLabelText("Parent workflow"));
    // Expected inputs sit in Smart setup; Add input adds the row open.
    fireEvent.click(screen.getByRole("button", { name: "Add input" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "priority" },
    });
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "select" },
    });
    fireEvent.change(screen.getByLabelText("Options"), {
      target: { value: "high, low" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Save workflow settings" }),
    );

    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        "/api/workflows/weekly_report",
        expect.objectContaining({ method: "PATCH" }),
      );
    });
    const workflowCall = fetchSpy.mock.calls.find(
      (call) =>
        call[0] === "/api/workflows/weekly_report" &&
        call[1]?.method === "PATCH",
    );
    expect(JSON.parse(firstStringFromUnknownValue([workflowCall?.[1]?.body, "{}"]))).toMatchObject({
      expected_revision: 12,
      display_name: "Weekly Report Edited",
      description: "Updated description",
      enabled: false,
      settings: {
        callable: {
          enabled: true,
          trigger_channels: ["ui", "parent_workflow"],
          input_mode: "fields",
          expected_inputs: [
            { name: "ticket_id", type: "string", required: true },
            {
              name: "priority",
              type: "select",
              required: false,
              options: ["high", "low"],
            },
          ],
          output_mode: "variables",
        },
      },
    });
    expect(
      await screen.findByText("Workflow settings saved."),
    ).toBeInTheDocument();
  });

  it("preserves workflow display name and expected input rows when saving multiple inputs", async () => {
    let patchBody: Record<string, unknown> | null = null;
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      settings: {
        callable: {
          enabled: true,
          trigger_channels: ["ui"],
          input_mode: "fields",
          strict_inputs: true,
          expected_inputs: [],
          output_mode: "variables",
          expected_outputs: [],
          returned_files: [],
        },
      },
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    mockedFetchWorkflowContract.mockResolvedValue({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      interface: {
        enabled: true,
        trigger_channels: ["ui"],
        input_mode: "fields",
        strict_inputs: true,
        expected_inputs: [],
        output_mode: "variables",
        expected_outputs: [],
        returned_files: [],
      },
      definition_issues: [],
    } as never);
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (url === "/api/workflows/weekly_report" && init?.method === "PATCH") {
        patchBody = JSON.parse(firstStringFromUnknownValue([init.body, "{}"])) as Record<string, unknown>;
      }
      return new Response(JSON.stringify({ status: "updated" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    renderPage();
    expect(await screen.findByText("Workflow steps")).toBeInTheDocument();

    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("platform:workflow-builder-action", {
          detail: { action: "workflow-settings" },
        }),
      );
    });

    // No inputs yet: the block says so, and each Add input adds one row, open.
    expect(await screen.findByText("No expected inputs. The workflow launches without data.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add input" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "email" },
    });
    fireEvent.change(screen.getByLabelText("Label"), {
      target: { value: "Email" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add input" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "amount" },
    });
    fireEvent.change(screen.getByLabelText("Label"), {
      target: { value: "Amount" },
    });
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "number" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add input" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "approved" },
    });
    fireEvent.change(screen.getByLabelText("Label"), {
      target: { value: "Approved" },
    });
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "boolean" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save workflow settings" }));

    await waitFor(() => {
      expect(patchBody).not.toBeNull();
    });
    expect(patchBody).toMatchObject({
      display_name: "Weekly Report",
      settings: {
        callable: {
          expected_inputs: [
            { name: "email", type: "string", label: "Email" },
            { name: "amount", type: "number", label: "Amount" },
            { name: "approved", type: "boolean", label: "Approved" },
          ],
        },
      },
    });
  });

  it("explains that a platform workflow must be cloned when its settings are saved", async () => {
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (url === "/api/workflows/weekly_report" && init?.method === "PATCH") {
        return new Response(
          JSON.stringify({ detail: "platform_workflow_requires_clone" }),
          { status: 409, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });

    renderPage();
    expect(await screen.findByText("Workflow steps")).toBeInTheDocument();
    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("platform:workflow-builder-action", {
          detail: { action: "workflow-settings" },
        }),
      );
    });

    // Save rests until a setting changes.
    fireEvent.change(await screen.findByRole("textbox", { name: "Display name" }), {
      target: { value: "Weekly Report copy" },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "Save workflow settings" }),
    );

    expect(
      await screen.findByText(
        "This platform workflow is read-only. Use “Clone Workflow” to create an editable copy for this account.",
      ),
    ).toBeInTheDocument();
  });

  it("blocks a blank configured number before sending the settings PATCH", async () => {
    mockedFetchWorkflowDetail.mockResolvedValue({
      workflow_id: "weekly_report",
      revision: 12,
      display_name: "Weekly Report",
      enabled: true,
      settings: { callable: {} },
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValue({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValue({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    mockedFetchWorkflowContract.mockResolvedValue({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      interface: {
        enabled: true,
        trigger_channels: ["ui"],
        input_mode: "fields",
        strict_inputs: true,
        expected_inputs: [
          {
            name: "retries",
            type: "number",
            default: 3,
            default_configured: true,
          },
        ],
        output_mode: "none",
        expected_outputs: [],
        returned_files: [],
      },
      definition_issues: [],
    } as never);

    renderPage();
    expect(await screen.findByText("Workflow steps")).toBeInTheDocument();
    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("platform:workflow-builder-action", {
          detail: { action: "workflow-settings" },
        }),
      );
    });
    fireEvent.click(await screen.findByRole("button", { name: /^retries/ }));
    fireEvent.change(screen.getByDisplayValue("3"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save workflow settings" }));

    expect(
      await screen.findByText(/Configured default for 'retries' must be a valid number/i),
    ).toBeInTheDocument();
    expect(
      fetchSpy.mock.calls.some(
        ([url, init]) =>
          url === "/api/workflows/weekly_report" && init?.method === "PATCH",
      ),
    ).toBe(false);
  });
});


it("saves an enabled interface draft without channels with an actionable not-ready warning, then clears it after explicit repair", async () => {
  let stored = { workflow_id: "weekly_report", display_name: "Weekly Report", enabled: false, revision: 12,
    settings: { callable: { enabled: true, trigger_channels: [] as string[], input_mode: "fields", strict_inputs: true,
      expected_inputs: [], output_mode: "none", expected_outputs: [], returned_files: [] } }, steps: [], groups: [] };
  mockedFetchWorkflowDetail.mockImplementation(async () => structuredClone(stored) as never);
  mockedFetchWorkflowContract.mockImplementation(async () => ({ workflow_id: "weekly_report", enabled: false,
    interface: structuredClone(stored.settings.callable), definition_issues: stored.settings.callable.trigger_channels.length ? [] : [
      { code: "workflow.interface.trigger_channels_missing", message: "Callable workflow must declare trigger_channels", path: "workflow.callable.trigger_channels", severity: "error" },
    ] }) as never);
  mockedFetchWorkflowCanvasGraph.mockResolvedValue(canvasGraphFixture({ enabled: false }));
  const originalFetch = fetchSpy.getMockImplementation()!;
  const savedChannels: string[][] = [];
  fetchSpy.mockImplementation(async (input, init) => {
    if (input === "/api/workflows/weekly_report" && init?.method === "PATCH") {
      const body = JSON.parse(firstStringFromUnknownValue([init.body, "{}"]));
      expect(body.expected_revision).toBe(stored.revision);
      expect(body.enabled).toBe(false);
      expect(body.settings.callable.enabled).toBe(true);
      savedChannels.push(body.settings.callable.trigger_channels);
      stored = { ...stored, ...body, revision: stored.revision + 1 };
      return new Response(JSON.stringify(stored), { status: 200, headers: { "content-type": "application/json" } });
    }
    return originalFetch(input, init);
  });
  const view = renderPage({ surface: "builder" });
  try {
    // Workflow settings open from the workflow name in the builder header.
    fireEvent.click(await screen.findByRole("button", { name: "Weekly Report" }));
    const enabled = await screen.findByLabelText("Enable workflow interface");
    expect(enabled).toBeChecked();
    expect(enabled).toHaveAccessibleDescription("Choose at least one trigger channel before running a Test Run.");
    // Save rests until a setting changes: an unrelated description edit sends the channel-less draft.
    fireEvent.change(screen.getByRole("textbox", { name: "Description" }), { target: { value: "Interface without channels" } });
    fireEvent.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(savedChannels).toEqual([[]]));
    await screen.findByText("Workflow settings saved.");
    // The not-ready warning stays with the switch, where it is repaired.
    expect(enabled).toHaveAccessibleDescription("Choose at least one trigger channel before running a Test Run.");
    expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("disabled");
    expect(screen.getByLabelText("UI")).not.toBeChecked();
    fireEvent.click(screen.getByLabelText("API"));
    fireEvent.click(screen.getByRole("button", { name: "Save workflow settings" }));
    await waitFor(() => expect(savedChannels).toEqual([[], ["api"]]));
    await screen.findByText("Workflow settings saved.");
    expect(enabled).not.toHaveAttribute("aria-describedby");
    expect(screen.queryByText("Choose at least one trigger channel before running a Test Run.")).not.toBeInTheDocument();
  } finally { view.unmount(); }
});
