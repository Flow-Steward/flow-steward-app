import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";

import {
  acceptInvitation,
  declineInvitation,
  invitationTokenFromPath,
  previewInvitation,
} from "@/lib/auth/account-invitations";
import type * as InvitationsModule from "@/lib/auth/account-invitations";
import { useAuth } from "@/lib/auth/useAuth";
import { HttpError } from "@/lib/http/errors";
import { InvitationPage } from "@/pages/invitations";

vi.mock("@/lib/auth/useAuth", () => ({ useAuth: vi.fn() }));
vi.mock("@/lib/auth/account-invitations", async (importOriginal) => {
  const actual = await importOriginal<typeof InvitationsModule>();
  return {
    ...actual,
    acceptInvitation: vi.fn(),
    declineInvitation: vi.fn(),
    previewInvitation: vi.fn(),
  };
});

const mockedUseAuth = vi.mocked(useAuth);
const mockedPreview = vi.mocked(previewInvitation);
const mockedAccept = vi.mocked(acceptInvitation);
const mockedDecline = vi.mocked(declineInvitation);

function authState(isAuthenticated: boolean) {
  const state = {
    error: null,
    isAuthenticated,
    logout: vi.fn(async () => undefined),
    refresh: vi.fn(async () => undefined),
    switchAccount: vi.fn(async () => undefined),
    session: null,
    status: isAuthenticated ? "authenticated" : "anonymous",
  };
  return state as unknown as ReturnType<typeof useAuth>;
}

function Where() {
  const location = useLocation();
  return <div data-testid="where">{`${location.pathname}${location.search}`}</div>;
}

function renderPage(path = "/invitations/tok-1", authenticated = true) {
  const auth = authState(authenticated);
  mockedUseAuth.mockReturnValue(auth);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<InvitationPage />} path="/invitations/:token" />
          <Route element={<Where />} path="/login" />
          <Route element={<Where />} path="/" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { auth };
}

describe("InvitationPage (ADR 0085)", () => {
  beforeEach(() => {
    mockedPreview.mockReset();
    mockedAccept.mockReset();
    mockedDecline.mockReset();
  });

  it("reads the token of an invitation link path only", () => {
    expect(invitationTokenFromPath("/invitations/abc_DEF-1")).toBe("abc_DEF-1");
    expect(invitationTokenFromPath("/invitations/abc?x=1")).toBe("abc");
    expect(invitationTokenFromPath("/settings/security")).toBe("");
    expect(invitationTokenFromPath("/invitations/a/b")).toBe("");
    expect(invitationTokenFromPath(null)).toBe("");
  });

  it("sends a signed-out visitor to sign in first, back to the link", () => {
    renderPage("/invitations/tok-1", false);

    expect(screen.getByTestId("where")).toHaveTextContent(`/login?next=${encodeURIComponent("/invitations/tok-1")}`);
    expect(mockedPreview).not.toHaveBeenCalled();
  });

  it("shows the company, the role and the inviter, and joins with the role shown", async () => {
    mockedPreview.mockResolvedValue({ accountName: "Northwind", role: "operator", invitedBy: "Bob", expiresAt: "" });
    mockedAccept.mockResolvedValue({ status: "accepted", accountId: "acct-n", accountName: "Northwind", role: "operator", signInAgain: false });
    const { auth } = renderPage();

    await waitFor(() => expect(screen.getByRole("heading", { name: "Join Northwind?" })).toBeInTheDocument());
    expect(screen.getByText("Bob")).toBeInTheDocument();
    expect(mockedPreview).toHaveBeenCalledWith("tok-1");
    fireEvent.click(screen.getByRole("button", { name: "Join Northwind as Operator" }));

    await waitFor(() => expect(mockedAccept).toHaveBeenCalledWith("tok-1", "operator"));
    await waitFor(() => expect(auth.switchAccount).toHaveBeenCalledWith("acct-n"));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/"));
  });

  it("still lands at home when the session cannot switch accounts", async () => {
    mockedPreview.mockResolvedValue({ accountName: "Northwind", role: "viewer", invitedBy: "", expiresAt: "" });
    mockedAccept.mockResolvedValue({ status: "accepted", accountId: "acct-n", accountName: "Northwind", role: "viewer", signInAgain: false });
    const { auth } = renderPage();
    vi.mocked(auth.switchAccount).mockRejectedValueOnce(new Error("pinned"));

    fireEvent.click(await screen.findByRole("button", { name: "Join Northwind as Viewer" }));

    await waitFor(() => expect(auth.refresh).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/"));
  });

  it("sends a newcomer moved into the company to sign in again, saying so", async () => {
    mockedPreview.mockResolvedValue({ accountName: "Northwind", role: "viewer", invitedBy: "", expiresAt: "" });
    mockedAccept.mockResolvedValue({ status: "accepted", accountId: "acct-n", accountName: "Northwind", role: "viewer", signInAgain: true });
    const { auth } = renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Join Northwind as Viewer" }));

    await waitFor(() =>
      expect(screen.getByTestId("where")).toHaveTextContent(
        `/login?flash=${encodeURIComponent("You joined Northwind. Sign in again to open it.")}`,
      ),
    );
    expect(auth.refresh).toHaveBeenCalled();
    expect(auth.switchAccount).not.toHaveBeenCalled();
  });

  it("declines without joining", async () => {
    mockedPreview.mockResolvedValue({ accountName: "Northwind", role: "viewer", invitedBy: "", expiresAt: "" });
    mockedDecline.mockResolvedValue({ status: "declined", accountId: "acct-n", accountName: "Northwind", role: "viewer", signInAgain: false });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Decline" }));

    await waitFor(() => expect(screen.getByRole("heading", { name: "Invitation declined" })).toBeInTheDocument());
    expect(mockedDecline).toHaveBeenCalledWith("tok-1");
    expect(mockedAccept).not.toHaveBeenCalled();
  });

  it("says plainly when the link is no longer valid", async () => {
    mockedPreview.mockRejectedValue(new HttpError({ status: 404, url: "/api/auth/invitations/preview", payload: { error: "This invitation is no longer valid" } }));
    renderPage();

    await waitFor(() => expect(screen.getByRole("heading", { name: "This invitation is no longer valid" })).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Join/ })).not.toBeInTheDocument();
  });

  it("refuses to join when the invitation changed after it was shown", async () => {
    mockedPreview.mockResolvedValue({ accountName: "Northwind", role: "viewer", invitedBy: "", expiresAt: "" });
    mockedAccept.mockRejectedValue(new HttpError({ status: 409, url: "/api/auth/invitations/accept", payload: { error: "changed" } }));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Join Northwind as Viewer" }));

    await waitFor(() => expect(screen.getByText("This invitation has changed")).toBeInTheDocument());
  });
});
