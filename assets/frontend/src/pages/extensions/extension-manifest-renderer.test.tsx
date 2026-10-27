import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { vi } from "vitest";

import { extensionBrowserAssetUrl, type ManifestRenderContext } from "@/pages/extensions/extension-manifest-render-model";
import { renderManifestComponent } from "@/pages/extensions/extension-manifest-renderer";
import { contractActionInputNames } from "@/pages/extensions/extension-manifest-utils";

function renderContext(overrides: Partial<ManifestRenderContext> = {}): ManifestRenderContext {
  return {
    busy: false,
    canManageProject: true,
    connectionCreationBlockedReason: "",
    callbacks: [],
    callbackStatuses: {},
    componentErrors: {},
    componentQueryData: {},
    componentQueryParams: {},
    extensionId: "example.markdown-extension",
    extensionInstallId: "",
    extensionReleaseId: "",
    extensionDescriptorSha256: "",
    formValuesByComponent: {},
    oauthConnectionTypes: [],
    projectConnections: [],
    projectId: "project_123",
    projectState: [],
    onCallbackSetup: () => undefined,
    onComponentQueryParamsChange: () => undefined,
    onConnectionDelete: () => undefined,
    onConnectionOAuth: () => undefined,
    onConnectionOAuthDisconnect: () => undefined,
    onConnectionSave: () => undefined,
    onConnectionSelect: () => undefined,
    onFieldChange: () => undefined,
    onExternalResourcePickerBootstrap: async () => ({}),
    onManifestAction: () => undefined,
    onProjectStateDelete: () => undefined,
    onProjectStateSave: () => undefined,
    onProjectStateSelect: () => undefined,
    ...overrides,
  };
}

