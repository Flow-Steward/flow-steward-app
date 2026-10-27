import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { useAuth } from "@/lib/auth/useAuth";
import { fetchJson } from "@/lib/http/client";
import { BootstrapPage } from "@/pages/auth/bootstrap";

vi.mock("@/lib/auth/useAuth", () => ({ useAuth: vi.fn() }));
vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));

const mockedUseAuth = vi.mocked(useAuth);
const mockedFetchJson = vi.mocked(fetchJson);

describe("first-admin setup token handoff", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
    mockedFetchJson.mockResolvedValue({ status: "bootstrapped" });
    mockedUseAuth.mockReturnValue({
      error: null,
      isAuthenticated: false,
      logout: vi.fn(async () => undefined),
      refresh: vi.fn(async () => undefined),
      switchAccount: vi.fn(async () => undefined),
      session: {
        authenticated: false,
        local_auth_enabled: true,
        bootstrap_required: true,
        external_auth_enabled: false,
        auth_source: "",
        roles: [],
        permissions: {},
        account_memberships: [],
        i18n: { locale: "en", fallback_locale: "en", supported_locales: [], messages: {} },
        feature_flags: {},
        environment: { edition: "community" },
        settings_pages: [],
        registered_pages: [],
        shell_navigation: {},
      },
      status: "anonymous",
    } as ReturnType<typeof useAuth>);
    window.history.replaceState({}, "", "/bootstrap#setup-token=one-time-secret");
  });

  it("removes the fragment immediately and submits the token only as a header", async () => {
    const localSet = vi.spyOn(window.localStorage, "setItem");
    const sessionSet = vi.spyOn(window.sessionStorage, "setItem");
    render(
      <MemoryRouter initialEntries={["/bootstrap"]}>
        <BootstrapPage />
      </MemoryRouter>,
    );

    await waitFor(() => expect(window.location.hash).toBe(""));
    expect(screen.getByRole("textbox", { name: "Display name (optional)" })).not.toBeRequired();
    fireEvent.change(screen.getByLabelText("Admin email"), {
      target: { value: "admin@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "VeryStrongPassword1!" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "VeryStrongPassword1!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create admin user" }));

    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledTimes(1));
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/auth/bootstrap", {
      method: "POST",
      headers: { "x-platform-setup-token": "one-time-secret" },
      body: JSON.stringify({
        email: "admin@example.com",
        password: "VeryStrongPassword1!",
      }),
    });
    expect(localSet).not.toHaveBeenCalled();
    expect(sessionSet).not.toHaveBeenCalled();
    expect(screen.queryByTestId("bootstrap-setup-token-notice")).not.toBeInTheDocument();
  });

  it("says the token is missing and offers a way back instead of a dead form", async () => {
    // AC17-R46: the fragment is wiped as soon as it is read, so a reload arrives here.
    // Before this the form rendered in full, accepted everything, and only failed on
    // submit with "Invalid bootstrap setup token".
    window.history.replaceState({}, "", "/bootstrap");
    const localSet = vi.spyOn(window.localStorage, "setItem");
    const sessionSet = vi.spyOn(window.sessionStorage, "setItem");
    render(
      <MemoryRouter initialEntries={["/bootstrap"]}>
        <BootstrapPage />
      </MemoryRouter>,
    );

    const notice = await screen.findByTestId("bootstrap-setup-token-notice");
    expect(notice).toBeInTheDocument();
    // it must not disclose whether a token is actually required for this installation
    expect(notice.textContent ?? "").not.toMatch(/required|not required|no token exists/i);

    fireEvent.change(screen.getByTestId("bootstrap-setup-token-input"), {
      target: { value: "recovered-secret" },
    });
    fireEvent.change(screen.getByLabelText("Admin email"), {
      target: { value: "admin@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "VeryStrongPassword1!" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "VeryStrongPassword1!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create admin user" }));

    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledTimes(1));
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/auth/bootstrap", {
      method: "POST",
      headers: { "x-platform-setup-token": "recovered-secret" },
      body: JSON.stringify({
        email: "admin@example.com",
        password: "VeryStrongPassword1!",
      }),
    });
    // the recovered token never reaches browser storage or the address bar
    expect(localSet).not.toHaveBeenCalled();
    expect(sessionSet).not.toHaveBeenCalled();
    expect(window.location.hash).toBe("");
  });

  it("reads a token that arrives as a fragment change on an already-open page", async () => {
    // AC17-R48: the owner pasted the printed link into the address bar of a tab that was
    // already showing /bootstrap. Only the fragment changed, so the document was not
    // reloaded and the page was not remounted. It kept insisting the token was absent
    // while the address bar displayed it, and the fragment was never cleared.
    window.history.replaceState({}, "", "/bootstrap");
    render(
      <MemoryRouter initialEntries={["/bootstrap"]}>
        <BootstrapPage />
      </MemoryRouter>,
    );
    await screen.findByTestId("bootstrap-setup-token-notice");

    window.history.replaceState({}, "", "/bootstrap#setup-token=pasted-into-address-bar");
    fireEvent(window, new HashChangeEvent("hashchange"));

    // the page stops contradicting its own address: notice gone, fragment gone
    await waitFor(() =>
      expect(screen.queryByTestId("bootstrap-setup-token-notice")).not.toBeInTheDocument(),
    );
    expect(window.location.hash).toBe("");

    fireEvent.change(screen.getByLabelText("Admin email"), {
      target: { value: "admin@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "VeryStrongPassword1!" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "VeryStrongPassword1!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create admin user" }));

    await waitFor(() => expect(mockedFetchJson).toHaveBeenCalledTimes(1));
    expect(mockedFetchJson).toHaveBeenCalledWith("/api/auth/bootstrap", {
      method: "POST",
      headers: { "x-platform-setup-token": "pasted-into-address-bar" },
      body: JSON.stringify({
        email: "admin@example.com",
        password: "VeryStrongPassword1!",
      }),
    });
  });

  it("clears the fragment again when the same link is pasted a second time", async () => {
    // The second paste carries a token the page already holds, so the state value does not
    // change and no re-render follows. The address bar must still be cleaned.
    render(
      <MemoryRouter initialEntries={["/bootstrap"]}>
        <BootstrapPage />
      </MemoryRouter>,
    );
    await waitFor(() => expect(window.location.hash).toBe(""));

    window.history.replaceState({}, "", "/bootstrap#setup-token=one-time-secret");
    fireEvent(window, new HashChangeEvent("hashchange"));

    await waitFor(() => expect(window.location.hash).toBe(""));
    expect(screen.queryByTestId("bootstrap-setup-token-notice")).not.toBeInTheDocument();
  });
});
