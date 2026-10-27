import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { connectionActionTarget } from "./connection-form-support";
import { connectionPayloadForSave, prepareConnectionSave } from "./connection-save";
import { renderManifestField } from "./extension-manifest-fields";
import { collectManifestFormValues, type ManifestRenderContext } from "./extension-manifest-render-model";
import { renderManifestComponent } from "./extension-manifest-renderer";
import { useExtensionConnectionActions } from "./use-extension-connection-actions";
import type { useExtensionLifecycleActions } from "./use-extension-lifecycle-actions";
import type { useExtensionsHubState } from "./use-extensions-hub-state";

const component = {
  component_id: "api", type: "connection_form", title: "API connection",
  data: { connection_type: "fixture_api", config_fields: ["allowed_hosts"], test_action: "test_request" },
  defaults: { allowed_hosts: [], resource_id: "resource-1", perform_live_request: false },
  fields: [
    { path: "allowed_hosts", widget: "tag_picker", label: "Allowed Hosts", help: "Only these hosts may be called." },
    { path: "resource_id", widget: "text", label: "Resource" },
    { path: "perform_live_request", widget: "boolean", label: "Live request" },
  ],
};
const storedRow = {
  connection_id: "connection-1", extension_id: "fixture.extension", extension_install_id: "install-1",
  connection_type: "fixture_api", display_name: "Stored API", enabled: true,
  connection_config: { allowed_hosts: ["saved.example.test", "backup.example.test"] },
};

function NativeForm({ row = storedRow, onSave, onAction }: {
  row?: Record<string, unknown>;
  onSave: ManifestRenderContext["onConnectionSave"];
  onAction: ManifestRenderContext["onManifestAction"];
}) {
  const [values, setFormValuesByComponent] = useState<Record<string, Record<string, unknown>>>({});
  const state = {
    detailExtension: { extension_id: "fixture.extension" }, extensionInstallId: "install-1",
    activePageProjectId: "project-1", projectConnectionsQuery: { data: [row] },
    archiveLifecycleQuery: { isPending: false }, archiveLifecycleVerificationFailed: false, archiveLifecycleUnavailable: false,
    setFormValuesByComponent, setComponentErrors: () => undefined, setComponentError: () => undefined,
  } as unknown as ReturnType<typeof useExtensionsHubState>;
  const actions = useExtensionConnectionActions(state, {} as ReturnType<typeof useExtensionLifecycleActions>);
  const context: ManifestRenderContext = {
    busy: false, canManageProject: true, connectionCreationBlockedReason: "",
    callbacks: [], callbackStatuses: {}, componentErrors: {}, componentQueryParams: {}, componentQueryData: {},
    extensionId: "fixture.extension", extensionInstallId: "install-1", extensionReleaseId: "", extensionDescriptorSha256: "",
    formValuesByComponent: values, oauthConnectionTypes: [], projectId: "project-1", projectConnections: [row], projectState: [],
    onCallbackSetup: () => undefined, onComponentQueryParamsChange: () => undefined,
    onFieldChange: (id, name, value) => setFormValuesByComponent((current) => ({ ...current, [id]: { ...current[id], [name]: value } })),
    onExternalResourcePickerBootstrap: async () => ({}), onManifestAction: onAction, onConnectionSave: onSave,
    onConnectionDelete: () => undefined, onConnectionOAuth: () => undefined, onConnectionOAuthDisconnect: () => undefined,
    onConnectionSelect: actions.handleConnectionSelect, onProjectStateSave: () => undefined,
    onProjectStateDelete: () => undefined, onProjectStateSelect: () => undefined,
  };
  return <>{renderManifestComponent(component, context)}</>;
}

describe("native manifest string-array tags", () => {
  it("hydrates a selected stored row, edits arrays, and preserves save and request identity", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn();
    const onAction = vi.fn();
    const view = render(<NativeForm onSave={onSave} onAction={onAction} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByText("saved.example.test")).toBeVisible();
    expect(screen.getByText("backup.example.test")).toBeVisible();
    const draft = screen.getByLabelText("Allowed Hosts");
    expect(draft).not.toHaveAttribute("name");
    expect(draft).toHaveAccessibleDescription("Only these hosts may be called.");
    await user.type(draft, "third.example.test{Enter}");
    await user.click(screen.getByRole("button", { name: "Remove saved.example.test" }));
    await user.type(draft, "draft-must-not-replace-array");
    expect(onSave).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Save connection" }));
    const values = onSave.mock.calls[0][1];
    expect(values).toMatchObject({ connection_id: "connection-1", allowed_hosts: ["backup.example.test", "third.example.test"] });
    const preparation = prepareConnectionSave({ component, values, projectConnections: [storedRow], oauthConnectionTypes: [], connectionMatchesCurrentInstall: () => true });
    const payload = connectionPayloadForSave({ component, fieldValues: values, preparation, extensionKey: "fixture.extension", extensionInstallId: "install-1" });
    expect(payload).toMatchObject({ extension_id: "fixture.extension", extension_install_id: "install-1", connection_config: { allowed_hosts: ["backup.example.test", "third.example.test"] } });
    expect(Object.keys(payload.connection_config as object)).toEqual(["allowed_hosts"]);
    await user.click(screen.getByRole("button", { name: "Test" }));
    const [, form, actionComponent, overrides] = onAction.mock.calls[0];
    const requestValues = { ...overrides.values, ...collectManifestFormValues(form) };
    expect(requestValues.allowed_hosts).toEqual(["backup.example.test", "third.example.test"]);
    expect(connectionActionTarget(actionComponent, requestValues)).toMatchObject({ connection_id: "connection-1" });
    view.unmount();
    // Fresh host row; database durability remains a real browser acceptance concern.
    render(<NativeForm row={{ ...storedRow, connection_config: JSON.stringify({ allowed_hosts: ["reloaded.example.test"] }) }} onSave={onSave} onAction={onAction} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByText("reloaded.example.test")).toBeVisible();
    expect(screen.queryByText("backup.example.test")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Allowed Hosts")).toHaveValue("");
  });

  it("adds and removes tags accessibly without submitting, retaining an array when empty", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn((event) => event.preventDefault());
    const onChange = vi.fn();
    function Field() {
      const [values, setValues] = useState<Record<string, unknown>>({ allowed_hosts: ["saved.example.test"] });
      return <form onSubmit={onSubmit}>
        {renderManifestField({ componentId: "api", field: component.fields[0], values, invalidFieldPath: "allowed_hosts", invalidMessage: "Check host", onFieldChange: (name, value) => { onChange(name, value); setValues({ [name]: value }); } })}
        <button type="submit">Submit form</button>
      </form>;
    }
    render(<Field />);
    const draft = screen.getByLabelText("Allowed Hosts");
    expect(draft).toHaveAttribute("aria-invalid", "true");
    expect(draft).toHaveAccessibleDescription("Check host Only these hosts may be called.");
    await user.type(draft, "  next.example.test  {Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onChange).toHaveBeenLastCalledWith("allowed_hosts", ["saved.example.test", "next.example.test"]);
    await user.type(draft, "next.example.test{Enter}");
    expect(onChange).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(draft, { key: "Enter", isComposing: true });
    expect(onChange).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Remove saved.example.test" }));
    await user.click(screen.getByRole("button", { name: "Remove next.example.test" }));
    expect(onChange).toHaveBeenLastCalledWith("allowed_hosts", []);
    await user.type(draft, "button.example.test");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(onChange).toHaveBeenLastCalledWith("allowed_hosts", ["button.example.test"]);
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
