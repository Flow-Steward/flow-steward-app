import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { ProjectNotificationRoutesEditor } from "@/pages/projects/notifications";
import type * as NotificationsApi from "@/pages/projects/notifications/api";
import {
  fetchProjectNotificationProviders,
  fetchProjectNotificationRoutes,
  fetchProjectProviderChannels,
  saveProjectNotificationRoutes,
} from "@/pages/projects/notifications/api";

vi.mock("@/pages/projects/notifications/api", async (importOriginal) => ({
  ...await importOriginal<typeof NotificationsApi>(),
  fetchProjectNotificationProviders: vi.fn(),
  fetchProjectNotificationRoutes: vi.fn(),
  fetchProjectProviderChannels: vi.fn(),
  saveProjectNotificationRoutes: vi.fn(),
}));

const providers = vi.mocked(fetchProjectNotificationProviders);
const routes = vi.mocked(fetchProjectNotificationRoutes);
const channels = vi.mocked(fetchProjectProviderChannels);
const save = vi.mocked(saveProjectNotificationRoutes);

function renderEditor() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const content = (projectId: string) => <MemoryRouter><QueryClientProvider client={client}>
    <ProjectNotificationRoutesEditor projectId={projectId} />
  </QueryClientProvider></MemoryRouter>;
  const view = render(content("project-1"));
  return { client, changeProject: (projectId: string) => view.rerender(content(projectId)) };
}

function channelProvider() {
  providers.mockResolvedValue([{
    provider_key: "fixture.notifications", display_name: "Fixture notifications",
    provider_type_key: "notification_provider", enabled: true, provider_enabled: true,
    features: ["notify"], notification_target_kind: "channel",
  }]);
  channels.mockResolvedValue([{
    channel_id: "ops", display_name: "Operations", chat_id: "owned-target",
    enabled: true, capabilities: { notify: true, human_decision: false },
  }]);
}

async function choose(user: ReturnType<typeof userEvent.setup>, field: string, label: string) {
  await user.click(screen.getByRole("combobox", { name: field }));
  await user.click(await screen.findByRole("option", { name: label }));
}

async function fillTarget(user: ReturnType<typeof userEvent.setup>) {
  await choose(user, "Provider", "Fixture notifications");
  await choose(user, "Registered channel", "Operations (ops)");
}

