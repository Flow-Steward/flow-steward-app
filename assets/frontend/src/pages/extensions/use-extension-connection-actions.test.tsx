import { act, renderHook } from "@testing-library/react";
import { useState } from "react";

import { useExtensionConnectionActions } from "./use-extension-connection-actions";
import type { useExtensionsHubState } from "./use-extensions-hub-state";
import type { useExtensionLifecycleActions } from "./use-extension-lifecycle-actions";
import type { ComponentError } from "./extension-manifest-utils";
import {
  createProjectConnection,
  deleteExtensionProjectState,
  deleteProjectConnection,
  disconnectProjectConnectionOAuth,
  putExtensionProjectState,
  saveProjectConnectionSecrets,
  startProjectConnectionOAuth,
  updateProjectConnection,
} from "@/pages/projects/connections/api";

vi.mock("@/pages/projects/connections/api", async (importOriginal) => ({
  ...await importOriginal<Record<string, unknown>>(),
  createProjectConnection: vi.fn(),
  deleteExtensionProjectState: vi.fn(),
  deleteProjectConnection: vi.fn(),
  disconnectProjectConnectionOAuth: vi.fn(),
  putExtensionProjectState: vi.fn(),
  saveProjectConnectionSecrets: vi.fn(),
  startProjectConnectionOAuth: vi.fn(),
  updateProjectConnection: vi.fn(),
}));

const component = {
  component_id: "account",
  data: { connection_type: "mail", config_fields: ["host"] },
  fields: [{ path: "host" }, { path: "password", widget: "secret" }],
  defaults: { host: "default.example" },
};
const connection = {
  extension_id: "mail-extension",
  extension_install_id: "install-1",
  connection_id: "connection-1",
  connection_type: "mail",
  display_name: "Mailbox",
  environment: "production",
  connection_config: { host: "saved.example", password: "private" }, // pragma: allowlist secret - non-authenticating redaction test sentinel
};
const stateComponent = {
  component_id: "state",
  data: { namespace: "settings", state_key_prefix: "folder:", key_field: "folder" },
  defaults: { folder: "", label: "Default" },
  fields: [{ path: "folder" }, { path: "label", required: true, label: "Folder label" }],
};

function renderActions(options: {
  projectId?: string;
  extensionId?: string;
  installId?: string;
  connections?: Record<string, unknown>[];
  oauthTypes?: string[];
  errors?: Record<string, ComponentError>;
} = {}) {
  const refetchConnections = vi.fn().mockResolvedValue({});
  const refetchState = vi.fn().mockResolvedValue({});
  const withBusy = vi.fn(async (_extensionId: string, run: () => Promise<void>) => run());
  const view = renderHook(() => {
    const [formValues, setFormValuesByComponent] = useState<Record<string, Record<string, unknown>>>({});
    const [errors, setComponentErrors] = useState<Record<string, ComponentError>>(options.errors ?? {});
    const [statuses, setStatusById] = useState<Record<string, string>>({});
    const state = {
      detailExtension: { extension_id: options.extensionId ?? "mail-extension" },
      extensionInstallId: options.installId ?? "install-1",
      activePageProjectId: options.projectId ?? "project-1",
      location: { pathname: "/extensions/mail-extension", search: "?project_id=project-1" },
      projectConnectionsQuery: { data: options.connections ?? [connection], refetch: refetchConnections },
      projectStateQuery: { refetch: refetchState },
      oauthConnectionTypes: options.oauthTypes ?? ["oauth-mail"],
      archiveLifecycleQuery: { isPending: false },
      archiveLifecycleVerificationFailed: false,
      archiveLifecycleUnavailable: false,
      setFormValuesByComponent,
      setComponentErrors,
      setStatusById,
      setComponentError: (target: Record<string, unknown>, message: string, fieldPath = "") => {
        const id = String(target.component_id || target.id || target.key);
        setComponentErrors((current) => ({ ...current, [id]: { message, fieldPath } }));
      },
    } as unknown as ReturnType<typeof useExtensionsHubState>;
    const actions = useExtensionConnectionActions(state, { withBusy } as unknown as ReturnType<typeof useExtensionLifecycleActions>);
    return { actions, formValues, errors, statuses };
  });
  return { ...view, refetchConnections, refetchState, withBusy };
}