describe("renderManifestComponent", () => {
  it("reads authoritative inputs from a production-shaped browser operation manifest", () => {
    expect(contractActionInputNames({
      operation_manifest: [{
        operation_id: "save",
        inputs: [{ name: "display_name", type: "string" }],
      }],
    }, "save")).toEqual(["display_name"]);
  });

  it("renders callback setup as read-only for viewers", () => {
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "callback_setup",
            type: "callback_setup",
            title: "Incoming updates",
            callback_id: "updates",
          },
          renderContext({ canManageProject: false }),
        )}
      </>,
    );

    expect(screen.getByText("Callback setup is managed by project managers.")).toBeVisible();
    expect(screen.queryByText("not registered")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create and Configure" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Refresh Status" })).not.toBeInTheDocument();
  });

  it("renders callback mutation controls for project managers", () => {
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "callback_setup",
            type: "callback_setup",
            title: "Incoming updates",
            callback_id: "updates",
          },
          renderContext({ canManageProject: true }),
        )}
      </>,
    );

    expect(screen.getByRole("button", { name: "Create and Configure" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Refresh Status" })).toBeVisible();
  });

  it("binds browser asset URLs to the exact install release and project", () => {
    expect(extensionBrowserAssetUrl({
      extensionId: "example.markdown-extension",
      extensionInstallId: "install-1",
      extensionReleaseId: "release-1",
      extensionDescriptorSha256: "a".repeat(64),
      projectId: "project-1",
      assetRef: "assets/picker/script.js",
    })).toBe(
      "/api/extensions/example.markdown-extension/assets/assets/picker/script.js?extension_install_id=install-1&release_id=release-1&descriptor_sha256="
      + `${"a".repeat(64)}&project_id=project-1`,
    );
    expect(extensionBrowserAssetUrl({
      extensionId: "example.markdown-extension",
      extensionInstallId: "install-1",
      extensionReleaseId: "",
      extensionDescriptorSha256: "a".repeat(64),
      projectId: "project-1",
      assetRef: "assets/picker/script.js",
    })).toBe("");
  });

  it("binds a provider-neutral external picker selection to its declared target form", async () => {
    const user = userEvent.setup();
    const onFieldChange = vi.fn();
    const onBootstrap = vi.fn().mockResolvedValue({ session: "short-lived" });

    render(
      <>
        {renderManifestComponent(
          {
            component_id: "resource_picker",
            type: "external_resource_picker",
            data: {
              allowed_script_origins: ["https://sdk.example.test"],
              bootstrap_action_id: "picker_bootstrap",
              bridge_global: "NeutralFixturePicker",
              button_label: "Choose resource",
              scripts: [{ src: "https://sdk.example.test/picker.js" }],
              target_component_id: "resource_form",
              selection: {
                maximum: 1,
                fields: [
                  { source: "opaque_id", target: "resource_id", required: true, max_length: 128 },
                ],
              },
            },
          },
          renderContext({
            externalResourcePickerBridgeFactory: (_declaration, executeBootstrap) => ({
              open: async () => {
                await executeBootstrap("picker_bootstrap");
                return [{ opaque_id: "resource-a", vendor_extra: "not-bound" }];
              },
            }),
            onExternalResourcePickerBootstrap: onBootstrap,
            onFieldChange,
          }),
        )}
      </>,
    );

    await user.click(screen.getByRole("button", { name: "Choose resource" }));

    await waitFor(() => {
      expect(onFieldChange).toHaveBeenCalledWith("resource_form", "resource_id", "resource-a");
    });
    expect(onBootstrap).toHaveBeenCalledWith("picker_bootstrap", expect.objectContaining({
      component_id: "resource_picker",
    }));
  });

  it("preserves each picker component identity when components share a bootstrap action", async () => {
    const user = userEvent.setup();
    const onBootstrap = vi.fn().mockResolvedValue({ session: "short-lived" });
    const component = (componentId: string, label: string) => ({
      component_id: componentId,
      type: "external_resource_picker",
      data: {
        allowed_script_origins: ["https://sdk.example.test"],
        bootstrap_action_id: "picker_bootstrap",
        bridge_global: "NeutralFixturePicker",
        button_label: label,
        scripts: [{ src: "https://sdk.example.test/picker.js" }],
        target_component_id: `${componentId}_form`,
        selection: {
          maximum: 1,
          fields: [{ source: "opaque_id", target: "resource_id", required: true, max_length: 128 }],
        },
      },
    });
    const pickerHost = {
      externalResourcePickerBridgeFactory: (_declaration, executeBootstrap) => ({
        open: async () => {
          await executeBootstrap("picker_bootstrap");
          return [{ opaque_id: "resource-a" }];
        },
      }),
      onExternalResourcePickerBootstrap: onBootstrap,
    } satisfies Partial<ManifestRenderContext>;
    render(<>{renderManifestComponent(component("first_picker", "Choose first"), renderContext(pickerHost))}{renderManifestComponent(component("second_picker", "Choose second"), renderContext(pickerHost))}</>);

    await user.click(screen.getByRole("button", { name: "Choose second" }));
    expect(onBootstrap).toHaveBeenCalledWith(
      "picker_bootstrap",
      expect.objectContaining({ component_id: "second_picker" }),
    );
    expect(onBootstrap).not.toHaveBeenCalledWith(
      "picker_bootstrap",
      expect.objectContaining({ component_id: "first_picker" }),
    );
  });

  it("associates required connection controls with deterministic labels", () => {
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "imap_mailbox_connection",
            type: "connection_form",
            title: "IMAP mailbox connection",
            fields: [
              { path: "imap_host", label: "IMAP host", widget: "text", required: true },
              {
                path: "tls_mode",
                label: "TLS mode",
                widget: "select",
                required: true,
                options: [{ value: "ssl", label: "SSL/TLS" }],
              },
              { path: "remember_mailbox", label: "Remember mailbox", widget: "boolean", required: true },
            ],
          },
          renderContext(),
        )}
      </>,
    );

    const host = screen.getByRole("textbox", { name: "IMAP host" });
    const tlsMode = screen.getByRole("combobox", { name: "TLS mode" });
    const rememberMailbox = screen.getByRole("checkbox", { name: "Remember mailbox" });

    expect(host).toHaveAttribute("id", "extension-field-imap_mailbox_connection-imap_host");
    expect(screen.getByText("IMAP host")).toHaveAttribute("for", host.id);
    for (const control of [host, tlsMode, rememberMailbox]) {
      expect(control).toBeRequired();
      expect(control).toHaveAttribute("aria-required", "true");
    }
    expect(screen.getAllByText("*", { selector: "span" })).toHaveLength(3);
  });

  it("keeps labels associated with distinct controls for colliding field paths", () => {
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "connection",
            type: "connection_form",
            title: "Connection",
            fields: [
              { path: "a.b", label: "Nested value", widget: "text" },
              { path: "a-b", label: "Dashed value", widget: "text" },
            ],
          },
          renderContext(),
        )}
      </>,
    );

    const nested = screen.getByRole("textbox", { name: "Nested value" });
    const dashed = screen.getByRole("textbox", { name: "Dashed value" });

    expect(nested.id).toBe("extension-field-connection-a~2e~b");
    expect(dashed.id).toBe("extension-field-connection-a-b");
    expect(nested.id).not.toBe(dashed.id);
    expect(screen.getByText("Nested value")).toHaveAttribute("for", nested.id);
    expect(screen.getByText("Dashed value")).toHaveAttribute("for", dashed.id);
  });

  it("renders OAuth account lifecycle actions and preserved connected email", async () => {
    const user = userEvent.setup();
    const onConnectionOAuth = vi.fn();
    const onConnectionOAuthDisconnect = vi.fn();
    const connection = {
      connection_id: "mail-1",
      extension_id: "fixture.extension-oauth",
      connection_type: "fixture_mail_account",
      display_name: "Finance Mail",
      health_status: "healthy",
      runtime_status: "ready",
      connection_config: {
        oauth: {
          lifecycle_status: "connected",
          connected_email: "finance@example.test",
        },
      },
    };

    render(
      <>
        {renderManifestComponent(
          {
            component_id: "mail_accounts",
            type: "connection_form",
            title: "Mail accounts",
            data: { connection_type: "fixture_mail_account", reconnect_intent: "reconnect_account" },
            fields: [{ path: "display_name", label: "Display name", widget: "text" }],
          },
          renderContext({
            extensionId: "fixture.extension-oauth",
            oauthConnectionTypes: ["fixture_mail_account"],
            projectConnections: [connection],
            onConnectionOAuth,
            onConnectionOAuthDisconnect,
          }),
        )}
      </>,
    );

    expect(screen.getByText("finance@example.test")).toBeInTheDocument();
    expect(screen.getByText("Connected and ready")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add account" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reconnect Finance Mail (finance@example.test)" }));
    expect(onConnectionOAuth).toHaveBeenCalledWith(
      expect.anything(),
      connection,
      "reconnect_account",
    );
    await user.click(screen.getByRole("button", { name: "Disconnect Finance Mail (finance@example.test)" }));
    expect(onConnectionOAuthDisconnect).toHaveBeenCalledWith(expect.anything(), connection);
  });

  it("identifies two Mail accounts in row actions and disables mutations while busy", () => {
    const connections = [
      {
        connection_id: "mail-invoices",
        extension_id: "fixture.extension-oauth",
        connection_type: "fixture_mail_account",
        display_name: "Invoices",
        connection_config: {
          oauth: { lifecycle_status: "connected", connected_email: "invoices@example.test" },
        },
      },
      {
        connection_id: "mail-stock",
        extension_id: "fixture.extension-oauth",
        connection_type: "fixture_mail_account",
        display_name: "Stock",
        connection_config: {
          oauth: { lifecycle_status: "reconnect_required", connected_email: "stock@example.test" },
        },
      },
    ];

    render(
      <>
        {renderManifestComponent(
          {
            component_id: "mail_accounts",
            type: "connection_form",
            title: "Mail accounts",
            data: { connection_type: "fixture_mail_account", reconnect_intent: "reconnect_account" },
          },
          renderContext({
            busy: true,
            extensionId: "fixture.extension-oauth",
            oauthConnectionTypes: ["fixture_mail_account"],
            projectConnections: connections,
          }),
        )}
      </>,
    );

    for (const name of [
      "Edit Invoices (invoices@example.test)",
      "Disconnect Invoices (invoices@example.test)",
      "Remove Invoices (invoices@example.test)",
      "Edit Stock (stock@example.test)",
      "Reconnect Stock (stock@example.test)",
      "Remove Stock (stock@example.test)",
    ]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    expect(
      screen.getByRole("group", {
        name: "Invoices (invoices@example.test) actions",
      }),
    ).toHaveClass("fs-manifest-actions");
  });

  it("shows and targets only accounts owned by the exact extension install", async () => {
    const user = userEvent.setup();
    const onConnectionOAuth = vi.fn();
    const currentConnection = {
      connection_id: "mail-current",
      extension_id: "fixture.extension-oauth",
      extension_install_id: "install-current",
      connection_type: "fixture_mail_account",
      display_name: "Current account",
      connection_config: {
        oauth: { lifecycle_status: "connected", connected_email: "current@example.test" },
      },
    };
    const foreignConnection = {
      ...currentConnection,
      connection_id: "mail-foreign",
      extension_install_id: "install-foreign",
      display_name: "Foreign account",
      connection_config: {
        oauth: { lifecycle_status: "connected", connected_email: "foreign@example.test" },
      },
    };

    render(
      <>
        {renderManifestComponent(
          {
            component_id: "mail_accounts",
            type: "connection_form",
            title: "Mail accounts",
            data: { connection_type: "fixture_mail_account", reconnect_intent: "reconnect_account" },
          },
          renderContext({
            extensionId: "fixture.extension-oauth",
            extensionInstallId: "install-current",
            oauthConnectionTypes: ["fixture_mail_account"],
            projectConnections: [currentConnection, foreignConnection],
            onConnectionOAuth,
          }),
        )}
      </>,
    );

    expect(screen.getByText("1 configured")).toBeInTheDocument();
    expect(screen.getByText("current@example.test")).toBeInTheDocument();
    expect(screen.queryByText("foreign@example.test")).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: "Reconnect Current account (current@example.test)",
      }),
    );
    expect(onConnectionOAuth).toHaveBeenCalledWith(
      expect.anything(),
      currentConnection,
      "reconnect_account",
    );
  });

  it("keeps independent OAuth connection types isolated in the generic renderer", async () => {
    const user = userEvent.setup();
    const onConnectionOAuth = vi.fn();
    const disabledFiles = {
      connection_id: "fixture-invoices",
      extension_id: "fixture.extension",
      connection_type: "files_account",
      display_name: "Invoices",
      runtime_status: "ready",
      connection_config: {
        oauth: {
          lifecycle_status: "connected",
          connected_email: "invoices@example.test",
          services: {
            mail: { enabled: true, readiness: "ready" },
            files: { enabled: false, readiness: "disabled" },
            records: { enabled: false, readiness: "disabled" },
          },
        },
      },
    };

    render(
      <>
        {renderManifestComponent(
          {
            component_id: "files_accounts",
            type: "connection_form",
            title: "Files accounts",
            data: {
              connection_type: "files_account",
              oauth_intent: "files_connect",
              account_label: "Files account",
            },
          },
          renderContext({
            extensionId: "fixture.extension",
            oauthConnectionTypes: ["files_account"],
            projectConnections: [disabledFiles],
            onConnectionOAuth,
          }),
        )}
      </>,
    );

    expect(screen.getByText("1 configured")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reconnect Invoices (invoices@example.test)" }));
    expect(onConnectionOAuth).toHaveBeenCalledWith(expect.anything(), disabledFiles, "files_connect");
  });

  it("renders a generic connection creation block without provider-specific guidance", () => {
    const disabledFiles = {
      connection_id: "fixture-records",
      extension_id: "fixture.extension-oauth",
      connection_type: "fixture_mail_account",
      display_name: "Stock",
      runtime_status: "ready",
      connection_config: {
        oauth: {
          lifecycle_status: "connected",
          connected_email: "stock@example.test",
          services: {
            mail: { enabled: true, readiness: "ready" },
            files: { enabled: false, readiness: "disabled" },
            records: { enabled: false, readiness: "disabled" },
          },
        },
      },
    };
    const pickerGuidance =
      "This connection type is not configured for new connections.";

    render(
      <>
        {renderManifestComponent(
          {
            component_id: "files_accounts",
            type: "connection_form",
            title: "Files accounts",
            data: {
              connection_type: "files_account",
              oauth_intent: "files_connect",
              account_label: "Files account",
            },
            fields: [{ path: "display_name", label: "Display name", widget: "text" }],
          },
          renderContext({
            connectionCreationBlockedReason: pickerGuidance,
            extensionId: "fixture.extension-oauth",
            formValuesByComponent: {
              files_accounts: {
                connection_id: "fixture-records",
                display_name: "Stock",
              },
            },
            oauthConnectionTypes: ["files_account"],
            projectConnections: [disabledFiles],
          }),
        )}
      </>,
    );

    expect(screen.getByRole("button", { name: "Add Files account" })).toHaveAttribute("title", pickerGuidance);
  });

  it("does not render an OAuth connection form owned by the page composer", () => {
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "mail_accounts",
            type: "connection_form",
            title: "Mail accounts",
            data: { connection_type: "mail_account", oauth_provider_id: "fixture_oauth" },
          },
          renderContext({
            extensionId: "fixture.extension-oauth",
            oauthConnectionTypes: ["mail_account"],
            oauthConnectionProviderIds: ["fixture_oauth"],
            projectConnections: [{
              connection_id: "mail-disabled-api",
              extension_id: "fixture.extension-oauth",
              connection_type: "fixture_mail_account",
              display_name: "Invoices",
              health_status: "degraded",
              runtime_status: "unavailable",
              last_error: "Mail API is unavailable",
              connection_config: { oauth: { lifecycle_status: "connected" } },
            }],
          }),
        )}
      </>,
    );

    expect(screen.queryByText("Mail API unavailable")).not.toBeInTheDocument();
  });

  it.each(["policy_denied", "missing_required_scope"])(
    "keeps raw OAuth reconnect reason out of a generic connection row for %s",
    (reconnectReason) => {
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "mail_accounts",
            type: "connection_form",
            title: "Mail accounts",
            data: { connection_type: "mail_account" },
          },
          renderContext({
            extensionId: "fixture.extension-oauth",
            oauthConnectionTypes: ["mail_account"],
            projectConnections: [{
              connection_id: "mail-policy-denied",
              extension_id: "fixture.extension-oauth",
              connection_type: "fixture_mail_account",
              display_name: "Workspace mailbox",
              health_status: "degraded",
              runtime_status: "unavailable",
              last_error: "TECHNICAL_PROVIDER_CANARY",
              connection_config: {
                oauth: {
                  lifecycle_status: "reconnect_required",
                  reconnect_reason: reconnectReason,
                },
              },
            }],
          }),
        )}
      </>,
    );

    expect(screen.queryByText("Reconnect required")).not.toBeInTheDocument();
    expect(screen.queryByText(reconnectReason)).not.toBeInTheDocument();
    expect(screen.queryByText("TECHNICAL_PROVIDER_CANARY")).not.toBeInTheDocument();
    },
  );

  it("renders top-level stats alongside a nested query result", () => {
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "showcase_stats",
            title: "Included Coverage",
            type: "stats_row",
          },
          renderContext({
            componentQueryData: {
              showcase_stats: {
                items: [
                  { label: "Workflows", value: "10" },
                  { label: "Agents", value: "5" },
                ],
                result: { checks: [{ name: "Runtime", status: "healthy" }] },
              },
            },
          }),
        )}
      </>,
    );

    expect(screen.getByText("Workflows")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("Agents")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
  });

  it("renders canonical markdown body fields", () => {
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "overview",
            title: "Overview",
            type: "markdown",
            body: "Example extension renders Markdown body content.",
          },
          renderContext(),
        )}
      </>,
    );

    expect(screen.getByText("Overview")).toBeInTheDocument();
    expect(
      screen.getByText("Example extension renders Markdown body content."),
    ).toBeInTheDocument();
  });

  it("renders only safe absolute links from extension Markdown", () => {
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "safe_links",
            title: "Setup links",
            type: "markdown",
            body: "[Official guide](https://developers.example.test/setup)\n\n[Unsafe](javascript:alert(1))",
          },
          renderContext(),
        )}
      </>,
    );

    expect(screen.getByRole("link", { name: "Official guide" })).toHaveAttribute(
      "href",
      "https://developers.example.test/setup",
    );
    expect(screen.queryByRole("link", { name: "Unsafe" })).not.toBeInTheDocument();
    expect(screen.getByText(/Unsafe/)).toBeInTheDocument();
  });

  it("passes the selected eligible channel row to its test action", async () => {
    const user = userEvent.setup();
    const onManifestAction = vi.fn();
    const rows = [
      {
        channel_id: "old-self-bot",
        display_name: "Old self bot",
        enabled: true,
        capabilities: { notify: true },
      },
      {
        channel_id: "release-audit-group",
        display_name: "Release audit group",
        enabled: true,
        capabilities: { notify: true },
      },
      {
        channel_id: "disabled-channel",
        display_name: "Disabled channel",
        enabled: false,
        capabilities: { notify: true },
      },
      {
        channel_id: "human-only",
        display_name: "Human decisions only",
        enabled: true,
        capabilities: { notify: false },
      },
    ];
    const component = {
      component_id: "channel_test_grid",
      type: "grid",
      title: "Test a Channel",
      columns: [
        { key: "display_name", label: "Channel" },
        { key: "enabled", label: "Enabled" },
        { key: "capabilities.notify", label: "Notify" },
      ],
      row_actions: [
        {
          action_id: "test_delivery",
          label: "Send test",
          scope: "row",
          visible_if: [
            { path: "enabled", eq: true },
            { path: "capabilities.notify", eq: true },
          ],
        },
      ],
    };

    render(
      <>
        {renderManifestComponent(
          component,
          renderContext({
            componentQueryData: {
              channel_test_grid: { items: rows, total: rows.length },
            },
            onManifestAction,
          }),
        )}
      </>,
    );

    const testButtons = screen.getAllByRole("button", { name: "Send test" });
    expect(testButtons).toHaveLength(2);
    await user.click(testButtons[1]);
    expect(onManifestAction).toHaveBeenCalledWith(
      expect.objectContaining({ actionId: "test_delivery" }),
      null,
      component,
      {
        target: expect.objectContaining({
          channel_id: "release-audit-group",
          scope: "row",
        }),
        data: rows[1],
      },
    );
  });

  it("paginates an unpaged grid response locally", async () => {
    const user = userEvent.setup();
    const connectors = Array.from({ length: 40 }, (_, index) => ({
      connector_type_id: `toolbox_connector_${index + 1}`,
      display_name: `Connector ${index + 1}`,
    }));

    function GridHarness() {
      const [queryParams, setQueryParams] = useState<Record<string, unknown>>({});
      return (
        <>
          {renderManifestComponent(
            {
              component_id: "catalog",
              type: "grid",
              title: "Supported connectors",
              columns: [
                { key: "connector_type_id", label: "Connector type" },
                { key: "display_name", label: "Display name" },
              ],
            },
            renderContext({
              componentQueryData: { catalog: { connectors, total: 40 } },
              componentQueryParams: { catalog: queryParams },
              onComponentQueryParamsChange: (_componentId, nextParams) => {
                setQueryParams(nextParams);
              },
            }),
          )}
        </>
      );
    }

    render(<GridHarness />);

    expect(screen.getByText("Connector 1")).toBeInTheDocument();
    expect(screen.queryByText("Connector 21")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(screen.queryByText("Connector 1")).not.toBeInTheDocument();
    expect(screen.getByText("Connector 21")).toBeInTheDocument();
  });
  it("renders a collapsible markdown component closed, with the title as its summary", () => {
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "setup_guide",
            type: "markdown",
            title: "Detailed guide",
            collapsible: true,
            content: "## Token revocation\n\nRevoke it in @BotFather.",
          },
          renderContext(),
        )}
      </>,
    );

    // Disclosure placement is the point of this test, as in the result-panel suite.
    // eslint-disable-next-line testing-library/no-node-access -- asserting summary vs. disclosure placement.
    const disclosure = screen.getByText("Detailed guide").closest("details");
    expect(disclosure).not.toBeNull();
    expect(disclosure).not.toHaveAttribute("open");
    expect(screen.getByRole("heading", { name: "Token revocation" })).toBeInTheDocument();
  });

  it("offers a readiness row's contextual action and navigates to its anchor", async () => {
    const user = userEvent.setup();
    const onNavigateToAnchor = vi.fn();
    render(
      <>
        {renderManifestComponent(
          {
            component_id: "health_panel",
            type: "health_panel",
            title: "Readiness",
            query_id: "provider_health",
          },
          renderContext({
            onNavigateToAnchor,
            componentQueryData: {
              health_panel: {
                result: {
                  summary: { status: "Setup incomplete" },
                  checks: [
                    {
                      name: "Bot",
                      status: "warning",
                      label: "Setup incomplete",
                      detail: "Setup incomplete: no bot token saved.",
                      action: { label: "Configure bot", page_id: "setup", component_id: "secrets_form" },
                    },
                    {
                      name: "Channels",
                      status: "healthy",
                      label: "Ready",
                      detail: "Enabled: 1 notification, 0 human-decision",
                    },
                  ],
                },
              },
            },
          }),
        )}
      </>,
    );

    expect(screen.getByText("Setup incomplete: no bot token saved.")).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Configure bot" }));
    expect(onNavigateToAnchor).toHaveBeenCalledWith("setup", "secrets_form");
  });
});
