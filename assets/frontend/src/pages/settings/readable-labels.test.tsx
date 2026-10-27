import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";

import { SettingsGeneralPage } from "@/pages/settings/general";
import { SettingsNotificationsPage } from "@/pages/settings/notifications";

const internalId = "e33de3e5-9bd0-4d5b-a1ea-2fc6cb9325b1";

function renderPage(page: "general" | "notifications") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{page === "general" ? <SettingsGeneralPage /> : <SettingsNotificationsPage />}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function installHttp(
  providerName = "Production model provider",
  projectName = "Production project",
  state: "ready" | "loading" | "unavailable" = "ready",
) {
  const requests: Array<{ path: string; method: string }> = [];
  const providers = [{ id: internalId, name: providerName, provider_type: "openai" }];
  const projects = [{ id: internalId, name: projectName }];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    requests.push({ path: url.pathname, method: init?.method || "GET" });
    if (url.pathname === "/api/model-providers" || url.pathname === "/api/v2/projects") {
      if (state === "loading") return new Promise<Response>(() => undefined);
      if (state === "unavailable") return response({ detail: "Catalog unavailable" }, 503);
    }
    if (url.pathname === "/api/app/bootstrap") return response({ auth: { local_auth_enabled: true } });
    if (url.pathname === "/api/auth/me") return response({ auth_source: "local" });
    if (url.pathname === "/api/model-providers") return response({ providers });
    if (url.pathname === "/api/v2/projects") {
      return response({ projects, page: 1, per_page: 200, total: 1, total_pages: 1 });
    }
    if (url.pathname === `/api/projects/${internalId}/notification-routes`) {
      return response({ project_id: internalId, notification_routes: [] });
    }
    if (url.pathname === `/api/projects/${internalId}/providers`) return response({ project_providers: [] });
    if (url.pathname === `/api/projects/${internalId}/workflow-catalog`) return response({ workflows: [] });
    throw new Error(`Unexpected HTTP ${init?.method || "GET"} ${url.pathname}`);
  });
  return { requests, providers, projects };
}

describe("Settings readable labels with internal IDs unchanged", () => {
  beforeEach(() => { globalThis.localStorage.clear(); });
  afterEach(() => { vi.restoreAllMocks(); });

  it.each([
    ["Production model provider", "Production model provider"],
    ["   ", "Unnamed provider"],
  ])("uses backend provider name or a readable unnamed state (%s)", async (name, label) => {
    const http = installHttp(name);
    renderPage("general");
    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(new RegExp(internalId));
    expect(http.providers[0]?.id).toBe(internalId);
    expect(http.requests.every(({ method }) => method === "GET")).toBe(true);
    expect(screen.getByRole("link", { name: "Model providers" })).toHaveAttribute("href", "/settings/providers");
  });

  it("distinguishes loading provider inventory from an empty inventory", async () => {
    installHttp(undefined, undefined, "loading");
    renderPage("general");
    expect(await screen.findByText("Loading model providers...")).toBeInTheDocument();
    expect(screen.queryByText("No model providers configured yet.")).not.toBeInTheDocument();
  });

  it("distinguishes unavailable provider inventory from an empty inventory", async () => {
    installHttp(undefined, undefined, "unavailable");
    renderPage("general");
    expect(await screen.findByText("Model providers unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("No model providers configured yet.")).not.toBeInTheDocument();
  });

  it.each([
    ["Production project", "Production project"],
    ["   ", "Unnamed project"],
  ])("keeps project UUID in scoped HTTP requests, not in the option label (%s)", async (name, label) => {
    const http = installHttp(undefined, name);
    renderPage("notifications");
    const scope = await screen.findByRole("combobox", { name: "Project scope" });
    await waitFor(() => expect(scope).toBeEnabled());
    expect(http.requests.some(({ path }) => path.startsWith(`/api/projects/${internalId}/`))).toBe(false);
    await userEvent.click(scope);
    const option = await screen.findByRole("option", { name: label });
    expect(document.body).not.toHaveTextContent(new RegExp(internalId));
    await userEvent.click(option);
    expect(scope).toHaveTextContent(label);
    // The actual routing editor and its existing API functions run, not a
    // mock child exposing the ID as visible text. Only HTTP I/O is doubled.
    await waitFor(() => expect(http.requests).toEqual(expect.arrayContaining([
      { path: `/api/projects/${internalId}/notification-routes`, method: "GET" },
      { path: `/api/projects/${internalId}/providers`, method: "GET" },
    ])));
    expect(http.projects[0]?.id).toBe(internalId);
    expect(document.body).not.toHaveTextContent(new RegExp(internalId));
    expect(http.requests.every(({ method }) => method === "GET")).toBe(true);
  });

  it("shows loading scope without inventing a project selection", async () => {
    const http = installHttp(undefined, undefined, "loading");
    renderPage("notifications");
    const scope = await screen.findByRole("combobox", { name: "Project scope" });
    expect(scope).toBeDisabled();
    expect(scope).toHaveTextContent("Loading projects...");
    expect(http.requests.some(({ path }) => path.startsWith("/api/projects/"))).toBe(false);
  });

  it("shows unavailable scope without leaking IDs or opening an editor", async () => {
    const http = installHttp(undefined, undefined, "unavailable");
    renderPage("notifications");
    expect(await screen.findByText("Projects could not be loaded")).toBeInTheDocument();
    const scope = screen.getByRole("combobox", { name: "Project scope" });
    expect(scope).toBeDisabled();
    expect(scope).toHaveTextContent("Projects unavailable");
    expect(http.requests.some(({ path }) => path.startsWith("/api/projects/"))).toBe(false);
  });
});
