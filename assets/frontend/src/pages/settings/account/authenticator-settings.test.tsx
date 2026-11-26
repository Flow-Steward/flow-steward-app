import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { fetchJson } from "@/lib/http/client";
import { AuthenticatorSettings } from "@/pages/settings/account/authenticator-settings";

vi.mock("@/lib/http/client", () => ({ fetchJson: vi.fn() }));
const mockedFetchJson = vi.mocked(fetchJson);

function renderAuthenticatorSettings() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AuthenticatorSettings userId="local-user" />
    </QueryClientProvider>,
  );
}

describe("AuthenticatorSettings", () => {
  beforeEach(() => {
    mockedFetchJson.mockReset();
  });

  it("shows the setup QR and manual key until confirmation, then displays one-use recovery codes", async () => {
    const setupKey = "EXAMPLESETUPKEY";
    const uri = `otpauth://fixture/Flow%20Steward:local-user?secret=${setupKey}&issuer=Flow%20Steward`;
    mockedFetchJson
      .mockResolvedValueOnce({ enabled: false, required: false })
      .mockResolvedValueOnce({ secret: setupKey, uri })
      .mockResolvedValueOnce({ recovery_codes: ["abcd-efgh-jkmn"] });
    renderAuthenticatorSettings();

    fireEvent.click(await screen.findByRole("button", { name: "Set up authenticator" }));
    expect(screen.queryByRole("img", { name: "Authenticator setup QR code" })).not.toBeInTheDocument();
    const proof = "test-Current1!";
    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: proof } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    const qrCode = await screen.findByRole("img", { name: "Authenticator setup QR code" });
    expect(qrCode.tagName).toBe("svg");
    expect(screen.getByText(setupKey)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in an authenticator app" })).toHaveAttribute("href", uri);
    expect(screen.queryByText("abcd-efgh-jkmn")).not.toBeInTheDocument();
    expect(mockedFetchJson).toHaveBeenLastCalledWith("/api/auth/multi-factor/enroll", {
      method: "POST",
      body: JSON.stringify({ current_password: proof }),
    });

    fireEvent.change(screen.getByLabelText("Authenticator code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and enable" }));

    await waitFor(() => expect(screen.getByText("abcd-efgh-jkmn")).toBeInTheDocument());
    expect(qrCode).not.toBeInTheDocument();
    expect(screen.queryByText(setupKey)).not.toBeInTheDocument();
    expect(mockedFetchJson).toHaveBeenLastCalledWith("/api/auth/multi-factor/verify", {
      method: "POST",
      body: JSON.stringify({ code: "123456" }),
    });

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByText("abcd-efgh-jkmn")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Replace authenticator" })).toBeInTheDocument();
  });

  it("offers replacement but prevents disabling a required authenticator", async () => {
    mockedFetchJson.mockResolvedValueOnce({ enabled: true, required: true });
    renderAuthenticatorSettings();

    await waitFor(() => expect(screen.getByRole("button", { name: "Replace authenticator" })).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Disable authenticator" })).not.toBeInTheDocument();
    expect(screen.getByText("An authenticator is required for your account.")).toBeInTheDocument();
  });
});
