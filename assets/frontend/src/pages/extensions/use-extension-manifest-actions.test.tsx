import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { fetchJson } from "@/lib/http/client";
import { fetchExtensionCallbacks, type ExtensionCallbackRegistration } from "./api";
import { type ManifestRenderContext } from "./extension-manifest-render-model";
import { renderManifestComponent } from "./extension-manifest-renderer";
import { useExtensionManifestActions } from "./use-extension-manifest-actions";
import type { useExtensionLifecycleActions } from "./use-extension-lifecycle-actions";
import type { useExtensionsHubState } from "./use-extensions-hub-state";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));

const automaticComponent = {
  component_id: "incoming_setup", type: "callback_setup", title: "Incoming updates", callback_id: "updates",
};
const component = { ...automaticComponent, setup_mode: "manual" };
const registration = {
  id: "registration-a", extension_id: "example.connector", callback_id: "updates",
  project_id: "project-1", connection_id: "connection-a", enabled: true,
};
const credentials = {
  ...registration,
  callback_url: "https://callbacks.example.test/webhook/extensions/example.connector/updates/one-time-route",
  callback_secret: "non-authenticating-verify-fixture", // pragma: allowlist secret - credential lifetime test sentinel
};
const redacted = { ...registration, callback_url: "/webhook/extensions/example.connector/updates/<redacted>" };
const callbackPath = "/api/extensions/example.connector/callbacks/updates";

const initialScope = {
  callbackOwnerScopeKey: "user-1:account-1:workspace-1", projectId: "project-1", installId: "install-1",
  connectionId: "connection-a", connectionRequired: true, canManage: true, pageId: "setup",
};
type Scope = typeof initialScope;
type Options = { callbacks?: ExtensionCallbackRegistration[]; component?: Record<string, unknown> };

function useCallbackHarness(scope: Scope, options: Options = {}) {
  const [queryClient] = useState(() => new QueryClient());
  const [statuses, setStatusById] = useState<Record<string, string>>({});
  const [callbackStatuses, setCallbackStatuses] = useState<Record<string, Record<string, unknown>>>({});
  const [callbacks, setCallbacks] = useState(options.callbacks ?? []);
  const pendingManifestActionIntentRef = useRef(null);
  const callbacksQuery = {
    data: callbacks,
    refetch: async () => {
      const data = await fetchExtensionCallbacks("example.connector", scope.projectId);
      setCallbacks(data);
      return { data };
    },
  };
  const callbackDeclarations = [{ callback_id: "updates", connection_scoped: scope.connectionRequired }];
  const hubState = {
    callbackOwnerScopeKey: scope.callbackOwnerScopeKey,
    detailExtension: { extension_id: "example.connector" }, extensionInstallId: scope.installId,
    activePageProjectId: scope.projectId, selectedProjectId: scope.projectId,
    canManageProject: scope.canManage, queryClient, callbacksQuery, setCallbackStatuses, setStatusById,
    statusById: statuses, componentErrors: {}, actionResultByExtension: {},
    pendingManifestActionIntentRef, externalEffectsQuery: { data: { effects: [] } },
    activeManifestPage: {
      page_id: scope.pageId,
      connection: { connection_type_ids: ["service_profile"] },
    },
    pageConnection: { selectedId: scope.connectionId, types: ["service_profile"] },
    manifestQuery: { data: { callbacks: callbackDeclarations } },
    location: { search: "" },
  } as unknown as ReturnType<typeof useExtensionsHubState>;
  const actions = useExtensionManifestActions(hubState, {
    withBusy: async (_key: string, run: () => Promise<void>) => run(),
  } as unknown as ReturnType<typeof useExtensionLifecycleActions>);
  const context = {
    ...actions,
    busy: false, canManageProject: scope.canManage, connectionCreationBlockedReason: "",
    callbacks, callbackStatuses, componentErrors: {}, componentQueryParams: {}, componentQueryData: {},
    extensionId: "example.connector", extensionInstallId: scope.installId,
    extensionReleaseId: "release-1", extensionDescriptorSha256: "a".repeat(64),
    formValuesByComponent: {}, oauthConnectionTypes: [], projectConnections: [], projectState: [],
    projectId: scope.projectId, selectedConnectionId: scope.connectionId,
    callbackDeclarations,
    onCallbackSetup: (source: Record<string, unknown>, action: "configure" | "status") => {
      void actions.handleCallbackSetup(source, action);
    },
  } as unknown as ManifestRenderContext;
  return { actions, context, statuses, callbackStatuses, queryClient };
}

