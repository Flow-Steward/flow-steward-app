import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SettingsEmailPage } from "@/pages/settings/email";
import {
  clearSystemEmailSender,
  confirmSystemEmailSender,
  fetchSystemEmailSettings,
  selectSystemEmailSender,
  type SystemEmailSettings,
} from "@/pages/settings/email/api";

vi.mock("@/pages/settings/email/api", () => ({
  clearSystemEmailSender: vi.fn(),
  confirmSystemEmailSender: vi.fn(),
  fetchSystemEmailSettings: vi.fn(),
  selectSystemEmailSender: vi.fn(),
}));

const mockedFetch = vi.mocked(fetchSystemEmailSettings);
const mockedSelect = vi.mocked(selectSystemEmailSender);
const mockedConfirm = vi.mocked(confirmSystemEmailSender);
const mockedClear = vi.mocked(clearSystemEmailSender);

const CANDIDATES: SystemEmailSettings["candidates"] = [
  {
    connection_id: "conn-1", display_name: "Support inbox", connection_type: "example_mail",
    project_id: "project-1", project_name: "Operations", extension_key: "example_mail",
    extension_display_name: "Example mail", extension_install_id: "install-1",
  },
  {
    connection_id: "conn-2", display_name: "Billing inbox", connection_type: "example_mail",
    project_id: "project-2", project_name: "Finance", extension_key: "example_mail",
    extension_display_name: "Example mail", extension_install_id: "install-1",
  },
];

function settings(overrides: Partial<SystemEmailSettings> = {}): SystemEmailSettings {
  return {
    available: true,
    reason: "",
    selected: {
      connection_id: "conn-1", display_name: "Support inbox", connection_type: "example_mail",
      project_id: "project-1", extension_install_id: "install-1", source: "admin",
      confirmed_by: "admin@example.com", confirmed_at: "",
    },
    candidates: CANDIDATES,
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SettingsEmailPage />
    </QueryClientProvider>,
  );
}

describe("SettingsEmailPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("shows the selected sender and that system email is being sent", async () => {
    mockedFetch.mockResolvedValue(settings());
    renderPage();

    await waitFor(() => expect(screen.getByText("Sending system email")).toBeInTheDocument());
    expect(screen.getByText("Operations")).toBeInTheDocument();
    expect(screen.getAllByText("Support inbox").length).toBeGreaterThan(0);
    expect(screen.getByText("Example mail")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm again" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop sending system email" })).toBeInTheDocument();
  });

  it("offers Confirm again after the sender's connection changed and confirms it", async () => {
    mockedFetch.mockResolvedValue(settings({ available: false, reason: "confirmation_revoked" }));
    mockedConfirm.mockResolvedValue(settings());
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => expect(screen.getByText("The sender's connection changed since it was confirmed. Confirm it again.")).toBeInTheDocument());
    expect(screen.getByText("Not sending system email")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm again" }));

    await waitFor(() => expect(mockedConfirm).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("System email sender confirmed.")).toBeInTheDocument());
    expect(screen.getByText("Sending system email")).toBeInTheDocument();
    expect(mockedSelect).not.toHaveBeenCalled();
  });

  it("saves the chosen candidate by its connection", async () => {
    mockedFetch.mockResolvedValue(settings());
    mockedSelect.mockResolvedValue(settings({
      selected: { ...settings().selected!, connection_id: "conn-2", display_name: "Billing inbox", project_id: "project-2" },
    }));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("combobox", { name: "Sender" }));
    await user.click(await screen.findByRole("option", { name: "Finance · Billing inbox · Example mail" }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockedSelect).toHaveBeenCalledWith("conn-2"));
    await waitFor(() => expect(screen.getByText("System email sender saved.")).toBeInTheDocument());
    expect(screen.getByText("Finance")).toBeInTheDocument();
  });

  it("explains that nothing is sent when no sender is selected", async () => {
    mockedFetch.mockResolvedValue(settings({ available: false, reason: "not_selected", selected: null }));
    renderPage();

    await waitFor(() => expect(screen.getByText("No sender is selected: the platform sends no email; Forgot password? is hidden.")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Stop sending system email" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm again" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("stops sending system email after confirming", async () => {
    mockedFetch.mockResolvedValue(settings());
    mockedClear.mockResolvedValue(settings({ available: false, reason: "not_selected", selected: null }));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Stop sending system email" }));
    expect(mockedClear).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Stop sending system email" }));

    await waitFor(() => expect(mockedClear).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("No sender is selected: the platform sends no email; Forgot password? is hidden.")).toBeInTheDocument());
  });
});
