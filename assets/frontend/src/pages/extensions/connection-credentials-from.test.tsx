import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { vi } from "vitest";

import type { OAuthConfigurationView } from "@/pages/extensions/extension-oauth-api";
import type { ManifestRenderContext } from "@/pages/extensions/extension-manifest-render-model";
import type { ComponentError } from "@/pages/extensions/extension-manifest-utils";
import { renderManifestComponent } from "@/pages/extensions/extension-manifest-renderer";

import {
  connectionCredentialsFromByType,
  type CredentialsConfigurationOptions,
} from "./connection-credentials-from";

const manifest = {
  connection_types: [{
    connection_type_id: "example_app",
    credentials_from: {
      oauth_provider_id: "example_oauth",
      fields: {
        client_id: { into: "config", as: "client_id" },
        client_secret: { into: "secrets", as: "client_secret" },
      },
    },
  }],
};

const component = {
  component_id: "app_connection",
  type: "connection_form",
  title: "Application connections",
  data: { connection_type: "example_app", required_secret_fields: ["client_secret"] },
  fields: [
    { path: "display_name", label: "Display name" },
    { path: "site", label: "Site" },
    { path: "client_id", label: "Client ID" },
    { path: "client_secret", label: "Client secret", widget: "secret" },
  ],
};

function configuration(configurationId: string, displayName: string): OAuthConfigurationView {
  return {
    configuration_id: configurationId,
    display_name: displayName,
    status: "ready_for_authorization",
    fields: [],
    revision: 1,
    authorization_revision: 1,
  };
}

function options(configurations: OAuthConfigurationView[], setupPageId = "setup"): CredentialsConfigurationOptions {
  return { status: "ready", configurations, setupPageId };
}

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
    extensionId: "example.app-extension",
    extensionInstallId: "install-1",
    extensionReleaseId: "",
    extensionDescriptorSha256: "",
    formValuesByComponent: {},
    oauthConnectionTypes: [],
    connectionCredentialsFrom: connectionCredentialsFromByType(manifest),
    projectConnections: [],
    projectId: "project-1",
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

/** The form with its values kept, as the extension page keeps them. */
function ConnectionForm({ context }: Readonly<{ context: Partial<ManifestRenderContext> }>) {
  const [values, setValues] = useState<Record<string, unknown>>({});
  return (
    <>
      {renderManifestComponent(component, renderContext({
        formValuesByComponent: { app_connection: values },
        onFieldChange: (_componentId, fieldName, value) => setValues((current) => ({ ...current, [fieldName]: value })),
        ...context,
      }))}
    </>
  );
}

function renderForm(context: Partial<ManifestRenderContext>) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ConnectionForm context={context} />
    </QueryClientProvider>,
  );
}

describe("connection form for a type that takes its credentials from an OAuth configuration", () => {
  it("reads credentials_from declarations from the manifest's connection types", () => {
    expect(connectionCredentialsFromByType({
      connection_types: [
        ...manifest.connection_types,
        { connection_type_id: "example_plain" },
        { connection_type_id: "example_named", credentials_from: { oauth_provider_id: "example_oauth", fields: { tenant: { into: "config" }, bad: { into: "elsewhere" } } } },
      ],
    })).toEqual({
      example_app: {
        oauthProviderId: "example_oauth",
        fields: { client_id: { into: "config", as: "client_id" }, client_secret: { into: "secrets", as: "client_secret" } },
      },
      example_named: { oauthProviderId: "example_oauth", fields: { tenant: { into: "config", as: "tenant" } } },
    });
  });

  it("preselects the only configuration, hides borrowed fields and saves the chosen configuration", async () => {
    const user = userEvent.setup();
    const onConnectionSave = vi.fn();
    renderForm({
      credentialsConfigurations: { example_oauth: options([configuration("cfg-1", "Example app")]) },
      onConnectionSave,
    });

    const form = screen.getByRole("form", { name: "Application connections" });
    expect(within(form).getByRole("combobox", { name: "Sign-in configuration" })).toHaveTextContent("Example app");
    expect(within(form).getByLabelText("Site")).toBeVisible();
    expect(within(form).queryByLabelText("Client ID")).not.toBeInTheDocument();
    expect(within(form).queryByLabelText("Client secret")).not.toBeInTheDocument();

    await user.click(within(form).getByRole("button", { name: "Create connection" }));

    expect(onConnectionSave).toHaveBeenCalledWith(
      component,
      expect.objectContaining({ credentials_configuration_id: "cfg-1" }),
    );
  });

  it("preselects nothing when there are two configurations and records the one chosen", async () => {
    const user = userEvent.setup();
    const onConnectionSave = vi.fn();
    renderForm({
      credentialsConfigurations: {
        example_oauth: options([configuration("cfg-1", "Example app"), configuration("cfg-2", "Example backup app")]),
      },
      onConnectionSave,
    });

    const select = screen.getByRole("combobox", { name: "Sign-in configuration" });
    expect(select).toHaveTextContent("Choose a sign-in configuration");
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    expect(onConnectionSave).toHaveBeenLastCalledWith(
      component,
      expect.objectContaining({ credentials_configuration_id: "" }),
    );

    await user.click(select);
    await user.click(screen.getByRole("option", { name: "Example backup app" }));
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    expect(onConnectionSave).toHaveBeenLastCalledWith(
      component,
      expect.objectContaining({ credentials_configuration_id: "cfg-2" }),
    );
  });

  it("shows a refusal to save without a configuration beside the select, not above the form", () => {
    const componentErrors: Record<string, ComponentError> = {
      app_connection: {
        message: "Choose the sign-in configuration this connection takes its credentials from.",
        fieldPath: "credentials_configuration_id",
      },
    };
    renderForm({
      componentErrors,
      credentialsConfigurations: {
        example_oauth: options([configuration("cfg-1", "Example app"), configuration("cfg-2", "Example backup app")]),
      },
    });

    const select = screen.getByRole("combobox", { name: "Sign-in configuration" });
    expect(select).toHaveAccessibleDescription(/Choose the sign-in configuration this connection takes its credentials from\./);
    expect(screen.getAllByText("Choose the sign-in configuration this connection takes its credentials from.")).toHaveLength(1);
  });

  it("says when there is no configuration yet and opens the provider setup", async () => {
    const user = userEvent.setup();
    const onNavigateToAnchor = vi.fn();
    renderForm({
      credentialsConfigurations: { example_oauth: options([], "app_setup") },
      onNavigateToAnchor,
    });

    expect(screen.getByText("No sign-in configuration yet")).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Sign-in configuration" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open provider setup" }));
    expect(onNavigateToAnchor).toHaveBeenCalledWith("app_setup", "");
  });

  it("explains a connection whose credentials are unavailable", () => {
    renderForm({
      credentialsConfigurations: { example_oauth: options([configuration("cfg-2", "Example backup app")]) },
      projectConnections: [{
        connection_id: "connection-1",
        extension_id: "example.app-extension",
        extension_install_id: "install-1",
        connection_type: "example_app",
        display_name: "Reports",
        credentials_configuration_id: "cfg-1",
        credentials_status: "credentials_unavailable",
        runtime_status: "not_ready",
        last_error: "credentials_unavailable",
      }],
    });

    expect(screen.getByText("Credentials unavailable: its sign-in configuration was removed or is incomplete.")).toBeVisible();
  });
});
