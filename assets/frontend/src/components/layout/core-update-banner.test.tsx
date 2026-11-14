import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AuthContext, type AuthContextValue } from "@/lib/auth/context";
import { CoreUpdateBanner } from "./core-update-banner";

const notice = {
  status: "update_available", current_version: "0.9.17", latest_version: "0.9.18",
  release_url: "https://github.com/Flow-Steward/flow-steward-app/releases/tag/v0.9.18",
};

function view(role: string | null, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return <AuthContext.Provider value={{ session: role ? { roles: [role], user: { roles: [role] } } : null } as AuthContextValue}>
    <QueryClientProvider client={client}><p>Dashboard</p><CoreUpdateBanner /></QueryClientProvider>
  </AuthContext.Provider>;
}

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

it("shows the newer Core version, installed version and official release notes to an administrator", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json(notice)));
  render(view("admin"));
  await waitFor(() => expect(screen.getByText("Flow Steward 0.9.18 is available")).toBeVisible());
  expect(screen.getByText("Installed version: 0.9.17")).toBeVisible();
  expect(screen.getByRole("link", { name: "Release notes" })).toHaveAttribute("href", notice.release_url);
  expect(screen.queryByRole("button", { name: /upgrade|update/i })).not.toBeInTheDocument();
});

it.each(["operator", "viewer", null])("does not check or show Core updates for %s", (role) => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  render(view(role));
  expect(fetch).not.toHaveBeenCalled();
  expect(screen.queryByRole("link", { name: "Release notes" })).not.toBeInTheDocument();
});

it.each(["current", "unavailable"])("keeps the interface quiet for %s checks", async (status) => {
  const fetch = vi.fn(async () => Response.json({ ...notice, status }));
  vi.stubGlobal("fetch", fetch);
  render(view("admin"));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("link", { name: "Release notes" })).not.toBeInTheDocument();
});

it("keeps navigation usable when the release check fails", async () => {
  const fetch = vi.fn(async () => { throw new TypeError("Offline"); });
  vi.stubGlobal("fetch", fetch);
  render(view("admin"));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(screen.getByText("Dashboard")).toBeVisible();
  expect(screen.queryByRole("link", { name: "Release notes" })).not.toBeInTheDocument();
});

it("hides an already cached notice immediately after administrator access is removed", async () => {
  const fetch = vi.fn(async () => Response.json(notice));
  vi.stubGlobal("fetch", fetch);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { rerender } = render(view("admin", client));
  await waitFor(() => expect(screen.getByRole("link", { name: "Release notes" })).toBeVisible());
  rerender(view("operator", client));
  expect(screen.queryByRole("link", { name: "Release notes" })).not.toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("does not render a release link outside the official Core repository", async () => {
  const fetch = vi.fn(async () => Response.json({ ...notice, release_url: "javascript:alert(1)" }));
  vi.stubGlobal("fetch", fetch);
  render(view("admin"));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("link", { name: "Release notes" })).not.toBeInTheDocument();
});

it("checks again every five minutes without retrying failed requests", async () => {
  const fetch = vi.fn(async () => Response.json(notice));
  vi.stubGlobal("fetch", fetch);
  vi.useFakeTimers();
  render(view("admin"));
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000); });
  expect(fetch).toHaveBeenCalledTimes(2);
});
