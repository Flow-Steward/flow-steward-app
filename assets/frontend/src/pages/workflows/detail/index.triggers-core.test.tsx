import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";

import {
  fetchSpy,
  mockedFetchWorkflowDag,
  mockedFetchWorkflowContract,
  mockedFetchWorkflowDetail,
  mockedFetchWorkflowValidation,
  mockedFindWorkflowLinkedProjectId,
  renderPage,
} from "./index.test-helpers";
import { firstStringFromUnknownValue, stringFromUnknownValue } from "@/lib/utils/unknown-values";

describe("WorkflowsDetailPage trigger editor core", () => {
// The Webhook tab shows only while Incoming Webhook is on (R3-4), so these journeys start with it on.
beforeEach(() => {
  mockedFetchWorkflowContract.mockResolvedValue({
    workflow_id: "weekly_report",
    display_name: "Weekly Report",
    enabled: true,
    interface: { enabled: true, trigger_channels: ["webhook"], input_mode: "fields", strict_inputs: true, expected_inputs: [], output_mode: "none", expected_outputs: [], returned_files: [] },
    definition_issues: [],
  } as never);
});

it("keeps workflow settings drawer bound to workflow metadata instead of the selected group", async () => {
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      
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
    expect(within(screen.getByRole("dialog", { name: "Workflow settings" })).getByTestId("builder-drawer-body")).toHaveClass(
      "overflow-y-auto",
    );
    expect(
      screen.getByRole("textbox", { name: "Display name" }),
    ).toHaveAttribute("name", "workflow_display_name");
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue(
      "Weekly Report",
    );
    expect(
      screen.getByRole("textbox", { name: "Description" }),
    ).toHaveAttribute("name", "workflow_description");
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue(
      "Workflow-level description",
    );
    expect(screen.getByRole("combobox", { name: "Status" })).toHaveAttribute(
      "name",
      "workflow_status",
    );
    // Smart setup carries the input mode, its help and Only accept listed inputs itself.
    expect(screen.queryByRole("button", { name: "Open interface" })).not.toBeInTheDocument();
    expect(screen.getByText("Only accept listed inputs")).toBeInTheDocument();
    expect(
      screen.getByText(
        /^Normal mode\. Flow Steward builds a launch form from Expected inputs, and API\/MCP\/webhook\/schedule callers send those same field names\./,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Any payload")).not.toBeInTheDocument();
    expect(screen.queryByText("Raw JSON payload")).not.toBeInTheDocument();
  });

  it("shows the Triggers tab in workflow settings and loads webhook management data", async () => {
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/api/workflows/weekly_report/triggers/webhooks/inventory_sync/deliveries")) {
        return new Response(
          JSON.stringify({
            deliveries: [
              {
                id: "1",
                status: "accepted",
                timestamp: "2026-05-09T12:00:00Z",
                request_id: "req-1",
                job_id: "321",
              },
            ],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/workflows/weekly_report/triggers")) {
        return new Response(
          JSON.stringify({
            incoming_webhooks: [
              {
                workflow_id: "weekly_report",
                entry_key: "inventory_sync",
                enabled: true,
                allowed_methods: ["POST"],
                idempotency_mode: "header",
                response_mode: "accepted",
                input_mapping: {},
                requires_secret: true,
                public_path: "/webhook/workflows/inventory_sync",
                public_url: "http://localhost/webhook/workflows/inventory_sync",
              },
            ],
            provider_triggers: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          required_inputs: [],
          optional_inputs: [],
          custom_inputs: [],
          current_expected_inputs: [],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    });
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      steps: [], groups: [],
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

    fireEvent.click(await screen.findByRole("tab", { name: "Webhook" }));

    expect(await screen.findByText("Incoming webhooks")).toBeInTheDocument();
    expect(await screen.findByText("/webhook/workflows/inventory_sync")).toBeInTheDocument();
    expect(screen.queryByText("Recent Deliveries")).not.toBeInTheDocument();
    // Incoming webhooks are the tab's only section: provider triggers are hidden.
    expect(screen.queryByText("Provider triggers")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New provider trigger" })).not.toBeInTheDocument();
  });

  it("updates a saved webhook trigger and renders an auth-aware cURL example", async () => {
    let patchBody: Record<string, unknown> | null = null;
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (
        url.includes(
          "/api/workflows/weekly_report/triggers/webhooks/inventory_sync/deliveries",
        )
      ) {
        return new Response(JSON.stringify({ deliveries: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (
        url.includes(
          "/api/workflows/weekly_report/triggers/webhooks/inventory_sync",
        ) &&
        init?.method === "PATCH"
      ) {
        patchBody = JSON.parse(firstStringFromUnknownValue([init.body, "{}"])) as Record<string, unknown>;
        return new Response(
          JSON.stringify({
            workflow_id: "weekly_report",
            entry_key: "inventory_sync",
            enabled: false,
            allowed_methods: ["POST"],
            auth_mode: "basic_auth",
            auth_config: {
              username: "workflow",
              password_secret_ref: "workflow_entrypoint.inventory_sync.secret",
            },
            source_restrictions: {
              exact_ips: ["203.0.113.10"],
              cidrs: ["203.0.113.0/24"],
            },
            allowed_content_types: ["application/json"],
            max_body_bytes: 4096,
            max_file_bytes: 8192,
            max_files: 3,
            idempotency_mode: "none",
            response_mode: "no_content",
            input_mapping: { email: "headers.x-user-email" },
            requires_secret: true,
            public_path: "/webhook/workflows/inventory_sync",
            public_url: "https://public.example/webhook/workflows/inventory_sync",
            readiness: { status: "ready", errors: [] },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/workflows/weekly_report/triggers")) {
        return new Response(
          JSON.stringify({
            catalog: [],
            incoming_webhooks: [
              {
                workflow_id: "weekly_report",
                entry_key: "inventory_sync",
                enabled: true,
                allowed_methods: ["POST"],
                auth_mode: "basic_auth",
                auth_config: {
                  username: "workflow",
                  password_secret_ref: "workflow_entrypoint.inventory_sync.secret",
                },
                source_restrictions: {
                  exact_ips: ["203.0.113.10"],
                  cidrs: ["203.0.113.0/24"],
                },
                allowed_content_types: ["application/json"],
                max_body_bytes: 4096,
                max_file_bytes: 8192,
                max_files: 3,
                idempotency_mode: "header",
                response_mode: "accepted",
                input_mapping: { email: "body.email" },
                requires_secret: true,
                public_path: "/webhook/workflows/inventory_sync",
                public_url: "https://public.example/webhook/workflows/inventory_sync",
                readiness: { status: "ready", errors: [] },
              },
            ],
            provider_triggers: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          required_inputs: [],
          optional_inputs: [],
          custom_inputs: [],
          current_expected_inputs: [],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    });
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValueOnce({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    mockedFetchWorkflowContract.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      interface: {
        enabled: true,
        trigger_channels: ["webhook"],
        input_mode: "fields",
        strict_inputs: true,
        expected_inputs: [{ name: "email", required: true, type: "string" }],
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

    fireEvent.click(await screen.findByRole("tab", { name: "Webhook" }));
    fireEvent.click(await screen.findByRole("button", { name: /inventory_sync/i }));

    expect(await screen.findByText("Editing saved trigger settings.")).toBeInTheDocument();
    expect(await screen.findByText(/-u 'workflow:<password>'/)).toBeInTheDocument();
    expect(await screen.findByText(/Idempotency-Key: <unique-key>/)).toBeInTheDocument();
    expect(await screen.findByText(/Content-Type: application\/json/)).toBeInTheDocument();

    fireEvent.click(screen.getAllByLabelText("Enabled")[0]);
    fireEvent.change(screen.getByRole("combobox", { name: "Idempotency mode" }), {
      target: { value: "none" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Response mode" }), {
      target: { value: "no_content" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /email \*string/i }), {
      target: { value: "headers.x-user-email" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save webhook settings" }));

    await waitFor(() => {
      expect(patchBody).toMatchObject({
        enabled: false,
        idempotency_mode: "none",
        response_mode: "no_content",
        input_mapping: { email: "headers.x-user-email" },
      });
    });
    expect(
      screen.getByText(/Supported selector roots: `body\.`/),
    ).toBeInTheDocument();
  });

  it("re-hydrates webhook editor state from the saved backend response", async () => {
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (
        url.includes(
          "/api/workflows/weekly_report/triggers/webhooks/inventory_sync/deliveries",
        )
      ) {
        return new Response(JSON.stringify({ deliveries: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (
        url.includes(
          "/api/workflows/weekly_report/triggers/webhooks/inventory_sync",
        ) &&
        init?.method === "PATCH"
      ) {
        return new Response(
          JSON.stringify({
            workflow_id: "weekly_report",
            entry_key: "inventory_sync",
            enabled: true,
            allowed_methods: ["POST"],
            auth_mode: "bearer_token",
            auth_config: {},
            source_restrictions: {},
            allowed_content_types: ["multipart/form-data"],
            max_body_bytes: 1048576,
            max_file_bytes: 10485760,
            max_files: 4,
            idempotency_mode: "header",
            response_mode: "accepted",
            input_mapping: {},
            requires_secret: true,
            public_path: "/webhook/workflows/inventory_sync",
            public_url: "https://public.example/webhook/workflows/inventory_sync",
            readiness: { status: "ready", errors: [] },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/workflows/weekly_report/triggers")) {
        return new Response(
          JSON.stringify({
            catalog: [],
            incoming_webhooks: [
              {
                workflow_id: "weekly_report",
                entry_key: "inventory_sync",
                enabled: true,
                allowed_methods: ["POST"],
                auth_mode: "bearer_token",
                auth_config: {},
                source_restrictions: {},
                allowed_content_types: ["multipart/form-data"],
                max_body_bytes: 1048576,
                max_file_bytes: 10485760,
                max_files: 4,
                idempotency_mode: "header",
                response_mode: "accepted",
                input_mapping: {},
                requires_secret: true,
                public_path: "/webhook/workflows/inventory_sync",
                public_url: "https://public.example/webhook/workflows/inventory_sync",
                readiness: { status: "ready", errors: [] },
              },
            ],
            provider_triggers: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          required_inputs: [],
          optional_inputs: [],
          custom_inputs: [],
          current_expected_inputs: [
            { name: "amount", required: true, type: "string" },
            { name: "approved", required: false, type: "boolean" },
          ],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    });
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValueOnce({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    mockedFetchWorkflowContract.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      interface: {
        enabled: true,
        trigger_channels: ["webhook"],
        input_mode: "fields",
        strict_inputs: true,
        expected_inputs: [
          { name: "amount", required: true, type: "string" },
          { name: "approved", required: false, type: "boolean" },
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

    fireEvent.click(await screen.findByRole("tab", { name: "Webhook" }));
    const savedWebhookRow = await screen.findByRole("button", {
      name: /inventory_sync/i,
    });
    fireEvent.click(savedWebhookRow);
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Save webhook settings" }),
      ).toBeInTheDocument();
    });
    fireEvent.click(screen.getByLabelText("text/plain"));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Max body bytes" }), {
      target: { value: "5" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /amount \*string/i }), {
      target: { value: "body.body" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /approved/i }), {
      target: { value: "headers.x-approved" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save webhook settings" }));

    await waitFor(() => {
      expect(screen.getByLabelText("multipart/form-data")).toBeChecked();
    });
    expect(screen.getByLabelText("text/plain")).not.toBeChecked();
    expect(
      screen.getByRole("spinbutton", { name: "Max body bytes" }),
    ).toHaveValue(1048576);
    expect(screen.getByRole("textbox", { name: /amount \*string/i })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: /approved/i })).toHaveValue("");
  });

  it("shows a copy control on one-time credential reveal", async () => {
    let triggerFetchCount = 0;
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (
        url.includes("/api/workflows/weekly_report/triggers") &&
        !url.includes("/triggers/webhooks")
      ) {
        triggerFetchCount += 1;
        return new Response(
          JSON.stringify({
            catalog: [],
            incoming_webhooks:
              triggerFetchCount > 1
                ? [
                    {
                      workflow_id: "weekly_report",
                      entry_key: "inventory_sync",
                      enabled: true,
                      allowed_methods: ["POST"],
                      auth_mode: "bearer_token",
                      auth_config: {},
                      source_restrictions: {},
                      allowed_content_types: ["application/json"],
                      max_body_bytes: 1048576,
                      max_file_bytes: 10485760,
                      max_files: 4,
                      idempotency_mode: "header",
                      response_mode: "accepted",
                      input_mapping: {},
                      requires_secret: true,
                      public_path: "/webhook/workflows/inventory_sync",
                      public_url:
                        "https://public.example/webhook/workflows/inventory_sync",
                      readiness: { status: "ready", errors: [] },
                    },
                  ]
                : [],
            provider_triggers: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (
        url.includes("/api/workflows/weekly_report/triggers/webhooks") &&
        init?.method === "POST"
      ) {
        return new Response(
          JSON.stringify({
            entry_key: "inventory_sync",
            credential_reveal: { auth_mode: "bearer_token", token: "secret-123" },
          }),
          { status: 201, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          required_inputs: [],
          optional_inputs: [],
          custom_inputs: [],
          current_expected_inputs: [],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    });
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn().mockResolvedValue(undefined),
      },
    });
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValueOnce({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
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
    fireEvent.click(await screen.findByRole("tab", { name: "Webhook" }));
    fireEvent.click(await screen.findByRole("button", { name: "New webhook" }));
    fireEvent.click(screen.getByRole("button", { name: "Create webhook" }));

    expect(await screen.findByRole("button", { name: "Copy credential" })).toBeInTheDocument();
    expect(
      await screen.findByText(/secret-123/),
    ).toBeInTheDocument();
  });

  it("creates a webhook with the full backend payload shape from the trigger editor", async () => {
    let triggerFetchCount = 0;
    let postBody: Record<string, unknown> | null = null;
    mockedFindWorkflowLinkedProjectId.mockResolvedValueOnce("proj-linked");
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (
        url.includes("/api/workflows/weekly_report/triggers") &&
        !url.includes("/triggers/webhooks")
      ) {
        expect(url).toContain("project_id=proj-linked");
        triggerFetchCount += 1;
        return new Response(
          JSON.stringify({
            catalog: [],
            incoming_webhooks:
              triggerFetchCount > 1
                ? [
                    {
                      workflow_id: "weekly_report",
                      entry_key: "inventory_sync",
                      enabled: false,
                      allowed_methods: ["POST"],
                      auth_mode: "shared_secret_header",
                      auth_config: { header_name: "X-Demo-Secret" },
                      source_restrictions: {
                        exact_ips: ["203.0.113.10", "198.51.100.24"],
                        cidrs: ["203.0.113.0/24"],
                      },
                      allowed_content_types: [
                        "application/x-www-form-urlencoded",
                        "text/plain",
                      ],
                      max_body_bytes: 2048,
                      max_file_bytes: 8192,
                      max_files: 3,
                      idempotency_mode: "none",
                      response_mode: "ok",
                      input_mapping: {
                        email: "form.email",
                        event_type: "headers.x-event-type",
                        source: "query.source",
                        payload_text: "body.body",
                      },
                      requires_secret: true,
                      public_path: "/webhook/workflows/inventory_sync",
                      public_url: "https://public.example/webhook/workflows/inventory_sync",
                      readiness: { status: "ready", errors: [] },
                    },
                  ]
                : [],
            provider_triggers: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (
        url.includes("/api/workflows/weekly_report/triggers/webhooks") &&
        init?.method === "POST"
      ) {
        postBody = JSON.parse(firstStringFromUnknownValue([init.body, "{}"])) as Record<string, unknown>;
        return new Response(
          JSON.stringify({
            workflow_id: "weekly_report",
            entry_key: "inventory_sync",
            enabled: false,
            allowed_methods: ["POST"],
            auth_mode: "shared_secret_header",
            auth_config: { header_name: "X-Demo-Secret" },
            source_restrictions: {
              exact_ips: ["203.0.113.10", "198.51.100.24"],
              cidrs: ["203.0.113.0/24"],
            },
            allowed_content_types: [
              "application/x-www-form-urlencoded",
              "text/plain",
            ],
            max_body_bytes: 2048,
            max_file_bytes: 8192,
            max_files: 3,
            idempotency_mode: "none",
            response_mode: "ok",
            input_mapping: {
              email: "form.email",
              event_type: "headers.x-event-type",
              source: "query.source",
              payload_text: "body.body",
            },
            requires_secret: true,
            public_path: "/webhook/workflows/inventory_sync",
            public_url: "https://public.example/webhook/workflows/inventory_sync",
            readiness: { status: "ready", errors: [] },
            credential_reveal: {
              auth_mode: "shared_secret_header",
              header_name: "X-Demo-Secret",
              secret: "shared-secret-123",
            },
          }),
          { status: 201, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(
        JSON.stringify({
          required_inputs: [],
          optional_inputs: [],
          custom_inputs: [],
          current_expected_inputs: [],
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    });
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      description: "Workflow-level description",
      enabled: true,
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValueOnce({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    mockedFetchWorkflowContract.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      interface: {
        enabled: true,
        trigger_channels: ["webhook"],
        input_mode: "fields",
        strict_inputs: true,
        expected_inputs: [
          { name: "email", required: true, type: "string" },
          { name: "event_type", required: true, type: "string" },
          { name: "source", required: false, type: "string" },
          { name: "payload_text", required: false, type: "paragraph" },
        ],
        output_mode: "variables",
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
    fireEvent.click(await screen.findByRole("tab", { name: "Webhook" }));
    fireEvent.click(await screen.findByRole("button", { name: "New webhook" }));
    fireEvent.click(screen.getAllByLabelText("Enabled")[0]);
    fireEvent.change(screen.getByRole("textbox", { name: "Name" }), {
      target: { value: "Demo webhook" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Auth mode" }), {
      target: { value: "shared_secret_header" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Header name" }), {
      target: { value: "X-Demo-Secret" },
    });
    fireEvent.click(screen.getByLabelText("application/json"));
    fireEvent.click(screen.getByLabelText("application/x-www-form-urlencoded"));
    fireEvent.click(screen.getByLabelText("text/plain"));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Max body bytes" }), {
      target: { value: "2048" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Max file bytes" }), {
      target: { value: "8192" },
    });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Max files" }), {
      target: { value: "3" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Idempotency mode" }), {
      target: { value: "none" },
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Response mode" }), {
      target: { value: "ok" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "Exact IP allowlist" }), {
      target: { value: "203.0.113.10\n198.51.100.24" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "CIDR allowlist" }), {
      target: { value: "203.0.113.0/24" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /email \*string/i }), {
      target: { value: "form.email" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /event_type \*string/i }), {
      target: { value: "headers.x-event-type" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /source/i }), {
      target: { value: "query.source" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: /payload_text/i }), {
      target: { value: "body.body" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create webhook" }));

    await waitFor(() => {
      expect(postBody).toEqual({
        project_id: "proj-linked",
        description: "Demo webhook",
        enabled: false,
        auth_mode: "shared_secret_header",
        auth_config: { header_name: "X-Demo-Secret" },
        source_restrictions: {
          exact_ips: ["203.0.113.10", "198.51.100.24"],
          cidrs: ["203.0.113.0/24"],
        },
        allowed_content_types: [
          "application/x-www-form-urlencoded",
          "text/plain",
        ],
        max_body_bytes: 2048,
        max_file_bytes: 8192,
        max_files: 3,
        idempotency_mode: "none",
        response_mode: "ok",
        input_mapping: {
          email: "form.email",
          event_type: "headers.x-event-type",
          source: "query.source",
          payload_text: "body.body",
        },
      });
    });
    expect(await screen.findByRole("button", { name: "Copy credential" })).toBeInTheDocument();
    expect(await screen.findByText(/shared-secret-123/)).toBeInTheDocument();
  });

  const IMPORTED_SIGNING_SECRET = "whsec_from_provider"; // pragma: allowlist secret

  const SLACK_STYLE_AUTH_CONFIG = {
    signature: {
      source: "header",
      header: "X-Slack-Signature",
      parser: "prefixed",
      encoding: "hex",
      prefix: "v0=",
    },
    timestamp: {
      source: "header",
      unit: "seconds",
      tolerance_seconds: 300,
      header: "X-Slack-Request-Timestamp",
    },
    signed_payload: {
      parts: [
        { literal: "v0:" },
        { source: "timestamp" },
        { literal: ":" },
        { source: "raw_body" },
      ],
    },
  };

  function jsonResponse(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  }

  function savedWebhook(overrides: Record<string, unknown> = {}) {
    return {
      workflow_id: "weekly_report",
      entry_key: "inventory_sync",
      enabled: true,
      allowed_methods: ["POST"],
      auth_mode: "webhook_signature",
      auth_config: SLACK_STYLE_AUTH_CONFIG,
      source_restrictions: {},
      allowed_content_types: ["application/json"],
      max_body_bytes: 1048576,
      max_file_bytes: 10485760,
      max_files: 4,
      idempotency_mode: "none",
      response_mode: "accepted",
      input_mapping: {},
      requires_secret: true,
      public_path: "/webhook/workflows/inventory_sync",
      public_url: "https://public.example/webhook/workflows/inventory_sync",
      readiness: { status: "ready", errors: [] },
      ...overrides,
    };
  }

  function stubTriggerEditor(options: {
    webhooks?: Array<Record<string, unknown>>;
    onPost?: (body: Record<string, unknown>) => void;
    onPatch?: (body: Record<string, unknown>) => void;
    postResponse?: Record<string, unknown>;
    patchResponse?: Record<string, unknown>;
  }) {
    mockedFindWorkflowLinkedProjectId.mockResolvedValue("proj-linked");
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/deliveries")) {
        return jsonResponse({ deliveries: [] });
      }
      if (url.includes("/api/workflows/weekly_report/triggers/webhooks")) {
        const body = JSON.parse(
          firstStringFromUnknownValue([init?.body, "{}"]),
        ) as Record<string, unknown>;
        if (init?.method === "POST") {
          options.onPost?.(body);
          return jsonResponse(options.postResponse ?? savedWebhook(), 201);
        }
        if (init?.method === "PATCH") {
          options.onPatch?.(body);
          return jsonResponse(options.patchResponse ?? savedWebhook());
        }
      }
      if (url.includes("/api/workflows/weekly_report/triggers")) {
        return jsonResponse({
          catalog: [],
          incoming_webhooks: options.webhooks ?? [],
          provider_triggers: [],
        });
      }
      return jsonResponse({
        required_inputs: [],
        optional_inputs: [],
        custom_inputs: [],
        current_expected_inputs: [],
      });
    });
    mockedFetchWorkflowDetail.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      steps: [], groups: [],
    } as never);
    mockedFetchWorkflowDag.mockResolvedValueOnce({ nodes: [], edges: [] } as never);
    mockedFetchWorkflowValidation.mockResolvedValueOnce({
      validation: { valid: true, errors: [], warnings: [] },
    } as never);
    mockedFetchWorkflowContract.mockResolvedValueOnce({
      workflow_id: "weekly_report",
      display_name: "Weekly Report",
      enabled: true,
      interface: {
        enabled: true,
        trigger_channels: ["webhook"],
        input_mode: "fields",
        strict_inputs: true,
        expected_inputs: [],
        output_mode: "none",
        expected_outputs: [],
        returned_files: [],
      },
      definition_issues: [],
    } as never);
  }

  async function openTriggersTab() {
    renderPage();
    expect(await screen.findByText("Workflow steps")).toBeInTheDocument();
    act(() => {
      globalThis.dispatchEvent(
        new CustomEvent("platform:workflow-builder-action", {
          detail: { action: "workflow-settings" },
        }),
      );
    });
    fireEvent.click(await screen.findByRole("tab", { name: "Webhook" }));
  }

  async function openNewWebhook() {
    await openTriggersTab();
    fireEvent.click(await screen.findByRole("button", { name: "New webhook" }));
  }

  it("creates a signed webhook from the guided builder without raw JSON editing", async () => {
    let postBody: Record<string, unknown> | null = null;
    stubTriggerEditor({ onPost: (body) => (postBody = body) });

    await openNewWebhook();
    fireEvent.change(await screen.findByRole("combobox", { name: "Auth mode" }), {
      target: { value: "webhook_signature" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Stripe-style" }));

    expect(
      screen.getByText('Read v1 from Stripe-Signature as hex; sign timestamp + "." + raw body'),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Stripe-Signature: t=<unix-timestamp>,v1=<hex-digest>"),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Signature header" })).toHaveValue(
      "Stripe-Signature",
    );
    expect(screen.getByRole("combobox", { name: "Signature value" })).toHaveValue(
      "comma_kv",
    );
    expect(screen.getByRole("combobox", { name: "Digest format" })).toHaveValue("hex");
    expect(screen.getByRole("combobox", { name: "Timestamp" })).toHaveValue(
      "signature_param",
    );
    expect(
      screen.getByLabelText("Accept any matching repeated signature key"),
    ).toBeChecked();

    fireEvent.change(screen.getByRole("spinbutton", { name: "Tolerance" }), {
      target: { value: "120" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create webhook" }));

    await waitFor(() => {
      expect(postBody).toMatchObject({
        auth_mode: "webhook_signature",
        auth_config: {
          signature: {
            source: "header",
            header: "Stripe-Signature",
            parser: "comma_kv",
            encoding: "hex",
            value_key: "v1",
            allow_multiple: true,
          },
          timestamp: {
            source: "signature_param",
            unit: "seconds",
            tolerance_seconds: 120,
            key: "t",
          },
          signed_payload: {
            parts: [{ source: "timestamp" }, { literal: "." }, { source: "raw_body" }],
          },
        },
      });
    });
    const body = postBody as unknown as Record<string, unknown>;
    expect(body).not.toHaveProperty("signing_secret");
    expect(JSON.stringify(body)).not.toContain("stripe_style");
    expect(JSON.stringify(body)).not.toContain("profile");
  });

  it("sends an imported signing secret only when one is supplied, then clears it", async () => {
    let postBody: Record<string, unknown> | null = null;
    stubTriggerEditor({
      onPost: (body) => (postBody = body),
      postResponse: { ...savedWebhook(), credential_configured: true },
    });

    await openNewWebhook();
    fireEvent.change(await screen.findByRole("combobox", { name: "Auth mode" }), {
      target: { value: "webhook_signature" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Slack-style" }));

    expect(
      screen.queryByLabelText("Provider signing secret"),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText(/Use an existing provider signing secret/));
    fireEvent.change(screen.getByLabelText("Provider signing secret"), {
      target: { value: `  ${IMPORTED_SIGNING_SECRET}  ` },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create webhook" }));

    await waitFor(() => {
      expect(postBody).toMatchObject({ signing_secret: IMPORTED_SIGNING_SECRET });
    });
    expect(
      await screen.findByText("Webhook created. Your signing secret is stored."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy credential" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Provider signing secret")).not.toBeInTheDocument();
  });

  it("never truncates a fractional tolerance into a narrower freshness window", async () => {
    let postBody: Record<string, unknown> | null = null;
    stubTriggerEditor({ onPost: (body) => (postBody = body) });

    await openNewWebhook();
    fireEvent.change(await screen.findByRole("combobox", { name: "Auth mode" }), {
      target: { value: "webhook_signature" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Stripe-style" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Tolerance" }), {
      target: { value: "1.5" },
    });

    expect(
      await screen.findByText("Tolerance must be a whole number of seconds."),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Create webhook" }));

    // Forwarded verbatim so the backend rejects it, rather than silently saved as 1s.
    await waitFor(() => {
      expect(postBody).toMatchObject({
        auth_config: { timestamp: { tolerance_seconds: "1.5" } },
      });
    });
  });

  it("drops a typed signing secret when the auth mode is switched away from signed webhook", async () => {
    let postBody: Record<string, unknown> | null = null;
    stubTriggerEditor({
      onPost: (body) => (postBody = body),
      postResponse: savedWebhook({ auth_mode: "bearer_token", auth_config: {} }),
    });

    await openNewWebhook();
    const authMode = await screen.findByRole("combobox", { name: "Auth mode" });
    fireEvent.change(authMode, { target: { value: "webhook_signature" } });
    fireEvent.click(await screen.findByRole("button", { name: "Slack-style" }));
    fireEvent.click(screen.getByLabelText(/Use an existing provider signing secret/));
    fireEvent.change(screen.getByLabelText("Provider signing secret"), {
      target: { value: IMPORTED_SIGNING_SECRET },
    });

    // The operator changes their mind: the signed-webhook editor disappears, but
    // the secret they typed must not ride along on a bearer-token webhook.
    fireEvent.change(authMode, { target: { value: "bearer_token" } });
    expect(screen.queryByLabelText("Provider signing secret")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create webhook" }));

    await waitFor(() => {
      expect(postBody).toMatchObject({ auth_mode: "bearer_token" });
    });
    const body = postBody as unknown as Record<string, unknown>;
    expect(body).not.toHaveProperty("signing_secret");
    expect(JSON.stringify(body)).not.toContain(IMPORTED_SIGNING_SECRET);
  });

  it("rehydrates a saved signed webhook into the builder controls and curl preview", async () => {
    stubTriggerEditor({ webhooks: [savedWebhook()] });

    await openTriggersTab();
    fireEvent.click(await screen.findByRole("button", { name: /inventory_sync/i }));

    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "Signature header" })).toHaveValue(
        "X-Slack-Signature",
      );
    });
    expect(screen.getByRole("combobox", { name: "Signature value" })).toHaveValue(
      "prefixed",
    );
    expect(screen.getByRole("textbox", { name: "Prefix" })).toHaveValue("v0=");
    expect(screen.getByRole("combobox", { name: "Timestamp" })).toHaveValue("header");
    expect(screen.getByRole("textbox", { name: "Timestamp header" })).toHaveValue(
      "X-Slack-Request-Timestamp",
    );
    expect(screen.getByRole("combobox", { name: "Signed payload" })).toHaveValue(
      "v0_timestamp_body",
    );
    // The header preview above the form and the saved cURL example both render
    // the same placeholders from the generic config.
    expect(
      await screen.findAllByText(/X-Slack-Signature: v0=<hex-digest>/),
    ).toHaveLength(2);
    expect(
      await screen.findAllByText(/X-Slack-Request-Timestamp: <unix-timestamp>/),
    ).toHaveLength(2);
  });

  it("renders a read-only advanced view for a config the builder cannot represent", async () => {
    stubTriggerEditor({
      webhooks: [
        savedWebhook({
          auth_config: {
            signature: {
              source: "header",
              header: "X-Signature",
              parser: "raw",
              encoding: "hex",
            },
            timestamp: { source: "none" },
            signed_payload: {
              parts: [{ source: "method" }, { source: "raw_body" }],
            },
          },
        }),
      ],
    });

    await openTriggersTab();
    fireEvent.click(await screen.findByRole("button", { name: /inventory_sync/i }));

    expect(
      await screen.findByText(/the simple editor cannot\s+represent/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: "Signature header" }),
    ).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Reset to supported builder config" }),
    );

    expect(screen.getByRole("textbox", { name: "Signature header" })).toHaveValue("");
  });

  it("lists webhooks with their preset, count and path, and no provider triggers", async () => {
    stubTriggerEditor({ webhooks: [savedWebhook({ description: "Orders feed", enabled: false })] });

    await openTriggersTab();

    expect(await screen.findByText("1 webhook")).toBeInTheDocument();
    const row = await screen.findByRole("button", { name: /Orders feed/ });
    expect(row).toHaveTextContent("Custom · /webhook/workflows/inventory_sync");
    expect(row).toHaveTextContent("Off");
    expect(screen.queryByText("No provider triggers configured yet.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create webhook" })).not.toBeInTheDocument();
  });

  it("starts a new webhook as My app or script, without an idempotency key", async () => {
    let postBody: Record<string, unknown> | null = null;
    stubTriggerEditor({ onPost: (body) => (postBody = body) });

    await openNewWebhook();

    expect(screen.getByRole("radio", { name: /My app or script/ })).toBeChecked();
    expect(screen.getByRole("combobox", { name: "Idempotency mode" })).toHaveValue("none");
    expect(screen.getByText("Safe defaults")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create webhook" }));

    await waitFor(() => {
      expect(postBody).toMatchObject({ auth_mode: "bearer_token", idempotency_mode: "none", allowed_content_types: ["application/json"] });
    });
  });

  it("fills a provider preset and keeps it on no idempotency key", async () => {
    let postBody: Record<string, unknown> | null = null;
    stubTriggerEditor({ onPost: (body) => (postBody = body) });

    await openNewWebhook();
    fireEvent.change(screen.getByRole("combobox", { name: "Idempotency mode" }), { target: { value: "header" } });
    expect(screen.getByRole("radio", { name: /Custom/ })).toBeChecked();
    expect(screen.getByText(/Requests without an Idempotency-Key header are rejected/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: /GitHub/ }));

    expect(screen.getByRole("radio", { name: /GitHub/ })).toBeChecked();
    expect(screen.getByRole("combobox", { name: "Idempotency mode" })).toHaveValue("none");
    expect(screen.getByRole("textbox", { name: "Signature header" })).toHaveValue("X-Hub-Signature-256");
    fireEvent.click(screen.getByRole("button", { name: "Create webhook" }));

    await waitFor(() => {
      expect(postBody).toMatchObject({ auth_mode: "webhook_signature", idempotency_mode: "none" });
    });
    expect(postBody).not.toHaveProperty("signing_secret");
  });

  it("asks for the signing secret a Stripe preset needs, by name", async () => {
    let postBody: Record<string, unknown> | null = null;
    stubTriggerEditor({ onPost: (body) => (postBody = body) });

    await openNewWebhook();
    fireEvent.click(screen.getByRole("radio", { name: /Stripe/ }));
    fireEvent.change(screen.getByLabelText("Signing secret from Stripe"), { target: { value: IMPORTED_SIGNING_SECRET } });
    expect(screen.queryByLabelText(/Use an existing provider signing secret/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create webhook" }));

    await waitFor(() => {
      expect(postBody).toMatchObject({ idempotency_mode: "none", signing_secret: IMPORTED_SIGNING_SECRET });
    });
  });

  it("asks before rotating a saved webhook's credential", async () => {
    stubTriggerEditor({ webhooks: [savedWebhook()] });

    await openTriggersTab();
    fireEvent.click(await screen.findByRole("button", { name: /inventory_sync/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Rotate credential" }));

    expect(screen.getByText("Rotate the credential?")).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("group", { name: "Rotate the credential?" })).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByText("Rotate the credential?")).not.toBeInTheDocument();
  });

  it("keeps the advanced JSON surface read-only", async () => {
    stubTriggerEditor({ webhooks: [savedWebhook()] });

    await openTriggersTab();
    fireEvent.click(await screen.findByRole("button", { name: /inventory_sync/i }));
    await waitFor(() => {
      expect(screen.getByText("Advanced JSON (read-only)")).toBeInTheDocument();
    });

    // Rendered as text, and not bound to any editable form control.
    expect(screen.getByText(/"parser": "prefixed"/)).toBeInTheDocument();
    expect(screen.queryByDisplayValue(/"parser": "prefixed"/)).not.toBeInTheDocument();
  });
});