function CallbackHarness({ scope, options = {} }: { scope: Scope; options?: Options }) {
  const state = useCallbackHarness(scope, options);
  return scope.pageId === "setup"
    ? <>{renderManifestComponent(options.component ?? component, state.context)}</>
    : <p>Other extension page</p>;
}

function renderActions(overrides: Partial<Scope> = {}, options: Options = {}) {
  return renderHook(({ scope }) => useCallbackHarness(scope, options), {
    initialProps: { scope: { ...initialScope, ...overrides } },
  });
}

beforeEach(() => {
  vi.mocked(fetchJson).mockReset();
  vi.mocked(fetchJson).mockImplementation(async (_input, init) => (
    init?.method === "POST" ? credentials : { callbacks: [redacted] }
  ));
});

afterEach(() => vi.restoreAllMocks());

it("registers a manual callback for the page's selected connection without provider configuration", async () => {
  const { result } = renderActions();
  await act(async () => result.current.actions.handleCallbackSetup(component, "configure"));
  expect(fetchJson).toHaveBeenCalledWith(callbackPath, {
    method: "POST",
    body: expect.any(String),
  });
  const request = vi.mocked(fetchJson).mock.calls.find(([path]) => path === callbackPath)!;
  expect(JSON.parse(String(request[1]?.body))).toMatchObject({
    project_id: "project-1", connection_id: "connection-a", enabled: true, configure: false,
  });
});

it("keeps automatic global callbacks unscoped on a page with a selected connection", async () => {
  const { result } = renderActions({ connectionRequired: false });
  await act(async () => result.current.actions.handleCallbackSetup({ callback_id: "updates", configure_action_id: "install_callback" }, "configure"));
  const request = vi.mocked(fetchJson).mock.calls.find(([path]) => path === callbackPath)!;
  expect(JSON.parse(String(request[1]?.body))).toEqual({
    project_id: "project-1", enabled: true, configure: true, configure_action_id: "install_callback",
  });
});

it("refreshes automatic provider status for the selected connection", async () => {
  vi.mocked(fetchJson).mockResolvedValue({ webhook_status: { url_configured: true } });
  const { result } = renderActions({}, { callbacks: [
    { ...registration, id: "registration-other", connection_id: "connection-b" }, registration,
  ] });
  await act(async () => result.current.actions.handleCallbackSetup(automaticComponent, "status"));
  expect(fetchJson).toHaveBeenCalledWith("/api/extensions/example.connector/callbacks/registration-a/status", expect.objectContaining({ method: "POST" }));
  expect(result.current.callbackStatuses["registration-a"]).toEqual({ url_configured: true });
  expect(result.current.callbackStatuses["registration-other"]).toBeUndefined();
});

it("refreshes automatic global provider status even when the page has another selected connection", async () => {
  const { result } = renderActions({ connectionRequired: false }, { callbacks: [
    registration, { ...registration, id: "registration-global", connection_id: null },
  ] });
  await act(async () => result.current.actions.handleCallbackSetup(automaticComponent, "status"));
  expect(fetchJson).toHaveBeenCalledWith("/api/extensions/example.connector/callbacks/registration-global/status", expect.objectContaining({ method: "POST" }));
});

it("refreshes manual host registration details without invoking a provider status action", async () => {
  const user = userEvent.setup();
  vi.mocked(fetchJson).mockResolvedValue({ callbacks: [
    { ...redacted, id: "registration-other", connection_id: "connection-b", last_seen_at: "2026-11-14T17:00:00Z", last_error: "Other connection intake failure" },
    { ...redacted, last_seen_at: "2026-11-14T17:30:00Z", last_error: "Selected connection intake failure" },
  ] });
  render(<CallbackHarness scope={initialScope} options={{ callbacks: [redacted] }} />);
  await user.click(screen.getByRole("button", { name: "Refresh Status" }));
  await waitFor(() => expect(screen.getByText("Selected connection intake failure")).toBeVisible());
  expect(screen.getByText("2026-11-14T17:30:00Z")).toBeVisible();
  expect(screen.queryByText("Other connection intake failure")).not.toBeInTheDocument();
  expect(screen.queryByText("2026-11-14T17:00:00Z")).not.toBeInTheDocument();
  expect(screen.queryByText("configured", { exact: true })).not.toBeInTheDocument();
  expect(fetchJson).toHaveBeenCalledExactlyOnceWith(
    "/api/extensions/example.connector/callbacks?project_id=project-1", { cache: "no-store" },
  );
});