describe("notification route authoring", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    providers.mockResolvedValue([]);
    routes.mockResolvedValue({ project_id: "project-1", notification_routes: [] });
    channels.mockResolvedValue([]);
    save.mockResolvedValue({ project_id: "project-1", notification_routes: [] });
    // Keep the workflow catalog adapter real; only substitute the network.
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      if (url !== "/api/projects/project-1/workflow-catalog?enabled_only=false") {
        throw new Error(`Unexpected fixture request: ${String(url)}`);
      }
      return new Response(JSON.stringify({
      project_id: "project-1", workflows: [
        { workflow_id: "wf-a", display_name: "Alpha workflow", enabled: true },
        { workflow_id: "wf-b", display_name: "Beta workflow", enabled: false },
      ],
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("explains missing providers and prevents creating an invalid route", async () => {
    const user = userEvent.setup();
    renderEditor();
    await screen.findByText(/no enabled notification provider/i);
    expect(screen.getByRole("link", { name: /runtime connections/i })).toHaveAttribute("href", "/projects/project-1?tab=connections");
    expect(screen.getByRole("link", { name: /^extensions$/i })).toHaveAttribute("href", "/extensions");
    const create = screen.getByRole("button", { name: "Create new" });
    expect(create).toBeDisabled();
    await user.click(create);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(channels).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
  });

  it("does not reuse a survivor's key when the route count has shrunk", async () => {
    channelProvider();
    routes.mockResolvedValue({ project_id: "project-1", notification_routes: [{
      route_id: "survivor", route_key: "route_2", display_name: "Survivor",
      provider_key: "fixture.notifications", event_type: "job.failed", severity: "any",
      enabled: false, workflow_id: null, route_config: { channel_id: "ops" },
    }] });
    const user = userEvent.setup();
    renderEditor();
    await screen.findByText("Survivor");
    await user.click(screen.getByRole("button", { name: "Create new" }));
    expect(screen.getByRole("textbox", { name: /route key/i })).not.toHaveValue("route_2");
    expect(screen.getByRole("textbox", { name: /route key/i })).toHaveAttribute("readonly");
    await fillTarget(user);
    await user.click(screen.getByRole("button", { name: "Create route" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    const saved = save.mock.calls[0][1];
    expect(saved).toHaveLength(2);
    expect(saved[0]).toMatchObject({ route_key: "route_2", enabled: false, event_type: "job.failed" });
    expect(new Set(saved.map((row) => row.route_key)).size).toBe(2);
  });

  it("selects an event and named scoped workflow without typing technical IDs", async () => {
    channelProvider();
    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByRole("button", { name: "Create new" }));
    await choose(user, "Event type", "Job failed");
    await choose(user, "Workflow", "Beta workflow");
    await fillTarget(user);
    await user.click(screen.getByRole("button", { name: "Create route" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("project-1", [expect.objectContaining({
      event_type: "job.failed", workflow_id: "wf-b", route_config: { channel_id: "ops" },
    })]));
    expect(globalThis.fetch).toHaveBeenCalledWith("/api/projects/project-1/workflow-catalog?enabled_only=false", expect.anything());
  });

  it("preserves a stored custom event and unavailable workflow on unrelated edits", async () => {
    channelProvider();
    routes.mockResolvedValue({ project_id: "project-1", notification_routes: [{
      route_id: "custom", route_key: "stable-custom", display_name: "Custom route",
      provider_key: "fixture.notifications", event_type: "fixture.deployment.finished",
      enabled: false, severity: "any", workflow_id: "old-unlisted-workflow", route_config: { channel_id: "ops" },
    }] });
    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    expect(screen.getByRole("combobox", { name: "Event type" })).toHaveTextContent("fixture.deployment.finished");
    expect(screen.getByRole("combobox", { name: "Workflow" })).toHaveTextContent("old-unlisted-workflow");
    await user.type(screen.getByRole("textbox", { name: "Display name" }), " updated");
    await user.click(screen.getByRole("button", { name: "Save route" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("project-1", [expect.objectContaining({
      route_key: "stable-custom", enabled: false, event_type: "fixture.deployment.finished",
      workflow_id: "old-unlisted-workflow", route_config: { channel_id: "ops" },
    })]));
  });

  it("preserves generated identity through rejected save, retry and reopened edit", async () => {
    channelProvider();
    let stored: NotificationsApi.ProjectNotificationRoutesResponse["notification_routes"] = [];
    routes.mockImplementation(async () => ({ project_id: "project-1", notification_routes: stored }));
    save.mockRejectedValueOnce(new Error("Owned retry refusal"))
      .mockImplementation(async (_projectId, payload) => {
        stored = payload.map((row, index) => ({ ...row, route_id: `owned-${index}` }));
        return { project_id: "project-1", notification_routes: stored };
      });
    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByRole("button", { name: "Create new" }));
    const key = (screen.getByRole("textbox", { name: /route key/i }) as HTMLInputElement).value;
    expect(key.trim()).not.toBe("");
    await user.type(screen.getByRole("textbox", { name: "Display name" }), "Owned retry route");
    await fillTarget(user);
    await user.click(screen.getByRole("button", { name: "Create route" }));
    expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent("Owned retry refusal");
    expect(screen.getByRole("textbox", { name: /route key/i })).toHaveValue(key);
    await user.click(screen.getByRole("button", { name: "Create route" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    expect(screen.getByRole("textbox", { name: /route key/i })).toHaveValue(key);
    await user.type(screen.getByRole("textbox", { name: "Display name" }), " renamed");
    await user.click(screen.getByRole("button", { name: "Save route" }));
    await waitFor(() => expect(save).toHaveBeenCalledTimes(3));
    for (const [projectId, payload] of save.mock.calls) {
      expect(projectId).toBe("project-1");
      expect(payload).toHaveLength(1);
      expect(payload[0]).toMatchObject({ route_key: key, provider_key: "fixture.notifications", route_config: { channel_id: "ops" } });
    }
  });

  it("never offers Create while provider inventory is pending or failed", async () => {
    let rejectProviders!: (reason: Error) => void;
    providers.mockReturnValue(new Promise((_resolve, reject) => { rejectProviders = reject; }));
    const user = userEvent.setup();
    renderEditor();
    expect(screen.getByRole("button", { name: "Create new" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Create new" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => { rejectProviders(new Error("Owned inventory failure")); });
    await screen.findByText("Notification provider inventory is unavailable.");
    expect(screen.getByRole("button", { name: "Create new" })).toBeDisabled();
    expect(channels).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
  });

  it("explicit All choices save null workflow and wildcard event", async () => {
    channelProvider();
    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByRole("button", { name: "Create new" }));
    await choose(user, "Workflow", "Beta workflow");
    await choose(user, "Workflow", "All workflows");
    await choose(user, "Event type", "All events");
    await fillTarget(user);
    await user.click(screen.getByRole("button", { name: "Create route" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("project-1", [expect.objectContaining({ workflow_id: null, event_type: "*" })]));
  });

  it("closes a draft on real project context change and saves only the new context", async () => {
    channelProvider();
    providers.mockImplementation(async (projectId) => [{
      provider_key: `fixture.${projectId}`, display_name: `Provider ${projectId}`,
      provider_type_key: "notification_provider", enabled: true, provider_enabled: true,
      features: ["notify"], notification_target_kind: "channel",
    }]);
    routes.mockImplementation(async (projectId) => ({ project_id: projectId, notification_routes: [] }));
    vi.mocked(globalThis.fetch).mockImplementation(async (url) => {
      if (url !== "/api/projects/project-1/workflow-catalog?enabled_only=false" && url !== "/api/projects/project-2/workflow-catalog?enabled_only=false") throw new Error(`Unexpected fixture request: ${String(url)}`);
      return new Response(JSON.stringify({ workflows: [] }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const user = userEvent.setup();
    const view = renderEditor();
    await user.click(await screen.findByRole("button", { name: "Create new" }));
    await user.type(screen.getByRole("textbox", { name: "Display name" }), "Old project draft");
    view.changeProject("project-2");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Create new" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Create new" }));
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("");
    await choose(user, "Provider", "Provider project-2");
    await choose(user, "Registered channel", "Operations (ops)");
    await user.click(screen.getByRole("button", { name: "Create route" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    expect(save).toHaveBeenCalledWith("project-2", [expect.objectContaining({ provider_key: "fixture.project-2", route_config: { channel_id: "ops" } })]);
  });

  it.each([null, {}, { workflows: null }, { workflows: [{ display_name: "Missing identity" }] }])("refuses malformed workflow catalog %j rather than enabling Create", async (payload) => {
    channelProvider();
    vi.mocked(globalThis.fetch).mockImplementation(async () => new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } }));
    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByRole("button", { name: "Create new" }));
    await screen.findByText(/project workflow inventory is unavailable/i);
    expect(screen.getByRole("combobox", { name: "Workflow" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Create route" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Create route" }));
    expect(save).not.toHaveBeenCalled();
  });

  it("retains a stored workflow selection when catalog loading fails", async () => {
    channelProvider();
    routes.mockResolvedValue({ project_id: "project-1", notification_routes: [{
      route_id: "preserved", route_key: "stored-key", display_name: "Stored selection",
      provider_key: "fixture.notifications", event_type: "fixture.custom.event", workflow_id: "old-workflow",
      enabled: false, severity: "any", route_config: { channel_id: "ops" },
    }] });
    vi.mocked(globalThis.fetch).mockRejectedValue(new Error("Owned catalog failure"));
    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByRole("button", { name: /^edit$/i }));
    await screen.findByText(/project workflow inventory is unavailable/i);
    expect(screen.getByRole("combobox", { name: "Workflow" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Workflow" })).toHaveTextContent("old-workflow");
    await user.type(screen.getByRole("textbox", { name: "Display name" }), " renamed");
    await user.click(screen.getByRole("button", { name: "Save route" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("project-1", [expect.objectContaining({
      route_key: "stored-key", event_type: "fixture.custom.event", workflow_id: "old-workflow",
      enabled: false, route_config: { channel_id: "ops" },
    })]));
  });

  it("blocks new creation while catalog is pending and offers a retry after error", async () => {
    channelProvider();
    let rejectCatalog!: (reason: Error) => void;
    vi.mocked(globalThis.fetch).mockReturnValueOnce(new Promise((_resolve, reject) => { rejectCatalog = reject; }));
    const user = userEvent.setup();
    renderEditor();
    await user.click(await screen.findByRole("button", { name: "Create new" }));
    await screen.findByText("Loading project workflows...");
    expect(screen.getByRole("button", { name: "Create route" })).toBeDisabled();
    await act(async () => { rejectCatalog(new Error("Owned catalog failure")); });
    await screen.findByText(/project workflow inventory is unavailable/i);
    expect(screen.getByRole("button", { name: "Create route" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Retry project workflows" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Workflow" })).toBeEnabled());
    await fillTarget(user);
    await user.click(screen.getByRole("button", { name: "Create route" }));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
  });

  it("blocks an open new route after provider refresh fails without losing its draft", async () => {
    channelProvider();
    const user = userEvent.setup();
    const { client } = renderEditor();
    await user.click(await screen.findByRole("button", { name: "Create new" }));
    await user.type(screen.getByRole("textbox", { name: "Display name" }), "Keep after refresh failure");
    await fillTarget(user);
    const key = (screen.getByRole("textbox", { name: /route key/i }) as HTMLInputElement).value;
    providers.mockRejectedValueOnce(new Error("Owned provider refresh failure"));
    await act(async () => { await client.invalidateQueries({ queryKey: ["projects", "notification-providers", "project-1"] }); });
    await screen.findByText("Notification provider inventory is unavailable.");
    expect(screen.getByRole("button", { name: "Create route" })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("Keep after refresh failure");
    expect(screen.getByRole("textbox", { name: /route key/i })).toHaveValue(key);
    await user.click(screen.getByRole("button", { name: "Create route" }));
    expect(save).not.toHaveBeenCalled();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Retry notification data" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Create route" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Create route" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("project-1", [expect.objectContaining({ route_key: key })]));
  });

  it("ignores a late old-project catalog on the same mounted editor", async () => {
    channelProvider();
    let resolveOld!: (response: Response) => void;
    vi.mocked(globalThis.fetch).mockImplementation(async (url) => {
      if (url === "/api/projects/project-1/workflow-catalog?enabled_only=false") return new Promise((resolve) => { resolveOld = resolve; });
      if (url !== "/api/projects/project-2/workflow-catalog?enabled_only=false") throw new Error(`Unexpected fixture request: ${String(url)}`);
      return new Response(JSON.stringify({ workflows: [{ workflow_id: "wf-new", display_name: "New project workflow" }] }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const user = userEvent.setup();
    const view = renderEditor();
    await user.click(await screen.findByRole("button", { name: "Create new" }));
    await screen.findByText("Loading project workflows...");
    view.changeProject("project-2");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Create new" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Create new" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Workflow" })).toBeEnabled());
    await act(async () => { resolveOld(new Response(JSON.stringify({ workflows: [{ workflow_id: "wf-old", display_name: "Old project workflow" }] }), { status: 200, headers: { "Content-Type": "application/json" } })); });
    await user.click(screen.getByRole("combobox", { name: "Workflow" }));
    expect(await screen.findByRole("option", { name: "New project workflow" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Old project workflow" })).not.toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
  });

  it.each(["routes", "providers", "workflows"])("blocks new Save during a late %s refresh and retains the draft on failure", async (inventory) => {
    channelProvider();
    const user = userEvent.setup();
    const { client } = renderEditor();
    await user.click(await screen.findByRole("button", { name: "Create new" }));
    await fillTarget(user);
    await user.type(screen.getByRole("textbox", { name: "Display name" }), "Retained refresh draft");
    const key = (screen.getByRole("textbox", { name: /route key/i }) as HTMLInputElement).value;
    let rejectRead!: (error: Error) => void;
    const pendingRead = new Promise<never>((_resolve, reject) => { rejectRead = reject; });
    if (inventory === "routes") routes.mockReturnValueOnce(pendingRead);
    if (inventory === "providers") providers.mockReturnValueOnce(pendingRead);
    if (inventory === "workflows") vi.mocked(globalThis.fetch).mockReturnValueOnce(pendingRead);
    let refresh = Promise.resolve();
    await act(async () => { refresh = client.invalidateQueries({ queryKey: ["projects", `notification-${inventory}`, "project-1"] }); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Create route" })).toBeDisabled());
    await act(async () => { rejectRead(new Error("Owned inventory refresh failure")); await refresh; });
    await screen.findByText(inventory === "workflows"
      ? /project workflow inventory is unavailable/i
      : inventory === "providers" ? "Notification provider inventory is unavailable." : "Notification route inventory is unavailable.");
    await waitFor(() => expect(screen.getByRole("button", { name: "Create route" })).toBeDisabled());
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("Retained refresh draft");
    expect(screen.getByRole("textbox", { name: /route key/i })).toHaveValue(key);
    expect(save).not.toHaveBeenCalled();
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: inventory === "workflows" ? "Retry project workflows" : "Retry notification data" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Create route" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Create route" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("project-1", [expect.objectContaining({ route_key: key })]));
  });
});
