import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { connectionPayloadForSave, prepareConnectionSave } from "./connection-save";
import type { ManifestRenderContext } from "./extension-manifest-render-model";
import { renderManifestComponent } from "./extension-manifest-renderer";

// Production-shaped manifest; external bundle tests own its YAML and policy copy.
const component = {
  component_id: "smtp", type: "connection_form", title: "SMTP connection",
  data: { connection_type: "custom_smtp", config_fields: ["security"], required_secret_fields: [] },
  defaults: { security: "starttls" },
  fields: [
    { path: "environment", label: "Environment", widget: "text", description: "Connection metadata does not change deployment policy." },
    { path: "security", label: "Security", widget: "select", required: true,
      description: "Use TLS. No TLS requires an operator-provisioned development/test fixture without authentication.",
      options: [
        { value: "starttls", label: "STARTTLS" },
        { value: "ssl", label: "SSL/TLS" },
        { value: "none", label: "None / no TLS (unavailable in this form)", disabled: true },
      ] },
  ],
};
const storedRow = {
  connection_id: "smtp-1", extension_id: "fixture.smtp", connection_type: "custom_smtp",
  display_name: "Stored fixture", enabled: true, connection_config: { security: "none" },
};

function ConnectionForm({ saved = false, onSave }: {
  saved?: boolean;
  onSave: ManifestRenderContext["onConnectionSave"];
}) {
  const [values, setValues] = useState<Record<string, unknown>>(
    saved ? { connection_id: "smtp-1", environment: "development", security: "none" } : {},
  );
  const context: ManifestRenderContext = {
    busy: false, canManageProject: true, connectionCreationBlockedReason: "",
    callbacks: [], callbackStatuses: {}, componentErrors: {}, componentQueryParams: {}, componentQueryData: {},
    extensionId: "fixture.smtp", extensionInstallId: "", extensionReleaseId: "", extensionDescriptorSha256: "",
    formValuesByComponent: { smtp: values }, oauthConnectionTypes: [], projectId: "project-1",
    projectConnections: saved ? [storedRow] : [], projectState: [],
    onCallbackSetup: () => undefined, onComponentQueryParamsChange: () => undefined,
    onFieldChange: (_id, name, value) => setValues((current) => ({ ...current, [name]: value })),
    onExternalResourcePickerBootstrap: async () => ({}), onManifestAction: () => undefined, onConnectionSave: onSave,
    onConnectionDelete: () => undefined, onConnectionOAuth: () => undefined, onConnectionOAuthDisconnect: () => undefined,
    onConnectionSelect: () => undefined, onProjectStateSave: () => undefined,
    onProjectStateDelete: () => undefined, onProjectStateSelect: () => undefined,
  };
  return <>{renderManifestComponent(component, context)}</>;
}

it("keeps TLS usable while a manifest-disabled no-TLS choice cannot be selected by click or keyboard", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn();
  render(<ConnectionForm onSave={onSave} />);
  const security = screen.getByRole("combobox", { name: "Security" });
  expect(security).toHaveTextContent("STARTTLS");
  expect(security).toHaveAccessibleDescription(component.fields[1].description);
  expect(screen.getByRole("textbox", { name: "Environment" })).toHaveAccessibleDescription(component.fields[0].description);
  await user.click(security);
  const unavailable = screen.getByRole("option", { name: "None / no TLS (unavailable in this form)" });
  expect(unavailable).toBeDisabled();
  await user.click(unavailable);
  expect(security).toHaveTextContent("STARTTLS");
  await user.click(screen.getByRole("option", { name: "SSL/TLS" }));
  expect(security).toHaveTextContent("SSL/TLS");
  security.focus();
  await user.keyboard("{ArrowDown}{End}");
  await waitFor(() => expect(screen.getByRole("option", { name: "SSL/TLS" })).toHaveFocus());
  await user.keyboard("{ArrowDown}");
  await waitFor(() => expect(screen.getByRole("option", { name: "STARTTLS" })).toHaveFocus());
  await user.keyboard("{Enter}");
  expect(security).toHaveTextContent("STARTTLS");
  await user.click(screen.getByRole("button", { name: "Create connection" }));
  expect(onSave).toHaveBeenCalledWith(component, { security: "starttls" });
});

it("honestly displays and saves a stored disabled value until TLS is explicitly selected", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn();
  const view = render(<ConnectionForm saved onSave={onSave} />);
  const security = screen.getByRole("combobox", { name: "Security" });
  expect(security).toHaveTextContent("None / no TLS (unavailable in this form)");
  await user.click(security);
  const unavailable = screen.getByRole("option", { name: "None / no TLS (unavailable in this form)" });
  expect(unavailable).toBeDisabled();
  expect(unavailable).toHaveAttribute("aria-selected", "true");
  await user.keyboard("{Escape}");
  await user.click(screen.getByRole("button", { name: "Save connection" }));
  const values = onSave.mock.calls[0][1];
  const preparation = prepareConnectionSave({ component, values, projectConnections: [storedRow], oauthConnectionTypes: [], connectionMatchesCurrentInstall: () => true });
  const payload = connectionPayloadForSave({ component, fieldValues: values, preparation, extensionKey: "fixture.smtp", extensionInstallId: "" });
  expect(payload).toMatchObject({ environment: "development", connection_config: { security: "none" } });
  expect(values.security).toBe("none");
  view.unmount();
  render(<ConnectionForm saved onSave={onSave} />);
  const reloaded = screen.getByRole("combobox", { name: "Security" });
  expect(reloaded).toHaveTextContent("None / no TLS (unavailable in this form)");
  await user.click(reloaded);
  await user.click(screen.getByRole("option", { name: "STARTTLS" }));
  await user.click(screen.getByRole("button", { name: "Save connection" }));
  expect(onSave.mock.calls[1][1]).toMatchObject({ security: "starttls" });
});