it("renders the global callback status without borrowing the selected connection's status", () => {
  render(<CallbackHarness scope={{ ...initialScope, connectionRequired: false }} options={{ callbacks: [
    { ...registration, last_error: "Connection delivery failure" },
    { ...registration, id: "registration-global", connection_id: null, last_error: "Global delivery failure" },
  ] }} />);
  expect(screen.getByText("Global delivery failure")).toBeVisible();
  expect(screen.queryByText("Connection delivery failure")).not.toBeInTheDocument();
});

it("renders registration status only for the selected connection", () => {
  render(<CallbackHarness scope={initialScope} options={{ callbacks: [
    { ...registration, connection_id: "connection-b", last_error: "Other account delivery failure" },
  ] }} />);
  expect(screen.getByText("not registered", { exact: true })).toBeVisible();
  expect(screen.queryByText("Other account delivery failure")).not.toBeInTheDocument();
});

it.each(["configure", "status"] as const)("does not %s a connection-scoped callback without a selected connection", async (action) => {
  const { result } = renderActions({ connectionId: "" }, { callbacks: [registration] });
  await act(async () => result.current.actions.handleCallbackSetup(component, action));
  expect(fetchJson).not.toHaveBeenCalled();
});

it("offers manual callback registration without implying provider configuration", () => {
  render(<CallbackHarness scope={initialScope} />);
  expect(screen.getByRole("button", { name: "Create Callback" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Create and Configure" })).not.toBeInTheDocument();
});

it("disables manual registration until a connection is selected", () => {
  render(<CallbackHarness scope={{ ...initialScope, connectionId: "" }} />);
  expect(screen.getByRole("button", { name: /create/i })).toBeDisabled();
  expect(screen.queryByRole("button", { name: /copy/i })).not.toBeInTheDocument();
});

it("lets the manager copy one-time URL and verify token after a redacted list refresh", async () => {
  const user = userEvent.setup();
  const clipboard = vi.spyOn(navigator.clipboard, "writeText");
  const localWrite = vi.spyOn(localStorage, "setItem");
  const sessionWrite = vi.spyOn(sessionStorage, "setItem");
  render(<CallbackHarness scope={initialScope} />);
  await user.click(screen.getByRole("button", { name: /create/i }));
  await user.click(await screen.findByRole("button", { name: /copy callback url/i }));
  expect(clipboard).toHaveBeenLastCalledWith(credentials.callback_url);
  await user.click(screen.getByRole("button", { name: /copy verify token/i }));
  expect(clipboard).toHaveBeenLastCalledWith(credentials.callback_secret);
  const persisted = JSON.stringify([...localWrite.mock.calls, ...sessionWrite.mock.calls]);
  expect(persisted).not.toContain(credentials.callback_url);
  expect(persisted).not.toContain(credentials.callback_secret);
});

it("keeps callback setup read-only for a viewer even if its handler is invoked", async () => {
  const { result } = renderActions({ canManage: false });
  await act(async () => result.current.actions.handleCallbackSetup(component, "configure"));
  expect(fetchJson).not.toHaveBeenCalled();
});

it.each([
  ["user", { callbackOwnerScopeKey: "user-2:account-1:workspace-1" }],
  ["account", { callbackOwnerScopeKey: "user-1:account-2:workspace-1" }],
  ["workspace", { callbackOwnerScopeKey: "user-1:account-1:workspace-2" }],
  ["page", { pageId: "overview" }],
  ["project", { projectId: "project-2" }],
  ["installation", { installId: "install-2" }],
  ["connection", { connectionId: "connection-b" }],
  ["manager permission", { canManage: false }],
] as const)("hides one-time credentials after a %s switch", async (_label, change) => {
  const user = userEvent.setup();
  const { rerender } = render(<CallbackHarness scope={initialScope} />);
  await user.click(screen.getByRole("button", { name: /create/i }));
  await waitFor(() => expect(screen.getByRole("button", { name: /copy verify token/i })).toBeVisible());
  rerender(<CallbackHarness scope={{ ...initialScope, ...change }} />);
  expect(screen.queryByRole("button", { name: /copy/i })).not.toBeInTheDocument();
  expect(screen.queryByText(credentials.callback_secret)).not.toBeInTheDocument();
  expect(screen.queryByText(credentials.callback_url)).not.toBeInTheDocument();
});

it.each([
  ["user", { callbackOwnerScopeKey: "user-2:account-1:workspace-1" }],
  ["account", { callbackOwnerScopeKey: "user-1:account-2:workspace-1" }],
  ["workspace", { callbackOwnerScopeKey: "user-1:account-1:workspace-2" }],
  ["page", { pageId: "overview" }],
  ["project", { projectId: "project-2" }],
  ["installation", { installId: "install-2" }],
  ["connection", { connectionId: "connection-b" }],
] as const)("ignores a late registration response after a %s switch", async (_label, change) => {
  let finish!: (value: ExtensionCallbackRegistration) => void;
  vi.mocked(fetchJson).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const { result, rerender } = renderActions();
  let request!: Promise<void>;
  act(() => { request = result.current.actions.handleCallbackSetup(component, "configure"); });
  rerender({ scope: { ...initialScope, ...change } });
  await act(async () => { finish(credentials); await request; });
  expect(result.current.callbackStatuses).toEqual({});
  expect(result.current.statuses["example.connector"]).not.toMatch(/registered|configured/i);
  expect(fetchJson).toHaveBeenCalledTimes(1);
  const cached = JSON.stringify(result.current.queryClient.getQueryCache().getAll().map((query) => query.state.data));
  expect(cached).not.toContain(credentials.callback_url);
  expect(cached).not.toContain(credentials.callback_secret);
});

it("does not recreate one-time copy controls after remounting from a redacted registration", async () => {
  const user = userEvent.setup();
  const view = render(<CallbackHarness scope={initialScope} />);
  await user.click(screen.getByRole("button", { name: /create/i }));
  await waitFor(() => expect(screen.getByRole("button", { name: /copy verify token/i })).toBeVisible());
  view.unmount();
  render(<CallbackHarness scope={initialScope} options={{ callbacks: [redacted] }} />);
  await waitFor(() => expect(screen.getByText("registered", { exact: true })).toBeVisible());
  expect(screen.queryByRole("button", { name: /copy/i })).not.toBeInTheDocument();
});


it("does not resurrect one-time credentials after leaving the page while the hub hook stays mounted", async () => {
  const user = userEvent.setup();
  const { rerender } = render(<CallbackHarness scope={initialScope} />);
  await user.click(screen.getByRole("button", { name: /create/i }));
  await waitFor(() => expect(screen.getByRole("button", { name: /copy verify token/i })).toBeVisible());
  rerender(<CallbackHarness scope={{ ...initialScope, pageId: "overview" }} />);
  expect(screen.getByText("Other extension page")).toBeVisible();
  rerender(<CallbackHarness scope={initialScope} />);
  expect(screen.queryByRole("button", { name: /copy/i })).not.toBeInTheDocument();
  expect(screen.queryByText(credentials.callback_url)).not.toBeInTheDocument();
  expect(screen.queryByText(credentials.callback_secret)).not.toBeInTheDocument();
});

it("discards a late create receipt after leaving and returning to the same setup page", async () => {
  let finish!: (value: ExtensionCallbackRegistration) => void;
  vi.mocked(fetchJson).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const { result, rerender } = renderActions();
  let request!: Promise<void>;
  act(() => { request = result.current.actions.handleCallbackSetup(component, "configure"); });
  rerender({ scope: { ...initialScope, pageId: "overview" } });
  rerender({ scope: initialScope });
  await act(async () => { finish(credentials); await request; });
  expect(result.current.callbackStatuses).toEqual({});
  expect(result.current.statuses["example.connector"]).not.toMatch(/registered|configured/i);
  expect(fetchJson).toHaveBeenCalledTimes(1);
});
