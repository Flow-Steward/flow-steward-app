import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { AuthProvider } from "@/lib/auth/AuthProvider";
import { useAuth } from "@/lib/auth/useAuth";

function BootstrapContract() {
  const auth = useAuth();
  if (auth.status === "loading") return null;
  return <output aria-label="Bootstrap contract">{JSON.stringify({
    permissions: auth.session?.permissions,
    environment: auth.session?.environment,
  })}</output>;
}

describe("app bootstrap contract", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    {
      auth: { authenticated: true, local_auth_enabled: true, permissions: { can_read: true, can_mutate: false } },
      environment: { edition: "community" },
    },
    { auth: { authenticated: false, local_auth_enabled: true } },
  ])("uses the read/mutate permissions and edition contract", async (bootstrap) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(bootstrap), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={queryClient}><AuthProvider><BootstrapContract /></AuthProvider></QueryClientProvider>);

    const contract = JSON.parse((await screen.findByLabelText("Bootstrap contract")).textContent || "{}");
    expect(contract).toEqual({
      permissions: { can_read: true, can_mutate: false },
      environment: { edition: "community", mode: "production" },
    });
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toBe("/api/app/bootstrap");
  });
});


describe("installation mode bootstrap projection", () => {
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    [{ edition: "private", mode: "development" }, "development"],
    [{ edition: "private", mode: "production" }, "production"],
    [{ edition: "private" }, "production"],
    [{ edition: "private", mode: "unknown" }, "production"],
    [{ edition: "private", mode: "local_development" }, "production"],
    [undefined, "production"],
  ] as const)("normalizes server mode %j to %s", async (environment, expectedMode) => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      auth: { authenticated: true, local_auth_enabled: true },
      environment,
    }), { status: 200, headers: { "content-type": "application/json" } })));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={queryClient}><AuthProvider><BootstrapContract /></AuthProvider></QueryClientProvider>);

    const contract = JSON.parse((await screen.findByLabelText("Bootstrap contract")).textContent || "{}");
    expect(contract.environment).toEqual({
      edition: environment?.edition || "community",
      mode: expectedMode,
    });
  });
});

describe("account switch", () => {
  afterEach(() => vi.unstubAllGlobals());

  function SwitchProbe() {
    const auth = useAuth();
    if (auth.status === "loading") return null;
    return (
      <>
        <output aria-label="Acting role">{(auth.session?.user?.roles || []).join(",")}</output>
        <button onClick={() => void auth.switchAccount("acct-b")} type="button">Switch</button>
      </>
    );
  }

  it("reloads the bootstrap and drops the old account's answers (ADR 0085)", async () => {
    let account = "acct-a";
    const bootstrap = () => ({
      auth: {
        authenticated: true,
        local_auth_enabled: true,
        user: {
          id: "u1", email: "p@example.com", username: "p", display_name: "P",
          roles: account === "acct-a" ? ["admin"] : ["viewer"],
          account_id: account, account_ids: ["acct-a", "acct-b"], account_memberships: [],
        },
      },
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/auth/switch-account") {
        account = "acct-b";
        return new Response(JSON.stringify({ status: "ok", account_id: "acct-b" }), {
          status: 200, headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify(bootstrap()), {
        status: 200, headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(["settings", "users", {}], { users: [{ id: "from-a" }] });

    render(<QueryClientProvider client={queryClient}><AuthProvider><SwitchProbe /></AuthProvider></QueryClientProvider>);
    await waitFor(() => expect(screen.getByLabelText("Acting role")).toHaveTextContent("admin"));

    fireEvent.click(screen.getByRole("button", { name: "Switch" }));

    await waitFor(() => expect(screen.getByLabelText("Acting role")).toHaveTextContent("viewer"));
    const urls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(urls).toEqual(["/api/app/bootstrap", "/api/auth/switch-account", "/api/app/bootstrap"]);
    expect(queryClient.getQueryData(["settings", "users", {}])).toBeUndefined();
  });
});