describe("extension connection actions", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(createProjectConnection).mockResolvedValue({ connection_id: "created-1" });
    vi.mocked(updateProjectConnection).mockResolvedValue({});
    vi.mocked(saveProjectConnectionSecrets).mockResolvedValue({});
    vi.mocked(deleteProjectConnection).mockResolvedValue({});
    vi.mocked(disconnectProjectConnectionOAuth).mockResolvedValue({});
    vi.mocked(putExtensionProjectState).mockResolvedValue({});
    vi.mocked(deleteExtensionProjectState).mockResolvedValue({});
  });

  it("hydrates saved config without copying a secret into editable values and clears stale errors", () => {
    const { result } = renderActions({ errors: { account: { message: "Old error", fieldPath: "" } } });
    act(() => result.current.actions.handleConnectionSelect(component, connection));
    expect(result.current.formValues.account).toEqual({
      host: "saved.example", connection_id: "connection-1", display_name: "Mailbox", environment: "production",
    });
    expect(result.current.errors).toEqual({});
    act(() => result.current.actions.handleConnectionSelect(component, null));
    expect(result.current.formValues.account).toEqual({ connection_id: "" });
  });

  it("resets a variant form to its declared connection type", () => {
    const { result } = renderActions();
    act(() => result.current.actions.handleConnectionSelect({
      ...component, data: { connection_type_field: "provider", default_connection_type: "manual" },
    }, null));
    expect(result.current.formValues.account).toEqual({ provider: "manual", connection_id: "" });
  });

  it("clears only the error belonging to the field being edited", () => {
    const { result } = renderActions({ errors: { account: { message: "Host required", fieldPath: "host" } } });
    act(() => result.current.actions.clearFieldError("account", "password"));
    expect(result.current.errors.account.message).toBe("Host required");
    act(() => result.current.actions.clearFieldError("missing", "host"));
    expect(result.current.errors.account.message).toBe("Host required");
    act(() => result.current.actions.clearFieldError("account", "host"));
    expect(result.current.errors).toEqual({});
  });

  it.each(["handleConnectionSelect", "handleConnectionOAuth", "handleConnectionOAuthDisconnect", "handleConnectionDelete"] as const)(
    "%s rejects a row from a different installation before any action",
    async (action) => {
      const { result, withBusy } = renderActions();
      await act(async () => {
        await result.current.actions[action](component, { ...connection, extension_install_id: "foreign" }, "connect");
      });
      expect(result.current.errors.account.message).toBe("This connection belongs to another installed release. Refresh the page and choose an account from this installation.");
      expect(result.current.formValues).toEqual({});
      expect(result.current.actions.confirmation).toBeNull();
      expect(result.current.actions.pendingDisconnect).toBeNull();
      expect(withBusy).not.toHaveBeenCalled();
    },
  );

  it("allows an extension-matching row when the manifest has no installation identifier", () => {
    const { result } = renderActions({ installId: "" });
    act(() => result.current.actions.handleConnectionSelect(component, { ...connection, extension_install_id: "legacy" }));
    expect(result.current.formValues.account.connection_id).toBe("connection-1");
  });

  it("reports the missing project and unsaved connection before starting OAuth", async () => {
    const { result: missingProjectResult } = renderActions({ projectId: "" });
    await act(async () => missingProjectResult.current.actions.handleConnectionOAuth(component, connection, "connect"));
    expect(missingProjectResult.current.errors.account.message).toBe("Select a project before connecting an account.");
    const { result: unsavedResult } = renderActions();
    await act(async () => unsavedResult.current.actions.handleConnectionOAuth(component, { ...connection, connection_id: "" }, "connect"));
    expect(unsavedResult.current.errors.account.message).toBe("Save the connection before starting OAuth.");
    expect(startProjectConnectionOAuth).not.toHaveBeenCalled();
  });

  it("keeps an OAuth start failure visible and sends the current return path", async () => {
    vi.mocked(startProjectConnectionOAuth).mockResolvedValue({});
    const { result } = renderActions();
    await act(async () => result.current.actions.handleConnectionOAuth(component, connection, "reconnect"));
    expect(startProjectConnectionOAuth).toHaveBeenCalledWith("project-1", "connection-1", {
      intent: "reconnect", return_path: "/extensions/mail-extension?project_id=project-1",
    });
    expect(result.current.errors.account.message).toBe("Authorization couldn’t start. Try again or open the extension help page.");
  });

  it.each([{ projectId: "" }, { missingConnection: true }])("does not open destructive actions without a persisted project connection (%j)", async (options) => {
    const { result } = renderActions(options);
    const row = { ...connection, connection_id: options.missingConnection ? "" : "connection-1" };
    await act(async () => {
      await result.current.actions.handleConnectionOAuthDisconnect(component, row);
      await result.current.actions.confirmConnectionOAuthDisconnect(component, row);
      await result.current.actions.handleConnectionDelete(component, row);
    });
    expect(result.current.actions.pendingDisconnect).toBeNull();
    expect(result.current.actions.confirmation).toBeNull();
    expect(deleteProjectConnection).not.toHaveBeenCalled();
    expect(disconnectProjectConnectionOAuth).not.toHaveBeenCalled();
  });

  it("retains a disconnect failure for retry and refreshes the account only after success", async () => {
    vi.mocked(disconnectProjectConnectionOAuth).mockRejectedValueOnce(new Error("Provider unavailable"));
    const { result, refetchConnections } = renderActions();
    await act(async () => result.current.actions.handleConnectionOAuthDisconnect(component, connection));
    expect(result.current.actions.pendingDisconnect).toEqual({ connectionId: "connection-1", pending: false, error: "" });
    await act(async () => result.current.actions.confirmConnectionOAuthDisconnect(component, connection));
    expect(result.current.actions.pendingDisconnect).toEqual({ connectionId: "connection-1", pending: false, error: "Provider unavailable" });
    expect(refetchConnections).not.toHaveBeenCalled();
    await act(async () => result.current.actions.confirmConnectionOAuthDisconnect(component, connection));
    expect(disconnectProjectConnectionOAuth).toHaveBeenLastCalledWith("project-1", "connection-1");
    expect(refetchConnections).toHaveBeenCalledTimes(1);
    expect(result.current.actions.pendingDisconnect).toBeNull();
    expect(result.current.statuses).toEqual({ "mail-extension": "OAuth account disconnected." });
  });

  it("requires project scope before saving a connection", async () => {
    const { result } = renderActions({ projectId: "" });
    await act(async () => result.current.actions.handleConnectionSave(component, { host: "new.example" }));
    expect(result.current.errors.account.message).toBe("Select a project before configuring this extension.");
    expect(createProjectConnection).not.toHaveBeenCalled();
  });

  it("rejects an unavailable selected connection without persisting changes", async () => {
    const { result } = renderActions();
    await act(async () => result.current.actions.handleConnectionSave(component, { connection_id: "foreign", host: "new.example" }));
    expect(result.current.errors.account.message).toBe("This connection is not available in the current installed release.");
    expect(updateProjectConnection).not.toHaveBeenCalled();
    expect(createProjectConnection).not.toHaveBeenCalled();
  });

  it.each([false, true])("saves %s existing connection, refreshes, and selects non-secret values", async (existing) => {
    const { result, refetchConnections } = renderActions({ errors: { account: { message: "Old failure", fieldPath: "" } } });
    await act(async () => result.current.actions.handleConnectionSave(component, {
      connection_id: existing ? "connection-1" : "", host: "new.example", display_name: "Changed", environment: "test",
    }));
    expect(result.current.errors).toEqual({});
    expect(refetchConnections).toHaveBeenCalledTimes(1);
    expect(result.current.statuses["mail-extension"]).toBe(existing ? "Connection saved." : "Connection created.");
    expect(result.current.formValues.account).toEqual({
      connection_id: existing ? "connection-1" : "created-1", host: "new.example", display_name: "Changed", environment: "test",
    });
    if (existing) {
      expect(updateProjectConnection).toHaveBeenCalledWith("project-1", "connection-1", expect.objectContaining({ connection_config: expect.objectContaining({ host: "new.example" }) }));
      expect(createProjectConnection).not.toHaveBeenCalled();
    } else {
      expect(updateProjectConnection).toHaveBeenCalledWith("project-1", "created-1", { enabled: true });
    }
  });

  it("removes a newly created connection when secret persistence fails", async () => {
    vi.mocked(saveProjectConnectionSecrets).mockRejectedValueOnce(new Error("Secret store unavailable"));
    const { result, refetchConnections } = renderActions();
    await act(async () => result.current.actions.handleConnectionSave(component, { host: "new.example", password: "secret" })); // pragma: allowlist secret - mocked secret-store failure test sentinel
    expect(deleteProjectConnection).toHaveBeenCalledWith("project-1", "created-1");
    expect(refetchConnections).not.toHaveBeenCalled();
    expect(result.current.errors.account.message).toContain("Secret store unavailable");
    expect(result.current.formValues).toEqual({});
  });

  it("preserves a new OAuth connection after an authorization start failure so it can be retried", async () => {
    vi.mocked(startProjectConnectionOAuth).mockRejectedValue(new Error("Provider unavailable"));
    const { result, refetchConnections } = renderActions();
    await act(async () => result.current.actions.handleConnectionSave({
      ...component, data: { connection_type: "oauth-mail", oauth_intent: "connect" },
    }, { host: "new.example" }));
    expect(startProjectConnectionOAuth).toHaveBeenCalledWith("project-1", "created-1", {
      intent: "connect", return_path: "/extensions/mail-extension?project_id=project-1",
    });
    expect(deleteProjectConnection).not.toHaveBeenCalled();
    expect(refetchConnections).toHaveBeenCalledTimes(1);
    expect(result.current.errors.account.message).toBe("Authorization couldn’t start. Try again or open the extension help page.");
  });

  it("cancels deletion without deleting and keeps a failed confirmed deletion available for retry", async () => {
    vi.mocked(deleteProjectConnection).mockRejectedValueOnce(new Error("Connection in use"));
    const { result, refetchConnections } = renderActions();
    let deletion: Promise<void> = Promise.resolve();
    act(() => { deletion = result.current.actions.handleConnectionDelete(component, connection); });
    expect(result.current.actions.confirmation?.confirmLabel).toBe("Delete connection");
    await act(async () => { result.current.actions.cancelConfirmation(); await deletion; });
    expect(deleteProjectConnection).not.toHaveBeenCalled();
    act(() => { deletion = result.current.actions.handleConnectionDelete(component, { ...connection, connection_type: "oauth-mail" }); });
    expect(result.current.actions.confirmation?.confirmLabel).toBe("Remove account");
    await act(async () => result.current.actions.acceptConfirmation());
    expect(result.current.actions.confirmationError).toBe("Connection in use");
    expect(result.current.actions.confirmationPending).toBe(false);
    expect(result.current.actions.confirmation).not.toBeNull();
    await act(async () => { await result.current.actions.acceptConfirmation(); await deletion; });
    expect(result.current.actions.confirmation).toBeNull();
    expect(result.current.formValues.account).toEqual({ connection_id: "" });
    expect(refetchConnections).toHaveBeenCalledTimes(1);
  });

  it("restores state defaults and removes only the namespace prefix from a selected key", () => {
    const { result } = renderActions();
    act(() => result.current.actions.handleProjectStateSelect(stateComponent, {
      state_key: "folder:inbox", value_jsonb: { label: "Inbox" }, revision: 3,
    }));
    expect(result.current.formValues.state).toEqual({ folder: "inbox", label: "Inbox", state_key: "folder:inbox", revision: 3 });
    act(() => result.current.actions.handleProjectStateSelect(stateComponent, { state_key: "other:inbox", value_jsonb: {} }));
    expect(result.current.formValues.state.folder).toBe("other:inbox");
    act(() => result.current.actions.handleProjectStateSelect(stateComponent, null));
    expect(result.current.formValues.state).toEqual(stateComponent.defaults);
  });

  it.each(["", "with space", "with/slash", "with\nnewline"])("rejects invalid state key %j", async (folder) => {
    const { result } = renderActions();
    await act(async () => result.current.actions.handleProjectStateSave(stateComponent, { folder, label: "Inbox" }));
    expect(result.current.errors.state.message).toBe("State key is required and must not contain spaces, slashes, or newlines.");
    expect(putExtensionProjectState).not.toHaveBeenCalled();
  });

  it("requires project scope and required values before editing state", async () => {
    const { result: missingProjectResult } = renderActions({ projectId: "" });
    await act(async () => missingProjectResult.current.actions.handleProjectStateSave(stateComponent, { folder: "inbox", label: "Inbox" }));
    expect(missingProjectResult.current.errors.state.message).toBe("Select a project before editing extension state.");
    const { result: missingValueResult } = renderActions();
    await act(async () => missingValueResult.current.actions.handleProjectStateSave(stateComponent, { folder: "inbox", label: "" }));
    expect(missingValueResult.current.errors.state.message).toBe("Folder label is required.");
    expect(putExtensionProjectState).not.toHaveBeenCalled();
  });

  it.each([3, "bad"])("saves state with a validated revision (%j) and resets the form", async (revision) => {
    const { result, refetchState } = renderActions({ errors: { state: { message: "Old failure", fieldPath: "" } } });
    await act(async () => result.current.actions.handleProjectStateSave(stateComponent, { folder: "inbox", label: "Inbox", revision }));
    expect(putExtensionProjectState).toHaveBeenCalledWith("project-1", "mail-extension", "settings", "folder:inbox", {
      value_jsonb: { label: "Inbox" }, expected_revision: revision === 3 ? 3 : null,
    });
    expect(refetchState).toHaveBeenCalledTimes(1);
    expect(result.current.formValues.state).toEqual(stateComponent.defaults);
    expect(result.current.errors).toEqual({});
    expect(result.current.statuses).toEqual({ "mail-extension": "Extension state saved." });
  });

  it("keeps a state API failure visible without clearing the edited form", async () => {
    vi.mocked(putExtensionProjectState).mockRejectedValueOnce(new Error("Revision conflict"));
    const { result, refetchState } = renderActions();
    act(() => result.current.actions.handleProjectStateSelect(stateComponent, { state_key: "folder:inbox", value_jsonb: { label: "Inbox" }, revision: 3 }));
    await act(async () => result.current.actions.handleProjectStateSave(stateComponent, result.current.formValues.state));
    expect(result.current.errors.state.message).toBe("Revision conflict");
    expect(result.current.formValues.state.revision).toBe(3);
    expect(refetchState).not.toHaveBeenCalled();
  });

  it("confirms revision-bound state deletion and resets the form only after success", async () => {
    const { result, refetchState } = renderActions();
    let deletion: Promise<void> = Promise.resolve();
    act(() => { deletion = result.current.actions.handleProjectStateDelete(stateComponent, { state_key: "folder:inbox", revision: 3 }); });
    expect(result.current.actions.confirmation?.title).toBe("Delete state folder:inbox?");
    expect(deleteExtensionProjectState).not.toHaveBeenCalled();
    await act(async () => { await result.current.actions.acceptConfirmation(); await deletion; });
    expect(deleteExtensionProjectState).toHaveBeenCalledWith("project-1", "mail-extension", "settings", "folder:inbox", { expectedRevision: 3 });
    expect(refetchState).toHaveBeenCalledTimes(1);
    expect(result.current.formValues.state).toEqual(stateComponent.defaults);
    expect(result.current.actions.confirmation).toBeNull();
  });

  it("does not request deletion for incomplete state targets", async () => {
    const { result } = renderActions();
    await act(async () => {
      await result.current.actions.handleProjectStateDelete(stateComponent, {});
      await result.current.actions.handleProjectStateDelete({ component_id: "state" }, { state_key: "key" });
      await result.current.actions.acceptConfirmation();
    });
    expect(result.current.actions.confirmation).toBeNull();
    expect(deleteExtensionProjectState).not.toHaveBeenCalled();
  });
});
