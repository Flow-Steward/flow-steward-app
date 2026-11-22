import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AgentExpiryBanner } from "./agent-expiry-banner";
import type { AgentConnection } from "@/pages/settings/shared/types";

function connection(overrides: Partial<AgentConnection> = {}): AgentConnection {
  return {
    id: "tok-1",
    kind: "personal_token",
    client_id: "claude_code",
    client_label: "Desk Agent",
    created_at: "2026-08-27T12:00:00Z",
    ends_at: "2026-11-25T12:00:00Z",
    last_used_at: null,
    days_left: 5,
    ending_soon: true,
    renew: "connect_again",
    ...overrides,
  };
}

function view() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}><p>Dashboard</p><AgentExpiryBanner /></QueryClientProvider>;
}

function serve(connections: AgentConnection[]) {
  const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => Response.json({ connections, reminder_days: 7 }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

beforeEach(() => { globalThis.localStorage.clear(); });
afterEach(() => { vi.unstubAllGlobals(); });

it("tells the owner which agent connection stops working and links to the AI agents page", async () => {
  const fetch = serve([connection(), connection({ id: "tok-2", client_label: "Cursor", ending_soon: false, days_left: 60 })]);
  render(view());
  await waitFor(() => expect(screen.getByText(/^Your AI agent connection Desk Agent stops working on /)).toBeVisible());
  expect(screen.getByRole("link", { name: "Open AI agents" })).toHaveAttribute("href", "/settings/account?tab=ai-agents");
  expect(String(fetch.mock.calls[0][0])).toBe("/api/auth/agent-access/connections");
});

it("counts several connections ending soon in one line", async () => {
  serve([connection(), connection({ id: "grant-1", kind: "oauth_grant", client_label: "ChatGPT", renew: "reconnect_from_client" })]);
  render(view());
  await waitFor(() => expect(screen.getByText("2 of your AI agent connections stop working soon.")).toBeVisible());
});

it("stays quiet when nothing ends soon or the check fails", async () => {
  const fetch = serve([connection({ ending_soon: false })]);
  render(view());
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(screen.queryByTestId("agent-expiry-notice")).not.toBeInTheDocument();
  vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Offline"); }));
  render(view());
  expect(screen.getAllByText("Dashboard")).toHaveLength(2);
  expect(screen.queryByTestId("agent-expiry-notice")).not.toBeInTheDocument();
});

it("is dismissed per connection in this browser and comes back for another one", async () => {
  serve([connection()]);
  const { unmount } = render(view());
  fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));
  expect(screen.queryByTestId("agent-expiry-notice")).not.toBeInTheDocument();
  unmount();

  serve([connection()]);
  const again = render(view());
  await waitFor(() => expect(screen.getByText("Dashboard")).toBeVisible());
  expect(screen.queryByTestId("agent-expiry-notice")).not.toBeInTheDocument();
  again.unmount();

  serve([connection(), connection({ id: "grant-1", kind: "oauth_grant", client_label: "ChatGPT" })]);
  render(view());
  await waitFor(() => expect(screen.getByText(/^Your AI agent connection ChatGPT stops working on /)).toBeVisible());
});
