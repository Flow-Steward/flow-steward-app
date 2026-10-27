import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { SettingsRetentionPage } from "@/pages/settings/retention";
import { I18nContext, fallbackContext } from "@/lib/i18n/context";
import { fetchRetentionSettingsRecord, saveRetentionSettingsRecord } from "@/pages/settings/shared/api";

vi.mock("@/pages/settings/shared/api", () => ({
  fetchRetentionSettingsRecord: vi.fn(),
  saveRetentionSettingsRecord: vi.fn(),
}));

const mockedFetch = vi.mocked(fetchRetentionSettingsRecord);
const mockedSave = vi.mocked(saveRetentionSettingsRecord);

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <SettingsRetentionPage />
    </QueryClientProvider>,
  );
}

describe("SettingsRetentionPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedFetch.mockResolvedValue({
      available: true,
      defaults: {
        action_logs_days: 30,
        job_logs_days: 30,
        trash_days: 30,
        terminal_job_checkpoints_days: 30,
        revisions_keep: 5,
      },
      settings: {
        action_logs_days: 14,
        job_logs_days: 30,
        trash_days: 7,
        terminal_job_checkpoints_days: 30,
        revisions_keep: 12,
      },
    });
    mockedSave.mockResolvedValue({ status: "saved" });
  });

  it("shows how many revisions each item keeps and saves a new count", async () => {
    renderPage();

    const revisions = await screen.findByLabelText("Revisions kept per item");
    await waitFor(() => expect(revisions).toHaveValue(12));
    expect(screen.getByText(/deleted for good/)).toBeInTheDocument();

    fireEvent.change(revisions, { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Save retention settings" }));

    await waitFor(() => expect(mockedSave).toHaveBeenCalledTimes(1));
    expect(mockedSave).toHaveBeenCalledWith({
      settings: {
        action_logs_days: 14,
        job_logs_days: 30,
        trash_days: 7,
        terminal_job_checkpoints_days: 30,
        revisions_keep: 3,
      },
    });
  });

  it("refuses a revision count of zero", async () => {
    renderPage();

    const revisions = await screen.findByLabelText("Revisions kept per item");
    await waitFor(() => expect(revisions).toHaveValue(12));
    fireEvent.change(revisions, { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save retention settings" }));

    expect(await screen.findByText("All retention values must be whole numbers greater than 0.")).toBeInTheDocument();
    expect(mockedSave).not.toHaveBeenCalled();
  });

  it("announces a successful save as success after translating its message", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <I18nContext.Provider value={{ ...fallbackContext, locale: "lt", t: (key, fallback = "") => key === "settings.retention.saved" ? "Saugojimo nustatymai išsaugoti." : fallback || key }}>
        <QueryClientProvider client={queryClient}><SettingsRetentionPage /></QueryClientProvider>
      </I18nContext.Provider>,
    );
    const revisions = await screen.findByLabelText("Revisions kept per item");
    await waitFor(() => expect(revisions).toHaveValue(12));

    fireEvent.click(screen.getByRole("button", { name: "Save retention settings" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Saugojimo nustatymai išsaugoti.");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
