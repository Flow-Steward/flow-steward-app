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

// These page journeys take about 6 s when the quality gate shares the machine,
// over vitest's 5 s default. Per-test timeouts set below still take precedence.
vi.setConfig({ testTimeout: 10_000 });

describe("WorkflowsDetailPage trigger project context", () => {
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

it("scopes trigger list requests with the resolved linked project id", async () => {
    mockedFindWorkflowLinkedProjectId.mockResolvedValueOnce("proj-linked");
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/api/workflows/weekly_report/triggers")) {
        expect(url).toContain("project_id=proj-linked");
        return new Response(
          JSON.stringify({
            catalog: [],
            incoming_webhooks: [],
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
  });

  it("renders auth-aware request examples for every webhook auth mode and payload style", async () => {
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/deliveries")) {
        return new Response(JSON.stringify({ deliveries: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.includes("/api/workflows/weekly_report/triggers") && init?.method === "GET") {
        return new Response(
          JSON.stringify({
            catalog: [],
            incoming_webhooks: [
              {
                workflow_id: "weekly_report",
                entry_key: "bearer_json",
                description: "Bearer JSON",
                enabled: true,
                allowed_methods: ["POST"],
                auth_mode: "bearer_token",
                auth_config: {},
                source_restrictions: {},
                allowed_content_types: ["application/json"],
                max_body_bytes: 4096,
                max_file_bytes: 8192,
                max_files: 3,
                idempotency_mode: "header",
                response_mode: "accepted",
                input_mapping: {},
                requires_secret: true,
                public_path: "/webhook/workflows/bearer_json",
                public_url: "https://public.example/webhook/workflows/bearer_json",
                readiness: { status: "ready", errors: [] },
              },
              {
                workflow_id: "weekly_report",
                entry_key: "basic_json",
                description: "Basic JSON",
                enabled: true,
                allowed_methods: ["POST"],
                auth_mode: "basic_auth",
                auth_config: { username: "workflow" },
                source_restrictions: {},
                allowed_content_types: ["application/json"],
                max_body_bytes: 4096,
                max_file_bytes: 8192,
                max_files: 3,
                idempotency_mode: "none",
                response_mode: "accepted",
                input_mapping: {},
                requires_secret: true,
                public_path: "/webhook/workflows/basic_json",
                public_url: "https://public.example/webhook/workflows/basic_json",
                readiness: { status: "ready", errors: [] },
              },
              {
                workflow_id: "weekly_report",
                entry_key: "shared_form",
                description: "Shared Form",
                enabled: true,
                allowed_methods: ["POST"],
                auth_mode: "shared_secret_header",
                auth_config: { header_name: "X-Demo-Secret" },
                source_restrictions: {},
                allowed_content_types: ["application/x-www-form-urlencoded"],
                max_body_bytes: 4096,
                max_file_bytes: 8192,
                max_files: 3,
                idempotency_mode: "none",
                response_mode: "accepted",
                input_mapping: {},
                requires_secret: true,
                public_path: "/webhook/workflows/shared_form",
                public_url: "https://public.example/webhook/workflows/shared_form",
                readiness: { status: "ready", errors: [] },
              },
              {
                workflow_id: "weekly_report",
                entry_key: "hmac_text",
                description: "HMAC Text",
                enabled: true,
                allowed_methods: ["POST"],
                auth_mode: "hmac_sha256",
                auth_config: {
                  header_name: "X-Demo-Signature",
                  prefix: "sha256=",
                },
                source_restrictions: {},
                allowed_content_types: ["text/plain"],
                max_body_bytes: 4096,
                max_file_bytes: 8192,
                max_files: 3,
                idempotency_mode: "none",
                response_mode: "accepted",
                input_mapping: {},
                requires_secret: true,
                public_path: "/webhook/workflows/hmac_text",
                public_url: "https://public.example/webhook/workflows/hmac_text",
                readiness: { status: "ready", errors: [] },
              },
              {
                workflow_id: "weekly_report",
                entry_key: "multipart_upload",
                description: "Multipart Upload",
                enabled: true,
                allowed_methods: ["POST"],
                auth_mode: "bearer_token",
                auth_config: {},
                source_restrictions: {},
                allowed_content_types: ["multipart/form-data"],
                max_body_bytes: 4096,
                max_file_bytes: 8192,
                max_files: 3,
                idempotency_mode: "none",
                response_mode: "accepted",
                input_mapping: {},
                requires_secret: true,
                public_path: "/webhook/workflows/multipart_upload",
                public_url: "https://public.example/webhook/workflows/multipart_upload",
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
    const backToTriggers = () => fireEvent.click(screen.getByRole("button", { name: "Triggers" }));

    fireEvent.click(await screen.findByRole("button", { name: /Basic JSON/ }));
    expect(await screen.findByText(/-u 'workflow:<password>'/)).toBeInTheDocument();

    backToTriggers();
    fireEvent.click(await screen.findByRole("button", { name: /Shared Form/ }));
    expect(await screen.findByText(/X-Demo-Secret: <shared-secret>/)).toBeInTheDocument();
    expect(
      await screen.findByText(/--data-urlencode 'title=Sync inventory now'/),
    ).toBeInTheDocument();

    backToTriggers();
    fireEvent.click(await screen.findByRole("button", { name: /HMAC Text/ }));
    expect(
      await screen.findByText(/X-Demo-Signature: sha256=<hex-digest>/),
    ).toBeInTheDocument();
    expect(await screen.findByText(/Content-Type: text\/plain/)).toBeInTheDocument();
    expect(await screen.findByText(/--data-raw 'Sync inventory now'/)).toBeInTheDocument();

    backToTriggers();
    fireEvent.click(await screen.findByRole("button", { name: /Multipart Upload/ }));
    expect(await screen.findByText(/Authorization: Bearer <token>/)).toBeInTheDocument();
    expect(await screen.findByText(/-F 'files=@\/path\/to\/sample.json'/)).toBeInTheDocument();

    backToTriggers();
    fireEvent.click(await screen.findByRole("button", { name: /Bearer JSON/ }));
    expect(await screen.findByText(/Idempotency-Key: <unique-key>/)).toBeInTheDocument();
    expect(await screen.findByText(/Content-Type: application\/json/)).toBeInTheDocument();
  });

  it("threads explicit project context through webhook lifecycle writes", async () => {
    const seenUrls: string[] = [];
    let createBody: Record<string, unknown> | null = null;
    let saveBody: Record<string, unknown> | null = null;
    let dryRunBody: Record<string, unknown> | null = null;
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      seenUrls.push(url);
      if (
        url.includes("/api/workflows/weekly_report/triggers/webhooks/inventory_sync/deliveries")
      ) {
        expect(url).toContain("project_id=proj-3");
        return new Response(JSON.stringify({ deliveries: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (
        url.includes("/api/workflows/weekly_report/triggers/webhooks/inventory_sync/dry-run") &&
        init?.method === "POST"
      ) {
        expect(url).toContain("project_id=proj-3");
        dryRunBody = JSON.parse(firstStringFromUnknownValue([init.body, "{}"])) as Record<string, unknown>;
        return new Response(
          JSON.stringify({
            validation: { ok: true, errors: [], interface: {}, inputs: {} },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (
        url.includes("/api/workflows/weekly_report/triggers/webhooks/inventory_sync/rotate-secret") &&
        init?.method === "POST"
      ) {
        expect(url).toContain("project_id=proj-3");
        return new Response(
          JSON.stringify({
            credential_reveal: { auth_mode: "bearer_token", token: "rotated-123" },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (
        url.includes("/api/workflows/weekly_report/triggers/webhooks/inventory_sync/regenerate-entry-key") &&
        init?.method === "POST"
      ) {
        expect(url).toContain("project_id=proj-3");
        return new Response(
          JSON.stringify({
            entry_key: "inventory_sync_regenerated",
            workflow_id: "weekly_report",
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
            public_path: "/webhook/workflows/inventory_sync_regenerated",
            public_url: "https://public.example/webhook/workflows/inventory_sync_regenerated",
            readiness: { status: "ready", errors: [] },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (
        url.includes("/api/workflows/weekly_report/triggers/webhooks/inventory_sync") &&
        init?.method === "DELETE"
      ) {
        expect(url).toContain("project_id=proj-3");
        return new Response(JSON.stringify({ status: "deleted", entry_key: "inventory_sync" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (
        url.includes("/api/workflows/weekly_report/triggers/webhooks/inventory_sync") &&
        init?.method === "PATCH"
      ) {
        expect(url).toContain("project_id=proj-3");
        saveBody = JSON.parse(firstStringFromUnknownValue([init.body, "{}"])) as Record<string, unknown>;
        return new Response(
          JSON.stringify({
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
            public_url: "https://public.example/webhook/workflows/inventory_sync",
            readiness: { status: "ready", errors: [] },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (
        url.includes("/api/workflows/weekly_report/triggers/webhooks") &&
        init?.method === "POST"
      ) {
        createBody = JSON.parse(firstStringFromUnknownValue([init.body, "{}"])) as Record<string, unknown>;
        return new Response(
          JSON.stringify({
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
            public_url: "https://public.example/webhook/workflows/inventory_sync",
            credential_reveal: { auth_mode: "bearer_token", token: "secret-123" },
            readiness: { status: "ready", errors: [] },
          }),
          { status: 201, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/workflows/weekly_report/triggers")) {
        expect(url).toContain("project_id=proj-3");
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
                allowed_content_types: ["application/json"],
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

    renderPage({
      initialEntry: "/registry/workflows/weekly_report/builder?project_id=proj-3",
    });
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
    fireEvent.click(await screen.findByRole("button", { name: "Create webhook" }));
    await waitFor(() => {
      expect(createBody).toMatchObject({ project_id: "proj-3" });
    });

    fireEvent.click(await screen.findByRole("button", { name: "Triggers" }));
    const savedWebhookRow = await screen.findByRole("button", {
      name: /inventory_sync/i,
    });
    fireEvent.click(savedWebhookRow);
    fireEvent.click(await screen.findByRole("button", { name: "Save webhook settings" }));
    await waitFor(() => {
      expect(saveBody).toMatchObject({ project_id: "proj-3" });
    });

    fireEvent.click(screen.getByRole("button", { name: "Dry-run payload" }));
    await waitFor(() => {
      expect(dryRunBody).toMatchObject({ project_id: "proj-3" });
    });

    fireEvent.click(screen.getByRole("button", { name: "Rotate credential" }));
    // Rotating asks first, beside the actions; the confirming action is named.
    fireEvent.click(within(screen.getByRole("group", { name: "Rotate the credential?" })).getByRole("button", { name: "Rotate credential" }));
    await waitFor(() => {
      expect(
        seenUrls.some(
          (url) =>
            url.includes("/rotate-secret") && url.includes("project_id=proj-3"),
        ),
      ).toBe(true);
    });

    fireEvent.click(await screen.findByRole("button", { name: "Regenerate URL" }));
    fireEvent.click(within(screen.getByRole("group", { name: "Regenerate the public URL?" })).getByRole("button", { name: "Regenerate URL" }));
    await waitFor(() => {
      expect(
        seenUrls.some(
          (url) =>
            url.includes("/regenerate-entry-key") && url.includes("project_id=proj-3"),
        ),
      ).toBe(true);
    });
  });

  it("sends non-body dry-run sources and warns when public base URL is missing", async () => {
    let dryRunBody: Record<string, unknown> | null = null;
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (
        url.includes(
          "/api/workflows/weekly_report/triggers/webhooks/inventory_sync/dry-run",
        ) &&
        init?.method === "POST"
      ) {
        dryRunBody = JSON.parse(firstStringFromUnknownValue([init.body, "{}"])) as Record<string, unknown>;
        return new Response(
          JSON.stringify({
            validation: { ok: true, errors: [], interface: {}, inputs: {} },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
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
                auth_mode: "shared_secret_header",
                auth_config: { header_name: "X-Workflow-Secret" },
                allowed_content_types: ["application/x-www-form-urlencoded"],
                idempotency_mode: "none",
                response_mode: "accepted",
                input_mapping: { source: "query.source" },
                requires_secret: true,
                public_path: "/webhook/workflows/inventory_sync",
                public_url: "http://localhost/webhook/workflows/inventory_sync",
                public_url_source: "request_origin_fallback",
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

    expect(
      await screen.findByText(/Public base URL is not configured/i),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/Content-Type: application\/x-www-form-urlencoded/),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Dry-run headers"), {
      target: { value: '{\n  "x-event-type": "sync"\n}' },
    });
    fireEvent.change(screen.getByLabelText("Dry-run query"), {
      target: { value: '{\n  "source": "external"\n}' },
    });
    fireEvent.change(screen.getByLabelText("Dry-run form fields"), {
      target: { value: '{\n  "amount": "42"\n}' },
    });
    fireEvent.change(screen.getByLabelText("Dry-run files"), {
      target: { value: '[\n  {"field_name":"files","filename":"sample.json"}\n]' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Dry-run payload" }));

    await waitFor(() => {
      expect(dryRunBody).toMatchObject({
        body: { title: "Sync inventory now" },
        headers: { "x-event-type": "sync" },
        query: { source: "external" },
        form: { amount: "42" },
        files: [{ field_name: "files", filename: "sample.json" }],
      });
    });
  });

  it("updates mapping guidance when the webhook is restricted to text/plain only", async () => {
    fetchSpy.mockImplementation(async (input, init) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/api/workflows/weekly_report/triggers") && init?.method === "GET") {
        return new Response(
          JSON.stringify({
            catalog: [],
            incoming_webhooks: [],
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
    fireEvent.click(await screen.findByRole("button", { name: "New webhook" }));
    fireEvent.click(screen.getByLabelText("application/json"));
    fireEvent.click(screen.getByLabelText("text/plain"));

    expect(
      await screen.findByText(/Text-only webhooks need an explicit selector/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /email \*string/i })).toHaveAttribute(
      "placeholder",
      "body.body",
    );
  });

  it("shows only incoming webhooks: configured provider triggers stay hidden and are never requested", async () => {
    // Provider triggers are hidden from the UI (decision 2026-10-21); the API
    // still answers them, so the payload below carries a catalog entry and a
    // configured trigger, and the tab must offer neither.
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/api/workflows/weekly_report/triggers")) {
        return new Response(
          JSON.stringify({
            catalog: [
              {
                trigger_family: "provider",
                catalog_id: "sample_tasks:task-created",
                trigger_type_id: "task-created",
                provider_key: "sample_tasks",
                display_name: "Sample Tasks task created",
                description: "Run workflow when a Sample Tasks task is created.",
                ui_schema: { fields: [{ path: "space_id", label: "Space" }] },
              },
            ],
            incoming_webhooks: [],
            provider_triggers: [
              {
                trigger_id: "trig-1",
                workflow_id: "weekly_report",
                project_id: "proj-1",
                provider_key: "sample_tasks",
                trigger_type_id: "task-created",
                display_name: "Sample Tasks task created",
                enabled: true,
                event_name: "task.created",
                trigger_config: { space_id: "abc" },
                readiness: { status: "ready", errors: [] },
                provider_ingress_url: "https://public.example/webhook/providers/sample_tasks",
              },
            ],
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
      project_id: "proj-1",
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

    expect(await screen.findByText("Incoming webhooks")).toBeInTheDocument();
    expect(await screen.findByText("No incoming webhooks yet.")).toBeInTheDocument();
    expect(screen.queryByText("Provider triggers")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Provider triggers" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Sample Tasks task created/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New provider trigger" })).not.toBeInTheDocument();
    expect(screen.queryByText("Pick a provider trigger from the catalog.")).not.toBeInTheDocument();
    expect(screen.queryByText(/connected app/i)).not.toBeInTheDocument();
    const requested = fetchSpy.mock.calls.map(([input]) => stringFromUnknownValue(input));
    expect(requested.some((url) => url.includes("/api/workflows/weekly_report/triggers"))).toBe(true);
    expect(requested.filter((url) => url.includes("/triggers/providers"))).toEqual([]);
  });

  it("offers no provider trigger action while linked project context is still resolving", async () => {
    mockedFindWorkflowLinkedProjectId.mockImplementationOnce(
      () => new Promise<string>(() => {}),
    );
    fetchSpy.mockImplementation(async (input) => {
      const url = stringFromUnknownValue(input);
      if (url.includes("/api/workflows/weekly_report/triggers")) {
        return new Response(
          JSON.stringify({
            catalog: [
              {
                trigger_family: "provider",
                catalog_id: "sample_tasks:task-created",
                trigger_type_id: "task-created",
                provider_key: "sample_tasks",
                display_name: "Sample Tasks task created",
                description: "Run workflow when a Sample Tasks task is created.",
                ui_schema: {},
              },
            ],
            incoming_webhooks: [],
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
    expect(await screen.findByText("No incoming webhooks yet.")).toBeInTheDocument();
    // Hidden provider triggers leave no section, so no pending-context line
    // and no New provider trigger button wait on the project either.
    expect(screen.queryByText(/Resolving project context for provider triggers/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New provider trigger" })).not.toBeInTheDocument();
  });
});
